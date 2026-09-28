# riffmap#349 (September 17, 2026): a four-line list conflict, both sides appending a
# mockup entry, went to Steve as a decision and cost a day's round trip. watch.md §3 now
# says to resolve an additive conflict — both sides only added, nobody rewrote — itself:
# base text, then main's addition, then the tick's own, and keep going. This seeds exactly
# that shape: `origin/main` gets a second entry appended to the same list while the tick's
# branch (already cut from the first commit) appends a different one.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/docs"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34). Not a real project.

- The required CI check is `verify`.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/docs/mockups.md" <<'EOF'
# Mockups

- Mockup A
EOF
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  # The "other lane" merges its own addition to the same list while this tick is working.
  local other="$TMP/other"
  mkdir -p "$other/docs"
  cat > "$other/docs/mockups.md" <<'EOF'
# Mockups

- Mockup A
- Mockup C
EOF
  push_to_main "$other" "Add Mockup C"

  seed_card "$ISSUE" "Add a Mockup B entry to the mockups list" "In progress" "P1" <<'EOF'
## Summary

`docs/mockups.md` needs one more entry.

## The change

Append a new bullet to the end of the list in `docs/mockups.md`: `- Mockup B`. Leave the
existing entries alone. Docs-only.
EOF

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "additive-conflict: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "additive-conflict: tick exits 0"
  fi

  local final
  final="$(git -C "$ORIGIN" show main:docs/mockups.md 2>/dev/null || echo "<no such file>")"
  if printf '%s' "$final" | grep -qF -- '- Mockup A' \
     && printf '%s' "$final" | grep -qF -- '- Mockup B' \
     && printf '%s' "$final" | grep -qF -- '- Mockup C'; then
    pass "additive-conflict: main ends up with all three entries, nothing dropped"
  else
    flunk "additive-conflict: final docs/mockups.md" "$final"
  fi

  # "main's first, then yours" (watch.md §3): C (main's own addition) before B (the tick's).
  local c_line b_line
  c_line="$(printf '%s\n' "$final" | grep -n 'Mockup C' | head -1 | cut -d: -f1)"
  b_line="$(printf '%s\n' "$final" | grep -n 'Mockup B' | head -1 | cut -d: -f1)"
  if [ -n "$c_line" ] && [ -n "$b_line" ] && [ "$c_line" -lt "$b_line" ]; then
    pass "additive-conflict: main's addition kept ahead of the tick's own"
  else
    flunk "additive-conflict: ordering" "C at line $c_line, B at line $b_line"
  fi

  if state prs.1.comments | grep -qi 'additive conflict'; then
    pass "additive-conflict: the PR got the one-line FYI about the merge"
  else
    flunk "additive-conflict: no FYI comment on the PR" "$(state prs.1.comments)"
  fi

  if [ "$(state issues.100.state)" = "CLOSED" ]; then
    pass "additive-conflict: card closed (the merge went through after resolving)"
  else
    flunk "additive-conflict: card state" "$(state issues.100.state)"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "additive-conflict: in-flight leaked" "still on the card at exit"
  else
    pass "additive-conflict: in-flight removed"
  fi
}
