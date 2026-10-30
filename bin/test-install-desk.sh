#!/usr/bin/env bash
# deploy/install.sh's install_desk, exercised directly (INSTALL_LIB_ONLY=1 stops the file
# right after its functions, the same trick bin/test-install-hygiene.sh uses). No root, no
# systemd, no network: useradd, id, getent, install and systemctl are stubs that log to
# $CALLS; su runs the command as ourselves; git and cmp are real. DESK_HOME and
# DESK_UNIT_DIR point install_desk at folders under $TMP.
#
#   bash bin/test-install-desk.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL="$HERE/../deploy/install.sh"
REPO="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }

BIN="$TMP/bin"; mkdir -p "$BIN"
CALLS="$TMP/calls"
export CALLS TMP

cat > "$BIN/su" <<'EOF2'
#!/usr/bin/env bash
echo "su $*" >> "$CALLS"
[ "$1" = - ] && shift; shift; [ "$1" = -c ] && shift
exec bash -c "$1"
EOF2
# id: the watcher-desk user exists once $TMP/user-exists does (useradd creates that marker).
cat > "$BIN/id" <<'EOF2'
#!/usr/bin/env bash
[ "$1" = watcher-desk ] && { [ -f "$TMP/user-exists" ] && exit 0 || exit 1; }
exec /usr/bin/id "$@"
EOF2
# useradd -m makes the home, as the real one does.
cat > "$BIN/useradd" <<'EOF2'
#!/usr/bin/env bash
echo "useradd $*" >> "$CALLS"; touch "$TMP/user-exists"
while [ $# -gt 0 ]; do case "$1" in -d) mkdir -p "$2"; shift 2 ;; *) shift ;; esac; done
EOF2
cat > "$BIN/getent" <<'EOF2'
#!/usr/bin/env bash
[ "$2" = systemd-journal ] && [ ! -f "$TMP/no-journal-group" ] && exit 0; exit 2
EOF2
# install: the real thing needs root for -o/-g; this one copies and logs.
cat > "$BIN/install" <<'EOF2'
#!/usr/bin/env bash
echo "install $*" >> "$CALLS"
args=(); mkdir_mode=0
while [ $# -gt 0 ]; do
  case "$1" in -d) mkdir_mode=1; shift ;; -m|-o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
done
if [ "$mkdir_mode" = 1 ]; then mkdir -p "${args[@]}"; else cp "${args[0]}" "${args[1]}"; fi
EOF2
# chown / chmod: root-only against another user; logged, not done.
printf '#!/usr/bin/env bash\necho "chown $*" >> "$CALLS"\n' > "$BIN/chown"
printf '#!/usr/bin/env bash\necho "chmod $*" >> "$CALLS"\n' > "$BIN/chmod"
cat > "$BIN/systemctl" <<'EOF2'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
case "$1" in
  is-enabled) [ -f "$TMP/unit-enabled" ] ;;
  enable) touch "$TMP/unit-enabled" ;;
esac
EOF2
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

DESK_HOME="$TMP/home/watcher-desk"; UNITDIR="$TMP/units"
export DESK_HOME DESK_UNIT_DIR="$UNITDIR"
# The `su - watcher-desk -c "git config --global …"` step writes the desk user's own
# ~/.gitconfig; under the su stub that is OUR HOME, so HOME points into $TMP for every run.
RUNNER_HOME="$TMP/runner-home"; mkdir -p "$RUNNER_HOME"

fresh() { rm -rf "$TMP/home" "$UNITDIR" "$TMP/user-exists" "$TMP/unit-enabled" "$TMP/no-journal-group"; mkdir -p "$UNITDIR"; : > "$CALLS"; }
run() {
  : > "$CALLS"
  # WATCHER_DIR is this very checkout: deploy/desk/* and deploy/watcher-desk.service are the
  # real files. The .git check at the top of install.sh runs as whoever we are (no watcher
  # user here) and reads nothing we care about.
  HOME="$RUNNER_HOME" WATCHER_DIR="$REPO" WATCHER_REPO="https://example.test/dummy.git" INSTALL_LIB_ONLY=1 \
    bash -c "source '$INSTALL'; install_desk" > "$TMP/out" 2>&1
  echo $? > "$TMP/rc"
}
rc() { cat "$TMP/rc"; }
called() { grep -q -- "$1" "$CALLS"; }
not_called() { ! grep -q -- "$1" "$CALLS"; }

echo "install_desk"

# 1. First install: the user is made (in the journal group), the folder is a git repo with
#    both files committed, the unit is installed, enabled and started.
fresh; run
[ "$(rc)" = 0 ] && pass "first install: exit 0" || flunk "first install exit" "rc=$(rc) $(cat "$TMP/out")"
called "useradd -m -d $DESK_HOME -s /bin/bash -G systemd-journal watcher-desk" && pass "…creates watcher-desk with its own home, in systemd-journal" || flunk "useradd" "$(cat "$CALLS")"
cmp -s "$REPO/deploy/desk/CLAUDE.md" "$DESK_HOME/desk/CLAUDE.md" && pass "…desk/CLAUDE.md is deploy/desk/CLAUDE.md" || flunk "CLAUDE.md not installed"
cmp -s "$REPO/deploy/desk/settings.json" "$DESK_HOME/desk/.claude/settings.json" && pass "…desk/.claude/settings.json is deploy/desk/settings.json" || flunk "settings.json not installed"
called "install -d -o root -g root -m 755 $DESK_HOME/desk/.claude" && called "install -m 644 -o root -g root $REPO/deploy/desk/settings.json" && pass "…and .claude/ and settings.json are root's, so the desk cannot rewrite its own allow list" || flunk "settings not root-owned" "$(grep install "$CALLS")"
called "chown root:watcher-desk $DESK_HOME" && called "chmod 1775 $DESK_HOME" && called "install -d -o root -g watcher-desk -m 1775 $DESK_HOME/desk" && called "install -m 644 -o root -g root $REPO/deploy/desk/CLAUDE.md" && pass "…the home and the desk folder are root's, sticky, group-writable (nothing root put there can be renamed away), CLAUDE.md root's" || flunk "home/desk ownership" "$(grep -E 'chown|chmod|install -d' "$CALLS")"
[ "$(git config --file "$RUNNER_HOME/.gitconfig" --get-all safe.directory 2>/dev/null)" = "$DESK_HOME/desk" ] && pass "…and the desk user's own ~/.gitconfig names the root-owned repo safe (git's dubious-ownership check)" || flunk "safe.directory not set" "$(cat "$RUNNER_HOME/.gitconfig" 2>/dev/null)"
ok=1; for f in .bashrc .bash_profile .bash_login .profile .bash_logout; do called "install -m 644 -o root -g root /dev/null $DESK_HOME/$f" && [ -f "$DESK_HOME/$f" ] && [ ! -s "$DESK_HOME/$f" ] || ok=0; done
[ "$ok" = 1 ] && pass "…the shell startup files are root's and empty (the Bash tool loads ~/.bashrc)" || flunk "startup files not pinned" "$(grep dev/null "$CALLS")"
called "install -d -o root -g watcher-desk -m 1775 $DESK_HOME/.config $DESK_HOME/.config/gh" && [ "$(cat "$DESK_HOME/.config/gh/config.yml")" = "$(printf 'git_protocol: https\nversion: "1"')" ] && called "install -m 644 -o root -g root $DESK_HOME/.config/gh/config.yml.new" && pass "…and gh's config.yml is root's (its browser: setting is a program gh would run)" || flunk "gh config not pinned" "$(grep config "$CALLS")"
cmp -s "$REPO/bin/desk.sh" "$DESK_HOME/.desk.sh.installed" && pass "…a copy of the installed desk.sh is kept, to notice a change next deploy" || flunk "no installed desk.sh copy"
called "install -d -o root -g watcher-desk -m 1775 $DESK_HOME/.claude" && [ "$(cat "$DESK_HOME/.claude/settings.json")" = "{}" ] && called "install -m 644 -o root -g root $DESK_HOME/.claude/settings.json.new $DESK_HOME/.claude/settings.json" && pass "…and the USER-scope ~/.claude/settings.json is root's empty {} in a root-owned sticky folder (no self-widening through the user scope)" || flunk "user-scope settings not pinned" "$(grep -i 'claude' "$CALLS")"
[ -d "$DESK_HOME/desk/.git" ] && [ "$(git -C "$DESK_HOME/desk" rev-list --count HEAD 2>/dev/null)" = 1 ] && pass "…the desk folder is a git repo with the files committed (Remote Control wants a git root)" || flunk "desk folder is not a committed git repo"
called "su - watcher-desk -c git -C '$DESK_HOME/desk' init" && pass "…git init ran as watcher-desk, through su" || flunk "init not through su" "$(cat "$CALLS")"
cmp -s "$REPO/deploy/watcher-desk.service" "$UNITDIR/watcher-desk.service" && pass "…the unit is installed" || flunk "unit not installed"
called "systemctl daemon-reload" && called "systemctl enable --now watcher-desk.service" && pass "…daemon-reload, then enable --now" || flunk "not enabled" "$(cat "$CALLS")"
not_called "systemctl restart" && pass "…and no restart on the first install" || flunk "restarted on first install" "$(cat "$CALLS")"

# 2. A second install with nothing changed: no useradd, no copy, no restart — a deploy does
#    not cut off a conversation.
run
[ "$(rc)" = 0 ] && pass "second install, nothing changed: exit 0" || flunk "second install exit" "rc=$(rc)"
not_called "useradd" && pass "…no second useradd" || flunk "useradd again" "$(cat "$CALLS")"
not_called "install -m 644" && pass "…no file copied" || flunk "copied unchanged files" "$(cat "$CALLS")"
[ "$(git config --file "$RUNNER_HOME/.gitconfig" --get-all safe.directory | wc -l | tr -d ' ')" = 1 ] && pass "…and safe.directory is not added a second time" || flunk "safe.directory duplicated" "$(cat "$RUNNER_HOME/.gitconfig")"
not_called "install -m 644 -o root -g root /dev/null" && not_called "config.yml.new" && pass "…the pinned startup files and gh config are left alone when already right" || flunk "re-pinned unchanged files" "$(grep -E 'dev/null|config.yml' "$CALLS")"
not_called "systemctl restart" && not_called "systemctl enable" && pass "…and the desk is left running (no restart, no re-enable)" || flunk "touched the unit" "$(cat "$CALLS")"

# 3. The desk's CLAUDE.md changed on the box (an older deploy's copy): it is replaced,
#    committed, and the desk restarted.
echo "stale" > "$DESK_HOME/desk/CLAUDE.md"
run
cmp -s "$REPO/deploy/desk/CLAUDE.md" "$DESK_HOME/desk/CLAUDE.md" && pass "a changed CLAUDE.md: replaced with the deployed one" || flunk "CLAUDE.md not refreshed"
called "systemctl restart watcher-desk.service" && pass "…and the desk is restarted for it" || flunk "no restart after a change" "$(cat "$CALLS")"
# The stale copy was never committed, so putting the deployed file back leaves nothing to
# commit — the repo simply tracks what is on disk, which is the property that matters.
[ -z "$(git -C "$DESK_HOME/desk" status --porcelain)" ] && pass "…and the desk repo tracks exactly what is on disk (clean after the refresh)" || flunk "desk repo dirty after a refresh" "$(git -C "$DESK_HOME/desk" status --porcelain)"

# 3a. A widened user-scope settings file (whatever wrote it) is put back to {} and counts as a change
echo '{"permissions":{"allow":["Bash"]}}' > "$DESK_HOME/.claude/settings.json"
run
[ "$(cat "$DESK_HOME/.claude/settings.json")" = "{}" ] && called "systemctl restart watcher-desk.service" && pass "a widened ~/.claude/settings.json is reset to {} and the desk restarted" || flunk "user-scope settings not reset" "$(cat "$DESK_HOME/.claude/settings.json")"

# 3b. A stray file the session left in the folder is not committed (only the two desk files are)
echo "scratch" > "$DESK_HOME/desk/notes.txt"
run
git -C "$DESK_HOME/desk" ls-files --error-unmatch notes.txt >/dev/null 2>&1 && flunk "a stray file was committed" || pass "a stray file left by the session is never committed"
rm -f "$DESK_HOME/desk/notes.txt"

# 3c. desk.sh itself changed since the last deploy: that counts, and the desk restarts
echo "# older desk.sh" > "$DESK_HOME/.desk.sh.installed"
run
called "systemctl restart watcher-desk.service" && cmp -s "$REPO/bin/desk.sh" "$DESK_HOME/.desk.sh.installed" && pass "a changed bin/desk.sh restarts the desk and refreshes the kept copy" || flunk "desk.sh change not noticed" "$(cat "$CALLS")"

# 4. Only the unit changed: installed, daemon-reload, restart; files untouched
echo "# old unit" > "$UNITDIR/watcher-desk.service"
run
cmp -s "$REPO/deploy/watcher-desk.service" "$UNITDIR/watcher-desk.service" && called "systemctl daemon-reload" && called "systemctl restart watcher-desk.service" && pass "a changed unit: reinstalled, daemon-reload, restart" || flunk "unit change not applied" "$(cat "$CALLS")"
not_called "deploy/desk/CLAUDE.md" && not_called "deploy/desk/settings.json" && pass "…and the desk files were not copied again" || flunk "files copied for a unit change" "$(cat "$CALLS")"

# 5. No systemd-journal group on this box: useradd without -G
fresh; touch "$TMP/no-journal-group"; run
called "useradd -m -d $DESK_HOME -s /bin/bash watcher-desk" && pass "no systemd-journal group: useradd with no -G" || flunk "useradd -G with a missing group" "$(cat "$CALLS")"

# 6. useradd fails: a warning, return 1, nothing else touched (install.sh's caller has `|| true`)
fresh; printf '#!/usr/bin/env bash\necho "useradd $*" >> "$CALLS"; exit 1\n' > "$BIN/useradd"
run
[ "$(rc)" = 1 ] && grep -q "could not create the watcher-desk user" "$TMP/out" && pass "useradd fails: warns, returns 1" || flunk "useradd failure" "rc=$(rc) $(cat "$TMP/out")"
not_called "systemctl" && pass "…and never reaches systemctl" || flunk "went on after a failed useradd" "$(cat "$CALLS")"

echo
if [ "$fail" -eq 0 ]; then echo "$n cases, all passed"; else echo "$n cases, FAILURES ABOVE"; fi
exit "$fail"
