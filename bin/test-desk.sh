#!/usr/bin/env bash
# bin/desk.sh's cases, offline: `claude` and `gh` are stubs that log one line per call to
# $CALLS, HOME is a folder under $TMP, and the desk folder is a real (local) git repo.
#
#   bash bin/test-desk.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DESK="$HERE/desk.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }

BIN="$TMP/bin"; mkdir -p "$BIN"
CALLS="$TMP/calls"
export CALLS TMP

# claude: `auth status` prints $TMP/auth-status (default: not logged in); `remote-control`
# logs its argv, its cwd and CLAUDE_CODE_PLUGIN_DIRS, and exits 0 (standing for a server
# that ran and was stopped).
cat > "$BIN/claude" <<'EOF2'
#!/usr/bin/env bash
echo "claude $*" >> "$CALLS"
case "$1" in
  auth) cat "$TMP/auth-status" 2>/dev/null || echo '{ "loggedIn": false }' ;;
  remote-control) echo "remote-control cwd=$PWD plugins=${CLAUDE_CODE_PLUGIN_DIRS:-unset} argv=$*" >> "$CALLS" ;;
esac
EOF2
# gh: `auth status` succeeds unless $TMP/gh-logged-out exists.
cat > "$BIN/gh" <<'EOF2'
#!/usr/bin/env bash
echo "gh $*" >> "$CALLS"
[ -f "$TMP/gh-logged-out" ] && exit 1
exit 0
EOF2
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

HOME_DIR="$TMP/home"; DESK_DIR="$HOME_DIR/desk"
WDIR="$TMP/opt/watcher"; mkdir -p "$WDIR"

fresh() {
  rm -rf "$HOME_DIR" "$WDIR/mods"; mkdir -p "$DESK_DIR"
  git -C "$DESK_DIR" init -q
  rm -f "$TMP/auth-status" "$TMP/gh-logged-out"; : > "$CALLS"
}
run() { # run [env...] — desk.sh with the test's env
  env HOME="$HOME_DIR" DESK_DIR="$DESK_DIR" DESK_CLAUDE="$BIN/claude" WATCHER_DIR="$WDIR" \
      DESK_WAIT_SEC=0 DESK_NAME="testbox desk" "$@" bash "$DESK" > "$TMP/out" 2>&1
  echo $? > "$TMP/rc"
}
rc()  { cat "$TMP/rc"; }
out_has() { grep -q -- "$1" "$TMP/out"; }
called() { grep -qF -- "$1" "$CALLS"; }
not_called() { ! grep -qF -- "$1" "$CALLS"; }
logged_in() { echo '{ "loggedIn": true, "authMethod": "claude.ai" }' > "$TMP/auth-status"; }

echo "desk.sh"

# 1. No desk folder: exit 1, says so, never asks claude anything
fresh; rm -rf "$DESK_DIR"
run
[ "$(rc)" = 1 ] && out_has "no desk folder" && pass "no desk folder: exit 1, says so" || flunk "no desk folder" "rc=$(rc) $(cat "$TMP/out")"
not_called "claude" && pass "…and never calls claude" || flunk "claude called with no folder" "$(cat "$CALLS")"

# 2. Not logged in: says so once, waits, and (with DESK_MAX_WAITS) exits 3 without starting
fresh
run DESK_MAX_WAITS=2
[ "$(rc)" = 3 ] && pass "not logged in: gives up after DESK_MAX_WAITS with exit 3 (the unit restarts it)" || flunk "not logged in exit code" "rc=$(rc)"
out_has "not logged in yet" && out_has "claude auth login" && pass "…says so, with the one command to run" || flunk "no login line" "$(cat "$TMP/out")"
[ "$(grep -c "not logged in yet" "$TMP/out")" = 1 ] && pass "…and says it once, not once per check" || flunk "said more than once" "$(cat "$TMP/out")"
[ "$(grep -c "claude auth status" "$CALLS")" = 3 ] && pass "…having checked the login three times (once, then after each of two waits)" || flunk "wrong number of login checks" "$(cat "$CALLS")"
not_called "remote-control" && pass "…and never started Remote Control" || flunk "started while logged out" "$(cat "$CALLS")"

# 3. Logged in: trust is seeded, then Remote Control starts in the desk folder with the right flags
fresh; logged_in
run
[ "$(rc)" = 0 ] && pass "logged in: runs through (exit 0 once the stub server returns)" || flunk "logged in exit" "rc=$(rc) $(cat "$TMP/out")"
called "remote-control cwd=$DESK_DIR " && pass "…Remote Control runs in the desk folder" || flunk "wrong cwd" "$(cat "$CALLS")"
called "argv=remote-control --name testbox desk --spawn same-dir --capacity 4 --permission-mode default" && pass "…named for the box, same-dir, capacity 4, permission mode default" || flunk "wrong flags" "$(cat "$CALLS")"
python3 - "$HOME_DIR/.claude.json" "$DESK_DIR" <<'PY' && pass "…and ~/.claude.json trusts the desk folder" || flunk "trust not seeded" "$(cat "$HOME_DIR/.claude.json" 2>/dev/null)"
import json, sys
d = json.load(open(sys.argv[1])); sys.exit(0 if d["projects"][sys.argv[2]]["hasTrustDialogAccepted"] is True else 1)
PY
out_has "starting Remote Control as 'testbox desk'" && pass "…and says so in the journal" || flunk "no starting line" "$(cat "$TMP/out")"
out_has "exited after 0s (exit 0); waiting 0s" && pass "…a server that quits at once is followed by a wait before the unit's restart (no 30-second loop)" || flunk "no wait after a quick exit" "$(cat "$TMP/out")"

# 4. Trust is merged into an existing ~/.claude.json, never overwriting what is there
fresh; logged_in
mkdir -p "$HOME_DIR"; echo '{"theme": "dark", "projects": {"/elsewhere": {"hasTrustDialogAccepted": true}}}' > "$HOME_DIR/.claude.json"
run
python3 - "$HOME_DIR/.claude.json" "$DESK_DIR" <<'PY' && pass "an existing ~/.claude.json keeps its other keys and projects" || flunk "merge clobbered the file" "$(cat "$HOME_DIR/.claude.json")"
import json, sys
d = json.load(open(sys.argv[1]))
ok = d.get("theme") == "dark" and d["projects"]["/elsewhere"]["hasTrustDialogAccepted"] is True and d["projects"][sys.argv[2]]["hasTrustDialogAccepted"] is True
sys.exit(0 if ok else 1)
PY

# 5. The guard mod is loaded when the deployed release has it, and not named when it does not
fresh; logged_in
run
called "plugins=unset" && pass "no mods/guard in the release: CLAUDE_CODE_PLUGIN_DIRS is left unset" || flunk "plugin dir set without a mod" "$(cat "$CALLS")"
fresh; logged_in; mkdir -p "$WDIR/mods/guard"
run
called "plugins=$WDIR/mods/guard" && pass "mods/guard present: CLAUDE_CODE_PLUGIN_DIRS names it" || flunk "guard mod not loaded" "$(cat "$CALLS")"

# 6. gh not logged in: one line says so, the desk still starts
fresh; logged_in; touch "$TMP/gh-logged-out"
run
out_has "gh is not logged in" && pass "gh logged out: says so, with the command" || flunk "no gh line" "$(cat "$TMP/out")"
called "remote-control" && pass "…and still starts Remote Control (board reads will fail, the session will not)" || flunk "did not start" "$(cat "$CALLS")"

# 7. bin/floor-api.sh: one argument, a floor API path, nothing else gets through
FLOORAPI="$HERE/floor-api.sh"
cat > "$BIN/curl" <<'EOF2'
#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
EOF2
chmod +x "$BIN/curl"
api() { : > "$CALLS"; bash "$FLOORAPI" "$@" > "$TMP/out" 2>&1; echo $? > "$TMP/rc"; }
echo
echo "floor-api.sh"
api /api/state.json
[ "$(rc)" = 0 ] && called "curl -sS --max-time 20 http://localhost:8787/api/state.json" && pass "/api/state.json: one GET to the floor service on this box" || flunk "state.json" "rc=$(rc) $(cat "$CALLS" "$TMP/out")"
api '/api/tail.json?lane=riffmap-design'
called "http://localhost:8787/api/tail.json?lane=riffmap-design" && pass "a query string rides along" || flunk "query string" "$(cat "$CALLS")"
api 'https://any.host/x'
[ "$(rc)" = 2 ] && not_called "curl" && pass "a URL is refused: only a /api/ path" || flunk "URL not refused" "$(cat "$CALLS" "$TMP/out")"
api '/api/state.json' 'https://any.host/' '-T' "$HOME_DIR/.claude/.credentials.json"
[ "$(rc)" = 2 ] && not_called "curl" && pass "extra arguments are refused (no second host, no upload)" || flunk "extra args not refused" "$(cat "$CALLS" "$TMP/out")"
api '/api/state.json -o /tmp/x'
[ "$(rc)" = 2 ] && not_called "curl" && pass "a space or an option smuggled into the path is refused" || flunk "smuggled option not refused" "$(cat "$CALLS" "$TMP/out")"
api '/api/../etc/passwd'
[ "$(rc)" = 0 ] && called "http://localhost:8787/api/../etc/passwd" && pass "a dotted path still only reaches the floor service (it answers 404; nothing else is reachable)" || flunk "dotted path" "$(cat "$CALLS" "$TMP/out")"
api
[ "$(rc)" = 2 ] && pass "no argument: usage, exit 2" || flunk "no-arg" "rc=$(rc)"

# 8. bin/desk-read.sh: a lane name and one of five reads; it sources the lane's env itself
DESKREAD="$HERE/desk-read.sh"
RDIR="$TMP/release"; mkdir -p "$RDIR/projects" "$RDIR/bin"
printf 'PROJECT_NAME=riffmap\nPROJECT_REPO=roloc/riffmap\nBOARD_OWNER=roloc\nBOARD_NUMBER=1\n' > "$RDIR/projects/riffmap.env"
cat > "$RDIR/bin/board.py" <<'EOF2'
import os, sys
open(os.environ["CALLS"], "a").write("board.py %s owner=%s number=%s\n" % (" ".join(sys.argv[1:]), os.environ.get("BOARD_OWNER", "unset"), os.environ.get("BOARD_NUMBER", "unset")))
EOF2
printf '#!/usr/bin/env bash\necho "spawn.sh $* owner=${BOARD_OWNER:-unset}" >> "$CALLS"\n' > "$RDIR/bin/spawn.sh"
# The script finds its release by its own location, so a copy under $RDIR/bin reads $RDIR —
# and WATCHER_DIR in the environment is deliberately ignored (set here to prove it).
cp "$DESKREAD" "$RDIR/bin/desk-read.sh"
read_() { : > "$CALLS"; WATCHER_DIR="$TMP/not-this-one" bash "$RDIR/bin/desk-read.sh" "$@" > "$TMP/out" 2>&1; echo $? > "$TMP/rc"; }
echo
echo "desk-read.sh"
read_ riffmap show 12
[ "$(rc)" = 0 ] && called "board.py show 12 owner=roloc number=1" && pass "show <n>: board.py show, with the lane's BOARD_OWNER and BOARD_NUMBER sourced for it" || flunk "show" "rc=$(rc) $(cat "$CALLS" "$TMP/out")"
read_ riffmap list "In progress"
called "board.py list In progress owner=roloc" && pass "list <column>: a column name with a space goes through whole" || flunk "list" "$(cat "$CALLS" "$TMP/out")"
read_ riffmap ask 12; called "board.py ask 12" && pass "ask <n>" || flunk "ask" "$(cat "$CALLS")"
read_ riffmap images 12; called "board.py images 12" && pass "images <n>" || flunk "images" "$(cat "$CALLS")"
read_ riffmap dry-run
[ "$(rc)" = 0 ] && called "spawn.sh --dry-run owner=roloc" && pass "dry-run: bash spawn.sh --dry-run under the lane's env, nothing else" || flunk "dry-run" "rc=$(rc) $(cat "$CALLS" "$TMP/out")"
read_ riffmap move 12 Done
[ "$(rc)" = 2 ] && not_called "board.py" && pass "move: not one of the five reads, refused" || flunk "move not refused" "$(cat "$CALLS" "$TMP/out")"
read_ '../../home/watcher-desk/x' show 12
[ "$(rc)" = 2 ] && not_called "board.py" && pass "a lane with a slash or dots is refused (no sourcing of an arbitrary file)" || flunk "bad lane not refused" "$(cat "$CALLS" "$TMP/out")"
read_ nosuch show 12
[ "$(rc)" = 2 ] && out_has "no such lane" && pass "an unknown lane is refused, naming where to look" || flunk "unknown lane" "$(cat "$TMP/out")"
read_ riffmap show '12; rm -rf x'
[ "$(rc)" = 2 ] && not_called "board.py" && pass "show takes a card number only" || flunk "non-number" "$(cat "$CALLS" "$TMP/out")"
read_ riffmap dry-run --once
[ "$(rc)" = 2 ] && not_called "spawn.sh" && pass "dry-run takes no further arguments (no --once smuggled in)" || flunk "dry-run extra arg" "$(cat "$CALLS" "$TMP/out")"
read_ riffmap
[ "$(rc)" = 2 ] && pass "too few arguments: usage, exit 2" || flunk "usage" "rc=$(rc)"

# 9. bin/desk-gh.sh: one owner/repo on github.com, four reads, no host, no URL, no browser, no write
DESKGH="$HERE/desk-gh.sh"
printf '#!/usr/bin/env bash\necho "gh $*" >> "$CALLS"\n' > "$BIN/gh.read"; chmod +x "$BIN/gh.read"
cp "$BIN/gh.read" "$BIN/gh"   # replaces the login-status stub for these cases
ghr() { : > "$CALLS"; bash "$DESKGH" "$@" > "$TMP/out" 2>&1; echo $? > "$TMP/rc"; }
echo
echo "desk-gh.sh"
ghr roloc/watcher issue view 204 --comments
[ "$(rc)" = 0 ] && called "gh issue view 204 --comments -R roloc/watcher" && pass "issue view: gh with the repository fixed by the script, added LAST (gh takes the last -R)" || flunk "issue view" "rc=$(rc) $(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher pr list --search "204 in:body" --state all --json number,title --jq '.[] | .number'
called "gh pr list --search 204 in:body --state all --json number,title --jq .[] | .number -R roloc/watcher" && pass "pr list with a search, a state, --json and a --jq goes through" || flunk "pr list" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher issue list --limit=5 --state=open; called "gh issue list --limit 5 --state open -R roloc/watcher" && pass "--flag=value is accepted and split" || flunk "flag=value" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher run view 123 --json conclusion; called "gh run view 123 --json conclusion -R roloc/watcher" && pass "run view" || flunk "run view" "$(cat "$CALLS")"
ghr roloc/watcher pr checks 218; called "gh pr checks 218 -R roloc/watcher" && pass "pr checks" || flunk "pr checks" "$(cat "$CALLS")"
ghr roloc/watcher issue view 1 -cRlocalhost/o/r
[ "$(rc)" = 2 ] && not_called "gh" && pass "a short-flag cluster ending in R is refused (no short flags at all)" || flunk "-cR" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher issue view 1 -cw
[ "$(rc)" = 2 ] && not_called "gh" && pass "-cw (a cluster that opens the browser) is refused" || flunk "-cw" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher issue view 1 -t '{{.title}}'
[ "$(rc)" = 2 ] && not_called "gh" && pass "-t (short --template) is refused" || flunk "-t" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher issue list --state --web
[ "$(rc)" = 2 ] && not_called "gh" && pass "a flag's value may not itself be a flag" || flunk "value-is-flag" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher issue list --search 'https://evil.example/x'
[ "$(rc)" = 2 ] && not_called "gh" && pass "a value carrying a URL is refused" || flunk "url value" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher pr view main
[ "$(rc)" = 2 ] && not_called "gh" && pass "a positional that is not a number is refused" || flunk "positional" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/.. issue view 1
[ "$(rc)" = 2 ] && not_called "gh" && pass "owner/.. is not a repository" || flunk "dotdot repo" "$(cat "$CALLS" "$TMP/out")"
ghr evil.example/roloc/watcher issue view 1
[ "$(rc)" = 2 ] && not_called "gh" && pass "a host in the repository is refused" || flunk "host/owner/repo" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher issue view 1 -R evil.example/a/b
[ "$(rc)" = 2 ] && not_called "gh" && pass "-R in the arguments is refused" || flunk "-R" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher issue view 1 --repo=evil.example/a/b
[ "$(rc)" = 2 ] && not_called "gh" && pass "--repo= too" || flunk "--repo=" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher pr view https://evil.example/a/b/pull/1
[ "$(rc)" = 2 ] && not_called "gh" && pass "a URL in place of a number is refused" || flunk "URL" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher issue view 1 --web
[ "$(rc)" = 2 ] && not_called "gh" && pass "--web is refused (a browser is a program)" || flunk "--web" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher issue edit 1 --add-label x
[ "$(rc)" = 2 ] && not_called "gh" && pass "a write subcommand is refused" || flunk "edit" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher pr merge 1
[ "$(rc)" = 2 ] && not_called "gh" && pass "pr merge is refused" || flunk "merge" "$(cat "$CALLS" "$TMP/out")"
ghr roloc/watcher run view 1 --log
[ "$(rc)" = 2 ] && not_called "gh" && pass "run view --log is refused (it is not needed, and it is large)" || flunk "--log" "$(cat "$CALLS" "$TMP/out")"
ghr roloc issue view 1
[ "$(rc)" = 2 ] && pass "a bare owner is not a repository" || flunk "bare owner" "rc=$(rc)"

echo
if [ "$fail" -eq 0 ]; then echo "$n cases, all passed"; else echo "$n cases, FAILURES ABOVE"; fi
exit "$fail"
