# riffmap #679 (the local-model bake-off, twice on October 9, 2026): a measurement card
# that runs several real cases lost two ticks to the 150-minute wall, `TICK_TIMEOUT`
# (`bin/spawn.sh`) — once after every measurement had already finished, while writing the
# findings entry; once while re-recording a baseline. Nothing was lost only by luck (the
# same worktree, replayable cassettes); a card whose measuring takes longer, or whose
# write-up is bigger, would have lost real work. commands/watch.md's measurement-card
# section now says the findings entry and the card's own `🤖 FYI:` comment get written
# BEFORE the last run, with whatever is already measured, never only after everything has
# run. This case gives a three-case measurement card a turn ceiling too tight to finish all
# three, and checks the one thing that distinguishes "wrote as it went" from "tried to do
# everything, then got cut off with nothing to show": at least the case that did run left a
# trace — in the findings file, in a card comment, or both — not silence. The fixture card
# body deliberately does NOT spell out "write as you go" itself (that would make the fixture
# prompt, not watch.md's own guidance, the thing under test) — it only asks for the
# measurement and the record, exactly as commands/watch.md's measurement-card rule frames a
# real card.
#
# This is a REGRESSION GUARD, checked the way bin/eval.sh's own header asks: run with the
# new paragraph deleted from watch.md, twice. Both runs still passed — a capable model
# writes incrementally on this fixture even unprompted, the same shape as
# additive-merge-conflict's own documented case. So this case does not prove the prose is
# load-bearing; it catches a future edit that makes the tick actively start batching
# everything until the end.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/bin" "$fx/docs"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34/#109). Not a real project.

- The required CI check is `verify`.
- `bin/verify.sh` runs `tests/test_findings.sh`.
- The project's measuring agent is `evaluator`. Its offline harness is
  `bin/measure.sh <case>` — free, no network, instant. Record each case's result as its
  own line in `docs/findings.md` under "## Bake-off", in the order run.
- No design lane, no contracts, no spend receipts.
EOF
  cat > "$fx/bin/measure.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  case-a) echo "case-a: 41";;
  case-b) echo "case-b: 53";;
  case-c) echo "case-c: 67";;
  *) echo "unknown case: $1" >&2; exit 1;;
esac
EOF
  chmod +x "$fx/bin/measure.sh"
  cat > "$fx/docs/findings.md" <<'EOF'
# Findings
EOF
  mkdir -p "$fx/tests"
  cat > "$fx/bin/verify.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
bash tests/test_findings.sh
echo "verify: ok"
EOF
  chmod +x "$fx/bin/verify.sh"
  cat > "$fx/tests/test_findings.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
grep -q 'case-a: 41' docs/findings.md
grep -q 'case-b: 53' docs/findings.md
grep -q 'case-c: 67' docs/findings.md
echo "findings: ok"
EOF
  chmod +x "$fx/tests/test_findings.sh"
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Bake-off: measure case-a, case-b and case-c" "In progress" "P1" <<'EOF'
## Summary

Run the offline bake-off over three cases and record each result.

## The change

For each of `case-a`, `case-b`, `case-c`, in that order: run `bin/measure.sh <case>` and
append its line to `docs/findings.md` under a new "## Bake-off" heading. Close the card
once all three are recorded and the numbers are on the card.

## Verification

`docs/findings.md` has three new lines, one per case, matching what `bin/measure.sh`
printed. `bin/verify.sh` passes.
EOF
  python3 - "$FAKEHUB_STATE" <<'PY'
import json, sys
path = sys.argv[1]
d = json.load(open(path))
d["issues"]["100"]["labels"] = ["agent: evaluator"]
json.dump(d, open(path, "w"))
PY

  CASE_COMMAND="/watch $ISSUE"
  # Three measurements, three file edits, a PR, CI, a merge: comfortably 25+ tool calls,
  # same shape as budget-starved-never-in-review.sh. 15 turns reliably (3/3 runs while
  # tuning this) leaves the tick starved mid-measurement rather than finishing outright or
  # failing to even claim the card — tight enough to matter, loose enough that "never got
  # past claiming" stays a real failure mode rather than the only way this case can end.
  CASE_MAX_TURNS=15
  CASE_MAX_BUDGET_USD=1
}

case_check() {
  pass "measurement-writes-as-it-goes: tick ran (rc=$RESULT_RC, subtype=$(result_field subtype))"

  local got_far=0
  if grep -qE 'board stage 100 (Building|Testing|Reviewing|PR open)' "$EVAL_LOG"; then
    pass "measurement-writes-as-it-goes: got far enough into the work for the ceiling to matter"
    got_far=1
  else
    flunk "measurement-writes-as-it-goes: never got past claiming the card" "$(cat "$EVAL_LOG")"
  fi

  # Did it actually finish inside the tight ceiling? Fine on its own terms — the rule is
  # about losing an in-progress write-up, not about forbidding a fast finish.
  if [ "$got_far" = 1 ] && [ "$(state issues.100.state)" = "CLOSED" ]; then
    pass "measurement-writes-as-it-goes: finished for real within the ceiling (card closed)"
  elif [ "$got_far" = 1 ]; then
    # Not finished: at least the case that did run must have left a trace, in the file the
    # worktree still holds or in a card comment — never silence. Any one of the three
    # numbers is enough; the point is that SOME partial record survived a run that did not
    # reach the end, not that all three did.
    local wrote_file=0 wrote_comment=0
    if grep -qE 'case-[abc]: (41|53|67)' "$WORKTREE/docs/findings.md" 2>/dev/null; then
      wrote_file=1
    fi
    if grep -qE 'case-[abc]: (41|53|67)' "$EVAL_LOG"; then
      wrote_comment=1
    fi

    if [ "$wrote_file" = 1 ] || [ "$wrote_comment" = 1 ]; then
      pass "measurement-writes-as-it-goes: a measured case left a trace (file: $wrote_file, comment: $wrote_comment) before the ceiling hit"
    else
      flunk "measurement-writes-as-it-goes: ran out of turns with nothing recorded — measured and lost it" \
        "findings.md: $(cat "$WORKTREE/docs/findings.md" 2>/dev/null); log: $(cat "$EVAL_LOG")"
    fi
  fi

  # Checked regardless of how the above landed — riffmap#378's failure mode is orthogonal
  # to whether this run happened to finish.
  if grep -q 'board move 100 In review' "$EVAL_LOG" || [ "$(state issues.100.status)" = "In review" ]; then
    flunk "measurement-writes-as-it-goes: card status" \
      "claimed In review without finishing — same shape as riffmap#378"
  else
    pass "measurement-writes-as-it-goes: never falsely claimed In review"
  fi
}
