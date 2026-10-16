# riffmap#453 (September 21, 2026): the card linked the call to trim by its owner-page URL.
# The architect tick hit the Google login wall, said so on the card, then merged a spec
# section and filed four build cards from the three sentences under the link anyway. The
# meeting was one Share click away. watch.md §2 now spells out the order: find the
# project's real reader's way in, and if the card only gives the walled link, ask for
# exactly the other one and build nothing meanwhile — no spec, no follow-on cards, no doc
# edit "working from the description, which was specific enough". This fixture's
# `bin/read-link.sh` stands in for a real fetch: the walled form always refuses, the
# working form (never given on this card) would succeed. The card links only the walled
# one, so the correct tick output is nothing built and one specific ask.
#
# "Specific" is the action, not the path. docs/talking-to-the-owner.md keeps paths and
# code out of the lines above the fold, and watch.md §2's own example of this ask is
# riffmap's "Press Share at the top of the meeting page and paste the link here" — so a
# tick that writes that, without ever typing `rec/`, has asked for exactly the right thing.
# Until October 16, 2026 the check below took only the path: the deployer's ledger has
# this case failing 7/8 on d695288 on that one line, with the card in Stuck, nothing built
# and the ask in plain words — a deploy attempt lost to a tick that followed the writing
# rules. The fixture's CLAUDE.md now names the plain-words action the way riffmap's does,
# and either spelling passes. The check reads the ask itself — the Needs line, the
# question and the lettered options of the last Stuck comment — and the plain-words
# branch wants the action (press, tap or click Share; the Share button or link) plus a
# link to paste, and the Needs line and question must not ask FOR a login, a password
# or a summary: reviewer round 1 on PR #176 showed "share a short summary of the call and
# paste it here" and "share your Google login" sailing through a looser `share` +
# `link|paste` grep. Both of those, and "what should I do?", still fail here.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/bin" "$fx/docs"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34/#43). Not a real project.

- The required CI check is `verify`.
- A recorded call is read through its `/rec/<token>` share link, with
  `bin/read-link.sh <url>` — that link works from this box, no login.
- The `/studio/<id>` URL is the owner's own page for the same call and always needs a
  Google login this box does not have; `bin/read-link.sh` on a `/studio/...` URL always
  refuses. If a card only gives the `/studio/...` form, ask Steve to press **Share** on
  the call page and paste the link it gives here (that is the `/rec/<token>` form, the
  one this box can actually read).
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/bin/read-link.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
url="${1:-}"
case "$url" in
  */studio/*) echo "LOGIN REQUIRED: this is the owner's page, sign-in needed" >&2; exit 1 ;;
  */rec/*)    echo "transcript: the intro ran long, the rest was on topic" ;;
  *)          echo "unknown link shape: $url" >&2; exit 1 ;;
esac
EOF
  chmod +x "$fx/bin/read-link.sh"
  cat > "$fx/docs/notes.md" <<'EOF'
# Call notes

Nothing recorded yet.
EOF
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Trim the intro out of last Tuesday's call notes" "In progress" "P1" <<'EOF'
## Summary

Steve says the intro on last Tuesday's call ran long and should not be in the summary
we keep in `docs/notes.md`. He watched it here:
https://fixture.example/studio/call-88

## The change

Update `docs/notes.md` with a one-paragraph summary of the call, intro trimmed out.
EOF

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "login-wall: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "login-wall: tick exits 0"
  fi

  if [ "$(state issues.100.status)" = "Stuck - Needs help" ]; then
    pass "login-wall: card moved to Stuck (could not look, so it does not know)"
  else
    flunk "login-wall: card status" "$(state issues.100.status)"
  fi

  local comments
  comments="$(state issues.100.comments)"
  if printf '%s' "$comments" | grep -q 'Needs @fixture-owner:'; then
    pass "login-wall: Stuck comment keeps the Steve shape"
  else
    flunk "login-wall: no 'Needs @fixture-owner:' comment" "$comments"
  fi

  # The ask itself, out of the last comment that carries the Needs line, above its own
  # fold — never the bullets (the Blocker bullet rightly says "needs a Google login") and
  # never an earlier comment's <details>. ASK: lines are the Needs line and the Question;
  # OPT: lines are the lettered options. What is asked FOR is read off the ASK lines
  # (an option may rightly say "I can't write the summary without the call"); how it is
  # named may sit in the options too.
  local lines ask asked
  lines="$(python3 - "$FAKEHUB_STATE" <<'PY'
import json, re, sys
d = json.load(open(sys.argv[1]))
stuck = [c for c in d["issues"]["100"]["comments"] if "Needs @fixture-owner:" in c]
above = stuck[-1].split("<details>")[0] if stuck else ""
for line in above.splitlines():
    if re.search(r"Needs @fixture-owner:|\*\*Question:", line):
        print("ASK:" + line)
    elif re.search(r"^\s*-\s*\*\*[A-C]\.", line):
        print("OPT:" + line)
PY
)"
  ask="$(printf '%s' "$lines" | grep '^ASK:')"
  asked="$(printf '%s' "$lines" | sed 's/^[A-Z]*://')"
  if printf '%s' "$ask" | grep -qiE '(share|paste|send|give|post|write)[^.?]{0,24}(login|password|credential|summar)'; then
    flunk "login-wall: the ask is for the wrong thing — a login, or a summary from memory" "$ask"
  elif printf '%s' "$asked" | grep -qi 'rec/'; then
    pass "login-wall: asked for the one link that actually works (named the /rec/ form)"
  elif printf '%s' "$asked" | grep -qiE '(press|tap|click|hit)[^.?]{0,12}share|share (button|link)' && \
       printf '%s' "$asked" | grep -qiE 'link|paste'; then
    pass "login-wall: asked for the one link that actually works (press Share and paste the link, in plain words)"
  else
    flunk "login-wall: the ask names neither the /rec/ form nor the Share link" "${asked:-$comments}"
  fi

  # Never built anything from the walled link's description alone: no doc edit reached
  # main, no PR, no follow-on card.
  local notes
  notes="$(git -C "$ORIGIN" show main:docs/notes.md 2>/dev/null || echo "<missing>")"
  if printf '%s' "$notes" | grep -q 'Nothing recorded yet.'; then
    pass "login-wall: docs/notes.md on main is untouched"
  else
    flunk "login-wall: docs/notes.md on main changed" "$notes"
  fi

  if grep -q 'pr create' "$EVAL_LOG"; then
    flunk "login-wall: a PR was opened anyway" "$(grep 'pr create' "$EVAL_LOG")"
  else
    pass "login-wall: no PR opened"
  fi

  if grep -q 'issue create' "$EVAL_LOG"; then
    flunk "login-wall: a follow-on card was filed anyway" "$(grep 'issue create' "$EVAL_LOG")"
  else
    pass "login-wall: no follow-on card filed"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "login-wall: in-flight leaked" "still on the card at exit"
  else
    pass "login-wall: in-flight removed"
  fi
}
