# The board — how cards are made, read and moved

The board is GitHub Projects <https://github.com/users/roloc/projects/2> (board number 2,
owner `roloc`); every card is an issue in `roloc/watcher`. This is the one page for the
mechanics, in the shape of riffmap's and azeroth-rewind's `docs/board.md`, so a session
told "file a card" or "take a look at #n" knows what to run without going to look. The
owner watches the board, Discord and the floor page, not chat, so anything that needs them
is a card here.

## Reading a card ("take a look at #n")

One call gives the whole card: body, every comment, labels, and which column it is in.

```bash
gh issue view <n> -R roloc/watcher --json title,state,labels,body,comments,projectItems
```

- **The ask is the body's first line** — unless the owner commented later. **The owner's
  newest comment outranks the body** (watcher
  [#185](https://github.com/roloc/watcher/pull/185), October 17, 2026: five ticks answered
  the body and repeated a park he had already rejected). Read the comments before deciding
  what the card wants.
- **The column** is `projectItems[].status.name` (Backlog, Ready, In progress, In review,
  Done, Stuck - Needs help). A card with no `projectItems` is an issue nobody put on the
  board; see "Filing".
- **A Stuck card's question** is its newest `Needs @roloc:` comment. `python3 bin/board.py
  ask <n>` prints that one line, `images <n>` the pictures under its `Look:` section. The
  owner answers by replying on the card and moving it back to In progress; a `steve-decision`
  card ends when the owner's part is done.
- **What the lane is doing with it** is the floor page (port 8787 on the box), not the
  card: a desk per lane, and Peek for the tick's own session. `in-flight` on the card means
  a tick holds it right now; never add or remove that label by hand from a Mac session
  (`bin/board.py unclaim <n>` is for a leaked one, after the tick is gone).
- **Its PR**, if any: `gh pr list -R roloc/watcher --search "<n> in:body" --state all`
  (a tick's PR says `Closes #<n>`).

`bin/board.py show <n>` (live, bypasses the cache) and `list "<column>"` need the lane's
env for the board's owner and number:

```bash
set -a; source projects/watcher.env; set +a; python3 bin/board.py show <n>
```

Never list the whole board to find one card — `gh issue view --json projectItems` is
cheap, `gh project item-list` is what blew the GraphQL budget on October 8 and 9, 2026
([#114](https://github.com/roloc/watcher/issues/114)).

## Filing a card ("file a card for this")

Write the body in a scratch file and pass `--body-file`: bodies are long and full of
backticks. Then one command:

```bash
gh issue create -R roloc/watcher --title "<what ships, in one line>" --body-file card.md
```

**This board's Auto-add workflow is on**, so a new issue lands in **Backlog** by itself
(the weekly hygiene card is filed with `gh issue create` alone and arrives there). Confirm,
because a card that is not on the board is invisible to the lane:

```bash
gh issue view <n> -R roloc/watcher --json projectItems --jq '.projectItems[].status.name'
```

If that prints nothing, put it on by hand:
`gh project item-add 2 --owner roloc --url https://github.com/roloc/watcher/issues/<n>`.

**It stays in Backlog unless the owner says otherwise.** Backlog is never pulled from; it
is where a card waits for the owner to move it. "Put it in Ready" is
`python3 bin/board.py move <n> Ready` (with the env sourced, above) — and on this board
**Ready is the lane's queue** (`READY_PULL=1`): the lane promotes the top Ready card to In
progress and starts on it the next time nothing is in flight, so moving a card there is
starting the work, not scheduling it.

**A card that waits on another one** records the relationship — the words are for the
owner, the field is what the lane reads before it picks any card:

```bash
id="$(gh api repos/roloc/watcher/issues/<m> --jq .id)"
gh api -X POST repos/roloc/watcher/issues/<n>/dependencies/blocked_by -F issue_id="$id"
```

### Labels

This board has one lane and no `agent:` labels: the `watcher@watcher` lane takes every
card. The labels that mean something:

| label | meaning |
|---|---|
| `steve-decision` | a product decision for the owner, never a tick's; the card asks, lettered options, and ends when they answer |
| `blocked` | straight to Stuck |
| `deploy-anyway` | on a "Deploy held" card: ship that version anyway |
| `in-flight` | a tick holds the card right now — the lane's own, never set by hand |

Priority is the board's **Priority** field (P0–P2), not a label.

### The body

The owner's own cards are the house shape — [#182](https://github.com/roloc/watcher/issues/182)
and [#179](https://github.com/roloc/watcher/issues/179) are full examples:

1. **`**Ask:**`** — one line, bold, what ships. The first line is the ask, always.
2. **`## Why now`** — the incident, the reading, the thing the owner saw, with the date.
3. **`## What the owner gets`** — what changes in what they can see or do; "nothing they
   see changes" is a fine answer.
4. **`## How`** — files, functions, the exact lines when it is a wording change; what is
   *unchanged* too.
5. **`## Rules that still hold`** — when the change touches the gate (`commands/`,
   `agents/`, `deploy/`, the workflows): the reviewer runs, the replay suite runs, which
   cases change and which do not.
6. **`## Done when`** — what a reviewer can check: the test case that reproduces the
   symptom, the suites, "verify and review-receipt green; merged". For a change to a
   screen, the picture. For anything the floor page shows, what the owner sees when they
   open it the day it ships.
7. **`Replaces:`** — what this card makes obsolete and the PR deletes, or
   `Replaces: nothing`. Never left out.
8. **`<details><summary>Details</summary>`** — the history: which card this follows on
   from, the owner's words that asked for it, dated.

**Depends on** and **Part of** are Markdown links, never bare `#nnn` — a bare number is
dead text in chat and points at the wrong repo when quoted elsewhere. Every word a human
reads follows `docs/talking-to-the-owner.md`: first line is the ask, bullets, one
question, lettered options, how to answer.

## Moving a card

The owner moves cards on the board. From a session, `python3 bin/board.py move <n>
"<column>" [--why "<one line>"]`; `stage <n> "<stage>"` sets the Stage (`-` empties it).
A move to Stuck is the one move that reaches Discord, and it carries the card's newest
`Needs @roloc:` comment (or `--why`), so write that comment first. Every other move is
silent; the floor page shows it. A session on a Mac has no reason to move a card except
on the owner's word — which card a lane gets is the spawner's decision, and
`docs/how-it-works.md`, "Hard rules for every agent", is the list of what a tick never does
to the board.

## When something here is wrong

A board edit (a renamed column, a new field) is not a code change: `bin/board.py` resolves
fields by name and retries once live on a miss. If a command on this page stops working,
the fix is a card, filed in the shape above, naming the command and what it printed.
