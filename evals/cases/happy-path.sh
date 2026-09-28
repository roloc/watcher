# watcher#4 (the first night): a tick ended its turn "waiting" on a background task and the
# process died holding the card. This case is the harness's own smoke test — a trivial
# docs-only card, start to finish in one `claude -p` — and it doubles as that regression: if
# the tick ever again ends its turn instead of completing the ship steps in the foreground,
# this fails, because nothing will have merged the PR or closed the issue.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/docs"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34). Not a real project.

- The required CI check is `verify`.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/docs/faq.md" <<'EOF'
# FAQ
EOF
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Add a FAQ entry for 'What is Foo?'" "In progress" "P1" <<'EOF'
## Summary

`docs/faq.md` has no entry for "What is Foo?". Add one.

## The change

Append to `docs/faq.md`:

    **Q: What is Foo?**
    A: Bar.

Docs-only. No code to test.
EOF

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "happy-path: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "happy-path: tick exits 0"
  fi

  if [ "$(result_field subtype)" = "success" ]; then
    pass "happy-path: result subtype is success"
  else
    flunk "happy-path: result subtype" "got $(result_field subtype 2>&1)"
  fi

  if grep -q 'pr checks' "$EVAL_LOG"; then
    pass "happy-path: waited on CI in the foreground (gh pr checks was called)"
  else
    flunk "happy-path: no gh pr checks call" "$(cat "$EVAL_LOG")"
  fi

  if grep -q 'pr merge' "$EVAL_LOG"; then
    pass "happy-path: merged the PR itself (never left for a hypothetical next tick)"
  else
    flunk "happy-path: no gh pr merge call" "$(cat "$EVAL_LOG")"
  fi

  if [ "$(state issues.100.state)" = "CLOSED" ]; then
    pass "happy-path: card closed by the merge"
  else
    flunk "happy-path: card state" "$(state issues.100.state)"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "happy-path: in-flight leaked" "still on the card at exit"
  else
    pass "happy-path: in-flight removed"
  fi

  if state prs.1.body | grep -qE '[Cc]loses #100'; then
    pass "happy-path: PR body closes the card"
  else
    flunk "happy-path: PR body has no Closes #100" "$(state prs.1.body)"
  fi
}
