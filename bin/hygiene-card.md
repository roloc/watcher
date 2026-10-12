**Summary.** The weekly hygiene sweep (`bin/hygiene.sh`, run every Monday by
`.github/workflows/hygiene.yml`) found debt that CI's per-PR gate cannot see: things that
accumulate *between* PRs rather than inside one — abandoned branches, skipped cases, TODOs
nobody filed, scripts nothing runs, links to files that moved. This card clears it. Every
finding below is mechanical and verifiable in seconds; "is this script bad?" is not in
scope here — that is a separate card, filed with evidence.

**The change.** Work the sections below, one commit per section, in one PR. **Delete
rather than preserve**: a stale branch, an orphan script or a dead link has no reader.
Where a finding is legitimately not debt, make it so in the shape the sweep keeps: a
branch holding unshipped work for an open card is renamed `card-<n>-…`; a skipped case or
a TODO with a card behind it names that card (`#n`) on its line. Then the sweep stops
listing it, and the link is the answer. Do not refactor anything the sweep did not name —
the reviewer blocks a diff that reaches past the list.

Branch deletions are not part of a PR and the reviewer cannot see them in one: delete with
`gh api -X DELETE repos/roloc/watcher/git/refs/heads/<branch>` and list every branch you
deleted in one comment on this card.

A section that comes back two sweeps running is a check that should be a CI gate instead
of a weekly finding: say so in a comment here and file that card.

**Verification.** `bin/hygiene.sh` exits 0 on `main` after the merge. Every offline suite
in `.github/workflows/ci.yml` stays green. Nothing here touches what a model reads, so the
deployer ships it without the replay suite, by itself, once the lanes are idle.

**Where to look when it is done** (the Done comment names it): this card — the merged PR
linked from it, and the comment listing every branch deleted. Nothing on the floor page.
The later confirmation is that no new hygiene card appears on the board the next Monday.

**Depends on:** nothing. **Part of:** the weekly hygiene routine — README, "The weekly
hygiene card".

**Replaces:** nothing beyond what the sections below list; the PR carries every file
deletion, the comment on this card carries every branch deletion.

---

