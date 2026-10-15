#!/usr/bin/env bash
#
# receipt-check.sh <pr-number>: the CI-side half of the review receipt (card #142).
# `commands/watch.md` §3 has the tick post `🤖 reviewer: <first line>` (and `🤖 usability:`
# for a screen change) as a PR comment once each pass runs. This script reads those
# comments back and exits 0 only when the newest one names the PR's *current* head
# commit with a clean verdict — so a PR that was never reviewed, or was amended after its
# review, fails instead of merging silently. Read-only: it never posts anything itself.
#
#   bin/receipt-check.sh <pr-number>
#
# RECEIPT_GATE_PATHS: paths that always need a `reviewer` receipt, even when every changed
# file is a `.md` — the merge gate itself (`agents/reviewer.md`'s own "docs-only ... except
# the merge gate" rule). Default is this repo's own gate; a project overrides it from its
# own CI workflow, the same way `deploy/autodeploy.sh`'s EVAL_PATHS is overridden.
# RECEIPT_SCREEN_PATHS: paths that make a change a screen change, needing a `🤖 usability:`
# receipt too. Empty by default — most projects have no screen at all.
set -euo pipefail

pr="${1:?usage: receipt-check.sh <pr-number>}"
gate_paths="${RECEIPT_GATE_PATHS:-.github/workflows deploy agents/reviewer.md agents/usability.md commands/watch.md docs/how-it-works.md evals bin/eval.sh}"
screen_paths="${RECEIPT_SCREEN_PATHS:-}"

# path_in FILE PATHS...: true if FILE is one of PATHS, or sits under one of them.
path_in() {
  local f="$1" p
  shift
  for p in "$@"; do
    case "$f" in
      "$p") return 0 ;;
      "$p"/*) return 0 ;;
    esac
  done
  return 1
}

sha="$(gh pr view "$pr" --json headRefOid --jq .headRefOid)"
# `gh pr view --json files` truncates past 100 files; `--paginate` on the REST endpoint
# does not, so a large PR's later files still count toward needs_review.
files="$(gh api --paginate "repos/{owner}/{repo}/pulls/$pr/files" --jq '.[].filename')"

needs_review=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    *.md) path_in "$f" $gate_paths && needs_review=1 ;;
    *) needs_review=1 ;;
  esac
done <<< "$files"

if [ "$needs_review" -eq 0 ]; then
  echo "receipt-check: docs-only PR (touches no part of the merge gate) — no receipt required"
  exit 0
fi

comments="$(gh api --paginate "repos/{owner}/{repo}/issues/$pr/comments" --jq '.[].body')"

# newest_receipt PREFIX: the last comment line starting with PREFIX ("🤖 reviewer:" or
# "🤖 usability:") — comments come back oldest first, so the last match is the newest.
newest_receipt() {
  printf '%s\n' "$comments" | grep "^$1" | tail -1 || true
}

reviewer_line="$(newest_receipt '🤖 reviewer:')"
if ! printf '%s' "$reviewer_line" | grep -qE "Reviewed $sha: no blocking findings"; then
  found="$(printf '%s' "$reviewer_line" | grep -oE 'Reviewed [0-9a-f]+' || true)"
  found="${found#Reviewed }"
  echo "receipt-check: no clean reviewer receipt for $sha (found: ${found:-none})"
  exit 1
fi

needs_usability=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  path_in "$f" $screen_paths && needs_usability=1
done <<< "$files"

if [ "$needs_usability" -eq 1 ]; then
  usability_line="$(newest_receipt '🤖 usability:')"
  if ! printf '%s' "$usability_line" | grep -qE "Usability $sha: no blocking findings"; then
    found="$(printf '%s' "$usability_line" | grep -oE 'Usability [0-9a-f]+' || true)"
    found="${found#Usability }"
    echo "receipt-check: no clean usability receipt for $sha (found: ${found:-none})"
    exit 1
  fi
fi

echo "receipt-check: receipt(s) hold for $sha"
