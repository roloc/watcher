#!/usr/bin/env bash
#
# Offline test for deploy/mac-setup.sh (card #288): the desk App's identity read from a fixture
# key folder, the settings file merged (other keys kept, the suspended login's GH_CONFIG_DIR gone),
# the profile line that puts the shim first on PATH only for a session that names an App, a dry
# run that writes nothing, and a second run that changes nothing.
#
#   bash bin/test-mac-setup.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SCRIPT="$ROOT/deploy/mac-setup.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0; n=0
say()   { printf '  %-84s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }
check() { if eval "$2"; then pass "$1"; else flunk "$1" "${3:-}"; fi; }

HOME_DIR="$TMP/home"; mkdir -p "$HOME_DIR/.claude"
SETTINGS="$HOME_DIR/.claude/settings.json"
ZSHENV="$HOME_DIR/.zshenv"
export HOME="$HOME_DIR" MAC_SETUP_SETTINGS="$SETTINGS" MAC_SETUP_ZSHENV="$ZSHENV" WATCHER_HOME="$ROOT"
unset GH_APP_DIR WATCHER_APP_PROJECT PROJECT_NAME
run() { "$SCRIPT" "$@" >"$TMP/out" 2>"$TMP/err"; echo $?; }
envkey() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("env",{}).get(sys.argv[2],"<absent>"))' "$SETTINGS" "$1"; }

# The settings file as this Mac had it: the suspended machine account's login and identity, a
# plugin dir and a permissions block that must survive untouched.
cat > "$SETTINGS" <<'EOJ'
{
  "permissions": {"allow": ["Bash(ls:*)"]},
  "env": {
    "GH_CONFIG_DIR": "/Users/me/.config/gh-desk",
    "GIT_AUTHOR_NAME": "roloc-desk",
    "GIT_AUTHOR_EMAIL": "1+roloc-desk@users.noreply.github.com",
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "credential.helper",
    "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "credential.helper",
    "GIT_CONFIG_VALUE_1": "!gh auth git-credential",
    "CLAUDE_CODE_PLUGIN_DIRS": "/Users/me/Projects/watcher/mods/owner-pane"
  },
  "model": "opus"
}
EOJ
cp "$SETTINGS" "$TMP/settings.before"
printf 'export EDITOR=vim\n' > "$ZSHENV"

echo "1. no desk App on this Mac: exit 1, says what to run first, writes nothing"
RC="$(run)"
check "exit 1 and the app-setup command named" '[ "$RC" = 1 ] && grep -q "app-setup.py desk --all-repos" "$TMP/err"' "rc=$RC $(cat "$TMP/err")"
check "the settings file is untouched" 'cmp -s "$SETTINGS" "$TMP/settings.before"'
check "the profile is untouched" '[ "$(cat "$ZSHENV")" = "export EDITOR=vim" ]'

echo "2. the App set up: settings merged, the profile line added"
mkdir -p "$HOME_DIR/.config/roloc-apps/desk"
cat > "$HOME_DIR/.config/roloc-apps/desk/config" <<'EOC'
AUTHOR_APP_ID=7777
AUTHOR_INSTALLATION_ID=8888
AUTHOR_SLUG=roloc-desk-author
AUTHOR_BOT_ID=9999
EOC
RC="$(run)"
check "exit 0, and it names the App" '[ "$RC" = 0 ] && grep -q "the desk App is roloc-desk-author\[bot\]" "$TMP/out"' "rc=$RC $(cat "$TMP/out" "$TMP/err")"
check "WATCHER_APP_PROJECT=desk, so the checkout's remote never picks the App" '[ "$(envkey WATCHER_APP_PROJECT)" = desk ]'
check "the commit identity is the App's" '[ "$(envkey GIT_AUTHOR_NAME)" = "roloc-desk-author[bot]" ] && [ "$(envkey GIT_AUTHOR_EMAIL)" = "9999+roloc-desk-author[bot]@users.noreply.github.com" ] && [ "$(envkey GIT_COMMITTER_NAME)" = "roloc-desk-author[bot]" ] && [ "$(envkey GIT_COMMITTER_EMAIL)" = "9999+roloc-desk-author[bot]@users.noreply.github.com" ]' "$(envkey GIT_AUTHOR_NAME) $(envkey GIT_COMMITTER_EMAIL)"
check "git's credential helper is the shim's" '[ "$(envkey GIT_CONFIG_VALUE_1)" = "!$ROOT/bin/shim/gh-credential" ] && [ "$(envkey GIT_CONFIG_KEY_1)" = credential.helper ] && [ "$(envkey GIT_CONFIG_COUNT)" = 2 ]' "$(envkey GIT_CONFIG_VALUE_1)"
check "GH_CONFIG_DIR (the suspended login) is gone" '[ "$(envkey GH_CONFIG_DIR)" = "<absent>" ]'
check "every other key is kept: the plugin dir, permissions, model" '[ "$(envkey CLAUDE_CODE_PLUGIN_DIRS)" = /Users/me/Projects/watcher/mods/owner-pane ] && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d[\"permissions\"]=={\"allow\":[\"Bash(ls:*)\"]} and d[\"model\"]==\"opus\" else 1)" "$SETTINGS"'
check "the settings file is still JSON a human can read (indented, one trailing newline)" 'python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$SETTINGS" && [ "$(tail -c1 "$SETTINGS" | od -An -c | tr -d " ")" = "\\n" ] && grep -q "^  \"env\": {" "$SETTINGS"'
check "the profile keeps its own line and gains the guarded shim line" '[ "$(head -1 "$ZSHENV")" = "export EDITOR=vim" ] && grep -q "bin/shim:\$PATH" "$ZSHENV" && grep -q "WATCHER_APP_PROJECT" "$ZSHENV" && [ "$(wc -l < "$ZSHENV" | tr -d " ")" = 2 ]' "$(cat "$ZSHENV")"
check "the output says what to expect from the next session" 'grep -q "started from now on writes to GitHub as roloc-desk-author\[bot\]" "$TMP/out"' "$(cat "$TMP/out")"

echo "3. the profile line, sourced: the shim leads PATH only for a session that names an App"
P1="$(WATCHER_APP_PROJECT=desk bash -c "source \"$ZSHENV\"; printf %s \"\$PATH\"")"
P2="$(env -u WATCHER_APP_PROJECT bash -c "source \"$ZSHENV\"; printf %s \"\$PATH\"")"
check "with WATCHER_APP_PROJECT: bin/shim first" '[ "${P1#$ROOT/bin/shim:}" != "$P1" ]' "$P1"
check "without it (the owner's own terminal): PATH untouched" '[ "$P2" = "$PATH" ]' "$P2"

echo "4. a second run changes nothing and says so"
cp "$SETTINGS" "$TMP/settings.once"; cp "$ZSHENV" "$TMP/zshenv.once"
RC="$(run)"
check "exit 0, both files byte-identical" '[ "$RC" = 0 ] && cmp -s "$SETTINGS" "$TMP/settings.once" && cmp -s "$ZSHENV" "$TMP/zshenv.once"'
check "…and both reported unchanged" 'grep -q "^settings: .*: unchanged" "$TMP/out" && grep -q "^profile: .*: unchanged" "$TMP/out"' "$(cat "$TMP/out")"

echo "5. a moved checkout: the profile line is replaced, never doubled"
RC="$(WATCHER_HOME="$TMP/elsewhere" run)"
check "exit 1 when the checkout has no token script (WATCHER_HOME must be the watcher)" '[ "$RC" = 1 ]'
mkdir -p "$TMP/elsewhere/bin" && cp "$ROOT/bin/gh-app-token.sh" "$TMP/elsewhere/bin/"
RC="$(WATCHER_HOME="$TMP/elsewhere" run)"
check "one shim line, pointing at the new checkout" '[ "$RC" = 0 ] && [ "$(grep -c "bin/shim:" "$ZSHENV")" = 1 ] && grep -q "$TMP/elsewhere/bin/shim" "$ZSHENV" && grep -q "the shim line replaced" "$TMP/out"' "$(cat "$ZSHENV")"
check "and the credential helper follows it" '[ "$(envkey GIT_CONFIG_VALUE_1)" = "!$TMP/elsewhere/bin/shim/gh-credential" ]'
run >/dev/null   # back to this checkout for the cases below

echo "6. --dry-run prints the changes and writes nothing"
cp "$TMP/settings.before" "$SETTINGS"; printf 'export EDITOR=vim\n' > "$ZSHENV"
RC="$(run --dry-run)"
check "exit 0, the changes listed" '[ "$RC" = 0 ] && grep -q "dry run" "$TMP/out" && grep -q "^settings: .*WATCHER_APP_PROJECT" "$TMP/out" && grep -q "GH_CONFIG_DIR" "$TMP/out" && grep -q "^profile: .*added" "$TMP/out"' "$(cat "$TMP/out")"
check "nothing written" 'cmp -s "$SETTINGS" "$TMP/settings.before" && [ "$(cat "$ZSHENV")" = "export EDITOR=vim" ]'

echo "7. no settings file, no profile: both made"
rm -f "$SETTINGS" "$ZSHENV"
RC="$(run)"
check "exit 0, the settings file holds the env and nothing else" '[ "$RC" = 0 ] && [ "$(envkey WATCHER_APP_PROJECT)" = desk ] && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if list(d)==[\"env\"] else 1)" "$SETTINGS"' "$(cat "$SETTINGS" 2>&1)"
check "the profile is the one line" '[ "$(wc -l < "$ZSHENV" | tr -d " ")" = 1 ] && grep -q "bin/shim" "$ZSHENV"'

echo "8. a settings file that is not JSON: exit 1, untouched, named"
printf '{"env": {\n' > "$SETTINGS"; cp "$SETTINGS" "$TMP/settings.broken"
RC="$(run)"
check "exit 1 and the file named" '[ "$RC" = 1 ] && grep -q "not valid JSON" "$TMP/err"' "rc=$RC $(cat "$TMP/err")"
check "the file is as it was" 'cmp -s "$SETTINGS" "$TMP/settings.broken"'

echo "9. a bad argument is a usage error"
RC="$(run --force)"
check "exit 2" '[ "$RC" = 2 ]'

if [ "$fail" = 0 ]; then echo "test-mac-setup.sh: $n checks, all passed"; else echo "test-mac-setup.sh: $n checks, SOME FAILED"; fi
exit $fail
