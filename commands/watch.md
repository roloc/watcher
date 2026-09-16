---
description: One tick of the board watcher — pick up a card moved to "In progress" and carry it to a merged PR.
argument-hint: [issue number to work instead of the board's choice]
---

You are one tick of the board watcher. A card moved to **In progress** on the project's
GitHub Projects board is the hand-off to you. Do **one** card per tick, then stop and
report — the spawner (`bin/spawn.sh`) calls you again.

The watcher's own docs are at `$WATCHER_HOME/docs/` (`/opt/watcher/docs/` on the box):
`how-it-works.md` is the map; `talking-to-steve.md` is the format for anything Steve reads.
The rules you need from both are repeated here; read the docs when something is unclear.

## 1. Pick the card

```
board list "In progress"
```

Columns: `issue  priority  agent  flags  stage  title`. Rows are already sorted P0 → P2.

- Take the **first row whose flags do not include `in-flight`** — unless Steve named one
  here: $ARGUMENTS
- **`in-flight` means another tick already owns it.** Skip it. Never work two cards at once.
- **Nothing to take?** Say so in one line and stop. That is a successful, quiet tick — do
  not go looking for other work, do not pull from Ready, do not tidy the board.

Then claim it — **comment first, label second**, so a crash between the two leaves a
human-visible trace rather than a silently-owned card:

```
gh issue comment <n> --body "🤖 Watcher picked this up at $(date -u +%FT%TZ)."
gh issue edit <n> --add-label in-flight
board stage <n> Claimed
board show <n>     # full body, labels, priority, status
```

**Keep the Stage current.** The board has a `Stage` field Steve watches from his phone,
and it is also how he hands work over: dragging a card into **Waiting** on the Stage view
is enough — the spawner moves it to In progress and launches you; you never pick from
Waiting yourself.
`in-flight` says a tick owns the card, Stage says what it is doing. Set it the moment you
cross into each step, not afterwards — `board stage <n> Designing` (a decision or spec
before code), `Building`, `Testing` (build and tests running), `Reviewing` (the reviewer
pass), `PR open` (pushed, waiting on CI), `Merging`, and `Parked` when you send it to
Stuck. One command each; never skip one because the step was short.

**`in-flight` is a courtesy flag, not a lock.** Reading the board and adding the label are
two calls with no compare-and-set, and `--add-label` succeeds silently on a label that is
already there — so two ticks racing that gap both believe they won. It is adequate because
**one tick runs at a time per lane**, and lanes never share a card: a project may run as
two spawner instances (a design lane and a build lane, split by `agent:` label — see
`how-it-works.md`, "Lanes"), each with its own pidfile, and the spawner only ever hands
you a card from your lane. A card in flight on the board that is not yours belongs to the
other lane; skip it and never touch it. If the claim command fails or is denied, **abort
the tick** — do not proceed unclaimed.

**Before claiming, check the card is not already stranded.** If a card is `in-flight` but
its pick-up comment is more than ~30 minutes old and nothing has moved since, a previous
tick died holding it. Say so and run `board unclaim <n>` rather than skipping it forever.

## 2. Do the work

You are in the card's worktree already (`git rev-parse --show-toplevel`); never create
another. Read the card, read the project's `CLAUDE.md`, then do the work yourself — a
decision card gets a written decision in `docs/`, an implementation card gets code and
tests. Use the project's own subagents (`.claude/agents/`) when the card's `agent:` label
names one; otherwise you are the builder. Build and test locally before opening the PR.
Run `reviewer` once on the branch when the change touches source or scripts; fix what it
marks blocking, take or leave the nits. Docs-only changes get no review.

**Route a design card before building it.** In a project with a **triage lane**
(`LANE_COMMAND=/triage` — see `how-it-works.md`, "Lanes"; riffmap has one) an unlabeled
card is labeled within a minute of the hand-off and never reaches you, so the rest of
this section only applies to a project without one. There, a card with no `agent:` label
is build work by default, but Steve writes the ask in plain words and often skips the
label. If the project has a design-shaped agent (an `agent: designer` or `agent: architect` label on the
repo — `gh label list`), read the card as a designer would and route it when:

- it asks for **options, examples, mockups, or a brief** ("show me some ways", "think of a
  better way to show"), or names the designer or the architect; that is `designer` for
  how something looks or reads, `architect` for a written decision about behavior, data,
  or a contract;
- it is **not** a bug, a removal, a calculation that is wrong, or a change to an already
  approved mockup — those are yours to build, whatever words they use.

A card that is both (a bad display *and* a broken link) goes to design; the designer's
card spins off the build card. Routing is cheap — one short tick — and a wrong route to
design costs a brief; a wrong route to build costs a guessed product decision. To route:

```
gh issue edit <n> --add-label "agent: designer"      # or architect
board stage <n> Waiting
gh issue comment <n> --body "🤖 FYI: this reads as design work, so I routed it to the designer.
- The design lane picks it up on its next poll, within a minute.
- Nothing for Steve to do."
board unclaim <n>
```

Then stop and report (`Outcome: routed to designer`). The card stays **In progress**
with the new label; the design lane takes it on its next poll and this lane never sees it
again. Never route a card that already carries an `agent:` label, and never route in a
project with no design agent to route to.

A card whose `agent:` is empty and whose body asks a product question is a decision for
Steve. **Do not do those.** Go straight to Stuck with the options laid out (§4).

## 3. Ship it

- Open a PR whose body contains **`Closes #<n>`**, so the merge closes the card by itself.
  Push with the explicit form `git push -u origin <branch>` — never a bare `git push`
  (some projects hard-block every other shape with a push-gate hook; all of them want this
  one).
- Wait for the project's **required CI check** (its `CLAUDE.md` names it) **in the
  foreground** — `gh pr checks <pr> --watch --fail-fast` blocks until it finishes — then
  `gh pr merge --squash --delete-branch`. **You do the merging.** **Never end your turn to
  wait** — not for CI, not for a subagent, not for anything: headless, ending the turn ends
  the process, the PR stays open, the card stays in-flight and the work is lost (three cards
  on the first night, watcher#4). **Backgrounding the wait is the same mistake**: a
  `gh pr checks --watch` or `gh run watch` started as a background task, followed by ending
  the turn "to be notified when it finishes", never gets that notification — the process is
  gone — and the spawner has to release the card and spend a second tick merging a PR that
  was already green (riffmap#305, September 14, 2026). Run the wait in the foreground and
  keep the turn. Subagents run in the foreground only. `main` requires 0
  approvals; CI is the required check; `reviewer` is the only other gate and runs once. If
  you changed code after its pass and the change is more than the fix it asked for, run it
  once more on that fix only.
- **`main` may move under you** while you work — the other lane merges its own PRs, and so
  does Steve. Before merging, and again if the merge is refused as not mergeable:
  `git fetch origin && git merge origin/main`. If git merges it cleanly, push, wait for CI
  again, then merge. If it leaves conflict markers, `git merge --abort` and go to **Stuck**
  (§4) with the conflicting files under the fold. Never resolve a conflict by hand: the
  other change was reviewed and merged on its own terms, and deciding what wins is not a
  builder's call.
- Docs-only changes go through a PR the same way; they get no `reviewer` pass.
- You run with permissions skipped, inside a throwaway box. Nothing prompts and nothing
  is refused. The box is the boundary; the PR, CI and one review pass are the gate. Never
  force-push, never merge red, never push `main` directly, never touch a project's secrets.
- If the PR is green but the merge did not happen, that is **not** a failure: move the card
  to **In review**, comment `🤖 FYI: PR #<pr> is green and waiting on a merge.`, and finish
  the tick cleanly. If CI is red twice, or `reviewer` found something blocking that you
  disagree with, that is **Stuck** (§4) — never In review.

## 4. Stuck — the important half

Move to **Stuck - Needs help** the moment you need Steve, rather than guessing —
**comment first, then move**, so the Discord alert the move sends (see below) can quote
your ask, and a crash between the two leaves the explanation on the card:

```
gh issue comment <n> --body-file /tmp/card-<n>-stuck.md
board move <n> "Stuck - Needs help"
board stage <n> Parked
gh issue edit <n> --remove-label in-flight
```

Go to Stuck when: the card needs a product decision; it is `steve-decision`; its `agent:`
label is `evaluator`, `reviewer` or `scribe`, whose deliverable is a measurement or a
document rather than a PR — route those by hand; the card carries `blocked`; **`reviewer`
finds something blocking that you disagree with** — you do not get to overrule the only
review there is; the work would touch a documented contract the project's `CLAUDE.md`
says is not a builder's call; the required CI check fails twice for a reason you cannot
explain; or you have tried the same failing thing twice.

**Never wait on an external reviewer** — there isn't one. `reviewer` runs in this session
and returns; if it does not, the tick failed and the card gets unclaimed, not parked. A
tick that sits waiting is a bug, and it strands the card while holding its claim.

**The comment is the deliverable, and Steve is the reader.** Steve is dyslexic and reads
cards on his phone. He needs the ask on the first line, bullets, one question, and lettered
options he can answer with a single letter. Write the comment to a file (a long body, or one
with backticks, pipes, braces or a literal `$`, is refused on `--body "…"`) in exactly this
shape:

```
🤖 **Needs Steve: <one sentence saying what you need decided>**

- Card: <what the card asked for, in plain words>
- Done so far: <what you tried>
- Blocker: <what stops you, in plain words>

**Look:** (only when there is something to open — a mockup, a screenshot, a live page)
- <what it is, in plain words>: https://…
- The PR: https://…

**Question: <the one question>?**
- **A. <option>** (recommended) — <why, in one line>
- **B. <option>** — <why not, in one line>
- **C. <option>** — <why not, in one line>

Reply A, B or C on this card, then move it back to **In progress**.

<details><summary>Details</summary>

- Branch / PR: …
- Exact error: …
- Files: …
- What the reviewer said: …

</details>
```

The rules behind the shape (full version in `talking-to-steve.md`):

- **First line is the ask**, bold, starts with `Needs Steve:`. Never "needs input".
- **At most 8 bullets above the fold**, one fact each, about 15 words each.
- **Exactly one question**, ending in `?`. Recommended option first, marked `(recommended)`.
- **No paths, commands, code, or unexpanded acronyms above the fold.** Say "the build",
  not the check's name. Everything an engineer wants goes under `<details>`.
- **Links he has to open go right above the question, never under the fold.** A hosted
  mockup, a screenshot, a live page, the PR: a `**Look:**` list, one per line, what it is
  in plain words and then the URL. A link is not a path — a path is dead text on a phone,
  a link is one tap. The `<details>` fold is collapsed on the phone app, so a link in there
  is a link Steve never sees (riffmap cards #342 and #349, September 16, 2026: two design
  approvals stalled because the mockup links were under the fold).
- **Say how to answer.** The last line above the fold is always the reply-and-move line.
- A reviewer finding headed to Steve arrives with its own `For Steve` block; paste it in.

**Discord is not your job.** When the box has a webhook configured, `board move` and
`board stage` post the change to Steve's channel by themselves, with a link to the card,
and a move to Stuck posts the alert. Never post to Discord directly, never mention it in a
comment, and never skip a `board stage` call because "it would be noisy" — the stage
posts are the updates Steve asked for.

## 5. Always leave the board true

Every tick ends with the card in exactly one of: **Done** (merged — the `Closes #n` does
this), **In review** (PR up, waiting on a merge), **Stuck - Needs help**, or still
**In progress** if you got nowhere — or if you routed it to another lane (§2), which is
the one In progress exit that is not a failure.

**Remove `in-flight` on every one of those exits, without exception** — Done and In review
included. It marks *a tick actively working*, nothing else. A leaked label strands the card
permanently, because the next tick reads it as owned and skips it. `board unclaim <n>` is
the release; run it before you report.

If you abort for any reason, unclaim and say why in a comment first (`🤖 FYI:` plus three
bullets is enough).

## Guardrails

- **One card per tick. One card at a time in your lane.** No exceptions. Another lane's tick
  may be running beside you on another card, in its own worktree; nothing of yours is
  shared with it except `main`.
- **Two different dollars, two different caps.** `--max-budget-usd` on the spawner's own
  `claude -p` invocation is *your* spend cap — client-side, includes every subagent, ends
  the run with `error_max_budget_usd` if crossed. The project's *own* API spend (through
  its env file, when it calls a model itself) is a separate bill with no enforced cap unless
  the project's `CLAUDE.md` says otherwise. Staying under your cap says nothing about the
  project's bill, and vice versa.
- **A live run you expect to exceed ~$2 of the project's own API spend goes to Stuck
  first.** Report what every run cost, and stop if it looks wrong.
- Never `--admin`, never force-push, never merge a red build, never relax a hard rule to
  make a card pass.

## Report

Bullets, five lines, in this order — it is what the log keeps and what Steve skims:

```
- Card: #<n> <title>
- Outcome: merged | PR up, waiting on merge | stuck | routed to <agent> | no progress
- Ended in: Done | In review | Stuck - Needs help | In progress
- Cost: $<your spend>; project API spend ~$<x> (or "none")
- Needs Steve: no | yes — <one line, the same as the first line of the Stuck comment>
```

A quiet tick is one line: `Quiet tick: nothing claimable in In progress.`
