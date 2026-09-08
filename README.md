# watcher

A headless Claude Code loop that turns a GitHub Projects board into merged pull requests.
One throwaway box, one systemd unit per project, no permission prompts.

**How it works.** `bin/spawn.sh` polls the board's **In progress** column once a minute. When
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
    bin/board.py          the board CLI: list/show/move/unclaim (BOARD_OWNER, BOARD_NUMBER)
    bin/worktree.sh       one worktree per card; copies a project's private inputs in
    bin/test-spawn.sh     86 cases, no network, every external command stubbed
    commands/watch.md     the /watch contract, installed for the watcher user (all projects)
    agents/reviewer.md    the one review pass: blocking findings only, one round
    docs/how-it-works.md  the map, written for the agents: pieces, board, life of a card, state
    docs/talking-to-steve.md  the format for anything a human reads (see below)
    docs/steve-cheat-sheet.md one page for Steve: columns, answering a Stuck card, pause, logs
    CLAUDE.md             rules for editing this repo
    projects/<name>.env   PROJECT_NAME, PROJECT_ROOT, BOARD_OWNER, BOARD_NUMBER
    deploy/watcher@.service, deploy/install.sh
    deploy/rollout.sh     deploy a new version without killing a running tick (see below)

A project keeps its own `CLAUDE.md`, `.claude/agents/` and CI; the watcher brings the loop.

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
- **Paths, logs and code go under `<details>`**, never above the fold.

He answers by replying on the card and moving it. Moving a parked card back to
**In progress** resets its attempt count. `docs/steve-cheat-sheet.md` is his one page.

## Set up a box (Debian 12 LXC, unprivileged, 4 cores / 8 GB)

    apt install git python3 curl gh                # plus node 22 and whatever the project builds with
    npm i -g @anthropic-ai/claude-code
    git clone https://github.com/roloc/watcher /opt/watcher   # or rsync it in
    /opt/watcher/deploy/install.sh                 # user, unit, /etc/watcher/env, symlinks, `board`

Then by hand, once:

1. **Claude login:** on a machine with a browser run `claude setup-token`, and put the printed
   value in `/etc/watcher/env` as `CLAUDE_CODE_OAUTH_TOKEN=…`. That file must not set
   `ANTHROPIC_API_KEY` — the key outranks the login and would bill the API instead.
2. **GitHub:** `su - watcher`, `gh auth login` as the bot account, `gh auth setup-git`. The bot
   needs write access to the repo and access to the Projects board.
3. **Project:** `deploy/install.sh riffmap` clones it under `/home/watcher/projects/`; drop its
   `.env.local` and any private inputs in place.
4. **Start:** `systemctl enable --now watcher@riffmap`; `journalctl -u watcher@riffmap -f`.
   Pause with `touch ~watcher/.local/state/watcher/riffmap/PAUSE`.

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
