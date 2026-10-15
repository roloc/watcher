#!/usr/bin/env bash
#
# add-project.sh <name> <owner/repo> — puts a new project on the watcher: the board, the
# labels, main's branch protection, and projects/<name>.env. Card #39: setting up this
# repo's own board (PR #38) took a dozen hand steps, written down in three places, found
# one at a time by failing. This replaces that list with one idempotent script: every step
# checks first and does only what is missing, so a run that stops partway — or one you
# simply run again later — is always safe to run again.
#
# Run from Steve's Mac (or wherever), authenticated as YOURSELF (`gh auth login`) — the
# account that will own the repo and the new board — never as the bot. Two of the steps
# below (setting main's branch protection, and the one-time grants the ask-first list
# names) need admin on the repo and ownership of the board; the bot never has either, on
# purpose (bin/doctor.sh's own header explains why). Run it from inside a checkout of this
# repo (projects/<name>.env is written next to this script's own projects/ directory).
#
#   deploy/add-project.sh riffmap roloc/riffmap
#
# Env (everything has a sane default; TEMPLATE_BOARD is optional — card #156):
#   TEMPLATE_BOARD     <owner>/<number> of a clean, existing board to copy — Status
#                      (Backlog/Ready/In progress/In review/Done/Stuck - Needs help),
#                      Stage and Priority already set up, no live cards on it. `gh project
#                      copy` works on any project the caller can read; it does not need to
#                      be marked as a GitHub "template" (mark-template is organization-only
#                      anyway — a personal board like this one's can never carry that flag).
#                      Unset: a forker has no such board to point at, so this builds one
#                      from nothing instead — `gh project create`, then the Status, Stage
#                      and Priority fields built to match what bin/doctor.sh requires.
#   BOARD_OWNER        who owns the new board                   (default: the owner in <owner/repo>)
#   BOT_LOGIN           the bot account the watcher runs as      (default: riffmap-bot)
#   REQUIRED_CHECK      the CI check name to require on main     (default: verify)
#   ADD_PROJECT_YES     1 = treat every "have you done this?" confirmation as yes, and
#                        never prompt — for a non-interactive run (scripting, this script's
#                        own test)                               (default: 0; prompts when
#                                                                  stdin is a terminal, else
#                                                                  requires this)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHER_ROOT="${WATCHER_ROOT:-$(dirname "$HERE")}"
WATCHER_GH="${WATCHER_GH:-gh}"

usage() { echo "usage: deploy/add-project.sh <name> <owner/repo>" >&2; exit 64; }
[ $# -eq 2 ] || usage
NAME="$1"
REPO="$2"
case "$REPO" in */*) ;; *) echo "add-project.sh: <owner/repo> must contain a /, got '$REPO'" >&2; exit 64 ;; esac
REPO_OWNER="${REPO%%/*}"

BOARD_OWNER="${BOARD_OWNER:-$REPO_OWNER}"
BOT_LOGIN="${BOT_LOGIN:-riffmap-bot}"
REQUIRED_CHECK="${REQUIRED_CHECK:-verify}"
ADD_PROJECT_YES="${ADD_PROJECT_YES:-0}"
TEMPLATE_BOARD="${TEMPLATE_BOARD:-}"
if [ -n "$TEMPLATE_BOARD" ]; then
  TEMPLATE_OWNER="${TEMPLATE_BOARD%%/*}"
  TEMPLATE_NUMBER="${TEMPLATE_BOARD##*/}"
fi

say() { printf '\n== %s ==\n' "$*"; }

# confirm <question> — "have you done this?" gate. A tty gets a real prompt; a
# non-interactive run needs ADD_PROJECT_YES=1 (never assumes yes by default — the whole
# point of asking first is that a script must not guess it was done).
confirm() {
  if [ "$ADD_PROJECT_YES" = 1 ]; then return 0; fi
  if [ ! -t 0 ]; then
    echo "add-project.sh: '$1' needs confirming, and stdin is not a terminal — re-run with ADD_PROJECT_YES=1 once it's true" >&2
    return 1
  fi
  local reply
  read -r -p "$1 [y/N] " reply
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# --- ask first: the one step this script cannot do unasked --------------------------------
# Granting access is the board/repo owner's alone — GitHub gives no API a script can use to
# invite a collaborator onto a Projects v2 board, and a repo collaborator grant is exactly
# the kind of standing access decision that stays a human's, not something to script past.
say "ask first — $BOT_LOGIN needs access"
perm="$($WATCHER_GH api "repos/$REPO/collaborators/$BOT_LOGIN/permission" --jq .permission 2>/dev/null || true)"
case "$perm" in
  admin|maintain|write) echo "  repo: $BOT_LOGIN already has '$perm' on $REPO" ;;
  *)
    cat >&2 <<EOF
add-project.sh: $BOT_LOGIN does not have write access to $REPO (permission: '${perm:-none}').
Grant it, then run this script again:
  $REPO → Settings → Collaborators → Add people → $BOT_LOGIN → Write
EOF
    exit 1
    ;;
esac

# --- the board's required fields, for when there is no template to copy (card #156) -------
# bin/doctor.sh is the spec: the board this builds is the board it accepts, and it checks
# by presence only, never order — so idempotency matches the same way, order-independent.
# Status is built-in on every new project, with GitHub's own default options; Stage and
# Priority don't exist yet on a fresh board. On a rerun, once a field already carries
# everything required (in any order, extras allowed), it is touched not at all. Short of
# that: on a board THIS run just created ($BOARD_FRESH=1 — nothing could have a card on it
# yet, so there is nothing to protect), Status gets replaced outright, discarding GitHub's
# own defaults. On a board that already existed, nothing already there is ever discarded —
# a card may already carry one of those values — so the field keeps every option it has,
# by id, and gains only what's missing.
STATUS_REQUIRED='[{"name":"Backlog","color":"GRAY","description":""},{"name":"Ready","color":"BLUE","description":""},{"name":"In progress","color":"YELLOW","description":""},{"name":"In review","color":"ORANGE","description":""},{"name":"Done","color":"GREEN","description":""},{"name":"Stuck - Needs help","color":"RED","description":""}]'
STAGE_REQUIRED='[{"name":"Waiting","color":"GRAY","description":""},{"name":"Claimed","color":"BLUE","description":""},{"name":"Designing","color":"PURPLE","description":""},{"name":"Building","color":"YELLOW","description":""},{"name":"Testing","color":"ORANGE","description":""},{"name":"Reviewing","color":"PINK","description":""},{"name":"PR open","color":"GREEN","description":""},{"name":"Merging","color":"BLUE","description":""},{"name":"Parked","color":"RED","description":""}]'
PRIORITY_REQUIRED='[{"name":"P0","color":"RED","description":""},{"name":"P1","color":"YELLOW","description":""},{"name":"P2","color":"GRAY","description":""}]'

# ensure_field <field-name> <required-json> — reads $FIELDS_JSON and $BOARD_FRESH (the
# caller sets both first). Prints one plan, for apply_field to carry out:
#   SKIP
#   CREATE
#   <name1,name2,...>
#   REPLACE
#   <field-id>
#   <inline GraphQL option list, one line>
ensure_field() {
  python3 - "$FIELDS_JSON" "$BOARD_FRESH" "$1" "$2" <<'PY'
import json, sys
fields_json, fresh, name, required_json = sys.argv[1:5]
fields = {f["name"]: f for f in json.loads(fields_json).get("fields", [])}
required = json.loads(required_json)
f = fields.get(name)
if not f or f.get("type") != "ProjectV2SingleSelectField":
    print("CREATE")
    print(",".join(o["name"] for o in required))
    sys.exit(0)
existing = f.get("options", [])
existing_names = [o["name"] for o in existing]
required_names = [o["name"] for o in required]
if set(required_names) <= set(existing_names):
    print("SKIP")
    sys.exit(0)
missing = [o for o in required if o["name"] not in existing_names]
if fresh == "1":
    new_options = required
else:
    kept = [{"id": o["id"], "name": o["name"], "color": "GRAY", "description": ""} for o in existing]
    new_options = kept + missing
def lit(o):
    idpart = ('id: %s, ' % json.dumps(o["id"])) if "id" in o else ""
    return "{%sname: %s, color: %s, description: %s}" % (
        idpart, json.dumps(o["name"]), o["color"], json.dumps(o.get("description", "")))
print("REPLACE")
print(f["id"])
print("[" + ", ".join(lit(o) for o in new_options) + "]")
PY
}

apply_field() {
  local name="$1" plan action
  plan="$(ensure_field "$name" "$2")"
  action="$(echo "$plan" | sed -n 1p)"
  case "$action" in
    SKIP)
      echo "  $name: already has the required options (unchanged)"
      ;;
    CREATE)
      $WATCHER_GH project field-create "$BOARD_NUMBER" --owner "$BOARD_OWNER" --name "$name" \
        --data-type SINGLE_SELECT --single-select-options "$(echo "$plan" | sed -n 2p)" >/dev/null
      echo "  $name: created"
      ;;
    REPLACE)
      local field_id options
      field_id="$(echo "$plan" | sed -n 2p)"
      options="$(echo "$plan" | sed -n 3p)"
      $WATCHER_GH api graphql -f query="mutation { updateProjectV2Field(input: {fieldId: \"$field_id\", singleSelectOptions: $options}) { projectV2Field { ... on ProjectV2SingleSelectField { id } } } }" >/dev/null
      echo "  $name: set its options"
      ;;
  esac
}

ensure_board_fields() {
  FIELDS_JSON="$($WATCHER_GH project field-list "$BOARD_NUMBER" --owner "$BOARD_OWNER" --format json)"
  apply_field "Status" "$STATUS_REQUIRED"
  apply_field "Stage" "$STAGE_REQUIRED"
  apply_field "Priority" "$PRIORITY_REQUIRED"
}

# --- the board: find, copy or build it from nothing, then link the repo ------------------
say "the board"
existing="$($WATCHER_GH project list --owner "$BOARD_OWNER" --format json | python3 -c '
import json, sys
name = sys.argv[1]
for p in json.load(sys.stdin)["projects"]:
    if p["title"] == name:
        print(p["number"]); break
' "$NAME")"
if [ -n "$existing" ]; then
  BOARD_NUMBER="$existing"
  BOARD_FRESH=0
  echo "  board \"$NAME\" already exists for $BOARD_OWNER: #$BOARD_NUMBER (unchanged)"
elif [ -n "$TEMPLATE_BOARD" ]; then
  BOARD_NUMBER="$($WATCHER_GH project copy "$TEMPLATE_NUMBER" --source-owner "$TEMPLATE_OWNER" \
    --target-owner "$BOARD_OWNER" --title "$NAME" --format json --jq .number)"
  echo "  copied $TEMPLATE_OWNER/#$TEMPLATE_NUMBER → $BOARD_OWNER/#$BOARD_NUMBER, titled \"$NAME\""
else
  BOARD_NUMBER="$($WATCHER_GH project create --owner "$BOARD_OWNER" --title "$NAME" --format json --jq .number)"
  BOARD_FRESH=1
  echo "  created $BOARD_OWNER/#$BOARD_NUMBER, titled \"$NAME\""
fi
if [ -z "$TEMPLATE_BOARD" ]; then
  ensure_board_fields
fi
$WATCHER_GH project link "$BOARD_NUMBER" --owner "$BOARD_OWNER" --repo "$REPO" >/dev/null
echo "  linked to $REPO"

# --- ask first, part 2: only checkable now that the board exists --------------------------
# Neither of these has an API: board collaborator access is the same undocumented gap as
# above, and the GraphQL `workflows` field exposes only a workflow's name and whether it is
# on — never its filter (bin/doctor.sh's own header, and card #39's "Details": the Auto-add
# workflow does not survive `gh project copy` at all, so this is never a no-op).
say "ask first — board access and Auto-add"
cat <<EOF
  1. Board access: $BOARD_OWNER/#$BOARD_NUMBER → ⋯ menu → Settings → Manage access → add
     $BOT_LOGIN with Write.
  2. Auto-add: same board → ⋯ menu → Workflows → Auto-add to project → turn it on, filter
     "is:issue is:open" (NOT GitHub's own "is:issue,pr is:open label:bug" example — a board
     copy carries no cards across silently on that default; card #39, September 28, 2026).
EOF
confirm "Both of those done?" || { echo "add-project.sh: stopping here — re-run once they are" >&2; exit 1; }

# --- labels on the repo ---------------------------------------------------------------------
say "labels"
$WATCHER_GH label create in-flight --repo "$REPO" --color fef2c0 \
  --description "A tick is working this card right now" --force >/dev/null
$WATCHER_GH label create blocked --repo "$REPO" --color 000000 \
  --description "Straight to Stuck" --force >/dev/null
$WATCHER_GH label create steve-decision --repo "$REPO" --color d4c5f9 \
  --description "A product decision: Steve's, never a tick's" --force >/dev/null
# Not in doctor.sh's required list (card #85): most projects never need it, and a missing
# optional label must never block every lane from starting. Created here anyway so the
# first tick that DOES hit watch.md §3's awaiting-rebuild exit (a merged fix gated on a
# known downstream rebuild) can `gh issue edit --add-label` it without that call failing on
# a label that was never created.
$WATCHER_GH label create awaiting-rebuild --repo "$REPO" --color c5def5 \
  --description "Fix merged; waiting on a known downstream rebuild (watch.md §3)" --force >/dev/null
echo "  in-flight, blocked, steve-decision, awaiting-rebuild: ensured on $REPO"

# --- main's branch protection --------------------------------------------------------------
say "branch protection on main"
current="$($WATCHER_GH api "repos/$REPO/branches/main/protection" 2>/dev/null || true)"
already_set=0
if [ -n "$current" ]; then
  already_set="$(python3 -c '
import json, sys
d = json.loads(sys.argv[1])
check = sys.argv[2]
rsc = d.get("required_status_checks") or {}
checks = {c.get("context") for c in (rsc.get("checks") or []) if c.get("context")} | set(rsc.get("contexts") or [])
prr = d.get("required_pull_request_reviews") or {}
ok = (check in checks
      and prr.get("required_approving_review_count") == 0
      and not (d.get("enforce_admins") or {}).get("enabled"))
print(1 if ok else 0)
' "$current" "$REQUIRED_CHECK")"
fi
if [ "$already_set" = 1 ]; then
  echo "  already set: \"$REQUIRED_CHECK\" required, PR required, 0 approvals, admins not enforced (unchanged)"
else
  # Keep any OTHER required checks already on the branch — this only ever ADDS
  # $REQUIRED_CHECK, never drops a context some other setup put there.
  context_args=(-f "required_status_checks[contexts][]=$REQUIRED_CHECK")
  if [ -n "$current" ]; then
    while IFS= read -r ctx; do
      [ -n "$ctx" ] && [ "$ctx" != "$REQUIRED_CHECK" ] && context_args+=(-f "required_status_checks[contexts][]=$ctx")
    done < <(python3 -c '
import json, sys
d = json.loads(sys.argv[1])
rsc = d.get("required_status_checks") or {}
for c in sorted({c.get("context") for c in (rsc.get("checks") or []) if c.get("context")} | set(rsc.get("contexts") or [])):
    print(c)
' "$current")
  fi
  $WATCHER_GH api -X PUT "repos/$REPO/branches/main/protection" \
    -H "Accept: application/vnd.github+json" \
    -F 'required_status_checks[strict]=false' \
    "${context_args[@]}" \
    -F 'enforce_admins=false' \
    -F 'required_pull_request_reviews[required_approving_review_count]=0' \
    -F 'restrictions=null' >/dev/null
  echo "  set: \"$REQUIRED_CHECK\" required, PR required, 0 approvals, admins not enforced"
fi

# --- projects/<name>.env --------------------------------------------------------------------
say "projects/$NAME.env"
ENV_FILE="$WATCHER_ROOT/projects/$NAME.env"
if [ -f "$ENV_FILE" ]; then
  echo "  $ENV_FILE already exists — left unchanged (edit it by hand, or remove it and re-run)"
else
  copy_dirs=""
  if confirm "Does $REPO have gitignored, in-tree private inputs a tick should read (transcripts, fixtures)? (No → empty)"; then
    if [ -t 0 ]; then
      read -r -p "  WORKTREE_COPY_DIRS (space-separated, relative to the checkout): " copy_dirs
    else
      echo "  stdin is not a terminal — leaving WORKTREE_COPY_DIRS empty; set it by hand in $ENV_FILE"
    fi
  fi
  cat > "$ENV_FILE" <<EOF
PROJECT_NAME=$NAME
PROJECT_ROOT=/home/watcher/projects/$NAME
PROJECT_REPO=$REPO
BOARD_OWNER=$BOARD_OWNER
BOARD_NUMBER=$BOARD_NUMBER
WORKTREE_COPY_DIRS="$copy_dirs"
EOF
  echo "  wrote $ENV_FILE"
fi

say "done"
cat <<EOF
Next:
  - Commit and push projects/$NAME.env (git add/commit/push — this script does not).
  - On the box: deploy/install.sh $NAME; systemctl enable --now watcher@$NAME
  - bin/doctor.sh (run by install.sh) is the read-only check that everything above landed.
  - In the project's own CLAUDE.md: paste templates/project-CLAUDE.md and fill it in. A
    project with a screen only has to say where its pages are and how to serve a branch;
    the browser itself (bin/browse.py) comes with the box.
EOF
