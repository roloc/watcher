# riffmap#778 (October 17, 2026): a steve-decision card — the owner mints a mail app
# password and pushes five env lines to the box — whose body gave him the last step too
# ("Verification: only you can open the admin page and read your inbox"). He did his part,
# the live send failed with a 535, and he wrote "please just send a test mail with the
# credentials, you may have to try escaping the password" four times in three hours. Five
# triage ticks in a row read `gh issue view --json title,body,labels,state` — no comments —
# answered the body's Verification section, invented an outcome the contract does not have
# ("no agent lane fits, parking it"), and handed the card back to him with the same Needs
# comment each time. "I feel like the watcher is just fighting me."
#
# commands/triage.md now reads the comments, says the owner's newest comment is the ask and
# the body's Verification/Owner/Progress sections are what the card used to be, and that
# "this step is the owner's" is not one of triage's three outcomes: the tick labels the lane
# whose words it would take and lets that lane's /watch park it, if it must, with a real
# access fact. This seeds exactly that thread — the owner-does-steps body, a bot's Needs
# with A/B options, then the owner's "A, it failed: 535 … please run the send yourself" —
# and the first /triage case in the suite. The right tick labels `agent: builder`, posts an
# FYI whose Why quotes the owner's sentence, and never moves the card or posts a second
# Needs. Whether this is a sensitivity proof or a regression guard is for the deployer's
# first runs to say (bin/eval.sh's header).

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/bin" "$fx/.claude/agents"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34/#43). Not a real project.

- The required CI check is `verify`.
- `bin/mail.sh` sends the box's notification mail through the account named in
  `/etc/fixture/env` on the box (MAIL_HOST, MAIL_PORT, MAIL_USER, MAIL_PASSWORD, MAIL_FROM).
  That file is the box's; it is not in this repo and no tick can read it.
- One agent, `builder` (`.claude/agents/builder.md`): everything under `bin/`.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/.claude/agents/builder.md" <<'EOF'
---
name: builder
description: The general builder — any change under bin/, and anything no other agent owns.
---

You build what the card asks for, under `bin/`, and ship it as a PR.
EOF
  cat > "$fx/bin/mail.sh" <<'EOF'
#!/usr/bin/env bash
# Sends one notification mail with the account in /etc/fixture/env.
set -euo pipefail
. /etc/fixture/env
printf 'to: %s\nsubject: %s\n' "$1" "$2" | curl -s --url "smtp://$MAIL_HOST:$MAIL_PORT" \
  --mail-from "$MAIL_FROM" --mail-rcpt "$1" --user "$MAIL_USER:$MAIL_PASSWORD" -T -
EOF
  chmod +x "$fx/bin/mail.sh"
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Mail: move the box to a Gmail app password" "In progress" "P1" <<'EOF'
## Summary

The box's notification mail stopped going out on October 14, 2026: Gmail no longer takes the
account password and wants an app password instead. Nothing in the repo changes; the
new credentials go on the box.

## Owner

1. Mint an app password on the Google account's security page.
2. Push the five env lines (`MAIL_HOST`, `MAIL_PORT`, `MAIL_USER`, `MAIL_PASSWORD`,
   `MAIL_FROM`) to `/etc/fixture/env` on the box.

## Progress

- [ ] app password minted
- [ ] env lines pushed

## Verification

Only you can do this part: open the admin page at https://fixture.example/admin, press
**Send test mail**, and check your inbox for it. When it arrives, close this card.
EOF
  python3 - "$FAKEHUB_STATE" <<'PY'
import json, sys
path = sys.argv[1]
d = json.load(open(path))
d["labels"] = ["agent: builder", "steve-decision", "blocked", "in-flight", "bug"]
it = d["issues"]["100"]
it["labels"] = ["steve-decision"]
it["comments"] = [
    "🤖 **Needs @fixture-owner: run the live check**\n\n"
    "- Card: move the box's mail to a Gmail app password\n"
    "- Done so far: nothing to build — both steps are yours, on the box\n"
    "- Blocker: only you can open the admin page and read your inbox\n\n"
    "**Question: have you pushed the new credentials and sent the test mail?**\n"
    "- **A. Yes — I ran it** (recommended) — say here what happened, and close the card if it arrived\n"
    "- **B. Not yet** — leave this card in Stuck until you have\n\n"
    "Reply A or B on this card, then move it back to **In progress**.",
    "A I ran it and it failed: 535 Username and Password not accepted. Please run the send "
    "yourself with the credentials and try different escaping of the password before "
    "sending it back to me.",
]
json.dump(d, open(path, "w"))
PY

  CASE_COMMAND="/triage $ISSUE"
}

# comments_matching <python-regex> — how many of the card's comments start with it.
comments_matching() {
  python3 - "$FAKEHUB_STATE" "$1" <<'PY'
import json, re, sys
d = json.load(open(sys.argv[1]))
print(sum(1 for c in d["issues"]["100"]["comments"] if re.match(sys.argv[2], c)))
PY
}

# fyi_comment — the body of the tick's own FYI (the newest one), empty when there is none.
fyi_comment() {
  python3 - "$FAKEHUB_STATE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
fyis = [c for c in d["issues"]["100"]["comments"] if c.startswith("🤖 FYI:")]
print(fyis[-1] if fyis else "")
PY
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "owner-comment: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "owner-comment: tick exits 0"
  fi

  if state issues.100.labels | grep -qx 'agent: builder'; then
    pass "owner-comment: agent: builder is on the card"
  else
    flunk "owner-comment: no agent: builder label" "labels: $(state issues.100.labels | tr '\n' ',')"
  fi

  # The symptom itself: the card handed back to the owner. Status, and the move that does it.
  if [ "$(state issues.100.status)" = "In progress" ]; then
    pass "owner-comment: card still In progress"
  else
    flunk "owner-comment: card status" "$(state issues.100.status) (expected In progress)"
  fi
  if grep -qE 'board move 100 ' "$EVAL_LOG"; then
    flunk "owner-comment: a board move happened" "$(grep -E 'board move 100 ' "$EVAL_LOG" | head -1)"
  else
    pass "owner-comment: no board move"
  fi

  # One Needs is seeded (the bot's, already answered). A second one is the same ask again —
  # bolded as the template has it, or not.
  local needs; needs="$(comments_matching '🤖 \*{0,2}Needs')"
  if [ "$needs" = 1 ]; then
    pass "owner-comment: no new Needs comment posted"
  else
    flunk "owner-comment: a new Needs comment was posted" "$needs Needs comment(s) on the card, 1 seeded"
  fi

  local fyi; fyi="$(fyi_comment)"
  if printf '%s\n' "$fyi" | head -1 | grep -qE 'builder'; then
    pass "owner-comment: FYI names builder"
  else
    flunk "owner-comment: no FYI naming builder" "$(printf '%s' "$fyi" | head -1)"
  fi

  # Routed on the comment, not the body: the Why line quotes the owner's own sentence — the
  # 535, or "escaping" — and carries none of the body's "only you can …", which is the
  # Verification section talking, however much of the comment sits next to it.
  local why; why="$(printf '%s\n' "$fyi" | grep -iE '^- Why:' | head -1)"
  if printf '%s' "$why" | grep -qiE '535|escap' && ! printf '%s' "$why" | grep -qiE 'only you can'; then
    pass "owner-comment: FYI's Why quotes the owner's comment, not the body"
  else
    flunk "owner-comment: FYI's Why does not quote the owner's comment, or still reads from the body" "$why"
  fi
}
