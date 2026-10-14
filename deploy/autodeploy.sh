#!/usr/bin/env bash
# Deploy the watcher's `main` onto this box by itself, without ever waiting for a lane to go
# idle. Run as root by watcher-deploy.timer every few minutes; one run is one step, and it
# is idempotent, so a run that stops half way is picked up by the next. Nobody runs
# deploy/rollout.sh by hand any more (Steve, September 28, 2026: "I don't want to run
# rollout ever").
#
# Why this is on the box and not a CI job: a CI job that deploys here needs a GitHub runner
# on the box that holds the bot's GitHub login and the Claude token, and ci.yml's rule is
# that PR code never runs there. So CI decides WHETHER a commit may ship (its required check
# is green) and this decides WHEN (as soon as it can). It only ever reads GitHub.
#
# Card #106: each shipped commit gets its OWN folder under $RELEASES_DIR, never touched
# again once made, and $WATCHER_DIR (/opt/watcher) is a symlink — "the pointer" — this
# script flips onto the new one's folder. A lane never has its folder changed under it: a
# tick is pinned to whatever $WATCHER_DIR resolved to when bin/spawn.sh's own process
# started (see its own comment), so flipping the pointer disturbs nothing running. Each
# lane's own idle poll notices the pointer moved and re-launches itself from the new folder
# at its own next tick — no pause, no drain, no wait, here or there. (Before this card, this
# script paused every lane and waited for every running tick to finish before moving
# /opt/watcher in place; a deploy that landed while a lane was mid-tick could cost that lane
# over an hour of idle time, times every lane on the box.)
#
# One run, in order:
#   1. Fetch origin/main. Nothing new: exit quietly.
#   2. The commit's required check (`verify`) must be green. Pending: wait for a later run.
#   3. The replay suite (bin/eval.sh) runs in a SCRATCH clone at that exact commit, before
#      anything goes live — the suite copies the prompts from the checkout it runs in, so
#      this tests the new prompts, not the ones lanes are already using. Skipped when the
#      commits touch nothing a model reads. A fail is retried once (a live suite has flakes,
#      roloc/watcher#44); a second fail holds the commit.
#   3b. THE CANARY STAGE (card #46), only when the commit touches what a model reads and at
#      least one active lane's project file sets LANE_CHANNEL=canary: move the canary
#      checkout (/opt/watcher-canary — a second, independent clone; see bin/spawn.sh's own
#      LANE_CHANNEL shim) onto the commit and restart ONLY its lane(s), before the pointer
#      moves at all. This stage is unchanged by card #106 — it still pauses and restarts its
#      own lane(s) directly, on purpose: it is bake-testing a risky change on a small,
#      consenting set of lanes, not an ordinary deploy. A run later sees the soak through:
#      DEPLOY_CANARY_SOAK_TICKS clean ticks on those lanes promotes (falls through to step 4
#      below); DEPLOY_CANARY_QUIET_MIN minutes (default 30 — Steve's call, October 12, 2026:
#      a prompt change should not sit for an afternoon just because the Watcher board is
#      empty) with no canary work at all promotes anyway, so
#      a quiet Watcher board cannot hold every other project back forever; one tick that does
#      not finish clean (killed, a subtype of error_during_execution, or parked after 2
#      attempts) rolls the canary checkout back to the commit before it and holds this one
#      exactly like step 2/3 would. A project with no canary lane, or a commit that skips the
#      suite in step 3, skips this stage entirely and promotes straight away — canary is
#      additive, never a new gate on an ordinary deploy.
#   4. Clone the commit into its own folder under $RELEASES_DIR, run that folder's own
#      install.sh (without a pull — this clone IS the commit), and flip the pointer onto it.
#      A sweep then removes every older release folder no lane's own state still names,
#      always keeping the newest two.
#
# A HELD commit (red check, the suite failing twice, or a canary tick that did not finish
# clean) gets one card on the watcher's board in Stuck, in the talking-to-steve.md shape.
# Moving it to In progress lets the watcher fix the cause; adding the `deploy-anyway` label
# ships it (and skips both the suite and the canary stage). Nothing here ever signals a
# tick, force-pushes, or posts to Discord except through that card's move (board.py posts
# the Stuck alert, as it does for every card).
#
# Card #105: every step above that can actually block a run writes `$DSTATE/step` on
# entering it (write_step, clear_step — a fact, not a log line, so it is still true for as
# long as the step lasts, even the replay suite's own several minutes inside one run) —
# cleared once the target is live or held. It is the floor page's Ship desk's only source
# for "which step, out of how many" and, together with the canary stage's own PAUSE files,
# "which lane(s) it is waiting on and by when" (bin/floor.py, deploy_status).
set -uo pipefail

WATCHER_DIR="${WATCHER_DIR:-/opt/watcher}"
# Card #106: every release this script ever makes lives here, named by its commit (the
# first 12 hex chars are plenty — see release_dir). $WATCHER_DIR becomes a symlink into one
# of these; never a release's own real directory.
RELEASES_DIR="${DEPLOY_RELEASES_DIR:-/opt/watcher-releases}"
STATE_ROOT="${WATCHER_STATE_ROOT:-/home/watcher/.local/state/watcher}"
DSTATE="${DEPLOY_STATE:-/var/lib/watcher-deploy}"
SCRATCH_ROOT="${DEPLOY_SCRATCH:-/home/watcher/.cache/watcher-deploy}"
CGROUP_ROOT="${DEPLOY_CGROUP_ROOT:-/sys/fs/cgroup/system.slice}"
RUN_AS="${DEPLOY_USER:-watcher}"
BRANCH="${DEPLOY_BRANCH:-main}"
CHECK="${DEPLOY_CHECK:-verify}"
# The project whose repo is this one, and whose board gets a held commit's card.
SELF_PROJECT="${DEPLOY_PROJECT:-watcher}"
EVALS="${DEPLOY_EVALS:-1}"                     # 0 skips the replay suite entirely
# What a model reads: a commit range touching none of these skips the suite.
EVAL_PATHS="${DEPLOY_EVAL_PATHS:-commands agents docs evals bin/eval.sh CLAUDE.md}"
# Card #140: the replay suite's own ledger — one line per case per run, pass/fail, turns and
# quota, next to the per-commit logs already under $DSTATE/evals (mkdir -p below makes the
# directory). Nothing else should need to override this.
EVAL_LEDGER_PATH="${DEPLOY_EVAL_LEDGER:-$DSTATE/evals/ledger.tsv}"
# The canary checkout (card #46) — see bin/spawn.sh's own default for WATCHER_CANARY_DIR,
# which this must match. SOAK_TICKS and QUIET_MIN are read together: whichever the canary
# lane(s) reach first clears the way to promote.
CANARY_DIR="${DEPLOY_CANARY_DIR:-/opt/watcher-canary}"
CANARY_SOAK_TICKS="${DEPLOY_CANARY_SOAK_TICKS:-3}"
CANARY_QUIET_MIN="${DEPLOY_CANARY_QUIET_MIN:-30}"

# Every line goes to the journal, and to $DSTATE/history: a plain file the floor page's
# Ship desk reads for its Peek (bin/floor.py). The page runs as the lanes' user, which
# cannot read the journal. Best-effort — a write that fails must never fail a deploy — and
# trimmed, so it cannot grow without bound.
HISTORY_KEEP="${DEPLOY_HISTORY_LINES:-200}"
# Not a number would abort the first `say`, and with it the deploy: fall back, never fail.
case "$HISTORY_KEEP" in ''|*[!0-9]*) HISTORY_KEEP=200 ;; esac
say() {
  local now h="$DSTATE/history"
  now="$(date -u +%FT%TZ)"
  printf '%s autodeploy: %s\n' "$now" "$*"
  [ -d "$DSTATE" ] || return 0
  { printf '%s %s\n' "$now" "$*" >> "$h"; } 2>/dev/null || return 0
  if [ "$( { wc -l < "$h"; } 2>/dev/null || echo 0)" -gt $((HISTORY_KEEP * 2)) ]; then
    { tail -n "$HISTORY_KEEP" "$h" > "$h.tmp" && mv "$h.tmp" "$h"; } 2>/dev/null
  fi
  return 0
}
as_user() { su - "$RUN_AS" -c "$1"; }
# q <word>...: the words shell-quoted, for a command line that as_user runs.
q() { local out="" w; for w in "$@"; do out+="$(printf '%q' "$w") "; done; printf '%s' "${out% }"; }
wgit() { as_user "git -C '$WATCHER_DIR' $1"; }
cgit() { as_user "git -C '$CANARY_DIR' $1"; }
mkdir -p "$DSTATE/evals" "$DSTATE/held"
# Card #140: the ledger append happens inside the scratch clone, as $RUN_AS (as_user), not
# as this script's own root. Chown the ledger FILE only, never the directory: $DSTATE/evals
# also holds this script's own pass/fail record per commit (evals_state's $rec, written by
# root) — owning the directory would let anything that runs as $RUN_AS, which includes every
# lane and the suite's own live ticks, delete or rewrite that record.
touch "$EVAL_LEDGER_PATH" 2>/dev/null || true
chown "$RUN_AS" "$EVAL_LEDGER_PATH" 2>/dev/null || true

# say_once <key> <line>: journal a waiting line once per state, not once per run.
say_once() {
  local key="$1"; shift
  [ "$(cat "$DSTATE/said" 2>/dev/null)" = "$key" ] && return 0
  echo "$key" > "$DSTATE/said"; say "$*"
}

# write_step/clear_step: $DSTATE/step, a fact for the floor page's Ship desk (card #105) —
# four lines (step number, of how many, its plain-words name, one line of detail) so the
# page reads what the deployer is doing rather than re-deriving its own branching. Written
# each time a run enters a step, so a step that blocks THIS SAME invocation for minutes (the
# replay suite) still reads correctly for as long as it runs; cleared once the target is
# live or held, so a reader never sees a step left over from a different commit. Best
# effort, like every other write under $DSTATE: a failure here must never fail a deploy.
write_step() {
  local n="$1" total="$2" name="$3"; shift 3
  { printf '%s\n%s\n%s\n%s\n' "$n" "$total" "$name" "$*" > "$DSTATE/step.tmp" \
      && mv "$DSTATE/step.tmp" "$DSTATE/step"; } 2>/dev/null || true
}
clear_step() { rm -f "$DSTATE/step" "$DSTATE/step.tmp" 2>/dev/null || true; }

# --- the lanes ------------------------------------------------------------------------------

active_lanes() {
  systemctl list-units 'watcher@*.service' --state=active --no-legend --plain 2>/dev/null \
    | awk '{print $1}' | sed -n 's/^watcher@\(.*\)\.service$/\1/p'
}

# A lane file is projects/<name>.env with no dot in <name>. Anything else under projects/
# is a project's own helper (a probe script, a `<name>.detect.env` config) and never a lane:
# starting one as watcher@<file> is a unit that fails and restarts every 30 seconds.
is_lane_file() {
  local b; b="$(basename "$1")"
  [ "${b%.env}" != "$b" ] && [ -n "${b%.env}" ] && [ "${b%.env}" = "${b%%.*}" ]
}

# The project a lane belongs to: the shortest project file name it equals or extends with
# "-" (riffmap-design -> riffmap), the same naming rollout.sh and install.sh rely on.
project_of() {
  local lane="$1" best="" f m
  for f in "$WATCHER_DIR"/projects/*.env; do
    [ -f "$f" ] && is_lane_file "$f" || continue
    m="$(basename "$f" .env)"
    if [ "$lane" = "$m" ] || [ "${lane#"$m"-}" != "$lane" ]; then
      [ -z "$best" ] || [ "${#m}" -lt "${#best}" ] && best="$m"
    fi
  done
  echo "${best:-$lane}"
}

# A tick is a `claude … --dangerously-skip-permissions` process in the lane's cgroup —
# rollout.sh's test, so another lane's tick never counts. No cgroup: the spawner's own
# bookkeeping.
tick_running() {
  local lane="$1" procs="$CGROUP_ROOT/watcher@$1.service/cgroup.procs" pid
  # The spawner holds `current` from launch until the end of its exit handling, which keeps
  # running (unclaim, attempts, comments) after claude itself has gone. Restarting the unit
  # in that window kills the cleanup and the next spawner reports a dead run on a card that
  # finished fine. A stale `current` cannot block for long: every poll, paused or not,
  # reconciles it first.
  [ -s "$STATE_ROOT/$lane/current" ] && return 0
  if [ -r "$procs" ]; then
    while read -r pid; do
      [ -r "/proc/$pid/cmdline" ] || continue
      tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- '--dangerously-skip-permissions' && return 0
    done < "$procs"
    return 1
  fi
  return 1
}

# Card #106: the ordinary deploy path no longer pauses or waits for any lane (every lane
# keeps its own folder for the whole life of its current tick, so nothing it is doing can be
# disturbed) — only the canary stage still pauses, and only the lane(s) that opted into it
# (canary_pause/canary_unpause, below), which is why `ours` and PAUSE-file bookkeeping
# survive here for that one caller.
ours() { head -c 7 "$1" 2>/dev/null | grep -q '^deploy '; }

# --- a held commit --------------------------------------------------------------------------

# The board and repo for held cards come from the watcher's own project file.
self_env() {
  local f="$WATCHER_DIR/projects/$SELF_PROJECT.env"
  [ -f "$f" ] || return 1
  ( set +u; . "$f"; echo "${PROJECT_REPO:-} ${BOARD_OWNER:-} ${BOARD_NUMBER:-}" )
}

# overridden <sha>: Steve added deploy-anyway to that commit's held card.
overridden() {
  local n repo
  n="$(cat "$DSTATE/held/$1" 2>/dev/null)"; [ -n "$n" ] || return 1
  read -r repo _ _ <<< "$(self_env)"; [ -n "$repo" ] || return 1
  as_user "$(q gh issue view "$n" -R "$repo" --json labels -q '.labels[].name')" 2>/dev/null \
    | grep -qx 'deploy-anyway'
}

# hold <sha> <why, plain words> <details>: one Stuck card per commit, never repeated.
hold() {
  local sha="$1" why="$2" details="$3" short="${1:0:7}" repo owner board url n body
  [ -f "$DSTATE/held/$sha" ] && return 0
  say "HOLDING $short: $why"
  read -r repo owner board <<< "$(self_env)"
  if [ -z "$repo" ]; then
    say "no projects/$SELF_PROJECT.env to file a card from; the hold is in this journal only"
    : > "$DSTATE/held/$sha"; return 0
  fi
  # Written where the bot's gh can read it: the state directory is root's.
  body="$(mktemp /tmp/watcher-deploy-held.XXXXXX)"; chmod 644 "$body"
  cat > "$body" <<EOF
**Needs Steve: the watcher's newest version is held back and not deployed.**

- The version is \`$short\` on \`main\`: https://github.com/$repo/commit/$sha
- It was not deployed because $why.
- The box keeps running the version before it. Nothing is broken.
- Newer versions are still checked and deployed on their own if they pass.

**Look:** https://github.com/$repo/commit/$sha

What should happen to this version?

**A.** (recommended) **Hold** it, and let the watcher find and fix the cause.
**B.** **Ship** it anyway, as it is.

For A, move this card to **In progress**. For B, add the **deploy-anyway** label to this card.

<details><summary>Details</summary>

$details

Posted by \`deploy/autodeploy.sh\` on the agents box. It reads this card's labels on every run.
</details>
EOF
  url="$(as_user "$(q gh issue create -R "$repo" --title "Deploy held: $short is not live, because $why" --body-file "$body")" 2>/dev/null | tail -1)"
  rm -f "$body"
  n="${url##*/}"
  if ! [[ "$n" =~ ^[0-9]+$ ]]; then
    say "could not file the held card (gh failed); trying again next run"
    return 0
  fi
  echo "$n" > "$DSTATE/held/$sha"
  # Onto the board by hand (auto-add is not relied on), then Stuck — board.py's move is what
  # posts the Discord alert, after the change lands. The move is retried: GitHub's board
  # index lags a just-added item by seconds, and on September 28, 2026 all four held cards
  # were filed and never reached Stuck, so nobody heard about six hours of held deploys.
  as_user "$(q gh project item-add "$board" --owner "$owner" --url "$url") >/dev/null 2>&1"
  local try
  for try in 1 2 3 4; do
    sleep "${DEPLOY_MOVE_RETRY_SEC:-10}"
    if as_user "$(q env BOARD_OWNER="$owner" BOARD_NUMBER="$board" board move "$n" 'Stuck - Needs help') >/dev/null 2>&1"; then
      say "filed $url in Stuck"; return 0
    fi
  done
  say "filed $url but could not move it to Stuck after 4 tries"
}

# close_held_except <sha>: a deployed commit supersedes every older held card.
close_held_except() {
  local f s n repo
  read -r repo _ _ <<< "$(self_env)" || return 0
  for f in "$DSTATE"/held/*; do
    [ -f "$f" ] || continue
    s="$(basename "$f")"; n="$(cat "$f")"
    [ "$s" = "$1" ] && continue
    [ -n "$n" ] && as_user "gh issue close $n -R '$repo' --comment '🤖 FYI: superseded. \`${1:0:7}\` is deployed, so \`${s:0:7}\` never will be.' >/dev/null 2>&1"
    rm -f "$f"
  done
}

# --- the gates ------------------------------------------------------------------------------

# check_state <sha>: success | pending | failure, for the required check on that commit.
check_state() {
  local repo out
  read -r repo _ _ <<< "$(self_env)"
  [ -n "$repo" ] || repo="$(wgit 'remote get-url origin' | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
  out="$(as_user "$(q gh api "repos/$repo/commits/$1/check-runs?check_name=$CHECK" -q '.check_runs[] | .status + " " + (.conclusion // "")')" 2>/dev/null)" \
    || { echo pending; return; }
  case "$out" in
    "") echo pending ;;
    *"completed success"*) echo success ;;
    *completed*) echo failure ;;
    *) echo pending ;;
  esac
}

# evals_state <cur> <sha>: pass | fail, running the suite if this commit has no result yet.
evals_state() {
  local cur="$1" sha="$2" rec="$DSTATE/evals/$2" tries scratch log cmd retry_cases
  if [ "$EVALS" != 1 ]; then echo pass; return; fi
  # shellcheck disable=SC2086
  if [ -z "$(wgit "diff --name-only $cur $sha -- $EVAL_PATHS")" ]; then echo pass; return; fi
  case "$(cat "$rec" 2>/dev/null)" in pass) echo pass; return ;; "fail 2") echo fail; return ;; esac
  tries=0; [ "$(cat "$rec" 2>/dev/null)" = "fail 1" ] && tries=1
  scratch="$SCRATCH_ROOT/${sha:0:12}"; log="$DSTATE/evals/$sha.log"
  as_user "rm -rf '$scratch' && mkdir -p '$SCRATCH_ROOT' && git clone -q --shared '$WATCHER_DIR' '$scratch' && git -C '$scratch' checkout -q --detach $sha" >&2 \
    || { say "could not make a scratch clone at ${sha:0:7}; trying again next run" >&2; echo pending; return; }
  # Card #131: a second attempt reruns only the case(s) bin/eval.sh's own "failed cases: …"
  # line named on attempt 1 (read from $log before this attempt overwrites it) — a flake
  # costs one case, not the other ~14. That is a real trade: a flake elsewhere on attempt 2
  # goes unseen, on purpose, in exchange for not spending 20+ minutes re-proving cases that
  # already passed. An attempt-1 log with no such line (it died before printing it, or
  # predates this) reruns everything, same as always.
  cmd="bin/eval.sh"
  if [ "$tries" -ge 1 ]; then
    retry_cases="$(grep '^failed cases: ' "$log" 2>/dev/null | tail -1 | sed 's/^failed cases: //')"
    [ -n "$retry_cases" ] && cmd="bin/eval.sh $(q $retry_cases)"
  fi
  say "running the replay suite on ${sha:0:7} (attempt $((tries+1)) of 2)" >&2
  if as_user "cd '$scratch' && EVAL_LEDGER='$EVAL_LEDGER_PATH' $cmd" > "$log" 2>&1; then
    echo pass > "$rec"; say "replay suite passed on ${sha:0:7}" >&2
  else
    tries=$((tries+1)); echo "fail $tries" > "$rec"
    say "replay suite FAILED on ${sha:0:7} (attempt $tries of 2)" >&2
  fi
  as_user "rm -rf '$scratch'" >&2
  case "$(cat "$rec")" in pass) echo pass ;; "fail 2") echo fail ;; *) echo retry ;; esac
}

# --- the canary stage (card #46) --------------------------------------------------------------

has_canary() { [ -d "$CANARY_DIR/.git" ]; }

# canary_lane <lane> — this lane's project file sets LANE_CHANNEL=canary. Always read from
# $WATCHER_DIR: the systemd unit points WATCHER_PROJECT at the stable checkout's copy no
# matter which checkout the lane's own spawn.sh ends up re-execing into (its own shim's
# doc comment explains why), so this is the one place that fact is ever decided.
canary_lane() {
  local l="$1" f
  f="$WATCHER_DIR/projects/$(project_of "$l").env"
  [ -f "$f" ] && grep -qx 'LANE_CHANNEL=canary' "$f"
}

canary_lanes() { local l; for l in $(active_lanes); do canary_lane "$l" && echo "$l"; done; }

# canary_needed <cur> <tgt> — is there a canary stage to run at all? A checkout to run it
# in, a lane that opted in, and a commit that touches what a model reads — the same
# EVAL_PATHS test evals_state already gated on, so a commit that skipped the suite entirely
# (nothing here for a canary to catch that CI did not already) skips this stage too.
canary_needed() {
  local cur="$1" tgt="$2"
  has_canary || return 1
  [ -n "$(canary_lanes)" ] || return 1
  # shellcheck disable=SC2086
  [ -n "$(wgit "diff --name-only $cur $tgt -- $EVAL_PATHS")" ]
}

# canary_died <since_epoch> — true if a canary lane's own state shows a tick that has not
# finished clean since the soak for the current commit started: a result file the tick
# never got to write (killed by the watchdog or the box), one whose subtype is
# error_during_execution, or a card the spawner parked after 2 attempts. Read entirely from
# the lane's own state directory — the same files the spawner itself writes — never gh.
#
# A lane with a tick still running is skipped entirely, not judged: bin/spawn.sh creates
# its result json by redirection AT LAUNCH (`launch_tick`), so it sits at 0 bytes for the
# tick's whole run — indistinguishable, by content alone, from a tick the watchdog killed
# before it could write anything. `tick_running` (already used by canary_busy) is what the
# deployer everywhere else uses to tell "still going" from "gone"; once it says gone, THEN
# an empty or unparseable file really does mean killed, and is judged as such below.
canary_died() {
  local since="$1" l dir parked
  for l in $(canary_lanes); do
    tick_running "$l" && continue
    dir="$STATE_ROOT/$l"
    if as_user "python3 -c \"
import glob, json, os, sys
since = $since
for f in glob.glob('$dir/logs/*.json'):
    if os.path.getmtime(f) < since:
        continue
    try:
        d = json.load(open(f))
    except Exception:
        sys.exit(0)
    if d.get('subtype') == 'error_during_execution':
        sys.exit(0)
sys.exit(1)
\""; then
      return 0
    fi
    parked="$(files_since "$since" "$dir/parked" '' 0)"
    [ "${parked:-0}" -gt 0 ] && return 0
  done
  return 1
}

# files_since <since_epoch> <dir> <suffix> <min_bytes> — how many regular files anywhere
# under <dir> have a name ending in <suffix>, hold at least <min_bytes>, and were modified
# after <since_epoch>. A missing <dir> is 0. This is what GNU
# `find <dir> -type f -name '*<suffix>' -newermt '@<since>'` counts, to the nanosecond, and
# it is python because that spelling is GNU's alone: BSD find cannot parse '@<epoch>', and
# with its stderr thrown away the error read as "no such files" — a soak that never
# cleared and a parked card that never held, on a Mac, which is where bin/test-deploy.sh
# runs before a push. stderr is left alone here for the same reason.
files_since() {
  as_user "python3 -c \"
import os, stat, sys
since_ns, top, suffix, least = int(sys.argv[1]) * 10**9, sys.argv[2], sys.argv[3], int(sys.argv[4])
n = 0
for d, _, names in os.walk(top):
    for name in names:
        if not name.endswith(suffix):
            continue
        try:
            st = os.lstat(os.path.join(d, name))
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size >= least and st.st_mtime_ns > since_ns:
            n += 1
print(n)
\" $(q "$@")"
}

# canary_ticks <since_epoch> — how many result files the canary lane(s) have COMPLETED
# (written something) since. The 1-byte floor excludes the in-progress tick's own
# still-empty file (see canary_died above) — without it a soak could clear on a tick that
# never actually finished.
canary_ticks() {
  local since="$1" l n=0 c
  for l in $(canary_lanes); do
    c="$(files_since "$since" "$STATE_ROOT/$l/logs" .json 1)"
    n=$(( n + ${c:-0} ))
  done
  echo "$n"
}

# canary_busy — the canary lane(s) currently running a tick.
canary_busy() { local l; for l in $(canary_lanes); do tick_running "$l" && echo "$l"; done; }

# canary_pause/canary_unpause <sha> — scoped to only the canary lane(s): moving the canary
# checkout out from under a card in flight is exactly the restart-kills-the-cleanup bug
# active_lanes' own comment warns about, so a lane with a tick running is never restarted —
# it is paused (stops picking up new work) and waited on. The stable release path (swap,
# below) needs none of this any more (card #106): it never touches a running tick's folder
# at all, so it never pauses or waits for one.
canary_pause() {
  local sha="$1" l p
  for l in $(canary_lanes); do
    p="$STATE_ROOT/$l/PAUSE"
    if [ -f "$p" ] && ! ours "$p"; then
      say "watcher@$l was already paused by hand; leaving its PAUSE as it is"
      continue
    fi
    mkdir -p "$STATE_ROOT/$l"
    echo "deploy $sha" > "$p"; chown "$RUN_AS:$RUN_AS" "$STATE_ROOT/$l" "$p" 2>/dev/null
  done
}
canary_unpause() {
  local l p
  for l in $(canary_lanes); do
    p="$STATE_ROOT/$l/PAUSE"
    ours "$p" && rm -f "$p"
  done
}

# canary_stage <tgt> — advances the canary checkout for <tgt>, reported by exit status:
#   0  clear to promote (a clean or quiet soak finished)
#   1  wait (idle lane(s) pending, just swapped and starting the clock, or still soaking)
#   2  died, or could not move at all — CANARY_HOLD_WHY/CANARY_HOLD_DETAILS are set for the
#      caller to hold <tgt> with, exactly like a failed check or suite would
# Never called from inside a command substitution: it logs with `say` on its own stdout,
# same as every other step of a run, and reports only through its exit status and the two
# globals above — mixing those into a captured "$(...)" string is exactly the bug
# evals_state's own >&2 redirects exist to avoid.
canary_stage() {
  local tgt="$1" rec="$DSTATE/canary-soak" recorded_sha="" since="" prev lanes l

  # A commit already rolled back once is never retried on its own: without this, the very
  # next run reads no soak record (died, below, clears it), sees recorded_sha != tgt again,
  # and swaps the canary checkout right back onto the commit that just failed it — forever,
  # restarting the canary lane every deploy tick. Only a newer commit, or Steve's own
  # deploy-anyway (which skips this whole stage), ever moves past it.
  if [ "$(cat "$DSTATE/canary-died" 2>/dev/null)" = "$tgt" ]; then
    CANARY_HOLD_WHY="a canary tick on it did not finish clean"
    CANARY_HOLD_DETAILS="The canary checkout already rolled back off \`${tgt:0:7}\` once; it will not be tried again on its own. Fix the cause and push a new commit, or add \`deploy-anyway\` to ship this one regardless."
    return 2
  fi

  [ -f "$rec" ] && read -r recorded_sha since < "$rec"
  if [ "$recorded_sha" != "$tgt" ]; then
    canary_pause "$tgt"
    if [ -n "$(canary_busy)" ]; then
      say "canary: waiting for the canary lane(s) to go idle before moving to ${tgt:0:7}"
      CANARY_STEP_DETAIL="waiting for the canary lane(s) to go idle before moving to ${tgt:0:7}"
      return 1
    fi
    # $tgt is only ever a commit $WATCHER_DIR already fetched (main's own fetch, at the top
    # of this run) — this checkout is a separate clone, so it needs its own before either
    # a fast-forward or the reset fallback can even see the commit object.
    cgit "fetch -q origin $BRANCH" >/dev/null 2>&1
    prev="$(cgit 'rev-parse HEAD')"
    if ! cgit "merge -q --ff-only $tgt" >/dev/null 2>&1 && ! cgit "reset -q --hard $tgt" >/dev/null 2>&1; then
      canary_unpause
      CANARY_HOLD_WHY="the canary checkout could not be moved onto it"
      CANARY_HOLD_DETAILS="\`git merge --ff-only\` and \`git reset --hard\` both failed moving \`$CANARY_DIR\` to \`${tgt:0:7}\`. Something is wrong with that checkout; fix it by hand, or add \`deploy-anyway\` to skip the canary stage for this commit."
      return 2
    fi
    cgit "config core.fileMode false" || true
    as_user "chmod +x '$CANARY_DIR'/bin/*.sh '$CANARY_DIR'/bin/shim/* '$CANARY_DIR'/deploy/*.sh" 2>/dev/null || true
    lanes="$(canary_lanes | tr '\n' ' ')"
    for l in $lanes; do systemctl restart "watcher@$l" && say "watcher@$l restarted on the canary checkout at ${tgt:0:7}"; done
    canary_unpause
    since="$(date +%s)"
    printf '%s %s\n' "$tgt" "$since" > "$rec"
    echo "$prev" > "$DSTATE/canary-prev"
    say "canary: ${tgt:0:7} is live for ${lanes% }; soaking"
    CANARY_STEP_DETAIL="soaking: 0 of $CANARY_SOAK_TICKS clean tick(s) so far"
    return 1
  fi

  if canary_died "$since"; then
    prev="$(cat "$DSTATE/canary-prev" 2>/dev/null)"
    lanes="$(canary_lanes | tr '\n' ', ')"; lanes="${lanes%, }"
    if [ -n "$prev" ]; then
      cgit "reset -q --hard $prev" >/dev/null 2>&1
      for l in $(canary_lanes); do systemctl restart "watcher@$l"; done
      say "canary: a tick on ${tgt:0:7} did not finish clean; rolled the canary checkout back to ${prev:0:7}"
    fi
    rm -f "$rec" "$DSTATE/canary-prev"
    echo "$tgt" > "$DSTATE/canary-died"
    CANARY_HOLD_WHY="a canary tick on it did not finish clean"
    CANARY_HOLD_DETAILS="The canary lane(s) ($lanes) ran \`${tgt:0:7}\` and at least one tick did not finish clean (killed, \`error_during_execution\`, or parked after 2 attempts). The canary checkout is back on \`${prev:0:7}\`."
    return 2
  fi

  local ticks; ticks="$(canary_ticks "$since")"
  if [ "$ticks" -ge "$CANARY_SOAK_TICKS" ]; then
    say "canary: ${tgt:0:7} had $ticks clean tick(s) on the canary lane(s); promoting"
    return 0
  fi
  if [ -z "$(canary_busy)" ] && [ $(( $(date +%s) - ${since:-0} )) -ge $(( CANARY_QUIET_MIN * 60 )) ]; then
    say "canary: ${tgt:0:7} has been quiet on the canary lane(s) for over $CANARY_QUIET_MIN min; promoting anyway"
    return 0
  fi
  CANARY_STEP_DETAIL="soaking: $ticks of $CANARY_SOAK_TICKS clean tick(s) so far"
  return 1
}

# --- the release ----------------------------------------------------------------------------

# release_dir <sha> — the folder a given commit's release lives in, named by the first 12
# hex chars, the same abbreviation every scratch/canary clone in this file already uses.
release_dir() { printf '%s/%s' "$RELEASES_DIR" "${1:0:12}"; }

# adopt_plain_checkout — a one-time migration. Every box that existed before card #106 has
# $WATCHER_DIR as a real checkout, not a pointer; the first run of this code folds it into
# $RELEASES_DIR under its own HEAD and replaces it with a symlink, so every commit after
# this one can flip the pointer instead of pausing. A no-op once $WATCHER_DIR is already a
# symlink — which is every run after the first.
adopt_plain_checkout() {
  [ -L "$WATCHER_DIR" ] && return 0
  [ -d "$WATCHER_DIR/.git" ] || return 0
  local sha dir
  sha="$(wgit 'rev-parse HEAD')"
  dir="$(release_dir "$sha")"
  mkdir -p "$RELEASES_DIR"; chown "$RUN_AS:$RUN_AS" "$RELEASES_DIR" 2>/dev/null
  # `mv`, not `as_user "mv …"`: $WATCHER_DIR's PARENT (/opt) is root-owned, so the watcher
  # user cannot rename an entry inside it — only this script's own root can (found in
  # review: the as_user form failed silently here on a real box, every run, forever, with
  # the pointer never created and no hold to say why). The directory's own contents stay
  # watcher:watcher; only the rename needs root.
  if [ ! -d "$dir" ]; then
    mv "$WATCHER_DIR" "$dir" || { say_once "adopt-fail $sha" "could not adopt $WATCHER_DIR as its own first release; leaving it as a plain checkout"; return 1; }
  fi
  ln -sfn "$dir" "$WATCHER_DIR"
  echo "${sha:0:12}" >> "$DSTATE/releases"
  say "adopted the existing checkout as its first release, ${sha:0:7}, at $dir"
}

# release_new <sha> — a fresh, FULLY INDEPENDENT clone of exactly that commit under
# $RELEASES_DIR. Never --shared: a release outlives the one it was made from (sweep_releases,
# below, may remove an older release while a newer one made with --shared from it is still
# kept — alternates would then point a live release at a deleted one's objects) and the
# pointer it is cloned via can itself move before this finishes making one release's own
# existence depend on another's. Reused as-is if a previous run already made it (a run that
# died between the clone and the pointer flip) rather than cloned twice. Prints the folder on
# success; a failed clone cleans up after itself so a half-made folder is never mistaken for
# a finished one on a later run.
release_new() {
  local sha="$1" dir real_origin; dir="$(release_dir "$sha")"
  if [ -d "$dir/.git" ]; then echo "$dir"; return 0; fi
  mkdir -p "$RELEASES_DIR"; chown "$RUN_AS:$RUN_AS" "$RELEASES_DIR" 2>/dev/null
  # Cloning locally from $WATCHER_DIR is only ever for a fast, no-network copy of the
  # objects — the new release's own `origin` must still be the TRUE upstream, read from
  # $WATCHER_DIR before the clone touches anything, and set explicitly after: a plain local
  # clone's own origin defaults to the SOURCE PATH, which is the pointer itself (or a release
  # before this one), never the real remote. Left at that default, this release's own next
  # `git fetch origin` would fetch from wherever the pointer happens to be pointing by
  # then — itself, most of the time — and never see a commit newer than its own.
  # The clone's own `origin/main` is copied from the SOURCE'S local `main` branch, not
  # from the source's own `origin/main` (freshly fetched or not) — every release folder
  # after the first is checked out detached and never advances its local `main`, so that
  # branch, and anything cloned from it, drifts one release behind forever. Fetching once
  # here, against the now-correct URL, is what makes this release's own `origin/main`
  # agree with `$sha` instead of with whatever the chain behind it was last fetched to
  # (card #112: the Ship desk read "checking <the previous commit>" for five minutes after
  # every deploy, because the fresh release's own `origin/main` record was stale from the
  # moment it was made, not because anything on the box was actually behind).
  real_origin="$(wgit 'remote get-url origin')"
  if as_user "git clone -q '$WATCHER_DIR' '$dir' && git -C '$dir' remote set-url origin '$real_origin' && git -C '$dir' fetch -q origin '$BRANCH' && git -C '$dir' checkout -q --detach $sha" >&2; then
    echo "$dir"
  else
    as_user "rm -rf '$dir'" >&2
    return 1
  fi
}

# sweep_releases — removes every folder under $RELEASES_DIR except the newest two (by
# deploy ORDER — $DSTATE/releases, appended to by swap() every time the pointer flips, one
# sha per line; two deploys inside the same second have identical mtimes, so order on disk
# is the only reliable "current one and the one before") and any a lane's own state still
# names (bin/spawn.sh writes $WATCHER_STATE/<lane>/release at its own process start — see
# its comment there — so a lane whose current tick is still running from an older release
# keeps that release safe from the sweep for as long as the tick runs). Best-effort: never
# fails the deploy that calls it, and a folder it cannot list or remove is just left for
# the next run.
sweep_releases() {
  [ -d "$RELEASES_DIR" ] || return 0
  local inuse="" f name d newest l
  # Only a lane that is actually running right now can protect a release this way — a
  # lane's own state directory outlives the lane itself (a disabled or removed unit never
  # cleans up after itself), and a stale release/ file in it would otherwise pin an old
  # release against the sweep forever.
  for l in $(active_lanes); do
    f="$STATE_ROOT/$l/release"
    [ -f "$f" ] || continue
    name="$(cat "$f" 2>/dev/null)"
    [ -n "$name" ] && inuse="$inuse
$name"
  done
  newest="$(tail -2 "$DSTATE/releases" 2>/dev/null)"
  for d in "$RELEASES_DIR"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    printf '%s\n' "$inuse" | grep -qxF "$name" && continue
    printf '%s\n' "$newest" | grep -qxF "$name" && continue
    as_user "rm -rf '$d'" && say "swept old release $name"
  done
}

# swap <target sha> <commit the box was on> <force> — clone, install, flip. Never pauses or
# waits for a lane: a running tick stays pinned to the folder it already has (bin/spawn.sh's
# own comment on WATCHER_HERE), so moving the pointer disturbs nothing in flight.
swap() {
  local tgt="$1" cur="$2" force="$3" dir lanes projects l f p new=()
  adopt_plain_checkout || return 1
  dir="$(release_dir "$tgt")"
  if [ ! -d "$dir/.git" ]; then
    dir="$(release_new "$tgt")" || return 1
  fi
  if [ "$force" = force ]; then
    as_user "git -C '$dir' reset -q --hard $tgt" || return 1
  fi
  # Never silently nest a release INSIDE $WATCHER_DIR: adopt_plain_checkout above is what
  # turns a real directory there into a symlink, and if it could not (a permission problem,
  # logged there already), $WATCHER_DIR is still a real directory here — `ln -sfn` onto
  # one does not fail, it creates the link AS AN ENTRY INSIDE it (found in review: that
  # ran clean, logged "now points at", and never actually moved the box, forever, with
  # nothing holding the commit to say why).
  if [ -e "$WATCHER_DIR" ] && [ ! -L "$WATCHER_DIR" ]; then
    say "$WATCHER_DIR is a real directory, not the pointer — refusing to flip onto it; fix by hand"
    return 1
  fi
  # The pointer flips BEFORE install.sh runs: install.sh's own unconditional restart of
  # watcher-floor.service (never drained or paused) resolves /opt/watcher at the moment IT
  # restarts the unit — flip first, and that restart already serves the new code; flip
  # after, and the floor page runs one release behind until the NEXT deploy (found in
  # review). install.sh is still told WATCHER_DIR="$dir" explicitly either way, so its own
  # self-references never depend on the pointer having moved.
  ln -sfn "$dir" "$WATCHER_DIR" || { say "could not flip $WATCHER_DIR onto $dir"; return 1; }
  echo "${tgt:0:12}" >> "$DSTATE/releases"
  lanes="$(active_lanes | tr '\n' ' ')"
  projects="$(for l in $lanes; do project_of "$l"; done | sort -u | tr '\n' ' ')"
  # shellcheck disable=SC2086
  WATCHER_DIR="$dir" INSTALL_NO_PULL=1 "$dir/deploy/install.sh" $projects >/dev/null \
    || say "install.sh reported a problem (see above); the pointer already points at ${tgt:0:7}"
  say "$WATCHER_DIR now points at ${tgt:0:7}; each lane picks it up at its own next idle poll"
  # A lane file this commit added, for a project already running: started right away, the
  # same as every other lane adopting the new code at its own pace — unlike before this
  # card, nothing here waits for a sibling to be on the new code first. The one window that
  # leaves: a sibling still mid-tick on an OLDER commit, with that commit's own (possibly
  # non-disjoint) LANE_AGENTS split, could in principle race this freshly-started lane for
  # one card, for as long as that sibling's current tick runs. Splitting an existing lane's
  # filter and adding its new sibling in the SAME commit is rare and deliberate; doing it as
  # two commits (split first, add the lane once that is live) avoids the window entirely.
  while read -r f; do
    [ -n "$f" ] && [ -f "$dir/$f" ] && is_lane_file "$f" || continue
    l="$(basename "$f" .env)"; p="$(project_of "$l")"
    [ "$l" != "$p" ] && echo " $lanes " | grep -q " $p " && new+=("$l")
  done <<< "$(wgit "diff --name-only --diff-filter=A $cur $tgt -- projects/")"
  for l in "${new[@]+"${new[@]}"}"; do
    rm -f "$STATE_ROOT/$l/PAUSE"
    systemctl enable --now "watcher@$l" && say "watcher@$l is new in ${tgt:0:7}; started"
  done
  sweep_releases
  return 0
}

# --- one run --------------------------------------------------------------------------------

main() {
  # install.sh chmods every script in bin/ and deploy/, so a file committed without its
  # executable bit reads as a local change — the mode is the installer's business, not
  # git's (September 28, 2026: bin/test-deploy.sh held four commits for six hours before
  # this `config core.fileMode false` existed).
  wgit "config core.fileMode false" || true
  wgit "fetch -q origin $BRANCH" || { say_once fetch "fetch failed; trying again next run"; return 0; }
  local cur tgt force="" total=4 anyway=0 needs_canary=0
  cur="$(wgit 'rev-parse HEAD')"; tgt="$(wgit "rev-parse origin/$BRANCH")"
  if [ "$cur" = "$tgt" ]; then say_once "live $cur" "live at ${cur:0:7}; nothing to deploy"; clear_step; return 0; fi

  # Card #106 removed the old "box's checkout must be clean and able to fast-forward" gate:
  # a release is always a FRESH clone (release_new, above), never the live pointer's own
  # checkout edited in place, so there is nothing left that can ever be dirty or diverged.

  # Card #105: read `overridden` (a real `gh` call) and `canary_needed` (local git/systemctl
  # only) each exactly once per run, into `anyway`/`needs_canary` — both gate more than one
  # step below, and re-reading either mid-run risks reading a different answer the second
  # time for no benefit. `total` is the step file's own total: 5 when the canary stage will
  # actually run for this target, 4 when it will be skipped, 1 for a deploy-anyway that
  # skips every gate — so the floor page's Ship desk never lists a step that is not coming.
  overridden "$tgt" && anyway=1
  if [ "$anyway" = 0 ]; then
    if canary_needed "$cur" "$tgt"; then
      needs_canary=1
    # Card #120: a lane opted into the canary channel but the checkout itself is missing
    # (install.sh never made it, or it was removed) used to read exactly like "no canary
    # lane configured" — silent, with nothing in the history or the Ship desk. Say so.
    elif ! has_canary && [ -n "$(canary_lanes)" ]; then
      say_once "canary-missing" "canary: watcher opted in but $CANARY_DIR is missing; skipping the canary stage until the next install makes it"
    fi
  fi
  if [ "$anyway" = 1 ]; then total=1; elif [ "$needs_canary" = 1 ]; then total=5; fi

  # 2. CI, and 3. the replay suite — both skipped for a commit Steve said to ship.
  if [ "$anyway" = 0 ]; then
    write_step 2 "$total" "Wait for CI to pass" "waiting for $CHECK on ${tgt:0:7}"
    case "$(check_state "$tgt")" in
      pending) say_once "ci $tgt" "waiting for $CHECK on ${tgt:0:7}"; return 0 ;;
      failure) clear_step; hold "$tgt" "its $CHECK check failed" "The required check \`$CHECK\` did not pass on \`$tgt\`."; return 0 ;;
    esac
    write_step 3 "$total" "Run the replay suite" "checking ${tgt:0:7}"
    case "$(evals_state "$cur" "$tgt")" in
      pending|retry) return 0 ;;
      fail) clear_step
            hold "$tgt" "the replay suite failed on it twice" \
              "\`bin/eval.sh\` failed twice on \`$tgt\`. The last run's result lines:
\`\`\`
$(grep -E 'FAIL|ok$|passed|failed' "$DSTATE/evals/$tgt.log" 2>/dev/null | tail -20)
\`\`\`"
            return 0 ;;
    esac
  else
    force=force
    say "deploy-anyway on ${tgt:0:7}: shipping it without the check or the suite"
  fi

  # 3b. The canary stage — skipped for a commit Steve said to ship, exactly like CI and the
  # suite above, and for a commit/project canary_needed finds nothing to do for. Unchanged
  # by card #106: it still pauses and restarts only its own lane(s), directly.
  if [ "$needs_canary" = 1 ]; then
    CANARY_STEP_DETAIL=""
    canary_stage "$tgt"
    case $? in
      1) write_step 4 "$total" "Soak on the canary lane(s)" "$CANARY_STEP_DETAIL"; return 0 ;;
      2) clear_step; hold "$tgt" "$CANARY_HOLD_WHY" "$CANARY_HOLD_DETAILS"; return 0 ;;
    esac
  fi

  # 4. Release it: clone, install, flip the pointer. No pause, no drain, no wait.
  write_step "$total" "$total" "Install and flip the pointer" "installing ${tgt:0:7}"
  if ! swap "$tgt" "$cur" "$force"; then
    say "could not release ${tgt:0:7}; trying again next run"
    return 0
  fi
  clear_step
  echo "$tgt" > "$DSTATE/deployed"
  close_held_except "$tgt"
  # A canary soak record for the commit that just promoted has done its job; the next
  # commit that needs one starts a fresh clock rather than reading this stale one. Any
  # earlier canary-died commit is moot too, now that a newer, good one is live — it would
  # never be retried anyway (nothing ever fetches an older commit again), but a stale file
  # naming a superseded sha is one less thing to wonder about in the state directory.
  [ "$(cut -d' ' -f1 "$DSTATE/canary-soak" 2>/dev/null)" = "$tgt" ] && rm -f "$DSTATE/canary-soak" "$DSTATE/canary-prev"
  rm -f "$DSTATE/canary-died"
  say "deployed ${tgt:0:7}; live for new ticks"
}

exec 9> "$DSTATE/lock"
flock -n 9 || { say "another run is in progress"; exit 0; }
# Card #129: the lock's own mtime is truncated at the START of every run (the `exec` above),
# so the floor page's Ship desk — which reads it as "when last checked" between runs — read
# a run that just took 27 minutes as 27 minutes stale the moment it finished. `last-run` is
# touched on exit instead, in a trap so a run that errors out still leaves one; `deploy_alive`
# (bin/floor.py) reads it for "checked ago" between runs and keeps the lock's own mtime for
# "started ago" while a run is active, where only the start time means anything. Best effort,
# like every other write under $DSTATE: a failure here must never fail a deploy.
trap 'touch "$DSTATE/last-run" 2>/dev/null || true' EXIT
main
