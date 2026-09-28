#!/usr/bin/env bash
#
# Tests for bin/spawn.sh. No network, no launchd, no real claude/gh/board.py —
# every external command is a stub under $TMP/bin, wired in through spawn.sh's own
# override env vars. Each case gets a fresh state/log dir unless the case is explicitly
# about state persisting across two polls (the second-attempt → Stuck case).
#
#   bash bin/test-spawn.sh                    # exit 0 = all cases hold; 1 = a case failed
set -uo pipefail

# Hermetic regardless of the caller's shell: the watcher@watcher lane (CLAUDE.md, "When the
# watcher works its own board") exports GH_REPO, BOARD_OWNER, BOARD_NUMBER, WATCHER_HOME and
# WATCHER_PROJECT for its own tick, and a tick that runs this suite from inside that lane
# inherits them — silently overriding the fake "owner/proj" and other fixtures `run()` sets
# below, so cases that grep for the fixture's own values fail on a correct tree. Found while
# verifying card #34 from inside that exact lane.
unset GH_REPO BOARD_OWNER BOARD_NUMBER WATCHER_HOME WATCHER_PROJECT LANE_AGENTS LANE_COMMAND LANE_WORKDIR READY_PULL WIP_MAX

HERE="$(cd "$(dirname "$0")" && pwd)"
SPAWN="$HERE/spawn.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"
mkdir -p "$BIN"

fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }

# A fake repo root, so WORKTREE_BASE = $TMP/repo-worktrees — same derivation spawn.sh
# itself uses (dirname/basename of WATCHER_ROOT + "-worktrees"), computed once here so
# the stubs and the assertions agree with spawn.sh without duplicating its logic.
FAKE_ROOT="$TMP/repo"
WORKTREE_BASE="$(dirname "$FAKE_ROOT")/$(basename "$FAKE_ROOT")-worktrees"
# parse_trust_key (round 5, N4) now `cd`s a parsed key before trusting it, so — same as
# the real WATCHER_ROOT, which is always a real, already-existing directory (spawn.sh
# derives its default from its own location on disk) — $FAKE_ROOT has to actually exist
# for a test that names it as the parsed key (test 23) to resolve.
mkdir -p "$FAKE_ROOT" "$WORKTREE_BASE"

# --- stubs --------------------------------------------------------------------------------
# board.py: `list` prints $BOARD_LIST_FILE (or fails if $BOARD_LIST_FAIL=1); `show`
# prints $BOARD_SHOW_FILE, but:
#   - fails EVERY time if $BOARD_SHOW_FAIL=1 (card #65 review fold-in: board_show_retry
#     must not read three straight failures as the benign "{}")
#   - fails a COUNTED number of times then recovers, if $BOARD_SHOW_FAIL_COUNT is set and
#     $BOARD_SHOW_COUNTER_FILE names a (per-test) counter file: fails and decrements while
#     the counter is above 0, succeeds forever after — exercises both board_show_retry's
#     own transient-recovery path (recovers within its 3 attempts) and reconcile_stale_
#     current's cross-poll recovery (card #65 review round 2, B2 and fold-in 3's nit)
#   - ALWAYS writes a stderr warning first if $BOARD_SHOW_WARN=1, even on an otherwise
#     successful call — board_show_retry must not let that corrupt the JSON it returns
#     (card #65 review round 2, fold-in 3: the same bug poll_board already fixed)
# `move` fails every time if $BOARD_MOVE_FAIL=1 (round 5, N1: park_stuck must not spam a
# comment once per poll while a card can't actually be moved to Stuck) — or fails a COUNTED
# number of times then recovers, if $BOARD_MOVE_FAIL_COUNT is set and
# $BOARD_MOVE_COUNTER_FILE names a (per-test) counter file (round 6, B1: park_stuck must
# keep retrying the move on every poll while it keeps failing, and stop retrying — but keep
# the one comment it already sent — once it lands). On a successful move it also rewrites
# $BOARD_SHOW_FILE's "status" to "Stuck - Needs help", if that file exists and is valid
# JSON, so a test can see the board itself reflect the move without maintaining a second,
# parallel notion of the card's status. `unclaim`/anything else just logs and exits 0.
cat > "$BIN/board.py" <<'STUB'
#!/usr/bin/env bash
echo "board.py $*" >> "$TEST_LOG"
case "$1" in
  list)
    if [ "${BOARD_LIST_FAIL:-0}" = 1 ]; then
      echo "board.py: no 'Status' option 'In progress'" >&2
      exit 1
    fi
    # The In review sweep's poll answers from its own file ($BOARD_REVIEW_FILE, empty when
    # a test set none) — never from the In progress rows.
    if [ "${2:-}" = "In review" ]; then
      if [ "${BOARD_REVIEW_FAIL:-0}" = 1 ]; then echo "board.py: transient API error" >&2; exit 1; fi
      cat "${BOARD_REVIEW_FILE:-/dev/null}"
      exit 0
    fi
    # The Ready column (READY_PULL). Its own file, never the In progress rows, and its
    # own failure switch: a board with no Ready column exits 1 here, which the spawner
    # must read as "nothing to pull", not as a poll failure.
    if [ "${2:-}" = "Ready" ]; then
      if [ "${BOARD_READY_FAIL:-0}" = 1 ]; then echo "board.py: no 'Status' option 'Ready'" >&2; exit 1; fi
      cat "${BOARD_READY_FILE:-/dev/null}"
      exit 0
    fi
    cat "$BOARD_LIST_FILE"
    ;;
  queue)
    cat "${BOARD_QUEUE_FILE:-/dev/null}"
    ;;
  show)
    if [ "${BOARD_SHOW_FAIL:-0}" = 1 ]; then
      echo "board.py: transient API error" >&2
      exit 1
    fi
    if [ -n "${BOARD_SHOW_FAIL_COUNT:-}" ] && [ -n "${BOARD_SHOW_COUNTER_FILE:-}" ]; then
      remaining="$BOARD_SHOW_FAIL_COUNT"
      [ -f "$BOARD_SHOW_COUNTER_FILE" ] && remaining="$(cat "$BOARD_SHOW_COUNTER_FILE")"
      if [ "$remaining" -gt 0 ]; then
        echo $((remaining - 1)) > "$BOARD_SHOW_COUNTER_FILE"
        echo "board.py: transient API error ($remaining left)" >&2
        exit 1
      fi
    fi
    if [ "${BOARD_SHOW_WARN:-0}" = 1 ]; then
      echo "board.py: warning: something noisy on stderr" >&2
    fi
    cat "$BOARD_SHOW_FILE"
    ;;
  move)
    if [ "${BOARD_MOVE_FAIL:-0}" = 1 ]; then
      echo "board.py: move failed (simulated)" >&2
      exit 1
    fi
    if [ -n "${BOARD_MOVE_FAIL_COUNT:-}" ] && [ -n "${BOARD_MOVE_COUNTER_FILE:-}" ]; then
      remaining="$BOARD_MOVE_FAIL_COUNT"
      [ -f "$BOARD_MOVE_COUNTER_FILE" ] && remaining="$(cat "$BOARD_MOVE_COUNTER_FILE")"
      if [ "$remaining" -gt 0 ]; then
        echo $((remaining - 1)) > "$BOARD_MOVE_COUNTER_FILE"
        echo "board.py: move failed ($remaining left)" >&2
        exit 1
      fi
    fi
    if [ -n "${BOARD_SHOW_FILE:-}" ] && [ -f "$BOARD_SHOW_FILE" ]; then
      python3 -c '
import json, sys
path = sys.argv[1]
try:
    d = json.load(open(path))
except Exception:
    sys.exit(0)
d["status"] = "Stuck - Needs help"
json.dump(d, open(path, "w"))
' "$BOARD_SHOW_FILE" 2>/dev/null || true
    fi
    ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$BIN/board.py"

# claude: the trust probe ("-p" "reply ok") reports trusted (no output, exit 0) UNLESS
# $CLAUDE_UNTRUSTED_ONCE_MARKER is set and the file it names does not exist yet — then it
# reports untrusted exactly once (creating the marker so a second probe of the same
# worktree would report trusted), exercising the write-trust path (card #65 review, B4).
# The real tick call ("-p" "/watch <n>" ...) sleeps $CLAUDE_TICK_SLEEP seconds first, when
# set (card #65 review, B2's watchdog-reaches-the-process case), then prints the
# configured JSON result to stdout (spawn.sh redirects that to the per-card log file
# itself) and exits $CLAUDE_RC.
#
# When $CLAUDE_BG_SLEEP_MARKER is also set, the sleep is NOT run in this process directly
# (card #65 review round 2, fold-in 6: a foreground sleep never exits before its parent, so
# it's always still a live, ordinary child when the group-kill fires — it never exercises a
# child reparented away from this stub before the stub itself exits). Instead this writes a
# uniquely-named helper script and backgrounds THAT, then falls straight through to print
# its result and exit — so by the time spawn.sh's watchdog or its belt-and-suspenders
# group-kill runs, the immediate parent (this stub) is already gone and the sleep is a
# grandchild reparented away from it, same as launchd would do. The marker must be a
# unique per-test token, never a bare "sleep <n>": `pgrep -f 'sleep 41'` can match an
# unrelated process on a shared box.
#
# When $CLAUDE_BG_TERM_COUNT_FILE is ALSO set (card #65 round 4, N2), the backgrounded
# helper traps SIGTERM instead of just dying on the first one: it appends one line per
# delivery and keeps running (in a short sleep loop) rather than exiting, so a SECOND,
# spurious TERM arriving close behind the first is counted as a second line rather than
# silently lost because the process was already gone. This is what distinguishes the tick
# watchdog's checked-sleep form (only the guaranteed final cleanup kill ever reaches an
# ordinary tick's own background child) from the old unchecked `sleep …; kill …` form
# (which additionally fires the instant `pkill -P` cancels its own sleep, on every tick,
# timeout or not).
cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
echo "claude $* [cwd=$PWD]" >> "$TEST_LOG"
echo "claude-env background-tasks-disabled=${CLAUDE_CODE_DISABLE_BACKGROUND_TASKS:-unset}" >> "$TEST_LOG"
if [ "$1" = "-p" ] && [ "$2" = "reply ok" ]; then
  # untrusted_message: the real CLI names the exact key it checked in the warning, when
  # $CLAUDE_UNTRUSTED_KEY is set — round 4 fold-in, ensure_trust's parse_trust_key.
  untrusted_message() {
    if [ -n "${CLAUDE_UNTRUSTED_KEY:-}" ]; then
      echo "Ignoring 3 permissions.allow entries for ${CLAUDE_UNTRUSTED_KEY}: this workspace has not been trusted." >&2
    else
      echo "Error: this workspace has not been trusted." >&2
    fi
  }
  # $CLAUDE_UNTRUSTED_ALWAYS never self-clears (unlike $CLAUDE_UNTRUSTED_ONCE_MARKER below):
  # every probe reports untrusted, round 4 fold-in — simulates a trust WRITE that reported
  # success but lost a race to another Claude Code session's own save, so the confirming
  # re-probe still comes back "not trusted" and ensure_trust must not cache.
  if [ -n "${CLAUDE_UNTRUSTED_ALWAYS:-}" ]; then
    untrusted_message
    exit 1
  fi
  if [ -n "${CLAUDE_UNTRUSTED_ONCE_MARKER:-}" ] && [ ! -f "$CLAUDE_UNTRUSTED_ONCE_MARKER" ]; then
    touch "$CLAUDE_UNTRUSTED_ONCE_MARKER"
    untrusted_message
    exit 1
  fi
  # Round 3, N3: a probe that fails for a reason that ISN'T "has not been trusted" — a
  # transient network error, a crash, anything — so tests can prove the "already trusted"
  # branch isn't reached (and nothing gets cached) on a probe that told us nothing either
  # way. Never self-clearing like the untrusted-once marker: every probe fails while this
  # is set, exercising "still inconclusive on a second poll" too.
  if [ -n "${CLAUDE_PROBE_FAIL_OTHER:-}" ]; then
    echo "Error: some transient failure unrelated to trust" >&2
    exit 1
  fi
  exit 0
fi
if [ -n "${CLAUDE_TICK_SLEEP:-}" ]; then
  if [ -n "${CLAUDE_BG_SLEEP_MARKER:-}" ]; then
    marker_script="$(dirname "$0")/${CLAUDE_BG_SLEEP_MARKER}"
    if [ -n "${CLAUDE_BG_TERM_COUNT_FILE:-}" ]; then
      cat > "$marker_script" <<TERMSCRIPT
#!/usr/bin/env bash
trap 'echo term >> "$CLAUDE_BG_TERM_COUNT_FILE"' TERM
for _ in \$(seq 1 50); do sleep 0.1; done
TERMSCRIPT
    else
      printf '#!/usr/bin/env bash\nsleep %s\n' "$CLAUDE_TICK_SLEEP" > "$marker_script"
    fi
    chmod +x "$marker_script"
    "$marker_script" &
  else
    sleep "$CLAUDE_TICK_SLEEP"
  fi
fi
printf '{"subtype":"%s","total_cost_usd":%s,"num_turns":%s,"permission_denials":%s}\n' \
  "${CLAUDE_SUBTYPE:-success}" "${CLAUDE_COST:-0.01}" "${CLAUDE_TURNS:-1}" "${CLAUDE_DENIALS:-0}"
exit "${CLAUDE_RC:-0}"
STUB
chmod +x "$BIN/claude"

# gh: records every call, does nothing else.
# gh: logs every call on ONE line, newlines in a --body collapsed to spaces — the spawner's
# comments to Steve are multi-line (docs/talking-to-steve.md), and every assertion below
# greps the whole call as a single line ("gh issue comment 40.*2 attempts").
# `gh api repos/<r>/issues/<n>/dependencies/blocked_by` (the spawner's dependency check)
# prints $GH_DEPS_DIR/<n>.json when that file exists, else an empty list — or fails like
# an outage when $GH_API_FAIL=1 (open_blockers must fail CLOSED: skip the card, never
# launch it on a lookup that told us nothing).
# `gh api repos/<r>/issues/<n> --jq .body // ""` (body_blockers, the net under it) prints
# $GH_BODY_DIR/<n>.md when that file exists, else nothing; `--jq .state` prints
# $GH_STATE_DIR/<n> when that file exists, else `open` — a referenced card is open unless a
# test says otherwise. $GH_BODY_FAIL=1 / $GH_STATE_FAIL=1 make those two fail like an
# outage, separately from $GH_API_FAIL, so each half of the fail-closed rule is provable on
# its own.
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
args="$*"
echo "gh ${args//$'\n'/ } [GH_REPO=${GH_REPO:-}]" >> "$TEST_LOG"
case "$args" in
  "api repos/"*"/dependencies/blocked_by")
    if [ "${GH_API_FAIL:-0}" = 1 ]; then echo "gh: HTTP 502 (simulated)" >&2; exit 1; fi
    n="$(printf '%s' "$args" | sed -E 's#.*/issues/([0-9]+)/dependencies/blocked_by.*#\1#')"
    if [ -n "${GH_DEPS_DIR:-}" ] && [ -f "$GH_DEPS_DIR/$n.json" ]; then cat "$GH_DEPS_DIR/$n.json"; else echo "[]"; fi
    ;;
  "api repos/"*"/issues/"*" --jq .body"*)
    if [ "${GH_BODY_FAIL:-0}" = 1 ]; then echo "gh: HTTP 502 (simulated)" >&2; exit 1; fi
    n="$(printf '%s' "$args" | sed -E 's#.*/issues/([0-9]+) --jq.*#\1#')"
    if [ -n "${GH_BODY_DIR:-}" ] && [ -f "$GH_BODY_DIR/$n.md" ]; then cat "$GH_BODY_DIR/$n.md"; fi
    ;;
  "api repos/"*"/issues/"*" --jq .state")
    if [ "${GH_STATE_FAIL:-0}" = 1 ]; then echo "gh: HTTP 502 (simulated)" >&2; exit 1; fi
    n="$(printf '%s' "$args" | sed -E 's#.*/issues/([0-9]+) --jq.*#\1#')"
    # A state file holding the word `404` answers like the real gh does for a number that
    # names no issue: the 404 path must be told apart from an outage, never share its answer.
    if [ -n "${GH_STATE_DIR:-}" ] && [ "$(cat "$GH_STATE_DIR/$n" 2>/dev/null)" = 404 ]; then
      echo "gh: Not Found (HTTP 404)" >&2; exit 1
    fi
    if [ -n "${GH_STATE_DIR:-}" ] && [ -f "$GH_STATE_DIR/$n" ]; then cat "$GH_STATE_DIR/$n"; else echo open; fi
    ;;
  "pr list "*)
    # The sweeper's open-PR lookup (case 3b): answers with $GH_PR_LIST_FILE when a test set
    # one; every other test gets nothing, which spawn.sh reads as "no PR".
    # No file → `[]`, what the real gh prints for no matches; $GH_PR_LIST_FAIL=1 → an outage
    # (non-zero, nothing on stdout), which the In review sweep must never read as "no PR".
    if [ "${GH_PR_LIST_FAIL:-0}" = 1 ]; then echo "gh: HTTP 502 (simulated)" >&2; exit 1; fi
    if [ -n "${GH_PR_LIST_FILE:-}" ] && [ -f "$GH_PR_LIST_FILE" ]; then cat "$GH_PR_LIST_FILE"; else echo "[]"; fi
    ;;
esac
exit 0
STUB
chmod +x "$BIN/gh"

# worktree.sh: `new card-<n>` actually creates the directory (so spawn.sh's own
# [ -d ... ] reuse check behaves like the real thing on a second poll), UNLESS
# $WORKTREE_NEW_FAIL=1, in which case it logs and fails without creating anything (card
# #65 review fold-in: a worktree that never gets created must still count as an attempt).
# `remove` just logs.
cat > "$BIN/worktree.sh" <<STUB
#!/usr/bin/env bash
echo "worktree.sh \$*" >> "\$TEST_LOG"
if [ "\$1" = "new" ]; then
  if [ "\${WORKTREE_NEW_FAIL:-0}" = 1 ]; then
    exit 1
  fi
  mkdir -p "$WORKTREE_BASE/\$2"
fi
exit 0
STUB
chmod +x "$BIN/worktree.sh"

# fake-tick.sh: NOT one of spawn.sh's overridable commands — a standalone helper the tests
# themselves background directly, with argv chosen so its command line contains
# "watch <n> --dangerously-skip-permissions", matching reconcile_stale_current's own
# `pgrep -f "watch $n --dangerously-skip-permissions"` check. Used to simulate a real tick process still
# being alive during reconciliation (card #65 review round 2, nit: the live-pid branch).
cat > "$BIN/fake-tick.sh" <<'STUB'
#!/usr/bin/env bash
sleep 30
STUB
chmod +x "$BIN/fake-tick.sh"

# --- harness --------------------------------------------------------------------------
STATE=""; LOGS=""; TESTLOG=""
fresh_state() {
  STATE="$TMP/state-$((n+1))-$RANDOM"; LOGS="$TMP/logs-$((n+1))-$RANDOM"; TESTLOG="$TMP/testlog-$((n+1))-$RANDOM"
  mkdir -p "$STATE" "$LOGS"; : > "$TESTLOG"
}
board_list() { # board_list <tsv-lines...> -> writes $STATE/board-list.tsv, sets BOARD_LIST_FILE
  BOARD_LIST_FILE="$STATE/board-list.tsv"
  printf '%s\n' "$@" > "$BOARD_LIST_FILE"
}
board_ready() { # board_ready <tsv-lines...> -> the Ready column (READY_PULL)
  BOARD_READY_FILE="$STATE/board-ready.tsv"
  printf '%s\n' "$@" > "$BOARD_READY_FILE"
}
board_show() { # board_show <json> -> writes $STATE/board-show.json
  BOARD_SHOW_FILE="$STATE/board-show.json"
  printf '%s' "$1" > "$BOARD_SHOW_FILE"
}
# run [--once|--dry-run] — invokes spawn.sh with every override wired to the stubs above.
# WATCHER_CLAUDE_JSON is ALWAYS a path under $TMP (card #65 review fold-in: this file must
# never be able to touch the real ~/.claude.json even if something in the environment set
# WATCHER_CLAUDE_JSON already) — a test that wants a specific fixture there sets
# CLAUDE_JSON_PATH, a name spawn.sh itself never looks at.
run() {
  TEST_LOG="$TESTLOG" \
  BOARD_LIST_FILE="${BOARD_LIST_FILE:-/dev/null}" BOARD_LIST_FAIL="${BOARD_LIST_FAIL:-0}" \
  BOARD_QUEUE_FILE="${BOARD_QUEUE_FILE:-/dev/null}" \
  BOARD_READY_FILE="${BOARD_READY_FILE:-/dev/null}" BOARD_READY_FAIL="${BOARD_READY_FAIL:-0}" \
  READY_PULL="${READY_PULL:-}" WIP_MAX="${WIP_MAX:-}" \
  BOARD_REVIEW_FILE="${BOARD_REVIEW_FILE:-/dev/null}" BOARD_REVIEW_FAIL="${BOARD_REVIEW_FAIL:-0}" \
  INREVIEW_NO_PR_GRACE_SEC="${INREVIEW_NO_PR_GRACE_SEC:-}" INREVIEW_STALE_SEC="${INREVIEW_STALE_SEC:-}" \
  BOARD_SHOW_FILE="${BOARD_SHOW_FILE:-/dev/null}" BOARD_SHOW_FAIL="${BOARD_SHOW_FAIL:-0}" \
  BOARD_MOVE_FAIL="${BOARD_MOVE_FAIL:-0}" \
  BOARD_MOVE_FAIL_COUNT="${BOARD_MOVE_FAIL_COUNT:-}" BOARD_MOVE_COUNTER_FILE="${BOARD_MOVE_COUNTER_FILE:-}" \
  BOARD_SHOW_FAIL_COUNT="${BOARD_SHOW_FAIL_COUNT:-}" BOARD_SHOW_COUNTER_FILE="${BOARD_SHOW_COUNTER_FILE:-}" \
  BOARD_SHOW_WARN="${BOARD_SHOW_WARN:-0}" \
  CLAUDE_SUBTYPE="${CLAUDE_SUBTYPE:-success}" CLAUDE_RC="${CLAUDE_RC:-0}" \
  CLAUDE_COST="${CLAUDE_COST:-0.01}" CLAUDE_TURNS="${CLAUDE_TURNS:-1}" CLAUDE_DENIALS="${CLAUDE_DENIALS:-0}" \
  CLAUDE_TICK_SLEEP="${CLAUDE_TICK_SLEEP:-}" CLAUDE_BG_SLEEP_MARKER="${CLAUDE_BG_SLEEP_MARKER:-}" \
  CLAUDE_BG_TERM_COUNT_FILE="${CLAUDE_BG_TERM_COUNT_FILE:-}" \
  CLAUDE_UNTRUSTED_ONCE_MARKER="${CLAUDE_UNTRUSTED_ONCE_MARKER:-}" \
  CLAUDE_UNTRUSTED_ALWAYS="${CLAUDE_UNTRUSTED_ALWAYS:-}" \
  CLAUDE_UNTRUSTED_KEY="${CLAUDE_UNTRUSTED_KEY:-}" \
  CLAUDE_PROBE_FAIL_OTHER="${CLAUDE_PROBE_FAIL_OTHER:-}" \
  WORKTREE_NEW_FAIL="${WORKTREE_NEW_FAIL:-0}" \
  GH_PR_LIST_FILE="${GH_PR_LIST_FILE:-}" GH_PR_LIST_FAIL="${GH_PR_LIST_FAIL:-0}" \
  WATCHER_ROOT="$FAKE_ROOT" PROJECT_REPO="owner/proj" \
  WATCHER_CLAUDE="$BIN/claude" WATCHER_GH="$BIN/gh" WATCHER_BOARD="$BIN/board.py" WATCHER_WORKTREE="$BIN/worktree.sh" \
  WATCHER_STATE="$STATE" WATCHER_LOGS="$LOGS" \
  WATCHER_CLAUDE_JSON="${CLAUDE_JSON_PATH:-$TMP/unused-claude.json}" \
  WATCHER_TICK_TIMEOUT_SEC="${WATCHER_TICK_TIMEOUT_SEC:-}" \
  WATCHER_TRUST_PROBE_TIMEOUT_SEC="${WATCHER_TRUST_PROBE_TIMEOUT_SEC:-5}" \
  WATCHER_SLEEP="${WATCHER_SLEEP:-0}" \
  WATCHER_MAX_POLLS="${WATCHER_MAX_POLLS:-}" \
  LANE_AGENTS="${LANE_AGENTS:-}" \
  LANE_COMMAND="${LANE_COMMAND:-/watch}" LANE_WORKDIR="${LANE_WORKDIR:-card}" \
  GH_DEPS_DIR="${GH_DEPS_DIR:-}" GH_API_FAIL="${GH_API_FAIL:-0}" \
  GH_BODY_DIR="${GH_BODY_DIR:-}" GH_BODY_FAIL="${GH_BODY_FAIL:-0}" \
  GH_STATE_DIR="${GH_STATE_DIR:-}" GH_STATE_FAIL="${GH_STATE_FAIL:-0}" \
  bash "$SPAWN" "$@"
  return $?
}
grepped() { grep -qF -- "$1" "$TESTLOG"; }         # a stub call happened
not_grepped() { ! grep -qF -- "$1" "$TESTLOG"; }    # a stub call did NOT happen

# ============================================================================
echo "1. --dry-run: picks the first claimable row, launches nothing"
fresh_state
board_list $'10\tP1\tbuilder\tin-flight\tCard ten' \
           $'11\tP1\tbuilder\t-\tCard eleven (attempts already at 2)' \
           $'12\tP2\tbuilder\t-\tCard twelve (claimable)'
mkdir -p "$STATE/attempts"; echo 2 > "$STATE/attempts/11"
OUT="$(run --dry-run)"; RC=$?
if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q '^pick: #12 ' && printf '%s' "$OUT" | grep -q '/watch 12'; then
  pass "dry-run picks #12 (10 is in-flight, 11 has attempts=2), exit 0"
else
  flunk "dry-run pick" "rc=$RC out=[$OUT]"
fi
if not_grepped "claude " && not_grepped "worktree.sh new" && not_grepped "gh issue"; then
  pass "dry-run polls the board (and reads what cards wait on) but launches/writes/claims nothing"
else
  flunk "dry-run should launch nothing" "$(cat "$TESTLOG")"
fi

# ============================================================================
echo "2. --once: stub claude exits 0/success, in-flight already gone → no unclaim; worktree kept when card stays open"
fresh_state
board_list $'20\tP1\tbuilder\t-\tCard twenty'
board_show '{"number":20,"state":"OPEN","status":"In progress","labels":[]}'
CLAUDE_SUBTYPE=success CLAUDE_RC=0 RC=0
run --once >/dev/null; RC=$?
if [ "$RC" = 0 ]; then pass "successful tick → spawn.sh --once exits 0"; else flunk "exit code" "got $RC"; fi
if grepped "worktree.sh new card-20" && grepped "/watch 20"; then pass "worktree created and tick launched for #20"; else flunk "launch" "$(cat "$TESTLOG")"; fi
if grepped "claude-env background-tasks-disabled=1"; then pass "the tick runs with background tasks disabled (a headless CI wait must block)"; else flunk "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS not passed to the tick" "$(cat "$TESTLOG")"; fi
if not_grepped "board.py unclaim 20"; then pass "no unclaim: in-flight was never on the card"; else flunk "unexpected unclaim"; fi
if not_grepped "worktree.sh remove card-20"; then pass "worktree kept: card is still OPEN"; else flunk "worktree should not be removed"; fi
if not_grepped "board.py notify"; then pass "no Discord post from the spawner: an ordinary tick is not an event (board.py posts the tick's own moves)"; else flunk "unexpected board.py notify" "$(cat "$TESTLOG")"; fi
[ "$(cat "$STATE/attempts/20" 2>/dev/null)" = 1 ] && pass "attempts/20 = 1 after one launch" || flunk "attempts/20" "$(cat "$STATE/attempts/20" 2>/dev/null)"

echo "2b. …same, but the card is CLOSED → worktree removed"
fresh_state
board_list $'21\tP1\tbuilder\t-\tCard twenty-one'
board_show '{"number":21,"state":"CLOSED","status":"Done","labels":[]}'
run --once >/dev/null
if grepped "board.py stage 21 -"; then pass "stage cleared: a closed card leaves no stale Stage on the grouped view"; else flunk "stage clear on close" "$(cat "$TESTLOG")"; fi
if grepped "worktree.sh remove card-21"; then pass "worktree removed: card is CLOSED"; else flunk "worktree remove" "$(cat "$TESTLOG")"; fi
if grepped "board.py notify 21 shipped"; then pass "Discord: a card that closed is reported as shipped (board.py downgrades it to 'closed' if no PR merged)"; else flunk "expected board.py notify 21 shipped" "$(cat "$TESTLOG")"; fi

# ============================================================================
echo "3. --once: stub claude exits 143 (killed), in-flight STILL on the card → unclaim + sweeper comment with the right numbers"
fresh_state
board_list $'30\tP1\tbuilder\t-\tCard thirty'
board_show '{"number":30,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
CLAUDE_SUBTYPE=error_during_execution CLAUDE_RC=143 CLAUDE_COST=0.42 CLAUDE_TURNS=17 CLAUDE_DENIALS=2
run --once >/dev/null; RC=$?
[ "$RC" != 0 ] && pass "a killed tick is a non-success exit (spawn.sh --once returns non-zero)" || flunk "exit code should be non-zero, got $RC"
if grepped "board.py unclaim 30"; then pass "unclaim called: in-flight leaked"; else flunk "unclaim not called" "$(cat "$TESTLOG")"; fi
comment_line="$(grep 'gh issue comment 30' "$TESTLOG" || true)"
if printf '%s' "$comment_line" | grep -q '\[GH_REPO=owner/proj\]'; then pass "repo-scoped gh calls carry GH_REPO=<project repo> (watcher#3: the spawner's cwd is not the project)"; else flunk "GH_REPO on the sweeper comment" "$comment_line"; fi
if printf '%s' "$comment_line" | grep -q '143/error_during_execution' \
   && printf '%s' "$comment_line" | grep -q '2 permission denials' \
   && printf '%s' "$comment_line" | grep -q '\$0.42'; then
  pass "sweeper comment names 143/error_during_execution, 2 denials, \$0.42"
else
  flunk "sweeper comment content" "$comment_line"
fi
released_line="$(grep 'board.py notify 30 released' "$TESTLOG" || true)"
if [ -n "$released_line" ] && printf '%s' "$released_line" | grep -q 'error_during_execution' \
   && ! printf '%s' "$released_line" | grep -q 'card-30-'; then
  pass "Discord: the sweeper posts 'released' with the exit reason and no log path (plain words)"
else
  flunk "board.py notify 30 released" "$released_line"
fi

# ============================================================================
echo "3b. --once: the tick ended cleanly with its PR OPEN, in-flight still on the card → released, and the comment names the PR, not a death"
# riffmap#305 (September 14, 2026): the tick opened its PR, then ended its turn "waiting on
# CI" for a background notification a headless run never gets. The card is released and
# retried exactly as for a died run — but Steve must read "PR up, merge pending", not
# "the run ended without finishing".
fresh_state
board_list $'31\tP1\tui\t-\tCard thirty-one'
board_show '{"number":31,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
GH_PR_LIST_FILE="$STATE/prs.json"
printf '%s' '[{"number":52,"url":"https://github.com/owner/proj/pull/52","body":"## Summary\n\n- the change\n\nCloses #31\n"}]' > "$GH_PR_LIST_FILE"
CLAUDE_SUBTYPE=success CLAUDE_RC=0 CLAUDE_COST=1.50 CLAUDE_TURNS=24 CLAUDE_DENIALS=1
run --once >/dev/null
if grep -q 'gh pr list --state open --search #31 in:body.*\[GH_REPO=owner/proj\]' "$TESTLOG"; then pass "the sweeper looks for the card's open PR by its Closes line, repo-scoped"; else flunk "pr list lookup" "$(cat "$TESTLOG")"; fi
if grepped "board.py unclaim 31"; then pass "released all the same: the card goes back to In progress for the next poll to merge"; else flunk "unclaim not called" "$(cat "$TESTLOG")"; fi
comment_line="$(grep 'gh issue comment 31' "$TESTLOG" || true)"
if printf '%s' "$comment_line" | grep -q 'PR #52 is up' \
   && printf '%s' "$comment_line" | grep -q 'pull/52' \
   && printf '%s' "$comment_line" | grep -q 'merges once the checks are green' \
   && ! printf '%s' "$comment_line" | grep -q 'without finishing' \
   && ! printf '%s' "$comment_line" | grep -q 'card-31-'; then
  pass "sweeper comment: 'PR #52 is up', links it, promises the merge, no 'without finishing', no log path"
else
  flunk "sweeper comment with an open PR" "$comment_line"
fi
# The dollar figure is quota on a subscription, never a bill (docs/how-it-works.md,
# "The dollar figure is quota, not a bill"). This card comment used to read
# "Agent cost: $1.50", and a reader reasonably concluded he had been charged. He had not:
# the box runs on CLAUDE_CODE_OAUTH_TOKEN and /etc/watcher/env may not carry an API key.
if printf '%s' "$comment_line" | grep -q 'quota used' \
   && printf '%s' "$comment_line" | grep -q '1[.]5' \
   && printf '%s' "$comment_line" | grep -q 'not billed' \
   && ! printf '%s' "$comment_line" | grep -qi 'agent cost'; then
  pass "the card comment reports quota and says it is not billed, never 'cost'"
else
  flunk "quota wording in the sweeper comment" "$comment_line"
fi
released_line="$(grep 'board.py notify 31 released' "$TESTLOG" || true)"
if printf '%s' "$released_line" | grep -q 'PR #52 is up' && ! printf '%s' "$released_line" | grep -q 'without finishing'; then
  pass "Discord: 'released' names the PR, not a death"
else
  flunk "board.py notify 31 released" "$released_line"
fi
[ "$(cat "$STATE/attempts/31" 2>/dev/null)" = 1 ] && pass "attempts/31 = 1: still one of the two tries (a second such exit parks it)" || flunk "attempts/31" "$(cat "$STATE/attempts/31" 2>/dev/null)"

echo "3c. …but a PR whose Closes line names ANOTHER card is not this card's → the plain died wording"
fresh_state
board_list $'32\tP1\tui\t-\tCard thirty-two'
board_show '{"number":32,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
GH_PR_LIST_FILE="$STATE/prs.json"
printf '%s' '[{"number":53,"url":"https://github.com/owner/proj/pull/53","body":"Closes #320\n\nSee also #32."}]' > "$GH_PR_LIST_FILE"
CLAUDE_SUBTYPE=success CLAUDE_RC=0 CLAUDE_COST=0.10 CLAUDE_TURNS=5 CLAUDE_DENIALS=0
run --once >/dev/null
comment_line="$(grep 'gh issue comment 32' "$TESTLOG" || true)"
if printf '%s' "$comment_line" | grep -q 'without finishing' && ! printf '%s' "$comment_line" | grep -q 'PR #53'; then
  pass "a mention is not a Closes line: #320's PR and a bare '#32' do not claim the card"
else
  flunk "sweeper comment should be the died wording" "$comment_line"
fi
unset GH_PR_LIST_FILE

echo "3d. …and the LINKED form the house rule mandates — Closes [#33](…/issues/33) — claims the card"
fresh_state
board_list $'33\tP1\tui\t-\tCard thirty-three'
board_show '{"number":33,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
GH_PR_LIST_FILE="$STATE/prs.json"
printf '%s' '[{"number":54,"url":"https://github.com/owner/proj/pull/54","body":"Resolves [#330](https://github.com/owner/proj/issues/330).\n\nCloses [#33](https://github.com/owner/proj/issues/33)."}]' > "$GH_PR_LIST_FILE"
CLAUDE_SUBTYPE=success CLAUDE_RC=0 CLAUDE_COST=0.10 CLAUDE_TURNS=5 CLAUDE_DENIALS=0
run --once >/dev/null
comment_line="$(grep 'gh issue comment 33' "$TESTLOG" || true)"
if printf '%s' "$comment_line" | grep -q 'PR #54 is up' && ! printf '%s' "$comment_line" | grep -q 'without finishing'; then
  pass "a Markdown-linked Closes line is found; the linked #330 beside it is not mistaken for #33"
else
  flunk "sweeper comment with a linked Closes line" "$comment_line"
fi
unset GH_PR_LIST_FILE

# ============================================================================
echo "4. Second attempt on the same card (attempts file reaches 2, card still In progress) → Stuck move + comment"
fresh_state
board_list $'40\tP1\tbuilder\t-\tCard forty'
board_show '{"number":40,"state":"OPEN","status":"In progress","labels":[]}'
CLAUDE_SUBTYPE=error_max_turns CLAUDE_RC=1 CLAUDE_COST=0.10 CLAUDE_TURNS=400 CLAUDE_DENIALS=0
run --once >/dev/null
[ "$(cat "$STATE/attempts/40")" = 1 ] || flunk "precondition: attempts/40 should be 1 after the first run"
if not_grepped "Stuck"; then pass "first attempt: no Stuck move yet"; else flunk "Stuck moved too early"; fi
if not_grepped "board.py notify"; then pass "first attempt: nothing posted to Discord (a first failure is not an event)"; else flunk "unexpected board.py notify on the first attempt" "$(cat "$TESTLOG")"; fi
run --once >/dev/null
if [ "$(cat "$STATE/attempts/40")" = 2 ]; then pass "attempts/40 = 2 after the second launch"; else flunk "attempts/40" "$(cat "$STATE/attempts/40")"; fi
if grepped 'board.py move 40 Stuck - Needs help'; then pass "second attempt with no progress → moved to Stuck"; else flunk "Stuck move" "$(cat "$TESTLOG")"; fi
why_line="$(grep 'board.py move 40 Stuck - Needs help --why ' "$TESTLOG" || true)"
if printf '%s' "$why_line" | grep -q '2 attempts' && ! printf '%s' "$why_line" | grep -q '\.json'; then
  pass "the Stuck move carries --why for the Discord alert: names the two attempts, no log path"
else
  flunk "Stuck move --why" "$why_line"
fi
if grepped 'gh issue comment 40' && grep -q 'gh issue comment 40.*2 attempts' "$TESTLOG"; then pass "Stuck comment names the two attempts"; else flunk "Stuck comment" "$(cat "$TESTLOG")"; fi
# The park comment is read by Steve on his phone (docs/talking-to-steve.md): the ask on the
# first line, one question, lettered options with the recommendation first, and how to
# answer. A regression to a prose paragraph must fail here, not on a card.
park_line="$(grep 'gh issue comment 40' "$TESTLOG" || true)"
if printf '%s' "$park_line" | grep -q 'gh issue comment 40 --body 🤖 \*\*Needs Steve:' \
   && printf '%s' "$park_line" | grep -q '\*\*Question: [^?]*?\*\*' \
   && printf '%s' "$park_line" | grep -q '\*\*A\. [^*]*\*\* (recommended' \
   && printf '%s' "$park_line" | grep -q '\*\*B\. ' \
   && printf '%s' "$park_line" | grep -q '\*\*C\. ' \
   && printf '%s' "$park_line" | grep -q 'Reply A, B or C on this card'; then
  pass "Stuck comment keeps the Steve shape: 'Needs Steve:' first, one Question, A/B/C with A recommended, how to answer"
else
  flunk "Stuck comment shape" "$park_line"
fi

# ============================================================================
echo "5. pidfile guard refuses a second instance"
fresh_state
sleep 5 </dev/null >/dev/null 2>&1 & LIVEPID=$!; disown "$LIVEPID" 2>/dev/null || true
echo "$LIVEPID" > "$STATE/spawn.pid"
board_list $'50\tP1\tbuilder\t-\tCard fifty'
board_show '{"number":50,"state":"OPEN","status":"In progress","labels":[]}'
run --once >/dev/null 2>"$TMP/guard.err"; RC=$?
kill "$LIVEPID" 2>/dev/null || true
if [ "$RC" = 1 ] && grep -qi 'already running' "$TMP/guard.err" && [ ! -s "$TESTLOG" ]; then
  pass "a live pid in spawn.pid → exit 1, nothing launched"
else
  flunk "pidfile guard" "rc=$RC err=$(cat "$TMP/guard.err") testlog=$(cat "$TESTLOG")"
fi

# ============================================================================
echo "6. board.py fails (non-zero exit) → no pick, the 5-minute sleep branch runs (WATCHER_SLEEP=0 makes it instant)"
fresh_state
BOARD_LIST_FAIL=1
start=$(date +%s)
run --once >/dev/null 2>"$TMP/boardfail.err"; RC=$?
elapsed=$(( $(date +%s) - start ))
BOARD_LIST_FAIL=0
if [ "$RC" != 0 ] && [ "$elapsed" -lt 5 ] && not_grepped "claude " && not_grepped "worktree.sh new"; then
  pass "board.py failure: no pick, no claude/worktree call, returned fast under WATCHER_SLEEP=0 (${elapsed}s)"
else
  flunk "board.py failure handling" "rc=$RC elapsed=${elapsed}s testlog=$(cat "$TESTLOG")"
fi

# ============================================================================
echo "7. PAUSE file present → no poll at all"
fresh_state
touch "$STATE/PAUSE"
board_list $'70\tP1\tbuilder\t-\tCard seventy'
run --once >/dev/null
if [ ! -s "$TESTLOG" ]; then pass "PAUSE present: board.py never invoked"; else flunk "PAUSE should stop the poll before board.py" "$(cat "$TESTLOG")"; fi

# ============================================================================
echo "8. B2: the watchdog TERM reaches the tick process itself, not a wrapper subshell"
fresh_state
board_list $'80\tP1\tbuilder\t-\tCard eighty'
board_show '{"number":80,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
CLAUDE_TICK_SLEEP=41
WATCHER_TICK_TIMEOUT_SEC=1
run --once >/dev/null 2>"$TMP/watchdog.err"; RC=$?
CLAUDE_TICK_SLEEP=""
WATCHER_TICK_TIMEOUT_SEC=""
# Give any orphaned "sleep 41" a beat to actually die from the TERM/pkill -P cleanup;
# WATCHER_SLEEP=0 only fast-forwards spawn.sh's OWN sleeps, not the stub's.
for _ in 1 2 3 4 5; do pgrep -f 'sleep 41' >/dev/null 2>&1 || break; sleep 1; done
if pgrep -f 'sleep 41' >/dev/null 2>&1; then
  flunk "watchdog TERM reaches the tick" "a 'sleep 41' process is still alive after the tick returned (rc=$RC)"
else
  pass "watchdog TERM reaches the tick process: no 'sleep 41' survivor"
fi
if [ "$RC" != 0 ]; then pass "a watchdog-killed tick is a non-success exit"; else flunk "exit code should be non-zero, got $RC"; fi
if grepped "board.py unclaim 80"; then pass "sweeper ran: in-flight was still on the card after the watchdog kill"; else flunk "sweeper did not run" "$(cat "$TESTLOG")"; fi

# ============================================================================
echo "8b. B2 (round-2 fold-in 6): a grandchild reparented away from the stub before it exits still dies with the tick's process group"
fresh_state
board_list $'81\tP1\tbuilder\t-\tCard eighty-one'
board_show '{"number":81,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
MARKER="watcher-test8b-sleep-$$-$RANDOM"
CLAUDE_TICK_SLEEP=41
CLAUDE_BG_SLEEP_MARKER="$MARKER"
run --once >/dev/null 2>"$TMP/watchdog8b.err"; RC8B=$?
CLAUDE_TICK_SLEEP=""
CLAUDE_BG_SLEEP_MARKER=""
# The stub backgrounds "$MARKER"'s sleep and exits almost immediately, so by the time
# spawn.sh's unconditional post-wait group-kill runs, this process is a grandchild whose
# immediate parent (the stub) is already gone — reparented, same as launchd would do.
# WATCHER_SLEEP=0 only fast-forwards spawn.sh's OWN sleeps, not the stub's, so give the
# real "$MARKER" process a beat to actually die from that cleanup.
for _ in 1 2 3 4 5; do pgrep -f "$MARKER" >/dev/null 2>&1 || break; sleep 1; done
if pgrep -f "$MARKER" >/dev/null 2>&1; then
  flunk "reparented grandchild survives the group cleanup" "a '$MARKER' process is still alive after the tick returned (rc=$RC8B)"
else
  pass "the tick's process-group cleanup also reaches a grandchild reparented away from the stub before it exited: no '$MARKER' survivor"
fi

# ============================================================================
echo "10. board.py show fails on every retry at exit time → fail LOUD, never fail-open"
fresh_state
board_list $'100\tP1\tbuilder\t-\tCard one hundred'
board_show '{"number":100,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
BOARD_SHOW_FAIL=1
run --once >/dev/null 2>"$TMP/showfail.err"
BOARD_SHOW_FAIL=0
if not_grepped "board.py unclaim 100"; then pass "board.py show unreachable: sweeper NOT run (never assumes benign state)"; else flunk "unclaim ran despite board.py show failing every time"; fi
if not_grepped "board.py move 100"; then pass "board.py show unreachable: no Stuck move either"; else flunk "Stuck move ran despite board.py show failing"; fi
if [ "$(cat "$STATE/current" 2>/dev/null)" = "100" ]; then pass "current left untouched (still names #100) — not silently cleared"; else flunk "current file" "$(cat "$STATE/current" 2>/dev/null)"; fi
# Round 4, B2: this loud-fail branch itself no longer bumps attempts/100 — it used to
# (round 3, N1), but that double-counted the same board-outage against reconcile_stale_
# current's OWN bump once it later clears $CURRENTFILE (see test 21), reaching the
# two-attempt Stuck threshold after a single real failure with nothing ever calling
# park_stuck. The exit still counts toward that budget, just via exactly one bump, made by
# reconcile on the poll that actually reconciles this stale claim, not here.
if [ ! -f "$STATE/attempts/100" ]; then pass "attempts/100 NOT bumped here (the single bump for this outage happens in reconcile_stale_current instead — see test 21)"; else flunk "attempts/100 should not exist yet" "$(cat "$STATE/attempts/100" 2>/dev/null)"; fi
if grep -qi 'NOT assuming the benign state' "$TMP/showfail.err"; then pass "logs loudly about the failure"; else flunk "expected a loud log line" "$(cat "$TMP/showfail.err")"; fi

# ============================================================================
echo "11. attempts/<n> resets on a success that leaves In progress (Done/In review/Stuck)"
fresh_state
board_list $'110\tP1\tbuilder\t-\tCard one eleven'
mkdir -p "$STATE/attempts"; echo 1 > "$STATE/attempts/110"
board_show '{"number":110,"state":"CLOSED","status":"Done","labels":[]}'
CLAUDE_SUBTYPE=success CLAUDE_RC=0
run --once >/dev/null
[ ! -f "$STATE/attempts/110" ] && pass "success + card left In progress (now Done) → attempts/110 reset (removed)" \
  || flunk "attempts/110 should have been removed" "$(cat "$STATE/attempts/110")"

# ============================================================================
echo "12. prepare_worktree failure counts as an attempt: parks after two failed worktree creates"
fresh_state
board_list $'120\tP1\tbuilder\t-\tCard one twenty'
WORKTREE_NEW_FAIL=1
run --once >/dev/null; RC1=$?
WORKTREE_NEW_FAIL=0
[ "$(cat "$STATE/attempts/120" 2>/dev/null)" = 1 ] && pass "first failed worktree create → attempts/120 = 1" \
  || flunk "attempts/120 after first failure" "$(cat "$STATE/attempts/120" 2>/dev/null)"
if not_grepped "Stuck"; then pass "first failure: no Stuck move yet"; else flunk "Stuck moved too early" "$(cat "$TESTLOG")"; fi
WORKTREE_NEW_FAIL=1
run --once >/dev/null; RC2=$?
WORKTREE_NEW_FAIL=0
[ "$(cat "$STATE/attempts/120" 2>/dev/null)" = 2 ] && pass "second failed worktree create → attempts/120 = 2" \
  || flunk "attempts/120 after second failure" "$(cat "$STATE/attempts/120" 2>/dev/null)"
if grepped 'board.py move 120 Stuck - Needs help'; then pass "two failed worktree creates → parked in Stuck without ever launching claude"; else flunk "Stuck move after two worktree failures" "$(cat "$TESTLOG")"; fi
if not_grepped "claude "; then pass "claude was never invoked (worktree never became ready)"; else flunk "claude should never have been invoked" "$(cat "$TESTLOG")"; fi

# ============================================================================
echo "13. pick_card logs WHY a row was skipped, and a row already at 2 attempts is parked (round 4, B2 safety net)"
fresh_state
board_list $'130\tP1\tbuilder\tin-flight\tCard in flight' \
           $'131\tP1\tbuilder\t-\tCard parked (2 attempts)'
mkdir -p "$STATE/attempts"; echo 2 > "$STATE/attempts/131"
run --once >/dev/null 2>"$TMP/pick.err"
if grep -q '#130 skipped: in-flight' "$TMP/pick.err"; then pass "logs '#130 skipped: in-flight'"; else flunk "expected in-flight skip log" "$(cat "$TMP/pick.err")"; fi
if grep -q '#131 skipped: 2 attempts, parking in Stuck' "$TMP/pick.err"; then pass "logs '#131 skipped: 2 attempts, parking in Stuck'"; else flunk "expected 2-attempts skip log" "$(cat "$TMP/pick.err")"; fi
if grepped 'board.py move 131 Stuck - Needs help'; then pass "pick_card itself parks a row already at 2 attempts, not just skips it"; else flunk "expected board.py move 131 to Stuck" "$(cat "$TESTLOG")"; fi
if grep -q 'gh issue comment 131' "$TESTLOG"; then pass "pick_card's park leaves a comment on #131"; else flunk "expected a Stuck comment on #131" "$(cat "$TESTLOG")"; fi

# ============================================================================
echo "14. Startup reconciliation: a stale 'current' from a crashed run is unclaimed and removed"
fresh_state
echo 140 > "$STATE/current"
board_show '{"number":140,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
board_list ""
run --once >/dev/null 2>"$TMP/reconcile.err"
if grepped "board.py unclaim 140"; then pass "startup reconciliation unclaimed the stale card (no tick process alive)"; else flunk "expected an unclaim of #140" "$(cat "$TESTLOG")"; fi
if grep -q 'restarted' "$TMP/reconcile.err" 2>/dev/null || grepped "gh issue comment 140"; then pass "a comment records the restart"; else flunk "expected a restart comment for #140" "$(cat "$TESTLOG")"; fi
[ ! -f "$STATE/current" ] && pass "stale current file removed" || flunk "current file should have been removed" "$(cat "$STATE/current")"

echo "15. Startup reconciliation: current names a card that is no longer in-flight → just cleaned up, no unclaim"
fresh_state
echo 141 > "$STATE/current"
board_show '{"number":141,"state":"OPEN","status":"In progress","labels":[]}'
board_list ""
run --once >/dev/null 2>"$TMP/reconcile2.err"
if not_grepped "board.py unclaim 141"; then pass "no in-flight label on #141: nothing to unclaim"; else flunk "should not have unclaimed #141" "$(cat "$TESTLOG")"; fi
[ ! -f "$STATE/current" ] && pass "stale current file removed even with nothing to unclaim" || flunk "current file should have been removed" ""

# ============================================================================
echo "16. Reconciliation: a live tick process for the stale 'current' is left alone (nit: the live-pid branch)"
fresh_state
echo 160 > "$STATE/current"
board_show '{"number":160,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
board_list ""
"$BIN/fake-tick.sh" watch 160 --dangerously-skip-permissions &
LIVETICKPID=$!
run --once >/dev/null 2>"$TMP/reconcile-live.err"
kill "$LIVETICKPID" 2>/dev/null || true
wait "$LIVETICKPID" 2>/dev/null || true
if not_grepped "board.py show 160"; then pass "live tick process for #160: board.py show never even called (pgrep short-circuits first)"; else flunk "board.py show should not have run while the tick is alive" "$(cat "$TESTLOG")"; fi
if not_grepped "board.py unclaim 160"; then pass "live tick process for #160: not unclaimed"; else flunk "should not have unclaimed #160 while its tick is alive" "$(cat "$TESTLOG")"; fi
[ "$(cat "$STATE/current" 2>/dev/null)" = "160" ] && pass "current left in place (tick still running)" || flunk "current file should still name #160" "$(cat "$STATE/current" 2>/dev/null)"

# ============================================================================
echo "17. board_show_retry: a stderr warning on an otherwise-successful show does not corrupt the parsed JSON (round-2 fold-in 3)"
fresh_state
board_list $'170\tP1\tbuilder\t-\tCard one seventy'
board_show '{"number":170,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
BOARD_SHOW_WARN=1
run --once >/dev/null 2>"$TMP/warn.err"
BOARD_SHOW_WARN=0
if grepped "board.py unclaim 170"; then pass "sweeper ran despite a stderr warning on the show call: JSON parsed cleanly"; else flunk "expected an unclaim of #170 despite the stderr noise" "$(cat "$TESTLOG")"; fi
[ ! -f "$STATE/current" ] && pass "current cleared" || flunk "current file should have been removed" "$(cat "$STATE/current" 2>/dev/null)"

# ============================================================================
echo "18. board_show_retry: recovers from transient failures within its own 3 attempts (never reaches the loud-fail branch)"
fresh_state
board_list $'180\tP1\tbuilder\t-\tCard one eighty'
board_show '{"number":180,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
BOARD_SHOW_FAIL_COUNT=2
BOARD_SHOW_COUNTER_FILE="$STATE/show-fail-counter-180"
run --once >/dev/null 2>"$TMP/retry.err"
BOARD_SHOW_FAIL_COUNT=""
BOARD_SHOW_COUNTER_FILE=""
show_calls="$(grep -c '^board\.py show 180$' "$TESTLOG" || true)"
if [ "$show_calls" = 3 ]; then pass "board.py show called exactly 3 times (2 transient failures + 1 recovery)"; else flunk "expected 3 calls to board.py show 180" "$show_calls calls: $(cat "$TESTLOG")"; fi
if grepped "board.py unclaim 180"; then pass "recovered within its own retries: sweeper ran normally"; else flunk "expected an unclaim of #180 after the transient failures cleared" "$(cat "$TESTLOG")"; fi
if not_grepped "NOT assuming the benign state"; then pass "the loud-fail branch never fired (recovered before exhausting retries)"; else flunk "loud-fail branch should not have fired" "$(cat "$TMP/retry.err")"; fi

# ============================================================================
echo "19. B2: reconcile_stale_current runs at the top of EVERY poll of the long-running daemon, not just once at process start"
fresh_state
echo 190 > "$STATE/current"
board_show '{"number":190,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
board_list ""    # nothing claimable — isolates reconciliation itself from pick_card/launch
BOARD_SHOW_FAIL_COUNT=3
BOARD_SHOW_COUNTER_FILE="$STATE/show-fail-counter-190"
WATCHER_MAX_POLLS=4
# No --once: the real run_daemon loop (bounded by WATCHER_MAX_POLLS, tests only — see the
# header comment). Poll 1's reconciliation exhausts board_show_retry's 3 attempts (all
# fail, per BOARD_SHOW_FAIL_COUNT=3) and leaves $CURRENTFILE in place, reproducing the
# original bug's starting condition (a board.py show outage at exit/reconcile time).
# Poll 2's reconciliation retries board.py show on its own — no restart, no human, no
# second board.py show outage — recovers, and unclaims. Reproduces, then disproves, "171
# polls, zero unclaims" (review round 2, B2).
run >/dev/null 2>"$TMP/daemon19.err"
BOARD_SHOW_FAIL_COUNT=""
BOARD_SHOW_COUNTER_FILE=""
WATCHER_MAX_POLLS=""
show_calls="$(grep -c '^board\.py show 190$' "$TESTLOG" || true)"
if [ "$show_calls" -ge 4 ]; then pass "board.py show #190 was retried across more than one poll ($show_calls calls total, not just the first poll's 3)"; else flunk "expected at least 4 calls to board.py show 190 (3 failed + at least 1 recovery on a later poll)" "$show_calls calls: $(cat "$TESTLOG")"; fi
if grepped "board.py unclaim 190"; then pass "a later poll's own reconciliation unclaimed #190 — no process restart involved"; else flunk "expected #190 to be unclaimed by a later poll" "$(cat "$TESTLOG")"; fi
[ ! -f "$STATE/current" ] && pass "current cleared once board.py show recovered" || flunk "current file should have been removed once show recovered" "$(cat "$STATE/current" 2>/dev/null)"

# ============================================================================
echo "21. Round 4, B2: a board.py show outage at bookkeeping time right after a SUCCESSFUL"
echo "    tick bumps attempts exactly once (in reconcile, not handle_exit's loud-fail branch"
echo "    too), and never leaves a card at attempts >= 2 with no Stuck move and no comment"
fresh_state
board_list ""    # isolate handle_exit/reconcile from pick_card, same idea as test 19
board_show '{"number":210,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
CLAUDE_SUBTYPE=success CLAUDE_RC=0
echo 210 > "$STATE/current"    # simulate launch_tick already having claimed and run #210
BOARD_SHOW_FAIL_COUNT=3
BOARD_SHOW_COUNTER_FILE="$STATE/show-fail-counter-210"
# Poll 1 here is really just reconcile_stale_current finding a stale $CURRENTFILE with no
# live tick process and calling board_show_retry itself — same code path handle_exit's own
# loud-fail branch would have left behind. board_show_retry's own 3 internal attempts
# exhaust the whole BOARD_SHOW_FAIL_COUNT=3 budget in this one call, so it fails outright.
run --once >/dev/null 2>"$TMP/case21-poll1.err"
if [ "$(cat "$STATE/current" 2>/dev/null)" = "210" ]; then pass "poll 1: board.py show exhausted its retries — current left in place, nothing bumped yet"; else flunk "current" "$(cat "$STATE/current" 2>/dev/null)"; fi
if [ ! -f "$STATE/attempts/210" ]; then pass "poll 1: attempts/210 does not exist yet (no bump on a failed reconcile)"; else flunk "attempts/210 should not exist yet" "$(cat "$STATE/attempts/210")"; fi
run --once >/dev/null 2>"$TMP/case21-poll2.err"
BOARD_SHOW_FAIL_COUNT=""
BOARD_SHOW_COUNTER_FILE=""
# Poll 2: the budget is spent, so this poll's board_show_retry recovers on its first
# attempt — reconcile unclaims and bumps attempts/210 for the first and only time.
if [ "$(cat "$STATE/attempts/210" 2>/dev/null)" = "1" ]; then pass "attempts/210 bumped to exactly 1 — one bump for one outage, not two"; else flunk "attempts/210" "$(cat "$STATE/attempts/210" 2>/dev/null || echo MISSING)"; fi
if grepped "board.py unclaim 210"; then pass "reconcile unclaimed #210 once board.py show recovered"; else flunk "expected an unclaim of #210" "$(cat "$TESTLOG")"; fi
if not_grepped "board.py move 210 Stuck - Needs help"; then pass "attempts=1 < 2: not parked — still re-pickable, not silently skipped"; else flunk "should not have been parked at attempts=1" "$(cat "$TESTLOG")"; fi
[ ! -f "$STATE/current" ] && pass "current cleared once reconciled" || flunk "current file should have been removed" "$(cat "$STATE/current" 2>/dev/null)"

echo "21b. …a second board-outage-after-success on the same card pushes attempts to 2 —"
echo "     reconcile itself parks it in Stuck with a comment, in the poll that crosses the"
echo "     threshold, rather than leaving it to be silently skipped forever"
board_list $'210\tP1\tbuilder\t-\tCard two ten, second cycle'
CLAUDE_SUBTYPE=success CLAUDE_RC=0
BOARD_SHOW_FAIL_COUNT=3
BOARD_SHOW_COUNTER_FILE="$STATE/show-fail-counter-210b"
# Poll 1: pick_card relaunches #210 (attempts=1 < 2, so it is still eligible), the tick
# succeeds, and handle_exit's own board_show_retry exhausts its 3 attempts again — the
# loud-fail branch fires (no bump), same as the very first outage above.
run --once >/dev/null 2>"$TMP/case21b-poll1.err"
if grep -q 'NOT assuming the benign state' "$TMP/case21b-poll1.err"; then pass "poll 1: handle_exit's loud-fail branch fired again for the second outage"; else flunk "expected the loud-fail log line" "$(cat "$TMP/case21b-poll1.err")"; fi
if [ "$(cat "$STATE/attempts/210" 2>/dev/null)" = "1" ]; then pass "poll 1: still attempts=1 (loud-fail branch never bumps)"; else flunk "attempts/210" "$(cat "$STATE/attempts/210" 2>/dev/null || echo MISSING)"; fi
board_list ""    # isolate poll 2's reconciliation from pick_card, same as poll 2 above
run --once >/dev/null 2>"$TMP/case21b-poll2.err"
BOARD_SHOW_FAIL_COUNT=""
BOARD_SHOW_COUNTER_FILE=""
if [ "$(cat "$STATE/attempts/210" 2>/dev/null)" = "2" ]; then pass "attempts/210 bumped to exactly 2 (still one bump per outage)"; else flunk "attempts/210" "$(cat "$STATE/attempts/210" 2>/dev/null || echo MISSING)"; fi
if grepped 'board.py move 210 Stuck - Needs help'; then pass "attempts=2: reconcile parks #210 in Stuck itself, no waiting on pick_card"; else flunk "expected a Stuck move for #210" "$(cat "$TESTLOG")"; fi
if grep -q 'gh issue comment 210.*attempts=2' "$TESTLOG"; then pass "the park comment names attempts=2, so the reason is visible on the card"; else flunk "expected a park comment naming attempts=2" "$(cat "$TESTLOG")"; fi

# ============================================================================
echo "22a. N2: the tick watchdog subshell gates its group-kill on sleep's own exit status —"
echo "     a structural check, since the race between this subshell and launch_tick's own"
echo "     subsequent kill of the SAME subshell process makes the runtime difference between"
echo "     the checked and bare forms unreliable to observe by signal-counting alone (see 22b)"
if grep -qF '( if sleep "$timeout_secs"; then kill -TERM -- "-$tick_pid" 2>/dev/null || true; fi ) &' "$SPAWN"; then
  pass "spawn.sh's tick watchdog still uses the checked \"if sleep …; then kill …; fi\" form"
else
  flunk "tick watchdog checked-sleep form" "expected the exact guarded construct in $SPAWN; found instead: $(grep -n 'tick_pid" 2>/dev/null' "$SPAWN" | head -3)"
fi

# ============================================================================
echo "22b. N2: an ordinary (non-timeout) tick's own background child gets at most the one"
echo "     guaranteed final cleanup kill, never an extra one from the watchdog itself"
fresh_state
board_list $'230\tP1\tbuilder\t-\tCard two thirty'
board_show '{"number":230,"state":"OPEN","status":"In progress","labels":["in-flight"]}'
# CLAUDE_TICK_SLEEP=0 (the stub's own "claude" foreground process returns almost
# immediately — an ORDINARY tick, nowhere near WATCHER_TICK_TIMEOUT_SEC) but, via
# CLAUDE_BG_SLEEP_MARKER (same mechanism as case 8b), it leaves a background child running
# behind it. CLAUDE_BG_TERM_COUNT_FILE makes that child trap SIGTERM instead of dying on
# the first delivery: it appends one line per TERM and keeps running, so a SECOND, spurious
# kill arriving close behind the first is observable as a second line, not silently
# coalesced into (or lost behind) the same delivery. This is exactly what distinguishes the
# checked-sleep form (only the one guaranteed final cleanup kill ever reaches an ordinary
# tick's own background child) from the old unchecked `sleep …; kill …` form, which fires
# an EXTRA kill the instant `pkill -P` cancels its own sleep, on every tick, timeout or not.
TERM_COUNT_FILE="$TMP/term-count-230"
: > "$TERM_COUNT_FILE"
MARKER="watcher-test22-term-counter-$$-$RANDOM"
CLAUDE_BG_SLEEP_MARKER="$MARKER" CLAUDE_BG_TERM_COUNT_FILE="$TERM_COUNT_FILE" CLAUDE_TICK_SLEEP=0
WATCHER_TICK_TIMEOUT_SEC=30    # generous: this tick must never be seen as timing out
run --once >/dev/null 2>"$TMP/watchdog22.err"; RC22=$?
CLAUDE_BG_SLEEP_MARKER=""; CLAUDE_BG_TERM_COUNT_FILE=""; CLAUDE_TICK_SLEEP=""; WATCHER_TICK_TIMEOUT_SEC=""
# Give the (still-running, up to 5s) background child a beat to receive and log every TERM
# it's going to get, then let it finish or clean it up.
for _ in 1 2 3 4 5 6; do pgrep -f "$MARKER" >/dev/null 2>&1 || break; sleep 1; done
pkill -f "$MARKER" 2>/dev/null || true
term_count="$(wc -l < "$TERM_COUNT_FILE" | tr -d ' ')"
if [ "$RC22" = 0 ]; then pass "an ordinary (non-timeout) tick is still a success exit"; else flunk "exit code should be 0, got $RC22"; fi
if [ "${term_count:-0}" -le 1 ]; then pass "the ordinary tick's background child received at most one TERM (the guaranteed final cleanup, not an extra early one from the watchdog's own unchecked sleep)"; else flunk "expected at most 1 TERM delivery, counted $term_count" "$(cat "$TERM_COUNT_FILE")"; fi

# ============================================================================
echo "26. Round 5/6, N1/B1: a card already at 2 attempts whose board.py move keeps failing"
echo "    gets exactly one Stuck comment across 3 polls (not one per poll), but the move"
echo "    itself is retried on EVERY poll — never permanently given up on after one failure"
fresh_state
board_list $'260\tP1\tbuilder\t-\tCard two sixty'
mkdir -p "$STATE/attempts"; echo 2 > "$STATE/attempts/260"
BOARD_MOVE_FAIL=1
run --once >/dev/null
run --once >/dev/null
run --once >/dev/null
BOARD_MOVE_FAIL=0
move_attempts="$(grep -c 'board.py move 260 Stuck - Needs help' "$TESTLOG" || true)"
if [ "$move_attempts" = 3 ]; then pass "board.py move was attempted exactly 3 times (once per poll), never given up on after the first failure"; else flunk "expected exactly 3 move attempts" "$(cat "$TESTLOG")"; fi
comment_count="$(grep -c 'gh issue comment 260' "$TESTLOG" || true)"
if [ "$comment_count" = 1 ]; then pass "exactly one Stuck comment across 3 polls despite board.py move failing every time"; else flunk "expected exactly 1 comment on #260, got $comment_count" "$(cat "$TESTLOG")"; fi
if [ -f "$STATE/parked/260" ]; then pass "parked/260 (comment-posted) marker written even though the underlying move failed"; else flunk "expected \$STATE/parked/260 to exist"; fi
if [ ! -f "$STATE/parked-ok/260" ]; then pass "parked-ok/260 (confirmed) NOT written — the move never actually succeeded"; else flunk "parked-ok/260 should not exist when board.py move always fails"; fi
if not_grepped "claude -p /watch 260"; then pass "#260 was never relaunched across all 3 polls — a failed move is not a retry signal"; else flunk "expected #260 to stay unpicked" "$(cat "$TESTLOG")"; fi

# ============================================================================
echo "26b. Round 6, B1: a card whose board.py move fails once, then succeeds, gets exactly"
echo "     one Stuck comment and exactly 2 move attempts across 4 polls — the SAME comment"
echo "     marker as test 26, but this time the move is not permanently broken, so"
echo "     parked-ok must land as soon as it actually does (poll 2), and the move must stop"
echo "     being retried once it has (reproduced before this round's fix: parked/<n> was"
echo "     written before the move ever ran, so a single transient failure meant the move"
echo "     was NEVER attempted again — this is the case that bug hit hardest, since the"
echo "     move here is only ever ONE transient failure away from working)"
fresh_state
board_list $'261\tP1\tbuilder\t-\tCard two sixty-one'
board_show '{"number":261,"state":"OPEN","status":"In progress","labels":[]}'
mkdir -p "$STATE/attempts"; echo 2 > "$STATE/attempts/261"
BOARD_MOVE_FAIL_COUNT=1
BOARD_MOVE_COUNTER_FILE="$STATE/move-counter-261"
run --once >/dev/null   # poll 1: move fails (1 left -> 0)
run --once >/dev/null   # poll 2: move succeeds
if [ -f "$STATE/parked-ok/261" ]; then pass "poll 2: parked-ok/261 present as soon as the move actually landed"; else flunk "expected \$STATE/parked-ok/261 to exist after poll 2"; fi
status_after_poll2="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status"))' "$BOARD_SHOW_FILE")"
if [ "$status_after_poll2" = "Stuck - Needs help" ]; then pass "poll 2: the board stub itself reflects the card moved to Stuck"; else flunk "expected board-show status Stuck - Needs help after poll 2" "$status_after_poll2"; fi
# A real board.py list "In progress" would no longer return this row once the card is
# genuinely in Stuck — unlike test 27, nobody moved it back, so reflect that for polls 3-4
# (leaving the old static row here would instead exercise pick_card's OWN "Steve moved it
# back" reconciliation, test 27's scenario, not this one).
board_list
run --once >/dev/null   # poll 3: parked-ok present, move must not be retried again
run --once >/dev/null   # poll 4: same
BOARD_MOVE_FAIL_COUNT=""; BOARD_MOVE_COUNTER_FILE=""
move_attempts="$(grep -c 'board.py move 261 Stuck - Needs help' "$TESTLOG" || true)"
if [ "$move_attempts" = 2 ]; then pass "board.py move attempted exactly twice across 4 polls (fails once, succeeds once, never retried again once parked-ok landed)"; else flunk "expected exactly 2 move attempts" "$(cat "$TESTLOG")"; fi
comment_count="$(grep -c 'gh issue comment 261' "$TESTLOG" || true)"
if [ "$comment_count" = 1 ]; then pass "exactly one Stuck comment across 4 polls"; else flunk "expected exactly 1 comment on #261, got $comment_count" "$(cat "$TESTLOG")"; fi
if not_grepped "claude -p /watch 261"; then pass "#261 was never relaunched across all 4 polls"; else flunk "expected #261 to stay unpicked" "$(cat "$TESTLOG")"; fi

# ============================================================================
echo "27. Round 5, N1: Steve moving a parked card back to In progress by hand clears"
echo "    attempts and lets it be picked on the very next poll, instead of being"
echo "    re-parked forever with attempts/<n> stuck at 2"
fresh_state
board_list $'270\tP1\tbuilder\t-\tCard two seventy'
mkdir -p "$STATE/attempts"; echo 2 > "$STATE/attempts/270"
run --once >/dev/null
if grepped 'board.py move 270 Stuck - Needs help'; then pass "poll 1: #270 parked as expected (attempts already at 2)"; else flunk "expected #270 to be parked on poll 1" "$(cat "$TESTLOG")"; fi
if [ -f "$STATE/parked/270" ]; then pass "poll 1: parked/270 (comment-posted) marker written"; else flunk "expected \$STATE/parked/270 to exist after poll 1"; fi
if [ -f "$STATE/parked-ok/270" ]; then pass "poll 1: parked-ok/270 (confirmed) marker written — the move actually succeeded"; else flunk "expected \$STATE/parked-ok/270 to exist after poll 1"; fi
# Steve moves #270 back to In progress himself; board.py list keeps listing it (that is
# the only signal pick_card ever gets) and board.py show reflects the same, for the tick
# this poll launches to read back at exit.
board_show '{"number":270,"state":"OPEN","status":"In progress","labels":[]}'
run --once >/dev/null 2>"$TMP/retry27.err"
if [ ! -f "$STATE/parked-ok/270" ]; then pass "poll 2: parked-ok/270 cleared — Steve's manual retry recognized"; else flunk "expected \$STATE/parked-ok/270 to be removed on poll 2"; fi
if [ ! -f "$STATE/parked/270" ]; then pass "poll 2: parked/270 (comment-posted) cleared too, so a future park isn't silently guarded by a stale marker"; else flunk "expected \$STATE/parked/270 to be removed on poll 2"; fi
if grep -q '#270: In progress with a confirmed parked-ok marker on file' "$TMP/retry27.err"; then pass "poll 2 logs the manual-retry reconciliation"; else flunk "expected a log line naming the manual retry" "$(cat "$TMP/retry27.err")"; fi
if grepped 'claude -p /watch 270'; then pass "poll 2: #270 was picked and launched, not skipped again"; else flunk "expected #270 to be launched on poll 2" "$(cat "$TESTLOG")"; fi
if [ "$(cat "$STATE/attempts/270" 2>/dev/null)" = 1 ]; then pass "poll 2: attempts/270 = 1 (reset to 0 by the retry, then bumped once by this poll's own launch), not left at the old 2"; else flunk "expected attempts/270 = 1 after poll 2" "$(cat "$STATE/attempts/270" 2>/dev/null)"; fi

# ============================================================================
echo "29. The Waiting stage: a card dragged there (not In progress) is moved to In progress, then launched"
# ============================================================================
fresh_state
board_list ""                                    # nothing In progress
BOARD_QUEUE_FILE="$STATE/board-queue.tsv"
printf '300\tP1\tbuilder\t-\tWaiting\tCard three hundred\n' > "$BOARD_QUEUE_FILE"
board_show '{"number":300,"state":"OPEN","labels":[],"status":"In progress"}'
CLAUDE_SUBTYPE=success CLAUDE_RC=0
run --once >/dev/null 2>"$TMP/q.err" || true
if grepped "board.py move 300 In progress"; then pass "queued card moved to In progress before launch"; else flunk "expected board.py move 300 In progress" "$(cat "$TESTLOG")"; fi
if grepped "/watch 300"; then pass "queued card launched"; else flunk "expected a launch for #300" "$(cat "$TESTLOG")"; fi
if grep -q "picked #300 (P1) Card three hundred" "$TMP/q.err"; then pass "six-column row parsed: title intact after the stage column"; else flunk "title parse" "$(cat "$TMP/q.err")"; fi
# a queue row that is in-flight is skipped like any other
fresh_state
board_list ""
printf '301\tP1\tbuilder\tin-flight\tWaiting\tCard three-oh-one\n' > "$BOARD_QUEUE_FILE"
run --once >/dev/null 2>&1 || true
if not_grepped "/watch 301"; then pass "an in-flight queue row is not launched"; else flunk "in-flight queue row launched"; fi
unset BOARD_QUEUE_FILE

# ============================================================================
echo "30. Lanes: LANE_AGENTS partitions the board by agent label, so two instances of one"
echo "    project (a design lane and a build lane) can run side by side without ever"
echo "    wanting the same card — the per-instance pidfile stays the only lock"
# ============================================================================
# The same board for every sub-case: a builder card first (P0), then an architect card,
# a designer card, and one with no agent label at all.
lane_board() {
  board_list $'400\tP0\tbuilder\t-\tBuild card' \
             $'401\tP1\tarchitect\t-\tDesign card' \
             $'402\tP1\tdesigner\t-\tMockup card' \
             $'403\tP2\t-\t-\tUnlabeled card'
  board_show '{"number":401,"state":"OPEN","status":"In progress","labels":[]}'
}
fresh_state; lane_board
OUT="$(LANE_AGENTS='architect,designer' run --dry-run 2>"$TMP/lane.err")"
if printf '%s' "$OUT" | grep -q '^pick: #401 ' && printf '%s' "$OUT" | grep -q '^lane: architect,designer'; then
  pass "design lane (architect,designer): skips the P0 builder card, picks #401, and --dry-run names the lane"
else
  flunk "design lane pick" "out=[$OUT]"
fi
if grep -q "#400 skipped: agent 'builder' is not in this lane" "$TMP/lane.err"; then pass "the skip is logged with the agent and the lane"; else flunk "skip log" "$(cat "$TMP/lane.err")"; fi

fresh_state; lane_board
OUT="$(LANE_AGENTS='!architect,designer' run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #400 '; then pass "build lane (!architect,designer): picks the builder card"; else flunk "build lane pick" "out=[$OUT]"; fi

fresh_state
board_list $'401\tP1\tarchitect\t-\tDesign card' \
           $'403\tP2\t-\t-\tUnlabeled card'
OUT="$(LANE_AGENTS='!architect,designer' run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #403 '; then pass "build lane: a card with NO agent label is 'the rest' and lands with the builder"; else flunk "unlabeled card in the build lane" "out=[$OUT]"; fi

fresh_state
board_list $'401\tP1\tarchitect\t-\tDesign card' \
           $'403\tP2\t-\t-\tUnlabeled card'
OUT="$(LANE_AGENTS='-' run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #403 '; then pass "a lane may name '-' to take exactly the unlabeled cards"; else flunk "'-' lane" "out=[$OUT]"; fi

fresh_state; lane_board
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #400 ' && printf '%s' "$OUT" | grep -q '^lane: every card'; then pass "no LANE_AGENTS: every card, first row wins — a single-lane project is unchanged"; else flunk "single-lane default" "out=[$OUT]"; fi

fresh_state
board_list $'401\tP1\tarchitect\t-\tDesign card'
OUT="$(LANE_AGENTS='!architect,designer' run --dry-run 2>"$TMP/lane2.err")"; RC=$?
if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q 'nothing claimable'; then pass "build lane with only design cards on the board: a quiet poll, not an error"; else flunk "quiet poll" "rc=$RC out=[$OUT]"; fi

# The other lane's card at attempts >= 2 is NOT this lane's to park: the safety net in
# pick_card runs after the lane filter, so a build lane never parks a design card (and
# never even consults its own attempts/<n> for it).
fresh_state; lane_board
mkdir -p "$STATE/attempts"; echo 2 > "$STATE/attempts/401"; echo 2 > "$STATE/attempts/400"
LANE_AGENTS='!architect,designer' run --once >/dev/null 2>&1 || true
if grepped 'board.py move 400 Stuck - Needs help' && not_grepped 'board.py move 401'; then
  pass "build lane parks ITS card at 2 attempts (#400) and leaves the design lane's (#401) alone"
else
  flunk "cross-lane park" "$(cat "$TESTLOG")"
fi
if not_grepped '/watch 401'; then pass "…and never launches the other lane's card either"; else flunk "cross-lane launch" "$(cat "$TESTLOG")"; fi

# Waiting-stage rows go through the same filter.
fresh_state
board_list ""
BOARD_QUEUE_FILE="$STATE/board-queue.tsv"
printf '410\tP1\tbuilder\t-\tWaiting\tQueued build\n411\tP1\tarchitect\t-\tWaiting\tQueued design\n' > "$BOARD_QUEUE_FILE"
board_show '{"number":411,"state":"OPEN","labels":[],"status":"In progress"}'
CLAUDE_SUBTYPE=success CLAUDE_RC=0
LANE_AGENTS='architect,designer' run --once >/dev/null 2>&1 || true
if grepped "board.py move 411 In progress" && grepped "/watch 411" && not_grepped "/watch 410"; then
  pass "a Waiting card is taken by the lane its agent label belongs to, not by the first lane to poll"
else
  flunk "lane filter on the Waiting queue" "$(cat "$TESTLOG")"
fi
unset BOARD_QUEUE_FILE

# ============================================================================
echo "31. A lane's command and workdir: LANE_COMMAND=/triage LANE_WORKDIR=root runs the tick"
echo "    in the project root with no worktree — the triage lane"
# ============================================================================
fresh_state
board_list $'400\tP0\tbuilder\t-\tBuild card' \
           $'403\tP2\t-\t-\tUnlabeled card'
OUT="$(LANE_AGENTS='-' LANE_COMMAND=/triage LANE_WORKDIR=root run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #403 ' && printf '%s' "$OUT" | grep -q '^command: /triage' \
   && printf '%s' "$OUT" | grep -q '/triage 403' && printf '%s' "$OUT" | grep -q "^workdir: $FAKE_ROOT (no worktree)"; then
  pass "dry-run: names the command, the pick, and the project root as the workdir"
else
  flunk "triage dry-run" "out=[$OUT]"
fi
fresh_state
board_list $'403\tP2\t-\t-\tUnlabeled card'
board_show '{"number":403,"state":"OPEN","status":"In progress","labels":["agent: ui"]}'
LANE_AGENTS='-' LANE_COMMAND=/triage LANE_WORKDIR=root run --once >/dev/null 2>&1 || true
if grepped "/triage 403" && grepped "[cwd=$FAKE_ROOT]" && not_grepped "worktree.sh new"; then
  pass "--once: the triage command runs in the project root and no worktree is created"
else
  flunk "triage launch" "$(cat "$TESTLOG")"
fi
fresh_state
board_list $'404\tP2\t-\t-\tUnlabeled, closed by the time the tick ends'
board_show '{"number":404,"state":"CLOSED","status":"Done","labels":[]}'
LANE_AGENTS='-' LANE_COMMAND=/triage LANE_WORKDIR=root run --once >/dev/null 2>"$TMP/root.err" || true
if not_grepped "worktree.sh remove" && grep -q "no worktree to remove for #404" "$TMP/root.err"; then
  pass "a closed card in a root-workdir lane removes no worktree"
else
  flunk "root workdir remove" "$(cat "$TESTLOG") $(cat "$TMP/root.err")"
fi
fresh_state
board_list $'403\tP2\t-\t-\tUnlabeled card'
LANE_WORKDIR=elsewhere run --dry-run >/dev/null 2>"$TMP/bad.err"; RC=$?
if [ "$RC" = 64 ] && grep -q "LANE_WORKDIR must be" "$TMP/bad.err"; then pass "a bad LANE_WORKDIR is refused at startup (exit 64)"; else flunk "bad LANE_WORKDIR" "rc=$RC $(cat "$TMP/bad.err")"; fi
fresh_state
board_list $'403\tP2\t-\t-\tUnlabeled card'
LANE_COMMAND=triage run --dry-run >/dev/null 2>"$TMP/bad2.err"; RC=$?
if [ "$RC" = 64 ] && grep -q "LANE_COMMAND must be a slash command" "$TMP/bad2.err"; then pass "a LANE_COMMAND without the slash is refused at startup (exit 64)"; else flunk "bad LANE_COMMAND" "rc=$RC $(cat "$TMP/bad2.err")"; fi
fresh_state
board_list $'400\tP0\tbuilder\t-\tBuild card'
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^command: /watch' && printf '%s' "$OUT" | grep -q "^worktree: $WORKTREE_BASE/card-400"; then pass "defaults unchanged: /watch in a card worktree"; else flunk "defaults" "out=[$OUT]"; fi

# ============================================================================
echo "32. Dependencies: a card GitHub says is blocked by an OPEN issue waits, whatever its"
echo "    priority; the blocker is picked instead; one FYI comment per blocker set; picked"
echo "    the poll after the blocker closes; a failed lookup fails CLOSED"
# ============================================================================
deps_board() {
  board_list $'500\tP0\tbuilder\t-\tThe dependent card (P0, but waits)' \
             $'501\tP1\tbuilder\t-\tThe prerequisite'
  board_show '{"number":501,"state":"OPEN","status":"In progress","labels":[]}'
  GH_DEPS_DIR="$STATE/deps"; mkdir -p "$GH_DEPS_DIR"
  echo '[{"number":501,"state":"open"},{"number":502,"state":"closed"}]' > "$GH_DEPS_DIR/500.json"
}
fresh_state; deps_board
OUT="$(run --dry-run 2>"$TMP/deps.err")"
if printf '%s' "$OUT" | grep -q '^pick: #501 ' && grep -q "#500 skipped: waiting on open issue(s) 501" "$TMP/deps.err"; then
  pass "dry-run: the P0 card waits on #501 (its closed blocker #502 does not count); #501 is picked"
else
  flunk "deps dry-run" "out=[$OUT] err=[$(cat "$TMP/deps.err")]"
fi
if not_grepped "gh issue comment"; then pass "dry-run posts no waiting comment"; else flunk "dry-run commented" "$(cat "$TESTLOG")"; fi

fresh_state; deps_board
CLAUDE_SUBTYPE=success CLAUDE_RC=0
WATCHER_MAX_POLLS=2 run >/dev/null 2>&1 || true
comments="$(grep -c "gh issue comment 500 --body 🤖 FYI: this card waits on \[#501\](https://github.com/owner/proj/issues/501)" "$TESTLOG")"
if [ "$comments" = 1 ] && [ "$(cat "$STATE/waiting/500" 2>/dev/null)" = "501" ]; then
  pass "two polls, one FYI comment on #500 naming the blocker as a link; the marker holds the blocker list"
else
  flunk "waiting comment" "count=$comments $(cat "$TESTLOG")"
fi
if grepped "/watch 501" && not_grepped "/watch 500"; then pass "#501 launched, #500 never"; else flunk "deps launch" "$(cat "$TESTLOG")"; fi
# the blocker set changes → one more comment, for the new set
echo '[{"number":503,"state":"open"}]' > "$GH_DEPS_DIR/500.json"
run --once >/dev/null 2>&1 || true
if grepped "gh issue comment 500 --body 🤖 FYI: this card waits on [#503]" && [ "$(cat "$STATE/waiting/500")" = "503" ]; then
  pass "a changed blocker set gets one new comment"
else
  flunk "changed blocker set" "$(cat "$TESTLOG")"
fi
# the blocker closes → picked, marker gone
echo '[{"number":503,"state":"closed"}]' > "$GH_DEPS_DIR/500.json"
board_show '{"number":500,"state":"OPEN","status":"In progress","labels":[]}'
run --once >/dev/null 2>&1 || true
if grepped "/watch 500" && [ ! -f "$STATE/waiting/500" ]; then pass "once every blocker is closed the card is picked and its waiting marker is cleared"; else flunk "unblocked pick" "$(cat "$TESTLOG")"; fi

fresh_state; deps_board
OUT="$(GH_API_FAIL=1 run --dry-run 2>"$TMP/deps2.err")"; RC=$?
if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q 'nothing claimable' && grep -q "#500 skipped: could not read what it is blocked by" "$TMP/deps2.err"; then
  pass "a failed dependency lookup skips the card for this poll (fail closed), never launches it"
else
  flunk "deps lookup failure" "rc=$RC out=[$OUT] err=[$(cat "$TMP/deps2.err")]"
fi
# a Waiting-stage row goes through the same check
fresh_state
board_list ""
BOARD_QUEUE_FILE="$STATE/board-queue.tsv"
printf '510\tP0\tbuilder\t-\tWaiting\tQueued, blocked\n511\tP1\tbuilder\t-\tWaiting\tQueued, free\n' > "$BOARD_QUEUE_FILE"
board_show '{"number":511,"state":"OPEN","status":"In progress","labels":[]}'
GH_DEPS_DIR="$STATE/deps"; mkdir -p "$GH_DEPS_DIR"; echo '[{"number":511,"state":"open"}]' > "$GH_DEPS_DIR/510.json"
run --once >/dev/null 2>&1 || true
if grepped "board.py move 511 In progress" && not_grepped "/watch 510"; then pass "a Waiting-stage card that waits on another is skipped the same way"; else flunk "queue deps" "$(cat "$TESTLOG")"; fi
unset BOARD_QUEUE_FILE GH_DEPS_DIR

# ============================================================================
echo "32b. The net under it: a dependency the card only states in WORDS (nothing recorded in"
echo "     Relationships) is honored too — riffmap #447, September 20, 2026"
# ============================================================================
# /triage is the only thing that records a relationship from a card's words, and it only
# sees cards with NO agent: label — so the cards a design tick spins off, labeled from
# birth, arrived with their Depends-on line and an empty Relationships field, and were
# launched in board order. These cases pin the body reader: what counts, what does not,
# and that it fails closed on every lookup it cannot make.
words_board() { # <body> — card 520 says it, 521 is the prerequisite; nothing recorded
  board_list $'520\tP0\tbuilder\t-\tThe dependent card (P0, but its body says it waits)' \
             $'521\tP1\tbuilder\t-\tThe prerequisite'
  board_show '{"number":521,"state":"OPEN","status":"In progress","labels":[]}'
  GH_BODY_DIR="$STATE/bodies"; mkdir -p "$GH_BODY_DIR"
  printf '%s\n' "$1" > "$GH_BODY_DIR/520.md"
  GH_STATE_DIR="$STATE/states"; mkdir -p "$GH_STATE_DIR"
}
L521="[#521](https://github.com/owner/proj/issues/521)"

fresh_state; words_board "## Depends on / Part of

- Depends on $L521 (\`builder\`) for the contract field.
- Part of [#444](https://github.com/owner/proj/issues/444) (spec 3.19 #10)."
OUT="$(run --dry-run 2>"$TMP/words.err")"
if printf '%s' "$OUT" | grep -q '^pick: #521 ' && grep -q "#520 skipped: its own words say it waits on open issue(s) 521 (nothing recorded in Relationships)" "$TMP/words.err"; then
  pass "the real #447 shape: the Depends-on bullet blocks the card, the Part-of bullet does not"
else
  flunk "words dry-run" "out=[$OUT] err=[$(cat "$TMP/words.err")]"
fi

fresh_state; words_board "Depends on $L521."
CLAUDE_SUBTYPE=success CLAUDE_RC=0
WATCHER_MAX_POLLS=2 run >/dev/null 2>&1 || true
if grepped "gh issue comment 520 --body 🤖 FYI: this card waits on $L521, so I am not starting it yet. - Where I got that: this card's own words say so, and its Relationships field is empty." \
   && [ "$(cat "$STATE/waiting/520" 2>/dev/null)" = "521" ]; then
  pass "the FYI comment says the dependency came from the card's own words, and how to undo it"
else
  flunk "words comment" "$(cat "$TESTLOG")"
fi
if grepped "/watch 521" && not_grepped "/watch 520"; then pass "#521 launched, #520 never"; else flunk "words launch" "$(cat "$TESTLOG")"; fi

# Context, not order: none of these is a prerequisite (triage.md draws the same line).
fresh_state; words_board "Part of $L521. See $L521, like $L521 and same as $L521."
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #520 '; then pass "part of / see / like / same as are context, not order — #520 is picked"; else flunk "context refs" "out=[$OUT]"; fi

fresh_state; words_board "This does not depend on $L521 any more."
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #520 '; then pass "a negated phrase (does not depend on) is not a dependency"; else flunk "negation" "out=[$OUT]"; fi

# The contraction says the same thing as the word, and reads the opposite way if missed:
# the card would wait on the one card it says it does not wait on, and the FYI comment
# would assert the opposite of the sentence it came from.
fresh_state; words_board "This card isn't blocked by $L521, whatever the title suggests."
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #520 '; then pass "a contraction negates it too (isn't blocked by)"; else flunk "contraction" "out=[$OUT]"; fi

fresh_state; words_board "It doesn’t depend on $L521 any more."
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #520 '; then pass "…including the curly apostrophe a Mac types by itself"; else flunk "curly contraction" "out=[$OUT]"; fi

# board.md prints the label itself in bold, so the bolded form is one copy-paste away.
fresh_state; words_board "**Depends on** $L521 for the contract field."
OUT="$(run --dry-run 2>"$TMP/words-bold.err")"
if grep -q "#520 skipped: its own words say it waits on open issue(s) 521" "$TMP/words-bold.err"; then
  pass "the bolded house form (**Depends on** [#521](…)) is read like the plain one"
else
  flunk "bold phrase" "err=[$(cat "$TMP/words-bold.err")]"
fi

fresh_state; words_board "Depends on [#530](https://github.com/other/repo/issues/530)."
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #520 '; then pass "a card in ANOTHER repo is not followed — every link the spawner builds is this repo's"; else flunk "cross-repo" "out=[$OUT]"; fi

fresh_state; words_board "Depends on $L521."
echo closed > "$STATE/states/521"
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #520 '; then pass "a prerequisite that is already closed does not hold the card"; else flunk "closed ref" "out=[$OUT]"; fi

fresh_state; words_board "Depends on $L521 and [#522](https://github.com/owner/proj/issues/522), #523."
echo closed > "$STATE/states/522"
OUT="$(run --dry-run 2>"$TMP/words3.err")"
if grep -q "#520 skipped: its own words say it waits on open issue(s) 521 523" "$TMP/words3.err"; then
  pass "a list after one phrase is read whole (521, 522, 523), and the closed one is dropped"
else
  flunk "ref list" "err=[$(cat "$TMP/words3.err")]"
fi

fresh_state; words_board "Depends on #520 itself, which is this card."
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #520 '; then pass "a card naming itself is not blocked by itself"; else flunk "self ref" "out=[$OUT]"; fi

# The recorded field wins, and the body is never even fetched for a card that has one:
# a human-set relationship is the deliberate one, and the extra calls stay off the path.
fresh_state; words_board "Depends on $L521."
GH_DEPS_DIR="$STATE/deps"; mkdir -p "$GH_DEPS_DIR"
echo '[{"number":599,"state":"open"}]' > "$GH_DEPS_DIR/520.json"
run --dry-run >/dev/null 2>"$TMP/words4.err" || true
if grep -q "#520 skipped: waiting on open issue(s) 599" "$TMP/words4.err" && not_grepped "issues/520 --jq .body"; then
  pass "a recorded relationship is believed on its own — the body is not read at all"
else
  flunk "recorded wins" "err=[$(cat "$TMP/words4.err")] log=[$(cat "$TESTLOG")]"
fi
unset GH_DEPS_DIR

fresh_state; words_board "Depends on $L521."
OUT="$(GH_BODY_FAIL=1 run --dry-run 2>"$TMP/words5.err")"; RC=$?
# The outage is the API, not one card: every row's body lookup fails, so the whole poll
# comes up empty rather than guessing at any of them. One quiet minute, no wrong launch.
if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q 'nothing claimable' \
   && grep -q "#520 skipped: could not read the card's own words" "$TMP/words5.err" \
   && grep -q "#521 skipped: could not read the card's own words" "$TMP/words5.err"; then
  pass "a failed body lookup skips the card for this poll (fail closed), never launches it"
else
  flunk "body lookup failure" "rc=$RC out=[$OUT] err=[$(cat "$TMP/words5.err")]"
fi

fresh_state; words_board "Depends on $L521."
OUT="$(GH_STATE_FAIL=1 run --dry-run 2>"$TMP/words6.err")"; RC=$?
if [ "$RC" = 0 ] && grep -q "#520 skipped: could not read the card's own words" "$TMP/words6.err"; then
  pass "a failed state lookup on a referenced card fails closed the same way"
else
  flunk "state lookup failure" "rc=$RC out=[$OUT] err=[$(cat "$TMP/words6.err")]"
fi

fresh_state; words_board "No prerequisites here at all."
OUT="$(run --dry-run 2>/dev/null)"
if printf '%s' "$OUT" | grep -q '^pick: #520 '; then pass "a body with no dependency phrase costs one lookup and blocks nothing"; else flunk "no phrase" "out=[$OUT]"; fi

# A number that names no issue can block nothing. Handled like an outage it would skip the
# card on every poll for ever — no comment, no park, no attempt counted, one journal line.
fresh_state; words_board "Depends on #99999 and $L521."
echo 404 > "$STATE/states/99999"
OUT="$(run --dry-run 2>"$TMP/words404.err")"
if grep -q "#520 skipped: its own words say it waits on open issue(s) 521" "$TMP/words404.err" \
   && grep -q "there is no such issue in owner/proj" "$TMP/words404.err"; then
  pass "a reference to an issue that does not exist is dropped and logged, not treated as an outage"
else
  flunk "404 ref" "err=[$(cat "$TMP/words404.err")]"
fi

fresh_state; words_board "Depends on #601, #602, #603, #604, #605, #606, #607, #608, #609, #610, #611."
OUT="$(run --dry-run 2>"$TMP/words11.err")"
# Unlike an API outage, this refusal is about ONE body: #521, whose own body says nothing,
# is still claimable. Nothing is answered from the first ten references.
if printf '%s' "$OUT" | grep -q '^pick: #521 ' && grep -q "#520 skipped: could not read the card's own words" "$TMP/words11.err"; then
  pass "more than ten references is a body this cannot read: it fails closed, not on the first ten"
else
  flunk "ref cap" "out=[$OUT] err=[$(cat "$TMP/words11.err")]"
fi
unset GH_BODY_DIR GH_STATE_DIR

# ============================================================================
echo "33. A tick that relabels the card for ANOTHER lane (triage, or /watch routing a design"
echo "    card) is a success for this lane: attempts reset, nothing parked"
# ============================================================================
fresh_state
board_list $'600\tP2\t-\t-\tUnlabeled card'
board_show '{"number":600,"state":"OPEN","status":"In progress","labels":["agent: ui"]}'
mkdir -p "$STATE/attempts"; echo 1 > "$STATE/attempts/600"
CLAUDE_SUBTYPE=success CLAUDE_RC=0
LANE_AGENTS='-' LANE_COMMAND=/triage LANE_WORKDIR=root run --once >/dev/null 2>"$TMP/left.err" || true
if [ ! -f "$STATE/attempts/600" ] && grep -q "relabeled the card for another lane (agent 'ui')" "$TMP/left.err" && not_grepped "board.py move 600"; then
  pass "card left In progress but now carries another lane's label → attempts reset, not parked"
else
  flunk "left-lane success" "attempts=$(cat "$STATE/attempts/600" 2>/dev/null) err=[$(cat "$TMP/left.err")] $(cat "$TESTLOG")"
fi
fresh_state
board_list $'600\tP2\t-\t-\tUnlabeled card'
board_show '{"number":600,"state":"OPEN","status":"In progress","labels":[]}'
mkdir -p "$STATE/attempts"; echo 1 > "$STATE/attempts/600"
LANE_AGENTS='-' LANE_COMMAND=/triage LANE_WORKDIR=root run --once >/dev/null 2>&1 || true
if grepped "board.py move 600 Stuck - Needs help"; then pass "…but a tick that leaves it unlabeled and In progress is still an attempt: parked at 2"; else flunk "unlabeled still counts" "$(cat "$TESTLOG")"; fi

# ============================================================================
echo "In review sweep (watcher#18): a column nothing read becomes one the spawner empties"
board_review() { BOARD_REVIEW_FILE="$STATE/board-review.tsv"; printf '%s\n' "$@" > "$BOARD_REVIEW_FILE"; }
PRS="$TMP/prs-700.json"
echo '[{"number":71,"url":"https://github.com/owner/proj/pull/71","body":"Closes #700"}]' > "$PRS"

fresh_state; board_list ""; board_review $'700\tP1\tbuilder\t-\tFresh in review'
GH_PR_LIST_FILE="$PRS" run --once >/dev/null 2>&1
if not_grepped "board.py move 700" && not_grepped "gh issue comment 700"; then pass "fresh In review card with an open PR → untouched"; else flunk "fresh card was touched" "$(cat "$TESTLOG")"; fi
[ -f "$STATE/inreview/700.seen" ] && pass "…and its clock started (inreview/700.seen)" || flunk "no .seen marker"

fresh_state; board_list ""; board_review $'700\tP1\tbuilder\t-\tNo PR yet'
GH_PR_LIST_FILE="" run --once >/dev/null 2>&1
if not_grepped "board.py move 700"; then pass "no PR but inside the grace → untouched (a merge may be closing it)"; else flunk "parked inside the grace" "$(cat "$TESTLOG")"; fi

fresh_state; board_list ""; board_review $'700\tP1\tbuilder\t-\tDropped here by hand'
GH_PR_LIST_FILE="" INREVIEW_NO_PR_GRACE_SEC=0 run --once >/dev/null 2>&1
if grepped "board.py move 700 Stuck - Needs help"; then pass "In review + no PR past the grace → Stuck"; else flunk "expected a move to Stuck" "$(cat "$TESTLOG")"; fi
if grep -q 'gh issue comment 700.*Needs Steve: .*no pull request.*Question: .*\*\*A\. .*\*\*B\. .*\*\*C\. ' "$TESTLOG"; then pass "…with the ask first, one question, lettered options (talking-to-steve shape)"; else flunk "Stuck comment shape" "$(cat "$TESTLOG")"; fi

fresh_state; board_list ""; board_review $'700\tP1\tbuilder\t-\tStale green PR'
GH_PR_LIST_FILE="$PRS" INREVIEW_STALE_SEC=0 run --once >/dev/null 2>&1
if grepped "board.py move 700 In progress" && not_grepped "board.py move 700 Stuck"; then pass "In review + open PR past the stale limit → back to In progress"; else flunk "expected a re-queue" "$(cat "$TESTLOG")"; fi
if grep -q 'gh issue comment 700.*FYI: PR #71' "$TESTLOG"; then pass "…with an FYI naming the PR"; else flunk "re-queue FYI" "$(cat "$TESTLOG")"; fi
[ -f "$STATE/inreview/700.requeued" ] && [ ! -f "$STATE/inreview/700.seen" ] && pass "…and the one-turn marker is on file, the clock cleared" || flunk "markers after re-queue" "$(ls "$STATE/inreview")"
: > "$TESTLOG"
GH_PR_LIST_FILE="$PRS" run --once >/dev/null 2>&1
if grepped "board.py move 700 Stuck - Needs help" && not_grepped "board.py move 700 In progress"; then pass "back in In review after its one turn → Stuck, not a second re-queue"; else flunk "expected Stuck on the second visit" "$(cat "$TESTLOG")"; fi
if grep -q 'gh issue comment 700.*Needs Steve: .*pull/71.*Question: ' "$TESTLOG"; then pass "…and the Stuck comment carries the PR link above the question"; else flunk "second-visit comment" "$(cat "$TESTLOG")"; fi
[ ! -f "$STATE/inreview/700.requeued" ] && pass "…and parking clears the marker: Steve's answer starts the card over" || flunk "requeued marker survived the park"

fresh_state; board_list ""; board_review $'700\tP1\tbuilder\t-\tMove keeps failing'
GH_PR_LIST_FILE="" INREVIEW_NO_PR_GRACE_SEC=0 BOARD_MOVE_FAIL=1 run --once >/dev/null 2>&1
GH_PR_LIST_FILE="" INREVIEW_NO_PR_GRACE_SEC=0 BOARD_MOVE_FAIL=1 run --once >/dev/null 2>&1
[ "$(grep -c 'gh issue comment 700' "$TESTLOG")" = 1 ] && [ "$(grep -c 'board.py move 700 Stuck' "$TESTLOG")" = 2 ] \
  && pass "a failing move is retried every poll; the comment goes out once" || flunk "retry/comment counts" "$(cat "$TESTLOG")"
BOARD_MOVE_FAIL=0

fresh_state; board_list ""; board_review $'700\tP1\tbuilder\t-\tgh is down'
GH_PR_LIST_FAIL=1 INREVIEW_NO_PR_GRACE_SEC=0 run --once >/dev/null 2>&1
if not_grepped "board.py move 700" && not_grepped "gh issue comment 700"; then pass "a failed PR lookup is not 'no PR': the card is left alone for the poll (fails closed)"; else flunk "parked on a gh outage" "$(cat "$TESTLOG")"; fi
GH_PR_LIST_FAIL=0

fresh_state; board_review ""; mkdir -p "$STATE/inreview"; : > "$STATE/inreview/703.requeued"
board_list $'703\tP1\tbuilder\t-\tRe-queued, then the tick parks it itself'
board_show '{"number":703,"state":"OPEN","status":"Stuck - Needs help","labels":[]}'
run --once >/dev/null 2>&1
[ ! -f "$STATE/inreview/703.requeued" ] && pass "a tick that parks a re-queued card itself clears the one-turn marker (Steve's answer starts it over)" || flunk "requeued marker survived the tick's own park"

fresh_state; board_review ""; mkdir -p "$STATE/inreview" "$STATE/attempts"; : > "$STATE/inreview/704.requeued"; echo 2 > "$STATE/attempts/704"
board_list $'704\tP1\tbuilder\t-\tRe-queued, then parked by the spawner'
run --once >/dev/null 2>&1
[ ! -f "$STATE/inreview/704.requeued" ] && pass "park_stuck clears it too" || flunk "requeued marker survived park_stuck"

fresh_state; board_list ""; board_review $'700\tP1\tdesigner\t-\tAnother lane\'s card' $'701\tP1\tbuilder\tin-flight\tA tick is on it'
GH_PR_LIST_FILE="" INREVIEW_NO_PR_GRACE_SEC=0 LANE_AGENTS='!architect,designer,-' run --once >/dev/null 2>&1
if not_grepped "board.py move 70"; then pass "another lane's card and an in-flight card are not swept"; else flunk "swept a card that is not this lane's" "$(cat "$TESTLOG")"; fi

fresh_state; board_list ""; board_review $'700\tP1\tbuilder\t-\tSeen once'
GH_PR_LIST_FILE="$PRS" run --once >/dev/null 2>&1
board_review ""
GH_PR_LIST_FILE="$PRS" run --once >/dev/null 2>&1
[ ! -f "$STATE/inreview/700.seen" ] && pass "a card that left In review loses its clock" || flunk ".seen survived the card leaving"

fresh_state; board_list $'702\tP1\tbuilder\t-\tOrdinary card'; board_review ""
board_show '{"number":702,"state":"CLOSED","status":"Done","labels":[]}'
BOARD_REVIEW_FAIL=1 run --once >/dev/null 2>&1
if grepped "watch 702"; then pass "a failed In review poll never stops In progress from being served"; else flunk "tick did not launch" "$(cat "$TESTLOG")"; fi
BOARD_REVIEW_FAIL=0; BOARD_REVIEW_FILE=/dev/null; GH_PR_LIST_FILE=""

# ============================================================================
echo "Self-pull (READY_PULL): the loop starts a card nobody handed it, under a WIP cap"
# ============================================================================
# Ready is the THIRD source, after In progress and Waiting, and only while the In progress
# column is under WIP_MAX. Off unless the project's env file opts in.

# --- off by default ---------------------------------------------------------------------
fresh_state; board_list ""; board_ready $'800\tP1\tbuilder\t-\tReady\tA ready card'
board_show '{"number":800,"state":"OPEN","labels":[],"status":"In progress"}'
run --once >/dev/null 2>&1 || true
if not_grepped "/watch 800"; then pass "READY_PULL unset: a Ready card is never pulled"; else flunk "pulled from Ready with READY_PULL off" "$(cat "$TESTLOG")"; fi
if not_grepped "board.py move 800"; then pass "…and never moved"; else flunk "moved a Ready card with READY_PULL off"; fi

# --- on: nothing elsewhere, so Ready is served ------------------------------------------
fresh_state; board_list ""; board_ready $'800\tP1\tbuilder\t-\tReady\tA ready card'
board_show '{"number":800,"state":"OPEN","labels":[],"status":"In progress"}'
READY_PULL=1 run --once >/dev/null 2>"$TMP/ready.err" || true
if grepped "board.py move 800 In progress"; then pass "READY_PULL=1: the Ready card is moved to In progress before launch"; else flunk "expected board.py move 800 In progress" "$(cat "$TESTLOG")"; fi
if grepped "/watch 800"; then pass "…and launched"; else flunk "expected a launch for #800" "$(cat "$TESTLOG")"; fi
if grep -q "taken from Ready" "$TMP/ready.err"; then pass "…and the log names Ready, not Waiting"; else flunk "log says the wrong source column" "$(cat "$TMP/ready.err")"; fi

# --- In progress is always served first -------------------------------------------------
fresh_state; board_list $'810\tP1\tbuilder\t-\t-\tHanded over by Steve'
board_ready $'811\tP0\tbuilder\t-\tReady\tHigher priority, but only Ready'
board_show '{"number":810,"state":"OPEN","labels":[],"status":"In progress"}'
READY_PULL=1 run --once >/dev/null 2>&1 || true
if grepped "/watch 810" && not_grepped "/watch 811"; then pass "a card Steve moved by hand beats a higher-priority Ready card"; else flunk "Ready jumped the queue" "$(cat "$TESTLOG")"; fi

# --- Waiting beats Ready ----------------------------------------------------------------
fresh_state; board_list ""
BOARD_QUEUE_FILE="$STATE/board-queue.tsv"
printf '820\tP1\tbuilder\t-\tWaiting\tDragged to Waiting\n' > "$BOARD_QUEUE_FILE"
board_ready $'821\tP0\tbuilder\t-\tReady\tOnly Ready'
board_show '{"number":820,"state":"OPEN","labels":[],"status":"In progress"}'
READY_PULL=1 run --once >/dev/null 2>&1 || true
if grepped "/watch 820" && not_grepped "/watch 821"; then pass "Waiting is served before Ready"; else flunk "Ready beat Waiting" "$(cat "$TESTLOG")"; fi
unset BOARD_QUEUE_FILE

# --- the WIP cap ------------------------------------------------------------------------
# Two cards already In progress (in-flight, so unclaimable) and WIP_MAX=2 → the column is
# full; nothing is promoted even though Ready has a claimable card.
fresh_state; board_list $'830\tP1\tbuilder\tin-flight\t-\tBusy one' $'831\tP1\tbuilder\tin-flight\t-\tBusy two'
board_ready $'832\tP1\tbuilder\t-\tReady\tWants to start'
board_show '{"number":832,"state":"OPEN","labels":[],"status":"In progress"}'
READY_PULL=1 WIP_MAX=2 run --once >/dev/null 2>"$TMP/wip.err" || true
if not_grepped "/watch 832"; then pass "WIP_MAX=2 with 2 cards In progress: no promotion"; else flunk "promoted past the WIP cap" "$(cat "$TESTLOG")"; fi
if grep -q "WIP_MAX=2" "$TMP/wip.err"; then pass "…and the log says why the loop is quiet"; else flunk "no WIP reason logged" "$(cat "$TMP/wip.err")"; fi

# One card in flight, cap of 2 → there is room, so it promotes.
fresh_state; board_list $'830\tP1\tbuilder\tin-flight\t-\tBusy one'
board_ready $'832\tP1\tbuilder\t-\tReady\tWants to start'
board_show '{"number":832,"state":"OPEN","labels":[],"status":"In progress"}'
READY_PULL=1 WIP_MAX=2 run --once >/dev/null 2>&1 || true
if grepped "/watch 832"; then pass "WIP_MAX=2 with 1 card In progress: room, so it promotes"; else flunk "did not promote with room under the cap" "$(cat "$TESTLOG")"; fi

# An empty Ready column plus a full In progress column says nothing: the cap is not what
# is holding anything back, and that line would repeat once a minute, per lane, forever.
fresh_state; board_list $'833\tP1\tbuilder\tin-flight\t-\tBusy one' $'834\tP1\tbuilder\tin-flight\t-\tBusy two'
board_ready ""
READY_PULL=1 WIP_MAX=2 run --once >/dev/null 2>"$TMP/quiet.err" || true
if ! grep -q "not pulling from Ready" "$TMP/quiet.err"; then pass "nothing in Ready: the WIP cap stays quiet"; else flunk "logged a WIP reason with an empty Ready column" "$(cat "$TMP/quiet.err")"; fi

# --- the lane filter applies to Ready exactly as it does to the other two ----------------
fresh_state; board_list ""; board_ready $'840\tP1\tarchitect\t-\tReady\tDesign work'
board_show '{"number":840,"state":"OPEN","labels":[],"status":"In progress"}'
READY_PULL=1 LANE_AGENTS='!architect,designer,-' run --once >/dev/null 2>&1 || true
if not_grepped "/watch 840"; then pass "a Ready card belonging to another lane is not pulled"; else flunk "build lane pulled a design card from Ready" "$(cat "$TESTLOG")"; fi

# an in-flight Ready row is skipped like any other row
fresh_state; board_list ""; board_ready $'841\tP1\tbuilder\tin-flight\tReady\tAlready owned'
READY_PULL=1 run --once >/dev/null 2>&1 || true
if not_grepped "/watch 841"; then pass "an in-flight Ready row is not launched"; else flunk "in-flight Ready row launched"; fi

# --- failures close, never open ----------------------------------------------------------
# A board with no Ready column: `list Ready` exits 1. That must not fail the poll, and must
# not stop In progress from being served.
fresh_state; board_list $'850\tP1\tbuilder\t-\t-\tOrdinary card'
board_show '{"number":850,"state":"CLOSED","status":"Done","labels":[]}'
READY_PULL=1 BOARD_READY_FAIL=1 run --once >/dev/null 2>"$TMP/noready.err" || true
if grepped "/watch 850"; then pass "a board with no Ready column still serves In progress"; else flunk "a failed Ready lookup stopped the poll" "$(cat "$TESTLOG")"; fi
if grep -q "not pulling from Ready this poll" "$TMP/noready.err"; then pass "…and says so once"; else flunk "no log line for the failed Ready lookup" "$(cat "$TMP/noready.err")"; fi

# The promote itself failing means NOT launching: a claim on a card the board still calls
# Ready would be a card nobody can see is being worked.
fresh_state; board_list ""; board_ready $'860\tP1\tbuilder\t-\tReady\tMove will fail'
board_show '{"number":860,"state":"OPEN","labels":[],"status":"Ready"}'
READY_PULL=1 BOARD_MOVE_FAIL=1 run --once >/dev/null 2>&1 || true
if not_grepped "/watch 860"; then pass "a failed promote does not launch the tick"; else flunk "launched despite a failed move" "$(cat "$TESTLOG")"; fi

# --- dry-run tells the truth and changes nothing -----------------------------------------
fresh_state; board_list ""; board_ready $'870\tP1\tbuilder\t-\tReady\tDry run me'
OUT="$(READY_PULL=1 run --dry-run 2>/dev/null || true)"
if printf '%s' "$OUT" | grep -q '^pick: #870 ' && printf '%s' "$OUT" | grep -q 'from Ready'; then
  pass "dry-run picks the Ready card and says where it came from"
else
  flunk "dry-run Ready pick" "$OUT"
fi
if not_grepped "board.py move 870"; then pass "…and moves nothing"; else flunk "dry-run moved a card"; fi
BOARD_READY_FILE=/dev/null; BOARD_READY_FAIL=0

# ============================================================================
echo "Declined (watch.md 4b): a card that gates itself goes back to Backlog, and that is a success"
# ============================================================================
# riffmap #425, September 19, 2026: the card body said "do not work this until Steve says
# the production-posture question is live". The tick read it, correctly refused, released
# the claim — and left the card In progress, which this script reads as "no progress".
# Two of those and it parks the card in Stuck: a tick punished for obeying an instruction,
# and Steve woken for a card that is fine. watch.md 4b now sends it to Backlog instead.

fresh_state
board_list $'900\tP1\tarchitect\t-\t-\tA card that gates itself'
board_show '{"number":900,"state":"OPEN","status":"Backlog","labels":["agent: architect"]}'
CLAUDE_SUBTYPE=success CLAUDE_RC=0
run --once >/dev/null 2>"$TMP/decline.err" || true
[ ! -f "$STATE/attempts/900" ] && pass "declined to Backlog: attempts reset, not bumped" || flunk "attempts bumped on a decline" "$(cat "$STATE/attempts/900")"
if not_grepped "board.py move 900 Stuck"; then pass "…and the card is not parked"; else flunk "a declined card was parked" "$(cat "$TESTLOG")"; fi
if grep -q "succeeded and left In progress" "$TMP/decline.err"; then pass "…and the spawner logs it as a success"; else flunk "decline not logged as success" "$(cat "$TMP/decline.err")"; fi

# A second decline is still a success — the count never creeps toward the park threshold,
# however many times Steve moves a still-gated card back to Ready.
run --once >/dev/null 2>&1 || true
[ ! -f "$STATE/attempts/900" ] && pass "a second decline still resets: no creep toward the park threshold" || flunk "attempts accumulated across declines"

# The contrast, and the reason Backlog is load-bearing: the SAME successful exit with the
# card left In progress is no-progress, and parks on the second attempt.
fresh_state
board_list $'901\tP1\tarchitect\t-\t-\tLeft sitting In progress'
board_show '{"number":901,"state":"OPEN","status":"In progress","labels":["agent: architect"]}'
CLAUDE_SUBTYPE=success CLAUDE_RC=0
run --once >/dev/null 2>&1 || true
[ "$(cat "$STATE/attempts/901" 2>/dev/null)" = 1 ] && pass "left In progress instead: attempts bumped to 1" || flunk "expected attempts/901=1" "$(cat "$STATE/attempts/901" 2>/dev/null)"
run --once >/dev/null 2>&1 || true
if grepped "board.py move 901 Stuck - Needs help"; then pass "…and parked on the second — which is exactly what 4b avoids"; else flunk "expected a park for #901" "$(cat "$TESTLOG")"; fi

echo "$n cases, $( [ $fail = 0 ] && echo all passed || echo SOME FAILED )"
exit $fail
