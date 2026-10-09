#!/usr/bin/env bash
# Roll a new watcher version onto the box BY HAND, the same shape deploy/autodeploy.sh uses
# on its own. Run as root.
#
#   deploy/rollout.sh <project> [branch]      # default branch: main
#
# NOT THE DEPLOY PATH. Since September 28, 2026 deploy/autodeploy.sh (watcher-deploy.timer)
# ships main by itself, and Steve never runs this (Steve, September 28, 2026: "I don't want
# to run rollout ever"). Kept as the manual fallback for a Claude session: when the
# deployer itself is broken, or to try a branch on the box before it merges.
#
# Card #106: a deploy never touches a running tick's own folder any more — each lane is
# pinned, for the whole life of its own process, to whichever release folder it started
# in, and only ever picks up a new one at its own next idle poll (see bin/spawn.sh's own
# comment on WATCHER_HERE). So this never pauses a lane, never waits for one, and never
# restarts one either — the same as an ordinary deploy. It takes the SAME lock
# deploy/autodeploy.sh takes ($DSTATE/lock), so the two can never clone and flip onto two
# different releases in the same breath; one simply waits a few minutes and tries again.
#
# What it does, in order:
#   1. The replay suite (bin/eval.sh), in a scratch clone at the exact target commit —
#      before anything on the box changes. `EVAL_SKIP=1 deploy/rollout.sh …` skips it (a
#      live suite has flakes, roloc/watcher#44).
#   2. Clones that commit into its own folder under $RELEASES_DIR (the same release a
#      matching autodeploy.sh run would make — reused as-is if one already exists), runs
#      that folder's own install.sh for the named project, and flips the pointer onto it.
#      The very first run ever against a box whose pointer is still a plain checkout (every
#      box before this card) adopts it as that checkout's own first release first — the
#      one-time migration deploy/autodeploy.sh's own adopt_plain_checkout does; kept here
#      too, duplicated on purpose (same reasoning as this file's own long-standing
#      tick_running, which duplicates deploy/autodeploy.sh's rather than share a dependency
#      between two scripts that must each still work completely on their own) — a change
#      to one of these functions almost always wants the same change in the other.
#   3. A lane this commit added to the project is started. Nothing already running is
#      touched; every lane already running follows the new pointer on its own, later.
#
# Trying a branch leaves the pointer on that branch's commit. There is no "turning the
# timer back on" any more — watcher-deploy.timer is untouched by this script either way,
# and the next scheduled run simply deploys whatever is newest on `main` when it gets
# there, same as always. Run this again for `main` when you are done trying a branch.
set -euo pipefail

PROJECT="${1:-}"; BRANCH="${2:-main}"
[ -n "$PROJECT" ] || { echo "usage: deploy/rollout.sh <project> [branch]" >&2; exit 64; }

WATCHER_DIR="${WATCHER_DIR:-/opt/watcher}"
RELEASES_DIR="${DEPLOY_RELEASES_DIR:-/opt/watcher-releases}"
DSTATE="${DEPLOY_STATE:-/var/lib/watcher-deploy}"
SCRATCH_ROOT="${DEPLOY_SCRATCH:-/home/watcher/.cache/watcher-deploy}"
RUN_AS="${DEPLOY_USER:-watcher}"

say() { printf '%s rollout: %s\n' "$(date -u +%FT%TZ)" "$*"; }
as_user() { su - "$RUN_AS" -c "$1"; }

mkdir -p "$DSTATE"
exec 9> "$DSTATE/lock"
flock -n 9 || { say "deploy/autodeploy.sh is mid-run; try again in a few minutes"; exit 1; }

as_user "git -C '$WATCHER_DIR' fetch -q origin '$BRANCH'"
SHA="$(as_user "git -C '$WATCHER_DIR' rev-parse 'origin/$BRANCH'")"

if [ "${EVAL_SKIP:-0}" = 1 ]; then
  say "EVAL_SKIP=1: not running the replay suite"
else
  scratch="$SCRATCH_ROOT/${SHA:0:12}"
  say "running the replay suite on ${SHA:0:7} (bin/eval.sh, in a scratch clone)"
  if ! as_user "rm -rf '$scratch' && mkdir -p '$SCRATCH_ROOT' && git clone -q --shared '$WATCHER_DIR' '$scratch' && git -C '$scratch' checkout -q --detach '$SHA' && cd '$scratch' && bin/eval.sh"; then
    as_user "rm -rf '$scratch'"
    say "replay suite FAILED on ${SHA:0:7} — nothing on the box changed. Fix the prompts, or"
    say "re-run with EVAL_SKIP=1 if this is the known flake, not a real regression."
    exit 1
  fi
  as_user "rm -rf '$scratch'"
  say "replay suite passed"
fi

# Adopt a still-plain $WATCHER_DIR as its own first release — see deploy/autodeploy.sh's
# own adopt_plain_checkout for the full reasoning; kept here too (above).
if [ ! -L "$WATCHER_DIR" ] && [ -d "$WATCHER_DIR/.git" ]; then
  cur="$(as_user "git -C '$WATCHER_DIR' rev-parse HEAD")"
  curdir="$RELEASES_DIR/${cur:0:12}"
  mkdir -p "$RELEASES_DIR"; chown "$RUN_AS:$RUN_AS" "$RELEASES_DIR" 2>/dev/null || true
  if [ ! -d "$curdir" ]; then
    as_user "mv '$WATCHER_DIR' '$curdir'"
  fi
  ln -sfn "$curdir" "$WATCHER_DIR"
  echo "${cur:0:12}" >> "$DSTATE/releases"
  say "adopted the existing checkout as its first release, ${cur:0:7}, at $curdir"
fi

dir="$RELEASES_DIR/${SHA:0:12}"
if [ ! -d "$dir/.git" ]; then
  mkdir -p "$RELEASES_DIR"; chown "$RUN_AS:$RUN_AS" "$RELEASES_DIR" 2>/dev/null || true
  # Never --shared, and the origin set explicitly after cloning: see deploy/autodeploy.sh's
  # own release_new for why (a release must outlive the one it was made from, and a plain
  # local clone's own default origin is the SOURCE PATH, not the true upstream).
  real_origin="$(as_user "git -C '$WATCHER_DIR' remote get-url origin")"
  as_user "git clone -q '$WATCHER_DIR' '$dir' && git -C '$dir' remote set-url origin '$real_origin' && git -C '$dir' checkout -q --detach '$SHA'"
fi
say "moving $WATCHER_DIR to ${SHA:0:7} ($BRANCH)"
INSTALL_NO_PULL=1 WATCHER_DIR="$dir" "$dir/deploy/install.sh" "$PROJECT"
ln -sfn "$dir" "$WATCHER_DIR"
echo "${SHA:0:12}" >> "$DSTATE/releases"
say "code at $(as_user "git -C '$WATCHER_DIR' log --oneline -1")"

# A lane this commit added to the project: started right away, same as an ordinary deploy.
# Every lane already running is left exactly as it is — it follows this release at its own
# next idle poll.
for f in "$dir/projects/$PROJECT"-*.env "$dir/projects/$PROJECT.env"; do
  [ -f "$f" ] || continue
  name="$(basename "$f" .env)"
  systemctl is-active --quiet "watcher@$name" 2>/dev/null && continue
  rm -f "/home/watcher/.local/state/watcher/$name/PAUSE"
  say "watcher@$name is new in ${SHA:0:7}; started"
  systemctl enable --now "watcher@$name"
done

if [ "$BRANCH" = main ]; then
  say "done. Every lane already running follows this at its own next idle poll."
else
  say "done, on $BRANCH. Run this again for main when you are finished trying it."
fi
