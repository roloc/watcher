#!/usr/bin/env bash
#
# review-submit.sh <pr> <sha> <approve|request-changes> <body-file|->   (card #255)
#
# Files the reviewer's verdict as a real GitHub review from the project's reviewer App, on
# exactly the commit named. It is the ONE thing the watcher user may run as the separate
# watcher-reviewer user (one sudoers line, installed by deploy/install.sh), and that user is
# the only one that can read the reviewer App's private key — so a tick can ask for a
# review of its PR's current head, but cannot sign as the reviewer, mint its own token, or
# approve anything it was not shown. The installed copy is root-owned
# (/usr/local/lib/watcher-review/review-submit); the repo copy is the source and what the
# offline suite runs.
#
# The caller is not trusted: every argument is validated, the repo comes from GH_REPO (kept
# through sudo for this one variable) and must be owner/name, the sha must be the PR's head
# right now (a stale one is refused, never reviewed), and the body is cut at 4000 bytes.
# Only the first line of a reviewer's answer is ever sent (hooks/receipts.ts), so the body
# is that line. `-` reads the body from stdin.
#
# Test seams, none of which survives sudo's reset environment: WATCHER_REVIEW_GH (the gh),
# WATCHER_REVIEW_MINT (the token script), WATCHER_REVIEW_CACHE (its token cache).
set -euo pipefail
PATH="/usr/local/bin:/usr/bin:/bin"

usage() { echo "usage: review-submit.sh <pr> <sha> <approve|request-changes> <body-file|->" >&2; exit 2; }
[ $# -eq 4 ] || usage
pr="$1"; sha="$2"; verdict="$3"; body_src="$4"
[[ "$pr" =~ ^[0-9]{1,9}$ ]] || usage
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || usage
case "$verdict" in approve) event=APPROVE ;; request-changes) event=REQUEST_CHANGES ;; *) usage ;; esac
repo="${GH_REPO:-}"
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "review-submit: GH_REPO is not owner/name" >&2; exit 2; }
project="${repo##*/}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mint="${WATCHER_REVIEW_MINT:-}"
if [ -z "$mint" ]; then
  if [ -x "$here/gh-app-token" ]; then mint="$here/gh-app-token"; else mint="$here/gh-app-token.sh"; fi
fi
gh_bin="${WATCHER_REVIEW_GH:-gh}"

if [ "$body_src" = - ]; then body="$(head -c 4000)"; else
  [ -f "$body_src" ] && [ -r "$body_src" ] || { echo "review-submit: cannot read $body_src" >&2; exit 2; }
  body="$(head -c 4000 "$body_src")"
fi
[ -n "$body" ] || { echo "review-submit: empty body" >&2; exit 2; }

export GH_APP_DIR="/etc/watcher/apps/$project"
[ -n "${WATCHER_REVIEW_APPS:-}" ] && export GH_APP_DIR="$WATCHER_REVIEW_APPS/$project"
export GH_APP_CACHE="${WATCHER_REVIEW_CACHE:-/var/lib/watcher-review/cache}"
token="$("$mint" "$project" reviewer)" || { echo "review-submit: no reviewer token for $project" >&2; exit 1; }
export GH_TOKEN="$token"
unset GH_REPO GITHUB_TOKEN

head="$("$gh_bin" api "repos/$repo/pulls/$pr" --jq .head.sha)" || { echo "review-submit: cannot read $repo#$pr" >&2; exit 1; }
if [ "$head" != "$sha" ]; then
  echo "review-submit: $sha is not the head of $repo#$pr ($head) — nothing filed" >&2
  exit 1
fi
"$gh_bin" api -X POST "repos/$repo/pulls/$pr/reviews" -f "commit_id=$sha" -f "event=$event" -f "body=$body" --jq .html_url
