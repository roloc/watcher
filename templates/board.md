<!--
The one page a project keeps for its board: how a card is filed, read and moved, and the
body shape its cards use. Copy it to the project's docs/board.md and fill in every
<bracket> — deploy/add-project.sh prints the sed line that fills the three it knows
(<owner/repo>, <board owner>, <board number>); <owner login> is the GitHub login of the
person who answers a Stuck card. Delete a sentence only when it is untrue for the project
(most are not). This repo's own docs/board.md is this file filled in for the watcher's own
board — the worked example to read beside it.

Why this page exists: a session told "file a card" or "take a look at #n" with no page to
open goes and inspects other cards to work out the mechanics, every time. The page is the
answer, so the session runs what it says and says nothing about checking how cards are
filed.
-->

# The board — how cards are made, read and moved

The board is GitHub Projects `<board owner>/<board number>`; every card is an issue in
`<owner/repo>`. This is the one page for the mechanics. The owner watches the board, the
Discord or Slack channel and the floor page — from a phone — not chat or `docs/`, so
anything that needs them is a card here, written the way `docs/talking-to-the-owner.md`
in the watcher repo says: first line is the ask, bullets, one question, lettered options,
how to answer.

## Reading a card ("take a look at #n")

One call gives the whole card: body, every comment, labels, and which column it is in.

```bash
gh issue view <n> -R <owner/repo> --json title,state,labels,body,comments,projectItems
```

- **The ask is the body's first line** — unless the owner commented later. **The owner's
  newest comment outranks the body.** Read the comments before deciding what the card
  wants; a comment that rejects a plan or a park is the card's current state, whatever
  the body still says.
- **The column** is `projectItems[].status.name` (Backlog, Ready, In progress, In review,
  Done, Stuck - Needs help). A card with no `projectItems` is an issue nobody put on the
  board; see "Filing".
- **A Stuck card's question** is its newest `Needs @<owner login>:` comment.
  `python3 "$WATCHER_HOME/bin/board.py" ask <n>` prints that one line, `images <n>` the
  pictures under its `Look:` section. The owner answers by replying on the card and
  moving it back to In progress; a `steve-decision` card ends when the owner's part is
  done.
- **What the lane is doing with it** is the floor page (port 8787 on the watcher box),
  not the card: a desk per lane, and Peek for the tick's own session. `in-flight` on the
  card means a tick holds it right now; never add or remove that label by hand
  (`board.py unclaim <n>` is for a leaked one, after the tick is gone).
- **Its PR**, if any: `gh pr list -R <owner/repo> --search "<n> in:body" --state all`
  (a tick's PR says `Closes #<n>`).

`board.py show <n>` (live, bypasses the cache) and `list "<column>"` need the board's
owner and number in the environment — the lane's own `projects/<name>.env` in the watcher
repo has them:

```bash
BOARD_OWNER=<board owner> BOARD_NUMBER=<board number> python3 "$WATCHER_HOME/bin/board.py" show <n>
```

Never list the whole board to find one card — `gh issue view --json projectItems` is
cheap, `gh project item-list` is what blew a whole hour's GraphQL budget once (watcher
[#114](https://github.com/roloc/watcher/issues/114)).

## Filing a card ("file a card for this")

Write the body in a scratch file and pass `--body-file`: bodies are long and full of
backticks. Then one command:

```bash
gh issue create -R <owner/repo> --title "<what ships, in one line>" --body-file card.md
```

**The board's Auto-add workflow puts a new issue in Backlog by itself** (filter
`is:issue is:open`; `bin/doctor.sh` checks it is on). Confirm, because a card that is not
on the board is invisible to the lane:

```bash
gh issue view <n> -R <owner/repo> --json projectItems --jq '.projectItems[].status.name'
```

If that prints nothing, put it on by hand:
`gh project item-add <board number> --owner <board owner> --url https://github.com/<owner/repo>/issues/<n>`.

**It stays in Backlog unless the owner says otherwise.** Backlog is never pulled from; it
is where a card waits for the owner. "Put it in Ready" is
`board.py move <n> Ready` (with the env set, above) — and on a board with `READY_PULL=1`
**Ready is the lane's queue**: the lane promotes the top Ready card to In progress and
starts on it the next time nothing is in flight, so moving a card there is starting the
work, not scheduling it. Moving a card to **In progress** is the go signal on every board.

**A card that waits on another one** records the relationship — the words are for the
owner, the field is what the lane reads before it picks any card:

```bash
id="$(gh api repos/<owner/repo>/issues/<m> --jq .id)"
gh api -X POST repos/<owner/repo>/issues/<n>/dependencies/blocked_by -F issue_id="$id"
```

Only a real prerequisite — work that must be **merged** before this card can be built or
verified. A 422 means it is already recorded. Never remove one somebody else set.

### Labels

| label | meaning |
|---|---|
| `agent: <role>` | <which lane works the card — one label per lane in `projects/<name>-<lane>.env`'s `LANE_AGENTS`; delete this row when the project has one lane that takes every card.> Leaving it off is fine: the triage lane reads an unlabeled card and adds the one its words call for |
| `steve-decision` | a product decision for the owner, never a tick's; the card asks, lettered options, and ends when they answer |
| `blocked` | straight to Stuck |
| `awaiting-rebuild` | fix merged, waiting on a known downstream rebuild; set by a tick, never by hand |
| `in-flight` | a tick holds the card right now — the lane's own, never set by hand |

Priority is the board's **Priority** field (P0–P2), not a label.

### The body

1. **`**Ask:**`** — one line, bold, what ships. The first line is the ask, always.
2. **`## Why now`** — the incident, the reading, the thing the owner saw, with the date.
3. **`## What the owner gets`** — what changes in what they can see or do; "nothing they
   see changes" is a fine answer.
4. **`## How`** — files, functions, the exact lines when it is a wording change; what is
   *unchanged* too.
5. **`## Done when`** — what a reviewer can check: the test that reproduces the symptom,
   the suites, "CI green; merged". It can carry a ` ```verify ` block — shell lines, run
   from the worktree root once CI is green, that prove the card done; the tick pastes the
   block and its output on the PR, and a non-zero exit cannot merge. For a change to a
   screen, the picture. For anything the owner opens, what they see when they open it the
   day it ships.
6. **`Replaces:`** — what this card makes obsolete and the PR deletes, or
   `Replaces: nothing`. Never left out; the reviewer checks the deletion happened.
7. **`<details><summary>Details</summary>`** — the history: which card this follows on
   from, the owner's words that asked for it, dated.

**Depends on** and **Part of** are Markdown links, never bare `#nnn` — a bare number is
dead text in chat and points at the wrong repo when quoted elsewhere.

<How a tick reads the project's own data — the page a card links, the files it names —
and the one ask when that fails. Delete when every page is public.>

## Columns and who moves what

| column | meaning | moved by |
|---|---|---|
| **Backlog** | filed, not scheduled | Auto-add lands here |
| **Ready** | the owner wants it next; with `READY_PULL=1`, the lane pulls from it | the owner |
| **In progress** | claim me — the lane picks the first row without `in-flight` | the owner (the go signal), or the lane promoting a Ready card |
| **In review** | PR open, CI running | the tick |
| **Stuck - Needs help** | a question on the card, or two failed attempts | the tick, or anyone with a question for the owner |
| **Done** | `Closes #<n>` in a merged PR body moves it here by itself | automation |

```bash
python3 "$WATCHER_HOME/bin/board.py" list "In progress"            # TSV; exits 1 on an unknown column
python3 "$WATCHER_HOME/bin/board.py" move <n> "Stuck - Needs help" [--why "<one line>"]
python3 "$WATCHER_HOME/bin/board.py" stage <n> "Building"           # the Stage field — what a tick is doing right now
```

**Asking the owner something:** comment the ask on the card first — bold first line
`Needs @<owner login>: <one sentence>`, then the options lettered, how to answer — then
`move` it to Stuck. That order makes the Discord or Slack alert quote the question. A move
to Stuck is the one move that reaches the channel; every other move is silent, and the
floor page shows it.

**Closing:** `Closes #<n>` in the PR body. A merged PR without it leaves the card open;
`gh issue close <n> --comment "<what shipped, link to the PR>"` fixes that. The closing
comment says where to look and what is there right now.

**The owner never merges.** They read, reply on cards, and move them between columns.
Every merge, every follow-on card, every close is a tick's job, or a session's on the
owner's word. A session with no such word reads and reports; it never claims a card,
moves one, or tidies the board — which card a lane gets is the spawner's decision.
