#!/usr/bin/env python3
"""Tell the owner's Discord channel, Slack channel, or both, when a card is stuck,
something broke, or Monday's digest is due. Nothing else.

Imported by `board.py`; never called by a tick directly. Every message names the card
and links to it, so the owner can tap through from their phone. Configuration is per
transport — set either webhook, both, or neither:

    DISCORD_WEBHOOK_URL   the Discord channel's webhook. Unset or empty = no Discord post.
    DISCORD_MENTION       optional, prepended to Stuck alerts only — `<@123456789>`,
                          `<@&roleid>` or `@here` — so a parked card pings someone.
    SLACK_WEBHOOK_URL     the Slack channel's incoming webhook. Unset or empty = no Slack
                          post. Both set posts to both; neither set is a silent no-op.
    SLACK_MENTION         optional; plays DISCORD_MENTION's part for Slack — `<@U…>` (a
                          user) or `<!here>`.
    OWNER_LOGIN           optional; the GitHub login the Stuck alert's content line names
                          (card #154) — defaults to BOARD_OWNER, then to "the owner",
                          so a fork of this repo pings its own owner with no config at all.

What gets posted (and by whom):

    stuck      `board move`         — the card landed in Stuck - Needs help: the alert
    released   `board notify`       — a run died holding the card; the spawner released it
    breach     `board notify`       — the product detector filed a card for a large breach
    digest     `metrics.py --post`  — the Monday digest: the loop's own numbers, per lane,
                                      for the week that just ended

That is the whole list, plus the one exception `bin/metrics.py`'s own header always named
as a possibility: "a weekly Discord digest is a separate decision" — watcher#139 is that
decision, made once and run on a fixed Monday schedule rather than per event, same as
`stuck`/`released`/`breach` are each tied to one event. A card changing column, crossing
into a new stage, shipping or being closed still posts nothing: the floor page shows all
of that live, and a channel that repeats it is a channel nobody reads when the alert comes
(October 1, 2026: "Can we only hit the discord when something is stuck or broken?").
Nothing is posted per tick or per poll either, and the digest is the one post with no
single card behind it. A post that fails (network, a bad URL, Discord down) prints one
`notify: ...` line on stderr and returns — a notification is never allowed to fail a
board command.

`python3 bin/notify.py <kind> <number> <title> <url> [detail]` sends one message by hand
(smoke-testing a new webhook, say). `python3 bin/test-notify.py` checks the shapes offline.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

TIMEOUT_SEC = 8
SLACK_TEXT_MAX = 3000  # Slack's own limit on a section block's text field

# Discord embed colors (decimal RGB). One per meaning, so the color says it before the text.
RED = 0xE74C3C       # stuck: the owner has to act
ORANGE = 0xE67E22    # broken: a run died, a product check crossed its limit
NEUTRAL = 0x95A5A6   # digest: a status report, neither an alert nor something broken


def discord_webhook_url():
    return (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()


def slack_webhook_url():
    return (os.environ.get("SLACK_WEBHOOK_URL") or "").strip()


def enabled():
    return bool(discord_webhook_url() or slack_webhook_url())


def needs_label():
    """`Needs @<login>` once a project names one (OWNER_LOGIN, falling back to
    BOARD_OWNER), or `Needs the owner` with no `@` at all when neither is set — never
    a literal `@Steve` or similar, which is nobody's GitHub login."""
    login = (os.environ.get("OWNER_LOGIN") or os.environ.get("BOARD_OWNER") or "").strip()
    return f"Needs @{login}" if login else "Needs the owner"


def card_label(card):
    return f"#{card['number']} {card.get('title') or ''}".strip()


def _message(kind, card, detail="", pr=None, board_url=""):
    """Return the transport-independent shape of one event, or None for a kind that is
    not posted: the label, the card's url, the short lead line (Discord's `content`,
    Slack's own top line) and the body under it. Both `build()` and `build_slack()` wrap
    this the same way, so the two transports can never drift on what a post actually says.

    `card` is {"number", "title", "url"}; `detail` is the one line that varies (the
    Needs-the-owner ask, the spawner's reason, the detector's reading); `pr` is
    {"url", "state"} when a pull request for the card is known.
    """
    label = card_label(card)
    url = card.get("url") or ""
    pr_line = ""
    if pr and pr.get("url"):
        state = (pr.get("state") or "").lower()
        pr_line = f"\nPR: {pr['url']}" + (f" ({state})" if state else "")

    if kind == "stuck":
        lead = f"🚨 **{needs_label()}:** {label} is stuck."
        ask = detail or "Read the last comment on the card."
        body = (f"{ask}\n\nOpen the card, reply **A**, **B** or **C**, then move it back to "
                f"**In progress**.{pr_line}")
        if board_url:
            body += f"\nBoard: {board_url}"
    elif kind == "released":
        lead = f"⚠️ {label}: the run ended without finishing."
        body = (detail or "Released; the next poll may retry once.") + pr_line
    elif kind == "breach":
        lead = f"📉 **Something broke:** {label}"
        body = (detail or "A product check crossed its limit. The card has the numbers.") + pr_line
    elif kind == "digest":
        # No single card is behind this one: `detail` is the whole FYI-shaped message
        # (bin/metrics.py's format_digest()), so it goes in the body verbatim rather than
        # being folded into `lead` the way the other kinds' short lines are.
        lead = "📊 Weekly digest."
        body = detail or "No ticks this week."
        label = card.get("title") or "Weekly digest"  # no real card behind this one
    else:
        return None
    return {"kind": kind, "label": label, "url": url, "lead": lead, "body": body}


def build(kind, card, detail="", pr=None, board_url=""):
    """Return the Discord webhook payload for one event, or None for a kind that is not
    posted."""
    m = _message(kind, card, detail=detail, pr=pr, board_url=board_url)
    if m is None:
        return None
    content = m["lead"]
    if m["kind"] == "stuck":
        mention = (os.environ.get("DISCORD_MENTION") or "").strip()
        if mention:
            content = f"{mention} {content}"
    # Discord rejects an embed over 4096 chars outright (a 400, logged and nothing else,
    # per this module's own contract) rather than truncating it for us, the way it
    # handles `content` past 2000 — so the digest, the one body with no fixed length, has
    # to guard itself the same way.
    desc = m["body"][:4096] if m["kind"] == "digest" else m["body"]
    embed = {"title": m["label"], "url": m["url"],
             "color": {"stuck": RED, "released": ORANGE, "breach": ORANGE, "digest": NEUTRAL}[m["kind"]],
             "description": desc}
    if not embed.get("url"):  # Discord rejects an empty url; a card we could not look up has none
        embed.pop("url", None)
    return {"content": content[:1900], "embeds": [embed],
            # Only DISCORD_MENTION may ping anyone; card titles and asks never do.
            "allowed_mentions": {"parse": ["users", "roles", "everyone"]}}


def _slack_mrkdwn(s):
    # Slack's own escaping rule, applied before anything else: `&`, `<` and `>` are
    # special in mrkdwn (a raw `<` or `>` from a card title would otherwise read as the
    # start of a link or a mention). Discord's `**bold**` is Slack's `*bold*`; nothing
    # else in these messages uses markdown.
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", s)


def build_slack(kind, card, detail="", pr=None, board_url=""):
    """Return the Slack incoming-webhook payload for one event, or None for a kind that
    is not posted. The embed's title, the ask, and the card link all fold into one
    section block, with the same emoji lead Discord's `content` carries."""
    m = _message(kind, card, detail=detail, pr=pr, board_url=board_url)
    if m is None:
        return None
    lead = _slack_mrkdwn(m["lead"])
    if m["kind"] == "stuck":
        mention = (os.environ.get("SLACK_MENTION") or "").strip()
        if mention:
            lead = f"{mention} {lead}"
    label = _slack_mrkdwn(m["label"])
    link = f"<{m['url']}|{label}>" if m["url"] else label
    body = _slack_mrkdwn(m["body"])
    text = f"{lead}\n{link}\n{body}"[:SLACK_TEXT_MAX]
    return {"text": lead[:SLACK_TEXT_MAX],
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


def post(kind, card, detail="", pr=None, board_url=""):
    """Send one event to every configured transport (Discord, Slack, both, or neither).
    Returns True if at least one took it, False otherwise. Never raises."""
    sent = False
    discord_url = discord_webhook_url()
    if discord_url:
        payload = build(kind, card, detail=detail, pr=pr, board_url=board_url)
        if payload is not None:
            sent = send(discord_url, payload, transport="discord") or sent
    slack_url = slack_webhook_url()
    if slack_url:
        payload = build_slack(kind, card, detail=detail, pr=pr, board_url=board_url)
        if payload is not None:
            sent = send(slack_url, payload, transport="slack") or sent
    return sent


def send(url, payload, transport="discord"):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "watcher-notify/1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            if 200 <= r.status < 300:
                return True
            print(f"notify: {transport} answered {r.status}", file=sys.stderr)
            return False
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        print(f"notify: {transport} answered {e.code}: {body}", file=sys.stderr)
    except Exception as e:  # DNS, timeout, refused — none of these may fail the caller
        print(f"notify: {transport} post failed: {e}", file=sys.stderr)
    return False


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 4:
        sys.exit(__doc__)
    kind, number, title, url = a[0], a[1], a[2], a[3]
    detail = a[4] if len(a) > 4 else ""
    if not enabled():
        sys.exit("notify: neither DISCORD_WEBHOOK_URL nor SLACK_WEBHOOK_URL is set")
    ok = post(kind, {"number": number, "title": title, "url": url}, detail=detail)
    sys.exit(0 if ok else 1)
