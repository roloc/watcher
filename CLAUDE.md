# watcher

A headless Claude Code loop that turns a GitHub Projects board into merged PRs. Read
`docs/how-it-works.md` before changing anything here; it is the map. Read
`docs/talking-to-steve.md` before writing any text a human will read.

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
- `agents/reviewer.md` — the one review pass. Read-only, blocking findings only, one round.
- `bin/board.py`, `bin/worktree.sh` — the board CLI and the worktree helper.
- `bin/notify.py` — Discord posts, called only from `board.py` (`move`, `stage`, `notify`).
  `python3 bin/test-notify.py` is its offline test; run it after touching either file.
- `bin/metrics.py` — a read-only weekly report over a lane's own tick logs (first-pass
  merge rate, park rate, deaths, turns/quota and time to Done), run by hand, never by the
  loop. `python3 bin/test-metrics.py` is its offline test; run it after touching the file.
- `bin/tail.sh` and `bin/tail.py` — run by hand, on the box: which card a lane holds and
  what its tick is doing right now, decoded live from the tick's own session transcript
  (not the lane's own coarser `journalctl` line — see `bin/tail.sh`'s header). Read-only;
  never touches the board or GitHub. `bash bin/test-tail.sh` is its offline test (CI runs
  it); run it after touching either file. `bin/floor.py`'s Peek button is the same decode,
  reached from the floor page instead of the command line — it imports `bin/tail.py`
  directly rather than re-implementing it, so touching either file's decode logic means
  running both `bash bin/test-tail.sh` and `python3 bin/test-floor.py`.
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
  the caveat about what a case does and does not prove).
- `docs/` — for agents (`how-it-works.md`, `talking-to-steve.md`) and for Steve
  (`steve-cheat-sheet.md`).
- `deploy/autodeploy.sh` — **the deploy path.** A timer on the box (`watcher-deploy.timer`)
  ships `main` by itself: `verify` green on the commit, the replay suite green in a scratch
  clone, then every lane idle. Nobody runs a deploy by hand. `bash bin/test-deploy.sh` is
  its offline suite (CI runs it); run it after touching the file.
- `deploy/`, `projects/` — the systemd unit, the installer, `rollout.sh` (the manual
  fallback: deploy without
  killing a running tick: pull, pause, wait, restart, un-pause, per lane), one env file per
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

## Rules for editing this repo

- **Anything Steve reads follows `docs/talking-to-steve.md`.** That includes the Stuck
  comment template in `watch.md`, the reviewer's `For Steve` block, and the comment bodies
  in `spawn.sh`. First line is the ask, bullets, one question, lettered options, how to
  answer. A test in `test-spawn.sh` checks the spawner's park comment keeps that shape.
- Keep `watch.md` and `reviewer.md` generic. If a rule only makes sense for one project, it
  belongs in that project's `CLAUDE.md` or its `.claude/commands/watch.md` override.
- `spawn.sh` and `--dry-run` share `print_claude_cmd`; never let the real launch and the
  dry-run drift apart.
- Comment bodies passed to `gh issue comment --body` may be multi-line. The test stub logs
  each call on one line, so assertions can grep the whole body.
- No permission prompts exist on the box. Nothing in the tick may depend on one.
- Discord is a repeater, never a record: a post happens after the board change lands, a
  failed post is one stderr line and nothing else, and no message goes out per poll or
  per tick. The webhook URL is a secret; it lives in `/etc/watcher/env`, never in git.
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
- **A watcher card's symptom is re-checked by a test, not live.** The fix is not running
  until the deployer ships it, so `watch.md`'s "a symptom card is not done until its symptom is checked
  again" is met by a case that reproduces the symptom — in `bin/test-spawn.sh`, or a replay
  case in `bin/eval.sh` (card [#34](https://github.com/roloc/watcher/issues/34)) — failing
  on `main` and passing on the branch. Say which case, in one sentence, in the PR.
- **Merge your own PR on green and a clean review — including a change to the gate.**
  Steve never merges here, the same as riffmap (September 28, 2026: "I don't want to review
  all of these merges since I am only involved at the card / feature level"). The gate is
  `.github/workflows/`, `agents/reviewer.md`, the ship-and-merge rules in
  `commands/watch.md` §3, "Hard rules for every agent" in `docs/how-it-works.md`,
  everything under `deploy/`, and the replay suite (`bin/eval.sh`, `evals/`). A change to
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
