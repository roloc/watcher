# riffmap#447 (September 20, 2026): one architect tick filed three cards at 02:50, each
# one's body naming the one before it in prose, none of them recorded as a real GitHub
# dependency. Thirteen hours later a renderer tick picked up the last of the three, spent
# its whole run establishing that the contract field it needed did not exist yet, and
# parked in Stuck to ask Steve a question the first card's own body had already answered.
# watch.md §2 now says filing a follow-on that waits on another is two commands, not one
# sentence: `gh api repos/$GH_REPO/issues/<m> --jq .id`, then a POST to that issue's own
# `dependencies/blocked_by`. This seeds a card whose own scope is small (add a flag) but
# whose body explicitly asks for a follow-on that cannot be verified until this one merges
# — the shape that should produce a real recorded dependency, not just words in the body.
#
# Reshaped October 16, 2026. Until then the only prerequisite in the fixture was card #100
# itself — the card in hand — and the card's words were "once this lands, file a follow-on
# ... it depends on this card". Every tick filed the follow-on after the merge, when #100
# was already closed, and watch.md §2's own rule limits a recorded dependency to "work that
# must be **merged** before this card can be built or verified": at that moment there was
# no such work. Four kept runs on the box still recorded it (each wrote "Depends on #100
# (merged)"), and the deployer's ledger has two runs (04fd3ef and 0b89b08, October 15)
# that filed the card, named #100 in its body, and recorded nothing — 6/8, a deploy
# attempt each — which is the rule read literally, not the riffmap#447 shape. So the
# prerequisite is now a card that is open when the follow-on is filed: #101, a rewrite of
# the very file the follow-on edits, in progress on another lane. The follow-on cannot
# land before #101 does, whatever happens to #100, so the relationship the check looks for
# is one the rule itself demands.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/bin" "$fx/docs"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34/#43). Not a real project.

- The required CI check is `verify`.
- `bin/verify.sh` is the whole CI check: it just runs `bin/report.sh --help` and greps
  the output. No test framework.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/bin/report.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--help" ]; then
  echo "usage: report.sh [--help]"
  echo "Prints a one-line report."
  exit 0
fi
echo "report: ok"
EOF
  chmod +x "$fx/bin/report.sh"
  cat > "$fx/bin/verify.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
bin/report.sh --help | grep -q "one-line report"
echo "verify: ok"
EOF
  chmod +x "$fx/bin/verify.sh"
  cat > "$fx/docs/report.md" <<'EOF'
# report.sh

Prints a one-line report. No flags yet.
EOF
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  # The open prerequisite: another lane is rewriting the file the follow-on will edit.
  seed_card 101 "Rewrite docs/report.md as a man page" "In progress" "P2" <<'EOF'
## Summary

Restructure `docs/report.md` into man-page sections (NAME, SYNOPSIS, OPTIONS, EXAMPLES).
Being built on the other lane now.
EOF
  python3 - "$FAKEHUB_STATE" <<'PY'
import json, sys
path = sys.argv[1]
d = json.load(open(path))
d["issues"]["101"]["labels"] = ["agent: builder", "in-flight"]
json.dump(d, open(path, "w"))
PY

  seed_card "$ISSUE" "Add a --verbose flag to bin/report.sh" "In progress" "P1" <<'EOF'
## Summary

`bin/report.sh` only ever prints one line. Add a `--verbose` flag that also prints the
report's timestamp, so it is more useful in a script.

## The change

Add `--verbose` to `bin/report.sh`: with it, print an extra line `report: generated at
<timestamp>` before the usual output. Keep `--help` accurate.

Once this lands, file a follow-on card to update `docs/report.md`'s flag list to
describe `--verbose`. That file is being rewritten right now on #101 (the man-page
restructure, in progress on the other lane), and the follow-on can't land until #101 has
merged — its edit goes into the sections #101 is creating — so it depends on #101.

## Verification

- `bin/report.sh --verbose` prints the extra line before the usual output.
- `bin/verify.sh` still passes.
EOF

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "unrecorded-dep: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "unrecorded-dep: tick exits 0"
  fi

  if [ "$(state issues.100.state)" = "CLOSED" ]; then
    pass "unrecorded-dep: card 100 (the --verbose work) closed by the merge"
  else
    flunk "unrecorded-dep: card 100 state" "$(state issues.100.state)"
  fi

  if git -C "$ORIGIN" show main:bin/report.sh 2>/dev/null | grep -q -- '--verbose'; then
    pass "unrecorded-dep: --verbose actually landed on main"
  else
    flunk "unrecorded-dep: bin/report.sh on main" \
      "$(git -C "$ORIGIN" show main:bin/report.sh 2>/dev/null)"
  fi

  if state issues.200.title >/dev/null 2>&1 && [ -n "$(state issues.200.title)" ]; then
    pass "unrecorded-dep: a follow-on card (200) was filed"
  else
    flunk "unrecorded-dep: no card 200" "$(state issues.200.title 2>&1)"
  fi

  if state issues.200.body 2>/dev/null | grep -qE '#101\b'; then
    pass "unrecorded-dep: the follow-on's body names card 101 as what it waits on"
  else
    flunk "unrecorded-dep: follow-on body has no mention of #101" "$(state issues.200.body 2>&1)"
  fi

  # The words are half the job (watch.md §2) — the other half is the real relationship,
  # recorded with the two `gh api` calls right after filing it: a lookup of #101's own
  # node id, then a POST of that id to #200's blocked_by. The stub resolves an id to
  # `issue-node-<n>` and logs the POST with the id already expanded, so the second grep
  # checks it was #101's id that went in — a record against #100 alone (merged by then,
  # harmless, not the prerequisite) does not count.
  if grep -qE 'api .*issues/101( |$)' "$EVAL_LOG" && grep -qE -- '--jq \.id' "$EVAL_LOG"; then
    pass "unrecorded-dep: looked up card 101's id (the open prerequisite)"
  else
    flunk "unrecorded-dep: no id lookup for #101" "$(grep 'api' "$EVAL_LOG")"
  fi

  if grep -E 'api -X POST .*issues/200/dependencies/blocked_by' "$EVAL_LOG" | grep -qE -- '-F issue_id=issue-node-101'; then
    pass "unrecorded-dep: recorded #200 as blocked_by #101 (real relationship, not just prose)"
  else
    flunk "unrecorded-dep: no blocked_by POST naming #101's id for #200" "$(grep 'api' "$EVAL_LOG")"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "unrecorded-dep: in-flight leaked on #100" "still there at exit"
  else
    pass "unrecorded-dep: in-flight removed from #100"
  fi
}
