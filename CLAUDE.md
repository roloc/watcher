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
- `docs/` — for agents (`how-it-works.md`, `talking-to-steve.md`) and for Steve
  (`steve-cheat-sheet.md`).
- `deploy/`, `projects/` — the systemd unit, the installer, `rollout.sh` (deploy without
  killing a running tick: pull, pause, wait, restart, un-pause, per lane), one env file per
  systemd instance. A project with two lanes has two files; the lane file sources the
  project file and sets `PROJECT_NAME` and `LANE_AGENTS`, nothing else. `projects/watcher.env`
  is the watcher's own lane; see "When the watcher works its own board" below.

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
  not `/etc/watcher/`. Never `systemctl`, never `deploy/rollout.sh`. **Reading** the state
  directories and `journalctl -u 'watcher@*'` is fine, and it is how a card about the loop's
  behavior goes and looks instead of asking (`watch.md`: a fact is not a product decision).
- **Deploying is Steve's.** A merge changes nothing on the box until he runs `rollout.sh`,
  and a rollout of *any* project pulls `/opt/watcher` for *every* project. So the PR body
  and the Done comment say, in plain words, whether the change needs a rollout to take
  effect (anything under `bin/`, `commands/`, `agents/`, `deploy/`, `projects/`), and
  whether `bin/spawn.sh` changed (that one also needs the lane restart `rollout.sh` does).
- **A watcher card's symptom is re-checked by a test, not live.** The fix is not running
  until a rollout, so `watch.md`'s "a symptom card is not done until its symptom is checked
  again" is met by a case that reproduces the symptom — in `bin/test-spawn.sh`, or the
  prompt evals once [#34](https://github.com/roloc/watcher/issues/34) lands — failing on
  `main` and passing on the branch. Say which case, in one sentence, in the PR.
- **A change to the merge gate is built, but not merged.** The gate is
  `.github/workflows/`, `agents/reviewer.md`, the ship-and-merge rules in
  `commands/watch.md` §3, and "Hard rules for every agent" in `docs/how-it-works.md`.
  Build it, run the reviewer, get `verify` green, then park the card in Stuck (§4) with the
  PR under **Look:**, asking Steve to merge it. This is the contract `watch.md` §4 means by
  "not a builder's call": until the prompt evals exist, the reviewer is the only check on a
  change to the reviewer, so the watcher never loosens its own gate unseen.
