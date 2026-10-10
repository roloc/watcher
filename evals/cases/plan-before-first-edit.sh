# watcher#98 (October 9, 2026): the card showed only the pick-up comment for 57 minutes
# while a tick built, tested and ran the replay suite, and the plan comment ("You get" /
# "Test" / "Risk") landed at 20:26 UTC — after the work was done. From the board that read
# as a hung lane; Steve asked for a hang check that turned out to be a missing comment.
# commands/watch.md §2 now says the plan comment is the tick's first act, posted before the
# Stage moves past Claimed. This seeds a small code card and checks the ORDER of the fake
# hub's own call log: the plan comment (its first line matches "plan for this card", the
# literal template watch.md §2 gives) must appear before the first `board stage` call that
# moves the card past Claimed (Designing/Building/Testing/Reviewing/PR open/Merging/Parked)
# — never after the build, the tests or the PR are already underway.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/bin" "$fx/docs"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34/#103). Not a real project.

- The required CI check is `verify`.
- `bin/verify.sh` is the whole CI check: it just runs `bin/report.sh --help` and greps
  the output. No test framework.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/bin/report.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--help" ]; then
  echo "usage: report.sh [--help]"
  echo "Prints a one-line report."
  exit 0
fi
echo "report: ok"
EOF
  chmod +x "$fx/bin/report.sh"
  cat > "$fx/bin/verify.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
bin/report.sh --help | grep -q "one-line report"
echo "verify: ok"
EOF
  chmod +x "$fx/bin/verify.sh"
  cat > "$fx/docs/report.md" <<'EOF'
# report.sh

Prints a one-line report. No flags yet.
EOF
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Add a --quiet flag to bin/report.sh" "In progress" "P1" <<'EOF'
## Summary

`bin/report.sh` always prints its usual output. Add a `--quiet` flag that prints nothing
at all (still exits 0) — useful when a caller only wants the exit code.

## The change

Add `--quiet` to `bin/report.sh`: with it, print nothing and exit 0. Keep `--help`
accurate. Keep the plain (no-flag) behavior exactly as it is.

## Verification

- `bin/report.sh --quiet` prints nothing and exits 0.
- `bin/verify.sh` still passes.
EOF

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "plan-before-first-edit: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "plan-before-first-edit: tick exits 0"
  fi

  if [ "$(state issues.100.state)" = "CLOSED" ]; then
    pass "plan-before-first-edit: card closed by the merge"
  else
    flunk "plan-before-first-edit: card state" "$(state issues.100.state)"
  fi

  if git -C "$ORIGIN" show main:bin/report.sh 2>/dev/null | grep -q -- '--quiet'; then
    pass "plan-before-first-edit: --quiet actually landed on main"
  else
    flunk "plan-before-first-edit: bin/report.sh on main" \
      "$(git -C "$ORIGIN" show main:bin/report.sh 2>/dev/null)"
  fi

  # The order check (commands/watch.md §2). watch.md §1 already has the tick post ONE
  # comment before anything else — "picked this up" — so the plan is never literally the
  # tick's first comment; it is the SECOND `gh issue comment` call on this card, and a
  # body with backticks in it (as a plan naming a flag or a file almost always has) is
  # refused on `--body` and goes through `--body-file` instead (talking-to-steve.md), so
  # this cannot grep the comment's own text out of the log line — only its position.
  # Line numbers in $EVAL_LOG stand in for wall-clock order: fakehub.py logs one line per
  # call, in the order the tick made them.
  plan_line="$(grep -n "^gh issue comment $ISSUE " "$EVAL_LOG" | sed -n '2p' | cut -d: -f1)"
  stage_line="$(grep -nE "^board stage $ISSUE (Designing|Building|Testing|Reviewing|PR open|Merging|Parked)" \
    "$EVAL_LOG" | head -1 | cut -d: -f1)"

  if [ -n "$plan_line" ]; then
    pass "plan-before-first-edit: a second comment (the plan, after the pick-up one) was posted"
  else
    flunk "plan-before-first-edit: no second comment found" "$(cat "$EVAL_LOG")"
  fi

  if [ -n "$plan_line" ] && [ -n "$stage_line" ] && [ "$plan_line" -lt "$stage_line" ]; then
    pass "plan-before-first-edit: that comment landed before the Stage moved past Claimed"
  else
    flunk "plan-before-first-edit: plan comment order" \
      "plan_line=${plan_line:-none} stage_line=${stage_line:-none}; log: $(cat "$EVAL_LOG")"
  fi

  # Content check: the second comment really reads as the plan. fakehub.py's own
  # gh_issue_comment resolves --body-file to its contents before storing (body_of(), in
  # evals/fakehub.py) — $FAKEHUB_STATE always holds the real text, so this reads the
  # second comment straight out of there rather than the log line (which, for a
  # --body-file call, names only a path, and that temp file is not this case's to rely on
  # staying on disk).
  plan_text="$(python3 - "$FAKEHUB_STATE" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))["issues"]["100"]["comments"]
print(c[1] if len(c) > 1 else "")
PY
)"
  if printf '%s' "$plan_text" | grep -qi 'plan for this card'; then
    pass "plan-before-first-edit: that second comment's own text is the plan template"
  else
    flunk "plan-before-first-edit: second comment doesn't read as the plan" "$plan_text"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "plan-before-first-edit: in-flight leaked" "still on the card at exit"
  else
    pass "plan-before-first-edit: in-flight removed"
  fi
}
