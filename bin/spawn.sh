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
#   PROJECT_REPO     optional; owner/repo the cards live in, for every repo-scoped `gh`
#                    call here and in board.py (card #3). Defaults to this script's own
#                    WATCHER_ROOT's git remote "origin" — set it explicitly only when
#                    that checkout's own remote is not the right repo (a fork cloned
#                    under a different name, or no remote at all yet).
#   BOARD_OWNER      GitHub user/org that owns the Projects board   (board.py reads these)
#   BOARD_NUMBER     the board's number
#   OWNER_LOGIN      optional; the GitHub login every Stuck/park/awaiting-rebuild comment
#                    addresses (`Needs @<login>:`) — defaults to BOARD_OWNER when unset, so
#                    a fork of this repo pings its own owner with no config at all.
#   DISCORD_WEBHOOK_URL  optional; a per-project Discord channel for stuck and broken
#                    alerts. Usually set once for the box in /etc/watcher/env instead
#                    (see notify.py).
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
#   WORKTREE_COPY_DIRS optional; the project's gitignored private inputs, space-separated
#                    and relative to the checkout, copied into every card worktree
#                    (default: none — a project with such inputs, like riffmap's
#                    `docs/transcripts`, sets this itself). See bin/worktree.sh.
#   LANE_CHANNEL     optional; `canary` makes this lane run the box's canary checkout
#                    (default: `/opt/watcher-canary`, `WATCHER_CANARY_DIR` overrides) instead
#                    of the one every other lane shares, with its own `CLAUDE_CONFIG_DIR` so
#                    a personal or project `commands/`/`agents/` cannot win over it either.
#                    Unset or `stable` (the default) runs the checkout this script is in, as
#                    always. See deploy/autodeploy.sh, "the canary stage", and card #46.
#
# A card that GitHub says is blocked by an open issue (the issue's "Relationships" field,
# or what /triage recorded from the card's own words) is skipped by every lane until the
# blocker closes, so the owner can hand over a batch of cards in one go and the prerequisite
# is worked first. With nothing recorded there, the card's own body is read for an explicit
# "depends on #<m>" and honored the same way. One FYI comment per card says what it waits on.
#
# Every external command is overridable so this can be tested with stubs, never the network:
#   WATCHER_PROJECT    path to the project file                  (default: none — required
#                      unless WATCHER_ROOT is given directly)
#   WATCHER_ROOT       the project checkout                     (default: $PROJECT_ROOT)
#   WATCHER_CLAUDE     claude binary/command                    (default: claude)
#   WATCHER_GH         gh binary/command                        (default: gh)
#   WATCHER_DOCTOR     bin/doctor.sh invocation                 (default: <here>/doctor.sh)
#   WATCHER_BOARD      board.py invocation                      (default: python3 <here>/board.py)
#   WATCHER_WORKTREE   worktree.sh invocation                   (default: <here>/worktree.sh)
#   WATCHER_TAIL       tail.py invocation, `--path` mode only    (default: python3 <here>/tail.py)
#                      — resolves a dead tick's own session transcript so it can be kept
#                      beside the empty result log (card #143); see keep_transcript below.
#   TRANSCRIPT_MAX_BYTES  cap on a kept transcript's copy, in bytes (default: 50MB; tests only)
#   WATCHER_STATE      state dir                                (default: ~/.local/state/watcher/<name>)
#   WATCHER_LOGS       log dir                                  (default: <state>/logs)
#   WATCHER_SLEEP      when set, EVERY sleep uses this many seconds (tests: WATCHER_SLEEP=0);
#                      the watchdog's TICK_TIMEOUT is untouched by this, on purpose.
#   TICK_TIMEOUT       watchdog ceiling in MINUTES              (default: 150)
#   WATCHER_TICK_TIMEOUT_SEC  watchdog ceiling in SECONDS instead (tests only)
#   TICK_MAX_TURNS     --max-turns for the tick                 (default: 400)
#   TICK_MAX_BUDGET_USD  --max-budget-usd for the tick          (default: 15)
#                      A QUOTA ceiling, not a spending limit: the box runs on a
#                      subscription, so this dollar figure is never billed.
#   POLL_INTERVAL      daemon poll cadence in SECONDS           (default: 60)
#   READY_PULL         1 = also pull from the Ready column when nothing
#                      is claimable in In progress or Waiting   (default: 0, off)
#   WIP_MAX            with READY_PULL=1, the most cards that may sit In progress
#                      before this lane stops promoting          (default: 2)
#   WATCHER_MAX_POLLS  stop the daemon loop after this many polls (tests only)
#
# Exported to the tick (so `/watch` can read the watcher's own docs from any worktree):
#   WATCHER_HOME       this repo's root                          (default: parent of bin/)
#
# Every comment this script leaves on a card is read by the owner on their phone. The shape
# is fixed by docs/talking-to-the-owner.md: first line is the ask, bullets, one question,
# lettered options, how to answer. test-spawn.sh checks the park comment keeps that shape.
#
#   bin/spawn.sh              # the daemon loop (what systemd runs)
#   bin/spawn.sh --dry-run    # print the pick + the exact claude command, exit 0
#   bin/spawn.sh --once       # one poll iteration, then exit (tests)
set -euo pipefail

# Card #106: this script's own directory (bin/, as named on the command line — the systemd
# unit's ExecStart says /opt/watcher/bin/spawn.sh, a path through the box's pointer) is kept
# BOTH ways: $WATCHER_POINTER_BIN unresolved, re-read every poll to notice the pointer move
# (follow_pointer, below); $WATCHER_HERE resolved ONCE, right now, with the symlink followed
# (`cd -P`) — so WATCHER_HOME, and every tick this one long-running process ever launches,
# stay pinned to the exact release folder this process started in, for its whole life. A
# deploy that flips the pointer under a running lane changes nothing this process reads
# until it re-launches itself from the new pointer target, between ticks, never during one.
WATCHER_ARGV=("$@")
WATCHER_POINTER_BIN="$(dirname "${BASH_SOURCE[0]}")"
case "$WATCHER_POINTER_BIN" in /*) ;; *) WATCHER_POINTER_BIN="$PWD/$WATCHER_POINTER_BIN" ;; esac
WATCHER_HERE="$(cd -P "$WATCHER_POINTER_BIN" && pwd)"
export WATCHER_HOME="${WATCHER_HOME:-$(dirname "$WATCHER_HERE")}"
# Card #115: stand IN the release this process is pinned to, not wherever the caller (the
# systemd unit, or a test) happened to be. Without this, a deploy's sweep of the OLD
# release (once this process has followed the pointer onward, below) leaves it running
# with its cwd pointing at a directory that no longer exists — every call it makes uses an
# absolute path already, so nothing breaks, but `getcwd`/`chdir` fail on every shell this
# process spawns and the journal fills with noise that looks like a fault.
cd "$WATCHER_HOME"
if [ -n "${WATCHER_PROJECT:-}" ]; then
  # shellcheck disable=SC1090
  . "$WATCHER_PROJECT"
fi
# LANE_CHANNEL=canary (card #46): this lane runs the box's canary checkout, never the
# stable one every other lane shares — a bad prompt only reaches the board that opted in.
# The project file is always read from the stable checkout (its path is fixed by the
# systemd unit), so this is the one place a canary lane's code diverges: re-exec into the
# canary checkout's OWN spawn.sh, with its own docs (WATCHER_HOME) and its own Claude
# config dir (CLAUDE_CONFIG_DIR — commands/ and agents/ resolve from there, the same trick
# bin/eval.sh uses to test one checkout's prompts while the box runs another's). Same
# systemd unit, same pid (exec replaces the image, not the process), so nothing about the
# spawner's lock or the deployer's cgroup lookup changes. A stable lane (LANE_CHANNEL unset
# or "stable") falls straight through; a canary lane whose checkout is not there yet (a box
# that has not adopted this feature, or install.sh has not made it) falls through too,
# rather than failing to start.
WATCHER_CANARY_DIR="${WATCHER_CANARY_DIR:-/opt/watcher-canary}"
# Resolved the same way WATCHER_HERE itself was, above: a raw string compare against
# WATCHER_CANARY_DIR would miss a trailing slash or a symlink (a canary lane's own
# spawn.sh, re-launched with LANE_CHANNEL still set in its environment, comparing its real
# location against a path that names the same directory differently) and exec into itself
# forever. Never resolved when the directory does not exist — nothing to compare against,
# and the existence check right below falls through the same as always.
WATCHER_CANARY_RESOLVED="$(cd "$WATCHER_CANARY_DIR" 2>/dev/null && pwd)" || true
if [ "${LANE_CHANNEL:-stable}" = canary ] && [ "$WATCHER_HERE" != "${WATCHER_CANARY_RESOLVED:-$WATCHER_CANARY_DIR}/bin" ] \
   && [ -x "$WATCHER_CANARY_DIR/bin/spawn.sh" ]; then
  export CLAUDE_CONFIG_DIR="${WATCHER_CANARY_CLAUDE_CONFIG_DIR:-/home/watcher/.claude-canary}"
  unset WATCHER_HOME
  exec "$WATCHER_CANARY_DIR/bin/spawn.sh" "$@"
fi
WATCHER_ROOT="${WATCHER_ROOT:-${PROJECT_ROOT:-}}"
[ -n "$WATCHER_ROOT" ] || { echo "spawn.sh: set WATCHER_PROJECT=projects/<name>.env (or WATCHER_ROOT)" >&2; exit 64; }
PROJECT_NAME="${PROJECT_NAME:-$(basename "$WATCHER_ROOT")}"
export BOARD_OWNER="${BOARD_OWNER:-}" BOARD_NUMBER="${BOARD_NUMBER:-}"
# Card #154: who every Stuck/park/awaiting-rebuild comment addresses, so a fork of this
# repo pings its own owner from the first tick instead of the word "Steve". Defaults to
# BOARD_OWNER (the one login every project file already sets) when a project does not set
# its own; exported to the tick the same way BOARD_OWNER is, and to board.py/notify.py
# (both read it directly rather than being passed it as an argument). Left empty, never
# defaulted to the literal word "Steve", so this script's own comments (below) can still
# fall back to the old unadorned "Needs Steve:" rather than the nonsensical "Needs @Steve:".
export OWNER_LOGIN="${OWNER_LOGIN:-${BOARD_OWNER:-}}"
# "Needs @<login>" once one is known, else the old "Needs Steve" with no "@" at all.
NEEDS_WHO="${OWNER_LOGIN:+@$OWNER_LOGIN}"
NEEDS_WHO="${NEEDS_WHO:-Steve}"
# The ledger's caller column (card #114): this lane's own board.py calls (polling, the
# sweeps) are attributed to the lane by name. The tick itself gets a narrower one, set
# only on the one command line that launches it (print_claude_cmd) — never here, or every
# board.py call the tick makes would also read "lane:$PROJECT_NAME" and the ledger could
# never tell a lane's own poll from the card it is working.
export WATCHER_LEDGER_CALLER="lane:$PROJECT_NAME"
# Discord is board.py's job (notify.py), and it only hears about a card that is stuck or a
# run that broke: a `board move` to Stuck posts the alert by itself, from this script and
# from inside the tick alike, so the webhook only has to reach board.py's environment. A
# project file may set it; /etc/watcher/env usually does.
export DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}" DISCORD_MENTION="${DISCORD_MENTION:-}"
# The repo the cards live in. `gh` infers a repo from the cwd, and this script's cwd is
# /opt/watcher — a checkout of the WATCHER, not the project — so on the first night every
# unclaim and sweeper comment hit the wrong repo and three finished cards sat in-flight
# (watcher#3). GH_REPO pins every repo-scoped gh call here AND in board.py (a child).
PROJECT_REPO="${PROJECT_REPO:-$(git -C "$WATCHER_ROOT" remote get-url origin 2>/dev/null | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')}"
[ -n "$PROJECT_REPO" ] && export GH_REPO="$PROJECT_REPO"
WATCHER_CLAUDE="${WATCHER_CLAUDE:-claude}"
# What a tick's SHELL finds as `claude` is bin/shim/claude, not the binary: Claude Code strips
# CLAUDE_CODE_OAUTH_TOKEN from the environment of every Bash tool call, so a `claude` the tick
# ran from the shell (riffmap's RIFFMAP_BACKEND=claude|record pipeline, a probe, an eval) was
# logged out while the tick itself was not, and riffmap#602 parked on exactly that (watcher#82,
# October 8, 2026). The shim puts the token back and execs the real binary — resolved HERE,
# before the shim's directory goes on the tick's PATH, so the shim can never find itself. The
# tick is launched through that resolved path too; only its shell's `claude` is the shim.
WATCHER_SHIM_DIR="${WATCHER_SHIM_DIR:-$WATCHER_HERE/shim}"
# A name `command -v` cannot resolve is refused here, not handed to the shim: with the bare
# name as "the real binary" the shim would find itself on PATH and exec in a loop until the
# watchdog (found in review). Before the shim the same misconfiguration was an immediate
# 127 per tick; now it is one journal line and systemd's restart, until someone fixes PATH.
WATCHER_REAL_CLAUDE="$(command -v -- "$WATCHER_CLAUDE" 2>/dev/null || true)"
[ -n "$WATCHER_REAL_CLAUDE" ] || { echo "spawn.sh: claude not found: '$WATCHER_CLAUDE' is not an executable on PATH ($PATH); nothing can be launched" >&2; exit 69; }
WATCHER_GH="${WATCHER_GH:-gh}"
WATCHER_DOCTOR="${WATCHER_DOCTOR:-$WATCHER_HERE/doctor.sh}"
WATCHER_BOARD="${WATCHER_BOARD:-python3 $WATCHER_HERE/board.py}"
WATCHER_WORKTREE="${WATCHER_WORKTREE:-$WATCHER_HERE/worktree.sh}"
WATCHER_TAIL="${WATCHER_TAIL:-python3 $WATCHER_HERE/tail.py}"
# Card #143: capped so a huge transcript (a long-running tick, a tool call that read
# something large) is never copied in full — above this, the dest file names the source
# path instead. 50MB, same order of magnitude as a single `cleanupPeriodDays` session.
TRANSCRIPT_MAX_BYTES="${TRANSCRIPT_MAX_BYTES:-$((50 * 1024 * 1024))}"
export WORKTREE_ROOT="$WATCHER_ROOT"
# A headless tick never gets a background task's "finished" notification, so a CI wait
# it backgrounds just ends the run with the PR unmerged — watch.md forbids it, and ticks
# still did (riffmap#305, then riffmap#461 on September 21, 2026: "CI checks are running;
# I'll act once they complete"). This makes the CLI reject `run_in_background`, so
# `gh pr checks --watch` runs in the foreground and the same tick merges.
export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1
# Only when the project file set it: worktree.sh distinguishes unset (use its default)
# from empty (copy nothing), and an unconditional export would erase that difference.
# An `if`, not `x && export x`: under `set -e` that whole list fails when x is unset,
# which would exit the spawner for every project that never sets the variable.
if [ -n "${WORKTREE_COPY_DIRS+set}" ]; then export WORKTREE_COPY_DIRS; fi
WATCHER_STATE="${WATCHER_STATE:-$HOME/.local/state/watcher/$PROJECT_NAME}"
WATCHER_LOGS="${WATCHER_LOGS:-$WATCHER_STATE/logs}"
TICK_TIMEOUT="${TICK_TIMEOUT:-150}"
TICK_MAX_TURNS="${TICK_MAX_TURNS:-400}"
TICK_MAX_BUDGET_USD="${TICK_MAX_BUDGET_USD:-15}"
POLL_INTERVAL="${POLL_INTERVAL:-60}"
# Self-pull. Off by default: a project opts in from its env file, because pulling from
# Ready means the loop starts work nobody handed it, and that is a policy choice, not a
# default. With it on, Ready is the THIRD place a card can come from, tried only after
# In progress and Waiting are both dry (see do_poll) — so a card the owner moved by hand is
# always served first.
#
# WIP_MAX is a board-wide budget, not a per-lane one: it counts every open card in the
# In progress column (ROWS, already fetched — no extra API call), across all lanes,
# because that column IS the work in flight. A lane stops promoting when the column is
# full, whoever filled it.
#
# It is a SOFT cap, deliberately, for the same reason `in-flight` is a courtesy flag:
# three lanes poll on three clocks, and a read of the column followed by a promote is not
# a compare-and-set, so lanes that poll in the same second can each promote against the
# same count. The overshoot is bounded by the number of lanes (riffmap: 3) and self-
# corrects on the next poll. A hard cap would need a lock the board cannot give us, and
# the failure it would prevent — one extra card in flight — is cheaper than the lock.
READY_PULL="${READY_PULL:-0}"
WIP_MAX="${WIP_MAX:-2}"
case "$READY_PULL" in 0|1) ;; *) echo "spawn.sh: READY_PULL must be 0 or 1, not '$READY_PULL'" >&2; exit 64 ;; esac
case "$WIP_MAX" in ''|*[!0-9]*) echo "spawn.sh: WIP_MAX must be a number, not '$WIP_MAX'" >&2; exit 64 ;; esac
[ "$WIP_MAX" -ge 1 ] || { echo "spawn.sh: WIP_MAX must be at least 1" >&2; exit 64; }
# The In review sweep (sweep_in_review): how long a card may sit in In review with no open
# PR before it is parked, and how long with one before it is sent back to In progress.
INREVIEW_NO_PR_GRACE_SEC="${INREVIEW_NO_PR_GRACE_SEC:-600}"
INREVIEW_STALE_SEC="${INREVIEW_STALE_SEC:-7200}"
# The closed-card Stage sweep (sweep_closed_stages): the least time between two of them.
# Each one is a full board read, so it is not done every poll. Exported (card #114):
# board.py's own cmd_sweep_stages reads this same knob for its board-level stamp, so a
# project file that changes it changes both the per-lane gate here AND the board-wide one
# there, never one without the other.
export STAGE_SWEEP_SEC="${STAGE_SWEEP_SEC:-300}"
case "$STAGE_SWEEP_SEC" in ''|*[!0-9]*) echo "spawn.sh: STAGE_SWEEP_SEC must be a number, not '$STAGE_SWEEP_SEC'" >&2; exit 64 ;; esac
# The awaiting-rebuild stale-label sweep (sweep_awaiting_rebuild, card #85): a card that has
# carried `awaiting-rebuild` this long with no project workflow ever dropping the label is
# parked in Stuck rather than waiting forever on a notifier that may be dead.
AWAITING_REBUILD_STALE_SEC="${AWAITING_REBUILD_STALE_SEC:-43200}"
case "$AWAITING_REBUILD_STALE_SEC" in ''|*[!0-9]*) echo "spawn.sh: AWAITING_REBUILD_STALE_SEC must be a number, not '$AWAITING_REBUILD_STALE_SEC'" >&2; exit 64 ;; esac
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
RELEASEFILE="$WATCHER_STATE/release"
DOCTOR_OK=""  # set by run_doctor the first time tick_once gets past a PAUSE (see run_doctor)

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

# run_doctor — bin/doctor.sh (card #39): a project whose board or CLAUDE.md is not wired
# up for the watcher never gets a tick launched against it. Checked by tick_once, once per
# process (the DOCTOR_OK global, declared beside PIDFILE/PAUSEFILE above), on the first
# poll that gets past a PAUSE — a paused lane makes no external call at all, doctor
# included (test 7's own contract: PAUSE means silence). doctor.sh already names which
# check failed on its own stderr; this only adds the one line saying the run is refusing
# to start, and returning 1 here is read by tick_once's caller the same as any other
# failed poll — a 10-minute retry, forever, until whatever doctor named is fixed.
run_doctor() {
  if PROJECT_REPO="$PROJECT_REPO" PROJECT_ROOT="$WATCHER_ROOT" WATCHER_GH="$WATCHER_GH" $WATCHER_DOCTOR; then
    return 0
  else
    log "doctor failed (see lines above); not launching any tick this poll"
    return 1
  fi
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
    # The Waiting stage: cards the owner dragged there on the Stage-grouped view. Best-effort —
    # a failure here must not stop In progress from being served, so it only logs.
    QUEUE_ROWS="$($WATCHER_BOARD queue 2>/dev/null)" || { QUEUE_ROWS=""; log "board.py queue failed; serving In progress only"; }
    # The Ready column, when this lane is allowed to pull from it. Best-effort in exactly
    # the same way, and for the same reason: a project whose board has no Ready column
    # makes `list` exit 1, which must read as "nothing to pull", never as a poll failure.
    if [ "$READY_PULL" = 1 ]; then
      READY_ROWS="$($WATCHER_BOARD list Ready 2>/dev/null)" || { READY_ROWS=""; log "board.py list Ready failed; not pulling from Ready this poll"; }
    else
      READY_ROWS=""
    fi
    return 0
  else
    BOARD_ERR="$(cat "$errfile" 2>/dev/null)"
    rm -f "$errfile"
    ROWS=""
    READY_ROWS=""
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

# body_blockers <n> — the OPEN issues #n's own BODY names as prerequisites, space-separated,
# on stdout. The net under open_blockers, for the cards nothing ever recorded a Relationship
# for: `/triage` is the only thing that turns a card's words into one, and it only ever sees
# a card with NO `agent:` label — so a batch spun off together by one design tick, each card
# carrying "Depends on #<m>" in its body and a label from birth, was launched in whatever
# order the board returned it. riffmap #447 (September 20, 2026) spent a whole tick
# rediscovering by hand what its own Depends-on line already said, and #448 was one poll
# from doing it again.
#
# It reads the words and never writes the relationship. A regex is a guess; a guess recorded
# on the issue outlives the sentence that caused it, and only a human would ever take it
# off. The card is skipped and gets the same FYI comment a recorded blocker earns, so a
# wrong guess costs one comment and is undone by editing the card.
#
# Only the explicit ordering phrases count — depends on, depend on, blocked by, blocked on,
# waits on, waiting on — before the reference, with nothing between them but whitespace and
# the markdown that dresses them up (`**Depends on** [#12](…)` is the shape board.md itself
# prints), and only references to THIS repo (`#12`, `[#12](https://github.com/<repo>/issues/12)`,
# or that URL bare). "Part of #12", "refs", "see", "like #12" are context, not order;
# triage.md draws the same line. A false positive stalls a card nobody is watching, so
# precision beats recall here: "after #12", "once #12 lands" and "needs #12 first" are NOT
# matched, even though /triage records them — a model reading the sentence can tell those
# from prose, a regex over a card body cannot. A phrase negated in the 24 characters before
# it (not, never, no longer, and the contractions — "this card is not blocked by #12" and
# "it doesn't depend on #12" mean the same thing) is skipped for the same reason.
#
# Fails closed exactly like open_blockers: any failed lookup — the body, or any one
# reference's state — returns 1 with nothing printed, and the caller skips the card for this
# poll rather than launch it on what it could not read. More than ten references is a body
# this was not built to read, and fails closed too rather than answering from the first ten.
#
# The ONE thing that does not fail closed is a reference to an issue that does not exist
# (404): a typo can block nothing, and a permanent 404 handled like a transient outage would
# skip the card silently on every poll for ever. It is dropped, with a line in the journal.
body_blockers() {
  local n="$1" body refs m state errfile out=""
  [ -n "${PROJECT_REPO:-}" ] || return 0
  body="$($WATCHER_GH api "repos/$PROJECT_REPO/issues/$n" --jq '.body // ""' 2>/dev/null)" || return 1
  [ -n "$body" ] || return 0
  refs="$(python3 -c '
import re, sys
body, repo, self_n = sys.argv[1], sys.argv[2], sys.argv[3]
PHRASE = re.compile(r"(?:depends?\s+on|blocked\s+(?:by|on)|wait(?:s|ing)\s+on)[*_\x60\s]*:?[*_\x60\s]*", re.I)
REF = re.compile(r"\[?#(\d+)\]?(?:\(([^)\s]*)\))?|https?://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)\b", re.I)
SEP = re.compile(r"\s*(?:,|;|and|&)\s*", re.I)
NEG = re.compile(r"\b(?:not|never|no longer)\b|n[\x27’]t\b", re.I)
found = []
for m in PHRASE.finditer(body):
    if NEG.search(body[max(0, m.start() - 24):m.start()]):
        continue
    pos = m.end()
    while True:
        r = REF.match(body, pos)
        if not r:
            break
        if r.group(4):
            if r.group(3).lower() != repo.lower():
                break
            num = r.group(4)
        else:
            num = r.group(1)
            url = r.group(2)
            if url and not re.search(r"github\.com/" + re.escape(repo) + r"/issues/" + num + r"\b", url, re.I):
                break
        if num != self_n and num not in found:
            found.append(num)
        pos = r.end()
        s = SEP.match(body, pos)
        if not s:
            break
        pos = s.end()
if len(found) > 10:
    sys.exit(3)
print(" ".join(sorted(found, key=int)))
' "$body" "$PROJECT_REPO" "$n")" || return 1
  [ -n "$refs" ] || return 0
  errfile="$(mktemp)"
  for m in $refs; do
    if ! state="$($WATCHER_GH api "repos/$PROJECT_REPO/issues/$m" --jq .state 2>"$errfile")"; then
      # A 404 is not an outage, and the two must not share an answer. A number that names
      # no issue in this repo — a typo in a card body — can block nothing, so it is dropped
      # with a line in the journal; treating it like a failed lookup would skip the card on
      # every poll forever, with no comment, no park and no attempt ever counted.
      if grep -q '404\|Not Found' "$errfile" 2>/dev/null; then
        log "#$n names #$m as a prerequisite, but there is no such issue in $PROJECT_REPO; ignoring it"
        continue
      fi
      rm -f "$errfile"; return 1
    fi
    case "$state" in
      open|OPEN) out="${out:+$out }$m" ;;
      closed|CLOSED) ;;
      *) rm -f "$errfile"; return 1 ;;
    esac
  done
  rm -f "$errfile"
  printf '%s' "$out"
}

# note_waiting <n> <blockers> [source] — the one FYI comment per card saying what it waits
# on. The marker under $WATCHER_STATE/waiting/<n> holds the blocker list it was posted for,
# so a card whose blockers change gets one more comment, and a card seen waiting on the same
# ones poll after poll gets none. Written only when the comment lands. Never in --dry-run.
#
# <source> is `body` when the dependency was read out of the card's own words rather than
# its Relationships field (body_blockers), and then the comment says so and how to undo it:
# a dependency nobody recorded is the one kind the owner may actually need to overrule. The
# marker keys on the blocker list alone, so the same dependency later recorded properly
# does not earn a second comment.
note_waiting() {
  local n="$1" blockers="$2" source="${3:-relationships}" links b marker where undo
  marker="$WATCHER_STATE/waiting/$n"
  [ "${DRY_RUN:-0}" != 1 ] || return 0
  if [ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null)" = "$blockers" ]; then return 0; fi
  mkdir -p "$WATCHER_STATE/waiting"
  links=""
  for b in $blockers; do links="${links:+$links, }[#$b](https://github.com/$PROJECT_REPO/issues/$b)"; done
  where=""
  undo="- Nothing for the owner to do, unless one of those cards is not on its way."
  if [ "$source" = body ]; then
    where="- Where I got that: this card's own words say so, and its Relationships field is empty.
"
    undo="- Nothing for the owner to do, unless one of those cards is not on its way — or the card's words are wrong, in which case edit them and I stop waiting."
  fi
  if $WATCHER_GH issue comment "$n" --body "🤖 FYI: this card waits on $links, so I am not starting it yet.
$where- I start it on my own once every card it waits on is closed.
$undo"; then
    printf '%s' "$blockers" > "$marker"
  else
    log "waiting comment failed for #$n; retried on a later poll"
  fi
}

# note_awaiting_rebuild <n> — the one FYI comment per wait, the first poll this lane sees
# the card still carrying `awaiting-rebuild` (card #85: the tick's own exit for a merged fix
# whose artifact is gated on a known downstream rebuild, commands/watch.md §3). The marker
# under $WATCHER_STATE/awaiting-rebuild/<n>.noted guards the comment, the same way
# waiting/<n> guards note_waiting's; `.since`, read but never written here, is
# sweep_awaiting_rebuild's own clock for the 12-hour stale-label safety net below. Never in
# --dry-run.
note_awaiting_rebuild() {
  local n="$1" dir="$WATCHER_STATE/awaiting-rebuild" noted
  [ "${DRY_RUN:-0}" != 1 ] || return 0
  mkdir -p "$dir"
  noted="$dir/$n.noted"
  [ -f "$noted" ] && return 0
  if $WATCHER_GH issue comment "$n" --body "🤖 FYI: this card is waiting on a downstream rebuild, so I am not launching a tick for it.
- I pick it up again on the poll after the label drops.
- Nothing for the owner to do."; then
    : > "$noted"
  else
    log "awaiting-rebuild FYI comment failed for #$n; retried on a later poll"
  fi
}

# wip_count — how many cards are open in the In progress column right now, counted from
# ROWS (the poll we already did). Blank lines do not count; an empty ROWS is 0.
wip_count() {
  local n=0 line
  while IFS= read -r line; do
    case "$line" in "") continue ;; esac
    n=$((n + 1))
  done <<< "${ROWS:-}"
  printf '%s' "$n"
}

# ready_pull_allowed — may this lane promote a Ready card on this poll? Off unless the
# project opted in, and only while the In progress column is under WIP_MAX. Logs the
# reason when it says no, so a quiet loop is never a mystery in journalctl.
#
# Callers test READY_ROWS for content FIRST, so this only ever runs when there was really
# something to promote: otherwise an empty Ready column plus a full In progress column
# would log "not pulling from Ready" once a minute, per lane, forever — a line that says
# the cap is holding work back when nothing is waiting on it.
ready_pull_allowed() {
  [ "$READY_PULL" = 1 ] || return 1
  local wip
  wip="$(wip_count)"
  if [ "$wip" -ge "$WIP_MAX" ]; then
    log "not pulling from Ready: $wip card(s) already In progress, WIP_MAX=$WIP_MAX"
    return 1
  fi
  return 0
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
    # awaiting-rebuild (card #85): a tick already merged this card's fix and found the
    # artifact gated on a known downstream rebuild (commands/watch.md §3); it left the
    # label as its own hand-off and ended the turn. Skip it, the same way in-flight is
    # skipped above, with one FYI comment the first poll that sees it — never launch a
    # tick for it while the label is on, and never bump its attempts for sitting here.
    case ",$r_flags," in
      *,awaiting-rebuild,*) note_awaiting_rebuild "$r_issue"; log "#$r_issue skipped: awaiting-rebuild"; continue ;;
    esac
    attn="$WATCHER_STATE/attempts/$r_issue"
    attempts=0
    [ -f "$attn" ] && attempts="$(cat "$attn" 2>/dev/null || echo 0)"
    case "$attempts" in ''|*[!0-9]*) attempts=0 ;; esac

    # Round 5, N1: this row came from `board.py list "In progress"` — the card is In
    # progress RIGHT NOW. If parked-ok/<r_issue> also exists (park_stuck's own move for
    # this card actually succeeded, not merely attempted), the only way both can be true
    # is the owner moved it back themselves: a genuinely-parked card sits in Stuck - Needs help,
    # never In progress, and nothing else clears this marker. Checking parked-ok/<n>
    # specifically (not parked/<n>, which park_stuck also writes when the move FAILS) —
    # a card whose move simply failed was never actually elsewhere, so it reappearing in
    # this same list poll after poll is not a retry signal, it's the original failure
    # persisting; treating that as "the owner retried it" would relaunch a card whose
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
        log "#$r_issue: In progress with a confirmed parked-ok marker on file — the owner moved it back; attempts cleared, picking normally"
      else
        # Cosmetic (round 6): --dry-run's whole contract is "claim/write nothing" — see
        # test 1 — so nothing was actually cleared here; say so, don't claim it was.
        log "#$r_issue: In progress with a confirmed parked-ok marker on file — the owner moved it back; attempts would clear, picking normally"
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
    #
    # The recorded Relationships field is asked first and believed on its own; the card's
    # own words (body_blockers) are consulted ONLY when nothing is recorded, which keeps
    # the extra API calls off the common path and keeps a human-set relationship — the
    # deliberate one — from ever being second-guessed by a regex.
    local blockers blockers_src=relationships
    if ! blockers="$(open_blockers "$r_issue")"; then
      log "#$r_issue skipped: could not read what it is blocked by (gh api failed); trying again next poll"
      continue
    fi
    if [ -z "$blockers" ]; then
      if ! blockers="$(body_blockers "$r_issue")"; then
        log "#$r_issue skipped: could not read the card's own words (gh api failed); trying again next poll"
        continue
      fi
      [ -z "$blockers" ] || blockers_src=body
    fi
    if [ -n "$blockers" ]; then
      case "$blockers_src" in
        body) log "#$r_issue skipped: its own words say it waits on open issue(s) $blockers (nothing recorded in Relationships)" ;;
        *)    log "#$r_issue skipped: waiting on open issue(s) $blockers" ;;
      esac
      note_waiting "$r_issue" "$blockers" "$blockers_src"
      continue
    fi
    if [ "${DRY_RUN:-0}" != 1 ]; then
      rm -f "$WATCHER_STATE/waiting/$r_issue"
      rm -f "$WATCHER_STATE/awaiting-rebuild/$r_issue.since" "$WATCHER_STATE/awaiting-rebuild/$r_issue.noted"
    fi
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
#                     treats as "the owner must have moved it back" — board.py list only ever
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
  local n="$1" detail="$2" transcript="${3:-}"
  local tried="$WATCHER_STATE/parked/$n" ok="$WATCHER_STATE/parked-ok/$n"
  mkdir -p "$WATCHER_STATE/parked" "$WATCHER_STATE/parked-ok"
  # A card that reaches Stuck starts over when the owner sends it back: sweep_in_review's
  # "already had its one turn" marker must not outlive the park.
  rm -f "$WATCHER_STATE/inreview/$n.requeued"

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
  # The body is for the owner (docs/talking-to-the-owner.md): the ask on line one, bullets,
  # one question, lettered options with the recommendation first, and how to answer. The
  # spawner only knows the card failed twice, not why, so the options are the three moves
  # the owner can make; $detail, the log path and the transcript (card #143, when there is
  # one) are the engineer's crumbs, kept short.
  local transcript_line=""
  [ -n "$transcript" ] && transcript_line=$'\n- Transcript kept: '"$(basename "$transcript")"
  local body
  body="🤖 **Needs ${NEEDS_WHO}: this card failed 2 attempts, so I parked it.**

- Moved to **Stuck - Needs help**. The bots will not retry on their own.
- Why: $detail${retry_note}

**Question: what should happen to this card?**
- **A. Try again** (recommended if the failure looks like bad luck) — move it back to **In progress**.
- **B. Fix the card first** — make it smaller or clearer, then move it back to **In progress**.
- **C. Drop it** — close the card.

Reply A, B or C on this card, or say which way you lean, then move it.

<details><summary>Details</summary>

- Logs: $WATCHER_LOGS
- Attempts on file: 2${transcript_line}

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
(cd "$dir" && PATH="$WATCHER_SHIM_DIR:\$PATH" WATCHER_REAL_CLAUDE="$WATCHER_REAL_CLAUDE" \\
  WATCHER_LEDGER_CALLER="tick:$PROJECT_NAME:$n" \\
  "$WATCHER_REAL_CLAUDE" -p "$LANE_COMMAND $n" \\
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
  PROMOTE_FROM=""
  if ! pick_card "$ROWS"; then
    if [ -n "${QUEUE_ROWS:-}" ] && pick_card "$QUEUE_ROWS"; then
      PROMOTE_FROM="Waiting"
    elif [ -n "${READY_ROWS:-}" ] && ready_pull_allowed && pick_card "$READY_ROWS"; then
      PROMOTE_FROM="Ready"
    else
      local where="In progress or Waiting"
      # `if`, not `[ … ] && …`: under `set -e` a bare AND-list whose test fails returns
      # non-zero and kills the daemon — and READY_PULL=0 is the common case here.
      if [ "$READY_PULL" = 1 ]; then where="$where or Ready"; fi
      echo "dry-run: nothing claimable in $where"
      exit 0
    fi
  fi
  local dir="$WORKTREE_BASE/card-$ISSUE" ts
  [ "$LANE_WORKDIR" = root ] && dir="$WATCHER_ROOT"
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  echo "lane: ${LANE_AGENTS:-every card (no LANE_AGENTS)}"
  echo "command: $LANE_COMMAND"
  echo "pick: #$ISSUE ($PRIORITY) $TITLE${PROMOTE_FROM:+ (from $PROMOTE_FROM, would move to In progress first)}"
  if [ "$LANE_WORKDIR" = root ]; then echo "workdir: $dir (no worktree)"; else echo "worktree: $dir"; fi
  echo "claude command:"
  print_claude_cmd "$ISSUE" "$dir" "$ts"
}

# --- section 1, step 4: the worktree ------------------------------------------------------

# create_worktree <n> [base] — the create-from-scratch path, shared by prepare_worktree's
# two entries into it: a card with no worktree yet (base defaults to "main"), and a reused
# one sync_reused_worktree just set aside as unsyncable (base is that same card's own branch
# name, so worktree.sh resolves it to THAT branch's origin counterpart — see
# sync_reused_worktree — rather than branching fresh off main and quietly dropping whatever
# origin already had on it). A failed create counts as an attempt either way (card #65
# review fold-in: a worktree that never gets created must still count toward the
# two-attempt Stuck rule).
create_worktree() {
  local n="$1" base="${2:-main}"
  if $WATCHER_WORKTREE new "card-$n" "$base"; then return 0; fi
  log "worktree create failed for #$n"
  local attempts
  attempts="$(bump_attempts "$n")"
  if [ "$attempts" -ge 2 ]; then
    park_stuck "$n" "Could not create the card's work folder (git worktree) twice in a row."
  fi
  return 1
}

# sync_reused_worktree <n> <dir> — reuse path only, before WT_DIR is set. Brings the
# worktree's branch up to date with origin before the tick launches on it (card #86 —
# riffmap-site #1, October 8 2026: a reused worktree sat on its previous head while another
# lane pushed to the same branch, and only a hand `git pull --ff-only` kept that tick's push
# from being refused as non-fast-forward). Returns 1 when the directory was renamed aside —
# dirty, or the branch diverged — so the caller falls through to `create_worktree "$n"
# "$branch"` for a fresh one at origin's head for THIS branch (never plain `create_worktree
# "$n"`, which would branch from main and silently drop whatever origin already had); 0
# otherwise, with $dir left exactly as found. A branch strictly ahead of its own origin
# counterpart (unpushed local commits, clean or not) is left alone too: there is nothing on
# origin to sync to, and touching a dirty tree the tick may be resuming (watch.md: "resume
# it, never start the card over") is only ever safe when origin actually moved past it.
sync_reused_worktree() {
  local n="$1" dir="$2" branch="card-$n"
  git -C "$dir" fetch -q origin "$branch" 2>/dev/null || true
  if ! git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    log "reusing existing worktree for #$n at $dir (never pushed)"
    return 0
  fi
  local old new
  old="$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)"
  new="$(git -C "$dir" rev-parse "origin/$branch" 2>/dev/null || true)"
  if [ -z "$old" ] || [ -z "$new" ] || [ "$old" = "$new" ]; then
    log "reusing existing worktree for #$n at $dir"
    return 0
  fi
  local ahead behind
  ahead="$(git -C "$dir" rev-list --count "origin/$branch..HEAD" 2>/dev/null || echo 0)"
  behind="$(git -C "$dir" rev-list --count "HEAD..origin/$branch" 2>/dev/null || echo 0)"
  if [ "$behind" -eq 0 ]; then
    log "reusing existing worktree for #$n at $dir"
    return 0
  fi
  local dirty=0
  [ -n "$(git -C "$dir" status --porcelain 2>/dev/null)" ] && dirty=1
  if [ "$dirty" = 1 ] || [ "$ahead" -gt 0 ]; then
    local why ts stale
    if [ "$dirty" = 1 ]; then why="dirty"; else why="diverged from"; fi
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    stale="$dir.stale-$ts"
    # `git worktree move`, never a bare `mv`: this worktree shares $WATCHER_ROOT's .git, so
    # a plain rename leaves it still registered — and its branch still named "$branch" —
    # against the OLD path, and create_worktree's next `git worktree add -b "$branch"` at
    # that same path then fails outright ("branch already exists" / "already registered
    # worktree" — found in review). `worktree move` relocates the administrative link
    # along with the directory, so the set-aside copy stays a fully usable git tree (dirty
    # changes included), and the branch rename right after frees the name for the fresh
    # worktree create_worktree is about to make. If the move itself fails (a locked
    # worktree, an old git), don't make things worse than before this fix: leave the
    # directory exactly where it is and let the tick launch on it as-is.
    if git -C "$WATCHER_ROOT" worktree move "$dir" "$stale" 2>/dev/null; then
      git -C "$WATCHER_ROOT" branch -m "$branch" "$branch.stale-$ts" 2>/dev/null || true
      log "card-$n worktree is $why origin/$branch ($old..$new); set aside at $(basename "$stale"), starting fresh"
      return 1
    fi
    log "card-$n worktree is $why origin/$branch ($old..$new) but could not be set aside (git worktree move failed); reusing it as-is"
    return 0
  fi
  git -C "$dir" merge -q --ff-only "origin/$branch"
  log "synced card-$n to origin/$branch ($old..$new)"
  return 0
}

prepare_worktree() {
  local n="$1"
  local dir="$WORKTREE_BASE/card-$n"
  if [ ! -d "$dir" ]; then
    create_worktree "$n" || return 1
  else
    sync_reused_worktree "$n" "$dir" || create_worktree "$n" "card-$n" || return 1
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
  PATH="$WATCHER_SHIM_DIR:$PATH" WATCHER_REAL_CLAUDE="$WATCHER_REAL_CLAUDE" \
  "$WATCHER_REAL_CLAUDE" -p "$LANE_COMMAND $n" \
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
# the lookup RETURNS 1 with nothing printed: handle_exit reads that as "no PR" (it only
# picks the sweeper's wording), sweep_in_review skips the card for the poll — it must never
# read an outage as "this card has no PR" and park a card whose PR is up (the same
# fail-closed rule as open_blockers).
open_pr_for() {
  local n="$1" out
  out="$($WATCHER_GH pr list --state open --search "#$n in:body" --json number,url,body --limit 30 2>/dev/null)" || return 1
  [ -n "$out" ] || return 1
  python3 -c 'import json, re, sys
n = sys.argv[1]
try:
    prs = json.loads(sys.argv[2])
except Exception:
    sys.exit(3)
# Bare `Closes #31` and the linked form `Closes [#31](https://.../issues/31)` that the
# riffmap house rule mandates (always link board cards, never a bare #nnn) both count.
# (This is inside a single-quoted bash string: no apostrophes in here.)
pat = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b:? +\[?#%s\b" % re.escape(n), re.I)
for p in prs:
    if pat.search(p.get("body") or ""):
        print(p["number"], p["url"])
        break
' "$n" "$out" 2>/dev/null || return 1
}

# keep_transcript <n> <ts> — on a tick that did not succeed, copies its Claude Code
# session transcript to $WATCHER_LOGS/card-<n>-<ts>.transcript.jsonl, the same stem as the
# result JSON and stderr, so a post-mortem reads what the tick actually did instead of
# guessing from an empty result (card #143). A copy, not a link: the worktree the source
# is keyed on may be removed the moment this exit finishes (handle_exit's own
# $LANE_WORKDIR = root check, below). Prints the kept path on success; prints nothing
# (logging why) when there is no transcript to keep, so a caller can fold either outcome
# into its own comment without caring which happened.
keep_transcript() {
  local n="$1" ts="$2" src dest size
  src="$($WATCHER_TAIL --path "$n" --workdir-mode "$LANE_WORKDIR" --worktree-base "$WORKTREE_BASE" 2>/dev/null)" || true
  if [ -z "$src" ]; then
    log "no session transcript found for #$n; nothing to keep"
    return 0
  fi
  dest="$WATCHER_LOGS/card-$n-$ts.transcript.jsonl"
  size="$(wc -c < "$src" 2>/dev/null || echo 0)"
  if [ "$size" -gt "$TRANSCRIPT_MAX_BYTES" ]; then
    echo "source path (too large to copy, $size bytes): $src" > "$dest"
    log "transcript for #$n is $size bytes, over the ${TRANSCRIPT_MAX_BYTES}-byte cap; recorded its source path at $dest instead of copying"
  elif cp "$src" "$dest" 2>/dev/null; then
    log "kept transcript for #$n at $dest"
  else
    log "transcript copy failed for #$n ($src -> $dest)"
    return 0
  fi
  echo "$dest"
}

handle_exit() {
  local n="$1" json="$2" rc="$3"
  local subtype total_cost num_turns denials elapsed_min show has_in_flight is_closed is_in_progress attn attempts pr has_awaiting_rebuild
  local transcript="" transcript_note=""

  subtype="$(json_field "$json" 'd.get("subtype","unknown")')"; subtype="${subtype:-unknown}"
  total_cost="$(json_field "$json" 'd.get("total_cost_usd","")')"
  num_turns="$(json_field "$json" 'd.get("num_turns","")')"
  denials="$(json_field "$json" '(len(d.get("permission_denials")) if isinstance(d.get("permission_denials"),list) else d.get("permission_denials",0)) or 0')"
  elapsed_min=$(( ( $(date +%s) - ${TICK_START_EPOCH:-$(date +%s)} ) / 60 ))

  # `quota`, not `cost`: the box runs on a subscription, so this number is tokens x API
  # list price computed client-side, never a bill (docs/how-it-works.md, "The dollar
  # figure is quota, not a bill").
  log "tick #$n exited rc=$rc subtype=$subtype quota~\$${total_cost:-?} turns=${num_turns:-?} denials=${denials:-0}"

  # Card #143: a tick that did not succeed (an empty/unparseable result counts as
  # "unknown", above) may have died with no record but its own session transcript. Keep it
  # now, same stem as $json/$err, before anything below can remove the worktree it's keyed
  # on. A successful tick keeps no copy — the card's own comments are its record.
  if [ "$subtype" != success ]; then
    local ts
    ts="$(basename "$json" .json)"; ts="${ts#card-$n-}"
    transcript="$(keep_transcript "$n" "$ts")"
    if [ -n "$transcript" ]; then
      transcript_note="- Transcript kept: $(basename "$transcript")"
    else
      transcript_note="- Transcript: none found"
    fi
  fi

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
  # The tick parked the card itself (its own Stuck exit, invisible to park_stuck): same
  # rule as there — the owner's answer starts the card over, so sweep_in_review's one-turn
  # marker goes. Without this a card re-queued once, parked, answered and finished days
  # later would be parked again within a minute of reaching In review, with a comment
  # about a re-queue long past (review of watcher#19).
  if python3 -c 'import json,sys
sys.exit(0 if json.loads(sys.argv[1]).get("status")=="Stuck - Needs help" else 1)' "$show" 2>/dev/null; then
    rm -f "$WATCHER_STATE/inreview/$n.requeued"
  fi
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
  # The tick's own awaiting-rebuild exit (card #85, commands/watch.md §3): it merged the
  # card's fix, found the artifact gated on a known downstream rebuild, labeled the card
  # and left it In progress on purpose — a success, the same as leaving In progress to
  # route a design card to another lane, not a no-progress attempt.
  has_awaiting_rebuild="$(python3 -c 'import json,sys
try:
    d=json.loads(sys.argv[1]); print("yes" if "awaiting-rebuild" in d.get("labels",[]) else "no")
except Exception:
    print("no")' "$show")"

  # Only ever sweeps the card THIS run holds — never a claim it did not make.
  if [ "$has_in_flight" = yes ] && [ "$(cat "$CURRENTFILE" 2>/dev/null || true)" = "$n" ]; then
    $WATCHER_BOARD unclaim "$n" || log "unclaim failed for #$n"
    # Two exits look identical from here — the tick's process is gone, the card still
    # in-flight — and the owner reads them very differently. A run that DIED holding the card is
    # the failure the second wording below was written for. A run that opened the card's PR
    # and then ended its turn before merging has done the work: riffmap#305, September 14,
    # 2026, ended with "waiting on CI — I'll be notified when the background check
    # finishes", a notification a headless run never gets, and the next tick found the PR
    # green and merged it in 18 seconds. The release and the retry are the same either way
    # (the card is In progress again, so pick_card takes it back); only the words differ.
    # Card #143: the transcript note (if any) is an engineer's crumb, same rule as every
    # other log path in these comments — under the fold, never above it.
    local details_block=""
    if [ -n "$transcript_note" ]; then
      details_block="

<details><summary>Details</summary>

$transcript_note

</details>"
    fi
    pr="$(open_pr_for "$n" || true)"
    if [ -n "$pr" ]; then
      # FYI-shaped (docs/talking-to-the-owner.md): nothing is asked of the owner; the PR is
      # the thing to look at, so it is the link, and no log path sits above the fold.
      $WATCHER_GH issue comment "$n" --body "🤖 FYI: PR #${pr%% *} is up for this card, but the run ended before merging it. I released the card; the next poll picks it up and merges once the checks are green.
- PR: ${pr#* }
- Exit: ${rc}/${subtype} after ${elapsed_min} min
- Agent quota used: ~\$${total_cost:-0} equivalent (subscription — not billed)${details_block}" \
        || log "sweeper comment failed for #$n"
      # No Discord post: nothing is broken — the work is done and the next poll merges it.
      # A second exit like this one parks the card, and the park is what alerts.
    else
      # FYI-shaped (docs/talking-to-the-owner.md): nothing is asked of the owner here — the card is
      # released and the next poll may retry it; two of these in a row become a park.
      $WATCHER_GH issue comment "$n" --body "🤖 FYI: the run ended without finishing this card. I released it; the next poll may retry once.
- Exit: ${rc}/${subtype} after ${elapsed_min} min
- ${denials:-0} permission denials
- Agent quota used: ~\$${total_cost:-0} equivalent (subscription — not billed)
- Log: $(basename "$json")${details_block}" \
        || log "sweeper comment failed for #$n"
      # Discord, if configured (board.py notify is a no-op otherwise). A retry is not an
      # alert — the park, if it comes, is — but a died run is something broken, and that
      # and a stuck card are the two things the channel is for.
      $WATCHER_BOARD notify "$n" released "The run ended after ${elapsed_min} min without finishing (${subtype}). I released the card; the next poll may retry once." \
        || log "discord notify failed for #$n"
    fi
  fi

  attn="$WATCHER_STATE/attempts/$n"
  if [ "$subtype" = "success" ] && { [ "$is_in_progress" != yes ] || [ "$left_lane" = yes ] || [ "$has_awaiting_rebuild" = yes ]; }; then
    # The tick succeeded and moved the card out of In progress (Done, In review, Stuck by
    # its own contract) — reset rather than increment, so a card that comes back to
    # In progress later (a fresh pass, a re-open) starts its two-attempt budget over
    # instead of inheriting a count from a previous, unrelated success (review, card #65
    # fold-in).
    rm -f "$attn"
    if [ "$left_lane" = yes ]; then
      log "tick #$n succeeded and relabeled the card for another lane (agent '$agent_now'): attempts/$n reset"
    elif [ "$has_awaiting_rebuild" = yes ]; then
      log "tick #$n succeeded and labeled the card awaiting-rebuild: attempts/$n reset"
    else
      log "tick #$n succeeded and left In progress: attempts/$n reset"
    fi
  else
    attempts="$(bump_attempts "$n")"
    if [ "$attempts" -ge 2 ] && [ "$is_in_progress" = yes ]; then
      park_stuck "$n" "The run ended with the card still In progress (log: $(basename "$json"))." "$transcript"
    fi
  fi

  if [ "$is_closed" = yes ]; then
    # No Discord post for a card that shipped or was closed: the floor page shows it, and
    # the channel is for stuck and broken only.
    $WATCHER_BOARD stage "$n" - >/dev/null 2>&1 || log "stage clear failed for #$n"
    rm -f "$WATCHER_STATE/inreview/$n.requeued"   # sweep_in_review's one-turn marker; the card is done
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
    # Card #143: same rule as handle_exit's own — a result that is not "success" (missing,
    # unparseable, or a genuine failure subtype) may have left nothing but its own session
    # transcript to explain what happened. Found from the newest card-<n>-*.json under
    # $WATCHER_LOGS, since this reconciliation (a LATER poll, possibly a whole new spawn.sh
    # process) never saw handle_exit's own $json/$ts.
    local tsfile ts recon_subtype="unknown" transcript="" transcript_note="" details_block=""
    tsfile="$(ls "$WATCHER_LOGS"/card-"$n"-*.json 2>/dev/null | sort | tail -n1)" || true
    if [ -n "$tsfile" ]; then
      ts="$(basename "$tsfile" .json)"; ts="${ts#card-$n-}"
      recon_subtype="$(json_field "$tsfile" 'd.get("subtype","unknown")')"; recon_subtype="${recon_subtype:-unknown}"
    fi
    if [ "$recon_subtype" != success ] && [ -n "${ts:-}" ]; then
      transcript="$(keep_transcript "$n" "$ts")"
      if [ -n "$transcript" ]; then
        transcript_note="- Transcript kept: $(basename "$transcript")"
      else
        transcript_note="- Transcript: none found"
      fi
      details_block="

<details><summary>Details</summary>

$transcript_note

</details>"
    fi
    $WATCHER_BOARD unclaim "$n" || log "reconcile: unclaim failed for #$n"
    $WATCHER_GH issue comment "$n" --body "🤖 FYI: a run died while holding this card (still marked in-flight, no process running). I released it; the next poll may retry.${details_block}" \
      || log "reconcile: unclaim comment failed for #$n"
    $WATCHER_BOARD notify "$n" released "A run died while holding this card; found on cleanup. I released it; the next poll may retry." \
      || log "reconcile: discord notify failed for #$n"
    if [ "$attempts" -ge 2 ]; then
      park_stuck "$n" "A run died holding this card (a crashed tick, or a board outage at its exit); found on cleanup, attempts=$attempts." "$transcript"
    fi
  else
    log "reconcile: #$n was not in-flight; nothing to unclaim"
  fi
  rm -f "$CURRENTFILE"
}

# --- the In review sweep ---------------------------------------------------------------------
# In review used to be a column nothing read: the spawner's only input is In progress, so a
# card that landed there stayed until the owner happened to look. Two riffmap cards did, for most
# of a day (watcher#18, September 18, 2026): #378's tick ran out of budget before its review
# and wrote "next tick needs to…" on a card no tick would ever open again, and #391 was
# dropped there by hand six seconds after it was filed, with no PR at all. So every poll,
# for this lane's cards (lane_takes — the same complement rule that keeps two lanes off one
# card keeps two lanes from sweeping one):
#   - no open PR, for INREVIEW_NO_PR_GRACE_SEC  → Stuck: nobody is working it and the column
#     says somebody is. The grace covers the moments between a merge and the card closing.
#   - an open PR, for INREVIEW_STALE_SEC        → back to In progress, ONCE: a tick finishes
#     it (merge main, review if none ran, merge). `requeued` remembers that it had its turn.
#   - back in In review after that turn         → Stuck. The bots had two goes; it needs the owner.
# State is three files per card under inreview/: <n>.seen (epoch of the first poll that saw
# it here; removed when the card leaves the column), <n>.said (this round's comment went out
# — a move that fails is retried every poll, the comment is not repeated), <n>.requeued.
# Comment first, move second: once the move lands the card is out of this list and nothing
# would come back to write the comment.
sweep_in_review() {
  local dir="$WATCHER_STATE/inreview" rows errfile now
  mkdir -p "$dir"
  errfile="$(mktemp)"
  if ! rows="$($WATCHER_BOARD list "In review" 2>"$errfile")"; then
    log "board.py list 'In review' failed; no sweep this poll: $(cat "$errfile" 2>/dev/null)"
    rm -f "$errfile"
    return 0
  fi
  rm -f "$errfile"
  now="$(date +%s)"

  local present=" " r_issue r_priority r_agent r_flags r_stage r_title
  # (No five-column shift as in pick_card: only the first four fields are read here.)
  while IFS=$'\t' read -r r_issue r_priority r_agent r_flags r_stage r_title; do
    [ -z "$r_issue" ] && continue
    present="$present$r_issue "
    case ",$r_flags," in *,in-flight,*) continue ;; esac
    lane_takes "$r_agent" || continue

    local seen="$dir/$r_issue.seen" said="$dir/$r_issue.said" requeued="$dir/$r_issue.requeued"
    [ -f "$seen" ] || echo "$now" > "$seen"
    local since age pr pr_num pr_url
    since="$(cat "$seen" 2>/dev/null || echo "$now")"
    case "$since" in ''|*[!0-9]*) since="$now" ;; esac
    age=$((now - since))
    if ! pr="$(open_pr_for "$r_issue")"; then
      log "#$r_issue: PR lookup failed; not sweeping it this poll"
      continue
    fi
    pr_num="${pr%% *}"; pr_url="${pr#* }"

    local body="" why="" target="" kind=""
    if [ -z "$pr" ]; then
      [ "$age" -ge "$INREVIEW_NO_PR_GRACE_SEC" ] || continue
      kind=no-pr
      target="Stuck - Needs help"
      why="This card is in In review but has no pull request, so nobody is working it."
      body="🤖 **Needs ${NEEDS_WHO}: this card is in In review but has no pull request, so nobody is working it.**

- Moved to **Stuck - Needs help**. The bots only pick up cards from **In progress**.
- In review is where a bot leaves a card whose PR is up. This card has no open PR.

**Question: where did you mean to put it?**
- **A. Work it now** (recommended) — move it to **In progress**.
- **B. Not yet** — move it to **Backlog** or **Ready**.
- **C. Drop it** — close the card.

Move the card; no reply needed."
    elif [ -f "$requeued" ]; then
      kind=second-visit
      target="Stuck - Needs help"
      why="This card's PR came back to In review after a second try. It needs you."
      body="🤖 **Needs ${NEEDS_WHO}: this card's PR is still not merged after a second try.**

- PR: $pr_url
- Moved to **Stuck - Needs help**. I already sent it back to **In progress** once and it came back to In review. The tick's last comment above says why.

**Question: what should happen to it?**
- **A. Try again** (recommended if the comment above reads like bad luck) — move it back to **In progress**.
- **B. Answer what it is waiting on** — reply on this card, then move it back to **In progress**.
- **C. Drop it** — close the PR and the card.

Reply if you chose B, then move the card."
    else
      [ "$age" -ge "$INREVIEW_STALE_SEC" ] || continue
      kind=requeue
      target="In progress"
      body="🤖 FYI: PR #$pr_num has been waiting in In review, so I sent this card back to **In progress**. The next tick finishes it (merge main, review, merge) or parks it in Stuck with the question. Nothing for you to do."
    fi

    # .said names WHICH comment went out: a no-PR comment whose move kept failing must not
    # silence the different comment a later state of the same card calls for.
    if [ "$(cat "$said" 2>/dev/null || true)" != "$kind" ]; then
      if $WATCHER_GH issue comment "$r_issue" --body "$body"; then
        echo "$kind" > "$said"
      else
        log "#$r_issue: In review sweep comment failed; retrying next poll, not moving without it"
        continue
      fi
    fi
    if [ "$target" = "In progress" ]; then
      if $WATCHER_BOARD move "$r_issue" "In progress"; then
        : > "$requeued"; rm -f "$seen" "$said"
        log "#$r_issue: In review for ${age}s with PR #$pr_num open → back to In progress (once)"
      else
        log "#$r_issue: move back to In progress failed; retrying next poll"
      fi
    else
      if $WATCHER_BOARD move "$r_issue" "$target" --why "$why"; then
        # The owner's answer starts the card over: its next visit to In review gets its own turn.
        rm -f "$seen" "$said" "$requeued"
        log "#$r_issue: In review → Stuck ($why)"
      else
        log "#$r_issue: move to Stuck failed; retrying next poll"
      fi
    fi
  done <<< "$rows"

  # A card that left the column (merged, moved by hand) starts a fresh clock if it returns.
  local f k
  for f in "$dir"/*.seen "$dir"/*.said; do
    [ -e "$f" ] || continue
    k="$(basename "$f")"; k="${k%.*}"
    case "$present" in *" $k "*) ;; *) rm -f "$f" ;; esac
  done
}

# --- the closed-card Stage sweep ---------------------------------------------------------------
# handle_exit empties the Stage of the card a tick was holding when it closed, and nothing
# else did: a card that closes with NO tick on it kept its last Stage for good. Five cards
# on the watcher's own board sat in Done reading "Parked" (watcher#35, #36, #40, #43, #44,
# September 28, 2026) — each parked in Stuck for its PR to be merged, each merged by hand
# hours later, by which time the lane had long moved on. So every STAGE_SWEEP_SEC, board.py
# empties the Stage on every closed card that still has one (`sweep-stages`: one board
# read, a capped number of clears, no Discord post).
#
# Not per lane (the Stage is the board's, and a second lane's sweep finds nothing left),
# not every poll (a full board read each time, for something that is only ever cosmetic),
# and best-effort: a sweep that fails is one line in the journal and waits its turn like
# any other — the stamp is written before the call, so an outage cannot turn this into a
# retry every poll. It never fails the poll.
sweep_closed_stages() {
  local stamp="$WATCHER_STATE/stage-sweep.last" now last out errfile line
  now="$(date +%s)"
  last="$(cat "$stamp" 2>/dev/null || echo 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ $((now - last)) -lt "$STAGE_SWEEP_SEC" ]; then return 0; fi
  echo "$now" > "$stamp"
  errfile="$(mktemp)"
  if ! out="$($WATCHER_BOARD sweep-stages 2>"$errfile")"; then
    log "board.py sweep-stages failed; stale Stages stay until the next sweep: $(cat "$errfile" 2>/dev/null)"
    rm -f "$errfile"
    return 0
  fi
  rm -f "$errfile"
  while IFS= read -r line; do
    if [ -n "$line" ]; then log "closed card, stale Stage emptied: $line"; fi
  done <<< "$out"
  return 0
}

# --- the awaiting-rebuild stale-label sweep ----------------------------------------------
# A tick that merges a fix gated on a known downstream rebuild labels the card
# `awaiting-rebuild` and ends (commands/watch.md §3, card #85): pick_card (above) skips it
# until a project's own workflow removes the label when the rebuild finishes. That workflow
# runs off this box — a dead notifier, a rebuild that silently never ran, a label dropped
# by hand with nobody re-checking the card — so the wait cannot be open-ended. Every poll,
# for this lane's cards (lane_takes, the same complement rule every other sweep uses), a
# card still carrying the label after AWAITING_REBUILD_STALE_SEC (default 12 hours) is
# parked in Stuck. park_stale_rebuild, not park_stuck: "failed 2 attempts" would be a lie
# here, and attempts was reset to 0 the moment the label went on.
sweep_awaiting_rebuild() {
  local r_issue r_priority r_agent r_flags r_stage r_title now since age dir marker
  local parked_list="" new_rows="" line first tab nl
  tab="$(printf '\t')"; nl="$(printf '\n')"
  dir="$WATCHER_STATE/awaiting-rebuild"
  now="$(date +%s)"
  while IFS=$'\t' read -r r_issue r_priority r_agent r_flags r_stage r_title; do
    [ -z "$r_issue" ] && continue
    case ",$r_flags," in *,awaiting-rebuild,*) ;; *) continue ;; esac
    lane_takes "$r_agent" || continue
    mkdir -p "$dir"
    marker="$dir/$r_issue.since"
    [ -f "$marker" ] || echo "$now" > "$marker"
    since="$(cat "$marker" 2>/dev/null || echo "$now")"
    case "$since" in ''|*[!0-9]*) since="$now" ;; esac
    age=$((now - since))
    [ "$age" -ge "$AWAITING_REBUILD_STALE_SEC" ] || continue
    park_stale_rebuild "$r_issue"
    parked_list="$parked_list $r_issue "
  done <<< "${ROWS:-}"
  # $ROWS was read once at the top of this poll (poll_board), before this sweep ran, so a
  # card just parked above still sits in it as "In progress, awaiting-rebuild" — without
  # dropping it here, pick_card (which reads the same $ROWS right after this function
  # returns) would process that same stale row this same poll and have
  # note_awaiting_rebuild post a fresh "still waiting" FYI on a card that was just moved to
  # Stuck a moment ago.
  if [ -n "$parked_list" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      first="${line%%"$tab"*}"
      case "$parked_list" in *" $first "*) continue ;; esac
      new_rows="${new_rows:+$new_rows$nl}$line"
    done <<< "${ROWS:-}"
    ROWS="$new_rows"
  fi
}

# park_stale_rebuild <n> — the move+comment for the sweep above. Shares park_stuck's two
# markers (parked/<n>, parked-ok/<n>: "the one Stuck comment went out", "the move actually
# landed") so the two park paths can never both fire for the same card, but not its body —
# "this card failed 2 attempts" is not what happened here, so this writes its own.
park_stale_rebuild() {
  local n="$1" hours
  hours=$(( AWAITING_REBUILD_STALE_SEC / 3600 ))
  [ "$hours" -ge 1 ] || hours=1
  local tried="$WATCHER_STATE/parked/$n" ok="$WATCHER_STATE/parked-ok/$n"
  mkdir -p "$WATCHER_STATE/parked" "$WATCHER_STATE/parked-ok"
  rm -f "$WATCHER_STATE/inreview/$n.requeued"

  if [ ! -f "$ok" ]; then
    if $WATCHER_BOARD move "$n" "Stuck - Needs help" --why "The downstream rebuild this card is waiting on never reported back, so I parked it."; then
      : > "$ok"
      # Only now: removing the label here is what stops sweep_awaiting_rebuild (and
      # pick_card's own skip, above) from ever looking at this card again, so it must not
      # happen before the move actually lands — otherwise a failed move above leaves this
      # card unlabeled and with attempts still at 0, and the very next poll's pick_card
      # launches a fresh tick on it while this comment is telling the owner it is parked.
      $WATCHER_GH issue edit "$n" --remove-label in-flight --remove-label awaiting-rebuild || true
    else
      log "move to Stuck failed for #$n; will keep retrying on later polls"
    fi
  fi

  if [ -f "$tried" ]; then
    log "#$n already has a Stuck comment on file; not re-commenting"
  else
    local retry_note=""
    [ -f "$ok" ] || retry_note=$'\n- The move to Stuck did not go through yet; I will keep retrying it.'
    local body
    body="🤖 **Needs ${NEEDS_WHO}: the downstream rebuild this card is waiting on never reported back.**

- Moved to **Stuck - Needs help**. It had been waiting over $hours hours.
- Why: the project's own rebuild-finished notice never arrived.${retry_note}

**Question: what should happen to this card?**
- **A. Check and try again** (recommended) — look at whether the rebuild actually ran, then move it back to **In progress**.
- **B. Fix the card first** — make it smaller or clearer, then move it back to **In progress**.
- **C. Drop it** — close the card.

Reply A, B or C on this card, or say which way you lean, then move it.

<details><summary>Details</summary>

- Logs: $WATCHER_LOGS

</details>"
    $WATCHER_BOARD stage "$n" Parked >/dev/null 2>&1 || true
    if $WATCHER_GH issue comment "$n" --body "$body"; then
      : > "$tried"
    else
      log "stale-rebuild comment failed for #$n; retried on a later poll"
    fi
  fi
  rm -f "$WATCHER_STATE/awaiting-rebuild/$n.since" "$WATCHER_STATE/awaiting-rebuild/$n.noted"
}

# --- following the pointer (card #106) --------------------------------------------------
# pointer_moved — true when $WATCHER_POINTER_BIN (this script's own path AS NAMED on the
# command line, unresolved — the systemd unit's ExecStart, which never changes) now
# resolves somewhere other than $WATCHER_HERE, the real folder this process pinned itself
# to at launch (above). deploy/autodeploy.sh flips the pointer onto a new release's folder
# without ever touching this process; this is how a long-running lane notices, on its own
# clock, with no watcher of its own needed. A pointer that does not resolve at all right now
# (mid-flip, or the box not using one) reads as "not moved" — never follow into nothing.
pointer_moved() {
  local now
  now="$(cd -P "$WATCHER_POINTER_BIN" 2>/dev/null && pwd)" || return 1
  [ -n "$now" ] && [ "$now" != "$WATCHER_HERE" ]
}

# follow_pointer — re-execs this same process (same pid, same systemd unit) into the
# release the pointer now names, carrying the daemon's own original argv. Every call into
# tick_once is already between ticks — launch_tick blocks inside the PREVIOUS tick_once
# call until the tick is done — so being here at all already means idle; $CURRENTFILE is
# checked anyway as the one record everything else in this file already trusts for "a tick
# is running" (reconcile_stale_current, above). Never while paused is not a thing this
# checks on purpose: following the pointer makes no external call of any kind, so a paused
# lane keeps its docs and scripts current too, ready the moment it is unpaused.
follow_pointer() {
  [ -s "$CURRENTFILE" ] && return 0
  pointer_moved || return 0
  local new="$WATCHER_POINTER_BIN/spawn.sh"
  [ -x "$new" ] || return 0
  local new_here
  new_here="$(cd -P "$WATCHER_POINTER_BIN" && pwd)"
  log "the pointer moved to $new_here; re-launching from there"
  # Unset, never carried into the new process: WATCHER_HOME is already exported into THIS
  # process's own environment (right at the top of the file), and `export FOO="${FOO:-x}"`
  # in the re-exec'd copy would see it already set and keep the OLD value forever — exactly
  # the trap the LANE_CHANNEL=canary shim's own `unset WATCHER_HOME` (above) already avoids,
  # for the same reason: `exec` hands the whole environment to the new process, not just argv.
  unset WATCHER_HOME
  # Card #115: move into the new release's folder before handing off, the same reason the
  # process-start `cd` above exists — so this long-running process is never left standing
  # in the OLD release once a later deploy sweeps it. $new_here is .../bin; the release
  # folder is its parent, same as WATCHER_HOME's own derivation from WATCHER_HERE above.
  cd "$(dirname "$new_here")"
  # The `+` form, not a bare "${WATCHER_ARGV[@]}": the daemon runs with no arguments, and
  # bash before 4.4 (a Mac's /bin/bash 3.2) calls an empty array unbound under `set -u`, so
  # the re-exec died there instead of following the pointer. Same argv on every bash.
  exec "$new" ${WATCHER_ARGV[@]+"${WATCHER_ARGV[@]}"}
}

# record_release — $RELEASEFILE names the release folder (its bare directory name — a
# commit's short sha, under a deployed release layout) this process is pinned to, so
# deploy/autodeploy.sh's own sweep (sweep_releases) never removes a release this lane's
# current tick, if any, is still running from. Written once, at process start — WATCHER_HOME
# cannot change under a running process (above), so it never needs rewriting mid-run.
record_release() {
  mkdir -p "$WATCHER_STATE"
  basename "$WATCHER_HOME" > "$RELEASEFILE" 2>/dev/null || true
}

# --- one poll iteration --------------------------------------------------------------------
tick_once() {
  follow_pointer

  # Every poll, before anything else — see reconcile_stale_current's own comment for why
  # this can no longer be a one-shot call made only at process start.
  reconcile_stale_current

  if [ -f "$PAUSEFILE" ]; then
    log "paused ($PAUSEFILE present); idling"
    return 0
  fi

  if [ -z "$DOCTOR_OK" ]; then
    # Return 1, not 2: run_daemon's own case statement already waits 10 minutes on any
    # non-2 failure (rc=2 is poll_board's, which sleeps 5 min itself) — this reuses that
    # instead of sleeping twice.
    if run_doctor; then DOCTOR_OK=1; else return 1; fi
  fi

  if ! poll_board; then
    log "board.py failed, sleeping 5 min: $BOARD_ERR"
    wsleep 300
    return 2
  fi

  sweep_in_review
  sweep_closed_stages
  sweep_awaiting_rebuild

  QUEUED=0
  PROMOTE_FROM=""
  if ! pick_card "$ROWS"; then
    if [ -n "${QUEUE_ROWS:-}" ] && pick_card "$QUEUE_ROWS"; then
      QUEUED=1; PROMOTE_FROM="Waiting"
    elif [ -n "${READY_ROWS:-}" ] && ready_pull_allowed && pick_card "$READY_ROWS"; then
      QUEUED=1; PROMOTE_FROM="Ready"
    else
      local where="In progress or Waiting"
      # `if`, not `[ … ] && …`: under `set -e` a bare AND-list whose test fails returns
      # non-zero and kills the daemon — and READY_PULL=0 is the common case here.
      if [ "$READY_PULL" = 1 ]; then where="$where or Ready"; fi
      log "nothing claimable in $where"
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
      log "#$ISSUE taken from $PROMOTE_FROM → In progress"
    else
      log "#$ISSUE: could not move from $PROMOTE_FROM to In progress; not launching"
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
  record_release
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
    record_release
    # No standalone reconcile_stale_current call here either — tick_once's own top-of-poll
    # call covers this single iteration too.
    local rc=0
    tick_once || rc=$?
    exit "$rc"
  fi

  run_daemon
}

main "$@"
