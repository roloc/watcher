#!/usr/bin/env bash
# deploy/install.sh's hygiene_opts_in, exercised directly (INSTALL_LIB_ONLY=1 stops the
# file right after defining it, before anything that needs root, a watcher user or
# systemd — the same trick bin/test-deploy.sh already uses for canary_clone). No network:
# PROJECT_ROOT and its "origin" are both real local git repos.
#
#   bash bin/test-install-hygiene.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL="$HERE/../deploy/install.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }

BIN="$TMP/bin"; mkdir -p "$BIN"
CALLS="$TMP/calls"
export CALLS

# su - <user> -c <cmd>: run the command as ourselves, logging the call — the exact stand-in
# bin/test-deploy.sh already uses. It proves the real function goes through `su`, never a
# plain `git` as whoever runs install.sh (card #196 round 2: root's own git refuses a repo
# it does not own).
cat > "$BIN/su" <<'EOF'
#!/usr/bin/env bash
echo "su $*" >> "$CALLS"
[ "$1" = - ] && shift; shift; [ "$1" = -c ] && shift
exec bash -c "$1"
EOF
chmod +x "$BIN/su"
export PATH="$BIN:$PATH"

ORIGIN="$TMP/origin-repo"
mkdir -p "$ORIGIN"
git -C "$ORIGIN" init -q -b main
git -C "$ORIGIN" config user.email test@example.com
git -C "$ORIGIN" config user.name Test
echo hello > "$ORIGIN/README.md"
git -C "$ORIGIN" add README.md
git -C "$ORIGIN" commit -q -m initial

opts_in() { # opts_in <env-file-contents-minus-PROJECT_ROOT-line>
  : > "$CALLS"
  # WATCHER_DIR points at a directory with no .git, and WATCHER_REPO is set, so the
  # top-of-file REPO_URL derivation takes its no-op "else" branch and never itself calls
  # su or git — otherwise, run on this very box, WATCHER_DIR would default to the real
  # /opt/watcher (a real git checkout, with a real watcher user to su into), and that
  # unrelated call would land in $CALLS before hygiene_opts_in ever runs.
  WATCHER_DIR="$TMP/no-such-watcher-dir" WATCHER_REPO="https://example.test/dummy.git" \
    INSTALL_LIB_ONLY=1 bash -c "source '$INSTALL'; hygiene_opts_in '$1'"
}

envfile() { # envfile <name> <body> — writes $TMP/<name>.env, returns its path
  printf '%s\n' "$2" > "$TMP/$1.env"
  printf '%s' "$TMP/$1.env"
}

echo "hygiene_opts_in"

# 1. HYGIENE=1 opts in without any PROJECT_ROOT at all, and short-circuits before su
f="$(envfile explicit 'HYGIENE=1')"
opts_in "$f"
if [ $? -eq 0 ]; then pass "HYGIENE=1: opts in"; else flunk "HYGIENE=1 did not opt in"; fi
if [ ! -s "$CALLS" ]; then pass "…and never calls su at all"; else flunk "su called despite HYGIENE=1" "$(cat "$CALLS")"; fi

# 2. No HYGIENE, no PROJECT_ROOT: never opts in, never calls su
f="$(envfile nothing '')"
opts_in "$f"
if [ $? -ne 0 ]; then pass "no HYGIENE, no PROJECT_ROOT: does not opt in"; else flunk "opted in with nothing set"; fi
if [ ! -s "$CALLS" ]; then pass "…and never calls su"; else flunk "su called with no PROJECT_ROOT" "$(cat "$CALLS")"; fi

# 3. PROJECT_ROOT set but not a real checkout: does not opt in, never calls su
f="$(envfile missing "PROJECT_ROOT=$TMP/does-not-exist")"
opts_in "$f"
if [ $? -ne 0 ]; then pass "PROJECT_ROOT missing on disk: does not opt in"; else flunk "opted in with a missing PROJECT_ROOT"; fi
if [ ! -s "$CALLS" ]; then pass "…and never calls su"; else flunk "su called for a missing PROJECT_ROOT" "$(cat "$CALLS")"; fi

# 4. A real checkout with bin/hygiene.sh on origin/main: opts in, via su
PROOT="$TMP/with-bin-hygiene"
git clone -q "$ORIGIN" "$PROOT"
mkdir -p "$ORIGIN/bin"; echo '#!/bin/sh' > "$ORIGIN/bin/hygiene.sh"; chmod +x "$ORIGIN/bin/hygiene.sh"
git -C "$ORIGIN" add bin/hygiene.sh; git -C "$ORIGIN" commit -q -m "add bin/hygiene.sh"
f="$(envfile withbin "PROJECT_ROOT=$PROOT")"
opts_in "$f"
if [ $? -eq 0 ]; then pass "bin/hygiene.sh on origin/main: opts in"; else flunk "did not opt in with bin/hygiene.sh"; fi
if grep -q "^su - watcher -c" "$CALLS"; then pass "…reached through su - watcher, not a plain git call"; else flunk "su - watcher never called" "$(cat "$CALLS")"; fi

# 5. scripts/hygiene.sh (riffmap/riffmap-site's own path) opts in too
PROOT2="$TMP/with-scripts-hygiene"
rm -rf "$ORIGIN/bin"; git -C "$ORIGIN" add -A; git -C "$ORIGIN" commit -q -m "remove bin/hygiene.sh" --allow-empty >/dev/null
mkdir -p "$ORIGIN/scripts"; echo '#!/bin/sh' > "$ORIGIN/scripts/hygiene.sh"; chmod +x "$ORIGIN/scripts/hygiene.sh"
git -C "$ORIGIN" add scripts/hygiene.sh; git -C "$ORIGIN" commit -q -m "add scripts/hygiene.sh"
git clone -q "$ORIGIN" "$PROOT2"
f="$(envfile withscripts "PROJECT_ROOT=$PROOT2")"
opts_in "$f"
if [ $? -eq 0 ]; then pass "scripts/hygiene.sh on origin/main: opts in too"; else flunk "did not opt in with scripts/hygiene.sh"; fi

# 6. Neither bin/ nor scripts/ has one: does not opt in
rm -rf "$ORIGIN/scripts"; git -C "$ORIGIN" add -A; git -C "$ORIGIN" commit -q -m "remove scripts/hygiene.sh"
PROOT3="$TMP/with-neither"
git clone -q "$ORIGIN" "$PROOT3"
f="$(envfile neither "PROJECT_ROOT=$PROOT3")"
opts_in "$f"
if [ $? -ne 0 ]; then pass "neither bin/ nor scripts/ has one: does not opt in"; else flunk "opted in with no hygiene.sh anywhere"; fi

# 7. The regression this file exists to pin: PROJECT_ROOT's own checked-out working tree
#    is stale/detached and does NOT have bin/hygiene.sh on disk, but origin/main DOES —
#    the probe must read origin/main, never the working copy (round 2 finding #3).
PROOT4="$TMP/stale-working-tree"
git clone -q "$ORIGIN" "$PROOT4"
before="$(git -C "$ORIGIN" rev-parse HEAD)"   # neither bin/ nor scripts/ has one, at this point
git -C "$PROOT4" checkout -q --detach "$before"
mkdir -p "$ORIGIN/bin"; echo '#!/bin/sh' > "$ORIGIN/bin/hygiene.sh"; chmod +x "$ORIGIN/bin/hygiene.sh"
git -C "$ORIGIN" add bin/hygiene.sh; git -C "$ORIGIN" commit -q -m "bin/hygiene.sh lands after PROOT4 detached"
f="$(envfile stale "PROJECT_ROOT=$PROOT4")"
opts_in "$f"
if [ $? -eq 0 ]; then
  pass "a stale/detached PROJECT_ROOT still opts in off origin/main, not its own working tree"
else
  flunk "missed a hygiene.sh that only the working tree, not origin/main, lacked"
fi
if [ ! -f "$PROOT4/bin/hygiene.sh" ]; then pass "…and the working tree itself never had it checked out"; else flunk "test setup is wrong: bin/hygiene.sh is on disk after all"; fi

echo
if [ "$fail" -eq 0 ]; then
  echo "$n cases, all passed"
else
  echo "$n cases, FAILURES ABOVE"
fi
exit "$fail"
