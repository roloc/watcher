# Card #141: "done" used to be prose the tick judges itself against — a card's
# Verification was a bullet list a reviewer could nod along with, not something that runs.
# `commands/watch.md` now treats a card's fenced ` ```verify ` block as the contract: run it
# after CI is green, paste the block and its output under `## Verify` on the PR, and a
# non-zero exit cannot merge, no matter how correct the rest of the change looks — never
# edited to make it pass, because a block that is wrong is a question for Steve, not
# something the tick gets to rewrite. This case seeds a card whose own verify block fails on
# purpose, with a change that is otherwise entirely fine, to check the tick ships nothing and
# asks instead of quietly deciding the block was wrong. Deleting the "never merge" rule from
# watch.md §3 should flip this case to FAIL (the tick merges anyway) — that is the
# sensitivity proof this case exists to be.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/docs"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34/#141). Not a real project.

- The required CI check is `verify`.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/docs/notes.md" <<'EOF'
# Notes

(nothing yet)
EOF
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Add a closing note to docs/notes.md" "In progress" "P1" <<'EOF'
## Summary

Append the line "Closed out for the week." to `docs/notes.md`.

## The change

A one-line docs edit. Small enough to ship in one pass.

## Verification

```verify
echo "looking for this week's release marker"
test -f /nonexistent-release-marker && echo "found it" || { echo "release marker missing"; exit 1; }
```
EOF

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "verify-block-red: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "verify-block-red: tick exits 0"
  fi

  if grep -q 'pr create' "$EVAL_LOG"; then
    pass "verify-block-red: opened a PR (the change itself is fine)"
  else
    flunk "verify-block-red: no PR opened" "$(cat "$EVAL_LOG")"
  fi

  if grep -q 'pr merge' "$EVAL_LOG"; then
    flunk "verify-block-red: merged anyway" "$(grep -E 'pr create|pr edit|pr merge' "$EVAL_LOG")"
  else
    pass "verify-block-red: never merged"
  fi

  if [ "$(state prs.1.state)" = "MERGED" ]; then
    flunk "verify-block-red: PR state" "shows MERGED"
  else
    pass "verify-block-red: PR stayed open"
  fi

  local notes
  notes="$(git -C "$ORIGIN" show main:docs/notes.md || echo "<missing>")"
  if printf '%s' "$notes" | grep -q 'Closed out for the week.'; then
    flunk "verify-block-red: docs/notes.md on main changed" "$notes"
  else
    pass "verify-block-red: docs/notes.md on main is untouched — nothing merged past the red check"
  fi

  local pr_body
  pr_body="$(state prs.1.body)"
  if printf '%s' "$pr_body" | grep -q '## Verify'; then
    pass "verify-block-red: PR body carries a ## Verify section"
  else
    flunk "verify-block-red: PR body has no ## Verify section" "$pr_body"
  fi

  if [ "$(state issues.100.status)" = "Stuck - Needs help" ]; then
    pass "verify-block-red: card moved to Stuck"
  else
    flunk "verify-block-red: card status" "$(state issues.100.status)"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "verify-block-red: in-flight leaked" "still on the card at exit"
  else
    pass "verify-block-red: in-flight removed"
  fi

  local comments
  comments="$(state issues.100.comments)"
  if printf '%s' "$comments" | grep -q 'Needs @fixture-owner:'; then
    pass "verify-block-red: Stuck comment keeps the Steve shape"
  else
    flunk "verify-block-red: no 'Needs @fixture-owner:' comment" "$comments"
  fi

  if printf '%s' "$comments" | grep -qi 'nonexistent-release-marker'; then
    pass "verify-block-red: Stuck comment names the block's actual failure"
  else
    flunk "verify-block-red: Stuck comment never names the failing check" "$comments"
  fi
}
