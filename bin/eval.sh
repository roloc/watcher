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

HERE="$(cd "$(dirname "$0")" && pwd)"
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

fail=0; n=0; cases=0
say()   { printf '  %-66s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }

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
  export FAKEHUB_STATE FAKEHUB_ORIGIN="$ORIGIN" FAKEHUB_LOG="$EVAL_LOG"

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

  local out="$TMP/result.json" err="$TMP/result.stderr" rc=0
  ( cd "$WORKTREE" && \
    PATH="$EVALS/stub-bin:$PATH" \
    CLAUDE_CONFIG_DIR="$cfg" \
    CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 \
    WATCHER_HOME="$REPO" \
    timeout "$CASE_TIMEOUT" "$CLAUDE_BIN" -p "$CASE_COMMAND" \
      --dangerously-skip-permissions \
      --max-turns "$MAX_TURNS" --max-budget-usd "$MAX_BUDGET" \
      --output-format json \
    > "$out" 2> "$err" )
  rc=$?

  if [ "$rc" = 124 ]; then
    flunk "$name: tick" "timed out after ${CASE_TIMEOUT}s — see $err"
  fi
  RESULT_JSON="$(cat "$out" 2>/dev/null || echo '{}')"
  RESULT_RC="$rc"

  case_check

  if [ -n "${EVAL_KEEP:-}" ]; then
    echo "  (kept: $TMP)"
  else
    rm -rf "$TMP"
  fi
  unset -f case_setup case_check
}

CASES=()
if [ $# -gt 0 ]; then
  for a in "$@"; do CASES+=("$EVALS/cases/$a.sh"); done
else
  for f in "$EVALS"/cases/*.sh; do CASES+=("$f"); done
fi

for f in "${CASES[@]}"; do
  if [ ! -f "$f" ]; then
    flunk "$(basename "$f" .sh)" "no such case file: $f"
    continue
  fi
  run_case "$f"
done

echo "$cases case(s), $n check(s), $([ "$fail" = 0 ] && echo "all passed" || echo "SOME FAILED")"
exit "$fail"
