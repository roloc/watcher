#!/usr/bin/env bash
# The deployer's cases (deploy/autodeploy.sh), in test-spawn.sh's shape: no network, no
# root, no systemd. git is real (a bare repo stands in for GitHub, a clone for the box's
# own checkout); su, systemctl, gh, board and flock are stubs that log one line per call to
# $CALLS, so a case can grep what the deployer did. A lane is busy when its `current` file
# is non-empty — the deployer's fallback when a lane has no cgroup, which is every lane here.
#
# Card #106: the box's checkout ($BOX) becomes a pointer (a symlink) onto one of its own
# release folders (under $RELEASES) the first time a deploy runs against it — `fresh`
# recreates $BOX as a plain checkout before every case, the same shape every real box is in
# today, so that migration (adopt_plain_checkout) is exercised on every single case here,
# not just once. A deploy never pauses or restarts a stable lane any more: it clones the
# target, installs from that clone, and flips the pointer. Only the canary stage (still a
# separate, unchanged mechanism — section "Canary channel" below) pauses and restarts.
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

ORIGIN="$TMP/origin.git"; SEED="$TMP/seed"; BOX="$TMP/opt/watcher"; RELEASES="$TMP/opt/watcher-releases"
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
# "pointer=<target>" is what /opt/watcher itself resolves to AT THIS MOMENT — install.sh's
# own unconditional floor-service restart depends on the pointer already being flipped by
# the time this runs (found in review: it used to run before the flip, so the floor page
# served one release behind every deploy).
echo "install $* NO_PULL=${INSTALL_NO_PULL:-0} DIR=${WATCHER_DIR:-} pointer=$(basename "$(readlink -f "$TMP/opt/watcher" 2>/dev/null)")" >> "$CALLS"
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
# A second, independent checkout (card #46) — cloned once here; a case that wants
# LANE_CHANNEL=canary in play resets it to a known commit itself, the same way `fresh`
# resets $BOX, since most cases have no canary lane at all and must never touch it.
CANARY="$TMP/opt/watcher-canary"
mkdir -p "$(dirname "$CANARY")"; git clone -q "$ORIGIN" "$CANARY"

STATE="$TMP/state"
export WATCHER_DIR="$BOX" DEPLOY_RELEASES_DIR="$RELEASES" WATCHER_STATE_ROOT="$STATE" \
       DEPLOY_STATE="$TMP/dstate" DEPLOY_SCRATCH="$TMP/scratch" \
       DEPLOY_CGROUP_ROOT="$TMP/no-cgroups" DEPLOY_USER="$(id -un)" DEPLOY_MOVE_RETRY_SEC=0 \
       DEPLOY_CANARY_DIR="$CANARY" DEPLOY_CANARY_SOAK_TICKS=2 DEPLOY_CANARY_QUIET_MIN=60

# commit <path> <text>: a new commit on origin/main touching one file; prints its sha.
commit() {
  mkdir -p "$SEED/$(dirname "$1")"; echo "$2" > "$SEED/$1"
  git -C "$SEED" add -A; git -C "$SEED" commit -qm "$1"; git -C "$SEED" push -q origin main
  git -C "$SEED" rev-parse HEAD
}
run()   { : > "$CALLS"; bash "$DEPLOY" > "$TMP/out" 2>&1; }
head_() { git -C "$BOX" rev-parse HEAD; }
fresh() {  # a clean slate between cases: the box a plain checkout at origin/main — as if
           # freshly installed, never yet deployed by this card's own code — no releases,
           # no state, idle lanes.
  rm -rf "$TMP/dstate" "$STATE" "$TMP/eval-fails" "$TMP/labels" "$TMP/next-issue" "$TMP/board-fails" \
         "$RELEASES" "$BOX"
  git clone -q "$ORIGIN" "$BOX"
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
# What the deployer said on its last run, for the cases where what it decided leaves no call.
out_lacks() { ! grep -q -- "$1" "$TMP/out"; }

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

# 2a. Card #105: the step file is the floor page's only source for "which step" — written
# while CI is still the thing blocking this target, with the total steps a plain 4 (no
# canary lane configured in this fixture yet).
check "CI pending: the step file says step 2 of 4"     [ "$(sed -n '1p;2p' "$TMP/dstate/step")" = "$(printf '2\n4')" ]
check "...named in plain words"                        [ "$(sed -n 3p "$TMP/dstate/step")" = "Wait for CI to pass" ]

# 3. The happy path: a fresh release folder, installed, then the pointer flips. No lane is
# ever paused or restarted — each follows the pointer on its own, later, out of this
# script's sight (bin/spawn.sh's own job; see its test suite).
fresh; OLD="$(head_)"; T="$(commit commands/watch.md v3)"
run
check "green + suite: the pointer now resolves to exactly the target" [ "$(head_)" = "$T" ]
check "...as a symlink onto its own release folder"    [ -L "$BOX" ]
check "...named by the target commit"                  [ "$(basename "$(readlink "$BOX")")" = "${T:0:12}" ]
check "the suite ran at the target commit"             called "eval at $T"
check "…before the pointer moved (box still at the old commit)" called "box $OLD"
check "install.sh ran without its own pull"            called "NO_PULL=1"
check "...from the release folder, not the old pointer" called "DIR=$RELEASES/${T:0:12}"
check "the pointer is already flipped BEFORE install.sh runs (the floor page must see the new code when its restart happens inside install.sh)" \
      called "pointer=${T:0:12}"
check "install.sh got each project once"               called "install riffmap watcher NO_PULL"
check "no lane is ever restarted by an ordinary deploy" not_called "systemctl restart"
check "no PAUSE is ever written by an ordinary deploy"  [ ! -e "$STATE/riffmap/PAUSE" ] && [ ! -e "$STATE/watcher/PAUSE" ]
check "the deployed commit is recorded"                [ "$(cat "$TMP/dstate/deployed")" = "$T" ]
check "a deploy that landed clears the step file"      [ ! -e "$TMP/dstate/step" ]

# 4. Steve's own PAUSE is never touched by a deploy — nothing here writes or removes a
# PAUSE file for a stable lane any more, hand-made or not.
fresh; : > "$STATE/riffmap-design/PAUSE"; T="$(commit commands/watch.md v4)"
run
check "a hand-made PAUSE is still there after a deploy" [ -f "$STATE/riffmap-design/PAUSE" ]
check "…and it still deployed"                         [ "$(head_)" = "$T" ]

# 5. A busy lane never blocks, waits for, or is touched by a deploy: its folder is pinned
# for the whole life of its own tick (bin/spawn.sh's own comment on this), so the deployer
# does not need to know or care that it is busy at all.
fresh; echo 12 > "$STATE/riffmap/current"; T="$(commit commands/watch.md v5)"
run
check "a busy lane: the deploy lands anyway, with no pause"   [ "$(head_)" = "$T" ]
check "a busy lane: nothing is paused"                        [ ! -f "$STATE/riffmap/PAUSE" ]
check "a busy lane: its current file is left exactly as it was" [ "$(cat "$STATE/riffmap/current")" = 12 ]
check "a busy lane: it is never restarted"                    not_called_exact "systemctl restart watcher@riffmap"

# 6. The suite fails twice: one held card, in Stuck, once
fresh; touch "$TMP/eval-fails"; T="$(commit commands/watch.md v6)"
run
check "first fail: retried later, no card yet"         not_called "issue create"
check "first fail: the step file says step 3 (the replay suite)" [ "$(sed -n 1p "$TMP/dstate/step")" = 3 ]
run
check "second fail: one card filed"                    [ "$(count 'gh issue create')" = 1 ]
check "…in the talking-to-steve shape (the ask first)" called "Deploy held: ${T:0:7} is not live"
check "…put on the watcher board"                      called "gh project item-add 2 --owner roloc"
check "…and moved to Stuck"                            called "board move 77 Stuck - Needs help owner=roloc number=2"
check "…and the box did not move"                      [ "$(head_)" != "$T" ]
check "held: the step file is cleared, not left on the failed step" [ ! -e "$TMP/dstate/step" ]
run
check "a third run files no second card"               not_called "issue create"

# 7. deploy-anyway ships a held commit
echo deploy-anyway > "$TMP/labels"; run
check "deploy-anyway: the held commit ships"           [ "$(head_)" = "$T" ]
check "deploy-anyway: the step file is cleared once it lands" [ ! -e "$TMP/dstate/step" ]

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
check "…and the step file is cleared, not left on step 2" [ ! -e "$TMP/dstate/step" ]

# 10. A lane this commit adds starts right away — never waiting for a sibling lane, which
# (unlike before this card) may still be mid-tick on an older commit for a while yet.
fresh; T="$(commit projects/riffmap-triage.env PROJECT_NAME=riffmap-triage)"
run
check "a new lane file: its unit is enabled and started" called "systemctl enable --now watcher@riffmap-triage"
check "…and no existing lane is restarted for it"       not_called_exact "systemctl restart watcher@riffmap"

# 10a. Only projects/<name>.env is a lane: a project's helper files are never started
fresh; commit projects/riffmap-health-probe.sh 'echo 0' >/dev/null
T="$(commit projects/riffmap.detect.env DETECT_WHAT=x)"
run
check "a helper script under projects/ is not started as a lane" not_called "watcher@riffmap-health-probe"
check "…nor a <name>.detect.env config"                not_called "watcher@riffmap.detect"
check "…and the deploy still lands"                    [ "$(head_)" = "$T" ]

# 10b. The move to Stuck is retried when the board has not caught up with the new item
fresh; touch "$TMP/eval-fails"; echo 2 > "$TMP/board-fails"; T="$(commit commands/watch.md v10b)"
run; run
check "a held card still reaches Stuck when the first moves fail" [ "$(count 'board move 77 Stuck')" = 3 ]
rm -f "$TMP/board-fails"

# 11. A deploy closes the older held cards
fresh; touch "$TMP/eval-fails"; H="$(commit commands/watch.md v11)"; run; run
rm -f "$TMP/eval-fails"; T="$(commit commands/watch.md v11b)"; run
check "a later commit deploys past a held one"         [ "$(head_)" = "$T" ]
check "…and closes the held card as superseded"        called "gh issue close 77"

# 12. The sweep keeps the newest two release folders and nothing a lane still names
fresh
A="$(commit commands/watch.md v12a)"; run
echo "${A:0:12}" > "$STATE/watcher/release"   # a lane pins itself to the first release
B="$(commit commands/watch.md v12b)"; run
C="$(commit commands/watch.md v12c)"; run
check "the sweep keeps the newest release"             [ -d "$RELEASES/${C:0:12}" ]
check "...and the one before it"                       [ -d "$RELEASES/${B:0:12}" ]
check "...and a release a lane still names, however old" [ -d "$RELEASES/${A:0:12}" ]
rm -f "$STATE/watcher/release"
D="$(commit commands/watch.md v12d)"; run
check "once no lane names it any more, an old release is swept" [ ! -d "$RELEASES/${A:0:12}" ]
check "...but the newest two always survive"           [ -d "$RELEASES/${C:0:12}" ] && [ -d "$RELEASES/${D:0:12}" ]

# 12e. A lane's own state dir can outlive the lane (a disabled/removed unit never cleans up
# after itself) — its stale release/ file must not pin an old release against the sweep
# forever, only an ACTIVE lane's own file counts.
fresh
mkdir -p "$STATE/retired-lane"
E="$(commit commands/watch.md v12e)"; run
echo "${E:0:12}" > "$STATE/retired-lane/release"   # "retired-lane" is not in $TMP/active
F="$(commit commands/watch.md v12f)"; run
G="$(commit commands/watch.md v12g)"; run
check "a release only an INACTIVE lane's state still names is swept anyway" [ ! -d "$RELEASES/${E:0:12}" ]

# 12f. A $WATCHER_DIR this script could not adopt (the rename itself fails) is never
# silently nested into — the deploy holds instead of reporting success on a box that never
# moved. $RELEASES is made FIRST, and still exists (and so still needs no write on its own
# now-read-only parent to confirm, `mkdir -p` on an existing dir is a no-op) when
# $(dirname "$BOX") goes read-only — so the failure lands on the guarded `mv` inside
# adopt_plain_checkout specifically, not on an earlier, unguarded `mkdir -p` under `set -e`
# (that would crash the whole run instead of holding it, and prove nothing about the guard
# this case means to exercise — caught in review round 2).
fresh
mkdir -p "$RELEASES"
chmod 555 "$(dirname "$BOX")"
H="$(commit commands/watch.md v12h)"
run
chmod 755 "$(dirname "$BOX")"
check "a checkout the deployer cannot adopt is never silently nested into" \
      out_lacks "now points at"
check "...and the box genuinely did not move" [ "$(head_)" != "$H" ]

# ============================================================================
echo "Canary channel (card #46): a lane whose project file sets LANE_CHANNEL=canary"
echo "takes a commit first, on a second checkout, before it reaches every other lane"
# ============================================================================
# From here on, watcher.env carries LANE_CHANNEL=canary — the fixture equivalent of this
# card's own projects/watcher.env. $CANARY (cloned once, above) is this section's own
# checkout; every case resets it the same way `fresh` resets $BOX. Unchanged by card #106:
# the canary stage still pauses and restarts only its own lane(s), directly.
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
check "canary A: the step file says step 4 of 5 (canary applies, so the total is 5)" \
      [ "$(sed -n '1p;2p' "$TMP/dstate/step")" = "$(printf '4\n5')" ]
check "canary A: ...soaking, zero clean ticks so far"      [ "$(sed -n 4p "$TMP/dstate/step")" = "soaking: 0 of 2 clean tick(s) so far" ]

# B. A clean soak (DEPLOY_CANARY_SOAK_TICKS=2 in this fixture) promotes the rest — onto the
# ordinary release path, which (card #106) never restarts a stable lane either.
canary_fresh; T="$(commit commands/watch.md canary-b)"
run   # the canary swap; starts the soak clock
canary_tick_log success; canary_tick_log success
run   # sees 2 clean ticks, promotes
check "canary B: a clean soak promotes the stable checkout too" [ "$(head_)" = "$T" ]
check "canary B: the other lanes are never restarted for it"    not_called_exact "systemctl restart watcher@riffmap"

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
check "canary H: the step file says it's waiting for the canary lane(s) to go idle" \
      [ "$(sed -n 4p "$TMP/dstate/step")" = "waiting for the canary lane(s) to go idle before moving to ${T:0:7}" ]
rm -f "$STATE/watcher/current"
run
check "canary H: once idle, the swap and restart go ahead"            [ "$(canary_head)" = "$T" ]
check "canary H: …and the pause is lifted"                             [ ! -e "$STATE/watcher/PAUSE" ]

# I. A card the spawner parked during the soak (two attempts, no progress) holds the commit
# like a dead tick does. The marker is an empty file under the lane's parked/, so this is
# the "modified since" read with no result json to lean on — the half of canary_died that
# no case reached, and that a BSD find answered "nothing" to, silently, every time.
canary_fresh; OLD="$(canary_head)"; T="$(commit commands/watch.md canary-i)"
run   # the canary swap
mkdir -p "$STATE/watcher/parked"; : > "$STATE/watcher/parked/970"
run   # sees the parked card
check "canary I: a card parked during the soak rolls the canary back" [ "$(canary_head)" = "$OLD" ]
check "canary I: …the commit is held, not promoted"                   [ "$(head_)" != "$T" ]
check "canary I: …with one card filed"                                called "Deploy held: ${T:0:7} is not live"

# J. Only what happened since the soak began counts, on either side. Two result jsons
# that were there before this commit reached the canary do not clear its soak…
canary_fresh; T="$(commit commands/watch.md canary-j)"
canary_tick_log success; canary_tick_log success
touch -t 202601010000 "$STATE"/watcher/logs/*.json
run   # the canary swap
run   # nothing new since: still soaking
check "canary J: ticks from before the soak do not clear it"          out_lacks "promoting"
check "canary J: …the stable checkout has not moved"                  [ "$(head_)" != "$T" ]
# …and a card parked before it does not hold it.
canary_fresh; T="$(commit commands/watch.md canary-j2)"
mkdir -p "$STATE/watcher/parked"; : > "$STATE/watcher/parked/971"
touch -t 202601010000 "$STATE/watcher/parked/971"
run   # the canary swap
run
check "canary J: a card parked before the soak does not hold it"      not_called "issue create"
check "canary J: …and the canary stays on the commit"                 [ "$(canary_head)" = "$T" ]

# K. What counts as a clean tick is a result json with something in it, and nothing else
# in logs/. A finished tick's own stderr file is not a second one, and neither is the
# running tick's still-empty result (F's shape): the soak stays one short of
# DEPLOY_CANARY_SOAK_TICKS=2 until that tick finishes.
canary_fresh; T="$(commit commands/watch.md canary-k)"
run   # the canary swap
canary_tick_log success
echo "a warning the tick printed" > "$STATE/watcher/logs/card-979-1.stderr"
run
check "canary K: a tick's stderr file is not a second clean tick"     out_lacks "promoting"
: > "$STATE/watcher/logs/still-running.json"; echo 980 > "$STATE/watcher/current"
run
check "canary K: a still-empty result is not counted either"          out_lacks "promoting"
check "canary K: …so the stable checkout has not moved"               [ "$(head_)" != "$T" ]
echo '{"subtype":"success"}' > "$STATE/watcher/logs/still-running.json"; rm -f "$STATE/watcher/current"
run
check "canary K: once that tick finishes, the soak clears"            [ "$(head_)" = "$T" ]

# The history file: what the deployer said, where the floor page can read it (the page's
# user cannot read the journal).
fresh; T="$(commit bin/spawn.sh history-1)"
run
H="$TMP/dstate/history"
check "history: a deploy leaves its lines in a plain file"            grep -q "deployed ${T:0:7}" "$H"
check "history: each line is a UTC stamp, then what was said"         grep -qE "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z deployed ${T:0:7}" "$H"
check "history: the journal line is unchanged"                        grep -qE "Z autodeploy: deployed ${T:0:7}" "$TMP/out"
run; run
check "history: a quiet run that says nothing new adds nothing"       [ "$(grep -c "live at" "$H")" = 1 ]
i=0; while [ "$i" -lt 30 ]; do echo "2026-01-01T00:00:00Z filler $i" >> "$H"; i=$((i + 1)); done
T2="$(commit bin/spawn.sh history-2)"
DEPLOY_HISTORY_LINES=10 run
check "history: trimmed to the newest lines once it is twice the cap" [ "$(wc -l < "$H")" -le 20 ]
check "history: ...and the newest line survives the trim"             grep -q "deployed ${T2:0:7}" "$H"
chmod 000 "$H"; T3="$(commit bin/spawn.sh history-3)"
run
chmod 644 "$H"
check "history: a file it cannot write never stops a deploy"          [ "$(head_)" = "$T3" ]
check "history: ...and adds no noise of its own to the journal"       out_lacks "ermission denied"
T4="$(commit bin/spawn.sh history-4)"
DEPLOY_HISTORY_LINES=10x run
check "history: a cap that is not a number never stops a deploy either" [ "$(head_)" = "$T4" ]

echo
if [ "$fail" = 0 ]; then echo "$n cases, all passed"; else echo "FAILED (of $n cases)"; fi
exit "$fail"
