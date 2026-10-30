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
# means a future check gated the same way starts clean here too.
#
# Then the watcher's OWN section, appended to the same report (card #206): a project's
# .claude/agents/reviewer.md or usability.md shadows the installed copy under
# ~/.claude/agents (install.sh's symlink onto $WATCHER_DIR/agents), so the day the watcher's
# agents/ changes, a project carrying its own copies keeps running the old review with
# nothing to say so — no PR in the project can see a change that happened in another repo.
# Compared with `cmp` against $WATCHER_DIR, the release the lanes actually run under, never
# against GitHub: no network, and the deployed copy is the one that matters. A project
# with no copy at all is not a finding (the installed one serves it). The project's own
# hygiene.sh is optional for this: HYGIENE=1 in projects/<p>.env with no script of its own
# (azeroth-rewind) runs the watcher's section alone.
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
# No script of its own and no HYGIENE=1 is still "nothing to run" (card #206 made the
# script optional, not the opt-in): deploy/install.sh never enables the timer for such a
# project, so this can only be a by-hand run, and it says what it looked for.
if [ -z "$SCRIPT" ] && [ "${HYGIENE:-0}" != 1 ]; then
  say "no hygiene.sh under $CHECKOUT (checked bin/ and scripts/) and no HYGIENE=1 in $PROJECT_ENV"
  exit 2
fi

report="$(mktemp)"; errlog="$(mktemp)"
status=0
if [ -n "$SCRIPT" ]; then
  # unset, not just "don't export": if BOARD_NUMBER/BOARD_TOKEN are already exported in
  # THIS process's own environment (a by-hand run under a shell that sourced a project env,
  # say), a later plain assignment from sourcing PROJECT_ENV above keeps them exported —
  # bash's export bit is sticky per-variable, not per-assignment.
  ( unset BOARD_NUMBER BOARD_TOKEN; cd "$CHECKOUT" && GITHUB_REPOSITORY="$PROJECT_REPO" "$SCRIPT" ) > "$report" 2>"$errlog"
  status=$?
  case "$status" in
    0|1) ;; # clean, or a finding — the watcher's own section comes next either way
    *) say "hygiene.sh could not run (exit $status): $(cat "$errlog")"; rm -f "$report" "$errlog"; exit 2 ;;
  esac
fi

# --- the watcher's own section: agent copies behind the watcher's (card #206) -----------
# Same shape as hygiene.sh's own sections — a title with the count, the one line saying
# why it is a weekly finding and not a CI gate, one bullet per file — so a reader of the
# card tells them apart by nothing but the heading.
watcher_sha="$(git -C "$WATCHER_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
agent_findings=""
for name in reviewer usability; do
  copy="$CHECKOUT/.claude/agents/$name.md"; ours="$WATCHER_DIR/agents/$name.md"
  [ -f "$copy" ] && [ -f "$ours" ] || continue
  cmp -s "$copy" "$ours" && continue
  agent_findings="$agent_findings
- \`.claude/agents/$name.md\` differs from the watcher's \`agents/$name.md\` (\`$WATCHER_DIR\` at $watcher_sha) — cp it over and open the PR"
done
if [ -n "$agent_findings" ]; then
  n="$(printf '%s\n' "$agent_findings" | grep -c '^- ')"
  if [ "$status" = 0 ]; then
    # The project's own sweep was clean (or there was none): its "Clean: nothing to do"
    # line must not head a card that has a finding in it, so the report starts over with
    # a header of the watcher's own.
    {
      echo "## Hygiene sweep — $(date -u +%Y-%m-%d), the watcher's own checks"
      echo
      echo "**$n** finding(s). Done means the section below is empty: each copy matches the watcher's \`agents/\`."
    } > "$report"
  fi
  {
    echo
    echo "### Agent copies behind the watcher's — $n"
    echo "_Not a gate: the watcher's \`agents/\` changes without any PR in this repo, so no check on a PR here can see it. Copy the file in from the watcher and open the PR._"
    printf '%s\n' "$agent_findings" | sed '/^$/d'
  } >> "$report"
  status=1
fi

if [ "$status" = 0 ]; then say "clean"; rm -f "$report" "$errlog"; exit 0; fi

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
# The project's own card template, when it ships one next to its hygiene.sh; a project
# running the watcher's section alone has no script and so no template.
cardmd=""; [ -n "$SCRIPT" ] && cardmd="$(dirname "$SCRIPT")/hygiene-card.md"
if [ -n "$cardmd" ] && [ -f "$cardmd" ]; then cat "$cardmd" "$report" > "$cardbody"; else cat "$report" > "$cardbody"; fi

# Exported before the filing so `board file` (bin/board.py) can reach the board; it creates the
# issue, adds it and moves it to Ready in one go, never relying on Auto-add (card #200). The
# sweep itself is done by now, so the board variables never reach it.
export BOARD_OWNER BOARD_NUMBER
out="$(board file -R "$PROJECT_REPO" --title "Weekly hygiene sweep — $today" --body-file "$cardbody" --status Ready 2>/dev/null)"; filed_rc=$?
url="$(printf '%s\n' "$out" | tail -1)"
iss="${url##*/}"
rm -f "$report" "$errlog" "$cardbody"
if ! [[ "$iss" =~ ^[0-9]+$ ]]; then
  say "could not file a card (gh failed); trying again next run"
  exit 2
fi

if [ "$filed_rc" = 0 ]; then
  say "filed $url straight into Ready"
else
  say "filed $url but could not put it on the board in Ready (it still exists — check it by hand)"
fi
