# watcher#101 (October 9, 2026): the new `usability` pass exists to catch a screen that
# ships unreadable, not to sit in front of every PR. Its own gate is the same one
# `reviewer`'s screen rule already uses — watch.md says a docs-only PR, or one that
# touches no screen, never gets it. This case is `happy-path.sh`'s own fixture (card #34:
# a trivial docs-only card, start to finish in one `claude -p`), with one more thing
# proven alongside the merge: no browser was ever started. A docs change has no screen to
# look at, so if anything in the tick's path mistakenly decided this needed a phone shot —
# `usability` handed a live URL, or a screenshot script run on a page that does not exist
# — that would show up here as a browser launch that should never have happened. Reusing
# `happy-path`'s own fixture, rather than a near-duplicate, is deliberate: this proves the
# new pass left the oldest, simplest replay case exactly as fast and clean as it always
# was.

case_setup() {
  local fx="$TMP/fixture" real
  mkdir -p "$fx/docs"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34/#101). Not a real project.

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

  # Same wrapper as the browser-dependent cases, used here in the opposite direction: this
  # case passes only if the log it writes to STAYS EMPTY — no screen, so no browser call
  # should ever happen, from the tick or from a usability pass that should never run.
  real="$(env -u BROWSE_BROWSER python3 "$REPO/bin/browse.py" find 2>/dev/null || true)"
  if [ -n "$real" ]; then
    cat > "$TMP/browser" <<EOF
#!/usr/bin/env bash
echo "browser started" >> "$EVAL_LOG"
exec "$real" "\$@"
EOF
    chmod +x "$TMP/browser"
    export BROWSE_BROWSER="$TMP/browser"
  fi

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "usability-skips-docs: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "usability-skips-docs: tick exits 0"
  fi

  if [ "$(result_field subtype)" = "success" ]; then
    pass "usability-skips-docs: result subtype is success"
  else
    flunk "usability-skips-docs: result subtype" "got $(result_field subtype 2>&1)"
  fi

  if grep -q 'pr checks' "$EVAL_LOG"; then
    pass "usability-skips-docs: waited on CI in the foreground"
  else
    flunk "usability-skips-docs: no gh pr checks call" "$(cat "$EVAL_LOG")"
  fi

  if grep -q 'pr merge' "$EVAL_LOG"; then
    pass "usability-skips-docs: merged the PR itself"
  else
    flunk "usability-skips-docs: no gh pr merge call" "$(cat "$EVAL_LOG")"
  fi

  if [ "$(state issues.100.state)" = "CLOSED" ]; then
    pass "usability-skips-docs: card closed by the merge"
  else
    flunk "usability-skips-docs: card state" "$(state issues.100.state)"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "usability-skips-docs: in-flight leaked" "still on the card at exit"
  else
    pass "usability-skips-docs: in-flight removed"
  fi

  if state prs.1.body | grep -qE '[Cc]loses #100'; then
    pass "usability-skips-docs: PR body closes the card"
  else
    flunk "usability-skips-docs: PR body has no Closes #100" "$(state prs.1.body)"
  fi

  local runs
  runs="$(grep -c '^browser started' "$EVAL_LOG" || true)"
  if [ "${runs:-0}" -eq 0 ]; then
    pass "usability-skips-docs: no browser ever started — nothing treated this as a screen change"
  else
    flunk "usability-skips-docs: a browser was started $runs time(s) on a docs-only card" \
      "usability (or a screenshot step) ran on a change with no screen to look at"
  fi
}
