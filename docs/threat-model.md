# Threat model

What the box holds, what the GitHub Apps it acts as (or, on a fork, the one bot account)
can and cannot do, why a tick runs with permissions skipped there and nowhere else, and
what actually stops a bad PR from merging.

## What the box holds

- GitHub App keys, one author and one reviewer per project, in `/etc/watcher/apps/<project>/`
  (written by `deploy/app-setup.py`, never in git or a transcript). Each App is installed on
  that project's repos only, so a leaked or revoked key touches one project. The author key is
  readable by the `watcher` user; the reviewer key only by the separate `watcher-reviewer`
  user, and `watcher` may run exactly one program as it (`bin/review-submit.sh`, a root-owned
  copy, one sudoers line) that files a review on a PR's current head. A tick therefore cannot
  approve its own PR, and the guard refuses that helper and `sudo` to that user from its shell. The helper reads its body from stdin only, never a path (it runs as the user that can read the key), and takes the repo from the caller, so it can review on any repo where some project's reviewer App is installed — the same trust as a `🤖 reviewer:` comment.
  Tokens are minted per call and last an hour; none is stored. Nothing on your own personal
  account. (A fork on one bot account instead: a `gh auth login` for that bot, run once as the
  `watcher` user — write access to whatever repo you granted it, and the board. On the
  watcher's own box that login is still there too, for the readers that run outside a lane
  and so without the `gh` shim — the floor page, the hygiene timer, the detector and the
  deployer's check-run read — until the machine account is retired, card
  [#255](https://github.com/lonestarprod/watcher/issues/255)'s last step.)
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

- **Is:** the credentials above, and checkouts of code that is also on GitHub —
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

## What the bot (the author App, or a fallback bot account) can do

- Push a branch, open a PR, comment on a card or a PR, move a card on the board, merge its
  own PR — but only once the gate below agrees to the merge.
- Read anything its GitHub token can read: the repo it was granted (an App: the repos it
  is installed on, with the permissions its manifest names — Contents, Pull requests,
  Workflows, Issues and the organization's Projects, all write, for the author; Pull
  requests write, Contents read and Actions write for the reviewer), the board, issues and
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
   push to `main`). Neither App has the Administration permission (their manifests do not
   ask for it), and a fallback bot account has no admin on the repo, so the loop cannot
   weaken this setting even by mistake — only you, running `add-project.sh` as yourself,
   can change it.
2. **The project's own required CI check** — whatever that project names as its one
   required check.
3. **The review receipt** (`bin/receipt-check.sh`, card #142) — a second, independent
   required check, on this repo already. `deploy/add-project.sh` only ever sets up the
   first check above for a *project this watcher works*; a forked project adds its own
   `review-receipt` job the way `templates/project-CLAUDE.md` says, the first time it
   gets a build lane. A code PR cannot merge without the reviewer's clean verdict on that
   exact PR naming its *current head commit*: a real GitHub review from the project's
   reviewer App (`roloc-<project>-reviewer[bot]`, `RECEIPT_REVIEWER_APPS` for a fork's own
   prefix) — Approve on that SHA passes, Request changes on it fails even next to a clean
   comment — or, where there is no reviewer App, a `🤖 reviewer:` comment with the same
   SHA (and a matching `🤖 usability:` one, for any change to a screen). This check reads
   only the PR's own reviews and comments and its own current head SHA — never anything
   the tick says about itself — so an amend made after the review, or a review that simply
   never ran, fails this check instead of merging silently.

**The tick cannot forge the third one.** It can ask `agents/reviewer.md` to review its
own change, and it can even disagree with what comes back — but the verdict comes from a
separate review pass with no tools of its own beyond reading the diff, and
`receipt-check.sh` only ever trusts what that pass actually posted on the PR, checked
against the commit that is actually about to merge. The review itself is signed by a key
the tick cannot read: `reviewer.pem` belongs to the `watcher-reviewer` user, and the one
road to it is `bin/review-submit.sh` through a single sudoers line — a program that takes
the PR, the SHA, the verdict and the body on stdin, validates each, and refuses a SHA that
is not the PR's head right now. The guard mod refuses `sudo` and that helper from a tick's
shell, so only the receipts hook, with the reviewer subagent's own first line, ever calls
it. A tick that skips the review, or whose branch moves after the review ran, cannot
merge, regardless of what it claims in its own report.

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
- `bin/review-submit.sh`, `bin/gh-app-token.sh`, `deploy/app-setup.py` — the reviewer App's
  helper, the token minter, and how a project's Apps are made (card
  [#255](https://github.com/lonestarprod/watcher/issues/255)).
- `deploy/add-project.sh` — what branch protection it actually sets.
