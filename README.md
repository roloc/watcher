# watcher

A headless Claude Code loop that turns a GitHub Projects board into merged pull requests.
One throwaway box, one systemd unit per project, no permission prompts.

**How it works.** `bin/spawn.sh` polls the board's **In progress** column once a minute, and
falls back to the **Waiting** stage and then — where `READY_PULL=1` — the **Ready** column,
so the loop can start a card nobody handed it. When
a card is claimable it creates a git worktree for it and runs one tick:
`claude -p "/watch <n>" --dangerously-skip-permissions --max-turns 400 --max-budget-usd 15`.
The tick claims the card, does the work in the worktree, opens a PR, waits for CI, runs
one review pass, merges, and moves the card to **Done**; anything it cannot decide goes to
**Stuck - Needs help** with the options written on the card. Two failed attempts park the
card. The spawner is also the stale-claim sweeper.

**Why a box.** Skipping permissions is only safe where there is nothing to protect. The box
holds a bot GitHub login, a Claude subscription token, and a checkout. If a tick misbehaves,
rebuild the box. On a developer's own machine the same loop needs allow lists, trust
probes and a classifier, and the day that was tried it cost more than the feature work.

## Layout

    bin/spawn.sh          the loop (systemd runs it); --dry-run / --once
    bin/board.py          the board CLI: list/show/move/stage/unclaim/notify (BOARD_OWNER, BOARD_NUMBER)
    bin/notify.py         Discord: posts card events through a webhook (DISCORD_WEBHOOK_URL); bin/test-notify.py checks the shapes
    bin/worktree.sh       one worktree per card; copies a project's private inputs in
    bin/test-spawn.sh     the spawner's cases, no network, every external command stubbed
    .github/workflows/ci.yml  `verify`: both suites plus a syntax pass, on every PR (see below)
    commands/watch.md     the /watch contract, installed for the watcher user (all projects)
    commands/triage.md    one triage tick: label an unlabeled card, record what it waits on, stop
    agents/reviewer.md    the one review pass: blocking findings only, one round
    docs/how-it-works.md  the map, written for the agents: pieces, board, life of a card, state
    docs/talking-to-steve.md  the format for anything a human reads (see below)
    docs/steve-cheat-sheet.md one page for Steve: columns, answering a Stuck card, pause, logs
    CLAUDE.md             rules for editing this repo
    projects/<name>.env   PROJECT_NAME, PROJECT_ROOT, BOARD_OWNER, BOARD_NUMBER, PROJECT_REPO,
                          WORKTREE_COPY_DIRS, and LANE_AGENTS for a lane
    deploy/watcher@.service, deploy/install.sh
    deploy/rollout.sh     deploy a new version without killing a running tick (see below)

A project keeps its own `CLAUDE.md`, `.claude/agents/` and CI; the watcher brings the loop.

**Lanes.** A project can run as several instances side by side, split by `agent:` label,
so a design card and a build card are worked at the same time: `projects/riffmap-design.env`
takes `architect,designer`, `projects/riffmap.env` takes `!architect,designer,-` — every
other labeled card — and `projects/riffmap-triage.env` takes `-`, the cards with no label,
only to label them: its tick is `/triage` (`LANE_COMMAND`, run in the main checkout with
`LANE_WORKDIR=root`), which reads the card, adds the one `agent:` label, records which
cards it waits on, and stops, so the right lane starts within a minute whatever the others
are doing. The lists are one list (`DESIGN_AGENTS`, defined once) plus `-` named once, so
no card can ever belong to two lanes and each instance's pidfile stays the only lock. Each
lane is its own unit (`watcher@riffmap`, `watcher@riffmap-design`, `watcher@riffmap-triage`)
with its own state directory; `rollout.sh` rolls them all. Details in
`docs/how-it-works.md`, "Lanes".

**Dependencies.** A card GitHub says is *blocked by* an open issue (the issue's
Relationships field, recorded by the tick that filed the card, set by Steve, or read by
`/triage` from the card's words) is skipped by every lane until the blocker closes, so a
batch of cards handed over at once is worked in order. With nothing recorded there, the
spawner reads the card's own body for an explicit `Depends on #<m>` and honors that the
same way — the net under a card that was filed already labeled, which `/triage` never
sees. Details in `docs/how-it-works.md`, "Dependencies".

**Self-pull.** `READY_PULL=1` in a project file lets a lane take the top **Ready** card
when In progress and Waiting are both dry, moving it to In progress before it launches.
`WIP_MAX` (default 2) is the ceiling, counted across the whole In progress column rather
than per lane, so the loop keeps a fixed number of cards moving and leaves the rest of
Ready alone. Off by default: starting work nobody handed over is a policy choice, not a
default. Order is always In progress, then Waiting, then Ready — a card Steve moved by
hand is never overtaken by one the loop chose. Details in `docs/how-it-works.md`,
"Self-pull".

**Stage.** Besides Status, the board carries a single-select `Stage` field (Waiting,
Claimed, Designing, Building, Testing, Reviewing, PR open, Merging, Parked). **Waiting is
the hand-off:** drag a card there on a Stage-grouped view and the spawner moves it to In
progress and picks it up (`board queue` lists them), so Status never has to be touched by
hand. The tick sets it at each
step with `board stage <n> <value>`; the spawner sets Parked when it parks a card. Status
says who owns the card, Stage says what is happening to it. A board view grouped by Stage
is the per-step column view; `board list` prints the stage as its fifth column.
A project-level `.claude/commands/watch.md` overrides the generic one. The generic one reads
the project's `CLAUDE.md` for the names it needs (the required CI check, contracts a builder
must not touch). The spawner exports `WATCHER_HOME` so a tick can read `docs/` from any
worktree.

## Talking to Steve

Steve is the only human in the loop, and he is dyslexic. Every message aimed at him — a
Stuck comment from the tick, a park comment from the spawner, a reviewer finding headed his
way — follows `docs/talking-to-steve.md`:

- **First line is the ask**, bold, starting `Needs Steve:` (or `FYI:` when nothing is needed).
- **Bullets, not paragraphs.** At most 8 above the fold, one fact each.
- **One question**, ending in `?`, with lettered options. Recommended option first.
- **How to answer**, always: reply A, B or C on the card, move it back to In progress.
- **Links he has to open** (a mockup, a screenshot, the PR) **go right above the question**,
  as a `Look:` list — the fold is collapsed on his phone.
- **Paths, logs and code go under `<details>`**, never above the fold.

He answers by replying on the card and moving it. Moving a parked card back to
**In progress** resets its attempt count. `docs/steve-cheat-sheet.md` is his one page.

## Discord

Set `DISCORD_WEBHOOK_URL` in `/etc/watcher/env` (a channel webhook; a secret, so it stays
out of git) and the board CLI posts to that channel after every change it makes: a card
changing column (📋), a card crossing into a new stage (🔧), a card shipped (✅), a run that
died (⚠️), and — the one that matters — a card landing in **Stuck - Needs help** (🚨, red,
quoting the ask from the card). Every post names the card and links to it, and to its PR
when there is one. Nothing is posted per poll or per tick. `DISCORD_MENTION` (`<@id>` or
`@here`) makes the Stuck alert ping someone. Unset the URL and it is all silent again.

## Set up a box (Debian 12 LXC, unprivileged, 4 cores / 8 GB)

    apt install git python3 curl gh                # plus node 22 and whatever the project builds with
    npm i -g @anthropic-ai/claude-code
    git clone https://github.com/roloc/watcher /opt/watcher   # or rsync it in
    /opt/watcher/deploy/install.sh                 # user, unit, /etc/watcher/env, symlinks, `board`

Then by hand, once:

1. **Claude login:** on a machine with a browser run `claude setup-token`, and put the printed
   value in `/etc/watcher/env` as `CLAUDE_CODE_OAUTH_TOKEN=…`. That file must not set
   `ANTHROPIC_API_KEY` — the key outranks the login and would bill the API instead.
   `DISCORD_WEBHOOK_URL=…` goes in the same file if you want the channel (see above).
2. **GitHub:** `su - watcher`, `gh auth login` as the bot account, `gh auth setup-git`. The bot
   needs write access to the repo and access to the Projects board.
3. **Project:** `deploy/install.sh riffmap` clones it under `/home/watcher/projects/`; drop its
   `.env.local` and any private inputs in place.
4. **Start:** `systemctl enable --now watcher@riffmap watcher@riffmap-design
   watcher@riffmap-triage` (the build, design and triage lanes — without the triage lane
   an unlabeled card is nobody's); `journalctl -u watcher@riffmap -f` follows one.
   The watcher's own lane is `watcher@watcher` (`deploy/install.sh watcher` first); see
   `docs/how-it-works.md`, "The watcher's own board".
   Pause a lane with `touch ~watcher/.local/state/watcher/riffmap/PAUSE`.

## CI

`.github/workflows/ci.yml`, one job — `verify`: `bash -n` over the scripts systemd and the
installer run, then `bin/test-spawn.sh` and `bin/test-notify.py`. Both suites are offline
and stubbed, so the job needs no network, no secrets and no services, and takes seconds.

**Where it runs.** PR code is code any branch can write, so it never runs on a box that
holds credentials — riffmap's split (its `ci.yml`, riffmap
[#407](https://github.com/roloc/riffmap/issues/407)), which here rules out exactly one box:
the `agents` LXC this repo deploys to, holding the bot's GitHub login and a Claude
subscription token. The repo variable `CI_RUNNER` picks the runner. It is **`watcher-ci`**
(since September 28, 2026): the `ci-watcher` runner on the `ci` LXC (108), in
`/opt/actions-runner-watcher` beside riffmap's, run by the same `gha-runner` user. It needs
nothing beyond bash and python3.

- **Each repo needs its own runner registration.** Repos in a user account cannot share
  runners, so riffmap's `riffmap-ci` never picks up this repo's jobs; pointing
  `CI_RUNNER` at it queues every check, and with `verify` required, nothing merges.
- **If the ci box is down**, unset the variable (`gh variable delete CI_RUNNER`) and the
  job falls back to GitHub-hosted until it is back. That costs metered minutes, so set it
  again as soon as the box is up.
- Never point it at the agents box.

Deploying is deliberately not a CI job — see below: `rollout.sh` waits for a running tick to
finish on its own, which is a decision about live work, not a pipeline step.

## Deploy a new version (without killing a running tick)

    /opt/watcher/deploy/rollout.sh riffmap            # main
    /opt/watcher/deploy/rollout.sh riffmap <branch>   # a branch, before it merges

It pulls the code, pauses the spawner, **waits for the running tick to finish on its own**,
restarts the unit, and un-pauses. It never signals a tick. The new `/watch`, reviewer and
docs are live for the next tick as soon as the pull lands; only `spawn.sh` itself needs the
restart. If a tick is still running after 170 minutes it gives up, leaves the spawner
paused, and tells you to run it again.

## Add a project

Add `projects/<name>.env`, run `deploy/install.sh <name>`, `systemctl enable --now
watcher@<name>`. The project needs a board with the columns
`Backlog | Ready | In progress | In review | Done | Stuck - Needs help`, an `in-flight`
label, and CI that is a required check on `main`.

Nothing here is riffmap-shaped, but two settings are worth knowing before the first run:

- **`PROJECT_REPO` is the repo to clone**, and the installer uses it. The instance name is
  a watcher-side label, so the repo may be named differently, and the board's owner need
  not own the code. Set it whenever `<name>` is not literally the repo under `BOARD_OWNER`.
- **`WORKTREE_COPY_DIRS`** names the project's gitignored, in-tree private inputs —
  space-separated, relative to the checkout — copied into every card worktree so a tick can
  read them (riffmap's meeting transcripts are the default). A project with none sets
  `WORKTREE_COPY_DIRS=""`; a project with others sets
  `WORKTREE_COPY_DIRS="docs/fixtures data/seed"`. A directory that is not there is skipped.

To split it into lanes, add
`projects/<name>-<lane>.env` that sources `<name>.env` and sets `PROJECT_NAME=<name>-<lane>`
and `LANE_AGENTS`, give `<name>.env` the complementary `!` list, and enable
`watcher@<name>-<lane>` too; `rollout.sh <name>` finds the lane by the file name.
