# watcher

A headless Claude Code loop that turns a GitHub Projects board into merged PRs. Read
`docs/how-it-works.md` before changing anything here; it is the map. Read
`docs/talking-to-steve.md` before writing any text a human will read.

## Layout

- `bin/spawn.sh` — the loop systemd runs. `bin/test-spawn.sh` is its test suite: no network,
  every external command stubbed. **Run it before and after any change to `spawn.sh`:**
  `bash bin/test-spawn.sh` (exit 0 = all cases hold).
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
  project file and sets `PROJECT_NAME` and `LANE_AGENTS`, nothing else.

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
