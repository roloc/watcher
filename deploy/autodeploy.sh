#!/usr/bin/env bash
# Deploy the watcher's `main` onto this box by itself, when every lane is idle. Run as root
# by watcher-deploy.timer every few minutes; one run is one step, and it is idempotent, so
# a run that stops half way is picked up by the next. Nobody runs deploy/rollout.sh by hand
# any more (Steve, September 28, 2026: "I don't want to run rollout ever").
#
# Why this is on the box and not a CI job: a CI job that deploys here needs a GitHub runner
# on the box that holds the bot's GitHub login and the Claude token, and ci.yml's rule is
# that PR code never runs there. So CI decides WHETHER a commit may ship (its required check
# is green) and this decides WHEN (every lane idle). It only ever reads GitHub.
#
# One run, in order:
#   1. Fetch origin/main. Nothing new, and no drain in progress: exit quietly.
#   2. The deploy checkout (/opt/watcher) must be clean and able to fast-forward.
#   3. The commit's required check (`verify`) must be green. Pending: wait for a later run.
#   4. The replay suite (bin/eval.sh) runs in a SCRATCH clone at that exact commit, before
#      anything goes live — the suite copies the prompts from the checkout it runs in, so
#      this tests the new prompts, not the ones lanes are already using. Skipped when the
#      commits touch nothing a model reads. A fail is retried once (a live suite has flakes,
#      roloc/watcher#44); a second fail holds the commit.
#   4b. THE CANARY STAGE (card #46), only when the commit touches what a model reads and at
#      least one active lane's project file sets LANE_CHANNEL=canary: move the canary
#      checkout (/opt/watcher-canary — a second, independent clone; see bin/spawn.sh's own
#      LANE_CHANNEL shim) onto the commit and restart ONLY its lane(s), before /opt/watcher
#      moves at all. A run later sees the soak through: DEPLOY_CANARY_SOAK_TICKS clean ticks
#      on those lanes promotes (falls through to step 5 below); DEPLOY_CANARY_QUIET_MIN
#      minutes with no canary work at all promotes anyway, so a quiet Watcher board cannot
#      hold every other project back forever; one tick that does not finish clean (killed, a
#      subtype of error_during_execution, or parked after 2 attempts) rolls the canary
#      checkout back to the commit before it and holds this one exactly like step 3/4 would.
#      A project with no canary lane, or a commit that skips the suite in step 4, skips this
#      stage entirely and promotes straight away — canary is additive, never a new gate on
#      an ordinary deploy.
#   5. Wait until no lane on the box is running a tick. Then pause every lane (a PAUSE file
#      that says "deploy <sha>"), wait one poll interval so a spawner that was mid-poll
#      cannot launch behind our back, and check again. If lanes stay busy for
#      DEPLOY_DRAIN_AFTER_MIN, pause anyway and let the running ticks finish on their own.
#   6. Fast-forward /opt/watcher to exactly that commit, re-run install.sh without its pull,
#      restart every running lane, start a lane this commit added, and remove only the
#      PAUSE files this script wrote — a PAUSE Steve made himself stays.
#
# A HELD commit (red check, suite failing twice, a checkout that cannot fast-forward, or a
# canary tick that did not finish clean) gets one card on the watcher's board in Stuck, in
# the talking-to-steve.md shape. Moving it to In progress lets the watcher fix the cause;
# adding the `deploy-anyway` label ships it (and skips both step 4 and the canary stage).
# Nothing here ever signals a tick, force-pushes, or posts to Discord except through that
# card's move (board.py posts the Stuck alert, as it does for every card).
set -uo pipefail

WATCHER_DIR="${WATCHER_DIR:-/opt/watcher}"
STATE_ROOT="${WATCHER_STATE_ROOT:-/home/watcher/.local/state/watcher}"
DSTATE="${DEPLOY_STATE:-/var/lib/watcher-deploy}"
SCRATCH_ROOT="${DEPLOY_SCRATCH:-/home/watcher/.cache/watcher-deploy}"
CGROUP_ROOT="${DEPLOY_CGROUP_ROOT:-/sys/fs/cgroup/system.slice}"
RUN_AS="${DEPLOY_USER:-watcher}"
BRANCH="${DEPLOY_BRANCH:-main}"
CHECK="${DEPLOY_CHECK:-verify}"
# The project whose repo is this one, and whose board gets a held commit's card.
SELF_PROJECT="${DEPLOY_PROJECT:-watcher}"
GRACE="${DEPLOY_GRACE_SEC:-75}"                # longer than one spawner poll (60 s)
DRAIN_AFTER="${DEPLOY_DRAIN_AFTER_MIN:-240}"   # lanes busy this long: pause and drain
EVALS="${DEPLOY_EVALS:-1}"                     # 0 skips the replay suite entirely
# What a model reads: a commit range touching none of these skips the suite.
EVAL_PATHS="${DEPLOY_EVAL_PATHS:-commands agents docs evals bin/eval.sh CLAUDE.md}"
# The canary checkout (card #46) — see bin/spawn.sh's own default for WATCHER_CANARY_DIR,
# which this must match. SOAK_TICKS and QUIET_MIN are read together: whichever the canary
# lane(s) reach first clears the way to promote.
CANARY_DIR="${DEPLOY_CANARY_DIR:-/opt/watcher-canary}"
CANARY_SOAK_TICKS="${DEPLOY_CANARY_SOAK_TICKS:-3}"
CANARY_QUIET_MIN="${DEPLOY_CANARY_QUIET_MIN:-240}"

say() { printf '%s autodeploy: %s\n' "$(date -u +%FT%TZ)" "$*"; }
as_user() { su - "$RUN_AS" -c "$1"; }
# q <word>...: the words shell-quoted, for a command line that as_user runs.
q() { local out="" w; for w in "$@"; do out+="$(printf '%q' "$w") "; done; printf '%s' "${out% }"; }
wgit() { as_user "git -C '$WATCHER_DIR' $1"; }
cgit() { as_user "git -C '$CANARY_DIR' $1"; }
mkdir -p "$DSTATE/evals" "$DSTATE/held"

# say_once <key> <line>: journal a waiting line once per state, not once per run.
say_once() {
  local key="$1"; shift
  [ "$(cat "$DSTATE/said" 2>/dev/null)" = "$key" ] && return 0
  echo "$key" > "$DSTATE/said"; say "$*"
}

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

busy_lanes() { local l; for l in $(active_lanes); do tick_running "$l" && echo "$l"; done; }

ours() { head -c 7 "$1" 2>/dev/null | grep -q '^deploy '; }

pause_all() {
  local sha="$1" l p
  : > "$DSTATE/paused"
  for l in $(active_lanes); do
    p="$STATE_ROOT/$l/PAUSE"
    if [ -f "$p" ] && ! ours "$p"; then
      say "watcher@$l was already paused by hand; leaving its PAUSE as it is"
      continue
    fi
    mkdir -p "$STATE_ROOT/$l"
    echo "deploy $sha" > "$p"; chown "$RUN_AS:$RUN_AS" "$STATE_ROOT/$l" "$p" 2>/dev/null
    echo "$l" >> "$DSTATE/paused"
  done
}

unpause_ours() {
  local l p
  while read -r l; do
    p="$STATE_ROOT/$l/PAUSE"
    ours "$p" && rm -f "$p"
  done < "$DSTATE/paused"
  rm -f "$DSTATE/paused"
}

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
  local cur="$1" sha="$2" rec="$DSTATE/evals/$2" tries scratch log
  if [ "$EVALS" != 1 ]; then echo pass; return; fi
  # shellcheck disable=SC2086
  if [ -z "$(wgit "diff --name-only $cur $sha -- $EVAL_PATHS")" ]; then echo pass; return; fi
  case "$(cat "$rec" 2>/dev/null)" in pass) echo pass; return ;; "fail 2") echo fail; return ;; esac
  tries=0; [ "$(cat "$rec" 2>/dev/null)" = "fail 1" ] && tries=1
  scratch="$SCRATCH_ROOT/${sha:0:12}"; log="$DSTATE/evals/$sha.log"
  as_user "rm -rf '$scratch' && mkdir -p '$SCRATCH_ROOT' && git clone -q --shared '$WATCHER_DIR' '$scratch' && git -C '$scratch' checkout -q --detach $sha" >&2 \
    || { say "could not make a scratch clone at ${sha:0:7}; trying again next run" >&2; echo pending; return; }
  say "running the replay suite on ${sha:0:7} (attempt $((tries+1)) of 2)" >&2
  if as_user "cd '$scratch' && bin/eval.sh" > "$log" 2>&1; then
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
# before it could write anything. `tick_running` (already used by busy_lanes) is what the
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

# canary_busy — the canary lane(s) currently running a tick, same predicate busy_lanes uses.
canary_busy() { local l; for l in $(canary_lanes); do tick_running "$l" && echo "$l"; done; }

# canary_pause/canary_unpause <sha> — pause_all/unpause_ours, scoped to only the canary
# lane(s): moving the canary checkout out from under a card in flight is exactly the
# restart-kills-the-cleanup bug active_lanes' own comment warns about, so a lane with a
# tick running is never restarted — it is paused (stops picking up new work) and waited on,
# the same shape the stable promote below already uses for every lane.
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
    as_user "chmod +x '$CANARY_DIR'/bin/*.sh '$CANARY_DIR'/deploy/*.sh" 2>/dev/null || true
    lanes="$(canary_lanes | tr '\n' ' ')"
    for l in $lanes; do systemctl restart "watcher@$l" && say "watcher@$l restarted on the canary checkout at ${tgt:0:7}"; done
    canary_unpause
    since="$(date +%s)"
    printf '%s %s\n' "$tgt" "$since" > "$rec"
    echo "$prev" > "$DSTATE/canary-prev"
    say "canary: ${tgt:0:7} is live for ${lanes% }; soaking"
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
  if [ $(( $(date +%s) - ${since:-0} )) -ge $(( CANARY_QUIET_MIN * 60 )) ]; then
    say "canary: ${tgt:0:7} has been quiet on the canary lane(s) for over $CANARY_QUIET_MIN min; promoting anyway"
    return 0
  fi
  return 1
}

# --- the swap -------------------------------------------------------------------------------

swap() {
  local sha="$1" cur="$2" force="$3" lanes projects l f p new=()
  if [ "$force" = force ]; then
    wgit "reset -q --hard $sha" || return 1
  elif ! wgit "merge -q --ff-only $sha"; then
    return 1
  fi
  lanes="$(active_lanes | tr '\n' ' ')"
  projects="$(for l in $lanes; do project_of "$l"; done | sort -u | tr '\n' ' ')"
  # shellcheck disable=SC2086
  INSTALL_NO_PULL=1 "$WATCHER_DIR/deploy/install.sh" $projects >/dev/null \
    || say "install.sh reported a problem (see above); restarting the lanes anyway"
  for l in $lanes; do
    systemctl restart "watcher@$l" && say "watcher@$l restarted on ${sha:0:7}"
  done
  # A lane file this commit added, for a project already running, is started last, once
  # every sibling is on the new code (rollout.sh step 5 explains why the order matters).
  while read -r f; do
    [ -n "$f" ] && [ -f "$WATCHER_DIR/$f" ] && is_lane_file "$f" || continue
    l="$(basename "$f" .env)"; p="$(project_of "$l")"
    [ "$l" != "$p" ] && echo " $lanes " | grep -q " $p " && new+=("$l")
  done <<< "$(wgit "diff --name-only --diff-filter=A $cur $sha -- projects/")"
  for l in "${new[@]+"${new[@]}"}"; do
    rm -f "$STATE_ROOT/$l/PAUSE"
    systemctl enable --now "watcher@$l" && say "watcher@$l is new in ${sha:0:7}; started"
  done
  return 0
}

# --- one run --------------------------------------------------------------------------------

main() {
  # install.sh chmods every script in bin/ and deploy/, so a file committed without its
  # executable bit reads as a local change and every commit would be held as "the box's
  # copy has changes of its own" (September 28, 2026: bin/test-deploy.sh held four
  # commits for six hours). The mode is the installer's business, not git's.
  wgit "config core.fileMode false" || true
  wgit "fetch -q origin $BRANCH" || { say_once fetch "fetch failed; trying again next run"; return 0; }
  local cur tgt drain="" from="" force=""
  cur="$(wgit 'rev-parse HEAD')"; tgt="$(wgit "rev-parse origin/$BRANCH")"
  # The drain record: "<sha> <the commit the box was on> [force]". Kept across runs, so a
  # run that dies after the swap still knows which lane files the deploy added.
  [ -f "$DSTATE/draining" ] && read -r drain from force < "$DSTATE/draining"

  if [ -z "$drain" ]; then
    if [ "$cur" = "$tgt" ]; then rm -f "$DSTATE/ready"; say_once "live $cur" "live at ${cur:0:7}; nothing to deploy"; return 0; fi

    # 2. The deploy checkout: clean, and behind (never beside) the target.
    if [ -n "$(wgit 'status --porcelain --untracked-files=no')" ] || ! wgit "merge-base --is-ancestor $cur $tgt"; then
      if overridden "$tgt"; then
        # Reset at the swap, with the lanes paused — not now, while they run.
        say "deploy-anyway on ${tgt:0:7}: it replaces what is in $WATCHER_DIR at the swap"
        force=force
      else
        hold "$tgt" "the box's copy of the watcher has changes of its own and cannot be updated cleanly" \
          "\`git status\` in \`$WATCHER_DIR\` is not clean, or its \`HEAD\` (\`${cur:0:7}\`) is not an ancestor of \`${tgt:0:7}\`. B resets it to \`${tgt:0:7}\`, discarding what is there."
        return 0
      fi
    fi

    # 3. CI, and 4. the replay suite — both skipped for a commit Steve said to ship.
    if ! overridden "$tgt"; then
      case "$(check_state "$tgt")" in
        pending) say_once "ci $tgt" "waiting for $CHECK on ${tgt:0:7}"; return 0 ;;
        failure) hold "$tgt" "its $CHECK check failed" "The required check \`$CHECK\` did not pass on \`$tgt\`."; return 0 ;;
      esac
      case "$(evals_state "$cur" "$tgt")" in
        pending|retry) return 0 ;;
        fail) hold "$tgt" "the replay suite failed on it twice" \
                "\`bin/eval.sh\` failed twice on \`$tgt\`. The last run's result lines:
\`\`\`
$(grep -E 'FAIL|ok$|passed|failed' "$DSTATE/evals/$tgt.log" 2>/dev/null | tail -20)
\`\`\`"
              return 0 ;;
      esac
    fi

    # 4b. The canary stage — skipped for a commit Steve said to ship, exactly like CI and
    # the suite above, and for a commit/project canary_needed finds nothing to do for.
    if ! overridden "$tgt" && canary_needed "$cur" "$tgt"; then
      canary_stage "$tgt"
      case $? in
        1) return 0 ;;
        2) hold "$tgt" "$CANARY_HOLD_WHY" "$CANARY_HOLD_DETAILS"; return 0 ;;
      esac
    fi

    # 5. Idle, or waited long enough: pause everything and give any mid-poll spawner a beat.
    # "Undeployed since": keyed on the commit the box is ON, so a stream of new commits on a
    # busy box does not restart the clock and put the forced drain off for ever.
    [ "$(cut -d' ' -f1 "$DSTATE/ready" 2>/dev/null)" = "$cur" ] || echo "$cur $(date +%s)" > "$DSTATE/ready"
    local since busy
    since="$(cut -d' ' -f2 "$DSTATE/ready")"
    busy="$(busy_lanes | tr '\n' ' ')"
    if [ -n "${busy// /}" ] && [ $(( $(date +%s) - since )) -lt $(( DRAIN_AFTER * 60 )) ]; then
      say_once "busy $tgt $busy" "${tgt:0:7} is ready; waiting for idle lanes (busy: $busy)"
      return 0
    fi
    [ -n "${busy// /}" ] && say "lanes busy for over $DRAIN_AFTER min; pausing them so ${tgt:0:7} can land when their ticks finish"
    pause_all "$tgt"
    echo "$tgt $cur $force" > "$DSTATE/draining"; drain="$tgt"; from="$cur"
    say "paused the lanes for ${tgt:0:7}; waiting ${GRACE}s for any poll in progress"
    sleep "$GRACE"
  fi

  # 6. Draining: every paused lane idle, then swap.
  local busy
  busy="$(busy_lanes | tr '\n' ' ')"
  if [ -n "${busy// /}" ]; then
    say_once "drain $drain $busy" "waiting for running ticks to finish before ${drain:0:7} (busy: $busy)"
    return 0
  fi
  if ! swap "$drain" "${from:-$(wgit 'rev-parse HEAD')}" "$force"; then
    # Never leave the lanes paused on a swap that cannot happen: let them run on the old
    # code, and the next run's checkout check holds the commit with a card.
    say "could not move $WATCHER_DIR to ${drain:0:7}; un-pausing the lanes on the old code"
    unpause_ours; rm -f "$DSTATE/draining"
    return 0
  fi
  unpause_ours
  rm -f "$DSTATE/draining" "$DSTATE/ready"
  echo "$drain" > "$DSTATE/deployed"
  close_held_except "$drain"
  # A canary soak record for the commit that just promoted has done its job; the next
  # commit that needs one starts a fresh clock rather than reading this stale one. Any
  # earlier canary-died commit is moot too, now that a newer, good one is live — it would
  # never be retried anyway (nothing ever fetches an older commit again), but a stale file
  # naming a superseded sha is one less thing to wonder about in the state directory.
  [ "$(cut -d' ' -f1 "$DSTATE/canary-soak" 2>/dev/null)" = "$drain" ] && rm -f "$DSTATE/canary-soak" "$DSTATE/canary-prev"
  rm -f "$DSTATE/canary-died"
  say "deployed ${drain:0:7}; every lane un-paused"
}

exec 9> "$DSTATE/lock"
flock -n 9 || { say "another run is in progress"; exit 0; }
main
