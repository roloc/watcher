# How the watcher works

Written for the agents that run inside it. Read this once before touching a card, a
review, or this repo. If you are reading it as a human, `docs/steve-cheat-sheet.md` is the
short version.

## Who is who

| Piece | File | Runs as | Job |
|---|---|---|---|
| **Spawner** | `bin/spawn.sh` | a systemd unit, plain bash, no Claude session | polls the board once a minute, starts one tick at a time in its lane, sweeps stale claims, parks cards that fail twice |
| **Tick** | `commands/watch.md` (`/watch`) | `claude -p`, permissions skipped, in the card's worktree | claims one card, does the work, opens the PR, merges it or parks the card |
| **Triage tick** | `commands/triage.md` (`/triage`) | `claude -p`, permissions skipped, in the main checkout | puts the one `agent:` label on a card that has none and records what it waits on; never does the work |
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
| Backlog | Steve | not the watcher's business; never pulled from |
| **Ready** | Steve | **the loop's own queue** when `READY_PULL=1` (riffmap, since September 19, 2026): a lane promotes the top card here to In progress when In progress and Waiting are both dry and the column is under `WIP_MAX`. Without that setting it is Backlog's twin and nothing reads it |
| **In progress** | Steve, a lane promoting a Ready or Waiting card, or Steve moving a Stuck card back | **what a tick is handed.** A card here is work in flight |
| In review | the tick | the work is finished, reviewed and green, and only the merge did not happen. Not a resting place: the spawner sweeps it every poll (see "The In review sweep") |
| Done | the merge (`Closes #n`) | shipped |
| **Stuck - Needs help** | the tick or the spawner | the bots need Steve. The comment on the card says what, in the format in `docs/talking-to-steve.md` |

Labels and fields the bots read:

- **`in-flight`** — a tick is working this card right now. A courtesy flag, not a lock: the
  spawner's pidfile is the real lock, since only one tick runs at a time per lane and lanes
  never share a card (see "Lanes" below). Must be removed on every exit, or the next tick
  skips the card forever.
- **`agent: <name>`** — exactly one, or none. Names the project subagent that should do the
  work. `evaluator` (a measurement, posted on the card) and `scribe` (a docs-only PR) are
  worked by the tick like any other card, since September 19, 2026 — a measurement that
  needs a live, paid model run is the one that still goes to Stuck. `reviewer` produces a
  verdict on somebody else's change rather than an artifact of its own, so the tick routes
  that one to Stuck for a human.
- **`steve-decision`** — a product decision. The tick never does these; straight to Stuck.
- **`blocked`** — straight to Stuck.
- **Priority** field `P0`, `P1`, `P2` — `board list` sorts P0 first, then by issue number.

## Life of a card

1. **The card reaches In progress** — Steve moves it there, or drags it to the Waiting
   stage, or (with `READY_PULL=1`) a lane promotes it from **Ready** on its own.
2. **The spawner polls** (`board list "In progress"`) once a minute. It takes the first row
   that is not `in-flight`, has fewer than 2 attempts on file, and is not blocked by an
   open issue (see "Dependencies"). It never claims a card itself.
   **If nothing there is claimable** it tries the Waiting stage, and then — only with
   `READY_PULL=1`, and only while the In progress column holds fewer than `WIP_MAX` open
   cards — the top row of **Ready**, which it moves to In progress before launching, so
   Status stays truthful and Discord shows the card starting. See "Self-pull" below.
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

## The In review sweep

The spawner's input is In progress, so In review used to be a column nothing read: a card
there waited for Steve to notice it, and the Discord line for the move is an ordinary 📋
post, not an alert. Two riffmap cards sat there for most of a day on September 17, 2026
(watcher#18) — one whose tick ran out of budget before its review, one dropped there by
hand with no PR. So every poll, after the board read and before the pick, `spawn.sh`'s
`sweep_in_review` lists In review and, for the cards that are this lane's by label:

| The card has | For | The spawner |
|---|---|---|
| no open PR | 10 minutes (`INREVIEW_NO_PR_GRACE_SEC`; covers a merge that is still closing the card) | comments the ask, moves it to **Stuck** |
| an open PR | 2 hours (`INREVIEW_STALE_SEC`) | comments an FYI, moves it back to **In progress** — once. A tick finishes the PR |
| an open PR, and already had that turn | — | comments the ask with the PR link, moves it to **Stuck** |

An `in-flight` card is skipped. Comment first, move second, and the move is retried every
poll until it lands; the comment is not repeated. State is `inreview/<n>.seen|.said|.requeued`
under the lane's state directory; `.requeued` is cleared whenever the card reaches Stuck —
parked by the sweep, by `park_stuck`, or by the tick itself (`handle_exit` reads the status
back) — because Steve's answer starts it over, and when it closes. A re-queued card is
picked on the following poll, not the same one. Everything fails closed: a PR lookup that
errors is not "no PR" and the card is left alone for that poll, and a failed In review read
never stops In progress from being served. Both limits are env knobs a project file may set.

## Lanes

A project can run as **two spawner instances side by side** — two systemd units, two
project files, two state directories — so a design card and a build card are worked at the
same time. Each instance is a **lane**, and `LANE_AGENTS` in its project file says which
cards are its to pick, by `agent:` label:

| Lane | Unit | Project file | `LANE_AGENTS` | Takes |
|---|---|---|---|---|
| design | `watcher@riffmap-design` | `projects/riffmap-design.env` | `architect,designer` | exactly those agents |
| build | `watcher@riffmap` | `projects/riffmap.env` | `!architect,designer,-` | everything else that has a label |
| triage | `watcher@riffmap-triage` | `projects/riffmap-triage.env` | `-` | cards with no agent label — to label them, never to work them |

The lists are complements by construction (the design and triage files source the build
file and reuse its `DESIGN_AGENTS`; `-` is named exactly once), so **no card can ever
belong to two lanes**. That is what makes the pidfile-per-instance enough of a lock:
lanes never want the same card, so they never race for one, and the `in-flight` label
goes on doing the courtesy job it always did. A project with no `LANE_AGENTS` is one lane
that takes every card — nothing about it changed.

A lane's project file can also set **`LANE_COMMAND`** (the slash command its tick runs,
default `/watch`) and **`LANE_WORKDIR`** (`card`, the default, runs the tick in a worktree
made for the card; `root` runs it in the main checkout with no worktree). The triage lane
is `LANE_COMMAND=/triage LANE_WORKDIR=root` with a small turn and dollar cap.

What a lane shares with its sibling: the board, the repo, the main checkout, the worktree
directory (one worktree per card, so never the same one), and `main`. What it does not:
its state (`attempts/`, `parked/`, `current`, `PAUSE`, the pidfile, the logs), its poll
clock, its sleeps. A card that changes agent label mid-life changes lanes, and starts its
two-attempt budget over in the new lane's state.

**The label is Steve's, but the bots put it there.** Steve writes cards in plain words
and usually leaves the `agent:` label off. An unlabeled card is the **triage lane's**: on
its next poll — within a minute, whatever the other lanes are doing — it runs `/triage`
(`commands/triage.md`), which reads the card, adds the one `agent:` label its words call
for (design-shaped asks — options, examples, mockups, a brief, a decision — to `designer`
or `architect`; a bug, a removal or a change to an approved design to the build agent for
that area), records which cards it waits on (below), leaves an FYI comment, and stops.
The lane that label names picks the card on its own next poll. A triage never claims the
card, never touches a file, and costs cents. A card it cannot place goes to Stuck with the
candidate agents as the lettered options. The spawner reads the relabel as a success for
the triage lane (the card is In progress but no longer its to pick), so nothing counts
toward an attempt.

A project without a triage lane keeps the older shape: the unlabeled card lands in the
build lane, and `/watch`'s own routing rule relabels a design-shaped one for the design
lane and stops — one short tick, counted as a success the same way.

The one thing concurrency adds for a tick is that **`main` can move under it**: the other
lane merges its own PRs. `/watch` merges `origin/main` into the branch before merging and
re-runs CI if that changed anything. A conflict where both sides only added — two entries
appended to the same list, two sections at the end of the same doc — the tick resolves
itself by keeping both, main's first, and lets CI judge the result; a conflict where a side
rewrote or removed something the other side touched is a Stuck card, never a hand-resolved
merge. Design PRs live in `docs/` and build PRs in the source tree, so either should be
rare; the additive kind is the one that does happen (riffmap#349, September 17, 2026).

**Two attempts, then Steve.** That rule is enforced by the spawner, not by the tick. A tick
that keeps retrying the same failing thing inside one run is wasting the budget; go to Stuck
instead. Steve moving a parked card back to In progress resets the count to zero.

## Dependencies

Steve can hand over several cards at once and the prerequisite is worked first. The
spawner asks GitHub, before it picks any row, which open issues the card is **blocked by**
— the issue's *Relationships* field. A card with one is skipped, whatever its priority,
and gets one FYI comment naming what it waits on (one more if that set changes); the poll
after the last blocker closes, it is picked. If the lookup itself fails the card is skipped
for that poll, never launched on a guess. The Waiting stage goes through the same check.

Where the relationships come from: Steve sets them by hand on the issue, or `/triage`
records them from the card's own words ("after #291", "once the radar redesign lands",
"depends on", a `Depends on` line — not "part of", "refs" or "see"). A card that already
has an agent label is never triaged, so a dependency on one of those is Steve's to set or
to write into the card before he labels it.

## Self-pull

Without it, the loop cannot start anything: every card waits on Steve's hand reaching
In progress or Waiting. `READY_PULL=1` in a project file makes **Ready** a third source.

- **Order is fixed:** In progress, then Waiting, then Ready. A card Steve moved by hand is
  always served before one the loop chose, whatever the priorities say.
- **`WIP_MAX` (default 2) is board-wide,** not per lane: it counts every open card in the
  In progress column, because that column *is* the work in flight. A lane stops promoting
  when the column is full, whoever filled it. It costs no extra API call — the count comes
  from the poll already done.
- **It is a soft cap**, like `in-flight` is a courtesy flag. Three lanes poll on three
  clocks, and read-then-promote is not a compare-and-set, so lanes polling in the same
  second can each promote against the same count. The overshoot is bounded by the number
  of lanes and self-corrects on the next poll.
- **Failures close.** A board with no Ready column makes `board list Ready` exit 1; that
  reads as "nothing to pull", never as a poll failure, and In progress is still served.
  A promote that fails means the tick is **not** launched — a claim on a card the board
  still calls Ready is a card nobody can see is being worked.
- **The tick is unaffected.** It is still handed exactly one card and still never pulls
  from Ready itself; choosing the card is the spawner's job, as it always was.

## State on disk

`~/.local/state/watcher/<project>/` on the box (`WATCHER_STATE`), one directory per lane
(`riffmap/`, `riffmap-design/` and `riffmap-triage/`):

| Path | Meaning |
|---|---|
| `spawn.pid` | the singleton lock |
| `PAUSE` | present = the spawner idles; `touch` to pause, `rm` to resume |
| `current` | the card the running (or crashed) tick holds; reconciled at the top of every poll |
| `attempts/<n>` | attempt count for card n; reset on a success that left In progress, or when Steve moves a parked card back |
| `parked/<n>` | the one Stuck comment for card n was posted |
| `parked-ok/<n>` | the move to Stuck actually landed |
| `waiting/<n>` | the FYI comment saying what card n waits on was posted, and for which blockers; cleared when it is picked |
| `logs/card-<n>-<ts>.json` | the tick's JSON result: `subtype`, `total_cost_usd`, `num_turns`, `result` |
| `logs/card-<n>-<ts>.stderr` | the tick's stderr |

## How a tick ends

| The card ends in | With `in-flight` | Meaning to the spawner |
|---|---|---|
| Done | removed | success; attempts reset |
| In review | removed | success; attempts reset |
| Stuck - Needs help | removed | success as far as the tick goes; attempts reset; Steve's turn |
| In progress, with THIS lane's label (or none) | removed | no progress; attempts +1; at 2 the spawner parks it |
| In progress, with ANOTHER lane's label | removed | success: the tick relabeled it for that lane (a triage, or `/watch` routing a design card); attempts reset, the other lane starts its own count |
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
| a run ended with the card's PR open, unmerged | the spawner's sweeper | ⚠️ `PR #52 is up, but the run ended before merging it`; the next poll merges it once green |

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

- One card per tick. One card at a time per lane; the other lane's card is not yours,
  in flight or not. Never pull from Ready yourself or tidy the board — which card you get
  is the spawner's decision, including when it takes one from Ready.
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
pauses every lane of the project, waits for each lane's running tick to finish, restarts
that lane's unit, un-pauses it. It never signals a tick. The watcher's own files live at `/opt/watcher` on the box; the spawner exports that path to
the tick as `WATCHER_HOME`, so `cat "$WATCHER_HOME/docs/talking-to-steve.md"` works from any
worktree.
