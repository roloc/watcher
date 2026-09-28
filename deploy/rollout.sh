#!/usr/bin/env bash
# Roll a new watcher version onto the box WITHOUT killing a running tick. Run as root.
#
#   deploy/rollout.sh <project> [branch]      # default branch: main
#
# NOT THE DEPLOY PATH. Since September 28, 2026 deploy/autodeploy.sh (watcher-deploy.timer)
# ships main by itself when every lane is idle, and Steve never runs this. It is kept as the
# manual fallback for a Claude session: when the deployer is broken, or to try a branch on
# the box before it merges. It stops the deployer's timer first so the two never pause and
# restart the same lanes, and starts it again at the end only when it deployed main — after
# a branch, run it once more for main (the deployer would hold a box that is on a branch).
#
# A project with LANES (projects/<project>-<lane>.env next to projects/<project>.env —
# riffmap has riffmap-design and riffmap-triage) is rolled as one: every lane's unit is drained and swapped
# by this same command, each one the moment its own tick is done, so a long build never
# holds the design lane on old code or the other way around.
#
# What it does, in order:
#   1. Pulls /opt/watcher (the branch given, fast-forward only) and re-runs install.sh.
#      git replaces files by rename, so the spawn.sh that is already running keeps reading
#      its old copy; nothing running is disturbed. /watch, the reviewer and docs/ are
#      symlinked into ~watcher/.claude, so the NEXT tick already uses the new ones.
#   2. Pauses every unit (touch PAUSE): each finishes whatever tick it is on and idles.
#   3. Waits, per unit, until no tick is running in its cgroup — a running tick is left
#      alone until it exits on its own (up to TICK_TIMEOUT, 150 min by default) — and
#   4. restarts that unit, so the new spawn.sh is the one running, and removes its PAUSE.
#   5. Starts any unit of the project that was not running (a lane added by this version)
#      — last, once every running sibling is on the new code (see the note at step 2).
#
# If the wait times out, the units still waiting keep their PAUSE, are NOT restarted, and
# the exit code is 1 — run it again later; it is idempotent. Nothing here ever sends a
# signal to a tick.
set -euo pipefail

PROJECT="${1:-}"; BRANCH="${2:-main}"
[ -n "$PROJECT" ] || { echo "usage: deploy/rollout.sh <project> [branch]" >&2; exit 64; }
WAIT_MAX="${ROLLOUT_WAIT_MAX_MIN:-170}"     # minutes; TICK_TIMEOUT (150) plus margin
POLL="${ROLLOUT_POLL_SEC:-30}"

say() { printf '%s rollout: %s\n' "$(date -u +%FT%TZ)" "$*"; }

state_of() { echo "/home/watcher/.local/state/watcher/$1"; }

# A tick is a `claude … --dangerously-skip-permissions` process inside this unit's cgroup.
# Checking the cgroup (not a global pgrep) means another project's tick — or the other
# lane's — never blocks us.
tick_running() {
  local name="$1" procs="/sys/fs/cgroup/system.slice/watcher@$1.service/cgroup.procs" pid
  if [ -r "$procs" ]; then
    while read -r pid; do
      [ -r "/proc/$pid/cmdline" ] || continue
      if tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- '--dangerously-skip-permissions'; then
        return 0
      fi
    done < "$procs"
    return 1
  fi
  # cgroup v1 or a non-systemd box: fall back to the spawner's own bookkeeping.
  [ -f "$(state_of "$name")/current" ]
}

# 0. The deployer stands down while a person drives: its timer, any run in progress, and any
# drain it had started (its PAUSE files say "deploy <sha>"; a hand-made PAUSE is left
# alone). Whatever happens below, a rollout of main turns it back on when this exits.
systemctl stop watcher-deploy.timer watcher-deploy.service 2>/dev/null || true
for p in /home/watcher/.local/state/watcher/*/PAUSE; do
  [ -f "$p" ] && head -c 7 "$p" | grep -q '^deploy ' && rm -f "$p"
done
rm -f /var/lib/watcher-deploy/draining /var/lib/watcher-deploy/paused /var/lib/watcher-deploy/ready
if [ "$BRANCH" = main ]; then
  trap 'systemctl start watcher-deploy.timer 2>/dev/null || true' EXIT
fi

# 1. The replay suite FIRST, in a scratch clone at the exact commit about to go live: the
# suite copies the prompts from the checkout it runs in, and /opt/watcher is what every
# lane's ~/.claude links to, so running it there after the pull tested prompts that were
# already live (the gap roloc/watcher#42's own header named). A live suite has flakes
# (roloc/watcher#44), so `EVAL_SKIP=1 deploy/rollout.sh …` skips it.
su - watcher -c "git -C /opt/watcher fetch -q origin '$BRANCH'"
SHA="$(su - watcher -c "git -C /opt/watcher rev-parse 'origin/$BRANCH'")"
if [ "${EVAL_SKIP:-0}" = 1 ]; then
  say "EVAL_SKIP=1: not running the replay suite"
else
  scratch="/home/watcher/.cache/watcher-rollout-${SHA:0:12}"
  say "running the replay suite on ${SHA:0:7} (bin/eval.sh, in a scratch clone)"
  if ! su - watcher -c "rm -rf '$scratch' && git clone -q --shared /opt/watcher '$scratch' && git -C '$scratch' checkout -q --detach '$SHA' && cd '$scratch' && bin/eval.sh"; then
    su - watcher -c "rm -rf '$scratch'"
    say "replay suite FAILED on ${SHA:0:7} — nothing on the box changed. Fix the prompts, or"
    say "re-run with EVAL_SKIP=1 if this is the known flake, not a real regression."
    exit 1
  fi
  su - watcher -c "rm -rf '$scratch'"
  say "replay suite passed"
fi

# 2. Code: exactly that commit, fast-forward only. git replaces files by rename, so a
# running spawn.sh keeps reading its old copy.
say "moving /opt/watcher to ${SHA:0:7} ($BRANCH)"
su - watcher -c "git -C /opt/watcher checkout -q '$BRANCH' 2>/dev/null || git -C /opt/watcher checkout -q -b '$BRANCH' 'origin/$BRANCH'; git -C /opt/watcher merge -q --ff-only '$SHA'"
INSTALL_NO_PULL=1 /opt/watcher/deploy/install.sh "$PROJECT" >/dev/null
say "code at $(su - watcher -c 'git -C /opt/watcher log --oneline -1')"

# The project and its lanes, from the project files now on disk (after the pull, so a lane
# added by this very version is rolled too). A lane's unit that was never enabled is
# started by this same command: the file is the declaration. But only AFTER every running
# sibling has been swapped (step 5, below), never before. A spawner still on the OLD code
# may be holding a card the new lane would pick — the first rollout of lanes did exactly
# this: the pre-lanes build spawner had launched a tick on a designer card two seconds
# before the pause, the tick had not labeled it in-flight yet, and the freshly started
# design lane picked the same card into the same worktree. Once the sibling runs the new
# code the lane filters are disjoint and the window is gone.
NAMES=("$PROJECT")
for f in /opt/watcher/projects/"$PROJECT"-*.env; do
  [ -f "$f" ] || continue
  f="$(basename "$f" .env)"; NAMES+=("$f")
done

# 2. Drain: pause every unit that is running; remember the ones that are not.
PENDING=(); TO_START=()
for name in "${NAMES[@]}"; do
  unit="watcher@$name"
  if ! systemctl is-active --quiet "$unit"; then
    say "$unit is not running; it starts once the running units are on the new code"
    TO_START+=("$name")
    continue
  fi
  state="$(state_of "$name")"
  mkdir -p "$state"; chown watcher:watcher "$state"
  touch "$state/PAUSE"; chown watcher:watcher "$state/PAUSE"
  say "paused $unit; waiting for its running tick (if any) to finish on its own"
  PENDING+=("$name")
done

# 3. Wait, never kill — and 4. swap each unit the moment it is idle.
deadline=$(( $(date +%s) + WAIT_MAX * 60 ))
while [ "${#PENDING[@]}" -gt 0 ]; do
  STILL=()
  for name in "${PENDING[@]}"; do
    unit="watcher@$name"; state="$(state_of "$name")"
    if tick_running "$name"; then
      say "$unit: tick still running (card $(cat "$state/current" 2>/dev/null || echo '?'))"
      STILL+=("$name")
      continue
    fi
    say "$unit: no tick running; restarting it"
    systemctl restart "$unit"
    rm -f "$state/PAUSE"
    say "$unit is on the new code and un-paused. Follow it with: journalctl -u $unit -f"
  done
  PENDING=("${STILL[@]+"${STILL[@]}"}")
  [ "${#PENDING[@]}" -gt 0 ] || break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    say "a tick is still running after $WAIT_MAX min in: ${PENDING[*]}. NOT restarting those;"
    say "their PAUSE is left in place. Run this again later (it picks up where it left off),"
    say "or rm the PAUSE file to resume a unit as is."
    exit 1
  fi
  say "checking again in ${POLL}s"
  sleep "$POLL"
done

# 5. Now the units that were not running: every sibling is on the new code, so a lane
# started here can only ever pick cards its siblings will never touch.
for name in "${TO_START[@]+"${TO_START[@]}"}"; do
  unit="watcher@$name"
  # A stale PAUSE from an earlier, interrupted rollout would leave it idle while this
  # says it is live (review nit).
  rm -f "$(state_of "$name")/PAUSE"
  say "starting $unit on the new code"
  systemctl enable --now "$unit"
done
if [ "$BRANCH" = main ]; then
  say "done; the deployer is back on."
else
  say "done. The deployer is OFF while the box is on $BRANCH: run this again for main to turn it back on."
fi
