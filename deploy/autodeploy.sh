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
#   5. Wait until no lane on the box is running a tick. Then pause every lane (a PAUSE file
#      that says "deploy <sha>"), wait one poll interval so a spawner that was mid-poll
#      cannot launch behind our back, and check again. If lanes stay busy for
#      DEPLOY_DRAIN_AFTER_MIN, pause anyway and let the running ticks finish on their own.
#   6. Fast-forward /opt/watcher to exactly that commit, re-run install.sh without its pull,
#      restart every running lane, start a lane this commit added, and remove only the
#      PAUSE files this script wrote — a PAUSE Steve made himself stays.
#
# A HELD commit (red check, suite failing twice, a checkout that cannot fast-forward) gets
# one card on the watcher's board in Stuck, in the talking-to-steve.md shape. Moving it to
# In progress lets the watcher fix the cause; adding the `deploy-anyway` label ships it.
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

say() { printf '%s autodeploy: %s\n' "$(date -u +%FT%TZ)" "$*"; }
as_user() { su - "$RUN_AS" -c "$1"; }
# q <word>...: the words shell-quoted, for a command line that as_user runs.
q() { local out="" w; for w in "$@"; do out+="$(printf '%q' "$w") "; done; printf '%s' "${out% }"; }
wgit() { as_user "git -C '$WATCHER_DIR' $1"; }
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
  say "deployed ${drain:0:7}; every lane un-paused"
}

exec 9> "$DSTATE/lock"
flock -n 9 || { say "another run is in progress"; exit 0; }
main
