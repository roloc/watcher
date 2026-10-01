#!/usr/bin/env python3
"""Tell Steve's Discord channel when a card is stuck or something broke. Nothing else.

Imported by `board.py`; never called by a tick directly. Every message names the card
and links to it, so Steve can tap through from his phone. Configuration is one env var:

    DISCORD_WEBHOOK_URL   the channel's webhook. Unset or empty = every post is a no-op.
    DISCORD_MENTION       optional, prepended to Stuck alerts only — `<@123456789>`,
                          `<@&roleid>` or `@here` — so a parked card pings someone.

What gets posted (and by whom):

    stuck      `board move`   — the card landed in Stuck - Needs help: the alert
    released   `board notify` — a run died holding the card; the spawner released it
    breach     `board notify` — the product detector filed a card for a large breach

That is the whole list. A card changing column, crossing into a new stage, shipping or
being closed posts nothing: the floor page shows all of that live, and a channel that
repeats it is a channel nobody reads when the alert comes (October 1, 2026: "Can we only
hit the discord when something is stuck or broken?"). Nothing is posted per tick or per
poll either. A post that fails (network, a bad URL, Discord down) prints one
`notify: ...` line on stderr and returns — a notification is never allowed to fail a
board command.

`python3 bin/notify.py <kind> <number> <title> <url> [detail]` sends one message by hand
(smoke-testing a new webhook, say). `python3 bin/test-notify.py` checks the shapes offline.
"""
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_SEC = 8

# Discord embed colors (decimal RGB). One per meaning, so the color says it before the text.
RED = 0xE74C3C       # stuck: Steve has to act
ORANGE = 0xE67E22    # broken: a run died, a product check crossed its limit


def webhook_url():
    return (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()


def enabled():
    return bool(webhook_url())


def card_label(card):
    return f"#{card['number']} {card.get('title') or ''}".strip()


def build(kind, card, detail="", pr=None, board_url=""):
    """Return the webhook payload for one event, or None for a kind that is not posted.

    `card` is {"number", "title", "url"}; `detail` is the one line that varies (the
    Needs Steve ask, the spawner's reason, the detector's reading); `pr` is
    {"url", "state"} when a pull request for the card is known.
    """
    label = card_label(card)
    url = card.get("url") or ""
    pr_line = ""
    if pr and pr.get("url"):
        state = (pr.get("state") or "").lower()
        pr_line = f"\nPR: {pr['url']}" + (f" ({state})" if state else "")

    if kind == "stuck":
        mention = (os.environ.get("DISCORD_MENTION") or "").strip()
        content = f"🚨 **Needs Steve:** {label} is stuck."
        if mention:
            content = f"{mention} {content}"
        ask = detail or "Read the last comment on the card."
        desc = (f"{ask}\n\nOpen the card, reply **A**, **B** or **C**, then move it back to "
                f"**In progress**.{pr_line}")
        if board_url:
            desc += f"\nBoard: {board_url}"
        embed = {"title": label, "url": url, "color": RED, "description": desc}
    elif kind == "released":
        content = f"⚠️ {label}: the run ended without finishing."
        embed = {"title": label, "url": url, "color": ORANGE,
                 "description": (detail or "Released; the next poll may retry once.") + pr_line}
    elif kind == "breach":
        content = f"📉 **Something broke:** {label}"
        embed = {"title": label, "url": url, "color": ORANGE,
                 "description": (detail or "A product check crossed its limit. "
                                           "The card has the numbers.") + pr_line}
    else:
        return None

    if not embed.get("url"):  # Discord rejects an empty url; a card we could not look up has none
        embed.pop("url", None)
    return {"content": content[:1900], "embeds": [embed],
            # Only DISCORD_MENTION may ping anyone; card titles and asks never do.
            "allowed_mentions": {"parse": ["users", "roles", "everyone"]}}


def post(kind, card, detail="", pr=None, board_url=""):
    """Send one event. Returns True if Discord took it, False otherwise. Never raises."""
    url = webhook_url()
    if not url:
        return False
    payload = build(kind, card, detail=detail, pr=pr, board_url=board_url)
    if payload is None:
        return False
    return send(url, payload)


def send(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "watcher-notify/1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            if 200 <= r.status < 300:
                return True
            print(f"notify: discord answered {r.status}", file=sys.stderr)
            return False
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        print(f"notify: discord answered {e.code}: {body}", file=sys.stderr)
    except Exception as e:  # DNS, timeout, refused — none of these may fail the caller
        print(f"notify: discord post failed: {e}", file=sys.stderr)
    return False


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 4:
        sys.exit(__doc__)
    kind, number, title, url = a[0], a[1], a[2], a[3]
    detail = a[4] if len(a) > 4 else ""
    if not enabled():
        sys.exit("notify: DISCORD_WEBHOOK_URL is not set")
    ok = post(kind, {"number": number, "title": title, "url": url}, detail=detail)
    sys.exit(0 if ok else 1)
