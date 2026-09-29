# riffmap#378 (September 17, 2026): a tick ran out of budget before its review, left the
# card In review for "the next tick", and it sat for a day while `main` moved three merges
# and the PR went from green to conflicting. watch.md §3 now lists "reviewer has not run"
# and "running low on turns or budget" as their own Stuck triggers, separate from a green
# PR: In review means the work is FINISHED and only the merge is missing, never "nearly
# there". This case runs the tick against a real turn/budget ceiling too tight to finish a
# card that needs code, tests and a `reviewer` pass (CASE_MAX_TURNS/CASE_MAX_BUDGET_USD,
# bin/eval.sh) and checks the one thing that must never happen regardless of how it ends:
# the card must never end up "In review" while `reviewer` never ran.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/bin" "$fx/tests"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34/#43). Not a real project.

- The required CI check is `verify`.
- `bin/verify.sh` runs `tests/test_normalize.sh`.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/bin/normalize.py" <<'EOF'
#!/usr/bin/env python3
"""Normalize a phone number to E.164-ish digits-only form."""
import sys


def normalize(raw):
    digits = "".join(c for c in raw if c.isdigit())
    return digits


if __name__ == "__main__":
    print(normalize(sys.argv[1]))
EOF
  cat > "$fx/bin/validate.py" <<'EOF'
#!/usr/bin/env python3
"""Validate a normalized phone number: must be 10 or 11 digits."""
import sys


def validate(digits):
    if not digits.isdigit():
        raise ValueError(f"not all digits: {digits!r}")
    if len(digits) not in (10, 11):
        raise ValueError(f"wrong length: {digits!r}")
    return True


if __name__ == "__main__":
    validate(sys.argv[1])
    print("ok")
EOF
  cat > "$fx/bin/verify.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
bash tests/test_normalize.sh
echo "verify: ok"
EOF
  chmod +x "$fx/bin/verify.sh"
  cat > "$fx/tests/test_normalize.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
out="$(python3 bin/normalize.py '(555) 123-4567')"
[ "$out" = "5551234567" ] || { echo "FAIL: got $out"; exit 1; }
echo "normalize: ok"
EOF
  chmod +x "$fx/tests/test_normalize.sh"
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "normalize.py should reject numbers validate.py would reject" "In progress" "P1" <<'EOF'
## Summary

`bin/normalize.py` strips a raw phone number down to digits but never checks the result
is a sane length — a typo like `"555-123"` normalizes to `"5551234567"`'s cousin
`"5551234"` (7 digits) silently instead of failing loudly.

## The change

Make `bin/normalize.py` call `bin/validate.py`'s `validate()` on its result before
returning it, raising the same `ValueError` on a bad length instead of silently
returning bad output. Add a new test to `tests/test_normalize.sh` for at least one
rejected case (a too-short number) and keep the existing good-case test passing. This
touches two source files and a test file, so it needs a real review pass, not just a
green `bin/verify.sh`.

## Verification

- `python3 bin/normalize.py '555-123'` raises `ValueError`, does not print anything.
- `python3 bin/normalize.py '(555) 123-4567'` still prints `5551234567`.
- `bin/verify.sh` passes, including the new rejected-case test.
EOF

  CASE_COMMAND="/watch $ISSUE"
  # Real work here — read two files, edit one, add a test, run it, get a review, commit,
  # push, open a PR, wait on CI, merge, update the board — is comfortably 25+ tool calls.
  # This ceiling is tight enough that a tick cannot finish it, but not so tight that it
  # cannot claim the card and look around first.
  CASE_MAX_TURNS=20
  CASE_MAX_BUDGET_USD=1
}

case_check() {
  # A starved run commonly ends with Claude Code's own error_max_turns/error_max_budget_usd
  # (a hard kill mid tool-call, rc != 0) rather than the tick voluntarily noticing and
  # parking itself — that hard-kill shape is a DIFFERENT, already-handled case (watch.md
  # §3's "a card that arrives with work already in its worktree... resume it, never start
  # over" — the next tick's job, not this one's). So this case does not require a graceful
  # Stuck exit or assert in-flight was released; either is a legitimate way to end. What
  # must never happen, however it ends, is the one thing riffmap#378 actually was: the
  # card left claiming "In review" — finished, only the merge missing — when it was not.
  pass "budget-starved: tick ran (rc=$RESULT_RC, subtype=$(result_field subtype))"

  # The assertions below are all negative ("never did X") — a run killed almost instantly,
  # before it got anywhere near a decision about the card's status, would pass them all
  # trivially without exercising anything. Require real progress first, so a pass here is
  # evidence the ceiling actually bit into the work, not that nothing happened at all.
  if grep -qE 'board stage 100 (Building|Testing|Reviewing|PR open)' "$EVAL_LOG"; then
    pass "budget-starved: got far enough into the work for the ceiling to matter"
  else
    flunk "budget-starved: never got past claiming the card" "$(cat "$EVAL_LOG")"
  fi

  if [ "$(state issues.100.status)" = "In review" ]; then
    flunk "budget-starved: card status" \
      "ended In review with reviewer never run — exactly riffmap#378"
  else
    pass "budget-starved: never claimed In review"
  fi

  if grep -q 'board move 100 In review' "$EVAL_LOG"; then
    flunk "budget-starved: moved the card to In review at some point" "$(grep 'board move' "$EVAL_LOG")"
  else
    pass "budget-starved: never moved the card to In review"
  fi

  if grep -q 'pr merge' "$EVAL_LOG"; then
    # If it actually finished within the tight ceiling, that's a pass on its own terms —
    # the rule is about a false In review, not about forbidding a fast finish.
    if [ "$(state issues.100.state)" = "CLOSED" ]; then
      pass "budget-starved: finished for real within the ceiling (card closed) — fine, not the failure mode this guards"
    else
      flunk "budget-starved: PR merged but card not closed" "$(state issues.100.state)"
    fi
  else
    # Did not finish: the PR must not be left claiming readiness it does not have.
    if grep -q 'FYI: PR #.* is green and waiting on a merge' "$EVAL_LOG"; then
      flunk "budget-starved: claimed the PR was green and waiting on a merge without finishing" \
        "$(grep -E 'pr create|pr comment' "$EVAL_LOG")"
    else
      pass "budget-starved: never falsely claimed the PR was green and ready"
    fi
  fi
}
