# How the watcher works

Written for the agents that run inside it. Read this once before touching a card, a
review, or this repo. If you are reading it as a human, `docs/steve-cheat-sheet.md` is the
short version.

## Who is who

| Piece | File | Runs as | Job |
|---|---|---|---|
| **Spawner** | `bin/spawn.sh` | a systemd unit, plain bash, no Claude session | polls the board once a minute, starts one tick at a time, sweeps stale claims, parks cards that fail twice |
| **Tick** | `commands/watch.md` (`/watch`) | `claude -p`, permissions skipped, in the card's worktree | claims one card, does the work, opens the PR, merges it or parks the card |
| **Reviewer** | `agents/reviewer.md` | a subagent the tick calls once | the one review a change gets; blocking findings only |
| **Board CLI** | `bin/board.py` (`board` on the box) | called by both of the above | `list`, `show`, `move`, `unclaim` against the GitHub Projects board |
| **Worktrees** | `bin/worktree.sh` | called by the spawner | one worktree per card beside the main checkout; copies private inputs in |
| **Discord** | `bin/notify.py` | imported by the board CLI | posts a card's stage and column changes, and the Stuck alert, to Steve's channel — when a webhook is configured |
| **Steve** | a human | GitHub, on his phone | moves cards, answers Stuck cards, owns every product decision |

Steve is the only human. Everything else is a bot. The box the bots run in is throwaway and
holds nothing worth protecting beyond a bot GitHub login and a Claude token, which is why
the tick can run with permissions skipped.

## The board

Columns, in order: `Backlog | Ready | In progress | In review | Done | Stuck - Needs help`.

| Column | Who moves cards in | Meaning |
|---|---|---|
| Backlog, Ready | Steve | not the watcher's business; never pull from them |
| **In progress** | Steve (or Steve moving a Stuck card back) | **the watcher's only input.** A card here is a hand-off to the bots |
| In review | the tick | PR is green and waiting on a merge that did not happen; not a failure |
| Done | the merge (`Closes #n`) | shipped |
| **Stuck - Needs help** | the tick or the spawner | the bots need Steve. The comment on the card says what, in the format in `docs/talking-to-steve.md` |

Labels and fields the bots read:

- **`in-flight`** — a tick is working this card right now. A courtesy flag, not a lock: the
  spawner's pidfile is the real lock, since only one tick runs at a time. Must be removed on
  every exit, or the next tick skips the card forever.
- **`agent: <name>`** — exactly one, or none. Names the project subagent that should do the
  work. `evaluator`, `reviewer` and `scribe` produce a measurement or a document rather
  than a PR, so the tick routes those to Stuck for a human.
- **`steve-decision`** — a product decision. The tick never does these; straight to Stuck.
- **`blocked`** — straight to Stuck.
- **Priority** field `P0`, `P1`, `P2` — `board list` sorts P0 first, then by issue number.

## Life of a card

1. **Steve moves a card to In progress.**
2. **The spawner polls** (`board list "In progress"`) once a minute. It takes the first row
   that is not `in-flight` and has fewer than 2 attempts on file. It never claims a card
   itself.
3. **A worktree** `../<repo>-worktrees/card-<n>` is created (or reused) on branch `card-<n>`.
4. **The tick starts:** `claude -p "/watch <n>" --dangerously-skip-permissions --max-turns 400
   --max-budget-usd 15`, with a 150 minute watchdog that kills the whole process group.
5. **The tick** claims the card (comment first, label second), reads the card and the
   project's `CLAUDE.md`, does the work, opens a PR with `Closes #<n>`, waits for CI, runs
   the reviewer once, merges. Or it moves the card to In review, or Stuck, or leaves it in
   In progress if it got nowhere. It removes `in-flight` on every one of those exits.
6. **The spawner's exit handling** reads the card back. If `in-flight` leaked, it unclaims
   and leaves a sweeper comment. A success that left In progress resets the attempt count.
   Anything else bumps it; **two attempts with the card still In progress parks it in Stuck**
   with a comment. A closed card gets its worktree removed.
7. **Sleep:** 60 seconds after a normal poll, 10 minutes after a failed tick, 5 minutes
   after a board outage.

**Two attempts, then Steve.** That rule is enforced by the spawner, not by the tick. A tick
that keeps retrying the same failing thing inside one run is wasting the budget; go to Stuck
instead. Steve moving a parked card back to In progress resets the count to zero.

## State on disk

`~/.local/state/watcher/<project>/` on the box (`WATCHER_STATE`):

| Path | Meaning |
|---|---|
| `spawn.pid` | the singleton lock |
| `PAUSE` | present = the spawner idles; `touch` to pause, `rm` to resume |
| `current` | the card the running (or crashed) tick holds; reconciled at the top of every poll |
| `attempts/<n>` | attempt count for card n; reset on a success that left In progress, or when Steve moves a parked card back |
| `parked/<n>` | the one Stuck comment for card n was posted |
| `parked-ok/<n>` | the move to Stuck actually landed |
| `logs/card-<n>-<ts>.json` | the tick's JSON result: `subtype`, `total_cost_usd`, `num_turns`, `result` |
| `logs/card-<n>-<ts>.stderr` | the tick's stderr |

## How a tick ends

| The card ends in | With `in-flight` | Meaning to the spawner |
|---|---|---|
| Done | removed | success; attempts reset |
| In review | removed | success; attempts reset |
| Stuck - Needs help | removed | success as far as the tick goes; attempts reset; Steve's turn |
| In progress | removed | no progress; attempts +1; at 2 the spawner parks it |
| any | still there | the tick died; the spawner unclaims, comments, attempts +1 |

`claude -p` result subtypes the spawner reads: `success`; `error_max_turns`;
`error_max_budget_usd`; `error_during_execution`; a killed tick shows exit 143.

## Discord

Steve does not sit on the board; he gets pinged. When `DISCORD_WEBHOOK_URL` is set (in
`/etc/watcher/env` for the box, or in a project's env file), `board.py` posts to the
channel **after** a change lands, so the board is always the record and Discord only ever
repeats it. Every post names the card, links to it, and links the PR when one exists.

| Event | Posted by | Looks like |
|---|---|---|
| a column change (`board move`) | whoever moved it: the tick, or the spawner taking a Waiting card | 📋 `#87 Add speaker names → In review` |
| a stage step (`board stage`) | the tick, at every step | 🔧 `#87 … → Building` |
| **a move to Stuck** | the tick (comment first, then move) or the spawner's park | 🚨 **Needs Steve:** the ask, quoted from the card, red; `DISCORD_MENTION` pings someone |
| the card closed | the spawner, at the tick's exit | ✅ `shipped` with the merged PR, or 🗂️ `closed without a merge` |
| a run died holding the card | the spawner's sweeper or reconciliation | ⚠️ `the run ended without finishing`; the next poll may retry |

Nothing is posted per poll, per tick, or for a quiet tick; `Parked` as a stage is silent
because the Stuck alert already covered it. A post that fails is one line on stderr and
nothing else — a notification can never fail a board command or a tick. `python3
bin/test-notify.py` checks the shapes offline; `python3 bin/notify.py stuck 1 "smoke test"
https://example.com` sends one by hand.

## Where Steve comes in

- He moves cards in. He answers Stuck cards by replying on the card and moving it back to
  In progress. He can pause the box with a file and read the logs.
- **Every message aimed at him follows `docs/talking-to-steve.md`.** First line is the ask,
  bullets, one question, lettered options, how to answer. This is a hard rule for the tick,
  the reviewer's escalations, and the spawner's comments alike.
- He does not read reviewer reports, log files, or code comments unless he goes looking.
  Those can be as dense as they need to be.

## Hard rules for every agent

- One card per tick. One card at a time. Never pull from Ready or tidy the board.
- Never force-push, never merge red, never push `main` directly, never `--admin`, never
  touch a project's secrets, never relax a hard rule to make a card pass.
- Never wait on an external reviewer. There is none. The reviewer runs in the session and
  returns.
- Remove `in-flight` on every exit. A leaked label strands the card.
- A product decision is Steve's. Do not guess it; park the card with the options laid out.
- Report what the run cost.

## What a project brings, what the watcher brings

The watcher brings the loop: the spawner, `/watch`, the reviewer, the board CLI, worktrees.
A project keeps its own `CLAUDE.md` (hard rules, the required CI check, standing traps),
its own `.claude/agents/`, its own CI, and can override `/watch` with its own
`.claude/commands/watch.md`. The generic `/watch` reads the project's `CLAUDE.md` for the
names it needs (the CI check, the contracts it must not touch, the spend receipt if any).

Deploying a new watcher version is `deploy/rollout.sh <project> [branch]`: it pulls,
pauses, waits for a running tick to finish, restarts the unit, un-pauses. It never signals
a tick. The watcher's own files live at `/opt/watcher` on the box; the spawner exports that path to
the tick as `WATCHER_HOME`, so `cat "$WATCHER_HOME/docs/talking-to-steve.md"` works from any
worktree.
