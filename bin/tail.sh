#!/usr/bin/env bash
# Tail a lane's live activity: which card it holds, and what the tick itself is doing
# right now. `journalctl -u watcher@<lane> -f` (docs/steve-cheat-sheet.md) only shows
# spawn.sh's OWN lines — poll, launch, exit — because the tick's `--output-format json`
# file is written once, at exit (see the comment above `print_claude_cmd` in spawn.sh).
# The one thing that updates while a tick runs is its own session transcript, kept under
# `~/.claude/projects/<cwd>/<session>.jsonl` (same comment) because spawn.sh never passes
# `--no-session-persistence`. This script finds that file for a lane's CURRENT card and
# hands it to `tail.py`, which decodes it as it grows into one short line per tool call or
# assistant message — never the raw JSON, never a tool's full output.
#
#   bin/tail.sh <lane>              # follow: prints, then keeps watching
#   bin/tail.sh <lane> --no-follow  # print what is there now, then exit
#
# <lane> names a systemd instance (`watcher@<lane>`) with a `projects/<lane>.env` beside
# this script — riffmap, riffmap-design, riffmap-triage, watcher, ... Two things come from
# it: PROJECT_ROOT (to find the card's worktree) and LANE_WORKDIR (`root` runs with no
# worktree at all — see spawn.sh's own header).
#
# Read-only: never touches the board, the repo, or GitHub. Safe to run from any account
# that can read the tick user's `~/.claude/projects` and `~/.local/state/watcher`.
#
# RISK: the Claude CLI's on-disk transcript directory naming (every "/" in the cwd turned
# into "-") is an observed convention, not a documented contract. If a future CLI version
# changes it, this goes quietly silent ("waiting for a session...") rather than erroring —
# `bin/tail.py`'s `encode_dir` is the one place to fix.
#
# Every external path is overridable so this can be tested without a real box:
#   WATCHER_DIR          this repo's checkout            (default: parent of bin/)
#   WATCHER_STATE        the lane's state dir            (default: ~/.local/state/watcher/<name>)
#   CLAUDE_PROJECTS_DIR  where session transcripts live  (default: ~/.claude/projects)
set -uo pipefail

WATCHER_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHER_DIR="${WATCHER_DIR:-$(dirname "$WATCHER_HERE")}"

LANE="${1:?usage: tail.sh <lane> [--no-follow]}"
FOLLOW_ARGS=()
case "${2:-}" in
  --no-follow) FOLLOW_ARGS+=(--no-follow) ;;
  "") ;;
  *) echo "tail.sh: unknown argument '$2'" >&2; exit 2 ;;
esac

PROJECT_ENV="$WATCHER_DIR/projects/$LANE.env"
[ -f "$PROJECT_ENV" ] || { echo "tail.sh: no $PROJECT_ENV" >&2; exit 64; }
# shellcheck disable=SC1090
. "$PROJECT_ENV"

: "${PROJECT_ROOT:?$PROJECT_ENV sets no PROJECT_ROOT}"
# Same default spawn.sh uses (bin/spawn.sh: PROJECT_NAME="${PROJECT_NAME:-$(basename
# "$WATCHER_ROOT")}"), so an env file that leaves PROJECT_NAME unset keys the same state
# dir here as it would there — every env file on the box sets it explicitly today, so this
# is a fallback, not the common path.
PROJECT_NAME="${PROJECT_NAME:-$(basename "$PROJECT_ROOT")}"
LANE_WORKDIR="${LANE_WORKDIR:-card}"
case "$LANE_WORKDIR" in
  card|root) ;;
  *) echo "tail.sh: $PROJECT_ENV sets LANE_WORKDIR='$LANE_WORKDIR', must be 'card' or 'root'" >&2; exit 64 ;;
esac

WATCHER_STATE="${WATCHER_STATE:-$HOME/.local/state/watcher/$PROJECT_NAME}"

# ../<repo>-worktrees, the same derivation spawn.sh and worktree.sh use.
if [ "$LANE_WORKDIR" = root ]; then
  WORKTREE_BASE="$PROJECT_ROOT"
else
  WORKTREE_BASE="$(dirname "$PROJECT_ROOT")/$(basename "$PROJECT_ROOT")-worktrees"
fi

exec python3 "$WATCHER_HERE/tail.py" \
  --state "$WATCHER_STATE" \
  --workdir-mode "$LANE_WORKDIR" \
  --worktree-base "$WORKTREE_BASE" \
  "${FOLLOW_ARGS[@]}"
