#!/usr/bin/env bash
# deploy/add-project.sh's cases, in test-detect.sh's shape: no network, `gh` is a stub that
# logs one line per call to $CALLS so a case can grep what the script did. Card #156: with
# no TEMPLATE_BOARD, the script now builds a board from nothing instead of refusing to run.
# What the card itself asks for is cases 1-3 below: the no-template create path (the three
# field calls, in order, with the exact option lists), the with-template copy path
# (unchanged — no field calls at all), and a rerun on a board that already has the fields
# (no writes). Cases 4 and 5 are from the reviewer's pass on this card's own PR: a field
# matched by presence, not order (the real Watcher board's own Status field carries Done
# and Stuck - Needs help swapped relative to the order this script would have written —
# a full-match-but-reordered field must never be touched at all), and a field short just
# one option keeping every option it already has by id, so a live card's value survives.
#
#   bash bin/test-add-project.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../deploy/add-project.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else flunk "$what"; fi; }

BIN="$TMP/bin"; mkdir -p "$BIN"
CALLS="$TMP/calls"
export CALLS TMP

# gh: logs every call, then answers just enough to carry add-project.sh through a full run.
#   project list        -> $TMP/project-list.json (default: no board by that name)
#   project copy|create -> $TMP/next-board-number  (default: 42) — stands in for --jq .number
#   project field-list  -> $TMP/fields.json        (default: no fields at all)
#   api .../permission  -> $TMP/bot-permission     (default: write — the ask-first gate passes)
#   api .../protection (GET, no -X) -> exit 1 (404, "cannot verify") unless $TMP/protection.json exists
# Everything else (project link, project field-create, label create, api graphql, the
# protection PUT) is a no-op: the script never reads their output.
cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$CALLS"
case "$1" in
  project)
    case "$2" in
      list)       cat "$TMP/project-list.json" 2>/dev/null || echo '{"projects":[]}' ;;
      copy|create) cat "$TMP/next-board-number" 2>/dev/null || echo 42 ;;
      field-list) cat "$TMP/fields.json" 2>/dev/null || echo '{"fields":[]}' ;;
    esac
    ;;
  api)
    case " $* " in
      *" -X PUT "*) : ;;
      *collaborators*permission*) cat "$TMP/bot-permission" 2>/dev/null || echo write ;;
      *branches/main/protection*) [ -f "$TMP/protection.json" ] && cat "$TMP/protection.json" || exit 1 ;;
    esac
    ;;
esac
EOF
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"
export WATCHER_GH=gh ADD_PROJECT_YES=1

REPO_ROOT="$TMP/repo"
mkdir -p "$REPO_ROOT/deploy" "$REPO_ROOT/projects"
cp "$SCRIPT" "$REPO_ROOT/deploy/add-project.sh"
export WATCHER_ROOT="$REPO_ROOT"

project_list() { echo "$1" > "$TMP/project-list.json"; }
next_number()  { echo "$1" > "$TMP/next-board-number"; }
fields()       { echo "$1" > "$TMP/fields.json"; }

run() {
  : > "$CALLS"
  rm -f "$REPO_ROOT/projects/testproj.env"
  bash "$REPO_ROOT/deploy/add-project.sh" testproj roloc/testproj > "$TMP/out" 2>&1
  echo $? > "$TMP/rc"
}
rc()         { cat "$TMP/rc"; }
out_has()    { grep -qF -- "$1" "$TMP/out"; }
called()     { grep -qF -- "$1" "$CALLS"; }
not_called() { ! grep -qF -- "$1" "$CALLS"; }
line_of()    { grep -nF -- "$1" "$CALLS" | head -1 | cut -d: -f1; }
before()     { local a b; a="$(line_of "$1")"; b="$(line_of "$2")"; [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; }

STATUS_DEFAULTS='{"id":"PVTSSF_STATUS","name":"Status","type":"ProjectV2SingleSelectField","options":[{"id":"a","name":"Todo"},{"id":"b","name":"In Progress"},{"id":"c","name":"Done"}]}'
STATUS_DONE='{"id":"PVTSSF_STATUS","name":"Status","type":"ProjectV2SingleSelectField","options":[{"id":"a","name":"Backlog"},{"id":"b","name":"Ready"},{"id":"c","name":"In progress"},{"id":"d","name":"In review"},{"id":"e","name":"Done"},{"id":"f","name":"Stuck - Needs help"}]}'
STAGE_DONE='{"id":"PVTSSF_STAGE","name":"Stage","type":"ProjectV2SingleSelectField","options":[{"id":"a","name":"Waiting"},{"id":"b","name":"Claimed"},{"id":"c","name":"Designing"},{"id":"d","name":"Building"},{"id":"e","name":"Testing"},{"id":"f","name":"Reviewing"},{"id":"g","name":"PR open"},{"id":"h","name":"Merging"},{"id":"i","name":"Parked"}]}'
PRIORITY_DONE='{"id":"PVTSSF_PRIORITY","name":"Priority","type":"ProjectV2SingleSelectField","options":[{"id":"a","name":"P0"},{"id":"b","name":"P1"},{"id":"c","name":"P2"}]}'

echo "deploy/add-project.sh"

# 1. No TEMPLATE_BOARD, no existing board: builds one from nothing, in order.
unset TEMPLATE_BOARD
project_list '{"projects":[]}'; next_number 42; fields "{\"fields\":[$STATUS_DEFAULTS]}"
run
check "no template: exits 0"                              [ "$(rc)" = 0 ]
check "no template: creates the board, not a copy"        called "gh project create --owner roloc --title testproj"
check "no template: never calls project copy"             not_called "gh project copy"
check "no template: Status gets replaced wholesale"        called 'updateProjectV2Field(input: {fieldId: "PVTSSF_STATUS", singleSelectOptions: [{name: "Backlog", color: GRAY, description: ""}, {name: "Ready", color: BLUE, description: ""}, {name: "In progress", color: YELLOW, description: ""}, {name: "In review", color: ORANGE, description: ""}, {name: "Done", color: GREEN, description: ""}, {name: "Stuck - Needs help", color: RED, description: ""}]'
check "no template: Stage is created with all nine stages" called "gh project field-create 42 --owner roloc --name Stage --data-type SINGLE_SELECT --single-select-options Waiting,Claimed,Designing,Building,Testing,Reviewing,PR open,Merging,Parked"
check "no template: Priority is created with P0, P1, P2"   called "gh project field-create 42 --owner roloc --name Priority --data-type SINGLE_SELECT --single-select-options P0,P1,P2"
check "no template: Status before Stage"                    before "updateProjectV2Field" "name Stage"
check "no template: Stage before Priority"                  before "name Stage" "name Priority"
check "no template: links the new board to the repo"       called "gh project link 42 --owner roloc --repo roloc/testproj"
check "no template: writes projects/testproj.env"           grep -q "BOARD_NUMBER=42" "$REPO_ROOT/projects/testproj.env"

# 2. TEMPLATE_BOARD set: the copy path, unchanged — no field calls at all.
export TEMPLATE_BOARD="roloc/9"
project_list '{"projects":[]}'; next_number 7
run
check "with template: exits 0"                             [ "$(rc)" = 0 ]
check "with template: copies, never creates"               called "gh project copy 9 --source-owner roloc --target-owner roloc --title testproj"
check "with template: never calls project create"          not_called "gh project create"
check "with template: no field-list lookup"                not_called "field-list"
check "with template: no field-create call"                not_called "field-create"
check "with template: no graphql call"                     not_called "graphql"
check "with template: links the copied board"              called "gh project link 7 --owner roloc --repo roloc/testproj"

# 3. Rerun on a board that already exists with the right fields: no writes.
unset TEMPLATE_BOARD
project_list '{"projects":[{"title":"testproj","number":42}]}'
fields "{\"fields\":[$STATUS_DONE,$STAGE_DONE,$PRIORITY_DONE]}"
run
check "rerun: exits 0"                                     [ "$(rc)" = 0 ]
check "rerun: the existing board is reused, not recreated" out_has "already exists for roloc: #42"
check "rerun: never calls project create"                  not_called "gh project create"
check "rerun: never calls project copy"                    not_called "gh project copy"
check "rerun: Status untouched"                             out_has "Status: already has the required options (unchanged)"
check "rerun: Stage untouched"                              out_has "Stage: already has the required options (unchanged)"
check "rerun: Priority untouched"                            out_has "Priority: already has the required options (unchanged)"
check "rerun: no field-create call"                        not_called "field-create"
check "rerun: no graphql call"                              not_called "graphql"
check "rerun: still links (link is itself idempotent)"      called "gh project link 42 --owner roloc --repo roloc/testproj"

# 4. Status already carries every required name, but in a different order than this
#    script would have written — the live Watcher board's own shape. Left alone entirely:
#    a full-match-by-presence is enough, and a destructive replace here would drop every
#    option's id, orphaning whatever a real card already has set.
STATUS_REORDERED='{"id":"PVTSSF_STATUS","name":"Status","type":"ProjectV2SingleSelectField","options":[{"id":"a","name":"Backlog"},{"id":"b","name":"Ready"},{"id":"c","name":"In progress"},{"id":"d","name":"In review"},{"id":"e","name":"Stuck - Needs help"},{"id":"f","name":"Done"}]}'
fields "{\"fields\":[$STATUS_REORDERED,$STAGE_DONE,$PRIORITY_DONE]}"
run
check "reordered Status: exits 0"                           [ "$(rc)" = 0 ]
check "reordered Status: left alone, not rewritten"          out_has "Status: already has the required options (unchanged)"
check "reordered Status: no graphql call at all"            not_called "graphql"

# 5. Rerun where Stage is missing just one option: gets that option, keeping every
#    existing one by id — nothing else.
STAGE_MISSING_PARKED='{"id":"PVTSSF_STAGE","name":"Stage","type":"ProjectV2SingleSelectField","options":[{"id":"a","name":"Waiting"},{"id":"b","name":"Claimed"},{"id":"c","name":"Designing"},{"id":"d","name":"Building"},{"id":"e","name":"Testing"},{"id":"f","name":"Reviewing"},{"id":"g","name":"PR open"},{"id":"h","name":"Merging"}]}'
fields "{\"fields\":[$STATUS_DONE,$STAGE_MISSING_PARKED,$PRIORITY_DONE]}"
run
check "missing one option: exits 0"                        [ "$(rc)" = 0 ]
check "missing one option: Status still untouched"          out_has "Status: already has the required options (unchanged)"
check "missing one option: Priority still untouched"        out_has "Priority: already has the required options (unchanged)"
check "missing one option: Stage gets a graphql replace"    called "updateProjectV2Field"
check "missing one option: keeps every existing stage, by id" called '{id: "a", name: "Waiting", color: GRAY, description: ""}, {id: "b", name: "Claimed", color: GRAY, description: ""}'
check "missing one option: and appends the missing one, with no id of its own" called '{name: "Parked", color: RED, description: ""}'
check "missing one option: Priority gets no graphql call for its own sake" [ "$(grep -c graphql "$CALLS")" = 1 ]

echo
if [ "$fail" = 0 ]; then echo "$n cases, all passed"; else echo "FAILED (of $n cases)"; fi
exit "$fail"
