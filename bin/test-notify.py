#!/usr/bin/env python3
"""Offline tests for bin/notify.py and board.py's Discord hooks. No network: notify.send
is replaced with a recorder, and board.py's GitHub calls are stubbed.

    python3 bin/test-notify.py      # exit 0 = all cases hold
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.example/webhook"
os.environ.pop("DISCORD_MENTION", None)
# Slack stays off for every case except section 8, which turns it on and off by hand —
# cleared here rather than inherited, same reason as OWNER_LOGIN/BOARD_OWNER below.
os.environ.pop("SLACK_WEBHOOK_URL", None)
os.environ.pop("SLACK_MENTION", None)
# This suite's own default (section 2's "Needs the owner:") assumes neither is set — true for a
# real lane's own tick only when its project never sets OWNER_LOGIN/BOARD_OWNER, but NOT
# true of the shell this suite happens to run in (this box's own watcher lane exports
# BOARD_OWNER=roloc for itself), so it is cleared here rather than inherited.
os.environ.pop("OWNER_LOGIN", None)
os.environ.pop("BOARD_OWNER", None)

import notify  # noqa: E402
import board   # noqa: E402
# board.py requires OWNER lazily, only where it is actually used (card #157) — a move to
# Stuck below calls announce(), which needs one for the board-URL it posts to Discord.
# Set directly on the module, never through BOARD_OWNER in the environment: this suite
# tests notify.py's own "Needs the owner:" fallback, which reads OWNER_LOGIN/BOARD_OWNER live
# at call time, and those stay unset.
board.OWNER = "test-owner"

CARD = {"number": 87, "title": "Add speaker names", "url": "https://github.com/o/r/issues/87"}
PR = {"url": "https://github.com/o/r/pull/90", "state": "OPEN"}

sent = []
notify.send = lambda url, payload, transport="discord": sent.append((url, payload, transport)) or True

fails = 0
count = 0


def check(name, cond, detail=""):
    global fails, count
    count += 1
    print(f"  {name:<78} {'ok' if cond else 'FAIL ' + detail}")
    if not cond:
        fails = 1


def last():
    return sent[-1][1]


def embed():
    return last()["embeds"][0]


# --- notify.build shapes -------------------------------------------------------------
print("1. every kind names the card and links to it")
for kind in ("stuck", "released", "breach"):
    sent.clear()
    notify.post(kind, CARD, detail="Building", pr=PR, board_url="https://github.com/users/o/projects/1")
    p = last()
    check(f"{kind}: content names #87", "#87 Add speaker names" in p["content"])
    check(f"{kind}: embed title is the card, url is the issue",
          embed()["title"] == "#87 Add speaker names" and embed()["url"] == CARD["url"])
    check(f"{kind}: PR link included when known", PR["url"] in embed()["description"])

print("2. the Stuck alert")
sent.clear()
notify.post("stuck", CARD, detail="Needs the owner: pick where speaker names live.",
            board_url="https://github.com/users/o/projects/1")
check("first line is the ask", last()["content"].startswith("🚨 **Needs the owner:**"))
check("the ask from the card is quoted", "pick where speaker names live" in embed()["description"])
check("says how to answer", "reply **A**, **B** or **C**" in embed()["description"])
check("links the board too", "https://github.com/users/o/projects/1" in embed()["description"])
check("red", embed()["color"] == notify.RED)
sent.clear()
notify.post("stuck", CARD)
check("no ask on file → points at the card's last comment",
      "Read the last comment on the card." in embed()["description"])
os.environ["DISCORD_MENTION"] = "<@123>"
sent.clear()
notify.post("stuck", CARD)
check("DISCORD_MENTION is prepended to the alert", last()["content"].startswith("<@123> 🚨"))
for kind in ("released", "breach", "digest"):
    sent.clear()
    notify.post(kind, CARD)
    check(f"...but not to '{kind}': broken is a line, stuck is the ping",
          "<@123>" not in last()["content"])
del os.environ["DISCORD_MENTION"]

print("2b. the Stuck alert names a GitHub login, not the word Steve, once configured")
os.environ["OWNER_LOGIN"] = "octocat"
sent.clear()
notify.post("stuck", CARD, detail="Needs @octocat: pick where speaker names live.",
            board_url="https://github.com/users/o/projects/1")
check("OWNER_LOGIN=octocat → the alert's own line names that login",
      last()["content"].startswith("🚨 **Needs @octocat:**"))
del os.environ["OWNER_LOGIN"]
os.environ["BOARD_OWNER"] = "someorg"
sent.clear()
notify.post("stuck", CARD)
check("no OWNER_LOGIN, but BOARD_OWNER set → falls back to it",
      last()["content"].startswith("🚨 **Needs @someorg:**"))
del os.environ["BOARD_OWNER"]
sent.clear()
notify.post("stuck", CARD)
check("neither set → 'Needs the owner:', never the bare word 'Steve' (card #179)",
      last()["content"].startswith("🚨 **Needs the owner:**"))

print("3. the two 'something broke' posts")
sent.clear()
notify.post("released", CARD, detail="The run ended after 12 min without finishing.")
check("a died run: says the run ended, carries the spawner's line, orange",
      "the run ended without finishing" in last()["content"]
      and "12 min" in embed()["description"] and embed()["color"] == notify.ORANGE)
sent.clear()
notify.post("breach", CARD, detail="Health probes failing: now 1, averaging 0.7500.")
check("a product breach: says something broke, carries the detector's line, orange",
      "Something broke" in last()["content"]
      and "averaging 0.7500" in embed()["description"] and embed()["color"] == notify.ORANGE)

print("3b. the Monday digest: no single card behind it, so the text is the whole message")
DIGEST_TEXT = ("🤖 **FYI:** the loop's numbers for the week just ended.\n"
               "- **riffmap**: **12** ticks (9), **82%** first-pass (75%), **2** parked (3), "
               "**46** median turns (51), **0** deaths (1)")
sent.clear()
notify.post("digest", {"number": 0, "title": "Weekly digest", "url": ""}, detail=DIGEST_TEXT)
check("neutral color: a report, not an alert and not something broken",
      embed()["color"] == notify.NEUTRAL)
check("the digest text lives in the embed description, verbatim",
      embed()["description"] == DIGEST_TEXT)
check("content stays short — the digest's own bullets never get truncated by the 1900 cut",
      len(last()["content"]) < 50)
sent.clear()
notify.post("digest", {"number": 0, "title": "Weekly digest", "url": ""})
check("no text on file -> says there were no ticks, rather than an empty embed",
      "No ticks this week" in embed()["description"])
sent.clear()
notify.post("digest", {"number": 0, "title": "Weekly digest", "url": ""}, detail="x" * 5000)
check("the embed description is capped at Discord's own 4096 limit, same as content's 1900",
      len(embed()["description"]) == 4096)

print("4. off switch, and everything that is not stuck or broken")
saved = os.environ.pop("DISCORD_WEBHOOK_URL")
sent.clear()
check("no webhook → not enabled, nothing sent", notify.post("stuck", CARD) is False and not sent)
os.environ["DISCORD_WEBHOOK_URL"] = saved
check("an unknown kind is not posted", notify.post("bogus", CARD) is False and not sent)
for kind in ("status", "stage", "shipped", "closed"):
    check(f"'{kind}' is no longer a post: the floor page shows it",
          notify.build(kind, CARD, detail="Building", pr=PR) is None
          and notify.post(kind, CARD, detail="Building", pr=PR) is False and not sent)
check("an unknown card gets no empty embed url",
      "url" not in notify.build("released", {"number": 5, "title": "", "url": ""})["embeds"][0])
check("content stays under Discord's limit",
      len(notify.build("stuck", {"number": 1, "title": "x" * 3000, "url": ""}, "ask")["content"]) <= 2000)

# --- board.py hooks ------------------------------------------------------------------
print("5. board.py: only a move to Stuck posts; every other move and every stage is silent")
calls = []
pr_lookups = []
board.set_option = lambda issue, field, value: (calls.append((field, value)) or (value, dict(CARD)))
board.pr_for = lambda issue: pr_lookups.append(issue) or PR
board.needs_steve_ask = lambda issue: "Needs Steve: pick where speaker names live."

for column in ("Backlog", "Ready", "In progress", "In review", "Done"):
    sent.clear(); calls.clear(); pr_lookups.clear()
    board.cmd_move("87", column)
    check(f"move → {column}: the board changes, Discord hears nothing",
          calls == [("Status", column)] and not sent)
    check(f"move → {column}: no PR lookup spent on a post that is not sent", not pr_lookups)
sent.clear(); board.cmd_move("87", "Stuck - Needs help")
check("move to Stuck → the alert leads with 'Needs the owner' (no login configured here) "
      "and quotes the card's own (old-format) Needs Steve comment in the body",
      len(sent) == 1 and "Needs the owner" in last()["content"] and "speaker names" in embed()["description"])
check("the Stuck alert carries the PR link", PR["url"] in embed()["description"])
sent.clear(); board.cmd_move("87", "Stuck - Needs help", "--why", "This card failed 2 attempts, so I parked it.")
check("--why overrides the comment lookup (the spawner's park)",
      "failed 2 attempts" in embed()["description"] and "speaker names" not in embed()["description"])
for stage in ("Claimed", "Designing", "Building", "Testing", "Reviewing", "PR open", "Merging", "Parked"):
    sent.clear(); calls.clear()
    board.cmd_stage("87", stage)
    check(f"stage {stage}: the board changes, Discord hears nothing",
          calls == [("Stage", stage)] and not sent)
try:
    board.cmd_move("87", "In review", "--bogus")
    check("move rejects stray arguments", False)
except SystemExit as e:
    check("move rejects stray arguments", "unexpected arguments" in str(e))

print("6. board.py notify: a died run and a product breach, and nothing else")
board.items = lambda fresh=False: [dict(CARD, state="OPEN", status="In progress")]
sent.clear(); board.cmd_notify("87", "released", "The run ended after 12 min without finishing.")
check("released carries the spawner's detail", len(sent) == 1 and "12 min" in embed()["description"])
check("released names the card and links it",
      "#87 Add speaker names" in last()["content"] and embed()["url"] == CARD["url"])
sent.clear(); board.cmd_notify("87", "breach", "Health probes failing: now 1, averaging 0.7500.")
check("breach carries the detector's detail",
      len(sent) == 1 and "Something broke" in last()["content"] and "0.7500" in embed()["description"])


def board_down(fresh=False):
    sys.exit("board.py: gh failed: outage")


board.items = board_down
sent.clear()
check("a board outage does not stop the post; it goes out without a title",
      board.cmd_notify("87", "released", "died") == 0 and len(sent) == 1 and "#87" in last()["content"])
for kind in ("shipped", "closed", "bogus"):
    sent.clear()
    try:
        board.cmd_notify("87", kind)
        check(f"notify {kind} exits 1: not an event any more", False)
    except SystemExit as e:
        check(f"notify {kind} exits 1: not an event any more", "unknown event" in str(e) and not sent)
os.environ.pop("DISCORD_WEBHOOK_URL")
sent.clear()
check("notify with no webhook is a silent no-op, exit 0", board.cmd_notify("87", "released") == 0 and not sent)

print("7. board.py sweep-stages: a closed card does not keep its Stage")
os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.example/webhook"
# STAGE_SWEEP_SEC=0: this section calls cmd_sweep_stages() several times in a row to check
# the clearing logic itself, never the board-level interval gate (card #114; that gate has
# its own case in test-board.py) — 0 means "no gating, run every call" for board.py's stamp
# the same way it already does for spawn.sh's own per-lane one.
os.environ["STAGE_SWEEP_SEC"] = "0"


def card(n, state, stage):
    return {"item": f"I{n}", "number": n, "title": "", "url": "", "state": state,
            "labels": [], "status": "Done" if state == "CLOSED" else "Stuck - Needs help",
            "priority": None, "stage": stage}


cleared, looked_up = [], []
board.gh = lambda q, **v: cleared.append(v["i"]) or {}
board.project = lambda: (looked_up.append(1) or
                         ({"id": "P"}, {"Stage": {"id": "F", "name": "Stage", "options": []}}))
board.items = lambda fresh=False: [card(35, "CLOSED", "Parked"), card(36, "CLOSED", None),
                       card(37, "OPEN", "Parked"), card(38, "CLOSED", "Merging")]
sent.clear()
check("exit 0", board.cmd_sweep_stages() == 0)
check("every closed card with a Stage is emptied, whatever the Stage", cleared == ["I35", "I38"],
      repr(cleared))
check("an open card keeps its Stage (a parked card is still Parked)", "I37" not in cleared)
check("no Discord post", not sent)
cleared.clear(); looked_up.clear()
board.items = lambda fresh=False: [card(36, "CLOSED", None), card(37, "OPEN", "Parked")]
check("nothing stale → nothing cleared, and no second lookup",
      board.cmd_sweep_stages() == 0 and not cleared and not looked_up)
board.items = lambda fresh=False: [card(n, "CLOSED", "Merging") for n in range(100, 100 + board.SWEEP_MAX + 5)]
board.cmd_sweep_stages()
check("one run empties at most SWEEP_MAX; the rest wait for the next",
      len(cleared) == board.SWEEP_MAX, str(len(cleared)))
cleared.clear()
board.project = lambda: ({"id": "P"}, {})
board.items = lambda fresh=False: [card(35, "CLOSED", "Parked")]
check("a board with no Stage field is a no-op", board.cmd_sweep_stages() == 0 and not cleared)

print("8. Slack: the same three posts, picked by which webhook is set (card #159)")
os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/services/T000/B000/xxxx"
for kind in ("stuck", "released", "breach"):
    sent.clear()
    notify.post(kind, CARD, detail="Building", pr=PR, board_url="https://github.com/users/o/projects/1")
    check(f"{kind}: both webhooks set -> posts to both, discord first",
          [s[2] for s in sent] == ["discord", "slack"])
    slack = sent[1][1]
    check(f"{kind}: slack payload carries text and one mrkdwn section block",
          "text" in slack and slack["blocks"][0]["text"]["type"] == "mrkdwn")
    block = slack["blocks"][0]["text"]["text"]
    check(f"{kind}: the card link and the ask share the one block",
          CARD["url"] in block and "Building" in block)

sent.clear()
notify.post("stuck", CARD, detail="Needs the owner: pick where speaker names live.",
            board_url="https://github.com/users/o/projects/1")
slack = sent[1][1]
block = slack["blocks"][0]["text"]["text"]
check("stuck: slack's top line leads with the same ask Discord's content carries",
      slack["text"].startswith("🚨 *Needs the owner:*"))
check("stuck: the ask from the card is quoted in the block", "pick where speaker names live" in block)
os.environ["SLACK_MENTION"] = "<!here>"
sent.clear()
notify.post("stuck", CARD)
check("SLACK_MENTION is prepended to the Stuck alert, same as DISCORD_MENTION",
      sent[1][1]["text"].startswith("<!here> 🚨"))
sent.clear()
notify.post("released", CARD)
check("...but not to 'released': Slack mirrors Discord's own rule", "<!here>" not in sent[1][1]["text"])
del os.environ["SLACK_MENTION"]

sent.clear()
notify.post("digest", {"number": 0, "title": "Weekly digest", "url": ""}, detail=DIGEST_TEXT)
check("digest: posts to slack too", len(sent) == 2 and sent[1][2] == "slack")
check("digest: the whole message lands in the one block, verbatim markdown aside",
      notify._slack_mrkdwn(DIGEST_TEXT) in sent[1][1]["blocks"][0]["text"]["text"])

sent.clear()
weird = dict(CARD, title="A <script> & B > C")
notify.post("released", weird)
block = sent[1][1]["blocks"][0]["text"]["text"]
check("a title with '&', '<', '>' is escaped, not left to break the Slack link",
      "<script>" not in block and "&lt;script&gt;" in block and "&amp;" in block)

print("8b. Slack on its own, and both off together")
saved_discord = os.environ.pop("DISCORD_WEBHOOK_URL")
sent.clear()
notify.post("stuck", CARD)
check("Discord unset, Slack set: only slack posts", [s[2] for s in sent] == ["slack"])
del os.environ["SLACK_WEBHOOK_URL"]
sent.clear()
check("neither set: not enabled, nothing sent anywhere",
      notify.enabled() is False and notify.post("stuck", CARD) is False and not sent)
os.environ["DISCORD_WEBHOOK_URL"] = saved_discord

print(f"{count} cases, {'all passed' if not fails else 'FAILURES'}")
sys.exit(fails)
