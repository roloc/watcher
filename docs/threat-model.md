# Threat model

What the box holds, what the bot account can and cannot do, why a tick runs with
permissions skipped there and nowhere else, and what actually stops a bad PR from merging.

## What the box holds

- A bot GitHub login (`gh auth login`, run once as the `watcher` user) — write access to
  whatever repo you granted it, and whatever the board's owner granted it on the board.
  Nothing on your own personal account.
- A Claude **subscription** token, `CLAUDE_CODE_OAUTH_TOKEN` in `/etc/watcher/env` — never
  an API key; that file is never allowed to carry `ANTHROPIC_API_KEY`, because the key
  would outrank the login and bill your API account instead of spending the subscription's
  own quota (`docs/how-it-works.md`, "The dollar figure is quota, not a bill").
- Checkouts: one clone per project, plus one git worktree per card in flight.
- Optionally, a Discord and/or Slack webhook URL — write-only, used only to post when a
  card is stuck or something broke.
- Nothing else. No keys to other hosts, no cloud credentials, no production secrets —
  unless you put one there yourself, which this threat model assumes you never do (next
  section).

## Why permissions are skipped here, and nowhere else

Every tick runs `claude -p ... --dangerously-skip-permissions`. No confirmation prompt
exists on the box; nothing a tick does waits on a human to approve it, mid-run.

That is only safe because of what is, and is not, on the box:

- **Is:** the two credentials above, and checkouts of code that is also on GitHub —
  nothing here is the only copy of anything.
- **Is not:** anything whose loss or misuse would actually cost something — a production
  database, a cloud account, a payments key, your own GitHub token.

If a tick ever does something wrong, the fix is "rebuild the box" — not "undo what it did
to something valuable." On a developer's own laptop the same loop needs an allow-list, a
trust probe and a classifier around every tool call instead, and the one time that was
tried it cost more engineering time than the feature work it was meant to save. The
throwaway box is the cheaper boundary, and it only works as long as the box stays
throwaway.

**This is a one-way door.** Put a credential for anything else on this box — a production
secret, a second project's deploy key, your own personal token — and the box stops being
throwaway: a tick can now reach something worth protecting, unsupervised, with no prompt
in its way. Don't add one. If a project needs to call something with real stakes, that
call belongs in the project's own deploy pipeline, off this box, not in a tick.

## What the bot account can do

- Push a branch, open a PR, comment on a card or a PR, move a card on the board, merge its
  own PR — but only once the gate below agrees to the merge.
- Read anything its GitHub token can read: the repo it was granted, the board, issues and
  PRs.
- Spend Claude subscription quota, up to `TICK_MAX_BUDGET_USD` per tick (`bin/spawn.sh`,
  default $15) — an imputed usage meter, not a bill (see above).

## What it cannot do

- **Merge without the gate below agreeing.** Merging is the one action something outside
  the tick's own say-so actually checks.
- **Reach anything that is not the repo it was granted, the board, and this box.** Not
  your personal account, not your laptop, not another project's checkout.
- **Spend real money on its own.** The watcher's own loop runs on the subscription only. A
  *project* calling a model through its own API key is a separate, project-level choice
  that project's own `CLAUDE.md` names — the watcher's quota ceiling says nothing about
  that bill, and vice versa.
- **Force-push, merge a red build, change `main`'s branch protection, or push directly to
  `main`.** `commands/watch.md`'s hard rules forbid all four; the branch protection below
  forbids them a second time, independently of whether the tick behaves.

## The gate — what actually stops a bad PR

Three independent things have to agree before any PR lands on `main` of a project set up
the way this repo's own template means (both required checks below in place), and nothing
the tick does can satisfy more than one of them:

1. **Branch protection on `main`**, set by `deploy/add-project.sh` when the project was
   added: the project's own required CI check must pass, and a PR is required (no direct
   push to `main`). The bot account has no admin on the repo, so it cannot weaken this
   setting even by mistake — only you, running `add-project.sh` as yourself, can change it.
2. **The project's own required CI check** — whatever that project names as its one
   required check.
3. **The review receipt** (`bin/receipt-check.sh`, card #142) — a second, independent
   required check, on this repo already. `deploy/add-project.sh` only ever sets up the
   first check above for a *project this watcher works*; a forked project adds its own
   `review-receipt` job the way `templates/project-CLAUDE.md` says, the first time it
   gets a build lane. A code PR cannot merge without a `🤖 reviewer:` comment on that
   exact PR naming its *current head commit* with a clean verdict (and a matching
   `🤖 usability:` one, for any change to a screen). This check reads only the PR's own
   comments and its own current head SHA — never anything the tick says about itself — so
   an amend made after the review, or a review that simply never ran, fails this check
   instead of merging silently.

**The tick cannot forge the third one.** It can ask `agents/reviewer.md` to review its
own change, and it can even disagree with what comes back — but the verdict comes from a
separate review pass with no tools of its own beyond reading the diff, and
`receipt-check.sh` only ever trusts what that pass actually posted on the PR, checked
against the commit that is actually about to merge. A tick that skips the review, or whose
branch moves after the review ran, cannot merge, regardless of what it claims in its own
report.

## What a misbehaving tick could actually do, worst case

- Push branches, open PRs nobody asked for, leave noisy comments, move cards around the
  board. All of it is visible — every PR, every comment, the floor page — and all of it is
  reversible: delete the branch, close the PR, move the card back.
- It cannot merge any of that without all three gates above agreeing, and it cannot touch
  anything that is not this box, the repo it was granted, and the board.

## Further reading

- `commands/watch.md` — the tick's own rules, in full.
- `docs/how-it-works.md`, "Hard rules for every agent" and "The dollar figure is quota, not
  a bill".
- `bin/receipt-check.sh` — the review-receipt check, in full.
- `deploy/add-project.sh` — what branch protection it actually sets.
