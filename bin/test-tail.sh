#!/usr/bin/env bash
# bin/tail.sh + bin/tail.py's cases. No network, no real ~/.claude/projects: a fake
# CLAUDE_PROJECTS_DIR holds synthetic session transcripts, a fake WATCHER_DIR holds the
# project env files, a fake WATCHER_STATE holds the `current` marker. Every case runs
# --no-follow, so there is no polling loop to race.
#
#   bash bin/test-tail.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TAIL="$HERE/tail.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else flunk "$what"; fi; }

WDIR="$TMP/opt"; STATE_ROOT="$TMP/state"; PROJECTS="$TMP/claude-projects"
mkdir -p "$WDIR/projects" "$STATE_ROOT" "$PROJECTS"

run() {
  local lane="${LANE:-testlane}"
  WATCHER_DIR="$WDIR" CLAUDE_PROJECTS_DIR="$PROJECTS" WATCHER_STATE="$STATE_ROOT/$lane" \
    bash "$TAIL" "$lane" --no-follow \
    > "$TMP/out" 2> "$TMP/err"
  echo $? > "$TMP/rc"
}
rc()      { cat "$TMP/rc"; }
out_has() { grep -qF -- "$1" "$TMP/out"; }
err_has() { grep -qF -- "$1" "$TMP/err"; }
out_lacks() { ! grep -qF -- "$1" "$TMP/out"; }

# encode_dir mirrors tail.py's own: every "/" becomes "-".
session_dir_for() { echo "$PROJECTS/$(echo "$1" | sed 's#/#-#g')"; }

write_transcript() {
  # $1 = target .jsonl path. Covers: an assistant Bash tool_use with a description, an
  # assistant Edit tool_use, an assistant text reply, the initial user turn, and three
  # kinds of noise that must be dropped — a non-assistant/user type, a thinking-only
  # assistant message with no text/tool_use, and one line of garbage JSON.
  mkdir -p "$(dirname "$1")"
  cat > "$1" <<'EOF'
{"type":"user","message":{"role":"user","content":"<command-message>watch</command-message>\n<command-name>/watch</command-name>\n<command-args>42</command-args>"}}
{"type":"queue-operation","operation":"enqueue","content":"/watch 42"}
{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"hmm"}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"board list \"In progress\"","description":"List cards in In progress"}}]}}
not valid json at all
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit","input":{"file_path":"bin/foo.sh","old_string":"a","new_string":"b"}}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"Card #42 looks claimable."}]}}
EOF
}

fresh() {
  rm -rf "$STATE_ROOT" "$PROJECTS"
  mkdir -p "$STATE_ROOT" "$PROJECTS"
  rm -f "$WDIR/projects"/*.env
  LANE=testlane
}

echo "tail.sh / tail.py"

# 1. No projects/<lane>.env at all
fresh
run
check "no project env: exits 64"                        [ "$(rc)" = 64 ]
check "no project env: says which file is missing"      err_has "testlane.env"

# 2. Project env sets no PROJECT_ROOT
fresh
cat > "$WDIR/projects/testlane.env" <<'EOF'
PROJECT_NAME=testlane
EOF
run
# bash's own ${var:?msg} exits 1, not a code this script chooses — only the message is ours.
check "no PROJECT_ROOT: fails, not silently"             [ "$(rc)" != 0 ]
check "no PROJECT_ROOT: says so"                          err_has "PROJECT_ROOT"

# 3. A bad LANE_WORKDIR in the project env
fresh
cat > "$WDIR/projects/testlane.env" <<EOF
PROJECT_NAME=testlane
PROJECT_ROOT=$TMP/repo
LANE_WORKDIR=sideways
EOF
run
check "bad LANE_WORKDIR: exits 64"                        [ "$(rc)" = 64 ]
check "bad LANE_WORKDIR: names the bad value"             err_has "sideways"

# 4. Lane idle: no `current` file at all
fresh
cat > "$WDIR/projects/testlane.env" <<EOF
PROJECT_NAME=testlane
PROJECT_ROOT=$TMP/repo
EOF
mkdir -p "$STATE_ROOT/testlane"
run
check "idle lane: exits 0"                                [ "$(rc)" = 0 ]
check "idle lane: says so"                                 out_has "lane idle"

# 5. Lane idle: `current` file present but empty
fresh
cat > "$WDIR/projects/testlane.env" <<EOF
PROJECT_NAME=testlane
PROJECT_ROOT=$TMP/repo
EOF
mkdir -p "$STATE_ROOT/testlane"; : > "$STATE_ROOT/testlane/current"
run
check "empty current: reads as idle, not a card"          out_has "lane idle"

# 6. A card is current, but its session hasn't started writing yet
fresh
cat > "$WDIR/projects/testlane.env" <<EOF
PROJECT_NAME=testlane
PROJECT_ROOT=$TMP/repo
EOF
mkdir -p "$STATE_ROOT/testlane"; echo 42 > "$STATE_ROOT/testlane/current"
run
check "no session yet: exits 0, not an error"             [ "$(rc)" = 0 ]
check "no session yet: names the card"                     out_has "#42"
check "no session yet: says it's waiting"                  out_has "waiting for a session"

# 7. The real case: card workdir, a transcript with signal and noise mixed
fresh
cat > "$WDIR/projects/testlane.env" <<EOF
PROJECT_NAME=testlane
PROJECT_ROOT=$TMP/repo
EOF
mkdir -p "$STATE_ROOT/testlane"; echo 42 > "$STATE_ROOT/testlane/current"
write_transcript "$(session_dir_for "$TMP/repo-worktrees/card-42")/session-1.jsonl"
run
check "card workdir: exits 0"                             [ "$(rc)" = 0 ]
check "card workdir: banners the card"                     out_has "-- #42 --"
check "card workdir: shows the slash command"               out_has "> <command-message>watch"
check "card workdir: shows Bash's own description, not its raw command" out_has '$ List cards in In progress'
check "card workdir: shows Edit's file path"                out_has "Edit: bin/foo.sh"
check "card workdir: shows the assistant's own words"       out_has "Card #42 looks claimable."
check "card workdir: drops the queue-operation noise"        out_lacks "queue-operation"
check "card workdir: drops the thinking-only turn (no line for it)" out_lacks "hmm"
check "card workdir: drops the garbage JSON line, not fatal"  out_lacks "not valid json"

# 8. LANE_WORKDIR=root: the session directory is keyed on PROJECT_ROOT itself, no
# "-worktrees/card-<n>" suffix — a triage-shaped lane.
fresh
cat > "$WDIR/projects/testlane.env" <<EOF
PROJECT_NAME=testlane
PROJECT_ROOT=$TMP/repo
LANE_WORKDIR=root
EOF
mkdir -p "$STATE_ROOT/testlane"; echo 42 > "$STATE_ROOT/testlane/current"
write_transcript "$(session_dir_for "$TMP/repo")/session-1.jsonl"
run
check "root workdir: exits 0"                              [ "$(rc)" = 0 ]
check "root workdir: finds the session keyed on PROJECT_ROOT" out_has "List cards in In progress"

# 9. Two session files for the same card: the newer one (by mtime) wins, not the older one
fresh
cat > "$WDIR/projects/testlane.env" <<EOF
PROJECT_NAME=testlane
PROJECT_ROOT=$TMP/repo
EOF
mkdir -p "$STATE_ROOT/testlane"; echo 42 > "$STATE_ROOT/testlane/current"
d="$(session_dir_for "$TMP/repo-worktrees/card-42")"
mkdir -p "$d"
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"an old stale resumed session"}]}}' > "$d/old.jsonl"
touch -d '1 hour ago' "$d/old.jsonl"
write_transcript "$d/new.jsonl"
run
check "two sessions: exits 0"                              [ "$(rc)" = 0 ]
check "two sessions: reads the newer file"                  out_has "Card #42 looks claimable."
check "two sessions: ignores the older one"                 out_lacks "an old stale resumed session"

# 10. A lane name with no matching project file at all still fails closed, not silently
fresh
cat > "$WDIR/projects/testlane.env" <<EOF
PROJECT_NAME=testlane
PROJECT_ROOT=$TMP/repo
EOF
LANE=nosuchlane
run
check "unknown lane: exits 64"                             [ "$(rc)" = 64 ]
check "unknown lane: names the file it looked for"          err_has "nosuchlane.env"

# 11. --path <n>: the other caller (bin/spawn.sh, card #143) — prints the newest session's
# path and exits, no --state or polling involved.
fresh
write_transcript "$(session_dir_for "$TMP/repo-worktrees/card-42")/session-1.jsonl"
CLAUDE_PROJECTS_DIR="$PROJECTS" python3 "$HERE/tail.py" --path 42 \
  --workdir-mode card --worktree-base "$TMP/repo-worktrees" \
  > "$TMP/out" 2> "$TMP/err"
echo $? > "$TMP/rc"
check "--path: exits 0"                                    [ "$(rc)" = 0 ]
check "--path: prints the session file's own path"          out_has "$(session_dir_for "$TMP/repo-worktrees/card-42")/session-1.jsonl"

# 12. --path <n>: no session for that card — exits non-zero and says so on stderr, same as
# tail.sh's own "waiting for a session" case but for a one-shot caller with no polling.
fresh
CLAUDE_PROJECTS_DIR="$PROJECTS" python3 "$HERE/tail.py" --path 99 \
  --workdir-mode card --worktree-base "$TMP/repo-worktrees" \
  > "$TMP/out" 2> "$TMP/err"
echo $? > "$TMP/rc"
check "--path, no session: exits non-zero"                 [ "$(rc)" != 0 ]
check "--path, no session: says so on stderr"                err_has "no session transcript found"

echo
if [ "$fail" -eq 0 ]; then
  echo "tail: $n checks, all ok"
else
  echo "tail: $n checks, FAILURES ABOVE"
fi
exit "$fail"
