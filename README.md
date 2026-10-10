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
    bin/board.py          the board CLI: list/show/move/stage/unclaim/notify/dump/ask (BOARD_OWNER, BOARD_NUMBER)
    bin/notify.py         Discord: posts through a webhook (DISCORD_WEBHOOK_URL) when a card is stuck or something broke; bin/test-notify.py checks the shapes
    bin/worktree.sh       one worktree per card; copies a project's private inputs in
    bin/doctor.sh         read-only sanity check that a project is wired up; deploy/install.sh and
                          bin/spawn.sh both run it before a lane's first tick (see "Add a project")
    deploy/add-project.sh the dozen hand steps of putting a new project on the watcher, as one
                          idempotent script — run from Steve's Mac, never by a tick (see below)
    templates/project-CLAUDE.md  the section a project's own CLAUDE.md needs for /watch to run
                          against it, built from what watch.md/triage.md/reviewer.md actually read
    bin/detect.sh         the product detector: a metric, a rolling baseline, fixed bands (see below)
    bin/floor.py          the floor page: what every lane on the box is doing, live and read-only (see below); bin/test-floor.py checks it offline
    bin/browse.py         a real browser for every tick on every project: check / text / shot a page (see "Looking at pages")
    bin/test-floor-browser.py  the floor page loaded and clicked in that browser; also the worked example of a suite built on it
    bin/test-spawn.sh     the spawner's cases, no network, every external command stubbed
    bin/test-detect.sh    the detector's cases, offline, every external command stubbed
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
    deploy/autodeploy.sh  THE deploy path: ships main by itself when every lane is idle (see below)
    deploy/watcher-deploy.service, .timer   run it every five minutes
    deploy/watcher-floor.service  the floor page's unit; install.sh restarts it on every install
    deploy/rollout.sh     the manual fallback, for a Claude session (see below)
    bin/test-deploy.sh    the deployer's cases, offline, every external command stubbed
    deploy/watcher-detect@.service, .timer  bin/detect.sh, per project, opt-in (see below)
    projects/<name>.detect.env   opt-in per-project config for bin/detect.sh (see below)

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
with its own state directory; a deploy restarts them all. Details in
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

## The floor page

**What every lane on the box is doing, without ssh or journalctl.** `bin/floor.py`
(card [#40](https://github.com/roloc/watcher/issues/40)) is a small stdlib HTTP server,
always on (`watcher-floor.service`), that groups every lane in `projects/*.env` by the
board it works and shows one of five states per lane — working (the card, its Stage,
minutes since launch), idle (why), needs Steve (the card and the first line of its ask),
paused, or sleeping after a failure — plus the last few ticks across every project. Each
board's lanes read in SDLC order — the triage lane drawn as a smaller dispatcher, then
design, then build — and end at a **ship station**: the check runs on the newest `main`
commit (`gh api`, REST only), and on the board whose repo is this checkout, the deployer's
state from `/var/lib/watcher-deploy` (live, checking, testing, waiting, swapping, held — a
held commit counts as needing Steve). It reads `projects/*.env`, each lane's state
directory, one `board.py dump` per board and two REST calls per project (all cached,
refreshed about once a minute); it never changes anything. Read-only, LAN-only
(reached off-site over WireGuard), runs as the unprivileged `watcher` user.
`bin/test-floor.py` is its offline suite: fixture state directories, a stubbed board.

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
out of git) and the board CLI posts to that channel when a card is stuck or something
broke, and for nothing else: a card landing in **Stuck - Needs help** (🚨, red, quoting the
ask from the card), a run that died holding a card (⚠️), and a card the product detector
filed (📉). Every post names the card and links to it. A card changing column, crossing
into a new stage or shipping posts nothing — the floor page shows those — and nothing is
posted per poll or per tick. `DISCORD_MENTION` (`<@id>` or `@here`) makes the Stuck alert
ping someone. Unset the URL and it is all silent again.

## Watching the product

The loop above watches itself — stale claims, In review, parked cards. `bin/detect.sh`
watches the *product*: a per-project, opt-in timer that reads one metric
(`projects/<name>.detect.env`), keeps a rolling baseline, and compares it to two fixed
bands. Below the small band, nothing; at or above it, one log line; at or above the large
band, one card filed in **Backlog** with the evidence — no model, and no second card while
the first stays open. Off until a project opts in: `deploy/install.sh` installs the unit
for every box, then enables an instance for every `projects/<name>.detect.env` it finds —
merging that file, on a rerun of `install.sh` or the next deploy, is what turns detection
on. Details in `docs/how-it-works.md`, "Watching the product".

## Set up a box (Debian 12 LXC, unprivileged, 4 cores / 8 GB)

    apt install git python3 curl gh chromium       # plus node 22 and whatever the project builds with
    npm i -g @anthropic-ai/claude-code
    git clone https://github.com/roloc/watcher /opt/watcher   # or rsync it in
    /opt/watcher/deploy/install.sh                 # user, unit, /etc/watcher/env, symlinks, `board`

Then by hand, once:

1. **Claude login:** on a machine with a browser run `claude setup-token`, and put the printed
   value in `/etc/watcher/env` as `CLAUDE_CODE_OAUTH_TOKEN=…`. That file must not set
   `ANTHROPIC_API_KEY` — the key outranks the login and would bill the API instead.
   `DISCORD_WEBHOOK_URL=…` goes in the same file if you want the channel (see above).
   That one file logs in the tick *and* anything the tick runs from its shell: Claude Code
   strips the token from every Bash tool call's environment, so `bin/shim/claude` — first
   on the tick's `PATH` — reads it back from this file before exec'ing the real binary.
2. **GitHub:** `su - watcher`, `gh auth login` as the bot account, `gh auth setup-git`. The bot
   needs write access to the repo and access to the Projects board.
3. **Project:** `deploy/add-project.sh riffmap roloc/riffmap` (from your own machine, see
   "Add a project" below) sets up the board, the labels, and `projects/riffmap.env`; commit
   and push that file, then `deploy/install.sh riffmap` clones the repo under
   `/home/watcher/projects/` and drop its `.env.local` and any private inputs in place.
4. **Start:** `systemctl enable --now watcher@riffmap watcher@riffmap-design
   watcher@riffmap-triage` (the build, design and triage lanes — without the triage lane
   an unlabeled card is nobody's); `journalctl -u watcher@riffmap -f` follows one.
   The watcher's own lane is `watcher@watcher` (`deploy/install.sh watcher` first); see
   `docs/how-it-works.md`, "The watcher's own board".
   Pause a lane with `touch ~watcher/.local/state/watcher/riffmap/PAUSE`.
5. **The floor page** is already up (`deploy/install.sh` starts `watcher-floor.service`
   the first time it runs, before any project is even cloned): `http://<this box's LAN
   address>:8787/`, reached off-site over whatever WireGuard tunnel already reaches the
   box. `journalctl -u watcher-floor -f` follows it; `systemctl restart watcher-floor`
   after hand-editing `bin/floor.py` outside the normal deploy path.

## Looking at pages

A tick that is told "the page is broken", or that changes a page, has to see the page the
way a person does — after its scripts ran. `curl` cannot do that, and a project should not
have to build the means each time. So the browser comes with the box:

    python3 "$WATCHER_HOME/bin/browse.py" check <url>            # exit 1 and the errors if the page's own code or a request failed
    python3 "$WATCHER_HOME/bin/browse.py" text  <url>            # the text a reader sees
    python3 "$WATCHER_HOME/bin/browse.py" shot  <url> out.png    # a picture a tick can Read

- **Every project has it.** `$WATCHER_HOME` is set for every tick on every lane, so there
  is nothing to install or copy per project. `commands/watch.md` tells the tick when to
  use it, and `agents/reviewer.md` blocks a changed screen that nothing rendered.
- **It needs one thing on the box:** a Chrome or Chromium. `apt install chromium` (in the
  setup line above); it also finds one in Playwright's cache. `deploy/install.sh` lists a
  missing browser under "Next, by hand", and `bin/doctor.sh` warns about it before a
  lane's first tick. No node, no Playwright package, no port: standard-library Python
  speaking Chrome's own protocol over a pipe.
- **What a project adds** is only what the watcher cannot know: where its pages are, and
  the command that serves a branch's version (`templates/project-CLAUDE.md`, "Looking at a
  screen"). A project that wants its page run in its **own CI** copies `bin/browse.py`
  (one file) and writes a suite like `bin/test-floor-browser.py`.
- **The replay suite holds it in place:** `evals/cases/page-symptom-is-run-in-a-browser.sh`
  hands a tick a bare project with a broken page and no browser tool of its own, and fails
  unless the tick ran the page in the browser and the page on `main` draws afterward.

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

Deploying is deliberately not a CI job: it would need a runner on the agents box. CI's
`verify` is instead the first thing the box's deployer checks — see below.

## Deploys happen by themselves

**Nobody runs a deploy.** `watcher-deploy.timer` runs `deploy/autodeploy.sh` every five
minutes on the box (as root; everything git and GitHub it does as the watcher user). Each
run is one step, idempotent, so an interrupted one is picked up by the next:

1. A new commit on `main`? Otherwise nothing, and nothing in the journal.
2. `verify` green on that exact commit (pending: wait).
3. The replay suite (`bin/eval.sh`) green on it, **in a scratch clone at that commit**,
   before anything goes live. Skipped when the commits touch nothing a model reads
   (`commands/ agents/ docs/ evals/ bin/eval.sh CLAUDE.md`). One failure is retried.
4. **The canary stage** (card #46), only for a commit that touches what a model reads and
   a project that has opted a lane into it (`LANE_CHANNEL=canary` — the Watcher board's own
   lane is the first): `/opt/watcher-canary`, a second checkout, moves onto the commit
   first and only that lane restarts. A clean soak, or a while with no work at all so a
   quiet board cannot hold every other project back, promotes to everyone else below. One
   tick that does not finish clean rolls the canary checkout back and holds the commit,
   same as a failed check. No canary lane configured: this step does nothing.
5. **The release.** The commit is cloned into its own folder under
   `/opt/watcher-releases/<sha>`, that folder's own `install.sh` runs, and `/opt/watcher` —
   a symlink, the pointer — flips onto it. A sweep then removes every older release folder
   no lane's own state still names, keeping at least the newest two.

Card #106 (October 2026): this never waits for or pauses a lane. A lane already mid-tick
is pinned, for the whole life of its own process, to whatever folder the pointer named
when it started — the flip disturbs nothing in flight — and each lane follows the new
pointer, on its own, at its own next idle poll, before its next tick. The floor page's
Ship desk shows which release each lane is on.

A **held** commit — red `verify`, the suite failing twice, or a canary tick that does not
finish clean — gets one card on the Watcher board in **Stuck**. Move it to In progress and
the watcher fixes the cause; add the `deploy-anyway` label and the next run ships it
(skipping the suite and the canary stage both). A later commit that deploys closes it as
superseded. Nothing here ever signals a tick.

    journalctl -u watcher-deploy -n 30        # what the deployer last did
    systemctl stop watcher-deploy.timer       # turn it off; `start` turns it back on

State is in `/var/lib/watcher-deploy/`. Knobs (environment on the unit): `DEPLOY_EVALS=0`,
`DEPLOY_RELEASES_DIR` (default `/opt/watcher-releases`).

**Rolling back** is pointing `/opt/watcher` at an older release folder by hand
(`ln -sfn /opt/watcher-releases/<sha> /opt/watcher`) — the sweep's "keep the newest two"
is exactly what keeps one there to point back at.

**The manual fallback**, for a Claude session when the deployer itself is broken or to try
a branch on the box before it merges — never for Steve:

    /opt/watcher/deploy/rollout.sh riffmap            # main
    /opt/watcher/deploy/rollout.sh riffmap <branch>   # a branch, before it merges

The same shape as the timer's own run: the suite in a scratch clone, a release cloned and
installed, the pointer flipped. It never pauses or waits for a lane either. After a
branch, run it again for `main`.

## Add a project

Putting a project on the watcher used to be a dozen hand steps, written down in three
places, most of them found only when they broke (card #39 — setting up this repo's own
board, PR #38, is what surfaced them). Now it is one idempotent script, run from Steve's
Mac (or wherever), authenticated as **yourself** — never as the bot, since two of its steps
(main's branch protection, the board itself) need admin/ownership the bot is never given:

    TEMPLATE_BOARD=<owner>/<number> deploy/add-project.sh <name> <owner/repo>

`TEMPLATE_BOARD` is a clean, existing board to copy — `gh project copy` works on any board
the caller can read, so it does not have to be a GitHub "template" (that flag is
organization-only anyway). The script checks each step before doing it, so running it again
after fixing something is the normal way to finish a partial run — it prints "unchanged"
for everything already in place. It copies the board, links the repo, creates the `in-
flight` / `blocked` / `steve-decision` labels, sets `main`'s branch protection (the named
check required, PR required, 0 approvals, admins not enforced), and writes
`projects/<name>.env`. Two things it cannot do for you — GitHub gives no API for either —
it prints as an ask-first checklist and waits for a yes: granting `riffmap-bot` access to
the new board, and turning on the board's **Auto-add to project** workflow with the filter
`is:issue is:open` (`gh project copy` does not carry that workflow across at all, and a
board whose filter is still GitHub's own `label:bug` example silently adds no cards — both
found by failing, card #39, September 28, 2026).

Then commit and push `projects/<name>.env` (the script writes it but does not push it), and
paste `templates/project-CLAUDE.md`'s section into the project's own `CLAUDE.md` — it is
built from what `/watch`, `/triage` and the reviewer actually read out of a project's
`CLAUDE.md`, with a blank for each one. Finally: `deploy/install.sh <name>` (clones the repo,
runs `bin/doctor.sh` — the same read-only check, confirming the board reads, Status/Stage/
Priority are there, the labels exist, Auto-add is on, and the project's `CLAUDE.md` names a
required check that `main` actually requires — and tells you if something above is still
missing before you start the service), then `systemctl enable --now watcher@<name>`.
`bin/spawn.sh` runs the same `doctor.sh` itself, once, before a lane's first tick ever
launches — so a project that drifts out of shape after setup (a label deleted, Auto-add
turned off) is refused, not silently worked wrong, the next time the service restarts.

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
`watcher@<name>-<lane>` too; the deployer starts a lane file the moment a commit adds it.
