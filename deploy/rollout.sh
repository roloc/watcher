#!/usr/bin/env bash
# Roll a new watcher version onto the box WITHOUT killing a running tick. Run as root.
#
#   deploy/rollout.sh <project> [branch]      # default branch: main
#
# A project with LANES (projects/<project>-<lane>.env next to projects/<project>.env —
# riffmap has riffmap-design) is rolled as one: every lane's unit is drained and swapped
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

# 1. Code first, so even if we bail out below the next tick already has the new /watch.
say "pulling /opt/watcher ($BRANCH)"
su - watcher -c "git -C /opt/watcher fetch -q origin '$BRANCH' && git -C /opt/watcher checkout -q '$BRANCH' && git -C /opt/watcher pull -q --ff-only origin '$BRANCH'"
/opt/watcher/deploy/install.sh "$PROJECT" >/dev/null
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
say "done."
