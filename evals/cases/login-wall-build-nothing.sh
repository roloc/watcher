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
  refuses. If a card only gives the `/studio/...` form, ask Steve to open it and paste
  the `/rec/<token>` link instead — that is the one this box can actually read.
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

  if printf '%s' "$comments" | grep -qi 'rec/'; then
    pass "login-wall: asked for the one link that actually works (the /rec/ form)"
  else
    flunk "login-wall: no mention of the working link form" "$comments"
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
