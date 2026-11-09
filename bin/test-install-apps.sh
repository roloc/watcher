#!/usr/bin/env bash
# deploy/install.sh's install_apps (card #255), exercised directly (INSTALL_LIB_ONLY=1 stops
# the file right after its functions, as bin/test-install-desk.sh does). No root, no network:
# id, useradd, chown, chmod, install and visudo are stubs that log to $CALLS (install copies).
# APPS, REVIEW_LIB_DIR, REVIEW_HOME and SUDOERS_DIR point the function at folders under $TMP.
#
#   bash bin/test-install-apps.sh      # exit 0 = every case holds
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL="$HERE/../deploy/install.sh"
REPO="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }
check() { if eval "$2"; then pass "$1"; else flunk "$1" "${3:-}"; fi; }

BIN="$TMP/bin"; mkdir -p "$BIN"; CALLS="$TMP/calls"; export CALLS TMP
cat > "$BIN/id" <<'EOF2'
#!/usr/bin/env bash
[ "$1" = watcher-reviewer ] && { [ -f "$TMP/user-exists" ] && exit 0 || exit 1; }
exec /usr/bin/id "$@"
EOF2
cat > "$BIN/useradd" <<'EOF2'
#!/usr/bin/env bash
echo "useradd $*" >> "$CALLS"
[ -f "$TMP/useradd-fails" ] && exit 1
touch "$TMP/user-exists"
EOF2
printf '#!/usr/bin/env bash\necho "chown $*" >> "$CALLS"\n' > "$BIN/chown"
printf '#!/usr/bin/env bash\necho "chmod $*" >> "$CALLS"\n' > "$BIN/chmod"
cat > "$BIN/visudo" <<'EOF2'
#!/usr/bin/env bash
echo "visudo $*" >> "$CALLS"
[ -f "$TMP/visudo-fails" ] && exit 1; exit 0
EOF2
cat > "$BIN/install" <<'EOF2'
#!/usr/bin/env bash
echo "install $*" >> "$CALLS"
args=(); dirmode=0
while [ $# -gt 0 ]; do case "$1" in -d) dirmode=1; shift ;; -m|-o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done
if [ "$dirmode" = 1 ]; then mkdir -p "${args[@]}"; else cp "${args[0]}" "${args[1]}"; fi
EOF2
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

export WATCHER_APPS_DIR="$TMP/etc/apps" REVIEW_LIB_DIR="$TMP/lib/watcher-review" REVIEW_HOME="$TMP/var/watcher-review" SUDOERS_DIR="$TMP/sudoers.d"
fresh() { rm -rf "$TMP/etc" "$TMP/lib" "$TMP/var" "$TMP/sudoers.d" "$TMP/user-exists" "$TMP/useradd-fails" "$TMP/visudo-fails"; mkdir -p "$SUDOERS_DIR"; : > "$CALLS"; }
mkproject() { mkdir -p "$WATCHER_APPS_DIR/$1"; : > "$WATCHER_APPS_DIR/$1/config"; : > "$WATCHER_APPS_DIR/$1/author.pem"; [ "${2:-}" = reviewer ] && : > "$WATCHER_APPS_DIR/$1/reviewer.pem"; return 0; }
run() { : > "$CALLS"; OUT="$(WATCHER_DIR="$REPO" WATCHER_REPO="https://example.test/dummy.git" INSTALL_LIB_ONLY=1 bash -c ". '$INSTALL'; install_apps" 2>&1)"; RC=$?; }

echo "1. no /etc/watcher/apps: install.sh behaves exactly as today"
fresh; run
check "nothing is called, nothing is said" '[ "$RC" = 0 ] && [ ! -s "$CALLS" ] && [ -z "$OUT" ]' "rc=$RC $OUT $(cat "$CALLS")"

echo "2. an author App only (a fork with no reviewer App)"
fresh; mkproject demo; run
check "the author key is root:watcher 640, the config readable by all" 'grep -q "chown root:watcher $WATCHER_APPS_DIR/demo/author.pem" "$CALLS" && grep -q "chmod 640 $WATCHER_APPS_DIR/demo/author.pem" "$CALLS" && grep -q "chmod 644 $WATCHER_APPS_DIR/demo/config" "$CALLS"' "$(cat "$CALLS")"
check "no reviewer user, no sudoers line, no root-owned copy" '! grep -q "useradd\|visudo" "$CALLS" && [ ! -e "$SUDOERS_DIR/watcher-review-submit" ] && [ ! -e "$REVIEW_LIB_DIR/review-submit" ]'

echo "3. a reviewer App: the separate user, the key it alone reads, the root-owned helper, one sudoers line"
fresh; mkproject demo reviewer; mkproject other reviewer; run
check "watcher-reviewer is created as a system user with its own home" 'grep -q "useradd -r -m -d $REVIEW_HOME -s /usr/sbin/nologin watcher-reviewer" "$CALLS"' "$(cat "$CALLS")"
check "each reviewer key is root:watcher-reviewer 640, never the watcher group" 'grep -c "chown root:watcher-reviewer $WATCHER_APPS_DIR/.*/reviewer.pem" "$CALLS" | grep -qx 2 && ! grep -q "chown root:watcher .*reviewer.pem" "$CALLS"' "$(cat "$CALLS")"
check "the helper and the token script are copied in, root-owned and executable" 'cmp -s "$REPO/bin/review-submit.sh" "$REVIEW_LIB_DIR/review-submit" && cmp -s "$REPO/bin/gh-app-token.sh" "$REVIEW_LIB_DIR/gh-app-token" && grep -q "install -m 755 -o root -g root $REPO/bin/review-submit.sh" "$CALLS"'
check "the sudoers line lets watcher run that one program as watcher-reviewer, and keep GH_REPO" \
  '[ "$(cat "$SUDOERS_DIR/watcher-review-submit")" = "$(printf "Defaults>watcher-reviewer env_keep += \"GH_REPO\"\nwatcher ALL=(watcher-reviewer) NOPASSWD: %s/review-submit" "$REVIEW_LIB_DIR")" ]' "$(cat "$SUDOERS_DIR/watcher-review-submit" 2>&1)"
check "…and visudo checked it first; the file is 0440 root" 'grep -q "^visudo -cf" "$CALLS" && grep -q "install -m 440 -o root -g root .* $SUDOERS_DIR/watcher-review-submit" "$CALLS"'
check "the reviewer's token cache is private to it" 'grep -q "install -d -o watcher-reviewer -g watcher-reviewer -m 700 $REVIEW_HOME/cache" "$CALLS"'

echo "4. a second run changes nothing it need not"
run
check "the user exists, the sudoers file is the same: no useradd and no second sudoers install" '! grep -q "^useradd" "$CALLS" && ! grep -q "install -m 440" "$CALLS"' "$(cat "$CALLS")"

echo "5. failures leave reviews as comments, never a broken sudo"
fresh; mkproject demo reviewer; touch "$TMP/visudo-fails"; run
check "visudo refusing: no sudoers file, one warning" '[ ! -e "$SUDOERS_DIR/watcher-review-submit" ] && printf "%s" "$OUT" | grep -q "did not pass visudo"' "$OUT"
fresh; mkproject demo reviewer; touch "$TMP/useradd-fails"; run
check "useradd failing: the reviewer key is root-only, nothing is installed, one warning" 'grep -q "chown root:root $WATCHER_APPS_DIR/demo/reviewer.pem" "$CALLS" && [ ! -e "$SUDOERS_DIR/watcher-review-submit" ] && printf "%s" "$OUT" | grep -q "could not create the watcher-reviewer user"' "$OUT $(cat "$CALLS")"

echo
if [ "$fail" = 0 ]; then echo "test-install-apps.sh: $n checks, all passed"; else echo "test-install-apps.sh: $n checks, SOME FAILED"; fi
exit $fail
