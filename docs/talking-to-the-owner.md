# Talking to the owner

The owner is the only human in this loop. Assume the reader is dyslexic and on a phone
between meetings — that is the design assumption behind every rule here, not a biography.
A wall of prose does not get read. A short list with one clear question gets answered in a
minute.

This contract covers **every message aimed at the owner**:

- a **Stuck** card comment written by the `/watch` tick;
- the spawner's own park / sweeper comments (`bin/spawn.sh`);
- a reviewer finding that is headed to the owner;
- the tick's end-of-run report;
- a PR description or decision doc the owner is expected to read.

It does **not** cover agent-to-agent text (a reviewer report the tick consumes, a code
comment, a log line). Those can be as dense as they need to be.

## The rules

1. **First line is the ask.** Bold, one sentence, starts with `Needs @<login>:` (the real
   GitHub login that environment's `OWNER_LOGIN` variable names; the old unadorned
   `Needs Steve:` only if it is somehow unset). If nothing is needed from them, start with
   `FYI:` instead and keep the whole message to three bullets.
2. **Bullets, not paragraphs.** At most 8 bullets above the fold. One fact per bullet. Aim
   for 15 words or fewer per bullet.
3. **Exactly one question**, written as a question and ending in `?`. Never two questions.
   If you have two, pick the one that unblocks the most and put the other under details.
4. **Lettered options, one line each.** `A.`, `B.`, `C.`. Put your recommendation **first**
   and mark it `(recommended)`. Three options is plenty. Two is fine.
5. **Say how to answer.** Always end with the exact action: *"Reply A, B or C on this card,
   or say which way you lean, then move it back to In progress."*
6. **Reading a reply.** A letter, or a stated preference in any words ("leaning A", "A and
   C first", "go with the first one"), is the answer; record it and proceed. If the reply
   carries a preference *and* a request (evidence, a change, a further question), do the
   request as part of carrying out the preference, and say on the card which sentence you
   read the choice from. Only a reply that is purely a question, with no preference
   anywhere in it, gets a second round — and a third, and however many it honestly takes:
   this is never license to guess the decision yourself. A product decision stays
   the owner's, no matter how many rounds it takes to get.
7. **Bold only what decides.** Bold the option labels and the one word that differs between
   options. Do not bold whole sentences.
8. **Plain words above the fold.** No file paths, commands, code, stack traces, or
   unexpanded acronyms in the bullets. Say "the build" not "CI `verify`", say "the meeting
   summary format" not "the `MeetingMap` contract".
   **This covers the project's own names for the things on a screen.** A widget, panel,
   chart or field has an internal name in the spec and the renderer, and the owner did not
   read either — they looked at the page. Describe what they would see: "the horizontal
   bar of who talked when", not "the talk ribbon". If a name cannot be avoided, describe it
   first and put the name in brackets after. **Asking them to choose between two things
   they have no name for is asking them nothing at all** (riffmap
   [#431](https://github.com/roloc/riffmap/issues/431), September 19, 2026: a card asked
   whether the owner had seen "the ruler" or "the ribbon"; their answer was that there is
   nothing called a ribbon on that page. Both are real names in `template.html`. Neither is
   a name a reader has).
9. **Details go under a fold.** Everything an engineer would want (paths, diffs, logs, the
   exact error) goes at the bottom inside `<details><summary>Details</summary> … </details>`.
   A link is not a detail — see the next rule.
10. **Links they have to open go right above the question.** A hosted mockup, a
    screenshot, a live page, the PR that shows the thing: a `**Look:**` list between the
    bullets and the question, one link per line, what it is in plain words and then the
    URL. The fold is collapsed on the phone app, and the owner reviews on their phone, so a
    link under it is a link they never see. A repo path is not a link; it stays under the
    fold. A direct link to an image (`.png`, `.jpg`, `.gif`, `.webp`) is also what the
    floor page's Needs-the-owner carousel shows inline, captioned with the plain words
    written next to it — a `.html` mockup or a live page has nothing to embed there and
    stays a link.
11. **American English**, dates like *September 7, 2026*. Never *favour*, *optimise*,
    *behaviour*.
12. **Never write "needs input".** Say what input. Never restate the card title as the ask.

A good test: could the owner answer with a single letter, from the first screen, without
opening the details? If the answer needs them to look at something, is the link to it on
that first screen? If not, rewrite.

## Templates

### A Stuck card comment (the tick)

```
🤖 **Needs @<login>: <one sentence saying what you need decided>**

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

Reply A, B or C on this card, or say which way you lean, then move it back to **In progress**.

<details><summary>Details</summary>

- Branch / PR: …
- Exact error: …
- Files: …
- What the reviewer said: …

</details>
```

### The spawner's park comment (generated by `bin/spawn.sh`)

The spawner cannot know *why* a card failed, only that it failed twice. Its comment says
that, offers the three moves the owner can make, and points at the log under the fold.
Keep any edit to it inside these rules; a test in `bin/test-spawn.sh` checks the shape.

### A reviewer finding headed to the owner

The reviewer talks to the tick, not to the owner. But when a finding is going to end up on
the card (a blocking finding the tick disagrees with, or a third round), the reviewer adds
a `For the owner` block at the end of its report in the shape above, so the tick can paste
it into the Stuck comment unchanged.

### The tick's end-of-run report

```
- Card: #<n> <title>
- Outcome: merged | PR up, waiting on merge | stuck | declined (gated) | routed to <agent> | no progress | quiet tick
- Ended in: Done | In review | Stuck - Needs help | Backlog | In progress
- Quota: ~$<n> equivalent (subscription — not billed); project API spend $<x> (or "none")
- Needs the owner: no | yes — <one line, same as the first line of the Stuck comment>
```

A quiet tick (nothing claimable) is one line: `Quiet tick: nothing claimable in In progress.`

## Before and after

**Before** (what not to write):

> Needs input. I looked into the transcript ingestion path and there are a couple of ways
> this could go: we could either extend the existing MeetingMap schema with an optional
> speakers array, which would require touching the JSON contract and the four fixtures
> under test/fixtures, or we could keep the schema as-is and derive speakers at render time,
> which is cheaper but loses the information for downstream consumers. There's also the
> option of a separate sidecar file. Let me know how you'd like to proceed.

**After:**

> 🤖 **Needs @roloc: pick where speaker names live in the meeting summary.**
>
> - Card: add speaker names to the meeting summary.
> - Done so far: the code change is ready either way.
> - Blocker: one option changes a format other tools rely on. That is your call, not mine.
>
> **Question: where should speaker names live?**
> - **A. Inside the summary format** (recommended) — one source of truth; changes a shared format.
> - **B. Worked out at display time** — no format change; other tools never see the names.
> - **C. In a separate file** — no format change; two files to keep in sync.
>
> Reply A, B or C on this card, or say which way you lean, then move it back to **In progress**.
>
> <details><summary>Details</summary>
>
> - Branch `card-87`, no PR yet.
> - A touches the `MeetingMap` JSON contract and 4 fixtures under `test/fixtures/`.
>
> </details>
