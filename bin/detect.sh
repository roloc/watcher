#!/usr/bin/env bash
# One check of one project's one metric — no model, ever (card #36: "detection is
# deterministic, and a model is only invoked once a threshold is crossed"). Runs as the
# watcher user off a per-project systemd timer (deploy/watcher-detect@.timer), beside the
# lanes, and does nothing until a project opts in with a `projects/<name>.detect.env`.
#
#   bin/detect.sh <project>
#
# What `<project>.detect.env` sets, sourced after `<project>.env` so it has PROJECT_REPO,
# BOARD_OWNER, BOARD_NUMBER already:
#
#   DETECT_WHAT           plain words for what the metric is, used in the filed card
#   DETECT_METRIC_CMD     a command, run from $WATCHER_DIR (60s timeout), that prints one number
#   DETECT_BASELINE_WINDOW  readings kept for the rolling average (default 20)
#   DETECT_MIN_READINGS   readings needed before a breach can be tiered (default 3)
#   DETECT_SMALL_BAND     the rolling average at or above this: log a line, nothing else
#   DETECT_LARGE_BAND     the rolling average at or above this: file a Backlog card
#   DETECT_LOOK_URL       optional; a live page named in the filed card as the artifact
#
# One reading a run, appended to state; the *rolling average* of the last
# DETECT_BASELINE_WINDOW readings is what gets compared to the fixed bands, so one noisy
# reading cannot look like a sustained regression. A large breach files one card and, while
# that card stays open, files no second one for the same breach — closing it (fixed, or
# Steve saying not now) is what allows the next one.
set -uo pipefail

WATCHER_DIR="${WATCHER_DIR:-/opt/watcher}"
STATE_ROOT="${WATCHER_STATE_ROOT:-/home/watcher/.local/state/watcher}"
PROJECT="${1:?usage: detect.sh <project>}"

say() { printf '%s detect(%s): %s\n' "$(date -u +%FT%TZ)" "$PROJECT" "$*"; }

PROJECT_ENV="$WATCHER_DIR/projects/$PROJECT.env"
CONF="$WATCHER_DIR/projects/$PROJECT.detect.env"
[ -f "$CONF" ] && [ -f "$PROJECT_ENV" ] || { say "no $CONF (or no $PROJECT_ENV); nothing configured, exiting quietly"; exit 0; }

# shellcheck disable=SC1090
. "$PROJECT_ENV"
# shellcheck disable=SC1090
. "$CONF"

: "${DETECT_WHAT:?$CONF sets no DETECT_WHAT}"
: "${DETECT_METRIC_CMD:?$CONF sets no DETECT_METRIC_CMD}"
: "${DETECT_SMALL_BAND:?$CONF sets no DETECT_SMALL_BAND}"
: "${DETECT_LARGE_BAND:?$CONF sets no DETECT_LARGE_BAND}"
: "${PROJECT_REPO:?$PROJECT_ENV sets no PROJECT_REPO}"
: "${BOARD_OWNER:?$PROJECT_ENV sets no BOARD_OWNER}"
: "${BOARD_NUMBER:?$PROJECT_ENV sets no BOARD_NUMBER}"
export BOARD_OWNER BOARD_NUMBER   # board (bin/board.py) reads these from the environment
WINDOW="${DETECT_BASELINE_WINDOW:-20}"
MIN_READINGS="${DETECT_MIN_READINGS:-3}"

STATE="$STATE_ROOT/$PROJECT/detect"
mkdir -p "$STATE"
READINGS="$STATE/readings"
FILED="$STATE/filed"

value="$(cd "$WATCHER_DIR" && timeout 60 bash -c "$DETECT_METRIC_CMD" 2>"$STATE/error.log")"
if ! [[ "$value" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
  say "the metric command produced no number (${value:-empty}); skipping this run"
  exit 0
fi

echo "$(date -u +%FT%TZ) $value" >> "$READINGS"
tail -n "$((WINDOW * 4))" "$READINGS" > "$READINGS.tmp" && mv "$READINGS.tmp" "$READINGS"

window_lines="$(tail -n "$WINDOW" "$READINGS")"
count="$(printf '%s\n' "$window_lines" | grep -c .)"
avg="$(printf '%s\n' "$window_lines" | awk '{s+=$2; n++} END{if(n>0) printf "%.4f", s/n; else print "0"}')"

if [ "$count" -lt "$MIN_READINGS" ]; then
  say "warming up ($count/$MIN_READINGS readings); value=$value avg=$avg"
  exit 0
fi

tier=ok
awk -v a="$avg" -v b="$DETECT_LARGE_BAND" 'BEGIN{exit !(a>=b)}' && tier=large
[ "$tier" = ok ] && awk -v a="$avg" -v b="$DETECT_SMALL_BAND" 'BEGIN{exit !(a>=b)}' && tier=small

case "$tier" in
  ok)    say "ok: value=$value avg=$avg (of $count readings)" ;;
  small) say "SMALL BREACH: $DETECT_WHAT — value=$value avg=$avg over $count readings (band $DETECT_SMALL_BAND)" ;;
  large) say "LARGE BREACH: $DETECT_WHAT — value=$value avg=$avg over $count readings (band $DETECT_LARGE_BAND)" ;;
esac

[ "$tier" = large ] || exit 0

# Already filed for this breach, and it is still open: no second card. A failed lookup
# fails closed — skip this run rather than risk a duplicate on a transient `gh` error.
n="$(cat "$FILED" 2>/dev/null || true)"
if [ -n "$n" ]; then
  if ! state="$(gh issue view "$n" -R "$PROJECT_REPO" --json state -q .state 2>/dev/null)"; then
    say "could not check #$n's state; not filing a possible duplicate this run"
    exit 0
  fi
  if [ "$state" = "OPEN" ]; then
    say "already filed #$n for this breach and it is still open; not filing again"
    exit 0
  fi
  rm -f "$FILED"
fi

# Best-effort: the commit deploy-dev would have shipped, if this project tracks one.
deploy_ref=""
if [ -n "${PROJECT_ROOT:-}" ] && [ -d "$PROJECT_ROOT/.git" ]; then
  deploy_ref="$(git -C "$PROJECT_ROOT" rev-parse origin/main 2>/dev/null || true)"
fi

body="$(mktemp /tmp/watcher-detect-XXXXXX)"
{
  echo "## Summary"
  echo
  echo "**A regression detector caught a large breach.** $DETECT_WHAT — the current reading"
  echo "is $value, and the rolling average is $avg over the last $count readings (the large"
  echo "band is $DETECT_LARGE_BAND). Filed by \`bin/detect.sh\`, no model involved."
  echo
  echo "## Evidence"
  echo
  echo "- Metric: $DETECT_WHAT"
  echo "- Value / rolling average: $value / $avg"
  echo "- Bands: small $DETECT_SMALL_BAND, large $DETECT_LARGE_BAND"
  [ -n "$deploy_ref" ] && echo "- Newest commit on \`main\` at the time of this reading: \`${deploy_ref:0:7}\`"
  [ -n "${DETECT_LOOK_URL:-}" ] && echo "- Look: $DETECT_LOOK_URL"
  echo
  echo "## Verification"
  echo
  echo "- Whatever the fix is, re-check $DETECT_WHAT after it ships (the symptom rule in"
  echo "  \`commands/watch.md\`) and say in one sentence what it now shows."
} > "$body"

url="$(gh issue create -R "$PROJECT_REPO" \
        --title "🚨 $DETECT_WHAT: $value, avg $avg over $count readings" \
        --body-file "$body" 2>/dev/null | tail -1)"
rm -f "$body"
iss="${url##*/}"
if ! [[ "$iss" =~ ^[0-9]+$ ]]; then
  say "could not file a card (gh failed); trying again next run"
  exit 0
fi
echo "$iss" > "$FILED"

if gh project item-add "$BOARD_NUMBER" --owner "$BOARD_OWNER" --url "$url" >/dev/null 2>&1 \
   && board move "$iss" Backlog >/dev/null 2>&1; then
  say "filed $url in Backlog"
else
  say "filed $url but could not put it on the board (it still exists — check it by hand)"
fi
