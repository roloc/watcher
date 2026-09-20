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

**Look at what the card shows before you decide what is wrong.** A card that carries a
picture (`<img src="…">` or `![…](…)` in the body — `gh issue view <n> --json body`) is
a card whose evidence is the picture, not the sentence under it. Fetch it and look at it
first: `curl -sL -H "Authorization: token $(gh auth token)" -o /tmp/card-<n>-1.png <url>`,
then `Read` the file — the Read tool shows you a PNG. Say in one sentence what the picture
shows, and check that against the code before you accept the card's own explanation of
the cause: Steve's diagnosis is a hypothesis, the picture and the code are the evidence.
If they disagree, say so on the card and go to Stuck (§4) with what you saw — never build
the fix for the wrong cause. (riffmap [#359](https://github.com/roloc/riffmap/issues/359),
September 16, 2026: the card said "too many topics"; the screenshot showed every arc as a
dashed outline, the portal's designed "behind the pipeline" state the morning after a
pipeline bump. The tick built a cap on topic count, and the ring came out worse.)

**The card names a thing you can go and look at? Look at it.** A picture is the obvious
case, but so is a live page, a meeting, a record, a deployed screen — anything the project
can hand you a URL or a file for. **Whether you *can* look is a question about access, not
about effort**, so find out before you decide you cannot: the project's `CLAUDE.md` says
what its data is and how a tick is allowed to read it, and a read-only capability link is
the usual answer. A card that says *"this page is wrong"* is a card whose evidence is the
page. (riffmap [#431](https://github.com/roloc/riffmap/issues/431), September 19, 2026: a
tick asked Steve which of two widgets he had seen on a specific meeting. The meeting was
one fetch away — its data said neither widget could have drawn — so the question spent a
day of Steve's attention on something the tick could have answered in a minute.)

**A spawned card re-reads the complaint, not just its parent.** When this card was filed by
another card — an architect's build card, a designer's `ui` card, a follow-on — walk the
chain up to its **root**: the card a human wrote, at the top. Read the root, and say in one
line which sentence of it this work serves, on the card and in the PR body. The chain is in
the cards themselves: a spawned card names the one that filed it, and the root's comments
name what came off it (`gh issue view <n> --json body,comments`). If the work in hand
cannot be traced to a sentence of the root, **say so on the card before you build it**. A
parent card's reasoning is a hypothesis exactly as Steve's diagnosis is, and the frame may
have been set wrong upstream — every card after it inherits that. The root's complaint is
the thing that has to be true at the end.
(riffmap [#429](https://github.com/roloc/riffmap/issues/429), September 19, 2026: the root
said a Friday 1-1 had the sales-call widgets in it. The architect read that as kind
detection, wrote it into the spec, and four correct PRs shipped against that frame over the
next six hours. What actually drew was gated somewhere else entirely, and nobody between
the first card and the last re-read the sentence.)

**Answer your own factual questions. Only judgment goes to Steve.**

- **A fact is anything the data, the code or the page decides**: what drew, what the value
  was, which branch ran, whether it reproduces. **Go and find out.** Never ask Steve to
  recall something a machine recorded.
- **A judgment is what *should* happen** — taste, priority, product direction, what a user
  ought to see. That is his, and asking is right.
- **Never put both in one Stuck card.** A card that asks "what should the rule be, and also
  which widget did you see?" cannot be answered with one letter, so it is not answered at
  all. Answer the fact, then ask the judgment on its own.
- **Before you open a Stuck card, re-read the card that spawned this one.** Steve states his
  goal in the original ask far more often than he restates it later; if that goal already
  decides your question, it is decided — build it, and say on the card which sentence you
  read it from. (riffmap [#429](https://github.com/roloc/riffmap/issues/429) →
  [#431](https://github.com/roloc/riffmap/issues/431), September 19, 2026: the parent card
  said "the sales widgets should only show when it is a sales call", and the child card
  asked Steve whether a sales widget should show on a non-sales call.)

**Build the rule the card names, or say why not.** When Steve states a rule ("only topics
over five minutes"), that rule is the spec. Build it, or put the reason you did not in the
PR body and on the card — never substitute your own rule silently. Not knowing what a
chart should show past the state its mockup drew is a `designer` card, not a guess.

**A change to a screen ships with its picture.** Any PR that touches what a user sees (the
project's web app — riffmap's `web/src/**` — or a rendered page template) runs the
project's screenshot script (riffmap: `cd web && npm run shots`) and you `Read` the PNGs
for every route you touched, before and after, both widths and both themes where the
script makes them. The PR body links the before and the after (the project's hosted
preview URLs where it has them) and says in one sentence what the chart now tells a
reader. If that sentence is "most of it is in one grey bucket", you have not fixed a
chart, you have removed one: stop, and file the designer card instead. The `reviewer`
treats a screen change with no picture as blocking. Tests, lint and a green build are
not a look — they passed on [#359](https://github.com/roloc/riffmap/issues/359).

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

- Open a PR whose body contains **`Closes #<n>`**, so the merge closes the card by itself —
  unless the card's symptom is still there at the end, which is the one case that takes
  `Refs #<n>` instead (next bullet).
  Push with the explicit form `git push -u origin <branch>` — never a bare `git push`
  (some projects hard-block every other shape with a push-gate hook; all of them want this
  one).
- **A symptom card is not done until the symptom is checked again.** A card that reports
  something *observed* — a page that looks wrong, a number that is off, a screen showing
  what it should not — names the artifact that shows it, and §2 had you look at that
  artifact before you decided what was wrong. **Fetch it again before the card closes**, by
  whatever means the project's `CLAUDE.md` gives you, and say in **one sentence what it now
  shows** — in the PR body and in a comment on the card.
  - **The symptom is gone**: say so, and close the card the normal way (`Closes #<n>`,
    merge).
  - **The symptom is still there: the card does not close** — even when the change is
    correct, the reviewer is happy and CI is green. Its PR body says **`Refs #<n>`, never
    `Closes #<n>`**, so the merge leaves the card open; merge it, then go to **Stuck** (§4)
    with what was built, what you expected the artifact to show, and what it still shows.
    **A correct fix for the wrong cause is the outcome this rule exists to catch.**
  - **A card with no observable symptom** — a refactor, a doc, a decision, a piece of
    tooling — is unaffected. There is nothing to fetch and nothing to re-check.
  - If the artifact cannot move until the merge deploys, fetch it anyway and read it
    against the change you made. What you are checking is **whether the thing you changed
    is the thing the artifact shows**, and that is answerable before the deploy: a veto
    with nothing to veto, a ruler that never drew, a branch never reached leaves its trace
    in the artifact you were given. Green tests say the code does what you wrote; only the
    artifact says you wrote the right thing.
- **When the last card spawned from a root card closes, re-check the root's own symptom.**
  Walk the chain up (§2), fetch the root's artifact, and comment what it now shows on the
  **root card**. A root whose children all closed green while its symptom survives is the
  exact shape this catches: if it survives, the root goes to **Stuck** (§4) with what
  shipped and what it still shows. A root card is not closed by its children being done.
  (riffmap [#429](https://github.com/roloc/riffmap/issues/429), September 19, 2026: four
  merged PRs, every link green, and the meeting looked exactly the same at the end as it
  did in the sentence that started it.)
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
  `git fetch origin && git -c merge.conflictStyle=diff3 merge origin/main`. If git merges
  it cleanly, push, wait for CI again, then merge. If it leaves conflict markers, read
  every hunk — `diff3` shows three blocks: yours, the base both branches started from, and
  main's — and sort the conflict into one of two kinds:
  - **Additive: both sides added, nobody rewrote.** In every hunk each side is the base
    text plus new lines, and anything a side did change in the base text the other side
    changed the same way (the seam of a list: a period that became a semicolon on both
    sides so each could append after it). The two additions are different things — two
    entries in a list of mockups, two sections at the end of a doc, two test cases — not
    two versions of one thing. Resolve it yourself: the base text, then main's addition,
    then yours; delete the markers; commit the merge; push; wait for CI; merge. Leave one
    comment on the PR: `🤖 FYI: merged main; kept both sides of an additive conflict in
    <file>.` If CI is red on the merge commit, that is **Stuck** (§4), not a second try.
  - **Anything else** — a side rewrote or deleted lines the other side kept or changed
    differently, the same entry written two ways, a rename against an edit, or you are not
    sure — `git merge --abort` and go to **Stuck** (§4) with the conflicting files and
    which of these it was under the fold. The other change was reviewed and merged on its
    own terms, and deciding what wins is not a builder's call.

  Once per tick: if `main` moves again and conflicts again after your resolution, that is
  Stuck too. (riffmap#349, September 17, 2026: a four-line list conflict, both sides
  appending a mockup entry, went to Steve as a decision and cost a day's round trip.)
- Docs-only changes go through a PR the same way; they get no `reviewer` pass.
- You run with permissions skipped, inside a throwaway box. Nothing prompts and nothing
  is refused. The box is the boundary; the PR, CI and one review pass are the gate. Never
  force-push, never merge red, never push `main` directly, never touch a project's secrets.
- **In review means exactly one thing: the work is finished and only the merge is missing.**
  The PR is green, `reviewer` ran (or the change is docs-only), every item in the card's
  Verification is met, and the merge itself did not go through. Then: move the card to
  **In review**, comment `🤖 FYI: PR #<pr> is green and waiting on a merge.`, and finish the
  tick cleanly. The spawner sends it back to In progress after two hours for one more try
  and parks it in Stuck after that, so it does not rest there.
- **Anything less than finished is Stuck (§4), never In review** — nobody reads In review
  as a queue, and "next tick needs to…" written on an In review card is a note to no one.
  That covers: CI red twice; a blocking `reviewer` finding you disagree with; **`reviewer`
  has not run**; **a Verification item you cannot do from this box** (a file that is not
  here, a machine you cannot reach) — say which, and ask whether to merge without it; and
  **running low on turns or budget**. Past roughly three quarters of either cap, stop
  starting new work: what is left is for leaving the board true — the Stuck comment and
  the move are part of the card, not extra. (riffmap#378, September 17, 2026: a tick ran
  out of budget before its review, left the card In review for "the next tick", and it sat
  for a day while `main` moved three merges and the PR went from green to conflicting.)
- **A card that arrives with its PR already open** (the branch has commits, `gh pr list`
  shows it) came back from In review or from Stuck. Read the last comments on the card and
  the PR, then finish that PR — merge `origin/main`, fix what the comments name, run
  `reviewer` if it never ran, merge. Do not start over and do not open a second PR.

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
label is `reviewer`, whose deliverable is a verdict on somebody else's change rather than
an artifact of its own — route those by hand; the card carries `blocked`; **`reviewer`
finds something blocking that you disagree with** — you do not get to overrule the only
review there is; the work would touch a documented contract the project's `CLAUDE.md`
says is not a builder's call; the required CI check fails twice for a reason you cannot
explain; **the change is built and merged and the card's symptom survives** (§3); or you
have tried the same failing thing twice.

**The "built, but the symptom survives" ask has the same shape as every other.** The first
line is still `Needs Steve:`, and the question is still one question with lettered options
— what changed is that you have a fact to put under it, not an opinion: what shipped, and
what the thing he reported still shows. Say both in plain words ("the fix for X landed and
the page still shows Y"), never the project's name for the code path you changed.

**A measurement or a document is a deliverable, and it is yours** (September 19, 2026).
A card whose `agent:` is the project's measuring agent (`evaluator`) or its docs agent
(`scribe`) used to go straight to Stuck, because what it produces is not a code PR. Both
now run in the tick:

- **A measurement card**: run the project's **offline** harness — the checks its
  `CLAUDE.md` says cost nothing — and post the numbers on the card as an `🤖 FYI:` comment,
  before and after, never an adjective. Then close the card if the card asked only for the
  measurement, or file the follow-on card the numbers call for and link it. **A measurement
  that needs a live model run is still Stuck** (§4), with what it would cost as the
  question — in whichever currency the project bills in, an API charge or the
  subscription's quota. The offline checks are free; a live run is not.
- **A documentation card**: write it and ship it as an ordinary docs-only PR, which gets
  no `reviewer` pass and has CI as its gate. That is a normal merge, not an exception.

If the offline harness itself is red before you change anything, that is **Stuck** — you
are measuring a broken tree and the number would be a lie.

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
  **Including the project's own names for what is on a screen** — describe what Steve
  would see ("the horizontal bar of who talked when"), never the name the code uses for
  it ("the talk ribbon"). He looked at the page, not the spec.
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

## 4b. Declined — the card says not to do it yet

Some cards gate themselves. A card body that says *"do not work this until X"* is an
instruction, and **being handed the card is not the same as X having happened** — Steve
moves cards in batches, and a batch move is not a considered answer to a gate written
weeks earlier. Honor the gate.

**A decline is not Stuck** when the card itself says nothing is broken: Stuck sends Steve
a 🚨 alert and asks him a question, and there is no question here — the card already
states its own condition. It is also **not** a card you leave In progress. Move it back
to **Backlog**:

```
gh issue comment <n> --body-file /tmp/card-<n>-declined.md
board move <n> Backlog
board unclaim <n>
```

**Why Backlog specifically, and why this matters:** the spawner reads a card left
**In progress** with this lane's label as *no progress*, bumps its attempt count, and
**parks it in Stuck after two** — so a tick that correctly obeys a gate gets punished for
it, twice, and then wakes Steve. Moving the card out of In progress is what makes the exit
read as the success it is (`how-it-works.md`, "How a tick ends"). Backlog is the honest
column: *filed, not scheduled*. Nothing pulls from Backlog, so the card waits there until
Steve moves it to Ready again — which is him answering the gate.

The comment is `🤖 FYI:` shaped — nothing is being asked, so **no question and no lettered
options**:

```
🤖 FYI: not doing this yet — the card gates itself.

- The gate: "<quote the card's own words>"
- Why not lifted: <one line — what would count as lifting it, and why being handed the card isn't that>
- Where it is now: back in Backlog. Move it to Ready when the gate is open and I'll pick it up.
```

Report `Outcome: declined (gated)` and `Ended in: Backlog`.

**Only for a gate the card states about itself.** A card you merely think is a bad idea is
not a decline — build it, or take it to Stuck (§4) with the reason as the question. Your
opinion of a card is not a gate; the card's own words are.

## 5. Always leave the board true

Every tick ends with the card in exactly one of: **Done** (merged — the `Closes #n` does
this), **In review** (finished, reviewed, green — only the merge is missing; §3),
**Stuck - Needs help**, **Backlog** (you declined a card that gates itself; §4b), or still
**In progress** if you got nowhere — or if you routed it to another lane (§2), which is
the one In progress exit that is not a failure.

**A merged PR does not always mean Done.** A symptom card whose artifact still shows the
symptom ends in **Stuck - Needs help** with its PR merged and its `Refs #<n>` leaving the
card open (§3). That is a complete, honest tick, not a leak and not a failure: the code
shipped, and the thing Steve reported did not change. Report it as `Outcome: stuck` and
`Ended in: Stuck - Needs help`, and say in the `Needs Steve` line that the fix landed and
the symptom survived.

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
- **Your own dollars are quota, not a bill.** The box runs on a Claude **subscription**
  (`CLAUDE_CODE_OAUTH_TOKEN`; `/etc/watcher/env` may not carry an API key). So
  `--max-budget-usd` is a **quota ceiling**, not a spending limit, and the `total_cost_usd`
  you report is **tokens × API list price, computed client-side** — what the run *would*
  have cost on the API. Nobody is invoiced for it. It still ends the run with
  `error_max_budget_usd` when crossed, which is the point: it stops a tick that is looping.
  Full explanation in `$WATCHER_HOME/docs/how-it-works.md`, "The dollar figure is quota,
  not a bill".
- **The project's own API spend is the one real bill.** When a project calls a model itself,
  through its own env file and its own key, that is money, with no enforced cap unless the
  project's `CLAUDE.md` says otherwise. A project whose lane pins a subscription backend
  (riffmap pins `RIFFMAP_BACKEND=claude`) has no such path. Your quota says nothing about
  that bill, and vice versa.
- **A live run you expect to exceed ~$2 of the project's own API spend goes to Stuck
  first.** Report every run's quota use, and stop if it looks wrong.
- Never `--admin`, never force-push, never merge a red build, never relax a hard rule to
  make a card pass.

## Report

Bullets, five lines, in this order — it is what the log keeps and what Steve skims:

```
- Card: #<n> <title>
- Outcome: merged | PR up, waiting on merge | stuck | declined (gated) | routed to <agent> | no progress
- Ended in: Done | In review | Stuck - Needs help | Backlog | In progress
- Quota: ~$<n> equivalent (subscription — not billed); project API spend $<x> (or "none")
- Needs Steve: no | yes — <one line, the same as the first line of the Stuck comment>
```

A quiet tick is one line: `Quiet tick: nothing claimable in In progress.`
