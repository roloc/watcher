#!/usr/bin/env bash
#
# The watcher's spawner: a systemd-kept-alive loop with no Claude session of its own. It
# polls one project's board once a minute and starts `claude -p "/watch <n>"` only when a
# claimable card exists. One spawner that refuses to start a second tick while one runs IS
# the lock (a pidfile) — there is no compare-and-set on the board's `in-flight` label. A
# project may run two of these side by side as LANES (LANE_AGENTS below), and the lock
# still holds because the lanes partition the board and never want the same card. The
# spawner is also the stale-claim sweeper: it never claims a card itself.
#
# It runs INSIDE a throwaway box with --dangerously-skip-permissions: no allow lists, no
# trust probes, no permission prompts. The box is the boundary — a scoped GitHub identity,
# a Claude login token, nothing else worth protecting. Rebuild it if a tick goes wrong.
#
# One project per instance, chosen by a project file (projects/<name>.env), sourced first:
#   PROJECT_NAME     short name (state/log dirs are keyed on it)
#   PROJECT_ROOT     the project's main checkout on this box (worktrees go beside it)
#   BOARD_OWNER      GitHub user/org that owns the Projects board   (board.py reads these)
#   BOARD_NUMBER     the board's number
#   DISCORD_WEBHOOK_URL  optional; a per-project Discord channel for card events. Usually
#                    set once for the box in /etc/watcher/env instead (see notify.py).
#   DISCORD_MENTION  optional; who a Stuck alert pings (`<@id>`, `<@&roleid>`, `@here`)
#   LANE_AGENTS      optional; which cards THIS instance may pick, by `agent:` label. A
#                    comma-separated list of agent names (`architect,designer`), or the
#                    same list with a leading `!` for every card EXCEPT those
#                    (`!architect,designer`). `-` names a card with no agent label. Unset
#                    means every card — a project with a single lane. Two instances of one
#                    project (two systemd units, two project files, two state dirs) become
#                    two LANES that run side by side; give them complementary lists so no
#                    card can ever belong to both, and the per-instance pidfile stays the
#                    only lock the design needs. See docs/how-it-works.md, "Lanes".
#   LANE_COMMAND     optional; the slash command this lane's tick runs (default `/watch`).
#                    A triage lane runs `/triage`: it labels an unlabeled card and records
#                    what the card waits on, then stops — see commands/triage.md.
#   LANE_WORKDIR     optional; `card` (default) runs the tick in a worktree made for the
#                    card, `root` runs it in PROJECT_ROOT with no worktree at all — for a
#                    lane whose tick only touches the board, never the code.
#
# A card that GitHub says is blocked by an open issue (the issue's "Relationships" field,
# or what /triage recorded from the card's own words) is skipped by every lane until the
# blocker closes, so Steve can hand over a batch of cards in one go and the prerequisite
# is worked first. One FYI comment per card says what it waits on.
#
# Every external command is overridable so this can be tested with stubs, never the network:
#   WATCHER_PROJECT    path to the project file                  (default: none — required
#                      unless WATCHER_ROOT is given directly)
#   WATCHER_ROOT       the project checkout                     (default: $PROJECT_ROOT)
#   WATCHER_CLAUDE     claude binary/command                    (default: claude)
#   WATCHER_GH         gh binary/command                        (default: gh)
#   WATCHER_BOARD      board.py invocation                      (default: python3 <here>/board.py)
#   WATCHER_WORKTREE   worktree.sh invocation                   (default: <here>/worktree.sh)
#   WATCHER_STATE      state dir                                (default: ~/.local/state/watcher/<name>)
#   WATCHER_LOGS       log dir                                  (default: <state>/logs)
#   WATCHER_SLEEP      when set, EVERY sleep uses this many seconds (tests: WATCHER_SLEEP=0);
#                      the watchdog's TICK_TIMEOUT is untouched by this, on purpose.
#   TICK_TIMEOUT       watchdog ceiling in MINUTES              (default: 150)
#   WATCHER_TICK_TIMEOUT_SEC  watchdog ceiling in SECONDS instead (tests only)
#   TICK_MAX_TURNS     --max-turns for the tick                 (default: 400)
#   TICK_MAX_BUDGET_USD  --max-budget-usd for the tick          (default: 15)
#   POLL_INTERVAL      daemon poll cadence in SECONDS           (default: 60)
#   WATCHER_MAX_POLLS  stop the daemon loop after this many polls (tests only)
#
# Exported to the tick (so `/watch` can read the watcher's own docs from any worktree):
#   WATCHER_HOME       this repo's root                          (default: parent of bin/)
#
# Every comment this script leaves on a card is read by Steve on his phone. The shape is
# fixed by docs/talking-to-steve.md: first line is the ask, bullets, one question, lettered
# options, how to answer. test-spawn.sh checks the park comment keeps that shape.
#
#   bin/spawn.sh              # the daemon loop (what systemd runs)
#   bin/spawn.sh --dry-run    # print the pick + the exact claude command, exit 0
#   bin/spawn.sh --once       # one poll iteration, then exit (tests)
set -euo pipefail

WATCHER_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export WATCHER_HOME="${WATCHER_HOME:-$(dirname "$WATCHER_HERE")}"
if [ -n "${WATCHER_PROJECT:-}" ]; then
  # shellcheck disable=SC1090
  . "$WATCHER_PROJECT"
fi
WATCHER_ROOT="${WATCHER_ROOT:-${PROJECT_ROOT:-}}"
[ -n "$WATCHER_ROOT" ] || { echo "spawn.sh: set WATCHER_PROJECT=projects/<name>.env (or WATCHER_ROOT)" >&2; exit 64; }
PROJECT_NAME="${PROJECT_NAME:-$(basename "$WATCHER_ROOT")}"
export BOARD_OWNER="${BOARD_OWNER:-}" BOARD_NUMBER="${BOARD_NUMBER:-}"
# Discord is board.py's job (notify.py): every `board move` / `board stage` posts by itself,
# from this script and from inside the tick alike, so the webhook only has to reach
# board.py's environment. A project file may set it; /etc/watcher/env usually does.
export DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}" DISCORD_MENTION="${DISCORD_MENTION:-}"
# The repo the cards live in. `gh` infers a repo from the cwd, and this script's cwd is
# /opt/watcher — a checkout of the WATCHER, not the project — so on the first night every
# unclaim and sweeper comment hit the wrong repo and three finished cards sat in-flight
# (watcher#3). GH_REPO pins every repo-scoped gh call here AND in board.py (a child).
PROJECT_REPO="${PROJECT_REPO:-$(git -C "$WATCHER_ROOT" remote get-url origin 2>/dev/null | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')}"
[ -n "$PROJECT_REPO" ] && export GH_REPO="$PROJECT_REPO"
WATCHER_CLAUDE="${WATCHER_CLAUDE:-claude}"
WATCHER_GH="${WATCHER_GH:-gh}"
WATCHER_BOARD="${WATCHER_BOARD:-python3 $WATCHER_HERE/board.py}"
WATCHER_WORKTREE="${WATCHER_WORKTREE:-$WATCHER_HERE/worktree.sh}"
export WORKTREE_ROOT="$WATCHER_ROOT"
WATCHER_STATE="${WATCHER_STATE:-$HOME/.local/state/watcher/$PROJECT_NAME}"
WATCHER_LOGS="${WATCHER_LOGS:-$WATCHER_STATE/logs}"
TICK_TIMEOUT="${TICK_TIMEOUT:-150}"
TICK_MAX_TURNS="${TICK_MAX_TURNS:-400}"
TICK_MAX_BUDGET_USD="${TICK_MAX_BUDGET_USD:-15}"
POLL_INTERVAL="${POLL_INTERVAL:-60}"
LANE_AGENTS="${LANE_AGENTS:-}"
LANE_COMMAND="${LANE_COMMAND:-/watch}"
LANE_WORKDIR="${LANE_WORKDIR:-card}"
case "$LANE_WORKDIR" in card|root) ;; *) echo "spawn.sh: LANE_WORKDIR must be 'card' or 'root', not '$LANE_WORKDIR'" >&2; exit 64 ;; esac
case "$LANE_COMMAND" in /*) ;; *) echo "spawn.sh: LANE_COMMAND must be a slash command, not '$LANE_COMMAND'" >&2; exit 64 ;; esac

# ../<repo>-worktrees, same derivation as worktree.sh's own MAIN/HOME_DIR.
WORKTREE_BASE="$(dirname "$WATCHER_ROOT")/$(basename "$WATCHER_ROOT")-worktrees"

PIDFILE="$WATCHER_STATE/spawn.pid"
PAUSEFILE="$WATCHER_STATE/PAUSE"
CURRENTFILE="$WATCHER_STATE/current"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

# Every sleep in the script funnels through here so WATCHER_SLEEP=0 makes a test instant
# without touching the call sites that decide HOW LONG a real run would wait.
wsleep() {
  local secs="$1"
  if [ -n "${WATCHER_SLEEP:-}" ]; then secs="$WATCHER_SLEEP"; fi
  if [ "$secs" -gt 0 ] 2>/dev/null; then sleep "$secs"; fi
}

usage() {
  cat <<USAGE >&2
Usage: bin/spawn.sh [--dry-run | --once]
  (no flags)   the daemon loop systemd runs
  --dry-run    print the pick and the exact claude command, launch/write/claim nothing, exit 0
  --once       run one poll iteration and exit (tests)
USAGE
}

DRY_RUN=0
ONCE=0
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --once) ONCE=1 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "spawn.sh: unknown argument '$1'" >&2; usage; exit 2 ;;
    esac
    shift
  done
}

# --- section 1, step 1: the single-process guard ---------------------------------------
check_singleton() {
  if [ -f "$PIDFILE" ]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && [ "$pid" != "$$" ] && kill -0 "$pid" 2>/dev/null; then
      log "another spawn.sh (pid $pid, pidfile $PIDFILE) is already running; exiting"
      exit 1
    fi
  fi
  echo "$$" > "$PIDFILE"
}

# --- section 1, step 2: the board poll --------------------------------------------------
# Sets ROWS on success. A non-zero exit is logged and the caller sleeps 5 minutes — never
# treated as "empty column" (board.py exits 1 for an unknown column precisely so a poll
# failure can never be read as nothing to do). stderr is captured SEPARATELY from ROWS
# (review, card #65 fold-in) — `2>&1` inside the command substitution used to merge any
# stray stderr line (a warning, a traceback fragment) straight into the TSV that pick_card
# parses, silently corrupting a row.
poll_board() {
  local errfile
  errfile="$(mktemp)"
  if ROWS="$($WATCHER_BOARD list "In progress" 2>"$errfile")"; then
    BOARD_ERR="$(cat "$errfile" 2>/dev/null)"
    rm -f "$errfile"
    # The Waiting stage: cards Steve dragged there on the Stage-grouped view. Best-effort —
    # a failure here must not stop In progress from being served, so it only logs.
    QUEUE_ROWS="$($WATCHER_BOARD queue 2>/dev/null)" || { QUEUE_ROWS=""; log "board.py queue failed; serving In progress only"; }
    return 0
  else
    BOARD_ERR="$(cat "$errfile" 2>/dev/null)"
    rm -f "$errfile"
    ROWS=""
    return 1
  fi
}

# board_show_retry <n> — `board.py show <n>` with two retries on failure (a short wsleep
# between attempts), so a single flaky call at exit-handling time cannot silently read as
# "card has no in-flight, not in progress" (review, card #65 fold-in: fail-open there means
# the sweeper never runs). Prints the JSON on success; returns 1 with nothing useful on
# stdout if all three attempts fail — the caller must treat that as "unknown", not "{}"'s
# benign-looking defaults. stderr is captured SEPARATELY from the JSON (review round 2,
# fold-in 3 — poll_board fixed this same bug at the top of this file; a `show` that exits 0
# but also writes a stray warning to stderr used to land inside `out` via `2>&1`, so every
# `json.loads` downstream failed and the caller printed "no" silently — fail-open past the
# very guard this function exists for).
board_show_retry() {
  local n="$1" attempt=1 out errfile
  while [ "$attempt" -le 3 ]; do
    errfile="$(mktemp)"
    if out="$($WATCHER_BOARD show "$n" 2>"$errfile")"; then
      rm -f "$errfile"
      printf '%s' "$out"
      return 0
    fi
    out="$(cat "$errfile" 2>/dev/null)"
    rm -f "$errfile"
    [ "$attempt" -lt 3 ] && wsleep 5
    attempt=$((attempt + 1))
  done
  printf '%s' "$out"
  return 1
}

# lane_takes <agent> — is a card with this `agent:` label (board.py prints `-` for none)
# this instance's to pick? Unset LANE_AGENTS takes everything. A list takes exactly those
# names; a `!`-prefixed list takes everything else, so one lane can be "the rest" and a
# label nobody listed (a new agent, no label at all) still lands somewhere instead of
# nowhere. Two lanes of one project must be complements of each other: this filter is
# what keeps them from ever wanting the same card, and nothing on the board enforces it.
lane_takes() {
  local agent="${1:--}" spec="$LANE_AGENTS" listed=0 name
  [ -n "$spec" ] || return 0
  local negate=0
  case "$spec" in '!'*) negate=1; spec="${spec#!}" ;; esac
  local IFS=','
  for name in $spec; do
    name="${name// /}"
    [ -n "$name" ] || continue
    if [ "$name" = "$agent" ]; then listed=1; break; fi
  done
  [ "$listed" != "$negate" ]
}

# open_blockers <n> — the numbers of the OPEN issues GitHub says #n is blocked by (the
# issue's "Relationships" field; /triage records them from a card's own words), space-
# separated, on stdout. Fails closed: if the lookup itself fails, prints nothing and
# returns 1, and the caller skips the card for this poll rather than launching a card
# that may be waiting on another — a transient outage costs one minute, a wrong launch
# costs a tick. No lookup at all (returns 0, prints nothing) when PROJECT_REPO is unknown.
open_blockers() {
  local n="$1" out
  [ -n "${PROJECT_REPO:-}" ] || return 0
  out="$($WATCHER_GH api "repos/$PROJECT_REPO/issues/$n/dependencies/blocked_by" 2>/dev/null)" || return 1
  python3 -c '
import json, sys
try:
    items = json.loads(sys.argv[1] or "[]")
except Exception:
    sys.exit(1)
print(" ".join(str(i["number"]) for i in items if isinstance(i, dict) and i.get("state") != "closed" and "number" in i))
' "$out" || return 1
}

# note_waiting <n> <blockers> — the one FYI comment per card saying what it waits on. The
# marker under $WATCHER_STATE/waiting/<n> holds the blocker list it was posted for, so a
# card whose blockers change gets one more comment, and a card seen waiting on the same
# ones poll after poll gets none. Written only when the comment lands. Never in --dry-run.
note_waiting() {
  local n="$1" blockers="$2" links b marker
  marker="$WATCHER_STATE/waiting/$n"
  [ "${DRY_RUN:-0}" != 1 ] || return 0
  if [ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null)" = "$blockers" ]; then return 0; fi
  mkdir -p "$WATCHER_STATE/waiting"
  links=""
  for b in $blockers; do links="${links:+$links, }[#$b](https://github.com/$PROJECT_REPO/issues/$b)"; done
  if $WATCHER_GH issue comment "$n" --body "🤖 FYI: this card waits on $links, so I am not starting it yet.
- I start it on my own once every card it waits on is closed.
- Nothing for Steve to do, unless one of those cards is not on its way."; then
    printf '%s' "$blockers" > "$marker"
  else
    log "waiting comment failed for #$n; retried on a later poll"
  fi
}

# --- section 1, step 3: the pick ---------------------------------------------------------
# First row whose flags do not include in-flight, whose attempts/<n> is below 2, and that
# GitHub does not say is blocked by an open issue. Rows are already P0-first (board.py).
# blocked/steve-decision are NOT filtered here — the tick's own contract parks those in
# Stuck on its first turn (one source of truth for routing lives in watch.md, not here).
pick_card() {
  ISSUE=""; PRIORITY=""; AGENT=""; TITLE=""
  local r_issue r_priority r_agent r_flags r_title attempts attn parked_ok parked_tried
  while IFS=$'\t' read -r r_issue r_priority r_agent r_flags r_stage r_title; do
    [ -z "$r_issue" ] && continue
    # Five-column rows (an older board.py, or the tests' fixtures) have no stage column.
    if [ -z "$r_title" ]; then r_title="$r_stage"; r_stage=""; fi
    case ",$r_flags," in
      *,in-flight,*) log "#$r_issue skipped: in-flight"; continue ;;
    esac
    # Another lane's card. Nothing below this line — the parked-ok reconciliation, the
    # attempts safety net, the pick itself — is this instance's business for it.
    if ! lane_takes "$r_agent"; then log "#$r_issue skipped: agent '$r_agent' is not in this lane ($LANE_AGENTS)"; continue; fi
    attn="$WATCHER_STATE/attempts/$r_issue"
    attempts=0
    [ -f "$attn" ] && attempts="$(cat "$attn" 2>/dev/null || echo 0)"
    case "$attempts" in ''|*[!0-9]*) attempts=0 ;; esac

    # Round 5, N1: this row came from `board.py list "In progress"` — the card is In
    # progress RIGHT NOW. If parked-ok/<r_issue> also exists (park_stuck's own move for
    # this card actually succeeded, not merely attempted), the only way both can be true
    # is Steve moved it back himself: a genuinely-parked card sits in Stuck - Needs help,
    # never In progress, and nothing else clears this marker. Checking parked-ok/<n>
    # specifically (not parked/<n>, which park_stuck also writes when the move FAILS) —
    # a card whose move simply failed was never actually elsewhere, so it reappearing in
    # this same list poll after poll is not a retry signal, it's the original failure
    # persisting; treating that as "Steve retried it" would relaunch a card whose
    # board.py move is simply broken on the very next poll. Before this branch existed at
    # all, a genuinely parked-then-retried card stayed parked forever: attempts/<n> was
    # never touched by a manual board move, so it sat at >= 2 and pick_card's safety net
    # just kept trying to re-park it (park_stuck's own marker makes that a no-op now, but
    # without this the card would still never be picked again). Treat it as a fresh start:
    # clear all three markers and let it fall through to the pick below. Skipped in
    # --dry-run — same "claim/write nothing" contract as the park branch below — but still
    # reported as pickable so the preview reflects what the next real poll would do.
    parked_ok="$WATCHER_STATE/parked-ok/$r_issue"
    parked_tried="$WATCHER_STATE/parked/$r_issue"
    if [ -f "$parked_ok" ]; then
      if [ "${DRY_RUN:-0}" != 1 ]; then
        rm -f "$parked_ok" "$parked_tried" "$attn"
        log "#$r_issue: In progress with a confirmed parked-ok marker on file — Steve moved it back; attempts cleared, picking normally"
      else
        # Cosmetic (round 6): --dry-run's whole contract is "claim/write nothing" — see
        # test 1 — so nothing was actually cleared here; say so, don't claim it was.
        log "#$r_issue: In progress with a confirmed parked-ok marker on file — Steve moved it back; attempts would clear, picking normally"
      fi
      attempts=0
    fi

    if [ "$attempts" -ge 2 ]; then
      # Safety net, round 4 B2: every OTHER bump site (prepare_worktree, handle_exit's own
      # is_in_progress branch, reconcile_stale_current's unclaim path) already parks in the
      # same call that crosses the threshold — but board.py list is the ground truth, and a
      # card can still turn up here at attempts >= 2 if one of those park_stuck calls itself
      # failed (logged, never retried) or raced with a board update. Never just skip-and-
      # log a card in that state again: park it here too, so "still In progress with
      # attempts >= 2" never reads as silently skipped forever with nothing on the board.
      # Never in --dry-run, though (do_dry_run also calls pick_card): dry-run's whole
      # contract is "launch/write/claim nothing" — see test 1 — so a park here would be a
      # board mutation dry-run is explicitly not supposed to make.
      log "#$r_issue skipped: $attempts attempts, parking in Stuck"
      if [ "${DRY_RUN:-0}" != 1 ]; then
        park_stuck "$r_issue" "The card was still In progress with $attempts attempts on file (an earlier move to Stuck may have failed)."
      fi
      continue
    fi

    # Dependencies: a card blocked by an open issue waits, whatever its priority — the
    # blocker is the card to work first, and if it is on this same board it is picked by
    # this same loop a few rows down. Checked after the attempts safety net so a card
    # that already failed twice is still parked, blocked or not.
    local blockers
    if ! blockers="$(open_blockers "$r_issue")"; then
      log "#$r_issue skipped: could not read what it is blocked by (gh api failed); trying again next poll"
      continue
    fi
    if [ -n "$blockers" ]; then
      log "#$r_issue skipped: waiting on open issue(s) $blockers"
      note_waiting "$r_issue" "$blockers"
      continue
    fi
    [ "${DRY_RUN:-0}" = 1 ] || rm -f "$WATCHER_STATE/waiting/$r_issue"
    ISSUE="$r_issue"; PRIORITY="$r_priority"; AGENT="$r_agent"; TITLE="$r_title"
    return 0
  done <<< "${1-$ROWS}"
  return 1
}

# bump_attempts <n> — increments attempts/<n> (0 if absent/garbage) and prints the new
# count. Shared by handle_exit and prepare_worktree so a launch that never even reaches a
# claude invocation still counts toward the two-attempt Stuck rule (review, card #65
# fold-in: a permanently failing worktree create must not retry every 10 minutes forever).
bump_attempts() {
  local n="$1" attn attempts
  attn="$WATCHER_STATE/attempts/$n"
  attempts=0
  [ -f "$attn" ] && attempts="$(cat "$attn" 2>/dev/null || echo 0)"
  case "$attempts" in ''|*[!0-9]*) attempts=0 ;; esac
  attempts=$((attempts + 1))
  echo "$attempts" > "$attn"
  echo "$attempts"
}

# park_stuck <n> <detail> — the two-attempt Stuck move + comment, shared by handle_exit,
# prepare_worktree, reconcile_stale_current, and pick_card's own safety-net branch.
#
# Round 5, N1: two markers, not one, under $WATCHER_STATE/parked/<n> and
# $WATCHER_STATE/parked-ok/<n>, because they answer two different questions:
#
#   parked-ok/<n>  — "the move actually landed": written ONLY when `board.py move`
#                     reports success. This is what pick_card's own reconciliation (above)
#                     treats as "Steve must have moved it back" — board.py list only ever
#                     shows a card that's genuinely In progress right now, and the ONLY way
#                     a card that was genuinely moved to Stuck shows up there again is a
#                     human moving it back. A card whose move simply failed was NEVER
#                     actually elsewhere, so its reappearing in the same "In progress" list
#                     poll after poll is not a retry signal at all.
#   parked/<n>     — "we already posted the one Stuck comment for this card, don't post a
#                     second one": guards ONLY the gh issue edit/comment below, never the
#                     move. pick_card's own safety net (above) calls this function again on
#                     EVERY poll for as long as the card still shows attempts >= 2, and
#                     board.py list is the ground truth it reads that from, not something
#                     this function controls — so at most one comment per card, no matter
#                     how many polls see it stuck (reproduced before this marker: 5 polls, 5
#                     "has had 2 attempts" comments, for one underlying `board.py move`
#                     failure that never got a second try anyway).
#
# Round 6, B1: parked/<n> used to be written UNCONDITIONALLY, before the move even ran, and
# guarded the move itself as well as the comment — so a single transient `board.py move`
# failure wrote the marker, the move was never attempted again on ANY later poll (the guard
# at the top of this function returned before reaching the move on every subsequent call),
# and the card sat In progress forever while the now-permanently-stale comment claimed it
# had already been parked in Stuck. Recovery needed two files deleted by hand. Fixed: the
# move is retried on every poll for as long as parked-ok/<n> is absent; only the
# comment/label-removal are guarded by parked/<n>, and that marker is written after the
# comment goes out (never before the move is even attempted).
park_stuck() {
  local n="$1" detail="$2"
  local tried="$WATCHER_STATE/parked/$n" ok="$WATCHER_STATE/parked-ok/$n"
  mkdir -p "$WATCHER_STATE/parked" "$WATCHER_STATE/parked-ok"

  if [ ! -f "$ok" ]; then
    # `--why` is the ask in the Discord alert board.py posts for a move to Stuck (the
    # spawner's own comment goes out after the move, so board.py cannot read it there).
    # Plain words, no paths: the log path lives under the comment's fold, not here.
    if $WATCHER_BOARD move "$n" "Stuck - Needs help" --why "This card failed 2 attempts, so I parked it. Details are in the comment on the card."; then
      : > "$ok"
    else
      log "move to Stuck failed for #$n; will keep retrying on later polls"
    fi
  fi

  if [ -f "$tried" ]; then
    log "#$n already has a Stuck comment on file; not re-commenting (the move above is retried independently of this marker)"
    return 0
  fi

  $WATCHER_GH issue edit "$n" --remove-label in-flight || true
  local retry_note=""
  [ -f "$ok" ] || retry_note=$'\n- The move to Stuck did not go through yet; I will keep retrying it.'
  # The marker means "the one Stuck comment was POSTED" (review round 7): it is written only
  # when `gh issue comment` succeeds, so a failed comment is retried on the next poll — a
  # retry there costs nothing, and a parked card with no explanation on it is what the
  # marker exists to prevent, not to cause.
  #
  # The body is for Steve (docs/talking-to-steve.md): the ask on line one, bullets, one
  # question, lettered options with the recommendation first, and how to answer. The
  # spawner only knows the card failed twice, not why, so the options are the three moves
  # Steve can make; $detail and the log path are the engineer's crumbs, kept short.
  local body
  body="🤖 **Needs Steve: this card failed 2 attempts, so I parked it.**

- Moved to **Stuck - Needs help**. The bots will not retry on their own.
- Why: $detail${retry_note}

**Question: what should happen to this card?**
- **A. Try again** (recommended if the failure looks like bad luck) — move it back to **In progress**.
- **B. Fix the card first** — make it smaller or clearer, then move it back to **In progress**.
- **C. Drop it** — close the card.

Reply A, B or C on this card, then move it.

<details><summary>Details</summary>

- Logs: $WATCHER_LOGS
- Attempts on file: 2

</details>"
  $WATCHER_BOARD stage "$n" Parked >/dev/null 2>&1 || true
  if $WATCHER_GH issue comment "$n" --body "$body"; then
    : > "$tried"
  else
    log "stuck comment failed for #$n; retried on a later poll only while the card is still seen in In progress (once the move lands, nothing re-enters park_stuck)"
  fi
}

# The exact claude command from section 1 step 5, flags verbatim — shared by the real
# launch and --dry-run so they can never drift apart.
#
# No `--no-session-persistence`: the tick's full transcript (every tool call, every file it
# read, every picture it looked at) is kept under the tick user's `~/.claude/projects/<cwd>/
# <session>.jsonl`, keyed by the worktree path. `$WATCHER_LOGS/card-<n>-<ts>.json` holds only
# the result block, which is not enough for a post-mortem — riffmap #359 (September 16,
# 2026) shipped a wrong fix and nobody could tell afterwards whether the tick had opened the
# card's screenshot. Disk is cheap; `claude`'s own cleanup (cleanupPeriodDays) ages them out.
print_claude_cmd() {
  local n="$1" dir="$2" ts="$3"
  cat <<EOF
(cd "$dir" && $WATCHER_CLAUDE -p "$LANE_COMMAND $n" \\
  --dangerously-skip-permissions \\
  --max-turns $TICK_MAX_TURNS --max-budget-usd $TICK_MAX_BUDGET_USD \\
  --output-format json \\
  > "$WATCHER_LOGS/card-$n-$ts.json" 2> "$WATCHER_LOGS/card-$n-$ts.stderr")
EOF
}

do_dry_run() {
  if ! poll_board; then
    echo "dry-run: board.py failed: $BOARD_ERR" >&2
    exit 1
  fi
  if ! pick_card "$ROWS" && ! { [ -n "${QUEUE_ROWS:-}" ] && pick_card "$QUEUE_ROWS"; }; then
    echo "dry-run: nothing claimable in In progress or Waiting"
    exit 0
  fi
  local dir="$WORKTREE_BASE/card-$ISSUE" ts
  [ "$LANE_WORKDIR" = root ] && dir="$WATCHER_ROOT"
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  echo "lane: ${LANE_AGENTS:-every card (no LANE_AGENTS)}"
  echo "command: $LANE_COMMAND"
  echo "pick: #$ISSUE ($PRIORITY) $TITLE"
  if [ "$LANE_WORKDIR" = root ]; then echo "workdir: $dir (no worktree)"; else echo "worktree: $dir"; fi
  echo "claude command:"
  print_claude_cmd "$ISSUE" "$dir" "$ts"
}

# --- section 1, step 4: the worktree ------------------------------------------------------
prepare_worktree() {
  local n="$1"
  local dir="$WORKTREE_BASE/card-$n"
  if [ ! -d "$dir" ]; then
    if ! $WATCHER_WORKTREE new "card-$n"; then
      log "worktree create failed for #$n"
      local attempts
      attempts="$(bump_attempts "$n")"
      if [ "$attempts" -ge 2 ]; then
        park_stuck "$n" "Could not create the card's work folder (git worktree) twice in a row."
      fi
      return 1
    fi
  else
    log "reusing existing worktree for #$n at $dir"
  fi
  WT_DIR="$dir"
}

# --- section 1, steps 5 (launch) + 6 (watchdog) -------------------------------------------
launch_tick() {
  local n="$1" dir="$2" ts out err rc
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  out="$WATCHER_LOGS/card-$n-$ts.json"
  err="$WATCHER_LOGS/card-$n-$ts.stderr"

  echo "$n" > "$CURRENTFILE"
  log "launching tick for #$n in $dir -> $(basename "$out")"
  TICK_START_EPOCH="$(date +%s)"

  # No `( cd "$dir" && claude … ) &` wrapper. That form backgrounds the SUBSHELL, and
  # `claude` runs as ITS child (bash forks a second process for the external command in an
  # `&&` chain) — `kill -TERM` on the wrapper's pid never reaches `claude`, which then
  # survives as an orphan for up to TICK_TIMEOUT while `handle_exit` has already unclaimed
  # the card, so a second tick can be launched onto the same card while the first is still
  # running (found in review, card #65 B2 — the process-level lock the whole design rests
  # on). `cd` the SCRIPT's own cwd instead, launch `claude` directly so `tick_pid` IS its
  # pid, then `cd` back before doing anything else.
  #
  # `set -m` (job control) around the launch puts `claude` in ITS OWN process group
  # (pgid == tick_pid), rather than this script's — so the watchdog and the final cleanup
  # can `kill -TERM` the whole GROUP at once. That matters even with tick_pid no longer a
  # wrapper: `claude` itself forks children (Bash tool calls, subagents), and a *sequential*
  # "kill the parent, then pkill -P its children" loses the race the instant the parent has
  # already been reaped — the surviving child is reparented away from tick_pid before the
  # follow-up pkill runs, exactly the failure mode macOS has no `setsid` to solve directly
  # (found while testing this fix: a `sleep` standing in for a hung child survived a
  # `kill "$tick_pid"` + `pkill -P "$tick_pid"` pair every time). A single signal to the
  # process group reaches everyone in it simultaneously, so there is no window to lose.
  local was_monitor=0
  case "$-" in *m*) was_monitor=1 ;; esac
  set -m
  local launch_pwd="$PWD"
  cd "$dir"
  "$WATCHER_CLAUDE" -p "$LANE_COMMAND $n" \
      --dangerously-skip-permissions \
      --max-turns $TICK_MAX_TURNS --max-budget-usd $TICK_MAX_BUDGET_USD \
      --output-format json \
      > "$out" 2> "$err" &
  local tick_pid=$!
  # Deliberately NOT disowned: `wait "$tick_pid"` below is how this function learns the
  # tick's real exit status, and disowning it (tried during round-2 review, reverted after
  # testing) makes that same `wait` return immediately with a bogus success status while
  # the process is still actually running — silently breaking every rc-dependent decision
  # in handle_exit. Only watchdog_pid, whose `wait` result nothing depends on, is disowned
  # below to quiet its own "Terminated" job-control notice.
  cd "$launch_pwd"
  [ "$was_monitor" = 1 ] || set +m

  # SIGTERM watchdog: default 150 minutes (WATCHER_TICK_TIMEOUT_SEC overrides in seconds,
  # tests only). Makes `claude -p` exit 143, terminate its Bash children, and run
  # SessionEnd hooks (docs/durable-watcher.md section 1, step 6). `-$tick_pid` (leading
  # dash) signals the whole process group, not just the leader.
  #
  # `if sleep …; then …; fi`, not a bare `sleep …; kill …` (round 3, N2: this was still the
  # unchecked sequential form — the same bug an earlier probe watchdog already
  # carries the fix for, at "if sleep \"$probe_timeout\"; then …; fi"). `pkill -P
  # "$watchdog_pid"` below runs on EVERY ordinary tick, timeout or not, to cancel this
  # subshell's sleep the instant the real tick exits. A bare `sleep …; kill …` does not
  # check sleep's exit status, so that ordinary cancellation (`sleep` killed, non-zero
  # exit) fell through to the SAME "signal the group" line as a genuine timeout (`sleep`
  # completing on its own, exit 0) — so the group-kill ran unconditionally after every
  # single tick, timing out or not, harmless only because the group is normally already
  # empty by the time it runs. Gating on `sleep`'s own exit status means the kill only
  # fires when `sleep` really did run to completion.
  local timeout_secs="${WATCHER_TICK_TIMEOUT_SEC:-$(( TICK_TIMEOUT * 60 ))}"
  ( if sleep "$timeout_secs"; then kill -TERM -- "-$tick_pid" 2>/dev/null || true; fi ) &
  local watchdog_pid=$!
  disown "$watchdog_pid" 2>/dev/null || true

  rc=0
  wait "$tick_pid" || rc=$?
  # Cancel the watchdog. `kill "$watchdog_pid"` alone signals only the `(...)` subshell
  # wrapper, not the `sleep` it forked as ITS child — bash does not forward SIGTERM to a
  # foreground grandchild, so the sleep survives as an orphan for up to TICK_TIMEOUT
  # minutes (found while testing this script). pkill -P kills the subshell's children
  # first, by parent pid, then the subshell itself.
  pkill -P "$watchdog_pid" 2>/dev/null || true
  kill "$watchdog_pid" 2>/dev/null || true
  wait "$watchdog_pid" 2>/dev/null || true

  # Belt and suspenders for the fix above: the whole process group again, now that we
  # know for certain the main wait has returned — anything the tick left running behind
  # it (a killed tick's own Bash subprocesses) dies with it, not as an orphan.
  kill -TERM -- "-$tick_pid" 2>/dev/null || true

  RESULT_JSON="$out"
  RESULT_RC="$rc"
}

# --- section 1, step 7: exit handling -----------------------------------------------------
json_field() {
  # json_field <file> <python-expr-using-d> — one python3 invocation per field, tolerant
  # of a missing/unparseable/partial result file (a killed tick may not have flushed one).
  python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
print(eval(sys.argv[2]))
' "$1" "$2" 2>/dev/null || true
}

# The card's OPEN pull request, if the tick opened one: "<number> <url>", or nothing. Found
# by the closing keyword in the body (`Closes #<n>` is what the merge acts on) rather than
# by branch name — worktree.sh names the branch card-<n>, but a tick may rename it
# (riffmap#305 shipped from card-305-overview-window, September 14, 2026). Any failure of
# the lookup reads as "no PR", which falls back to the plainer sweeper wording below.
open_pr_for() {
  local n="$1" out
  out="$($WATCHER_GH pr list --state open --search "#$n in:body" --json number,url,body --limit 30 2>/dev/null || true)"
  [ -n "$out" ] || return 0
  python3 -c 'import json, re, sys
n = sys.argv[1]
try:
    prs = json.loads(sys.argv[2])
except Exception:
    sys.exit(0)
# Bare `Closes #31` and the linked form `Closes [#31](https://.../issues/31)` that the
# riffmap house rule mandates (always link board cards, never a bare #nnn) both count.
# (This is inside a single-quoted bash string: no apostrophes in here.)
pat = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b:? +\[?#%s\b" % re.escape(n), re.I)
for p in prs:
    if pat.search(p.get("body") or ""):
        print(p["number"], p["url"])
        break
' "$n" "$out" 2>/dev/null || true
}

handle_exit() {
  local n="$1" json="$2" rc="$3"
  local subtype total_cost num_turns denials elapsed_min show has_in_flight is_closed is_in_progress attn attempts pr

  subtype="$(json_field "$json" 'd.get("subtype","unknown")')"; subtype="${subtype:-unknown}"
  total_cost="$(json_field "$json" 'd.get("total_cost_usd","")')"
  num_turns="$(json_field "$json" 'd.get("num_turns","")')"
  denials="$(json_field "$json" '(len(d.get("permission_denials")) if isinstance(d.get("permission_denials"),list) else d.get("permission_denials",0)) or 0')"
  elapsed_min=$(( ( $(date +%s) - ${TICK_START_EPOCH:-$(date +%s)} ) / 60 ))

  log "tick #$n exited rc=$rc subtype=$subtype cost=\$${total_cost:-?} turns=${num_turns:-?} denials=${denials:-0}"

  # board_show_retry tries three times. If it STILL fails, this is not read as "no
  # in-flight, not in progress" (review, card #65 fold-in: that fail-open means the
  # sweeper never runs, and a leaked in-flight label strands the card forever). Loudly
  # skip every board-touching step below instead — leave $CURRENTFILE and attempts/$n
  # exactly as they are. This used to strand the card forever: pick_card skips anything
  # in-flight, so with no board-touching step here, nothing ever ran board_show_retry for
  # #$n again (review round 2, B2 — reproduced at 171 polls, zero unclaims). Since then
  # reconcile_stale_current runs at the top of EVERY poll, not just at process start, so
  # the very next poll retries board.py show for #$n on its own before picking a card —
  # unclaiming it there if its tick process is gone, leaving it alone if one is still
  # running.
  if ! show="$(board_show_retry "$n")"; then
    # Round 3, N1 bumped attempts/$n right here, on the theory that leaving $CURRENTFILE
    # untouched (below) meant nothing else would ever count this exit toward the two-attempt
    # budget. Round 4, B2: that was wrong twice over. First, it double-counts a single
    # board-outage: reconcile_stale_current's own unclaim path (below in this file) ALSO
    # bumps once it finally reconciles this same stale $CURRENTFILE on a later poll — one
    # underlying outage, two bumps, so a card can hit the Stuck threshold after exactly one
    # real failure. Second, neither bump site called park_stuck, so a card that DID reach
    # attempts >= 2 this way was skipped forever with no Stuck move and no comment — the
    # exact "attempts=2, no Stuck move, no comment, card skipped forever" bug (repro: a
    # single board.py show outage at bookkeeping time right after a SUCCESSFUL tick). Fixed
    # by bumping in exactly one place: reconcile_stale_current's unclaim path, since that is
    # the poll where the card is actually released back to pick_card — see the comment
    # there. Nothing to bump here; just log and leave $CURRENTFILE in place so the next
    # poll's reconcile_stale_current can retry board.py show for #$n before picking a card.
    log "handle_exit: board.py show #$n failed on every retry — cannot tell whether "\
"in-flight/in-progress/closed. NOT assuming the benign state: leaving $CURRENTFILE "\
"untouched (attempts/$n NOT bumped here — see reconcile_stale_current's unclaim path), "\
"skipping the sweeper/Stuck/worktree-cleanup steps for this exit. The next poll's "\
"reconcile_stale_current will retry board.py show for #$n before picking a card. "\
"Last output: $show"
    [ "$subtype" = "success" ]
    return
  fi

  has_in_flight="$(python3 -c 'import json,sys
try:
    d=json.loads(sys.argv[1]); print("yes" if "in-flight" in d.get("labels",[]) else "no")
except Exception:
    print("no")' "$show")"
  is_closed="$(python3 -c 'import json,sys
try:
    d=json.loads(sys.argv[1]); print("yes" if d.get("state")=="CLOSED" else "no")
except Exception:
    print("no")' "$show")"
  is_in_progress="$(python3 -c 'import json,sys
try:
    d=json.loads(sys.argv[1]); print("yes" if d.get("status")=="In progress" else "no")
except Exception:
    print("no")' "$show")"
  # The card's agent label now, as board.py's `list` would print it (`-` for none). A
  # tick that relabels a card for another lane — /triage always, /watch when it routes a
  # design card — leaves it In progress on purpose, and that is a success for THIS lane,
  # not a no-progress attempt: the label keeps this lane from ever picking it again, and
  # the other lane starts its own count.
  local agent_now left_lane=no
  agent_now="$(python3 -c 'import json,sys
try:
    d=json.loads(sys.argv[1]); found=[l.split(": ",1)[1] for l in d.get("labels",[]) if l.startswith("agent: ")]
    print(found[0] if len(found)==1 else "-")
except Exception:
    print("-")' "$show")"
  if [ "$is_in_progress" = yes ] && ! lane_takes "$agent_now"; then left_lane=yes; fi

  # Only ever sweeps the card THIS run holds — never a claim it did not make.
  if [ "$has_in_flight" = yes ] && [ "$(cat "$CURRENTFILE" 2>/dev/null || true)" = "$n" ]; then
    $WATCHER_BOARD unclaim "$n" || log "unclaim failed for #$n"
    # Two exits look identical from here — the tick's process is gone, the card still
    # in-flight — and Steve reads them very differently. A run that DIED holding the card is
    # the failure the second wording below was written for. A run that opened the card's PR
    # and then ended its turn before merging has done the work: riffmap#305, September 14,
    # 2026, ended with "waiting on CI — I'll be notified when the background check
    # finishes", a notification a headless run never gets, and the next tick found the PR
    # green and merged it in 18 seconds. The release and the retry are the same either way
    # (the card is In progress again, so pick_card takes it back); only the words differ.
    pr="$(open_pr_for "$n")"
    if [ -n "$pr" ]; then
      # FYI-shaped (docs/talking-to-steve.md): nothing is asked of Steve; the PR is the
      # thing to look at, so it is the link, and no log path sits above the fold.
      $WATCHER_GH issue comment "$n" --body "🤖 FYI: PR #${pr%% *} is up for this card, but the run ended before merging it. I released the card; the next poll picks it up and merges once the checks are green.
- PR: ${pr#* }
- Exit: ${rc}/${subtype} after ${elapsed_min} min
- Agent cost: \$${total_cost:-0}" \
        || log "sweeper comment failed for #$n"
      $WATCHER_BOARD notify "$n" released "PR #${pr%% *} is up, but the run ended after ${elapsed_min} min before merging it. I released the card; the next poll merges it once the checks are green." \
        || log "discord notify failed for #$n"
    else
      # FYI-shaped (docs/talking-to-steve.md): nothing is asked of Steve here — the card is
      # released and the next poll may retry it; two of these in a row become a park.
      $WATCHER_GH issue comment "$n" --body "🤖 FYI: the run ended without finishing this card. I released it; the next poll may retry once.
- Exit: ${rc}/${subtype} after ${elapsed_min} min
- ${denials:-0} permission denials
- Agent cost: \$${total_cost:-0}
- Log: $(basename "$json")" \
        || log "sweeper comment failed for #$n"
      # Discord, if configured (board.py notify is a no-op otherwise). A retry is not an
      # alert — the park, if it comes, is — but a died run is rare enough to be worth a line.
      $WATCHER_BOARD notify "$n" released "The run ended after ${elapsed_min} min without finishing (${subtype}). I released the card; the next poll may retry once." \
        || log "discord notify failed for #$n"
    fi
  fi

  attn="$WATCHER_STATE/attempts/$n"
  if [ "$subtype" = "success" ] && { [ "$is_in_progress" != yes ] || [ "$left_lane" = yes ]; }; then
    # The tick succeeded and moved the card out of In progress (Done, In review, Stuck by
    # its own contract) — reset rather than increment, so a card that comes back to
    # In progress later (a fresh pass, a re-open) starts its two-attempt budget over
    # instead of inheriting a count from a previous, unrelated success (review, card #65
    # fold-in).
    rm -f "$attn"
    if [ "$left_lane" = yes ]; then
      log "tick #$n succeeded and relabeled the card for another lane (agent '$agent_now'): attempts/$n reset"
    else
      log "tick #$n succeeded and left In progress: attempts/$n reset"
    fi
  else
    attempts="$(bump_attempts "$n")"
    if [ "$attempts" -ge 2 ] && [ "$is_in_progress" = yes ]; then
      park_stuck "$n" "The run ended with the card still In progress (log: $(basename "$json"))."
    fi
  fi

  if [ "$is_closed" = yes ]; then
    # Done is the one column nothing moves a card into through board.py — the merge's
    # `Closes #n` does it — so the "shipped" post has to come from here. board.py checks
    # the card's PR and posts "closed without a merge" instead when there is none.
    $WATCHER_BOARD notify "$n" shipped || log "discord notify failed for #$n"
    if [ "$LANE_WORKDIR" = root ]; then
      log "no worktree to remove for #$n (LANE_WORKDIR=root)"
    elif $WATCHER_WORKTREE remove "card-$n"; then
      log "worktree removed for #$n (issue closed)"
    else
      log "worktree remove refused for #$n — leaving it (unshipped work?)"
    fi
  fi

  rm -f "$CURRENTFILE"

  [ "$subtype" = "success" ]
}

# --- stale-claim reconciliation ---------------------------------------------------------
# If $CURRENTFILE names a card from a run that crashed or exited through handle_exit's
# loud-fail branch (board.py show unreachable at exit time — see handle_exit above), and
# no tick process for that card is still alive, the card may be stuck holding `in-flight`
# with nobody coming back to release it. Log it, unclaim if still in-flight, and remove the
# stale file either way — but only ever touch a claim this reconciliation can actually
# verify is abandoned (review, card #65 fold-in).
#
# Runs at the top of EVERY poll (review round 2, B2), not just once when the process
# starts: systemd's Restart=always means this daemon does not exit and restart on its own, so a
# card stranded by handle_exit's loud-fail path (above) was never revisited by anything —
# pick_card skips it forever because it is still in-flight, and the one-shot call this used
# to be (before the daemon's `while true` loop, and again for --once) never runs a second
# time. Reproduced: a board.py show outage at exit time left a card in-flight with 171
# subsequent polls and zero unclaims. Calling this every poll instead means the very next
# poll after the outage clears retries board.py show for the stranded card on its own.
# Idempotent and cheap when $CURRENTFILE doesn't exist, so calling it unconditionally here
# costs nothing on the common case.
reconcile_stale_current() {
  [ -f "$CURRENTFILE" ] || return 0
  local n
  n="$(cat "$CURRENTFILE" 2>/dev/null)"
  if [ -z "$n" ]; then
    log "reconcile: $CURRENTFILE exists but is empty; removing"
    rm -f "$CURRENTFILE"
    return 0
  fi
  log "reconcile: found stale $CURRENTFILE, card #$n"
  if pgrep -f "${LANE_COMMAND#/} $n --dangerously-skip-permissions" >/dev/null 2>&1; then
    log "reconcile: a tick process for #$n still appears to be running; leaving it and the board alone"
    return 0
  fi
  local show has_in_flight
  if ! show="$(board_show_retry "$n")"; then
    log "reconcile: board.py show #$n failed on every retry; leaving $CURRENTFILE in place so a later poll can retry"
    return 0
  fi
  has_in_flight="$(python3 -c 'import json,sys
try:
    d=json.loads(sys.argv[1]); print("yes" if "in-flight" in d.get("labels",[]) else "no")
except Exception:
    print("no")' "$show")"
  if [ "$has_in_flight" = yes ]; then
    # Round 3, N1: bump here too. This is the other place a card can shed its in-flight
    # label without ever going through handle_exit's normal attempts bookkeeping (a crashed
    # tick, or a board outage at the previous exit followed by this reconciliation's own
    # retry succeeding) — without this, pick_card can relaunch #$n every poll forever with
    # nothing counting toward the two-attempt Stuck rule.
    #
    # Round 4, B2: this is now the ONLY place that bumps for a board-outage exit —
    # handle_exit's own loud-fail branch (above in this file) used to bump too, double-
    # counting the same outage across the exit and this reconciliation and reaching the
    # Stuck threshold after a single real failure. This is also the release point (the card
    # actually sheds in-flight here), so it is the right place to check the threshold and
    # park, the same way prepare_worktree and handle_exit's own bump-then-park do at their
    # own bump sites — reproduced bug: a board.py show outage right after a SUCCESSFUL tick
    # reached attempts=2 with no Stuck move and no comment, so the card was skipped forever
    # with nothing on the board explaining why.
    local attempts
    attempts="$(bump_attempts "$n")"
    $WATCHER_BOARD unclaim "$n" || log "reconcile: unclaim failed for #$n"
    $WATCHER_GH issue comment "$n" --body "🤖 FYI: a run died while holding this card (still marked in-flight, no process running). I released it; the next poll may retry." \
      || log "reconcile: unclaim comment failed for #$n"
    $WATCHER_BOARD notify "$n" released "A run died while holding this card; found on cleanup. I released it; the next poll may retry." \
      || log "reconcile: discord notify failed for #$n"
    if [ "$attempts" -ge 2 ]; then
      park_stuck "$n" "A run died holding this card (a crashed tick, or a board outage at its exit); found on cleanup, attempts=$attempts."
    fi
  else
    log "reconcile: #$n was not in-flight; nothing to unclaim"
  fi
  rm -f "$CURRENTFILE"
}

# --- one poll iteration --------------------------------------------------------------------
tick_once() {
  # Every poll, before anything else — see reconcile_stale_current's own comment for why
  # this can no longer be a one-shot call made only at process start.
  reconcile_stale_current

  if [ -f "$PAUSEFILE" ]; then
    log "paused ($PAUSEFILE present); idling"
    return 0
  fi

  if ! poll_board; then
    log "board.py failed, sleeping 5 min: $BOARD_ERR"
    wsleep 300
    return 2
  fi

  QUEUED=0
  if ! pick_card "$ROWS"; then
    if [ -n "${QUEUE_ROWS:-}" ] && pick_card "$QUEUE_ROWS"; then
      QUEUED=1
    else
      log "nothing claimable in In progress or Waiting"
    fi
  fi
  if [ -z "$ISSUE" ]; then
    log "nothing claimable in In progress"
    return 0
  fi

  log "picked #$ISSUE ($PRIORITY) $TITLE"
  if [ "$QUEUED" = 1 ]; then
    # Status stays truthful: a card the watcher works is In progress, whatever column it
    # was dragged from. If the move fails, don't launch — the tick's claim would then
    # sit on a card the board says nobody is working.
    if $WATCHER_BOARD move "$ISSUE" "In progress"; then
      log "#$ISSUE taken from Waiting → In progress"
    else
      log "#$ISSUE: could not move from Waiting to In progress; not launching"
      return 1
    fi
  fi

  if [ "$LANE_WORKDIR" = root ]; then
    # This lane's tick only touches the board (a triage): no worktree, run it in the
    # project's main checkout. Nothing there is written by such a tick.
    WT_DIR="$WATCHER_ROOT"
  elif ! prepare_worktree "$ISSUE"; then
    log "aborting this poll for #$ISSUE: worktree not ready"
    return 1
  fi

  launch_tick "$ISSUE" "$WT_DIR"
  if handle_exit "$ISSUE" "$RESULT_JSON" "$RESULT_RC"; then
    return 0
  else
    return 1
  fi
}

run_daemon() {
  check_singleton
  trap 'rm -f "$PIDFILE"' EXIT
  mkdir -p "$WATCHER_STATE/attempts" "$WATCHER_STATE/parked" "$WATCHER_STATE/parked-ok" "$WATCHER_STATE/waiting" "$WATCHER_LOGS"
  # No standalone reconcile_stale_current call here: tick_once runs it at the top of every
  # iteration (including this loop's first), so a separate call before the loop would only
  # duplicate the very first poll's reconciliation.
  log "lane: ${LANE_AGENTS:-every card (no LANE_AGENTS)}; state $WATCHER_STATE"
  local polls=0
  while true; do
    local rc=0
    tick_once || rc=$?
    case "$rc" in
      0) wsleep "$POLL_INTERVAL" ;;
      2) : ;;                       # tick_once already slept 5 min for the board failure
      *) wsleep 600 ;;              # step 8: 10 minutes after any non-success exit
    esac
    if [ -n "${WATCHER_MAX_POLLS:-}" ]; then
      polls=$((polls + 1))
      [ "$polls" -ge "$WATCHER_MAX_POLLS" ] && break
    fi
  done
}

main() {
  parse_args "$@"
  mkdir -p "$WATCHER_STATE/attempts" "$WATCHER_STATE/parked" "$WATCHER_STATE/parked-ok" "$WATCHER_STATE/waiting" "$WATCHER_LOGS"

  if [ "$DRY_RUN" = 1 ]; then
    do_dry_run
    return
  fi

  if [ "$ONCE" = 1 ]; then
    check_singleton
    trap 'rm -f "$PIDFILE"' EXIT
    # No standalone reconcile_stale_current call here either — tick_once's own top-of-poll
    # call covers this single iteration too.
    local rc=0
    tick_once || rc=$?
    exit "$rc"
  fi

  run_daemon
}

main "$@"
