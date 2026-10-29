#!/usr/bin/env bash
# deploy/install.sh's install_desk_service (card #205), exercised directly —
# INSTALL_LIB_ONLY=1 stops the file right after defining it, before anything that needs
# root, a watcher user or systemd, the same trick bin/test-install-hygiene.sh already uses
# for hygiene_opts_in and bin/test-deploy.sh for canary_clone. useradd, usermod, id and
# systemctl are stubs (no root here); chmod and install run for real, against a tmp
# directory (SYSTEMD_UNIT_DIR), so the case that matters most — the real unit file this
# repo ships lands byte-for-byte where a real run would put it — is not itself a stub.
#
# Why this file and not a case in bin/test-deploy.sh: that suite's own fixture replaces
# deploy/install.sh wholesale with a stub that only logs its own argv (see its header) —
# the real install.sh never runs there at all, for ANY unit it installs, so a case added
# to it would prove nothing about this repo's actual service-install logic. This is the
# same reason test-install-hygiene.sh is its own file rather than a test-deploy.sh case.
#
#   bash bin/test-install-desk.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL="$HERE/../deploy/install.sh"
REPO_ROOT="$HERE/.."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else flunk "$what"; fi; }

BIN="$TMP/bin"; mkdir -p "$BIN"
CALLS="$TMP/calls"
export CALLS

cat > "$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
exit 0
EOF
cat > "$BIN/useradd" <<'EOF'
#!/usr/bin/env bash
echo "useradd $*" >> "$CALLS"
EOF
cat > "$BIN/usermod" <<'EOF'
#!/usr/bin/env bash
echo "usermod $*" >> "$CALLS"
EOF
# id watcher-desk: exit 0 (exists) when $TMP/desk-exists is present, 1 (missing) otherwise —
# the one piece of real-world state these cases vary. Anything else (install.sh's own
# top-of-file `id watcher` check, run on every source regardless of INSTALL_LIB_ONLY) is
# always "missing", so that check takes its plain-git fallback and never shells out to
# `su` at all — WATCHER_DIR has to be the real repo root below, for the real unit file
# case (4), so unlike test-install-hygiene.sh's own stub this can't dodge that check by
# pointing WATCHER_DIR somewhere with no .git.
cat > "$BIN/id" <<EOF
#!/usr/bin/env bash
echo "id \$*" >> "$CALLS"
case "\$1" in
  watcher-desk) [ -f "$TMP/desk-exists" ] ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

UNITDIR="$TMP/units"
run() { # run — sources install.sh for its functions only, then calls install_desk_service
  : > "$CALLS"
  mkdir -p "$UNITDIR"
  # WATCHER_REPO: a worktree's own .git is a file (a gitdir pointer), not a directory, so
  # install.sh's top-of-file `[ -d "$WATCHER_DIR/.git" ]` reads this checkout as "not a
  # git checkout at all" and otherwise demands WATCHER_REPO — never read by
  # install_desk_service itself, just required to get past that unrelated check.
  WATCHER_DIR="$REPO_ROOT" WATCHER_REPO="https://example.test/dummy.git" \
    SYSTEMD_UNIT_DIR="$UNITDIR" INSTALL_LIB_ONLY=1 \
    bash -c "source '$INSTALL'; install_desk_service" > "$TMP/out" 2>&1
}
called()     { grep -q -- "$1" "$CALLS"; }
not_called() { ! grep -q -- "$1" "$CALLS"; }

echo "install_desk_service"

# 1. watcher-desk does not exist yet: useradd creates it
rm -f "$TMP/desk-exists"; rm -rf "$UNITDIR"
run
check "no such user: useradd creates watcher-desk"       called "useradd -m -s /bin/bash watcher-desk"
check "...as a real, non-login-shell-less account"       called "/bin/bash watcher-desk"

# 2. watcher-desk already exists: useradd is never called again
: > "$TMP/desk-exists"; rm -rf "$UNITDIR"
run
check "existing user: useradd is not called again"       not_called "useradd"
check "...but id is still asked"                          called "id watcher-desk"

# 3. Every run — new user or not — adds it to the watcher group for read access
check "every run: usermod adds watcher-desk to the watcher group" called "usermod -aG watcher watcher-desk"

# 4. The real unit file this repo ships lands at the (overridden) systemd unit dir,
#    byte-for-byte — not a stub standing in for it.
check "the real deploy/watcher-desk.service is installed verbatim" \
      diff -q "$REPO_ROOT/deploy/watcher-desk.service" "$UNITDIR/watcher-desk.service"
check "...at the exact mode install.sh's other units use (644)" \
      [ "$(stat -c %a "$UNITDIR/watcher-desk.service" 2>/dev/null || stat -f %Lp "$UNITDIR/watcher-desk.service")" = 644 ]

# 5. systemd is told about it, enabled, and (re)started — the same sequence the floor
#    service's own install.sh block already uses, so a deploy's restart also reaches this.
check "daemon-reload runs"                                called "systemctl daemon-reload"
check "enabled and started"                                called "systemctl enable --now watcher-desk.service"
check "and (re)started, the same as the floor page is"     called "systemctl restart watcher-desk.service"

echo
if [ "$fail" -eq 0 ]; then
  echo "$n cases, all passed"
else
  echo "$n cases, FAILURES ABOVE"
fi
exit "$fail"
