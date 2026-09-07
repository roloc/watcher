#!/usr/bin/env bash
# Roll a new watcher version onto the box WITHOUT killing a running tick. Run as root.
#
#   deploy/rollout.sh <project> [branch]      # default branch: main
#
# What it does, in order:
#   1. Pulls /opt/watcher (the branch given, fast-forward only) and re-runs install.sh.
#      git replaces files by rename, so the spawn.sh that is already running keeps reading
#      its old copy; nothing running is disturbed. /watch, the reviewer and docs/ are
#      symlinked into ~watcher/.claude, so the NEXT tick already uses the new ones.
#   2. Pauses the spawner (touch PAUSE): it finishes whatever tick it is on and idles.
#   3. Waits until no tick is running in the unit's cgroup — the running tick, if any, is
#      left alone until it exits on its own (up to TICK_TIMEOUT, 150 min by default).
#   4. Restarts the unit, so the new spawn.sh is the one running, and removes PAUSE.
#
# If the wait times out, PAUSE is left in place, the unit is NOT restarted, and the exit
# code is 1 — run it again later; it is idempotent. Nothing here ever sends a signal to a
# tick.
set -euo pipefail

PROJECT="${1:-}"; BRANCH="${2:-main}"
[ -n "$PROJECT" ] || { echo "usage: deploy/rollout.sh <project> [branch]" >&2; exit 64; }
UNIT="watcher@$PROJECT"
STATE="/home/watcher/.local/state/watcher/$PROJECT"
PAUSE="$STATE/PAUSE"
WAIT_MAX="${ROLLOUT_WAIT_MAX_MIN:-170}"     # minutes; TICK_TIMEOUT (150) plus margin
POLL="${ROLLOUT_POLL_SEC:-30}"

say() { printf '%s rollout: %s\n' "$(date -u +%FT%TZ)" "$*"; }

# A tick is a `claude … --dangerously-skip-permissions` process inside this unit's cgroup.
# Checking the cgroup (not a global pgrep) means another project's tick never blocks us.
tick_running() {
  local procs="/sys/fs/cgroup/system.slice/$UNIT.service/cgroup.procs" pid
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
  [ -f "$STATE/current" ]
}

# 1. Code first, so even if we bail out below the next tick already has the new /watch.
say "pulling /opt/watcher ($BRANCH)"
su - watcher -c "git -C /opt/watcher fetch -q origin '$BRANCH' && git -C /opt/watcher checkout -q '$BRANCH' && git -C /opt/watcher pull -q --ff-only origin '$BRANCH'"
/opt/watcher/deploy/install.sh "$PROJECT" >/dev/null
say "code at $(su - watcher -c 'git -C /opt/watcher log --oneline -1')"

if ! systemctl is-active --quiet "$UNIT"; then
  say "$UNIT is not running; starting it on the new code"
  systemctl start "$UNIT"
  exit 0
fi

# 2. Drain.
mkdir -p "$STATE"; chown watcher:watcher "$STATE"
touch "$PAUSE"; chown watcher:watcher "$PAUSE"
say "paused $UNIT; waiting for the running tick (if any) to finish on its own"

# 3. Wait, never kill.
deadline=$(( $(date +%s) + WAIT_MAX * 60 ))
while tick_running; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    say "a tick is still running after $WAIT_MAX min. NOT restarting. PAUSE left in place;"
    say "run this again later (it picks up where it left off), or rm $PAUSE to resume as is."
    exit 1
  fi
  say "tick still running (card $(cat "$STATE/current" 2>/dev/null || echo '?')); checking again in ${POLL}s"
  sleep "$POLL"
done

# 4. Swap the spawner and resume.
say "no tick running; restarting $UNIT"
systemctl restart "$UNIT"
rm -f "$PAUSE"
say "done. $UNIT is on the new code and un-paused. Follow it with: journalctl -u $UNIT -f"
