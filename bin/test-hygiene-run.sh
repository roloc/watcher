#!/usr/bin/env bash
# bin/hygiene-run.sh's cases, in test-detect.sh's shape: no network, `gh` and `board` are
# stubs that log one line per call to $CALLS so a case can grep what the run did. Git is
# real — PROJECT_ROOT is a real local checkout and the throwaway checkout is cloned from
# it, both entirely on disk, so this proves the real clone/fetch/checkout path works
# without ever reaching GitHub. The project's own hygiene.sh is a fixture that reads its
# exit code and report from files under $TMP, so one committed script can stand in for
# every case.
#
#   bash bin/test-hygiene-run.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HYGRUN="$HERE/hygiene-run.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }

BIN="$TMP/bin"; mkdir -p "$BIN"
CALLS="$TMP/calls"
export CALLS TMP

# gh: `issue list --search ... --json number --jq` prints $TMP/existing-issue (empty =
# none); `issue create` prints a URL numbered from $TMP/next-issue (default 701) and saves
# --body-file to $TMP/last-body; `issue comment <n>` saves --body-file to
# $TMP/last-comment and fails if $TMP/gh-comment-fails exists.
cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$CALLS"
case "$1 $2" in
  "issue list")
    cat "$TMP/existing-issue" 2>/dev/null
    ;;
  "issue create")
    args=("$@")
    for ((i=0; i<${#args[@]}; i++)); do
      [ "${args[$i]}" = "--body-file" ] && cp "${args[$i+1]}" "$TMP/last-body" 2>/dev/null
    done
    echo "https://github.com/roloc/testproj/issues/$(cat "$TMP/next-issue" 2>/dev/null || echo 701)"
    ;;
  "issue comment")
    [ -f "$TMP/gh-comment-fails" ] && exit 1
    args=("$@")
    for ((i=0; i<${#args[@]}; i++)); do
      [ "${args[$i]}" = "--body-file" ] && cp "${args[$i+1]}" "$TMP/last-comment" 2>/dev/null
    done
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
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

WDIR="$TMP/opt"
mkdir -p "$WDIR/projects"
export WATCHER_DIR="$WDIR"

# The watcher's own agents/, as every lane's deploy actually runs it (card #206's
# comparison target). $WDIR is not a git repo, so the script's own `git -C "$WATCHER_DIR"
# rev-parse` falls back to "unknown" for the sha in a finding's text — fine for these
# cases, which only check the file names and the "differs" wording.
mkdir -p "$WDIR/agents"
printf 'WATCHER REVIEWER v1\n' > "$WDIR/agents/reviewer.md"
printf 'WATCHER USABILITY v1\n' > "$WDIR/agents/usability.md"

cat > "$WDIR/projects/testproj.env" <<EOF
PROJECT_NAME=testproj
PROJECT_ROOT=$TMP/project-root
PROJECT_REPO=roloc/testproj
BOARD_OWNER=roloc
BOARD_NUMBER=9
EOF

# A real "origin" and a real PROJECT_ROOT cloned from it, same shape install.sh's own
# `gh repo clone` leaves behind — just two local directories, no network.
ORIGIN="$TMP/origin-repo"
mkdir -p "$ORIGIN"
git -C "$ORIGIN" init -q -b main
git -C "$ORIGIN" config user.email test@example.com
git -C "$ORIGIN" config user.name Test
echo "hello" > "$ORIGIN/README.md"
git -C "$ORIGIN" add README.md
git -C "$ORIGIN" commit -q -m initial
git clone -q "$ORIGIN" "$TMP/project-root"
git -C "$TMP/project-root" config user.email test@example.com
git -C "$TMP/project-root" config user.name Test

# hygiene_fixture <dir> — commits a bin/hygiene.sh under <dir> (relative to the repo root:
# "bin" or "scripts") into $ORIGIN, then fetches it into PROJECT_ROOT. The fixture logs its
# own env to $TMP/hygiene-calls, prints $TMP/hygiene-report and exits with $TMP/hygiene-exit
# (default 0) — so one script, committed once per case, stands in for every behavior.
hygiene_fixture() {
  local dir="$1"
  mkdir -p "$ORIGIN/$dir"
  cat > "$ORIGIN/$dir/hygiene.sh" <<'EOF'
#!/usr/bin/env bash
{
  echo "GITHUB_REPOSITORY=${GITHUB_REPOSITORY:-unset}"
  echo "BOARD_NUMBER=${BOARD_NUMBER:-unset}"
} >> "$TMP/hygiene-calls"
cat "$TMP/hygiene-report" 2>/dev/null
exit "$(cat "$TMP/hygiene-exit" 2>/dev/null || echo 0)"
EOF
  chmod +x "$ORIGIN/$dir/hygiene.sh"
  git -C "$ORIGIN" add "$dir/hygiene.sh"
  git -C "$ORIGIN" commit -q -m "add $dir/hygiene.sh" >/dev/null
  git -C "$TMP/project-root" fetch -q origin main
  git -C "$TMP/project-root" merge -q --ff-only origin/main
}
no_hygiene_script() {
  rm -rf "$ORIGIN/bin" "$ORIGIN/scripts"
  git -C "$ORIGIN" add -A
  git -C "$ORIGIN" commit -q -m "remove hygiene.sh" --allow-empty >/dev/null
  git -C "$TMP/project-root" fetch -q origin main
  git -C "$TMP/project-root" merge -q --ff-only origin/main
}
hygiene_card_md() {
  local dir="$1"
  echo "CARD TEMPLATE" > "$ORIGIN/$dir/hygiene-card.md"
  git -C "$ORIGIN" add "$dir/hygiene-card.md"
  git -C "$ORIGIN" commit -q -m "add $dir/hygiene-card.md" >/dev/null
  git -C "$TMP/project-root" fetch -q origin main
  git -C "$TMP/project-root" merge -q --ff-only origin/main
}
# agents_file <name> <content> — sets the project's own .claude/agents/<name>.md (card
# #206's "a project's copy shadows the installed symlink" case) and fetches it into
# PROJECT_ROOT, same shape as hygiene_fixture above.
agents_file() {
  local name="$1" content="$2"
  mkdir -p "$ORIGIN/.claude/agents"
  printf '%s\n' "$content" > "$ORIGIN/.claude/agents/$name.md"
  git -C "$ORIGIN" add .claude
  git -C "$ORIGIN" commit -q -m "set .claude/agents/$name.md" >/dev/null
  git -C "$TMP/project-root" fetch -q origin main
  git -C "$TMP/project-root" merge -q --ff-only origin/main
}
no_agents_copy() {
  rm -rf "$ORIGIN/.claude"
  git -C "$ORIGIN" add -A
  git -C "$ORIGIN" commit -q -m "remove .claude/agents" --allow-empty >/dev/null
  git -C "$TMP/project-root" fetch -q origin main
  git -C "$TMP/project-root" merge -q --ff-only origin/main
}

report()      { printf '%s\n' "$1" > "$TMP/hygiene-report"; }
exitcode()     { printf '%s\n' "$1" > "$TMP/hygiene-exit"; }
existing()    { printf '%s\n' "$1" > "$TMP/existing-issue"; }
next_issue()  { printf '%s\n' "$1" > "$TMP/next-issue"; }

run() { : > "$CALLS"; bash "$HYGRUN" testproj > "$TMP/out" 2>&1; echo $? > "$TMP/rc"; }
rc()  { cat "$TMP/rc"; }
out_has()    { grep -q -- "$1" "$TMP/out"; }
called()     { grep -q -- "$1" "$CALLS"; }
not_called() { ! grep -q -- "$1" "$CALLS"; }

fresh() {
  rm -f "$TMP/hygiene-report" "$TMP/hygiene-exit" "$TMP/hygiene-calls" "$TMP/existing-issue" \
        "$TMP/next-issue" "$TMP/item-add-fails" "$TMP/board-move-fails" "$TMP/gh-comment-fails" \
        "$TMP/last-body" "$TMP/last-comment"
  rm -rf "$TMP/project-root-worktrees"
  : > "$TMP/existing-issue"
}

echo "hygiene-run.sh"

# 1. No projects/<p>.env at all
fresh
bash "$HYGRUN" missing > "$TMP/out" 2>&1; echo $? > "$TMP/rc"
[ "$(rc)" = 2 ] && pass "no project file: exit 2" || flunk "no project file: exit 2" "got $(rc)"
out_has "nothing configured" && pass "no project file: says so" || flunk "no project file: says so" "$(cat "$TMP/out")"

# 2. PROJECT_ROOT is not a git checkout
fresh
sed -i.bak "s#PROJECT_ROOT=.*#PROJECT_ROOT=$TMP/not-a-repo#" "$WDIR/projects/testproj.env"
run
check1() { [ "$(rc)" = 2 ] && out_has "not a checkout"; }
if check1; then pass "PROJECT_ROOT missing: exit 2, says so"; else flunk "PROJECT_ROOT missing" "$(cat "$TMP/out")"; fi
mv "$WDIR/projects/testproj.env.bak" "$WDIR/projects/testproj.env"

# 3. origin fetch fails (PROJECT_ROOT's own origin is unreachable)
fresh
git -C "$TMP/project-root" remote set-url origin "$TMP/does-not-exist"
run
if [ "$(rc)" = 2 ] && out_has "could not fetch"; then pass "origin fetch fails: exit 2, says so"; else flunk "origin fetch failure" "$(cat "$TMP/out")"; fi
not_called "gh" && pass "…and no gh calls at all" || flunk "gh called despite the fetch failure" "$(cat "$CALLS")"
git -C "$TMP/project-root" remote set-url origin "$ORIGIN"

# 4. No hygiene.sh anywhere in the checkout
fresh; no_hygiene_script
run
if [ "$(rc)" = 2 ] && out_has "no hygiene.sh"; then pass "no hygiene.sh in the repo: exit 2, says so"; else flunk "missing hygiene.sh" "$(cat "$TMP/out")"; fi
not_called "gh" && pass "…and no gh calls" || flunk "gh called with no hygiene.sh" "$(cat "$CALLS")"

# 5. scripts/hygiene.sh (riffmap/riffmap-site's own path) is found when bin/ has none
fresh; hygiene_fixture scripts; exitcode 0
run
if [ "$(rc)" = 0 ]; then pass "scripts/hygiene.sh is found and run (the riffmap/riffmap-site shape)"; else flunk "scripts/hygiene.sh not found" "$(cat "$TMP/out")"; fi

# 6. Clean sweep (exit 0): nothing filed
fresh; hygiene_fixture bin; exitcode 0
run
check6() { [ "$(rc)" = 0 ] && out_has "clean"; }
if check6; then pass "clean sweep: exit 0, says so"; else flunk "clean sweep" "$(cat "$TMP/out")"; fi
not_called "gh" && pass "…and no gh calls at all" || flunk "gh called on a clean sweep" "$(cat "$CALLS")"

# 7. hygiene.sh could not run (exit 2): no card, says so
fresh; hygiene_fixture bin; exitcode 2; report "boom"
run
if [ "$(rc)" = 2 ] && out_has "could not run"; then pass "hygiene.sh exit 2: this run exits 2, says so"; else flunk "hygiene.sh exit 2 not surfaced" "$(cat "$TMP/out")"; fi
not_called "gh" && pass "…and no gh calls" || flunk "gh called when hygiene.sh could not run" "$(cat "$CALLS")"

# 8. A finding (exit 1), no existing open card: files one, puts it on the board, moves it
#    straight to Ready — never Backlog.
fresh; hygiene_fixture bin; exitcode 1; report "- stale branch card-9-old"; next_issue 701
run
if [ "$(rc)" = 0 ]; then pass "a finding: exits 0 once filed"; else flunk "a finding exit code" "got $(rc)"; fi
called "gh issue create -R roloc/testproj" && pass "a finding: files a card" || flunk "no issue create" "$(cat "$CALLS")"
called "Weekly hygiene sweep" && pass "…titled 'Weekly hygiene sweep — <date>'" || flunk "wrong title" "$(cat "$CALLS")"
called "gh project item-add 9 --owner roloc" && pass "…put on board 9, owner roloc" || flunk "no item-add" "$(cat "$CALLS")"
called "board move 701 Ready" && pass "…and moved straight to Ready, never Backlog" || flunk "not moved to Ready" "$(cat "$CALLS")"
grep -q "stale branch card-9-old" "$TMP/last-body" && pass "…the card body carries the report" || flunk "report missing from card body" "$(cat "$TMP/last-body" 2>/dev/null)"

# 9. hygiene.sh's own environment never sees BOARD_NUMBER — only this script's later
#    `board move` call does. GITHUB_REPOSITORY is set, so hygiene.sh's own branch/TODO
#    checks can still run.
fresh; hygiene_fixture bin; exitcode 1; report "finding"; next_issue 702
run
grep -q "^BOARD_NUMBER=unset$" "$TMP/hygiene-calls" && pass "hygiene.sh's own subprocess never sees BOARD_NUMBER" || flunk "BOARD_NUMBER leaked into hygiene.sh" "$(cat "$TMP/hygiene-calls")"
grep -q "^GITHUB_REPOSITORY=roloc/testproj$" "$TMP/hygiene-calls" && pass "…but GITHUB_REPOSITORY is set" || flunk "GITHUB_REPOSITORY missing" "$(cat "$TMP/hygiene-calls")"

# 10. hygiene-card.md, when the project ships one, is prepended to the card body
fresh; hygiene_fixture bin; hygiene_card_md bin; exitcode 1; report "finding"; next_issue 703
run
grep -q "CARD TEMPLATE" "$TMP/last-body" && pass "hygiene-card.md is prepended to the filed card" || flunk "no card template in body" "$(cat "$TMP/last-body" 2>/dev/null)"

# 11. An already-open card: comments the fresh list, files nothing new, touches no board state
fresh; hygiene_fixture bin; exitcode 1; report "fresh finding"; existing 650
run
if [ "$(rc)" = 0 ]; then pass "existing open card: exits 0"; else flunk "existing card exit code" "got $(rc)"; fi
not_called "issue create" && pass "…no new card filed" || flunk "filed a second card" "$(cat "$CALLS")"
called "issue comment 650" && pass "…comments the open card instead" || flunk "no comment on the open card" "$(cat "$CALLS")"
not_called "project item-add" && pass "…never touches the board (already there)" || flunk "touched the board for an existing card" "$(cat "$CALLS")"
not_called "board move" && pass "…never moves it" || flunk "moved an existing card" "$(cat "$CALLS")"
grep -q "fresh finding" "$TMP/last-comment" && pass "…the comment carries this week's list" || flunk "comment missing the report" "$(cat "$TMP/last-comment" 2>/dev/null)"

# 12. The board placement fails after the card is filed: says so, does not pretend it landed
fresh; hygiene_fixture bin; exitcode 1; report "finding"; next_issue 704; touch "$TMP/board-move-fails"
run
called "gh issue create -R roloc/testproj" && pass "board move fails: the card still gets filed" || flunk "card not filed" "$(cat "$CALLS")"
out_has "could not put it on the board" && pass "…and says to check by hand" || flunk "no warning for the failed move" "$(cat "$TMP/out")"

# 12b. Same, when it's the item-add step that fails rather than the move
fresh; hygiene_fixture bin; exitcode 1; report "finding"; next_issue 705; touch "$TMP/item-add-fails"
run
called "gh issue create -R roloc/testproj" && pass "item-add fails: the card still gets filed" || flunk "card not filed" "$(cat "$CALLS")"
out_has "could not put it on the board" && pass "…and says to check by hand" || flunk "no warning for the failed item-add" "$(cat "$TMP/out")"
not_called "board move" && pass "…and never calls board move at all" || flunk "moved a card never added to the board" "$(cat "$CALLS")"

# 13. The throwaway checkout never touches PROJECT_ROOT's own working tree
fresh; hygiene_fixture bin; exitcode 0
echo "untouched sentinel" > "$TMP/project-root/sentinel.txt"
run
[ -f "$TMP/project-root/sentinel.txt" ] && pass "PROJECT_ROOT's working tree is untouched" || flunk "PROJECT_ROOT's working tree was modified"
[ -d "$TMP/project-root-worktrees/hygiene-sweep/.git" ] && pass "the throwaway checkout lives under <project-root>-worktrees/hygiene-sweep" || flunk "no throwaway checkout found"
rm -f "$TMP/project-root/sentinel.txt"

# 14. Running twice reuses the same throwaway checkout (no re-clone) and still works
fresh; hygiene_fixture bin; exitcode 0
run
run
[ "$(rc)" = 0 ] && pass "a second run reuses the throwaway checkout and still succeeds" || flunk "second run failed" "$(cat "$TMP/out")"

# 15. The throwaway checkout's own `origin` is the real remote (PROJECT_ROOT's own origin
#    url) — never PROJECT_ROOT's local path. Otherwise hygiene.sh's own stale-branch
#    check, which fetches and reads refs/remotes/origin/* INSIDE the checkout, would see
#    PROJECT_ROOT's own local branch state instead of what is actually on GitHub.
fresh; hygiene_fixture bin; exitcode 0
run
checkout_origin="$(git -C "$TMP/project-root-worktrees/hygiene-sweep" remote get-url origin)"
if [ "$checkout_origin" = "$ORIGIN" ]; then
  pass "the throwaway checkout's origin is the real remote, not PROJECT_ROOT's local path"
else
  flunk "checkout's origin is wrong" "got $checkout_origin, wanted $ORIGIN"
fi

# 16. PROJECT_ROOT's own checked-out branch can lag behind origin/main — nothing promises
#    it is current. hygiene-run.sh fetches origin/main itself before anything else, so a
#    stale PROJECT_ROOT does not leave the throwaway checkout behind.
fresh; hygiene_fixture bin; exitcode 0
echo newer > "$ORIGIN/newer.txt"
git -C "$ORIGIN" add newer.txt
git -C "$ORIGIN" commit -q -m "a newer commit, not yet in PROJECT_ROOT" >/dev/null
latest="$(git -C "$ORIGIN" rev-parse main)"
# PROJECT_ROOT is deliberately NOT fetched here — its own idea of main is still the older commit.
run
checkout_head="$(git -C "$TMP/project-root-worktrees/hygiene-sweep" rev-parse HEAD)"
if [ "$checkout_head" = "$latest" ]; then
  pass "a stale PROJECT_ROOT does not leave the throwaway checkout behind"
else
  flunk "throwaway checkout is on the wrong commit" "wanted $latest got $checkout_head"
fi

# 17. A project's .claude/agents copy has drifted from the watcher's own (card #206): the
#    sweep's own section files a card naming the file, even though the project's own
#    hygiene.sh reported clean.
fresh; hygiene_fixture bin; exitcode 0; agents_file reviewer "PROJECT'S OWN STALE REVIEWER"; next_issue 706
run
if [ "$(rc)" = 0 ]; then pass "drifted .claude/agents copy: files a card (exits 0 once filed)"; else flunk "drifted agents copy exit code" "got $(rc)"; fi
called "gh issue create -R roloc/testproj" && pass "...a card is filed" || flunk "no issue create for drifted agents copy" "$(cat "$CALLS")"
grep -q "agents/reviewer.md" "$TMP/last-body" && grep -q "differs from the watcher" "$TMP/last-body" \
  && pass "...the finding names the drifted file" || flunk "finding text missing" "$(cat "$TMP/last-body" 2>/dev/null)"
no_agents_copy

# 18. Identical copies, and no copy at all, are not findings: the project's own sweep
#    being clean is the whole story either way.
fresh; hygiene_fixture bin; exitcode 0; agents_file reviewer "WATCHER REVIEWER v1"
run
check18a() { [ "$(rc)" = 0 ] && out_has "clean"; }
if check18a; then pass "identical .claude/agents copy: still clean"; else flunk "identical agents copy" "$(cat "$TMP/out")"; fi
no_agents_copy
fresh; hygiene_fixture bin; exitcode 0
run
check18b() { [ "$(rc)" = 0 ] && out_has "clean"; }
if check18b; then pass "no .claude/agents copy at all: still clean"; else flunk "absent agents copy" "$(cat "$TMP/out")"; fi

# 19. HYGIENE=1 and no hygiene.sh of its own (azeroth-rewind's shape): runs the watcher's
#    own section alone and exits 0 when clean, instead of today's exit 2.
fresh; no_hygiene_script
printf 'HYGIENE=1\n' >> "$WDIR/projects/testproj.env"
run
check19() { [ "$(rc)" = 0 ] && out_has "clean"; }
if check19; then pass "HYGIENE=1, no hygiene.sh of its own: exits 0 clean"; else flunk "HYGIENE=1 no script" "$(cat "$TMP/out")"; fi
sed -i.bak '/^HYGIENE=1$/d' "$WDIR/projects/testproj.env"; rm -f "$WDIR/projects/testproj.env.bak"

echo
if [ "$fail" -eq 0 ]; then
  echo "$n cases, all passed"
else
  echo "$n cases, FAILURES ABOVE"
fi
exit "$fail"
