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
| **Discord** | `bin/notify.py` | imported by the board CLI | posts to Steve's channel when a card is stuck or something broke, and for nothing else — when a webhook is configured |
| **Metrics** | `bin/metrics.py` | run by hand, on the box | read-only report over a lane's own logs: first-pass merge rate, park rate, deaths, turns/quota and time to Done, per week |
| **Floor page** | `bin/floor.py` | `watcher-floor.service`, always on | live, read-only page: what every lane is working on, idle, needing Steve, paused or asleep after a failure, plus recent ticks across every project; a Peek button on every lane's desk opens that lane's session — live while it works, its last one when it does not — decoded the same way `bin/tail.sh` decodes it; the ship station's Peek lists every step CI ran on main's newest commit, the commits before it, and the deployer's last lines (from its `history` file). Each board also gets a **Needs Steve strip**: every card Stuck on that board, whichever lane last touched it (not only the one a lane's own desk happens to show), with a carousel of every picture its Needs Steve comments have shown — flip through with arrows, a swipe or the arrow keys, lettered by direction when the comment named one, newest round first, older rounds reachable past the last picture and marked with their own date. A **Studio** button shows the same data as one room, one row per board with its name on it: a lane stays in its board's row (at its desk working, beside it with a mug when idle, hands up when paused) and walks to the couch when asleep or to the podium by the door when it needs Steve, holding up the carousel's current picture; tap a character for a speech bubble, tap the bubble for its full Peek. `?demo=1` cycles a fixture through every state, for development and screenshots |
| **A browser** | `bin/browse.py` | run by a tick or by hand | headless Chrome, for looking at a page the way its reader does: `check` (did the page's own code fail?), `text`, `shot`. `curl` fetches what the server sent; a page that draws itself is only seen by running it |
| **Steve** | a human | GitHub, on his phone | moves cards, answers Stuck cards, owns every product decision |

Steve is the only human. Everything else is a bot. The box the bots run in is throwaway and
holds nothing worth protecting beyond a bot GitHub login and a Claude token, which is why
the tick can run with permissions skipped.

## The board

Columns, in order: `Backlog | Ready | In progress | In review | Done | Stuck - Needs help`.

| Column | Who moves cards in | Meaning |
|---|---|---|
| Backlog | Steve, or a tick returning a card it declined (`watch.md` §4b) | **never pulled from.** Where a self-gated card waits until Steve moves it to Ready |
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
- **`awaiting-rebuild`** (card #85) — the tick merged this card's fix and found the artifact
  gated on a known downstream rebuild (`commands/watch.md` §3): it left this label on and
  ended, instead of looping on the artifact or parking in Stuck with nothing to ask. The
  spawner skips the card while the label is on (one FYI comment, no attempt counted) and
  picks it up again once a project's own workflow removes it — see "The awaiting-rebuild
  sweep" below for the stale-label safety net.
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
   Status stays truthful and the floor page shows the card starting. See "Self-pull" below.
3. **A worktree** `../<repo>-worktrees/card-<n>` is created (or reused) on branch `card-<n>`.
4. **The tick starts:** `claude -p "/watch <n>" --dangerously-skip-permissions --max-turns 400
   --max-budget-usd 15`, with a 150 minute watchdog that kills the whole process group.
5. **The tick** claims the card (comment first, label second), reads the card and the
   project's `CLAUDE.md`, does the work, opens a PR with `Closes #<n>`, waits for CI, runs
   the reviewer once, merges. Or it moves the card to In review, or Stuck, or labels it
   `awaiting-rebuild` and leaves it In progress because the fix is merged but a known
   downstream rebuild has to finish first, or leaves it in In progress if it got nowhere.
   It removes `in-flight` on every one of those exits.
6. **The spawner's exit handling** reads the card back. If `in-flight` leaked, it unclaims
   and leaves a sweeper comment. A success that left In progress — including the
   `awaiting-rebuild` exit — resets the attempt count. Anything else bumps it; **two
   attempts with the card still In progress parks it in Stuck** with a comment. A closed
   card gets its Stage emptied and its worktree removed.
7. **Sleep:** 60 seconds after a normal poll, 10 minutes after a failed tick, 5 minutes
   after a board outage.

## The In review sweep

The spawner's input is In progress, so In review used to be a column nothing read: a card
there waited for Steve to notice it, and a move to In review is not an alert (it posts
nothing to Discord). Two riffmap cards sat there for most of a day on September 17, 2026
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

## The closed-card Stage sweep

The Stage field says what a tick is doing, so it means nothing once the card is closed —
and a Done card that still reads "Parked" or "Merging" sits in the wrong group on the
Stage-grouped view Steve works from. `handle_exit` empties the Stage of the card a tick
was holding when it closed (step 6 above), which covers a tick that merges its own PR.
It does not cover **a card that closes with no tick on it**: Steve merging the PR of a
card parked in Stuck, or closing a card by hand. Five cards on the watcher's own board
sat in Done reading "Parked" that way (watcher [#35](https://github.com/roloc/watcher/issues/35),
[#36](https://github.com/roloc/watcher/issues/36), [#40](https://github.com/roloc/watcher/issues/40),
[#43](https://github.com/roloc/watcher/issues/43), [#44](https://github.com/roloc/watcher/issues/44),
September 28, 2026).

So every five minutes (`STAGE_SWEEP_SEC`, an env knob like the In review limits) each
lane's spawner runs `board sweep-stages`: one board read, and the Stage emptied on every
closed card that still has one, at most 20 a run. It is quiet (no Discord post), it is
board-wide rather than per lane (a second lane's sweep finds nothing left), and it is
best-effort: a sweep that fails is one line in the journal, waits for its next turn, and
never costs the poll. An open card is never touched — a parked card stays Parked.

## The awaiting-rebuild sweep

A tick that merges a fix finds, sometimes, that the artifact cannot show it yet for a known
reason: the project's own `CLAUDE.md` names a downstream rebuild that has to finish first,
off this box, in its own time. `commands/watch.md` §3 gives that one case its own exit
instead of Stuck or a wasted re-poll inside the tick's own window: label the card
`awaiting-rebuild`, remove `in-flight`, leave it In progress, stop (riffmap
[#602](https://github.com/roloc/riffmap/issues/602), October 8, 2026, is why — a tick that
re-checked a 32-meeting rebuild a few times inside one run, then parked asking Steve
"should I keep checking back?", for a wait that needed nobody). `pick_card` skips a card
carrying that label (one FYI comment the first poll it sees it, no attempt counted), until
a project's own workflow removes it when the rebuild ends (riffmap
[#637](https://github.com/roloc/riffmap/issues/637)) — then the next poll hands it to a
tick to re-check the artifact and close or park on what it now shows.

That workflow runs off this box, so the wait cannot be open-ended: every poll, `spawn.sh`'s
`sweep_awaiting_rebuild` parks a card that has carried the label for more than
`AWAITING_REBUILD_STALE_SEC` (default 12 hours) in Stuck — a dead notifier, a rebuild that
silently never ran, or a label dropped by hand with nobody re-checking the card. Its own
comment, not `park_stuck`'s: "failed 2 attempts" would be a lie here, and attempts was
reset to 0 the moment the label went on. State is `awaiting-rebuild/<n>.since` (when the
wait was first seen) and `.noted` (the one FYI comment went out); both are cleared when the
card is picked normally or parked for staleness.

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

**`LANE_CHANNEL=canary`** (card #46) makes a lane run the watcher's own **second**
checkout, `/opt/watcher-canary`, instead of the one every other lane shares — so a bad
prompt reaches only the board that opted in before it reaches everyone else. `bin/spawn.sh`
itself carries the mechanism: right after it reads the lane's project file, a canary lane
re-execs into `/opt/watcher-canary/bin/spawn.sh` with its own `CLAUDE_CONFIG_DIR` (so a
personal or project `commands/`/`agents/` cannot win over the checkout under test, the same
trick `bin/eval.sh` uses) and a freshly-computed `WATCHER_HOME`. Same systemd unit, same
pid — exec replaces the process image, not the process — so nothing about the pidfile lock
or the deployer's own cgroup lookup changes; the only thing a canary lane does differently
is which checkout's code it is actually running. `deploy/autodeploy.sh`'s own header
comment documents the canary stage: it moves that second checkout onto a commit and
decides when the soak is done, restarting only the canary lane(s) until it clears; a
project with no canary lane is entirely unaffected. `projects/watcher.env` is the first
one to opt in (see "The watcher's own board", below).

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
card, never touches a file, and uses cents' worth of quota. A card it cannot place goes to Stuck with the
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

## The watcher's own board

The watcher is also a project: `watcher@watcher` (`projects/watcher.env`) is one lane,
with no `LANE_AGENTS`, over the [Watcher board](https://github.com/users/roloc/projects/2)
and this repo. Nothing about the spawner or `/watch` is special-cased for it; what differs
lives where every project's differences live, in its `CLAUDE.md` ("When the watcher works
its own board"). The three things that make a loop editing itself safe enough:

- **Two copies.** The lane's `PROJECT_ROOT` is `/home/watcher/projects/watcher`, a clone
  of its own. `/opt/watcher` is what every lane runs. A tick edits the clone, never the
  running copy.
- **The deploy is the gate.** A merge lands on `main`; `main` goes live only through
  `deploy/autodeploy.sh`, which ships a commit once `verify` passed on it, the replay suite
  passed on it in a scratch clone (the new prompts, tested before any lane uses them), and
  every lane on the box is idle. One deploy moves `/opt/watcher` for every project.
- **The gate is reviewed hardest, then merged like everything else.** A change to CI, the
  reviewer, the merge rules, the hard rules below, anything under `deploy/`, or the replay
  suite always gets the reviewer, which blocks anything that weakens it, and then merges on
  green like any card — Steve never merges, the same as riffmap (September 28, 2026). Until
  that day these parked in Stuck for Steve, who does not review code, so the park added a
  wait and no review.

`WIP_MAX=1` there: one watcher card in flight at a time, so two changes to the loop's rules
never race each other to `main`.

Since card #46, `projects/watcher.env` also sets `LANE_CHANNEL=canary` (see "Lanes",
above): a commit that touches what a model reads lands on the Watcher board's own lane
first, soaks there, and only then reaches every other project's. The systemd unit still
starts `/opt/watcher/bin/spawn.sh`; that copy's own re-exec shim is what hands the tick off
to `/opt/watcher-canary`, so "one deploy moves `/opt/watcher` for every project" above is
still true of the *entry point* — a canary lane just runs a second checkout once it is in.

## Dependencies

Steve can hand over several cards at once and the prerequisite is worked first. The
spawner asks GitHub, before it picks any row, which open issues the card is **blocked by**
— the issue's *Relationships* field. A card with one is skipped, whatever its priority,
and gets one FYI comment naming what it waits on (one more if that set changes); the poll
after the last blocker closes, it is picked. If the lookup itself fails the card is skipped
for that poll, never launched on a guess. The Waiting stage goes through the same check.

Where the relationships come from, best first: the tick that **files** the card records it
in the same turn it writes the Depends-on sentence (`watch.md`, section 2); Steve sets one
by hand on the issue; or `/triage` records it from the card's own words ("after #291",
"once the radar redesign lands", "depends on", a `Depends on` line — not "part of", "refs"
or "see").

**And the net, for the cards none of those three reached.** `/triage` only ever sees a card
with no `agent:` label, so a card filed with one — which is most of what a design tick
spins off — was never triaged, and if its author did not record the relationship, nothing
did. So when the Relationships field is **empty**, the spawner reads the card's own body
(`body_blockers`) and treats an explicit prerequisite there exactly like a recorded one:
skipped, one FYI comment, picked the poll after the last one closes. The comment says the
dependency came from the card's words and that editing them undoes it.

The reader is deliberately narrow, because a false positive stalls a card nobody is
watching. Only `depends on`, `depend on`, `blocked by`, `blocked on`, `waits on` and
`waiting on`, with nothing between the phrase and the reference but whitespace and the
markdown that dresses it up — `**Depends on:** [#12](…)`, the shape a project's own card
template tends to print, counts, and so does the plain one. Only `#12`, `[#12](…/issues/12)` or
that URL, and only in **this** repo; a list after one phrase (`#12 and #13`) is read whole;
a phrase negated in front (not, never, no longer, and the contractions — "isn't blocked by
#12" says what "is not blocked by #12" says) is ignored, as is a card naming itself.
"After #12", "once #12 lands" and "needs #12 first" are **not** matched here even though
`/triage` records them — a model reading the sentence can tell those from ordinary prose;
a regex cannot. And it only ever reads: the relationship is never written from a guess,
because a guess on the issue outlives the sentence that caused it.

Across every card riffmap has ever filed, that reader finds 38 dependency statements and
no false positive.

A recorded relationship is believed on its own and the body is not even fetched, so the
common path costs what it always did. Everything here fails closed like the first lookup —
an unreadable body, an unreadable state, or more than ten references in one body (a body
this was not built to read is not answered from its first ten) skips the card for that poll
rather than launching it. The **one** exception is a reference to an issue that does not
exist: a typo can block nothing, and a permanent 404 treated like a transient outage would
skip the card silently for ever, so it is dropped with a line in the journal naming it.

(riffmap [#447](https://github.com/roloc/riffmap/issues/447), September 20, 2026, is why
this exists: three cards filed together at 02:50, each naming the one before it in its
body, none recorded. The third was picked thirteen hours later and spent its whole tick
rediscovering by hand what its own first line said.)

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

## The dollar figure is quota, not a bill

**Nobody is invoiced for a tick.** The box authenticates with `CLAUDE_CODE_OAUTH_TOKEN`
— a Claude **subscription** login — and `/etc/watcher/env` is forbidden from carrying
`ANTHROPIC_API_KEY`, precisely because the key would outrank the login and bill the API
instead (`README.md`, "Set up a box", step 1).

The same login reaches what a tick runs **from its shell**. Claude Code strips
`CLAUDE_CODE_OAUTH_TOKEN` from the environment of every Bash tool call, so until October 8,
2026 a `claude` the tick ran itself — a project's pipeline on the subscription backend, a
`claude -p` probe, an eval's before/after — answered `Not logged in · Please run /login`
while the tick around it was logged in, and every card whose work needed such a run parked
itself in Stuck as "this box isn't logged into the model" (riffmap#602; watcher#82). Now
`bin/spawn.sh` puts `bin/shim/claude` first on the tick's `PATH`: when the variable is
missing it re-reads **only** the token from `/etc/watcher/env` (a file the watcher user could
already read) and execs the real binary, resolved by the spawner before the shim's directory
went on `PATH` so it can never find itself. A tick needs to do nothing — `claude` just works.

So every dollar amount the loop reports is **imputed**: `total_cost_usd` in a tick's JSON
result is copied from `modelUsage.<model>.costUSD`, which is **tokens × API list price,
computed client-side**. It is what the run *would* have cost on the API. It is a **usage
meter denominated in dollars**, and the subscription is what actually paid.

`--max-budget-usd` is the same number as a ceiling. It is still worth having — it stops a
tick that is looping — but it is a **quota ceiling, not a spending limit**.

**Say "quota", never "cost" or "spend", for this number.** A tick reports
`Quota: ~$<n> equivalent (subscription — not billed)`. The words *cost*, *spend* and
*money* are reserved for the one thing that can really be billed: **a project calling a
model on its own API key**, through its own env file. That is a separate bill with no
enforced cap, and a project whose lane pins a subscription backend (riffmap pins
`RIFFMAP_BACKEND=claude`) has no such path at all.

Why this is written down: the loop's own reports said "cost" and "spend" for two months,
and everything reading them — agents and humans alike — repeated it back to Steve as
money he had been charged (September 19, 2026). The number was never a bill.

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
| `stage-sweep.last` | when this lane last ran the closed-card Stage sweep (epoch seconds) |
| `waiting/<n>` | the FYI comment saying what card n waits on was posted, and for which blockers; cleared when it is picked |
| `awaiting-rebuild/<n>.since` | when a card first carried `awaiting-rebuild` was seen (the stale-label sweep's clock); cleared when it is picked normally or parked for staleness |
| `awaiting-rebuild/<n>.noted` | the one FYI comment for that wait was posted; cleared the same way |
| `logs/card-<n>-<ts>.json` | the tick's JSON result: `subtype`, `total_cost_usd` (quota, not a bill — see above), `num_turns`, `result` |
| `logs/card-<n>-<ts>.stderr` | the tick's stderr |

## How a tick ends

| The card ends in | With `in-flight` | Meaning to the spawner |
|---|---|---|
| Done | removed | success; attempts reset |
| In review | removed | success; attempts reset |
| Stuck - Needs help | removed | success as far as the tick goes; attempts reset; Steve's turn |
| Backlog | removed | the tick **declined** a card that gates itself (`watch.md` §4b): success, attempts reset. Nothing pulls from Backlog, so it waits for Steve to move it to Ready again |
| In progress, labeled `awaiting-rebuild` | removed | success: the fix is merged but gated on a known downstream rebuild (`watch.md` §3); attempts reset. `pick_card` skips it until a project's workflow drops the label, or the stale-label sweep parks it after 12 hours |
| In progress, with THIS lane's label (or none) | removed | no progress; attempts +1; at 2 the spawner parks it |
| In progress, with ANOTHER lane's label | removed | success: the tick relabeled it for that lane (a triage, or `/watch` routing a design card); attempts reset, the other lane starts its own count |
| any | still there | the tick died; the spawner unclaims, comments, attempts +1 |

`claude -p` result subtypes the spawner reads: `success`; `error_max_turns`;
`error_max_budget_usd`; `error_during_execution`; a killed tick shows exit 143.

## Discord

Steve does not sit on the board; he gets pinged — **only when a card is stuck or
something broke.** When `DISCORD_WEBHOOK_URL` is set (in `/etc/watcher/env` for the box, or
in a project's env file), `board.py` posts to the channel **after** a change lands, so the
board is always the record and Discord only ever repeats it. Every post names the card and
links to it.

| Event | Posted by | Looks like |
|---|---|---|
| **a move to Stuck** | the tick (comment first, then move), the spawner's park, or the deployer's held card | 🚨 **Needs Steve:** the ask, quoted from the card, red, with the PR when there is one; `DISCORD_MENTION` pings someone |
| a run died holding the card | the spawner's sweeper or reconciliation | ⚠️ `the run ended without finishing`; the next poll may retry |
| the product detector filed a card | `bin/detect.sh`, after the card lands in Backlog | 📉 **Something broke:** the metric, the reading and the average, and the page to look at |

That is the whole list. A card changing column, crossing into a new stage, shipping, or
being closed by hand posts nothing, and neither does a run that ended with its PR open
(the next poll merges it): the floor page shows all of that live. Until October 1, 2026
every one of them posted, and Steve asked for the channel back: "Now that we have the
floor page I don't think we need to be so verbose to discord. Can we only hit the discord
when something is stuck or broken?" A channel that repeats every step is a channel nobody
is reading when the alert comes.

Nothing is posted per poll, per tick, or for a quiet tick. A post that fails is one line
on stderr and nothing else — a notification can never fail a board command or a tick.
`python3 bin/test-notify.py` checks the shapes offline; `python3 bin/notify.py stuck 1
"smoke test" https://example.com` sends one by hand.

## Watching the product

The spawner's sweeps keep the *loop* healthy. Nothing above watches the product itself — a
regression that ships to a project's own staging box waited for Steve to notice it, until
card [#36](https://github.com/roloc/watcher/issues/36). `bin/detect.sh` closes that gap the
same way the deployer closes its own: a small script on a timer, no model, deterministic.

- **Generic in the watcher, specific in the project.** `bin/detect.sh <project>` does
  nothing unless `projects/<project>.detect.env` exists — a versioned config, sourced after
  the project's own `.env`, naming one metric (`DETECT_METRIC_CMD`, a command that prints
  one number), a rolling window, and two fixed bands (`DETECT_SMALL_BAND`,
  `DETECT_LARGE_BAND`). One reading a run; what gets compared to the bands is the *rolling
  average* of the last `DETECT_BASELINE_WINDOW` readings, so one noisy reading is never a
  breach on its own.
- **Tiers.** Below the small band: nothing. At or above the small band: one log line,
  nothing else. At or above the large band: a card, filed in **Backlog** with the metric,
  the rolling average, the readings behind it, and (best-effort) the commit on `main` at
  the time — evidence, not a diagnosis. **No second card while the first stays open** —
  closing it, however Steve closes it, is what allows the next one.
- **The card is written like one of Steve's**, with no `agent:` label, so a project with a
  triage lane routes it the same as anything else he might have filed. It names the
  artifact that shows the problem (`DETECT_LOOK_URL`, when the project has one) — the same
  "look before you diagnose" rule `commands/watch.md` §2 holds every tick to.
- **Discord stays a repeater.** `bin/detect.sh` never talks to Discord itself; once the
  card is in Backlog it calls `board notify <n> breach`, so the 📉 post happens because a
  card landed, never because a check ran, and never when the card could not be put on the
  board. Nothing is posted for a small breach, or for a clean run.
- **Opt-in, per project, off until its config exists.** `deploy/install.sh` installs
  `watcher-detect@.service`/`.timer` for every box, then enables `watcher-detect@<p>.timer`
  for every `projects/<p>.detect.env` it finds (the `.service` carries no `[Install]` — it
  is driven by the timer, the same shape as `watcher-deploy.service`/`.timer`). A project
  opts in by merging its `.detect.env`: `install.sh` reruns on every deploy
  (`deploy/autodeploy.sh`), so that merge is what turns detection on — nobody runs a
  command on the box. `bin/test-detect.sh` is the offline suite — a stubbed metric source
  crossing each band, run against stubbed `gh`/`board` — and CI runs it the same as the
  other two.

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
- **A fact is not a product decision.** What drew, what the value was, whether it
  reproduces — go and look, including at the project's own live data where its `CLAUDE.md`
  says how. Ask Steve for judgment, never for something a machine recorded, and never mix
  the two in one card.
- **A symptom card is not done until its symptom is checked again.** Fetch the artifact the
  card names one more time before the card closes, and say in one sentence what it now
  shows. Still there? The card does not close, however green the build, and however good
  the reason it hasn't moved (deploy-gated, cached, can't restart from here) — the reason
  explains the artifact, but it is never itself evidence the symptom is gone. The PR says
  `Refs #<n>` and the card goes to Stuck. A correct fix for the wrong cause is what this
  catches. GitHub's merge-time close scan matches a closing keyword next to the card's
  number wherever that pairing sits in the body, meaning included or not — so a card
  staying open means that keyword and the number never appear next to each other in the
  PR body, not even inside a sentence explaining why the other keyword was chosen.
- **A spawned card re-reads the complaint at the root of its chain**, not only the card that
  filed it, and says which sentence of the root it is serving. When the last card off a root
  closes, the root's own symptom is checked again and the answer is commented on the root.
- **What a card asks for lands where the person who asked can open it.** The plan's first
  line says what Steve gets and where; a command on the box is never the deliverable for
  someone who reads the board on his phone. When a card can be read two ways that land in
  different places, build the one he can open; only when he could open both is it a
  question, and then it is asked before building, not after.
- **A screen is run before it ships, and looked at in a browser when it is reported
  broken.** A fetch is not a look: the server answering says nothing about what was drawn.
- Report the run's quota use, and never call it cost or spend (see "The dollar figure is
  quota, not a bill").

## What a project brings, what the watcher brings

The watcher brings the loop: the spawner, `/watch`, the reviewer, the board CLI, worktrees
— and **a browser** (`"$WATCHER_HOME/bin/browse.py"`), so a tick on any project can load a
page the way a person does without the project building or installing anything. A project
with a screen only says where its pages are and how to serve a branch's version.
A project keeps its own `CLAUDE.md` (hard rules, the required CI check, standing traps),
its own `.claude/agents/`, its own CI, and can override `/watch` with its own
`.claude/commands/watch.md`. The generic `/watch` reads the project's `CLAUDE.md` for the
names it needs (the CI check, the contracts it must not touch, the spend receipt if any).
`templates/project-CLAUDE.md` (card #39) is that section with a blank for each one, built
straight from what `/watch`, `/triage` and the reviewer read; `deploy/add-project.sh` and
`bin/doctor.sh` are the rest of putting a project on the watcher — see README, "Add a
project".

Deploying a new watcher version happens by itself: `watcher-deploy.timer` runs
`deploy/autodeploy.sh`, which ships `main` when `verify` and the replay suite pass on the
commit and every lane is idle (README, "Deploys happen by themselves"). A held commit is a
Stuck card on the Watcher board. `deploy/rollout.sh` is the manual fallback for a session;
it stops the deployer's timer while it runs. Neither ever signals a tick. The watcher's own files live at `/opt/watcher` on the box; the spawner exports that path to
the tick as `WATCHER_HOME`, so `cat "$WATCHER_HOME/docs/talking-to-steve.md"` works from any
worktree.
