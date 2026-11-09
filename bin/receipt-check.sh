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
# RECEIPT_REVIEWER_APPS: the logins (shell globs, space-separated) of the project's reviewer
# GitHub App (card #255). A real GitHub review from one of them — Approve — on the PR's exact
# head SHA is a receipt too, and its Request changes on that SHA fails the check even when a
# `🤖 reviewer:` comment says clean. Names without the `[bot]` suffix, which the login must carry (a person's login never does).
# Default `roloc-*-reviewer`; a fork sets its own prefix.
# RECEIPT_SCREEN_PATHS: paths that make a change a screen change, needing a `🤖 usability:`
# receipt too. Empty by default — most projects have no screen at all.
set -euo pipefail

pr="${1:?usage: receipt-check.sh <pr-number>}"
gate_paths="${RECEIPT_GATE_PATHS:-.github/workflows deploy agents/reviewer.md agents/usability.md commands/watch.md docs/how-it-works.md evals bin/eval.sh}"
screen_paths="${RECEIPT_SCREEN_PATHS:-}"
reviewer_apps="${RECEIPT_REVIEWER_APPS:-roloc-*-reviewer}"

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

# app_review_state: the newest review by a reviewer App on the head SHA, as APPROVED,
# CHANGES_REQUESTED or empty (none, or only comments). Reviews come back oldest first. A review
# on an older commit says nothing about this one: an amend after the review fails here too.
app_review_state() {
  local login commit state pat state_out=""
  while IFS=$'\t' read -r login commit state; do
    [ -z "$login" ] && continue
    [ "$commit" = "$sha" ] || continue
    case "$login" in *"[bot]") login="${login%"[bot]"}" ;; *) continue ;; esac
    for pat in $reviewer_apps; do
      # shellcheck disable=SC2254
      case "$login" in
        $pat) case "$state" in APPROVED|CHANGES_REQUESTED) state_out="$state" ;; esac ;;
      esac
    done
  done <<< "$reviews"
  printf '%s' "$state_out"
}
reviews="$(gh api --paginate "repos/{owner}/{repo}/pulls/$pr/reviews" --jq '.[] | [.user.login, .commit_id, .state] | @tsv' 2>/dev/null || true)"
app_state="$(app_review_state)"

if [ "$app_state" = CHANGES_REQUESTED ]; then
  echo "receipt-check: the reviewer App requested changes on $sha"
  exit 1
fi
if [ "$app_state" != APPROVED ] && ! printf '%s' "$reviewer_line" | grep -qE "Reviewed $sha: no blocking findings"; then
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

echo "receipt-check: receipt(s) hold for $sha${app_state:+ (reviewer App review: $app_state)}"
