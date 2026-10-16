#!/usr/bin/env bash
# Offline tests for bin/eval.sh's ledger (card #140) and for run_case's per-case ceilings
# (cards #170, #172): no live tick, no network. Sourcing bin/eval.sh (its BASH_SOURCE guard
# keeps the driver — the part that globs evals/cases/ and runs a real `claude -p` — from
# running) hands us ledger_append, ledger_prev_turns, ledger_regressed and run_case directly,
# so a case here is fake RESULT_JSON-shaped numbers and a scratch ledger file, or a stub
# `claude` that only records the ceilings it was launched under — exactly like every other
# offline suite's stubbed call log.
#
#   bash bin/test-eval-ledger.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/eval.sh"

fail=0; n=0
say()   { printf '  %-70s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else flunk "$what" "$*"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- ledger_append writes one well-shaped line --------------------------------------------

L="$TMP/ledger.tsv"
ledger_append "$L" happy-path pass 5 5 31 0.42 37
check "a first append writes exactly one line" [ "$(wc -l < "$L")" -eq 1 ]
line="$(cat "$L")"
check "it has 7 tab-separated fields" [ "$(awk -F'\t' '{print NF}' "$L")" -eq 7 ]
check "field 3 is the case name" [ "$(cut -f3 "$L")" = happy-path ]
check "field 4 is pass/fail with check counts" [ "$(cut -f4 "$L")" = "pass 5/5" ]
check "field 5 is num_turns" [ "$(cut -f5 "$L")" = 31 ]
check "field 6 is total_cost_usd" [ "$(cut -f6 "$L")" = 0.42 ]
check "field 7 is wall seconds" [ "$(cut -f7 "$L")" = 37 ]
check "field 1 is a UTC timestamp" bash -c "case '$(cut -f1 "$L")' in [0-9][0-9][0-9][0-9]-*Z) exit 0;; *) exit 1;; esac"
check "field 2 is this repo's own short commit, not empty" [ -n "$(cut -f2 "$L")" ]

ledger_append "$L" a-flaky-case fail 3 5 12 0.10 22
check "a fail line names the check counts too" [ "$(awk -F'\t' '$3=="a-flaky-case"{print $4}' "$L")" = "fail 3/5" ]

# --- result_field -> ledger_append, the real pipeline ---------------------------------------

RESULT_JSON='{"num_turns": 23, "total_cost_usd": 0.1234, "subtype": "success"}'
check "result_field reads num_turns out of a fake tick result" [ "$(result_field num_turns)" = 23 ]
check "result_field reads total_cost_usd too" [ "$(result_field total_cost_usd)" = 0.1234 ]
ledger_append "$L" via-result-field pass 4 4 "$(result_field num_turns)" "$(result_field total_cost_usd)" 19
check "the line built from result_field reads back whole" \
  [ "$(awk -F'\t' '$3=="via-result-field"{print $5"/"$6}' "$L")" = "23/0.1234" ]

# --- ledger_prev_turns: the most recent line for a name, not the first ----------------------

check "no file at all: empty, not an error" [ -z "$(ledger_prev_turns "$TMP/no-such-file" happy-path)" ]
check "a real file with no line for this name: empty" [ -z "$(ledger_prev_turns "$L" never-run-before)" ]
check "one line for the name: that line's turns" [ "$(ledger_prev_turns "$L" happy-path)" = 31 ]
ledger_append "$L" happy-path pass 5 5 40 0.50 41
check "two lines for the name: the latest, not the first" [ "$(ledger_prev_turns "$L" happy-path)" = 40 ]
ledger_append "$L" happy-path fail 3 5 2 0.05 6
check "a failed line (died early, at its own cap) is never the baseline" \
  [ "$(ledger_prev_turns "$L" happy-path)" = 40 ]

# --- ledger_regressed: the 1.5x line, and only that line -------------------------------------

SNAP="$TMP/snapshot.tsv"
: > "$SNAP"
ledger_append "$SNAP" case-x pass 5 5 10 0.20 30

not_regressed() { ! ledger_regressed "$@" >/dev/null; }

check "a first-ever run has nothing to regress against" \
  not_regressed "$SNAP" case-x-never-seen 999
check "turns unchanged: not a regression" not_regressed "$SNAP" case-x 10
check "turns up but under 1.5x (10 -> 14): not a regression" \
  not_regressed "$SNAP" case-x 14
check "exactly 1.5x (10 -> 15) is not yet 'more than': not a regression" \
  not_regressed "$SNAP" case-x 15
check "just over 1.5x (10 -> 16) is a regression" ledger_regressed "$SNAP" case-x 16
check "a doubled run (10 -> 20) names the case with both numbers" \
  [ "$(ledger_regressed "$SNAP" case-x 20)" = "case-x: 10 -> 20 turns" ]

# --- parallel appends (card #130's worry): short lines, O_APPEND, never interleaved ---------

P="$TMP/parallel.tsv"
: > "$P"
for i in $(seq 1 20); do
  ( ledger_append "$P" "case-$i" pass "$i" "$i" "$((i*2))" "0.0$i" "$i" ) &
done
wait
check "20 concurrent appends land as 20 whole lines" [ "$(wc -l < "$P")" -eq 20 ]
check "...and every one of them still has 7 fields" \
  bash -c "awk -F'\t' '{if (NF != 7) exit 1}' '$P'"

# --- run_case: a case's own ceilings reach the tick, and never the next case --------------
#
# Cards #170, #172 (October 15, 2026): usability-blocks-sliver died at the suite-wide 480s
# timeout and 60-turn cap on commits that never touched it, so a case may now set
# CASE_TIMEOUT_SEC beside CASE_MAX_TURNS / CASE_MAX_BUDGET_USD. A stub `claude` records the
# argv it was launched with and prints an empty result; a stub `timeout` records the seconds
# it was handed and runs the rest. Neither starts a model, and the stubs are first on PATH,
# so this holds on a Mac with no coreutils `timeout` at all. run_case overwrites $TMP with
# the case's own scratch dir, so the suite's is saved and put back around every call.

STUB="$TMP/stub-bin"; mkdir -p "$STUB"
cat > "$STUB/claude" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$CEILING_LOG.claude"
echo '{"subtype":"success","num_turns":1,"total_cost_usd":0}'
EOF
cat > "$STUB/timeout" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$1" > "$CEILING_LOG.timeout"
shift; exec "$@"
EOF
chmod +x "$STUB/claude" "$STUB/timeout"

cat > "$TMP/own-ceilings.sh" <<'EOF'
case_setup() {
  mkdir -p "$TMP/fx"; echo "fixture" > "$TMP/fx/README.md"
  seed_repo "$TMP/fx"
  new_worktree "card-$ISSUE" main
  CASE_COMMAND="/watch $ISSUE"
  CASE_MAX_TURNS=7
  CASE_MAX_BUDGET_USD=0.5
  CASE_TIMEOUT_SEC=123
}
case_check() { pass "own-ceilings: ran"; }
EOF
cat > "$TMP/suite-ceilings.sh" <<'EOF'
case_setup() {
  mkdir -p "$TMP/fx"; echo "fixture" > "$TMP/fx/README.md"
  seed_repo "$TMP/fx"
  new_worktree "card-$ISSUE" main
  CASE_COMMAND="/watch $ISSUE"
}
case_check() { pass "suite-ceilings: ran"; }
EOF

SUITE_TMP="$TMP"
export CEILING_LOG="$SUITE_TMP/own"
( PATH="$STUB:$PATH" run_case "$SUITE_TMP/own-ceilings.sh" ) > "$SUITE_TMP/own.log" 2>&1
TMP="$SUITE_TMP"
check "a case's CASE_TIMEOUT_SEC is the seconds handed to timeout" \
  [ "$(cat "$CEILING_LOG.timeout" 2>/dev/null)" = 123 ]
check "a case's CASE_MAX_TURNS reaches claude as --max-turns" \
  grep -q -- '--max-turns 7 ' "$CEILING_LOG.claude"
check "a case's CASE_MAX_BUDGET_USD reaches claude as --max-budget-usd" \
  grep -q -- '--max-budget-usd 0.5 ' "$CEILING_LOG.claude"
check "...and the case itself ran to its own check" grep -q 'own-ceilings: ran *ok' "$SUITE_TMP/own.log"

# The very next case sets nothing: it gets the suite's ceilings, not the last case's.
export CEILING_LOG="$SUITE_TMP/suite"
( PATH="$STUB:$PATH" run_case "$SUITE_TMP/suite-ceilings.sh" ) > "$SUITE_TMP/suite.log" 2>&1
TMP="$SUITE_TMP"
check "a case that sets nothing is handed the suite's own timeout ($CASE_TIMEOUT)" \
  [ "$(cat "$CEILING_LOG.timeout" 2>/dev/null)" = "$CASE_TIMEOUT" ]
check "...and the suite's own --max-turns ($MAX_TURNS), not the previous case's 7" \
  grep -q -- "--max-turns $MAX_TURNS " "$CEILING_LOG.claude"
check "...and the suite's own --max-budget-usd ($MAX_BUDGET), not the previous case's 0.5" \
  grep -q -- "--max-budget-usd $MAX_BUDGET " "$CEILING_LOG.claude"

echo "$n check(s), $([ "$fail" = 0 ] && echo "all passed" || echo "SOME FAILED")"
exit "$fail"
