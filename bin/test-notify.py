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

import notify  # noqa: E402
import board   # noqa: E402

CARD = {"number": 87, "title": "Add speaker names", "url": "https://github.com/o/r/issues/87"}
PR = {"url": "https://github.com/o/r/pull/90", "state": "OPEN"}

sent = []
notify.send = lambda url, payload: sent.append((url, payload)) or True

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
for kind in ("status", "stage", "stuck", "shipped", "closed", "released"):
    sent.clear()
    notify.post(kind, CARD, detail="Building", pr=PR, board_url="https://github.com/users/o/projects/1")
    p = last()
    check(f"{kind}: content names #87", "#87 Add speaker names" in p["content"])
    check(f"{kind}: embed title is the card, url is the issue",
          embed()["title"] == "#87 Add speaker names" and embed()["url"] == CARD["url"])
    check(f"{kind}: PR link included when known", PR["url"] in embed()["description"])

print("2. the Stuck alert")
sent.clear()
notify.post("stuck", CARD, detail="Needs Steve: pick where speaker names live.",
            board_url="https://github.com/users/o/projects/1")
check("first line is the ask", last()["content"].startswith("🚨 **Needs Steve:**"))
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
sent.clear()
notify.post("status", CARD, detail="In review")
check("...but not to an ordinary status move", "<@123>" not in last()["content"])
del os.environ["DISCORD_MENTION"]

print("3. off switch and unknown kinds")
saved = os.environ.pop("DISCORD_WEBHOOK_URL")
sent.clear()
check("no webhook → not enabled, nothing sent", notify.post("stuck", CARD) is False and not sent)
os.environ["DISCORD_WEBHOOK_URL"] = saved
check("an unknown kind is not posted", notify.post("bogus", CARD) is False and not sent)
check("an unknown card gets no empty embed url",
      "url" not in notify.build("released", {"number": 5, "title": "", "url": ""})["embeds"][0])
check("content stays under Discord's limit",
      len(notify.build("status", {"number": 1, "title": "x" * 3000, "url": ""}, "In review")["content"]) <= 2000)

# --- board.py hooks ------------------------------------------------------------------
print("4. board.py: move and stage post after the change lands")
calls = []
board.set_option = lambda issue, field, value: (calls.append((field, value)) or (value, dict(CARD)))
board.pr_for = lambda issue: PR
board.needs_steve_ask = lambda issue: "Needs Steve: pick where speaker names live."

sent.clear(); board.cmd_move("87", "In review")
check("move → a 'status' post naming the new column", "**In review**" in last()["content"])
check("In review carries the PR link", PR["url"] in embed()["description"])
sent.clear(); board.cmd_move("87", "Stuck - Needs help")
check("move to Stuck → the alert, quoting the card's Needs Steve comment",
      "Needs Steve" in last()["content"] and "speaker names" in embed()["description"])
sent.clear(); board.cmd_move("87", "Stuck - Needs help", "--why", "This card failed 2 attempts, so I parked it.")
check("--why overrides the comment lookup (the spawner's park)",
      "failed 2 attempts" in embed()["description"] and "speaker names" not in embed()["description"])
sent.clear(); board.cmd_stage("87", "Building")
check("stage → a 'stage' post", "**Building**" in last()["content"] and embed()["color"] == notify.GRAY)
sent.clear(); board.cmd_stage("87", "Parked")
check("stage Parked posts nothing (the Stuck move already did)", not sent)
sent.clear(); board.cmd_stage("87", "PR open")
check("PR open carries the PR link", PR["url"] in embed()["description"])
try:
    board.cmd_move("87", "In review", "--bogus")
    check("move rejects stray arguments", False)
except SystemExit as e:
    check("move rejects stray arguments", "unexpected arguments" in str(e))

print("5. board.py notify: the spawner's events")
board.items = lambda: [dict(CARD, state="CLOSED", status="Done")]
board.pr_for = lambda issue: {"url": PR["url"], "state": "MERGED"}
sent.clear(); board.cmd_notify("87", "shipped")
check("shipped with a merged PR → green 'shipped'", "shipped" in last()["content"] and embed()["color"] == notify.GREEN)
board.pr_for = lambda issue: None
sent.clear(); board.cmd_notify("87", "shipped")
check("shipped with no merged PR → 'closed without a merge'", "closed without a merge" in last()["content"])
sent.clear(); board.cmd_notify("87", "released", "The run ended after 12 min without finishing.")
check("released carries the spawner's detail", "12 min" in embed()["description"])
try:
    board.cmd_notify("87", "bogus")
    check("unknown event exits 1", False)
except SystemExit as e:
    check("unknown event exits 1", "unknown event" in str(e))
os.environ.pop("DISCORD_WEBHOOK_URL")
sent.clear()
check("notify with no webhook is a silent no-op, exit 0", board.cmd_notify("87", "shipped") == 0 and not sent)

print(f"{count} cases, {'all passed' if not fails else 'FAILURES'}")
sys.exit(fails)
