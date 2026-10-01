#!/usr/bin/env bash
# bin/detect.sh's cases, in test-deploy.sh's shape: no network, no systemd, `gh` and
# `board` are stubs that log one line per call to $CALLS so a case can grep what the
# detector did. The metric itself is a one-line file under $TMP a case sets before each
# run, standing in for card #36's "a stubbed metric source crosses each band".
#
#   bash bin/test-detect.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DETECT="$HERE/detect.sh"
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

# gh: `issue create` prints a URL numbered from $TMP/next-issue (default 501) and saves
# the --body-file content to $TMP/last-body; `issue view ... --json state -q .state`
# prints $TMP/issue-state, or fails if that file is absent (a lookup a case wants to fail).
cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$CALLS"
case "$1 $2" in
  "issue create")
    args=("$@")
    for ((i=0; i<${#args[@]}; i++)); do
      [ "${args[$i]}" = "--body-file" ] && cp "${args[$i+1]}" "$TMP/last-body" 2>/dev/null
    done
    echo "https://github.com/roloc/testproj/issues/$(cat "$TMP/next-issue" 2>/dev/null || echo 501)"
    ;;
  "issue view")
    [ -f "$TMP/issue-state" ] || exit 1
    cat "$TMP/issue-state"
    ;;
  "project item-add") [ ! -f "$TMP/item-add-fails" ] ;;
esac
EOF
cat > "$BIN/board" <<'EOF'
#!/usr/bin/env bash
echo "board $*" >> "$CALLS"
[ "$1" = move ] && [ -f "$TMP/board-move-fails" ] && exit 1
exit 0
EOF
# timeout: detect.sh caps the metric command with coreutils' `timeout`, which a stock Mac
# does not ship — there every reading came back empty and the suite failed 14 cases before
# it tested anything. Only a host with none gets this stand-in, so Linux (CI, the box)
# still runs the real one; it keeps the limit and GNU's exit status 124 rather than
# dropping them, so a case about a hung metric would mean the same thing on both.
if ! command -v timeout >/dev/null 2>&1; then
  cat > "$BIN/timeout" <<'EOF'
#!/usr/bin/env bash
exec python3 -c '
import subprocess, sys
try:
    sys.exit(subprocess.run(sys.argv[2:], timeout=float(sys.argv[1])).returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
' "$@"
EOF
fi
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

WDIR="$TMP/opt"; STATE="$TMP/state"
mkdir -p "$WDIR/projects"
export WATCHER_DIR="$WDIR" WATCHER_STATE_ROOT="$STATE"

cat > "$WDIR/projects/testproj.env" <<'EOF'
PROJECT_NAME=testproj
PROJECT_REPO=roloc/testproj
BOARD_OWNER=roloc
BOARD_NUMBER=9
EOF

# conf <window> <min-readings> <small> <large> [look-url]: writes the detect.env, reading
# the metric from $TMP/metric-value (a case sets it with `metric`).
conf() {
  {
    echo "DETECT_WHAT=\"the test metric\""
    echo "DETECT_METRIC_CMD=\"cat $TMP/metric-value\""
    echo "DETECT_BASELINE_WINDOW=${1:-5}"
    echo "DETECT_MIN_READINGS=${2:-1}"
    echo "DETECT_SMALL_BAND=${3:-0.3}"
    echo "DETECT_LARGE_BAND=${4:-0.6}"
    [ -n "${5:-}" ] && echo "DETECT_LOOK_URL=\"$5\""
  } > "$WDIR/projects/testproj.detect.env"
}
no_conf()  { rm -f "$WDIR/projects/testproj.detect.env"; }
metric()   { echo "$1" > "$TMP/metric-value"; }
next_issue() { echo "$1" > "$TMP/next-issue"; }
issue_state() { echo "$1" > "$TMP/issue-state"; }

run() { : > "$CALLS"; bash "$DETECT" testproj > "$TMP/out" 2>&1; echo $? > "$TMP/rc"; }
rc()  { cat "$TMP/rc"; }
out_has()   { grep -q -- "$1" "$TMP/out"; }
out_lacks() { ! grep -q -- "$1" "$TMP/out"; }
called()     { grep -q -- "$1" "$CALLS"; }
not_called() { ! grep -q -- "$1" "$CALLS"; }
count()      { grep -c -- "$1" "$CALLS"; }
# discord_after_move <n>: the breach post for card <n> was made, and only after its move
# into Backlog — Discord repeats a card that landed, it never runs ahead of the board.
discord_after_move() {
  local move post
  move="$(grep -n -- "board move $1 Backlog" "$CALLS" | head -1 | cut -d: -f1)"
  post="$(grep -n -- "board notify $1 breach" "$CALLS" | head -1 | cut -d: -f1)"
  [ -n "$move" ] && [ -n "$post" ] && [ "$post" -gt "$move" ]
}
fresh() {
  rm -rf "$STATE" "$TMP/issue-state" "$TMP/next-issue" "$TMP/item-add-fails" "$TMP/last-body" \
         "$TMP/board-move-fails"
  mkdir -p "$STATE"
  no_conf
}

echo "detect.sh"

# 1. No projects/<p>.detect.env: exits quietly, touches nothing
fresh
run
check "no config: exits 0"                             [ "$(rc)" = 0 ]
check "no config: no gh calls at all"                   not_called "gh"
check "no config: no state directory created"           [ ! -d "$STATE/testproj/detect" ]

# 2. Warming up: fewer readings than DETECT_MIN_READINGS
fresh; conf 5 3 0.3 0.6; metric 1
run
check "warming up: says so"                             out_has "warming up"
check "warming up: no card even though the value is bad" not_called "issue create"

# 3. The metric command produces no number
fresh; conf 5 1 0.3 0.6; metric "banana"
run
check "non-numeric metric: skips the run"               out_has "no number"
check "non-numeric metric: no card"                     not_called "issue"
check "non-numeric metric: exits 0, not an error"        [ "$(rc)" = 0 ]

# 4. Small breach: the rolling average crosses the small band, not the large one
fresh; conf 5 1 0.3 0.6; metric 0.4
run
check "small breach: logs a line"                       out_has "SMALL BREACH"
check "small breach: nothing else — no card"            not_called "issue create"
check "small breach: not even added to the board"        not_called "project item-add"
check "small breach: nothing to Discord"                 not_called "board notify"

# 5. Large breach: files one card, on the board, in Backlog
fresh; conf 5 1 0.3 0.6 "https://example.test/look"; metric 1; next_issue 701
run
check "large breach: logs it"                           out_has "LARGE BREACH"
check "large breach: files a card"                      called "gh issue create -R roloc/testproj"
check "large breach: title names the metric and value"  called "the test metric: 1"
check "large breach: put on the right board"            called "gh project item-add 9 --owner roloc"
check "large breach: moved into Backlog"                 called "board move 701 Backlog"
check "large breach: told to Discord, after the card landed" discord_after_move 701
check "large breach: the Discord line has the reading and the Look url" called "board notify 701 breach the test metric: now 1, averaging .* Look: https://example.test/look"
check "large breach: the card names the Look url"       grep -q "https://example.test/look" "$TMP/last-body"

# 5b. The board move fails after the card is filed: says so, does not lie about Backlog
fresh; conf 5 1 0.3 0.6; metric 1; next_issue 801; touch "$TMP/board-move-fails"
run
check "board move fails: the card still exists"         called "gh issue create -R roloc/testproj"
check "board move fails: does not claim it landed"      out_lacks "filed .* in Backlog"
check "board move fails: says to check by hand"         out_has "could not put it on the board"
check "board move fails: nothing to Discord (the card did not land)" not_called "board notify"

# 6. Still breaching, same card still open: no second card
fresh; conf 5 1 0.3 0.6 "https://example.test/look"; metric 1; next_issue 701; run
issue_state OPEN
run
check "still open: no second card"                      [ "$(count 'issue create')" = 0 ]
check "still open: says so"                              out_has "already filed #701"

# 7. The filed card closed; breaching again: a fresh card
issue_state CLOSED; next_issue 702
run
check "old card closed: a new one is filed"              called "gh issue create -R roloc/testproj"
check "old card closed: it is the next number"           called "board move 702 Backlog"

# 8. A `gh issue view` lookup failure fails closed — never risks a duplicate
rm -f "$TMP/issue-state"; run
check "lookup failure: no new card is risked"            not_called "issue create"
check "lookup failure: says why"                          out_has "could not check #702"

# 9. Back under the bands: quiet, no breach line
fresh; conf 5 1 0.3 0.6; metric 0
run
check "healthy: no breach reported"                      out_lacks "BREACH"
check "healthy: no card"                                  not_called "issue"

echo
echo "riffmap-health-probe.sh"

# riffmap's own metric script (projects/riffmap.detect.env's DETECT_METRIC_CMD): stub
# `curl` to hand back one status code and check the probe's verdict on it (roloc/watcher#36,
# Steve, September 28, 2026 — a probe of the bare IP was answered by a different app on the
# same box, so the fix probes RiffMap by name and treats its own 401-to-anonymous as up).
PROBE="$HERE/../projects/riffmap-health-probe.sh"
probe_curl() {
  cat > "$BIN/curl" <<CURLEOF
#!/usr/bin/env bash
echo "curl \$*" >> "$CALLS"
echo -n "$1"
CURLEOF
  chmod +x "$BIN/curl"
}
probe() { : > "$CALLS"; bash "$PROBE"; }

probe_curl 200
check "200: healthy"                                     [ "$(probe)" = 0 ]

probe_curl 401
check "401 (anonymous, app is up): healthy, not just 2xx/3xx" [ "$(probe)" = 0 ]
check "still probes RiffMap by its own hostname"          called "riffmap.lonestarprod.com"
check "pinned to lonestar-web's LAN address"              called "192.168.176.37"

probe_curl 500
check "500 (app unreachable behind the proxy): unhealthy" [ "$(probe)" = 1 ]

probe_curl 000
check "000 (timeout / no connection): unhealthy"          [ "$(probe)" = 1 ]
rm -f "$BIN/curl"   # a case added below must not inherit this stub or its wiped $CALLS

echo
if [ "$fail" = 0 ]; then echo "$n cases, all passed"; else echo "FAILED (of $n cases)"; fi
exit "$fail"
