# watcher

A headless Claude Code loop that turns a GitHub Projects board into merged PRs. Read
`docs/how-it-works.md` before changing anything here; it is the map. Read
`docs/talking-to-the-owner.md` before writing any text a human will read.

## Layout

- `bin/spawn.sh` — the loop systemd runs. `bin/test-spawn.sh` is its test suite: no network,
  every external command stubbed. **Run it before and after any change to `spawn.sh`:**
  `bash bin/test-spawn.sh` (exit 0 = all cases hold). CI runs it on every PR and every push
  to `main` (`.github/workflows/ci.yml`, job `verify`), so that rule is now checked rather
  than remembered — but a red CI run you could have seen locally in four seconds is a wasted
  round trip, so still run it here first.
- `commands/watch.md` — the `/watch` contract, one tick of the loop. Generic: it must work
  for any project on the box. Project-specific names (the CI check, contracts, spend
  receipts) come from the project's own `CLAUDE.md`, never hard-coded here.
- `commands/triage.md` — the `/triage` contract, the triage lane's tick: put the one
  `agent:` label on an unlabeled card, record what it waits on, stop. Generic the same way.
- `agents/reviewer.md` — the last review pass, run on every change that touches source or
  scripts. Read-only, blocking findings only, one round.
- `agents/usability.md` — a second, earlier review pass, run on any change that touches a
  screen, before `reviewer`. Sees only the card's ask and the pictures — never the diff —
  and blocks when the person the card names could not do the thing from a phone shot.
  Read-only, blocking findings only, one round.
- `bin/board.py`, `bin/worktree.sh` — the board CLI and the worktree helper. `bin/test-board.py`
  is `board.py`'s offline suite — the Needs-the-owner comment reading (`ask`, and `images`:
  the mockups and screenshots under a Stuck comment's `Look:` section, which `bin/floor.py`
  shows inline instead of making the owner click through to the card, card #91). `python3 bin/test-board.py`
  (CI runs it); run it after touching either of those two readers.
- `bin/notify.py` — Discord and/or Slack posts, called only from `board.py` (a `move` to
  Stuck, and `notify`). It posts when a card is stuck or something broke, and for nothing
  else. `python3 bin/test-notify.py` is its offline test; run it after touching either file.
- `bin/metrics.py` — a read-only weekly report over a lane's own tick logs (first-pass
  merge rate, park rate, deaths, turns/quota and time to Done). Run by hand for the full
  report; `--digest` (one bullet per lane, CI runs this suite) is also what
  `watcher-metrics.timer` runs unattended every Monday, piped to Discord with `--post`.
  `python3 bin/test-metrics.py` is its offline test; run it after touching the file.
- `bin/tail.sh` and `bin/tail.py` — run by hand, on the box: which card a lane holds and
  what its tick is doing right now, decoded live from the tick's own session transcript
  (not the lane's own coarser `journalctl` line — see `bin/tail.sh`'s header). Read-only;
  never touches the board or GitHub. `bash bin/test-tail.sh` is its offline test (CI runs
  it); run it after touching either file. `bin/floor.py`'s Peek button is the same decode,
  reached from the floor page instead of the command line — it imports `bin/tail.py`
  directly rather than re-implementing it, so touching either file's decode logic means
  running both `bash bin/test-tail.sh` and `python3 bin/test-floor.py`.
- `bin/floor.py` — the floor page (`watcher-floor.service`, port 8787): what every lane is
  doing, and a Peek at each one's session. **It has two suites, and they check different
  things.** `python3 bin/test-floor.py` checks the Python that builds the page's data.
  `python3 bin/test-floor-browser.py` loads the page in a real headless browser and clicks
  it — the only thing that runs the page's own script. Run both after touching the file
  (CI runs both); a change to what the page shows or does adds its case to the browser
  suite, never only a string looked for in the source. See "The floor page is a screen"
  below.
- `bin/browse.py` — a real browser from the command line, no dependencies beyond a Chrome
  or Chromium binary (it finds the one in Playwright's cache on the box):
  `python3 bin/browse.py check <url>` says whether the page's own code or requests failed,
  `text <url>` prints what a reader sees after the scripts ran, `shot <url> out.png` writes
  a picture to `Read`. This is how a page is looked at — never `curl`, which returns what
  the server sent and says nothing about what was drawn. **It is the browser every project
  on the box gets** (`"$WATCHER_HOME/bin/browse.py"`, named in `commands/watch.md`), so
  keep it dependency-free and keep its three commands stable: other projects' ticks call
  them. `bin/browse.py find` says which browser it would use (the installer and
  `bin/doctor.sh` ask it).
- `bin/eval.sh` — the replay suite: real past failures against `commands/watch.md`,
  `commands/triage.md` and `agents/reviewer.md`, each a case under `evals/cases/` run with a
  live `claude -p` against `evals/fakehub.py` (a closed, offline stand-in for `gh` and
  `board` — no network, ever). **Run it after any change to those three files:**
  `bash bin/eval.sh` (exit 0 = all cases hold; `bin/eval.sh <name>` for one case). It costs
  real quota (each case is a full tick) and needs a Claude login, so it never runs in CI —
  `deploy/autodeploy.sh` runs it on every new `main` commit that touches what a model reads,
  in a scratch clone at that commit, before anything goes live; a commit that fails it
  twice is held and never deployed. The playbook: every future incident that changes one of those
  three files adds its case in the same PR (`bin/eval.sh`'s own header has the how-to, and
  the caveat about what a case does and does not prove). `bash bin/test-eval-cases.sh` is
  the cases' own offline suite (CI runs it): it builds a case's real fixture, stands in for
  the tick (a comment, a status, a PR body, a commit on main) and reads what the case's
  `case_check()` makes of it — so a check's regex is tried against the shapes it must pass
  and the ones it exists to catch before a live run ever meets it. Touching a `case_check()`
  means adding its sample there and running it.
- `bin/receipt-check.sh` — the `review-receipt` CI check (card #142): a code PR cannot
  merge without a `🤖 reviewer:` comment (`commands/watch.md` §3 posts it) naming the PR's
  exact head commit with a clean verdict, and a screen change needs a matching
  `🤖 usability:` one too. Independent of the tick that opened the PR — it reads only the
  PR's own comments and its own current head SHA — so an amend after the review, or a
  review that never ran, fails it instead of merging silently. Required alongside `verify`
  in `.github/workflows/ci.yml`'s `review-receipt` job. `bash bin/test-receipt.sh` is its
  offline suite (CI runs it); run it after touching the file.
- `docs/` — for agents (`how-it-works.md`, `talking-to-the-owner.md`) and for the owner
  (`owner-cheat-sheet.md`).
- `deploy/autodeploy.sh` — **the deploy path.** A timer on the box (`watcher-deploy.timer`)
  ships `main` by itself: `verify` green on the commit, the replay suite green in a scratch
  clone, then a fresh release folder cloned, installed, and the `/opt/watcher` pointer
  flipped onto it (card #106) — never a pause or a wait for a lane, which is pinned to
  whatever folder it already has for the life of its own process and follows the new
  pointer on its own, later. Nobody runs a deploy by hand. `bash bin/test-deploy.sh` is
  its offline suite (CI runs it); run it after touching the file. Everything it says goes
  to the journal and to `/var/lib/watcher-deploy/history`, a plain file the floor page's
  Ship desk reads for its Peek (the page's user cannot read the journal).
- `deploy/`, `projects/` — the systemd unit, the installer, `rollout.sh` (the manual
  fallback, the same release-folder shape as the timer's own run: clone, install, flip the
  pointer — never a pause or a wait either), one env file per
  systemd instance. A project with two lanes has two files; the lane file sources the
  project file and sets `PROJECT_NAME` and `LANE_AGENTS`, nothing else. `projects/watcher.env`
  is the watcher's own lane; see "When the watcher works its own board" below.
- `bin/detect.sh` — the product detector (card #36): no model, one metric, a rolling
  baseline, fixed bands; a large breach files a Backlog card, never more than one while it
  stays open. Opt-in per project via `projects/<name>.detect.env`
  (`deploy/watcher-detect@.service`, `.timer` — installed by `install.sh` for every box;
  `install.sh` enables `watcher-detect@<name>.timer` for every `.detect.env` it finds, and
  reruns on every deploy, so merging that config file is what turns detection on).
  `bash bin/test-detect.sh` is its offline suite (CI runs it); run it after touching
  `detect.sh`. Detail in `docs/how-it-works.md`, "Watching the product".
- `bin/hygiene.sh` — the weekly debt sweep, run by `.github/workflows/hygiene.yml` on
  Mondays; it files one card when it finds something and nothing when it does not. A
  hygiene card is mechanical only: clear the sections it lists and nothing else, delete a
  branch with `gh api -X DELETE …/git/refs/heads/<branch>` and list it in a comment, and
  "done" is `bin/hygiene.sh` exiting 0 on `main` (by hand, that needs `GITHUB_REPOSITORY`
  exported, or `--no-remote`). README, "The weekly hygiene card".

## Rules for editing this repo

- **Anything the owner reads follows `docs/talking-to-the-owner.md`.** That includes the
  Stuck comment template in `watch.md`, the reviewer's `For the owner` block, and the
  comment bodies in `spawn.sh`. First line is the ask, bullets, one question, lettered
  options, how to answer. A test in `test-spawn.sh` checks the spawner's park comment keeps
  that shape.
- Keep `watch.md` and `reviewer.md` generic. If a rule only makes sense for one project, it
  belongs in that project's `CLAUDE.md` or its `.claude/commands/watch.md` override.
- `spawn.sh` and `--dry-run` share `print_claude_cmd`; never let the real launch and the
  dry-run drift apart.
- Comment bodies passed to `gh issue comment --body` may be multi-line. The test stub logs
  each call on one line, so assertions can grep the whole body.
- **The offline suites have to pass on a Mac as well as on Linux**, because "run it here
  first" happens on a Mac (BSD tools, `/bin/bash` 3.2) and CI only ever checks Linux. In a
  script a suite runs, no spelling that is GNU's alone: `find -newermt '@<epoch>'` cost two
  cases in `test-deploy.sh` that way, and `date -d`, `stat -c` and a bare `sed -i` are the
  same trap. A command a stock Mac does not ship (`timeout`, `flock`) gets a stand-in in
  the suite, the way `test-deploy.sh` stubs `flock`, not a fallback in the script.
- No permission prompts exist on the box. Nothing in the tick may depend on one.
- Discord/Slack is a repeater, never a record: a post happens after the board change
  lands, a failed post is one stderr line and nothing else, and no message goes out per
  poll or per tick. **It is for stuck, broken, and the Monday digest only** — a move to
  Stuck, a run that died, a card the detector filed, or `bin/metrics.py --post`'s weekly
  numbers (card #139, the one post on a clock instead of an event, named as a future
  possibility in `metrics.py`'s own header since it was written). A new kind of post
  needs one of those three to be true of it; progress belongs on the floor page
  (October 1, 2026). A webhook URL is a secret, whichever channel it is for; it lives in
  `/etc/watcher/env`, never in git.
- American English in every file. Dates like September 7, 2026.

## When the watcher works its own board

The `watcher@watcher` lane (`projects/watcher.env`) works the
[Watcher board](https://github.com/users/roloc/projects/2) with this repo as its project, so
a tick on that board reads this file as the project's `CLAUDE.md`. On top of everything
above:

- **The required CI check is `verify`.** Run both suites locally before pushing (Layout,
  above); a red `verify` you could have seen in four seconds is a wasted round trip.
- **You are editing the code you run under, but not the copy you are running.** Your
  `/watch`, the reviewer and `$WATCHER_HOME` are `/opt/watcher`, the deployed copy. Your
  worktree is a separate clone of the repo. Editing `commands/watch.md` there changes
  nothing about the tick in progress: read the worktree's copy for what the card is
  changing, `$WATCHER_HOME`'s for how to behave.
- **Never write outside your worktree.** Not `/opt/watcher`, not another project's
  checkout, not a lane's state directory (`~/.local/state/watcher/`), not a `PAUSE` file,
  not `/etc/watcher/`. Never `systemctl`, never `deploy/rollout.sh` or
  `deploy/autodeploy.sh`. **Reading** the state
  directories and `journalctl -u 'watcher@*'` is fine, and it is how a card about the loop's
  behavior goes and looks instead of asking (`watch.md`: a fact is not a product decision).
- **Deploying is the deployer's, never yours.** A merge goes live by itself, for *every*
  project on the box, the next time all lanes are idle — after `verify` and, when the change
  touches what a model reads, the replay suite pass on it (`deploy/autodeploy.sh`). So the
  PR body and the Done comment say, in plain words, that it goes live on its own once the
  lanes are idle, and flag anything that changes how every lane behaves (`commands/`,
  `agents/`, `bin/spawn.sh`).
- **The owner can open three things: the board, the Discord channel, and the floor page —
  all from their phone.** They do not ssh. So a card that asks for a way to *see*, *watch*,
  *check* or *look at* something is asking for it in one of those places — the floor page,
  unless its words say otherwise — and a command on the box is not an answer to it, however
  literally the title reads as one. A command is a fine *part* of the work (the floor
  page's Peek is `bin/tail.py` underneath); it is never the thing they get. When a card can
  be built two ways that land in different places, that is the card's real question: build
  the one they can reach, and say which in the first line of the plan; if you cannot tell,
  that is one question for Stuck before anything is built, not a guess they find afterwards.
  (Card [#63](https://github.com/roloc/watcher/issues/63), September 30, 2026: "a way to
  tail the log for a lane" shipped as `bin/tail.sh`, a command that needs ssh. He reopened
  it: "I would like to view these things from the floor page.")
- **The floor page is a screen: it ships with its picture, and its own code is run before
  it merges.** `watch.md`'s "a change to a screen ships with its picture" applies to any
  change to what `bin/floor.py` serves, and this repo's screenshot script is
  `python3 bin/test-floor-browser.py --shots /tmp/card-<n>-shots`: it loads the page in
  headless Chrome against a fixture floor, clicks through it, and writes desktop and phone
  PNGs. `Read` them, and say in the PR in one sentence what they show. The change also adds
  its own case to that suite — one that does the thing in the browser: opens it, clicks it,
  reads what appeared. A string found in the page's source is not the page working: #70
  merged with 115 such checks green and a `<script>` block that did not parse, so the page
  drew nothing at all.
- **A symptom on the floor page is reproduced with `bin/browse.py`, never with `curl`.**
  `python3 bin/browse.py check http://localhost:8787/` loads the live page the way a
  browser does and prints every error its own code raised; `shot` gives you the picture.
  The page draws itself with a script, so `curl` returning 200 and a full document says the
  service is up and nothing about what the owner saw. A page you only fetched is a page you
  did not look at, and "it loads fine from here" on the strength of `curl` is not a finding
  to take to Stuck. (Card [#71](https://github.com/roloc/watcher/issues/71), October 1, 2026:
  five clean fetches, then a Stuck card asking Steve what he had seen. `check` says it in
  one line.)
- **The Done comment says where to look and what is there right now.** The owner's next
  move after a card closes is to open the thing. So the closing comment names the place —
  the floor page's link, for anything on it — and says what they will see when they open it
  *today*, including when the honest answer is "nothing yet". A feature that only shows in
  a state the box is not in right now reads as a feature that did not ship; if that is
  what you are about to write, the design is usually not finished. (Card #63 once more:
  the Peek button existed only on a desk that was working, the floor was idle, and the
  closing comment did not say so. He looked, found no button, and came back a third time.)
- **A watcher card's symptom is re-checked by a test, not live.** The fix is not running
  until the deployer ships it, so `watch.md`'s "a symptom card is not done until its symptom is checked
  again" is met by a case that reproduces the symptom — in `bin/test-spawn.sh`, in
  `bin/test-floor-browser.py` for anything on the floor page, or a replay case in
  `bin/eval.sh` (card [#34](https://github.com/roloc/watcher/issues/34)) — failing on
  `main` and passing on the branch. Say which case, in one sentence, in the PR.
- **Merge your own PR on green and a clean review — including a change to the gate.**
  Steve never merges here, the same as riffmap (September 28, 2026: "I don't want to review
  all of these merges since I am only involved at the card / feature level"). The gate is
  `.github/workflows/`, `agents/reviewer.md`, `agents/usability.md`, the ship-and-merge rules in
  `commands/watch.md` §3, "Hard rules for every agent" in `docs/how-it-works.md`,
  everything under `deploy/`, `bin/receipt-check.sh` (the `review-receipt` required check,
  card #142), and the replay suite (`bin/eval.sh`, `evals/`). A change to
  any of it **always gets the reviewer**, even when it looks like prose or config, and
  anything that **weakens** it — drops a check, loosens a condition, removes or edits a
  replay case so it passes more easily — is a blocking finding, never a nit. **For the gate
  the reviewer is the only check**: `verify` is a workflow the PR can edit, the deployer
  skips the replay suite for commits that touch only `deploy/` or `.github/`, and a PR that
  weakens the suite is tested by that weakened suite. Behind ordinary changes (`commands/`,
  `agents/`, `docs/`) the deployer's run of the replay suite on the exact commit still
  stands, and a commit that fails it is held in Stuck.
  Stuck is still where a blocking finding you disagree with goes, and a third review round
  (the change is mis-shaped); never for "please merge".
