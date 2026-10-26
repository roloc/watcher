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
# no BOARD_NUMBER/BOARD_TOKEN in its environment: those only ever mattered for the
# GitHub-hosted job's own board-token check (hygiene.sh's "board token" section, gated on
# BOARD_NUMBER being set), which this path has no use for — the bot's own `gh` login
# already reaches the board, so the script itself is never touched by this change.
#
# On a finding (exit 1): dedupe against an already-open card by title and comment the
# fresh list there, or file a new one straight into Ready. On exit 0: nothing. On exit 2
# ("could not run") or any setup failure: one clear line to stderr/the journal and exit 2
# — no card, nothing to Discord (CLAUDE.md: a post happens for stuck, broken, or the
# Monday digest; a sweep that could not run is a `systemctl status`/`journalctl` fact, not
# a new kind of alert).
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

CHECKOUT="$(dirname "$PROJECT_ROOT")/$(basename "$PROJECT_ROOT")-worktrees/hygiene-sweep"
mkdir -p "$(dirname "$CHECKOUT")"
if [ ! -d "$CHECKOUT/.git" ]; then
  rm -rf "$CHECKOUT"
  git clone -q "$PROJECT_ROOT" "$CHECKOUT" \
    || { say "could not create the throwaway checkout at $CHECKOUT"; exit 2; }
fi
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
[ -n "$SCRIPT" ] || { say "no hygiene.sh under $CHECKOUT (checked bin/ and scripts/)"; exit 2; }

report="$(mktemp)"; errlog="$(mktemp)"
# unset, not just "don't export": if BOARD_NUMBER/BOARD_TOKEN are already exported in
# THIS process's own environment (a by-hand run under a shell that sourced a project env,
# say), a later plain assignment from sourcing PROJECT_ENV above keeps them exported —
# bash's export bit is sticky per-variable, not per-assignment. hygiene.sh's own
# board-token check is gated on BOARD_NUMBER being set, and this box never needs it.
( unset BOARD_NUMBER BOARD_TOKEN; cd "$CHECKOUT" && GITHUB_REPOSITORY="$PROJECT_REPO" "$SCRIPT" ) > "$report" 2>"$errlog"
status=$?

case "$status" in
  0) say "clean"; rm -f "$report" "$errlog"; exit 0 ;;
  1) ;; # a finding — handled below
  *) say "hygiene.sh could not run (exit $status): $(cat "$errlog")"; rm -f "$report" "$errlog"; exit 2 ;;
esac

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
cardmd="$(dirname "$SCRIPT")/hygiene-card.md"
if [ -f "$cardmd" ]; then cat "$cardmd" "$report" > "$cardbody"; else cat "$report" > "$cardbody"; fi

url="$(gh issue create -R "$PROJECT_REPO" --title "Weekly hygiene sweep — $today" --body-file "$cardbody" 2>/dev/null)"
iss="${url##*/}"
rm -f "$report" "$errlog" "$cardbody"
if ! [[ "$iss" =~ ^[0-9]+$ ]]; then
  say "could not file a card (gh failed); trying again next run"
  exit 2
fi

# Exported only now: hygiene.sh above never saw BOARD_NUMBER, so its own board-token check
# (which only ever mattered for the GitHub-hosted job's separate BOARD_TOKEN secret) stayed
# off — this box files and moves the card with its own `gh` login instead.
export BOARD_OWNER BOARD_NUMBER
if gh project item-add "$BOARD_NUMBER" --owner "$BOARD_OWNER" --url "$url" >/dev/null 2>&1 \
   && board move "$iss" Ready >/dev/null 2>&1; then
  say "filed $url straight into Ready"
else
  say "filed $url but could not put it on the board in Ready (it still exists — check it by hand)"
fi
