#!/usr/bin/env bash
# The weekly hygiene sweep: the debt CI's per-PR gate cannot see because it accumulates
# between PRs rather than inside one. Prints a Markdown report, exits 1 when it found
# anything, 0 when the repo is clean, 2 when it could not run — so "done" for the card it
# files is this script exiting 0 on main. Mechanical only, on purpose: a check is in here
# because a human can verify its finding in seconds; judgment ("is this script bad?") is
# not a check, it is a card somebody files with evidence.
#
# The same script riffmap and riffmap-site run (riffmap PR #711, riffmap-site PR #54),
# with this repo's paths: scripts live in bin/ and deploy/, the suites are bin/test-*,
# there is no next-session.md and no agent: labels (one lane takes every card).
#
# Each section names what it catches and why it is not a CI gate instead. A finding that
# comes back two sweeps running is a finding that should become one (README, "The weekly
# hygiene card").
#
#   bin/hygiene.sh              # the full sweep (needs `gh` and a fetched origin)
#   bin/hygiene.sh --no-remote  # skip the branch check: no gh, no network
#
# Plain bash 3 and BSD tools — the Mac runs it too (CLAUDE.md, "offline suites have to
# pass on a Mac"). Days are tunable through the env: STALE_BRANCH_DAYS (14),
# OLD_TODO_DAYS (30), NEXT_SESSION_DAYS (14).
set -uo pipefail

REPO="${GITHUB_REPOSITORY:-roloc/watcher}"
STALE_BRANCH_DAYS="${STALE_BRANCH_DAYS:-14}"
OLD_TODO_DAYS="${OLD_TODO_DAYS:-30}"
NEXT_SESSION_DAYS="${NEXT_SESSION_DAYS:-14}"
REMOTE=1
[ "${1:-}" = "--no-remote" ] && REMOTE=0

# A crash (an unbound variable under set -u, a failed cd) must not read as "1 = findings":
# the workflow would file a card with a cut-off report. Only the report's own exit, after
# done_ok=1, passes through; anything earlier is 2 = could not run.
done_ok=0
trap '[ "$done_ok" = 1 ] || { echo "hygiene: aborted before the report" >&2; exit 2; }' EXIT

cd "$(git rev-parse --show-toplevel)" || exit 2
now="$(date +%s)"
total=0
sections=""

# section <title> <why-not-a-gate> <findings...>  — appends one report section when there
# are findings; the "why" line is what tells the reader this is the right place for it.
section() {
  local title="$1" why="$2" body="$3" n
  [ -z "$body" ] && return 0
  n="$(printf '%s\n' "$body" | grep -c .)"
  total=$((total + n))
  sections="$sections
### $title — $n
_${why}_
$(printf '%s\n' "$body" | sed 's/^/- /')
"
}

# names_open_card <text> — true when the text links a card (#n or issues/n) that is still
# open. That is the "legitimately not debt" shape: a skip or a TODO with a card behind it.
# Without gh (--no-remote) any card reference counts.
names_open_card() {
  local n st
  n="$(printf '%s' "$1" | grep -oE '(#|issues/)[0-9]{1,5}' | head -1 | tr -dc '0-9')"
  [ -z "$n" ] && return 1
  [ "$REMOTE" = 0 ] && return 0
  st="$(gh issue view "$n" --repo "$REPO" --json state --jq .state 2>/dev/null || echo UNKNOWN)"
  [ "$st" != "CLOSED" ]   # open, or unreadable: kept either way
}

# ---------------------------------------------------------------- 1. stale branches
# Squash-merge deletes a merged branch, so anything left on origin is a parked or
# abandoned card's half-work. A branch named card-<n> whose issue is still open is kept:
# that is a parked card, not debt.
if [ "$REMOTE" = 1 ]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "hygiene: gh not on PATH — run with --no-remote, or install gh" >&2; done_ok=1; exit 2
  fi
  git fetch --quiet --prune origin 2>/dev/null || echo "hygiene: fetch failed — branch ages are from the last fetch" >&2
  open_prs="$(gh pr list --repo "$REPO" --state open --limit 200 --json headRefName --jq '.[].headRefName' 2>/dev/null || true)"
  stale=""
  for b in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin | sed 's#^origin/##' | grep -vE '^(main|HEAD)$'); do
    printf '%s\n' "$open_prs" | grep -qxF "$b" && continue
    ts="$(git log -1 --format=%ct "origin/$b" 2>/dev/null || echo "$now")"
    age=$(( (now - ts) / 86400 ))
    [ "$age" -lt "$STALE_BRANCH_DAYS" ] && continue
    n="$(printf '%s' "$b" | grep -oE '^card-[0-9]+' | tr -dc '0-9')"
    if [ -n "$n" ]; then
      st="$(gh issue view "$n" --repo "$REPO" --json state --jq .state 2>/dev/null || echo UNKNOWN)"
      [ "$st" = "CLOSED" ] || continue   # open, or unreadable: a card we cannot read is not a card we delete for
    fi
    ahead="$(git rev-list --count "origin/main..origin/$b" 2>/dev/null || echo '?')"
    stale="$stale
\`$b\` — last commit $age days ago, $ahead commit(s) not on main, no open PR"
  done
  section "Stale remote branches" \
    "Not a gate: a branch is not stale on the day it is pushed. Delete it with \`gh api -X DELETE repos/$REPO/git/refs/heads/<branch>\` (a deletion is not part of the PR, so list each one in a comment on the card). If \`git log origin/main..origin/<branch>\` is unshipped work for a card that is still open, check it out as \`card-<n>-<branch>\`, push that, delete the old name — the sweep keeps a \`card-<n>\` branch while its card is open." \
    "$(printf '%s' "$stale" | sed '/^$/d')"
fi

# ---------------------------------------------------------------- 2. skipped tests
# A skipped case is a case that is not running. Each one either names the card that will
# un-skip it, or goes.
skipped=""
while IFS= read -r hit; do
  [ -z "$hit" ] && continue
  names_open_card "$hit" && continue
  skipped="$skipped
\`$hit\`"
done < <(grep -nE '@unittest\.skip|skipTest\(|^\s*#\s*(case|t) .*skip' bin/test-*.py bin/test-*.sh 2>/dev/null)
section "Skipped tests" \
  "Not a gate: a skip with a card behind it is legitimate for a while. A skip whose line names an open card (\`#n\`) is kept; every other one links its card or the case is deleted." \
  "$(printf '%s' "$skipped" | sed '/^$/d')"

# ---------------------------------------------------------------- 3. old TODOs
# A TODO is a card nobody filed. After OLD_TODO_DAYS it is either a card or a lie. (This
# file is excluded: its comments say the word, and a sweep that reports itself can never
# reach exit 0.)
old=""
while IFS=: read -r file line _; do
  [ -z "$file" ] && continue
  ts="$(git blame -L "$line,$line" --porcelain -- "$file" 2>/dev/null | awk '/^committer-time/ {print $2}')"
  [ -z "$ts" ] && continue
  age=$(( (now - ts) / 86400 ))
  [ "$age" -lt "$OLD_TODO_DAYS" ] && continue
  names_open_card "$(sed -n "${line}p" "$file")" && continue
  old="$old
\`$file:$line\` — $age days old: $(sed -n "${line}p" "$file" | sed 's/^[[:space:]]*//' | cut -c1-120)"
done < <(grep -rnE '\b(TODO|FIXME|XXX|HACK)\b' bin deploy evals 2>/dev/null \
          --include='*.sh' --include='*.py' --include='*.service' --include='*.timer' \
          --exclude=hygiene.sh || true)
section "TODOs older than $OLD_TODO_DAYS days" \
  "Not a gate: a fresh TODO inside a PR is fine. An old one that names an open card (\`#n\`) on its line is kept; every other one becomes a card (link it on the line) or is deleted." \
  "$(printf '%s' "$old" | sed '/^$/d')"

# ---------------------------------------------------------------- 4. orphan scripts
# A script nothing names — not CI, not a unit file, not the README's layout, not another
# script — is a script nobody runs. The README lists every file in bin/ on purpose, so a
# hit here is also a README line that never got written.
orphans=""
for f in bin/* bin/shim/* deploy/*.sh evals/*.py; do
  [ -f "$f" ] || continue
  name="$(basename "$f")"
  if ! git grep -q -F "$name" -- . ":!$f" 2>/dev/null; then
    orphans="$orphans
\`$f\`"
  fi
done
section "Scripts nothing references" \
  "Not a gate: the PR that adds a script usually adds its caller a PR later. Either name the caller (a CI step, a unit, the README's Layout, a script) in the same change, or delete the script." \
  "$(printf '%s' "$orphans" | sed '/^$/d')"

# ---------------------------------------------------------------- 5. broken doc links
# A relative Markdown link to a file that is gone. (External links are not checked: that
# is a network question, and a dead vendor page is not our debt.)
broken=""
while IFS= read -r md; do
  dir="$(dirname "$md")"
  while IFS= read -r target; do
    [ -z "$target" ] && continue
    case "$target" in http://*|https://*|mailto:*|\#*|\<*) continue ;; esac
    printf '%s' "$target" | grep -qE '[A-Za-z0-9]' || continue   # `[#m](…)` in an example is a shape, not a link
    case "$target" in */*|*.*) ;; *) continue ;; esac               # `[#card](url)` is a placeholder, not a path
    case "$target" in …*|...*) continue ;; esac                      # `[#12](…/issues/12)` is an elided URL in prose
    path="${target%%#*}"; path="${path%% *}"
    [ -z "$path" ] && continue
    [ -e "$dir/$path" ] || [ -e "$path" ] || broken="$broken
\`$md\` → \`$target\`"
  done < <(grep -oE '\]\([^)]+\)' "$md" | sed 's/^](//; s/)$//' | sort -u)
done < <(git ls-files -- '*.md' ':!templates/*')
section "Broken relative links in docs" \
  "Not a gate yet: it should be, once this is zero — then a moved file breaks the PR that moved it. Fix the link or delete the sentence." \
  "$(printf '%s' "$broken" | sed '/^$/d')"

# ---------------------------------------------------------------- 6. paths that do not exist
# Every tick on this board loads CLAUDE.md, and every tick everywhere loads the README's
# siblings in docs/; a backticked path in them that no longer exists sends the tick looking
# for it. Only plain repo-rooted paths are checked — a pattern with <, * or $ is a shape,
# not a path, and a path that is the text of a link to another repo is pointing elsewhere
# on purpose. commands/ and templates/ are generic across projects and name other repos'
# files, so they are not scanned.
missing=""
for md in CLAUDE.md README.md docs/*.md; do
  [ -f "$md" ] || continue
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    [ -e "$p" ] || missing="$missing
\`$md\` names \`$p\`"
  done < <(sed -E 's/\[`[^`]*`\]\(https?:\/\/[^)]*\)//g' "$md" \
           | grep -oE '`(bin|deploy|commands|agents|docs|evals|projects|templates|\.github)/[A-Za-z0-9_./@-]+`' | tr -d '`' | sed 's#/$##' | sort -u)
done
section "Paths named in the tick-loaded docs that do not exist" \
  "Not a gate: these files change every week and a rename lands in a PR that did not touch the doc. Point the sentence at the new path, or delete it." \
  "$(printf '%s' "$missing" | sed '/^$/d')"

# ---------------------------------------------------------------- 7. next-session.md age
# The cold-start file, where a project keeps one. This repo does not today (the board and
# CLAUDE.md are its cold start); the check is here so the day one appears it is watched.
if [ -f docs/next-session.md ]; then
  ts="$(git log -1 --format=%ct -- docs/next-session.md 2>/dev/null)"
  [ -z "$ts" ] && ts="$now"
  age=$(( (now - ts) / 86400 ))
  if [ "$age" -ge "$NEXT_SESSION_DAYS" ]; then
    section "docs/next-session.md is stale" \
      "Not a gate: a quiet fortnight is possible. Rewrite it from the board as it is now." \
      "last rewritten $age days ago ($(date -r "$ts" +%Y-%m-%d 2>/dev/null || date -d "@$ts" +%Y-%m-%d))"
  fi
fi

# (riffmap's "agent files vs agent labels" section has no counterpart here: one lane takes
# every card, there are no agent: labels, and agents/ holds review passes, not lanes.)

# ---------------------------------------------------------------- report
done_ok=1
echo "## Hygiene sweep — $(date +%Y-%m-%d), \`$(git rev-parse --short HEAD)\`"
if [ "$total" -eq 0 ]; then
  echo
  echo "Clean: nothing to do this week."
  exit 0
fi
echo
echo "**$total** finding(s). Done means every section below is empty: \`bin/hygiene.sh\` exits 0 on \`main\`."
printf '%s\n' "$sections"
exit 1
