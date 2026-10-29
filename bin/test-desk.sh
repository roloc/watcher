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
called() { grep -q -- "$1" "$CALLS"; }
not_called() { ! grep -q -- "$1" "$CALLS"; }
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

echo
if [ "$fail" -eq 0 ]; then echo "$n cases, all passed"; else echo "$n cases, FAILURES ABOVE"; fi
exit "$fail"
