#!/usr/bin/env python3
"""Tell Steve's Discord channel when a card is stuck, something broke, or Monday's digest
is due. Nothing else.

Imported by `board.py`; never called by a tick directly. Every message names the card
and links to it, so Steve can tap through from his phone. Configuration is one env var:

    DISCORD_WEBHOOK_URL   the channel's webhook. Unset or empty = every post is a no-op.
    DISCORD_MENTION       optional, prepended to Stuck alerts only — `<@123456789>`,
                          `<@&roleid>` or `@here` — so a parked card pings someone.
    OWNER_LOGIN           optional; the GitHub login the Stuck alert's content line names
                          (card #154) — defaults to BOARD_OWNER, then to the word "Steve",
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
import sys
import urllib.error
import urllib.request

TIMEOUT_SEC = 8

# Discord embed colors (decimal RGB). One per meaning, so the color says it before the text.
RED = 0xE74C3C       # stuck: Steve has to act
ORANGE = 0xE67E22    # broken: a run died, a product check crossed its limit
NEUTRAL = 0x95A5A6   # digest: a status report, neither an alert nor something broken


def webhook_url():
    return (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()


def enabled():
    return bool(webhook_url())


def needs_label():
    """`Needs @<login>` once a project names one (OWNER_LOGIN, falling back to
    BOARD_OWNER), or the old `Needs Steve` with no `@` at all when neither is set — never
    the literal `@Steve`, which is nobody's GitHub login."""
    login = (os.environ.get("OWNER_LOGIN") or os.environ.get("BOARD_OWNER") or "").strip()
    return f"Needs @{login}" if login else "Needs Steve"


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
        content = f"🚨 **{needs_label()}:** {label} is stuck."
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
    elif kind == "digest":
        # No single card is behind this one: `detail` is the whole FYI-shaped message
        # (bin/metrics.py's format_digest()), so it goes in the embed description verbatim
        # rather than being folded into `content` the way the other kinds' short lines are.
        content = "📊 Weekly digest."
        # Discord rejects an embed over 4096 chars outright (a 400, logged and nothing
        # else, per this module's own contract) rather than truncating it for us, the way
        # it handles `content` past 2000 — so this one guards itself the same way.
        embed = {"title": card.get("title") or "Weekly digest", "url": url, "color": NEUTRAL,
                 "description": (detail or "No ticks this week.")[:4096]}
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
