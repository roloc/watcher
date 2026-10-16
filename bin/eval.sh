#!/usr/bin/env bash
#
# The replay suite (card #34): real past failures against the loop's own prompts, each one
# turned into a permanent case, run with a LIVE `claude -p` — the one thing bin/test-spawn.sh
# deliberately never does, because it tests the spawner's bash, not the prompts a model reads.
# No model grades another model: every case's expectations are plain bash/python assertions
# against a stub call log and real git state, exactly test-spawn.sh's own style.
#
#   bin/eval.sh                 # every case in evals/cases/, exit 0 iff all pass
#   bin/eval.sh <name>...       # only the named case(s) (the file's basename, no .sh)
#   EVAL_JOBS=<n> bin/eval.sh   # how many cases run at once (default 4); EVAL_JOBS=1 is
#                                 the old one-at-a-time run.
#   EVAL_LEDGER=<path> bin/eval.sh   # append one tab-separated line per case to <path>:
#                                 timestamp, commit, case name, pass/fail with check counts,
#                                 num_turns, total_cost_usd, wall seconds. Unset (the
#                                 default) writes nothing — a hand run, CI and the nested
#                                 tick on the watcher's own board never touch any file by
#                                 accident. The last line names any case whose turns grew
#                                 past 1.5x what <path> already had on record for it.
#
# On any failure the last line is `failed cases: <name> <name>...`, by file name (not a
# case's own check labels) — what `deploy/autodeploy.sh`'s `evals_state` passes back in as
# this script's own arguments on a second attempt, so a flake costs one case, not a full
# rerun. See the comment next to that logic for the trade this makes.
#
# WHERE THIS RUNS: never CI (`.github/workflows/ci.yml`'s own header explains why — PR code
# never runs on the box holding credentials) and never as part of an ordinary tick's turns.
# It runs by hand, and from the deployer (`deploy/autodeploy.sh`, and `deploy/rollout.sh`,
# its manual fallback) in a SCRATCH clone at the commit about to go live, before
# /opt/watcher moves: this script copies the prompts from the checkout it runs in, so that
# tests the new prompts while every lane is still on the old ones. (Until September 28,
# 2026 rollout.sh ran it in /opt/watcher after the pull, when the new prompts were already
# live.) A commit that fails twice is held with a Stuck card; `deploy-anyway` on the card
# ships it, and `EVAL_SKIP=1 deploy/rollout.sh …` skips it by hand — a flaky live case (see
# the note below) should not be able to jam every deploy.
#
# WHAT A CASE IS: a directory-free single file under evals/cases/, sourced into a fresh
# subshell-free scope with these already set: $TMP (its own scratch dir), $ORIGIN (an empty
# bare git repo standing in for GitHub), $WORKTREE (path for the case to create, via
# `new_worktree` below), $ISSUE (100, the card number), $FAKEHUB_STATE (the JSON board+issue
# store `evals/fakehub.py` reads and writes). It must define:
#
#   case_setup()   - build the fixture: seed_repo a starting commit, new_worktree off it,
#                    seed_card the issue text, set CASE_COMMAND ("/watch 100" or similar).
#                    May also set CASE_MAX_TURNS / CASE_MAX_BUDGET_USD / CASE_TIMEOUT_SEC to
#                    run that one case under its own ceilings instead of the suite's
#                    (EVAL_MAX_TURNS / EVAL_MAX_BUDGET_USD / EVAL_CASE_TIMEOUT_SEC) — tighter
#                    for a case that must run into one, looser for a case whose honest
#                    path is longer than the rest of the suite's (see usability-blocks-sliver).
#   case_check()   - after the tick runs, call pass/flunk against $FAKEHUB_STATE (read it
#                    with evals/state.py), $EVAL_LOG (the stub gh/board call log, one line
#                    per call — grep it), $ORIGIN (real git: `git -C "$ORIGIN" log/show`),
#                    $RESULT_JSON (the tick's own --output-format json result) and $RESULT_RC.
#
# A CASE PROVES A BEHAVIOR HOLDS NOW, NOT ALWAYS THAT ONE SENTENCE IS WHY. Verified while
# building the first three: deleting the "symptom card" rule from watch.md flips
# symptom-survives-deploy-gated to FAIL (the tick closes the card outright) — that one is a
# real prompt-sensitivity proof. Deleting the additive-conflict recipe from watch.md AND its
# restatement in how-it-works.md left additive-merge-conflict passing anyway: a capable
# model reasons its way to "keep both, main's first" unprompted. Both are legitimate cases —
# a case that stays green without the instruction still catches a REGRESSION (a future edit
# that makes the tick actively mishandle the conflict), it just is not proof that today's
# prose is load-bearing for it. Do not assume a new case is discriminating; check it the same
# way, and say in the PR whether it is a sensitivity proof or a regression guard.
#
# SAFETY: the nested tick runs with --dangerously-skip-permissions, same as a real one, but
# its `gh` and `board` resolve to evals/stub-bin/ (PATH is overridden, never inherited) and
# neither one ever shells out to the real `gh` — evals/fakehub.py is a closed simulation that
# dies loudly on any call it does not recognize (see its own header). `git push`/`fetch` go
# to $ORIGIN, a bare repo on local disk, never to a real remote. This is what makes it safe
# to run this suite from inside a live tick working the watcher's own board, which is
# genuinely on the same box as the real `gh` login these fixtures must never touch.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
EVALS="$REPO/evals"
export FAKEHUB_PY="$EVALS/fakehub.py"

# A live tick needs CLAUDE_CODE_OAUTH_TOKEN. The spawner's own ticks get it from systemd's
# EnvironmentFile, but this script also runs by hand and from the deployer (as root,
# via `su - watcher`), neither of which sources it — so read it here if the caller's shell
# does not already have it. Harmless where it does not exist (a laptop, CI never runs this).
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -r /etc/watcher/env ]; then
  set -a; . /etc/watcher/env; set +a
fi

CASE_TIMEOUT="${EVAL_CASE_TIMEOUT_SEC:-480}"
CLAUDE_BIN="${WATCHER_CLAUDE:-claude}"
MAX_TURNS="${EVAL_MAX_TURNS:-60}"
MAX_BUDGET="${EVAL_MAX_BUDGET_USD:-3}"
# owner/repo a case's tick sees in $GH_REPO, exactly as bin/spawn.sh exports it for a real
# lane (PROJECT_REPO) — needed so a case that records a card dependency (`gh api
# repos/$GH_REPO/issues/<n>/dependencies/blocked_by`, commands/watch.md's and
# commands/triage.md's own recipe) has the same env shape as production, not a guess.
EVAL_GH_REPO="${EVAL_GH_REPO:-fixture-org/fixture-repo}"
# Card #154: bin/spawn.sh always exports OWNER_LOGIN for a real lane (defaulting to
# BOARD_OWNER), so a case's tick gets the same env shape as production here too, rather
# than hitting an unset variable no real tick would ever see.
EVAL_OWNER_LOGIN="${EVAL_OWNER_LOGIN:-fixture-owner}"

fail=0; n=0; cases=0; flunks=0
say()   { printf '  %-66s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; flunks=$((flunks+1)); say "$1" "FAIL${2:+ — $2}"; }

# --- helpers a case's case_setup() calls -------------------------------------------------

FIXTURE_EMAIL="fixture@example.com"
FIXTURE_NAME="fixture"

# seed_repo <dir> — commit <dir>'s contents as $ORIGIN's first commit on main.
seed_repo() {
  local src="$1" seed="$TMP/seed"
  git init -q -b main "$seed"
  cp -R "$src/." "$seed/"
  git -C "$seed" -c user.email="$FIXTURE_EMAIL" -c user.name="$FIXTURE_NAME" add -A
  git -C "$seed" -c user.email="$FIXTURE_EMAIL" -c user.name="$FIXTURE_NAME" \
    commit -q -m "initial"
  git -C "$seed" remote add origin "$ORIGIN"
  git -C "$seed" push -q origin main
}

# push_to_main <dir> <message> — commit <dir>'s contents directly onto $ORIGIN's main, as if
# another lane's tick merged while this one was working (the additive-conflict case).
push_to_main() {
  local src="$1" msg="$2" scratch="$TMP/push-$RANDOM"
  git clone -q "$ORIGIN" "$scratch"
  git -C "$scratch" checkout -q main
  cp -R "$src/." "$scratch/"
  git -C "$scratch" -c user.email="$FIXTURE_EMAIL" -c user.name="$FIXTURE_NAME" add -A
  git -C "$scratch" -c user.email="$FIXTURE_EMAIL" -c user.name="$FIXTURE_NAME" \
    commit -q -m "$msg"
  git -C "$scratch" push -q origin main
}

# new_worktree <branch> [base-ref, default main] — what worktree.sh would have handed the
# tick: a clone on its own branch, already carrying the branch/base divergence a case wants.
new_worktree() {
  local branch="$1" base="${2:-main}"
  git clone -q "$ORIGIN" "$WORKTREE"
  git -C "$WORKTREE" checkout -q -b "$branch" "origin/$base"
  git -C "$WORKTREE" config user.email "$FIXTURE_EMAIL"
  git -C "$WORKTREE" config user.name "$FIXTURE_NAME"
}

# seed_card <number> <title> <status> <priority> <<'BODY' ... BODY — puts the card in the
# fake board, body read from stdin so a case can heredoc it verbatim.
seed_card() {
  local num="$1" title="$2" status="$3" priority="$4" body
  body="$(cat)"
  NUM="$num" TITLE="$title" STATUS="$status" PRIORITY="$priority" BODY="$body" \
    python3 - "$FAKEHUB_STATE" <<'PY'
import json, os, sys
path = sys.argv[1]
d = json.load(open(path))
n = os.environ["NUM"]
d["issues"][n] = {
    "number": int(n), "title": os.environ["TITLE"], "body": os.environ["BODY"],
    "state": "OPEN", "labels": [], "status": os.environ["STATUS"],
    "stage": "-", "priority": os.environ["PRIORITY"], "comments": [],
}
json.dump(d, open(path, "w"))
PY
}

state() { python3 "$EVALS/state.py" "$1" 2>/dev/null; }

# result_field <name> — a field out of the tick's own --output-format json result
# ("subtype", "total_cost_usd", "num_turns"), read by a case_check().
result_field() {
  printf '%s' "$RESULT_JSON" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('$1',''))" 2>/dev/null
}

# --- the ledger (card #140) ----------------------------------------------------------------
#
# Three plain functions, never a subshell of their own, so bin/test-eval-ledger.sh can source
# this file (the BASH_SOURCE guard at the bottom keeps that from also running every case) and
# call them directly against a scratch ledger and fake numbers — no live tick involved.

# ledger_prev_turns <ledger> <name> — the num_turns field of <name>'s most recent PASSING
# line in <ledger>, or "" when the file is missing or has no such line. A case's run_case
# below always calls this against the snapshot taken before the current run's own appends
# (never the live ledger) — comparing a case to the entry it is about to write would always
# read as no growth. Only a pass counts as a baseline: a failed run can die early (at its own
# --max-turns ceiling, say) with an artificially low or high turn count that proves nothing
# about the prompt, and taking it as the baseline would let every later run look like a
# regression, or silence the alarm for good.
ledger_prev_turns() {
  local ledger="$1" name="$2"
  [ -f "$ledger" ] || return 0
  awk -F'\t' -v n="$name" '$3==n && $4 ~ /^pass/ {t=$5} END{if (t!="") print t}' "$ledger"
}

# ledger_regressed <ledger> <name> <turns> — prints "<name>: <prev> -> <turns> turns" and
# returns 0 when <turns> is more than 1.5x the last run <ledger> has on record for <name>;
# prints nothing and returns 1 otherwise, including a case's first-ever run (nothing to
# regress against yet).
ledger_regressed() {
  local ledger="$1" name="$2" turns="$3" prev
  prev="$(ledger_prev_turns "$ledger" "$name")"
  case "$prev" in ''|*[!0-9]*) return 1 ;; esac
  case "$turns" in ''|*[!0-9]*) return 1 ;; esac
  if [ "$((turns * 2))" -gt "$((prev * 3))" ]; then
    printf '%s: %s -> %s turns\n' "$name" "$prev" "$turns"
    return 0
  fi
  return 1
}

# ledger_append <ledger> <name> <pass|fail> <passed> <total> <turns> <cost> <seconds> — one
# tab-separated line: UTC timestamp, the commit under test, case name, pass/fail with check
# counts, num_turns, total_cost_usd, wall seconds the tick ran. One printf per line (short,
# O_APPEND): cases run in parallel (card #130) and must never read-modify-write this file.
ledger_append() {
  local ledger="$1" name="$2" verdict="$3" passed="$4" total="$5" turns="$6" cost="$7" secs="$8"
  local ts commit
  ts="$(date -u +%FT%TZ)"
  commit="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  printf '%s\t%s\t%s\t%s %s/%s\t%s\t%s\t%s\n' \
    "$ts" "$commit" "$name" "$verdict" "$passed" "$total" "$turns" "$cost" "$secs" >> "$ledger"
}

# --- the driver ----------------------------------------------------------------------------

run_case() {
  local case_file="$1" name
  name="$(basename "$case_file" .sh)"
  echo "== $name =="
  cases=$((cases + 1))

  TMP="$(mktemp -d)"
  ORIGIN="$TMP/origin.git"
  WORKTREE="$TMP/worktree"
  FAKEHUB_STATE="$TMP/state.json"
  EVAL_LOG="$TMP/log.txt"
  ISSUE=100
  CASE_COMMAND=""
  CASE_MAX_TURNS=""
  CASE_MAX_BUDGET_USD=""
  CASE_TIMEOUT_SEC=""
  export FAKEHUB_STATE FAKEHUB_ORIGIN="$ORIGIN" FAKEHUB_LOG="$EVAL_LOG"

  # A case may export BROWSE_BROWSER (to hand the tick a browser that logs its own starts).
  # Whatever it was before this case, it is again after it, on every way out of here.
  local had_browser="${BROWSE_BROWSER+set}" old_browser="${BROWSE_BROWSER:-}"

  : > "$EVAL_LOG"
  echo '{"next_issue":200,"next_pr":1,"issues":{},"prs":{}}' > "$FAKEHUB_STATE"
  git init -q --bare "$ORIGIN"
  # `git init --bare` points HEAD at refs/heads/master (or init.defaultBranch) before any
  # push ever names "main" — left alone, every later `git clone "$ORIGIN"` (new_worktree,
  # push_to_main, fakehub's own merge scratch clone) tries to check out that nonexistent
  # ref and silently hands back an EMPTY working tree instead of main's contents. Point it
  # at main up front so a plain clone always lands on the real branch.
  git -C "$ORIGIN" symbolic-ref HEAD refs/heads/main

  # shellcheck disable=SC1090
  . "$case_file"
  case_setup

  if [ -z "$CASE_COMMAND" ] || [ ! -d "$WORKTREE/.git" ]; then
    flunk "$name: case_setup" "did not set CASE_COMMAND and a real \$WORKTREE"
    rm -rf "$TMP"
    unset -f case_setup case_check
    if [ -n "$had_browser" ]; then export BROWSE_BROWSER="$old_browser"; else unset BROWSE_BROWSER; fi
    return
  fi

  mkdir -p "$WORKTREE/.claude/commands" "$WORKTREE/.claude/agents"
  cp "$REPO"/commands/*.md "$WORKTREE/.claude/commands/"
  cp "$REPO"/agents/*.md "$WORKTREE/.claude/agents/"
  # The USER-level commands and agents too. On the box the watcher user's ~/.claude links to
  # /opt/watcher — the prompts every lane is running now — and a personal /watch can win
  # over a project one of the same name. A config dir of the case's own, pointing at this
  # checkout, makes both levels the prompts under test, so precedence cannot matter. The
  # login still comes from CLAUDE_CODE_OAUTH_TOKEN, which never lived in that directory.
  local cfg="$TMP/claude-config"
  mkdir -p "$cfg"
  ln -s "$REPO/commands" "$cfg/commands"
  ln -s "$REPO/agents" "$cfg/agents"

  # A case may set CASE_MAX_TURNS / CASE_MAX_BUDGET_USD / CASE_TIMEOUT_SEC in case_setup to
  # run under its own ceilings. Tighter (riffmap#378: proving a tick parks in Stuck rather
  # than claiming "In review" when it runs low needs a cap it can actually run into), or
  # looser (watcher#170, #172: a case whose honest path is longer than the suite's — a real
  # browser, a usability pass, a reviewer pass and a receipt — kept dying at the suite's
  # ceilings on commits that never touched it). Unset means the suite default; every case
  # starts from unset, so one case's ceiling never leaks into the next.
  local turns="${CASE_MAX_TURNS:-$MAX_TURNS}" budget="${CASE_MAX_BUDGET_USD:-$MAX_BUDGET}"
  local secs="${CASE_TIMEOUT_SEC:-$CASE_TIMEOUT}"

  local out="$TMP/result.json" err="$TMP/result.stderr" rc=0 t0 t1 elapsed
  t0="$(date +%s)"
  ( cd "$WORKTREE" && \
    PATH="$EVALS/stub-bin:$PATH" \
    CLAUDE_CONFIG_DIR="$cfg" \
    CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 \
    WATCHER_HOME="$REPO" \
    GH_REPO="$EVAL_GH_REPO" \
    OWNER_LOGIN="$EVAL_OWNER_LOGIN" \
    timeout "$secs" "$CLAUDE_BIN" -p "$CASE_COMMAND" \
      --dangerously-skip-permissions \
      --max-turns "$turns" --max-budget-usd "$budget" \
      --output-format json \
    > "$out" 2> "$err" )
  rc=$?
  t1="$(date +%s)"
  elapsed=$((t1 - t0))

  if [ "$rc" = 124 ]; then
    flunk "$name: tick" "timed out after ${secs}s — see $err"
  fi
  RESULT_JSON="$(cat "$out" 2>/dev/null || echo '{}')"
  RESULT_RC="$rc"

  case_check
  if [ -n "$had_browser" ]; then export BROWSE_BROWSER="$old_browser"; else unset BROWSE_BROWSER; fi

  if [ -n "${EVAL_LEDGER:-}" ]; then
    local num_turns total_cost verdict passed reg
    num_turns="$(result_field num_turns)"
    total_cost="$(result_field total_cost_usd)"
    verdict="pass"; [ "$fail" != 0 ] && verdict="fail"
    passed=$((n - flunks))
    if reg="$(ledger_regressed "${LEDGER_SNAPSHOT:-/dev/null}" "$name" "$num_turns")"; then
      echo "REGRESSION: $reg"
    fi
    ledger_append "$EVAL_LEDGER" "$name" "$verdict" "$passed" "$n" "$num_turns" "$total_cost" "$elapsed"
  fi

  if [ -n "${EVAL_KEEP:-}" ]; then
    echo "  (kept: $TMP)"
  else
    rm -rf "$TMP"
  fi
  unset -f case_setup case_check
}

# Everything above is just function and variable definitions — safe to `source` (card #140's
# bin/test-eval-ledger.sh does exactly that, to reach ledger_append/ledger_regressed without
# running any case for real). Only a direct run of this file gets past here.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then

CASES=()
if [ $# -gt 0 ]; then
  for a in "$@"; do CASES+=("$EVALS/cases/$a.sh"); done
else
  for f in "$EVALS"/cases/*.sh; do CASES+=("$f"); done
fi

# Each case already builds its own scratch dir, its own bare $ORIGIN and its own fake board
# state — nothing is shared between cases but this loop. So run up to EVAL_JOBS of them at
# once, each in its own subshell writing its own log and a one-line result (its own deltas
# to $cases/$n/$fail); once every case has finished, print the logs back in case order and
# sum the results, so the output reads exactly as a sequential run would. EVAL_JOBS=1 still
# runs one case at a time.
JOBS_CAP="${EVAL_JOBS:-4}"
case "$JOBS_CAP" in ''|*[!0-9]*|0) JOBS_CAP=4 ;; esac
JOBDIR="$(mktemp -d)"
# A snapshot of the ledger as it stood before this run's own appends — always created, even
# with no EVAL_LEDGER or no prior file, so run_case's lookup (${LEDGER_SNAPSHOT:-/dev/null})
# always has a real, empty-or-not file rather than branching on whether one exists.
LEDGER_SNAPSHOT="$(mktemp)"
[ -n "${EVAL_LEDGER:-}" ] && [ -f "$EVAL_LEDGER" ] && cp "$EVAL_LEDGER" "$LEDGER_SNAPSHOT"
trap 'rm -rf "$JOBDIR" "$LEDGER_SNAPSHOT"' EXIT

logs=()
results=()

run_job() {
  local f="$1" i="$2"
  logs[i]="$JOBDIR/$i.log"
  results[i]="$JOBDIR/$i.result"

  if [ ! -f "$f" ]; then
    (
      flunk "$(basename "$f" .sh)" "no such case file: $f"
      printf '%s %s %s\n' "$cases" "$n" "$fail" > "${results[i]}"
    ) > "${logs[i]}" 2>&1
    return
  fi

  (
    run_case "$f"
    printf '%s %s %s\n' "$cases" "$n" "$fail" > "${results[i]}"
  ) > "${logs[i]}" 2>&1 &
}

for i in "${!CASES[@]}"; do
  while [ "$(jobs -rp | wc -l)" -ge "$JOBS_CAP" ]; do
    sleep 0.5
  done
  run_job "${CASES[$i]}" "$i"
done
wait

# A case's subshell can die without ever reaching its own printf (a signal, an OOM kill, a
# `set -u` trip inside the case file) — that must count as a failure, same as it exiting
# bin/eval.sh entirely used to before any of this ran in a subshell at all. Never read a
# missing or empty result file as "no change since last case", which would silently score
# a dead case as a pass.
failed_names=()
regressions=()
for i in "${!CASES[@]}"; do
  cat "${logs[i]}"
  while IFS= read -r rline; do
    case "$rline" in "REGRESSION: "*) regressions+=("${rline#REGRESSION: }") ;; esac
  done < "${logs[i]}"
  if [ -s "${results[i]}" ]; then
    read -r d_cases d_n d_fail < "${results[i]}"
  else
    d_cases=1; d_n=1; d_fail=1
    say "$(basename "${CASES[$i]}" .sh): subshell" "FAIL — died without writing a result"
  fi
  cases=$((cases + d_cases))
  n=$((n + d_n))
  if [ "$d_fail" != 0 ]; then
    fail=1
    failed_names+=("$(basename "${CASES[$i]}" .sh)")
  fi
done

echo "$cases case(s), $n check(s), $([ "$fail" = 0 ] && echo "all passed" || echo "SOME FAILED")"
# Card #131: the deployer's second attempt reruns only the case(s) named here, by file name —
# a case's own check labels (e.g. "usability-sliver:") are not its file name, so this is the
# one line that can tell a caller what to pass back to `bin/eval.sh <name>...`.
[ "$fail" != 0 ] && echo "failed cases: ${failed_names[*]}"
# Card #140: a case whose turns grew past 1.5x what $EVAL_LEDGER had on record for it, named
# here in plain words — visible even on an otherwise all-green run.
if [ "${#regressions[@]}" -gt 0 ]; then
  joined="${regressions[0]}"
  for r in "${regressions[@]:1}"; do joined="$joined; $r"; done
  echo "turns grew more than 1.5x since last run: $joined"
fi
exit "$fail"

fi
