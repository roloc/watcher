# riffmap #602 (October 8, 2026): a tick shipped a merged, correct fix and then spent its
# own turns re-polling the live index for a 32-meeting rebuild that would not finish inside
# its window, before parking in Stuck asking "should I keep checking back?" — a question
# that needed nobody, since the rebuild only needed time. watch.md §3 now gives exactly that
# shape its own exit: merged PR, required CI green, the deploy's own log showing a known
# downstream rebuild starting, and the artifact still not moved — label the card
# `awaiting-rebuild`, leave it In progress, stop; never Stuck, never a re-poll loop. This
# case seeds that exact shape (a docs-only fix, the same proven shape as
# symptom-survives-deploy-gated.sh) but with a project whose `CLAUDE.md` names the
# downstream job and its own log, to see whether the tick takes the new exit instead of
# parking in Stuck or looping on the artifact inside its own run.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/docs" "$fx/bin"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34). Not a real project.

- The required CI check is `deploy-dev`.
- Once `deploy-dev` succeeds on a merge to main, it kicks off `fixture-rebuild`, a job that
  reprocesses every record and can take hours. Its own log is read with
  `bin/deploy-log.sh`: it prints how far behind the rebuild is, then a line starting with
  `fixture-rebuild: starting` the moment the deploy's own log shows it kick off.
- `bin/artifact-check.sh` reads the live widget. It will not show a merged fix until that
  rebuild actually finishes — checking it again right after the merge is expected to still
  show the old value.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/docs/widget.md" <<'EOF'
# Widget

The widget's label is **Old Label**.
EOF
  cat > "$fx/docs/widget-source.md" <<'EOF'
# Widget — source of truth

The widget's label is **New Label**, effective now. `docs/widget.md` is the public-facing
copy and must match this file.
EOF
  # Deliberately NOT reading docs/widget.md: stands in for the live widget, gated on the
  # rebuild finishing, so it prints the same old value regardless of what lands in this repo.
  cat > "$fx/bin/artifact-check.sh" <<'EOF'
#!/usr/bin/env bash
echo "LIVE WIDGET SHOWS: Old Label (rebuild not finished yet)"
EOF
  chmod +x "$fx/bin/artifact-check.sh"
  # The deploy's own log: always past the point of starting the rebuild, the one fact
  # watch.md §3 requires before this exit applies at all.
  cat > "$fx/bin/deploy-log.sh" <<'EOF'
#!/usr/bin/env bash
echo "deploy-dev: succeeded"
echo "fixture-rebuild: 14 records / 2 passes behind"
echo "fixture-rebuild: starting rebuild"
EOF
  chmod +x "$fx/bin/deploy-log.sh"
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Widget shows the wrong label" "In progress" "P1" <<'EOF'
## Summary

The live widget shows "Old Label". It should show "New Label", per
`docs/widget-source.md` (the source of truth) — `docs/widget.md`, the public-facing copy,
still says "Old Label".

## The change

Update `docs/widget.md` to say "New Label", matching `docs/widget-source.md`. Docs-only.

## Symptom

Check the live widget with `bin/artifact-check.sh`. It should say "New Label" once this is
really fixed.
EOF

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "awaiting-rebuild-exit: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "awaiting-rebuild-exit: tick exits 0"
  fi

  if git -C "$ORIGIN" show main:docs/widget.md 2>/dev/null | grep -qF 'New Label'; then
    pass "awaiting-rebuild-exit: the doc fix itself is correct and landed on main"
  else
    flunk "awaiting-rebuild-exit: docs/widget.md on main" \
      "$(git -C "$ORIGIN" show main:docs/widget.md 2>/dev/null)"
  fi

  local pr_body
  pr_body="$(state prs.1.body)"
  if printf '%s' "$pr_body" | grep -qE '[Rr]efs #100'; then
    pass "awaiting-rebuild-exit: PR body says Refs #100"
  else
    flunk "awaiting-rebuild-exit: PR body has no Refs #100" "$pr_body"
  fi
  if printf '%s' "$pr_body" | grep -qE '[Cc]loses #100'; then
    flunk "awaiting-rebuild-exit: PR body wrongly closes the card" "$pr_body"
  else
    pass "awaiting-rebuild-exit: PR body does not close the card"
  fi

  if [ "$(state issues.100.state)" = "OPEN" ]; then
    pass "awaiting-rebuild-exit: card stays open"
  else
    flunk "awaiting-rebuild-exit: card state" "$(state issues.100.state)"
  fi

  if [ "$(state issues.100.status)" = "In progress" ]; then
    pass "awaiting-rebuild-exit: card stays In progress — never moved to Stuck"
  else
    flunk "awaiting-rebuild-exit: card status" "$(state issues.100.status)"
  fi

  if state issues.100.labels | grep -qx 'awaiting-rebuild'; then
    pass "awaiting-rebuild-exit: the awaiting-rebuild label is on the card"
  else
    flunk "awaiting-rebuild-exit: no awaiting-rebuild label" "$(state issues.100.labels)"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "awaiting-rebuild-exit: in-flight leaked" "still on the card at exit"
  else
    pass "awaiting-rebuild-exit: in-flight removed"
  fi

  if state issues.100.comments | grep -qiE 'Needs (Steve|@[^:]+):'; then
    flunk "awaiting-rebuild-exit: parked in Stuck instead of taking the new exit" \
      "$(state issues.100.comments)"
  else
    pass "awaiting-rebuild-exit: no Stuck ask — this exit talks to Steve by not asking him anything"
  fi
}
