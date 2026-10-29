#!/usr/bin/env bash
# The desk's one road to the board and to a dry run of the spawner (card #205, review of
# roloc/watcher#218): a Claude Code permission rule is a command PREFIX, so an allow list that
# named `board`, `python3 …/board.py`, `set -a` or `. /opt/watcher/projects/` could be bent
# into a write or into sourcing an arbitrary file. This script takes a lane name and one of
# five READ subcommands, sources that lane's own projects/<lane>.env itself (so BOARD_OWNER
# and BOARD_NUMBER are set), and runs exactly one thing. It lives in the deployed release
# (/opt/watcher, the watcher user's), which the desk's own user cannot change.
#
#   bin/desk-read.sh <lane> show <n>          # a card: body, comments, column
#   bin/desk-read.sh <lane> list "<column>"   # one column of the board
#   bin/desk-read.sh <lane> ask <n>           # a Stuck card's question, one line
#   bin/desk-read.sh <lane> images <n>        # the pictures under its Look: section
#   bin/desk-read.sh <lane> dry-run           # the spawner's pick and exact command; launches nothing
#
# <lane> is a file name under projects/ (riffmap, riffmap-design, watcher, …), letters,
# digits and dashes only. WATCHER_DIR is where the release is (default /opt/watcher).
set -uo pipefail

WATCHER_DIR="${WATCHER_DIR:-/opt/watcher}"
usage() { echo "desk-read.sh: usage: desk-read.sh <lane> show <n> | list <column> | ask <n> | images <n> | dry-run" >&2; exit 2; }

[ $# -ge 2 ] || usage
lane="$1"; sub="$2"; shift 2
case "$lane" in
  *[!A-Za-z0-9-]*|"") echo "desk-read.sh: a lane is letters, digits and dashes, got: $lane" >&2; exit 2 ;;
esac
envfile="$WATCHER_DIR/projects/$lane.env"
[ -f "$envfile" ] || { echo "desk-read.sh: no such lane: $envfile (ls $WATCHER_DIR/projects/)" >&2; exit 2; }

set -a
# shellcheck disable=SC1090
. "$envfile"
set +a

case "$sub" in
  show|ask|images)
    [ $# -eq 1 ] || usage
    case "$1" in *[!0-9]*|"") echo "desk-read.sh: $sub takes a card number, got: $1" >&2; exit 2 ;; esac
    exec python3 "$WATCHER_DIR/bin/board.py" "$sub" "$1" ;;
  list)
    [ $# -eq 1 ] || usage
    case "$1" in *[!A-Za-z0-9\ -]*|"") echo "desk-read.sh: list takes a column name, got: $1" >&2; exit 2 ;; esac
    exec python3 "$WATCHER_DIR/bin/board.py" list "$1" ;;
  dry-run)
    [ $# -eq 0 ] || usage
    exec bash "$WATCHER_DIR/bin/spawn.sh" --dry-run ;;
  *) usage ;;
esac
