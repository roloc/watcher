#!/usr/bin/env bash
#
# doctor.sh — read-only check that a project is wired up for the watcher, before any tick
# ever runs against it. Run by deploy/install.sh right after cloning a project, and by
# bin/spawn.sh's tick_once, once per process, before a lane's first tick (see run_doctor
# there). Prints "doctor: ok" and exits 0 when everything checks out; otherwise prints one
# "doctor: ..." line per failing check to stderr and exits 1 — install.sh holds that
# project out of its "Next, by hand" list, and spawn.sh logs it and never launches a tick
# this poll (retried automatically; see run_doctor's own comment for the cadence).
#
# Reads what spawn.sh already resolved from the project file, so it never re-derives it:
#   BOARD_OWNER, BOARD_NUMBER   exported by spawn.sh already (board.py reads the same two)
#   PROJECT_REPO                owner/repo, passed in — not exported by spawn.sh itself
#   PROJECT_ROOT                the project's checkout, for its CLAUDE.md
# WATCHER_GH is overridable the same way spawn.sh's is, so this is stubbable offline.
#
# Runs AS THE BOT: write access to the repo, and whatever the board's owner granted it on
# the board (deploy/add-project.sh's ask-first list). Reading a branch's full protection
# detail needs admin, which the bot does not have — GitHub answers that request with a 404
# whether or not the branch is protected at all, so this check is best-effort: a 404 there
# is reported as a WARNING ("cannot verify"), never a failure. add-project.sh is what
# actually sets the protection, run by whoever owns the repo.
#
#   bin/doctor.sh          # reads BOARD_OWNER, BOARD_NUMBER, PROJECT_REPO, PROJECT_ROOT
set -uo pipefail

WATCHER_GH="${WATCHER_GH:-gh}"
ERR="$(mktemp)"; trap 'rm -f "$ERR"' EXIT

fail=0
problem() { printf 'doctor: %s\n' "$*" >&2; fail=1; }
warn()    { printf 'doctor: warning: %s\n' "$*" >&2; }

[ -n "${BOARD_OWNER:-}" ]   || problem "BOARD_OWNER is not set"
[ -n "${BOARD_NUMBER:-}" ]  || problem "BOARD_NUMBER is not set"
[ -n "${PROJECT_REPO:-}" ]  || problem "PROJECT_REPO is not set"
[ -n "${PROJECT_ROOT:-}" ]  || problem "PROJECT_ROOT is not set"
if [ "$fail" = 1 ]; then
  echo "doctor: cannot run the rest of the checks without those" >&2
  exit 1
fi

# --- the board: reads, and carries Status/Stage/Priority --------------------------------
if FIELDS_JSON="$($WATCHER_GH project field-list "$BOARD_NUMBER" --owner "$BOARD_OWNER" --format json 2>"$ERR")"; then
  if ! python3 - "$FIELDS_JSON" <<'PY' >&2
import json, sys
required_statuses = ["Backlog", "Ready", "In progress", "In review", "Done", "Stuck - Needs help"]
fields = {f["name"]: f for f in json.loads(sys.argv[1]).get("fields", [])}
problems = []
status = fields.get("Status")
if not status or status.get("type") != "ProjectV2SingleSelectField":
    problems.append("the board has no Status field")
else:
    have = {o["name"] for o in status.get("options", [])}
    missing = [s for s in required_statuses if s not in have]
    if missing:
        problems.append("the board's Status field is missing: " + ", ".join(missing))
for name in ("Stage", "Priority"):
    f = fields.get(name)
    if not f or f.get("type") != "ProjectV2SingleSelectField":
        problems.append("the board has no %s field" % name)
for p in problems:
    print("doctor: " + p)
sys.exit(1 if problems else 0)
PY
  then fail=1; fi
else
  problem "board $BOARD_OWNER/#$BOARD_NUMBER does not read: $(head -1 "$ERR")"
fi

# --- Auto-add workflow: on, so a card Steve files lands on the board by itself ----------
AUTOADD_JSON=""
for owner_type in user organization; do
  q="query(\$login:String!,\$number:Int!){ $owner_type(login:\$login){ projectV2(number:\$number){ workflows(first:20){ nodes{ name enabled } } } } }"
  AUTOADD_JSON="$($WATCHER_GH api graphql -f query="$q" -f login="$BOARD_OWNER" -F number="$BOARD_NUMBER" 2>/dev/null)" \
    && [ -n "$AUTOADD_JSON" ] && break
done
if [ -z "$AUTOADD_JSON" ]; then
  problem "could not read the board's workflows (tried as a user and as an organization)"
else
  if ! python3 - "$AUTOADD_JSON" <<'PY' >&2
import json, sys
d = json.loads(sys.argv[1])
owner = d.get("data", {}).get("user") or d.get("data", {}).get("organization")
if not owner:
    print("doctor: could not parse the board's workflows")
    sys.exit(1)
nodes = owner["projectV2"]["workflows"]["nodes"]
wf = next((w for w in nodes if w["name"] == "Auto-add to project"), None)
if not wf:
    print("doctor: the board has no \"Auto-add to project\" workflow")
    sys.exit(1)
if not wf["enabled"]:
    print("doctor: the board's \"Auto-add to project\" workflow is off")
    sys.exit(1)
PY
  then fail=1; fi
fi
# The filter itself ("is:issue is:open", not GitHub's own "label:bug" example) is not
# exposed by any API this script found — "enabled" is all the GraphQL workflows field
# carries (card #39's "Details"). A project whose filter is still the example silently
# adds nothing; nothing here can catch that, so it stays a thing to eyeball once by hand
# (board ⋯ → Workflows → Auto-add to project) when a newly added project files no cards.

# --- labels on the repo ------------------------------------------------------------------
if LABELS_JSON="$($WATCHER_GH label list --repo "$PROJECT_REPO" --json name --limit 200 2>"$ERR")"; then
  if ! python3 - "$LABELS_JSON" "$PROJECT_REPO" <<'PY' >&2
import json, sys
required = ["in-flight", "blocked", "steve-decision"]
have = {l["name"] for l in json.loads(sys.argv[1])}
missing = [l for l in required if l not in have]
if missing:
    print("doctor: %s is missing labels: %s" % (sys.argv[2], ", ".join(missing)))
    sys.exit(1)
PY
  then fail=1; fi
else
  problem "could not read labels on $PROJECT_REPO: $(head -1 "$ERR")"
fi

# --- the project's CLAUDE.md names a required check, and main requires it --------------
CLAUDE_MD="$PROJECT_ROOT/CLAUDE.md"
CHECK_NAME=""
if [ ! -f "$CLAUDE_MD" ]; then
  problem "$PROJECT_ROOT has no CLAUDE.md"
else
  CHECK_NAME="$(grep -ioE 'required CI check is `[^`]+`' "$CLAUDE_MD" | head -1 | sed -E 's/.*`([^`]+)`.*/\1/')"
  # Best-effort, not a failure: this is one specific phrasing ("required CI check is
  # `<name>`", this repo's own CLAUDE.md), and a project that names its check some other
  # way (riffmap's CLAUDE.md: "for CI `verify`") would otherwise have every one of its
  # lanes refused, forever, over a wording mismatch doctor cannot tell apart from a genuine
  # gap. Found exactly this way, reviewing this file (PR #64): riffmap's CLAUDE.md does not
  # use the exact phrase, and every riffmap lane would have stalled the next time
  # spawn.sh restarted.
  [ -n "$CHECK_NAME" ] || warn "$PROJECT_ROOT/CLAUDE.md does not use the exact words \"required CI check is \`<name>\`\" — could not confirm main requires one (templates/project-CLAUDE.md uses that phrase; consider matching it)"
fi
if [ -n "$CHECK_NAME" ]; then
  if PROTECTION_JSON="$($WATCHER_GH api "repos/$PROJECT_REPO/branches/main/protection" 2>"$ERR")"; then
    if ! python3 - "$CHECK_NAME" "$PROTECTION_JSON" <<'PY' >&2
import json, sys
check, raw = sys.argv[1], sys.argv[2]
d = json.loads(raw)
rsc = d.get("required_status_checks") or {}
names = set(rsc.get("contexts") or [])
names |= {c.get("context") for c in (rsc.get("checks") or []) if c.get("context")}
if check not in names:
    print("doctor: main does not require the \"%s\" check (has: %s)" % (check, ", ".join(sorted(names)) or "none"))
    sys.exit(1)
PY
    then fail=1; fi
  else
    # GitHub answers this endpoint with 404 both for "not protected" and for "you are not
    # an admin here" — the bot is never an admin (see header), so this can never be told
    # apart from here. Best-effort: warn, never fail doctor on it.
    warn "cannot read main's branch protection on $PROJECT_REPO (needs admin; the bot has write) — could not confirm '$CHECK_NAME' is required"
  fi
fi

# --- a browser, for looking at pages ------------------------------------------------------
# Every project gets bin/browse.py; it needs a Chrome or Chromium on the box. A warning,
# never a failure: plenty of cards touch no page, and a lane refused over this would stop
# those too.
python3 "$(dirname "$0")/browse.py" find >/dev/null 2>&1 \
  || warn "no Chrome or Chromium on this box — a tick cannot look at a page (apt install chromium)"

if [ "$fail" = 0 ]; then
  echo "doctor: ok"
fi
exit "$fail"
