#!/usr/bin/env bash
# One project's weekly hygiene sweep, run off the box's own clock instead of GitHub
# Actions (card #196): watcher-hygiene@<project>.timer, Monday morning, one instance per
# opted-in project (deploy/install.sh). The bot's own `gh` login already holds `project`
# scope on every board it manages, so this files the card AND moves it straight into
# Ready itself — no BOARD_TOKEN secret, and no Backlog-pull exception in bin/spawn.sh
# ("machine-filed cards", removed by this same change).
#
#   bin/hygiene-run.sh <project>
#
# Reads projects/<project>.env for PROJECT_ROOT, PROJECT_REPO, BOARD_OWNER, BOARD_NUMBER.
# Never touches PROJECT_ROOT's working tree — a lane may be mid-build in it — only its
# refs (a `fetch origin main`, the same call bin/worktree.sh's `new` already makes before
# branching), then materializes that commit into a throwaway checkout under the project's
# own worktree base, cloned locally FROM PROJECT_ROOT (fast, no network, the same trick
# deploy/install.sh's canary_clone uses for the canary checkout).
#
# Runs that checkout's own hygiene.sh (bin/ or scripts/, whichever the project ships) with
# no BOARD_NUMBER/BOARD_TOKEN in its environment, on principle: those two only ever meant
# anything to a board-token check hygiene.sh carried briefly and has since dropped (added
# by #192, reverted by #195) — this box needs no such secret, the bot's own `gh` login
# already reaches the board — but isolating the subprocess's environment costs nothing and
# means a future check gated the same way starts clean here too. A project with HYGIENE=1
# in its own env and no hygiene.sh of its own (azeroth-rewind) skips this step rather than
# failing: the watcher's own section, below, still runs.
#
# After the project's own sweep, this script runs its own section (card #206): for each of
# .claude/agents/reviewer.md and .claude/agents/usability.md that exists in the project's
# checkout and differs (cmp -s) from this deploy's own agents/<name>.md, one finding line —
# a project's copy shadows the installed symlink (deploy/install.sh), so a copy that falls
# behind runs stale with nothing saying so. Compared against $WATCHER_DIR, the release the
# lanes actually run under, never GitHub: no network, and it is the copy that matters. A
# project with no copy at all is not a finding — the installed one serves it.
#
# On a finding (exit 1, from either the project's own sweep or the section above): dedupe
# against an already-open card by title and comment the fresh list there, or file a new one
# straight into Ready. On exit 0: nothing. On exit 2 ("could not run") or any setup failure:
# one clear line to stderr/the journal and exit 2 — no card, nothing to Discord (CLAUDE.md:
# a post happens for stuck, broken, or the Monday digest; a sweep that could not run is a
# `systemctl status`/`journalctl` fact, not a new kind of alert).
set -uo pipefail

WATCHER_DIR="${WATCHER_DIR:-/opt/watcher}"
PROJECT="${1:?usage: hygiene-run.sh <project>}"

say() { printf '%s hygiene(%s): %s\n' "$(date -u +%FT%TZ)" "$PROJECT" "$*"; }

PROJECT_ENV="$WATCHER_DIR/projects/$PROJECT.env"
[ -f "$PROJECT_ENV" ] || { say "no $PROJECT_ENV; nothing configured"; exit 2; }
# shellcheck disable=SC1090
. "$PROJECT_ENV"
: "${PROJECT_ROOT:?$PROJECT_ENV sets no PROJECT_ROOT}"
: "${PROJECT_REPO:?$PROJECT_ENV sets no PROJECT_REPO}"
: "${BOARD_OWNER:?$PROJECT_ENV sets no BOARD_OWNER}"
: "${BOARD_NUMBER:?$PROJECT_ENV sets no BOARD_NUMBER}"
[ -d "$PROJECT_ROOT/.git" ] || { say "$PROJECT_ROOT is not a checkout"; exit 2; }

git -C "$PROJECT_ROOT" fetch -q origin main 2>/dev/null \
  || { say "could not fetch origin/main for $PROJECT_ROOT"; exit 2; }
ref="$(git -C "$PROJECT_ROOT" rev-parse origin/main 2>/dev/null)" \
  || { say "no origin/main in $PROJECT_ROOT"; exit 2; }
real_origin="$(git -C "$PROJECT_ROOT" remote get-url origin 2>/dev/null)" \
  || { say "could not read $PROJECT_ROOT's own origin url"; exit 2; }

CHECKOUT="$(dirname "$PROJECT_ROOT")/$(basename "$PROJECT_ROOT")-worktrees/hygiene-sweep"
mkdir -p "$(dirname "$CHECKOUT")"
if [ ! -d "$CHECKOUT/.git" ]; then
  rm -rf "$CHECKOUT"
  # Cloned FROM PROJECT_ROOT — fast, local, no login needed for the clone itself (the same
  # trick deploy/install.sh's canary_clone uses). But hygiene.sh's own stale-branch check
  # runs `git fetch --prune origin` and reads `refs/remotes/origin/*` INSIDE this checkout,
  # so `origin` has to be the real GitHub remote by the time that runs, never PROJECT_ROOT's
  # own path — otherwise it reports PROJECT_ROOT's local, possibly stale, branch list
  # instead of what is actually on GitHub.
  git clone -q "$PROJECT_ROOT" "$CHECKOUT" \
    || { say "could not create the throwaway checkout at $CHECKOUT"; exit 2; }
fi
git -C "$CHECKOUT" remote set-url origin "$real_origin" \
  || { say "could not point $CHECKOUT's origin at $real_origin"; exit 2; }
if ! git -C "$CHECKOUT" fetch -q origin "$ref" 2>/dev/null \
   || ! git -C "$CHECKOUT" checkout -q --detach "$ref" 2>/dev/null; then
  say "could not check out $ref in $CHECKOUT"
  exit 2
fi
git -C "$CHECKOUT" clean -q -fdx

SCRIPT=""
for candidate in bin/hygiene.sh scripts/hygiene.sh; do
  if [ -x "$CHECKOUT/$candidate" ]; then SCRIPT="$CHECKOUT/$candidate"; break; fi
done

report="$(mktemp)"; errlog="$(mktemp)"
: > "$report"
if [ -n "$SCRIPT" ]; then
  # unset, not just "don't export": if BOARD_NUMBER/BOARD_TOKEN are already exported in
  # THIS process's own environment (a by-hand run under a shell that sourced a project env,
  # say), a later plain assignment from sourcing PROJECT_ENV above keeps them exported —
  # bash's export bit is sticky per-variable, not per-assignment.
  ( unset BOARD_NUMBER BOARD_TOKEN; cd "$CHECKOUT" && GITHUB_REPOSITORY="$PROJECT_REPO" "$SCRIPT" ) > "$report" 2>"$errlog"
  status=$?
elif [ "${HYGIENE:-0}" = 1 ]; then
  status=0  # opted in with no hygiene.sh of its own — the watcher's own section still runs
else
  say "no hygiene.sh under $CHECKOUT (checked bin/ and scripts/)"
  rm -f "$report" "$errlog"
  exit 2
fi

case "$status" in
  0) ;; # clean so far — the watcher's own section below may still turn this into a finding
  1) ;; # a finding already
  *) say "hygiene.sh could not run (exit $status): $(cat "$errlog")"; rm -f "$report" "$errlog"; exit 2 ;;
esac

# The watcher's own section (card #206): a project's .claude/agents copy shadows the
# installed symlink, so it is checked against this deploy's own agents/, not GitHub.
agent_findings=""
watcher_sha="$(git -C "$WATCHER_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
for name in reviewer usability; do
  proj_copy="$CHECKOUT/.claude/agents/$name.md"
  watcher_copy="$WATCHER_DIR/agents/$name.md"
  if [ -f "$proj_copy" ] && [ -f "$watcher_copy" ] && ! cmp -s "$proj_copy" "$watcher_copy"; then
    agent_findings="$agent_findings
\`.claude/agents/$name.md\` differs from the watcher's agents/$name.md (/opt/watcher at $watcher_sha) — cp it over and open the PR"
  fi
done
agent_findings="$(printf '%s' "$agent_findings" | sed '/^$/d')"
if [ -n "$agent_findings" ]; then
  {
    echo
    echo "### Watcher agent copies out of step"
    echo "_Not a gate: the watcher's agents/ can change with no PR in this project, so no PR check here can see it._"
    printf '%s\n' "$agent_findings" | sed 's/^/- /'
  } >> "$report"
  status=1
fi

if [ "$status" = 0 ]; then
  say "clean"; rm -f "$report" "$errlog"; exit 0
fi

today="$(date -u +%Y-%m-%d)"
existing="$(gh issue list -R "$PROJECT_REPO" --state open \
              --search '"Weekly hygiene sweep" in:title' --json number \
              --jq '.[0].number // empty' 2>/dev/null)"
if [ -n "$existing" ]; then
  commentfile="$(mktemp)"
  { echo "Re-swept $today — the list as it stands now (the card body is last week's):"; echo; cat "$report"; } > "$commentfile"
  if gh issue comment "$existing" -R "$PROJECT_REPO" --body-file "$commentfile"; then
    say "refreshed the open hygiene card #$existing"
  else
    say "found open card #$existing but could not comment on it"
  fi
  rm -f "$report" "$errlog" "$commentfile"
  exit 0
fi

cardbody="$(mktemp)"
cardmd=""
[ -n "$SCRIPT" ] && [ -f "$(dirname "$SCRIPT")/hygiene-card.md" ] && cardmd="$(dirname "$SCRIPT")/hygiene-card.md"
if [ -n "$cardmd" ]; then cat "$cardmd" "$report" > "$cardbody"; else cat "$report" > "$cardbody"; fi

url="$(gh issue create -R "$PROJECT_REPO" --title "Weekly hygiene sweep — $today" --body-file "$cardbody" 2>/dev/null)"
iss="${url##*/}"
rm -f "$report" "$errlog" "$cardbody"
if ! [[ "$iss" =~ ^[0-9]+$ ]]; then
  say "could not file a card (gh failed); trying again next run"
  exit 2
fi

# Exported only now that hygiene.sh is done running, so board/gh use them for the move
# below without ever handing them to the sweep itself.
export BOARD_OWNER BOARD_NUMBER
if gh project item-add "$BOARD_NUMBER" --owner "$BOARD_OWNER" --url "$url" >/dev/null 2>&1 \
   && board move "$iss" Ready >/dev/null 2>&1; then
  say "filed $url straight into Ready"
else
  say "filed $url but could not put it on the board in Ready (it still exists — check it by hand)"
fi
