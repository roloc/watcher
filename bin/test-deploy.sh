#!/usr/bin/env bash
# The deployer's cases (deploy/autodeploy.sh), in test-spawn.sh's shape: no network, no
# root, no systemd. git is real (a bare repo stands in for GitHub, a clone for
# /opt/watcher); su, systemctl, gh, board and flock are stubs that log one line per call to
# $CALLS, so a case can grep what the deployer did. A lane is busy when its `current` file
# is non-empty — the deployer's fallback when a lane has no cgroup, which is every lane here.
#
#   bash bin/test-deploy.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEPLOY="$HERE/../deploy/autodeploy.sh"
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

# su - <user> -c <cmd>: run the command as ourselves.
cat > "$BIN/su" <<'EOF'
#!/usr/bin/env bash
[ "$1" = - ] && shift; shift; [ "$1" = -c ] && shift
exec bash -c "$1"
EOF
cat > "$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  list-units) while read -r l; do [ -n "$l" ] && echo "watcher@$l.service loaded active running Board watcher for $l"; done < "$TMP/active" ;;
  *) echo "systemctl $*" >> "$CALLS" ;;
esac
EOF
cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$CALLS"
case "$1 $2" in
  "api repos/"*) cat "$TMP/ci" 2>/dev/null ;;
  "issue create") echo "https://github.com/roloc/watcher/issues/$(cat "$TMP/next-issue" 2>/dev/null || echo 77)" ;;
  "issue view") cat "$TMP/labels" 2>/dev/null ;;
esac
EOF
cat > "$BIN/board" <<'EOF'
#!/usr/bin/env bash
echo "board $* owner=${BOARD_OWNER:-} number=${BOARD_NUMBER:-}" >> "$CALLS"
# $TMP/board-fails holds how many more moves fail (the board not caught up yet).
if [ -s "$TMP/board-fails" ]; then
  left="$(cat "$TMP/board-fails")"
  if [ "$left" -gt 0 ]; then echo $((left - 1)) > "$TMP/board-fails"; exit 1; fi
fi
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$BIN/flock"
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

# --- the fixture: origin, the box's checkout, the lanes ---------------------------------------

ORIGIN="$TMP/origin.git"; SEED="$TMP/seed"; BOX="$TMP/opt/watcher"
git init -q --bare "$ORIGIN"
git -C "$ORIGIN" symbolic-ref HEAD refs/heads/main
mkdir -p "$SEED"; git -C "$SEED" init -q -b main
git -C "$SEED" config user.email t@t; git -C "$SEED" config user.name t
mkdir -p "$SEED"/{projects,deploy,bin,commands}
cat > "$SEED/projects/watcher.env" <<'EOF'
PROJECT_NAME=watcher
PROJECT_REPO=roloc/watcher
BOARD_OWNER=roloc
BOARD_NUMBER=2
EOF
echo 'PROJECT_NAME=riffmap' > "$SEED/projects/riffmap.env"
echo 'PROJECT_NAME=riffmap-design' > "$SEED/projects/riffmap-design.env"
cat > "$SEED/deploy/install.sh" <<'EOF'
#!/usr/bin/env bash
echo "install $* NO_PULL=${INSTALL_NO_PULL:-0}" >> "$CALLS"
EOF
# The replay suite: logs the commit it ran at and the box's HEAD at that moment.
cat > "$SEED/bin/eval.sh" <<'EOF'
#!/usr/bin/env bash
echo "eval at $(git rev-parse HEAD) box $(git -C "$TMP/opt/watcher" rev-parse HEAD)" >> "$CALLS"
[ -f "$TMP/eval-fails" ] && { echo "  a case   FAIL — it closed the card"; exit 1; }
echo "  a case   ok"; exit 0
EOF
chmod +x "$SEED"/deploy/*.sh "$SEED"/bin/*.sh
echo "v1" > "$SEED/commands/watch.md"
git -C "$SEED" add -A; git -C "$SEED" commit -qm v1
git -C "$SEED" remote add origin "$ORIGIN"; git -C "$SEED" push -q origin main
mkdir -p "$(dirname "$BOX")"; git clone -q "$ORIGIN" "$BOX"
# A second, independent checkout (card #46) — cloned once here, like $BOX; a case that
# wants LANE_CHANNEL=canary in play resets it to a known commit itself, the same way
# `fresh` resets $BOX, since most cases have no canary lane at all and must never touch it.
CANARY="$TMP/opt/watcher-canary"
mkdir -p "$(dirname "$CANARY")"; git clone -q "$ORIGIN" "$CANARY"

STATE="$TMP/state"
export WATCHER_DIR="$BOX" WATCHER_STATE_ROOT="$STATE" DEPLOY_STATE="$TMP/dstate" \
       DEPLOY_SCRATCH="$TMP/scratch" DEPLOY_CGROUP_ROOT="$TMP/no-cgroups" DEPLOY_USER="$(id -un)" \
       DEPLOY_GRACE_SEC=0 DEPLOY_MOVE_RETRY_SEC=0 DEPLOY_DRAIN_AFTER_MIN=240 \
       DEPLOY_CANARY_DIR="$CANARY" DEPLOY_CANARY_SOAK_TICKS=2 DEPLOY_CANARY_QUIET_MIN=60

# commit <path> <text>: a new commit on origin/main touching one file; prints its sha.
commit() {
  mkdir -p "$SEED/$(dirname "$1")"; echo "$2" > "$SEED/$1"
  git -C "$SEED" add -A; git -C "$SEED" commit -qm "$1"; git -C "$SEED" push -q origin main
  git -C "$SEED" rev-parse HEAD
}
run()   { : > "$CALLS"; bash "$DEPLOY" > "$TMP/out" 2>&1; }
head_() { git -C "$BOX" rev-parse HEAD; }
fresh() {  # a clean slate between cases: box at origin/main, no state, idle lanes
  rm -rf "$TMP/dstate" "$STATE" "$TMP/eval-fails" "$TMP/labels" "$TMP/next-issue" "$TMP/board-fails"
  git -C "$BOX" fetch -q origin; git -C "$BOX" reset -q --hard origin/main
  printf 'riffmap\nriffmap-design\nwatcher\n' > "$TMP/active"
  echo "completed success" > "$TMP/ci"
  mkdir -p "$STATE"/{riffmap,riffmap-design,watcher}
}
called()     { grep -q -- "$1" "$CALLS"; }
not_called() { ! grep -q -- "$1" "$CALLS"; }
count()      { grep -c -- "$1" "$CALLS"; }
# Exact-line variants: "systemctl restart watcher@riffmap" is a SUBSTRING of "…@riffmap-design",
# so telling those two lanes apart (canary tests below) needs a full-line match, not called()'s.
called_exact()     { grep -qxF -- "$1" "$CALLS"; }
not_called_exact() { ! grep -qxF -- "$1" "$CALLS"; }

echo "autodeploy"

# 1. Nothing new
fresh
run
check "nothing new: no install, no restart"            not_called "install"
check "nothing new: says it is live"                   grep -q "live at" "$TMP/out"

# 2. CI still running
fresh; T="$(commit commands/watch.md v2)"
echo "in_progress " > "$TMP/ci"; run
check "CI pending: the box stays where it was"         [ "$(head_)" != "$T" ]
check "CI pending: the suite does not run yet"         not_called "eval at"
check "CI pending: no lane is paused"                  [ ! -f "$STATE/riffmap/PAUSE" ]

# 3. The happy path
fresh; OLD="$(head_)"; T="$(commit commands/watch.md v3)"
run
check "green + suite + idle: the box is at exactly the target" [ "$(head_)" = "$T" ]
check "the suite ran at the target commit"             called "eval at $T"
check "…before the box moved (box still at the old commit)" called "box $OLD"
check "install.sh ran without its own pull"            called "NO_PULL=1"
check "install.sh got each project once"               called "install riffmap watcher NO_PULL"
check "every running lane restarted"                   [ "$(count 'systemctl restart')" = 3 ]
check "no PAUSE left behind"                           [ ! -e "$STATE/riffmap/PAUSE" ] && [ ! -e "$STATE/watcher/PAUSE" ]
check "the deployed commit is recorded"                [ "$(cat "$TMP/dstate/deployed")" = "$T" ]

# 4. Steve's own PAUSE survives
fresh; : > "$STATE/riffmap-design/PAUSE"; T="$(commit commands/watch.md v4)"
run
check "a hand-made PAUSE is still there after a deploy" [ -f "$STATE/riffmap-design/PAUSE" ]
check "…and the deployer's own PAUSE files are gone"   [ ! -e "$STATE/riffmap/PAUSE" ]
check "…and it still deployed"                         [ "$(head_)" = "$T" ]

# 5. Busy lanes: wait, then drain after the limit
fresh; echo 12 > "$STATE/riffmap/current"; T="$(commit commands/watch.md v5)"
run
check "a busy lane: nothing is paused while it works"  [ ! -f "$STATE/watcher/PAUSE" ]
check "a busy lane: not deployed yet"                  [ "$(head_)" != "$T" ]
DEPLOY_DRAIN_AFTER_MIN=0 run
check "busy past the limit: lanes are paused to drain" grep -q "^deploy $T" "$STATE/watcher/PAUSE"
check "…but nothing restarts while the tick runs"      not_called "systemctl restart"
rm -f "$STATE/riffmap/current"; run
check "the tick finished: deployed on the next run"    [ "$(head_)" = "$T" ]
check "…and un-paused"                                 [ ! -e "$STATE/watcher/PAUSE" ]

# 6. The suite fails twice: one held card, in Stuck, once
fresh; touch "$TMP/eval-fails"; T="$(commit commands/watch.md v6)"
run
check "first fail: retried later, no card yet"         not_called "issue create"
run
check "second fail: one card filed"                    [ "$(count 'gh issue create')" = 1 ]
check "…in the talking-to-steve shape (the ask first)" called "Deploy held: ${T:0:7} is not live"
check "…put on the watcher board"                      called "gh project item-add 2 --owner roloc"
check "…and moved to Stuck"                            called "board move 77 Stuck - Needs help owner=roloc number=2"
check "…and the box did not move"                      [ "$(head_)" != "$T" ]
run
check "a third run files no second card"               not_called "issue create"

# 7. deploy-anyway ships a held commit
echo deploy-anyway > "$TMP/labels"; run
check "deploy-anyway: the held commit ships"           [ "$(head_)" = "$T" ]

# 8. Nothing a model reads changed: no suite
fresh; touch "$TMP/eval-fails"; T="$(commit README.md docs-only)"
run
check "a README-only commit skips the suite"           not_called "eval at"
check "…and deploys"                                   [ "$(head_)" = "$T" ]

# 9. Red check
fresh; T="$(commit commands/watch.md v9)"; echo "completed failure" > "$TMP/ci"
run
check "a red check: held, with one card"               [ "$(count 'gh issue create')" = 1 ]
check "…and the suite never ran"                       not_called "eval at"

# 10. main moves during a drain: the drained commit ships, not the newer one
fresh; echo 12 > "$STATE/riffmap/current"; T="$(commit commands/watch.md v10)"
DEPLOY_DRAIN_AFTER_MIN=0 run
N="$(commit commands/watch.md v10b)"; rm -f "$STATE/riffmap/current"; run
check "a newer commit mid-drain: the checked one ships" [ "$(head_)" = "$T" ]
run; run
check "…and the newer one follows on its own"          [ "$(head_)" = "$N" ]

# 11. A lane this commit adds starts last
fresh; T="$(commit projects/riffmap-triage.env PROJECT_NAME=riffmap-triage)"
run
check "a new lane file: its unit is enabled and started" called "systemctl enable --now watcher@riffmap-triage"
check "…after every running lane restarted"            [ "$(grep -n 'enable --now' "$CALLS" | cut -d: -f1)" -gt "$(grep -n 'systemctl restart' "$CALLS" | tail -1 | cut -d: -f1)" ]

# 11a. Only projects/<name>.env is a lane: a project's helper files are never started
fresh; commit projects/riffmap-health-probe.sh 'echo 0' >/dev/null
T="$(commit projects/riffmap.detect.env DETECT_WHAT=x)"
run
check "a helper script under projects/ is not started as a lane" not_called "watcher@riffmap-health-probe"
check "…nor a <name>.detect.env config"                not_called "watcher@riffmap.detect"
check "…and the deploy still lands"                    [ "$(head_)" = "$T" ]

# 11b. The drain clock counts from when the box fell behind, not from the newest commit
fresh; echo 12 > "$STATE/riffmap/current"; T="$(commit commands/watch.md v11b)"
DEPLOY_DRAIN_AFTER_MIN=60 run
echo "$(head_) $(( $(date +%s) - 3 * 3600 ))" > "$TMP/dstate/ready"   # behind for 3 hours
N="$(commit commands/watch.md v11c)"
DEPLOY_DRAIN_AFTER_MIN=60 run
check "busy for hours with new commits arriving: the drain still starts" grep -q "^deploy $N" "$STATE/watcher/PAUSE"

# 11c. A lane whose spawner still holds a card is busy, even with no tick process
fresh; T="$(commit commands/watch.md v11d)"; echo 7 > "$STATE/watcher/current"
run
check "a lane still finishing its exit handling blocks the swap" not_called "systemctl restart"

# 11d. A swap that cannot happen un-pauses the lanes, and the next run holds with a card
fresh; echo 12 > "$STATE/riffmap/current"; T="$(commit commands/watch.md v11e)"
DEPLOY_DRAIN_AFTER_MIN=0 run
echo local-edit >> "$BOX/commands/watch.md"; rm -f "$STATE/riffmap/current"; run
check "a failed fast-forward: the box is not moved"   [ "$(head_)" != "$T" ]
check "…the deployer's PAUSE files are gone"           [ ! -e "$STATE/watcher/PAUSE" ]
check "…and the drain is over"                         [ ! -e "$TMP/dstate/draining" ]
run
check "…and the next run holds it with a card"         [ "$(count 'gh issue create')" = 1 ]

# 12. A dirty checkout is never overwritten without Steve
fresh; T="$(commit commands/watch.md v12)"; echo local-edit >> "$BOX/commands/watch.md"
run
check "a dirty box checkout: held, not merged"         [ "$(head_)" != "$T" ]
check "…with one card (the reason has an apostrophe)"  [ "$(count 'gh issue create')" = 1 ]
echo deploy-anyway > "$TMP/labels"; run
check "deploy-anyway on it: reset to the target"       [ "$(head_)" = "$T" ]

# 12b. A file's executable bit changed on the box (install.sh chmods bin/) is not a change
fresh; T="$(commit commands/watch.md v12b)"; chmod +x "$BOX/commands/watch.md"
run
check "a mode-only change on the box does not hold the deploy" [ "$(head_)" = "$T" ]
check "…and files no card"                             not_called "issue create"

# 12c. The move to Stuck is retried when the board has not caught up with the new item
fresh; touch "$TMP/eval-fails"; echo 2 > "$TMP/board-fails"; T="$(commit commands/watch.md v12c)"
run; run
check "a held card still reaches Stuck when the first moves fail" [ "$(count 'board move 77 Stuck')" = 3 ]
rm -f "$TMP/board-fails"

# 13. A deploy closes the older held cards
fresh; touch "$TMP/eval-fails"; H="$(commit commands/watch.md v13)"; run; run
rm -f "$TMP/eval-fails"; T="$(commit commands/watch.md v13b)"; run
check "a later commit deploys past a held one"         [ "$(head_)" = "$T" ]
check "…and closes the held card as superseded"        called "gh issue close 77"

# ============================================================================
echo "Canary channel (card #46): a lane whose project file sets LANE_CHANNEL=canary"
echo "takes a commit first, on a second checkout, before it reaches every other lane"
# ============================================================================
# From here on, watcher.env carries LANE_CHANNEL=canary — the fixture equivalent of this
# card's own projects/watcher.env. $CANARY (cloned once, above) is this section's own
# checkout; every case resets it the same way `fresh` resets $BOX.
cat > "$SEED/projects/watcher.env" <<'EOF'
PROJECT_NAME=watcher
PROJECT_REPO=roloc/watcher
BOARD_OWNER=roloc
BOARD_NUMBER=2
LANE_CHANNEL=canary
EOF
git -C "$SEED" add -A; git -C "$SEED" commit -qm "watcher.env: LANE_CHANNEL=canary"; git -C "$SEED" push -q origin main

canary_fresh() { fresh; git -C "$CANARY" fetch -q origin; git -C "$CANARY" reset -q --hard origin/main; }
canary_head()  { git -C "$CANARY" rev-parse HEAD; }
# canary_tick_log <subtype> — a result json for the watcher lane, dated now (after whatever
# the most recent `run` set the soak's clock to).
canary_tick_log() {
  mkdir -p "$STATE/watcher/logs"
  echo "{\"subtype\":\"$1\"}" > "$STATE/watcher/logs/card-$RANDOM-$RANDOM.json"
}

# A. A canary deploy restarts only the canary lane(s)
canary_fresh; OLD="$(canary_head)"; T="$(commit commands/watch.md canary-a)"
run
check "canary A: the suite ran before anything moved"     called "eval at $T box $OLD"
check "canary A: the canary checkout reaches the target"  [ "$(canary_head)" = "$T" ]
check "canary A: the canary lane restarts"                called_exact "systemctl restart watcher@watcher"
check "canary A: riffmap's lane does not"                 not_called_exact "systemctl restart watcher@riffmap"
check "canary A: riffmap-design's lane does not"          not_called_exact "systemctl restart watcher@riffmap-design"
check "canary A: the stable checkout has not moved yet"   [ "$(head_)" != "$T" ]

# B. A clean soak (DEPLOY_CANARY_SOAK_TICKS=2 in this fixture) promotes the rest
canary_fresh; T="$(commit commands/watch.md canary-b)"
run   # the canary swap; starts the soak clock
canary_tick_log success; canary_tick_log success
run   # sees 2 clean ticks, promotes
check "canary B: a clean soak promotes the stable checkout too" [ "$(head_)" = "$T" ]
check "canary B: the other lanes restart on promotion"          called "systemctl restart watcher@riffmap"

# C. A canary tick that dies holds the commit and rolls the canary back
canary_fresh; OLD="$(canary_head)"; T="$(commit commands/watch.md canary-c)"
run   # the canary swap
canary_tick_log error_during_execution
run   # sees the dead tick
check "canary C: the canary checkout rolls back"        [ "$(canary_head)" = "$OLD" ]
check "canary C: the commit is held, not promoted"      [ "$(head_)" != "$T" ]
check "canary C: one card filed (talking-to-steve shape)" called "Deploy held: ${T:0:7} is not live"
check "canary C: …and moved to Stuck"                   called "board move 77 Stuck - Needs help owner=roloc number=2"

# D. A quiet canary (DEPLOY_CANARY_QUIET_MIN=60) promotes on time, with no ticks at all
canary_fresh; T="$(commit commands/watch.md canary-d)"
run   # the canary swap; no ticks logged
echo "$T $(( $(date +%s) - 61 * 60 ))" > "$TMP/dstate/canary-soak"   # quiet for 61 min > 60
run
check "canary D: a quiet canary promotes on time" [ "$(head_)" = "$T" ]

# E. A commit that skips the suite (nothing a model reads changed) promotes straight away,
# canary lane configured or not — canary is additive, never a new gate of its own.
canary_fresh; OLD_CANARY="$(canary_head)"; T="$(commit README.md canary-e)"
run
check "canary E: a docs-only commit still skips the suite" not_called "eval at"
check "canary E: …and promotes directly"                   [ "$(head_)" = "$T" ]
check "canary E: …never touching the canary checkout"       [ "$(canary_head)" = "$OLD_CANARY" ]

# F. A tick still in progress (its result json exists but is still empty — bin/spawn.sh
# creates it by redirection at launch, before the tick has written anything) is never read
# as died: only a lane with NO tick running is judged on its log files at all.
canary_fresh; T="$(commit commands/watch.md canary-f)"
run   # the canary swap
mkdir -p "$STATE/watcher/logs"; : > "$STATE/watcher/logs/still-running.json"
echo 950 > "$STATE/watcher/current"   # tick_running("watcher") now true
run
check "canary F: an in-progress tick is not judged"       [ "$(canary_head)" = "$T" ]
check "canary F: …the commit is not held"                 not_called "issue create"
rm -f "$STATE/watcher/current"

# G. A commit already rolled back once is never retried on its own — the flip-flop a died
# soak used to cause, restarting the canary lane onto the same bad commit every run.
canary_fresh; OLD="$(canary_head)"; T="$(commit commands/watch.md canary-g)"
run   # the canary swap
canary_tick_log error_during_execution
run   # dies, rolls back, files the one card
run; run   # two more deploy ticks — `run` resets $CALLS each time, so these check that
           # NEITHER later tick repeats the swap or files a second card, not a cumulative count
check "canary G: the canary checkout stays rolled back"   [ "$(canary_head)" = "$OLD" ]
check "canary G: a later tick never re-swaps onto it"     not_called "systemctl restart"
check "canary G: …or files a second card"                 not_called "gh issue create"

# H. A canary lane with a tick in flight is paused and waited on, never restarted out from
# under it — restarting `watcher@<lane>` mid-tick kills the spawner's own exit handling.
canary_fresh; OLD="$(canary_head)"; T="$(commit commands/watch.md canary-h)"
echo 960 > "$STATE/watcher/current"
run
check "canary H: a busy canary lane is not moved onto the commit yet" [ "$(canary_head)" = "$OLD" ]
check "canary H: …and is not restarted"                                not_called_exact "systemctl restart watcher@watcher"
check "canary H: …but is paused so it takes no new work meanwhile"    grep -q "^deploy $T" "$STATE/watcher/PAUSE"
rm -f "$STATE/watcher/current"
run
check "canary H: once idle, the swap and restart go ahead"            [ "$(canary_head)" = "$T" ]
check "canary H: …and the pause is lifted"                             [ ! -e "$STATE/watcher/PAUSE" ]

echo
if [ "$fail" = 0 ]; then echo "$n cases, all passed"; else echo "FAILED (of $n cases)"; fi
exit "$fail"
