#!/usr/bin/env bash
#
# deploy/mac-setup.sh [--dry-run]   (card #288)
#
# Points the owner's Claude sessions on this Mac at the desk App, roloc-desk-author, so every
# write one of them makes on GitHub — a comment, a card move, a commit, a PR — is signed by that
# App: never the owner's own login (`roloc` is his hand alone), never a lane's App, never a
# machine account. Run as yourself, on the Mac, after
#
#   python3 deploy/app-setup.py desk --all-repos
#
# has written the App's key under ~/.config/roloc-apps/desk/. Idempotent: run it again any time;
# it says "unchanged" for what is already in place. --dry-run prints every change and writes
# nothing. Never run by a tick, never on the box.
#
# What it writes:
#   ~/.claude/settings.json, its "env" (every other key is kept):
#     WATCHER_APP_PROJECT=desk        bin/gh-app-token.sh mints the desk App whatever checkout the
#                                     session is in; without it the git remote would pick that
#                                     project's lane App, so a session in the riffmap checkout
#                                     would write as roloc-riffmap-author
#     GIT_AUTHOR_*, GIT_COMMITTER_*   the App's identity, from `bin/gh-app-token.sh desk author --identity`
#     GIT_CONFIG_COUNT, _KEY_n, _VALUE_n   credential.helper emptied, then bin/shim/gh-credential
#     GH_CONFIG_DIR                   removed: it named the suspended roloc-desk login
#   ~/.zshenv: one guarded line that puts bin/shim first on PATH only when WATCHER_APP_PROJECT is
#     set — a Claude session's `gh` is the shim, the owner's own terminal `gh` is untouched.
#
# Test seams: MAC_SETUP_SETTINGS (the settings file), MAC_SETUP_ZSHENV (the profile), HOME (where
# ~/.config/roloc-apps/desk/ is looked for), WATCHER_HOME (the checkout; default: this script's parent).
set -euo pipefail

dry=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry=1 ;;
    *) echo "usage: deploy/mac-setup.sh [--dry-run]" >&2; exit 2 ;;
  esac
done

WATCHER_HOME="${WATCHER_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SETTINGS="${MAC_SETUP_SETTINGS:-$HOME/.claude/settings.json}"
ZSHENV="${MAC_SETUP_ZSHENV:-$HOME/.zshenv}"
TOKEN="$WATCHER_HOME/bin/gh-app-token.sh"
[ -x "$TOKEN" ] || { echo "mac-setup: $TOKEN is missing; WATCHER_HOME must be the watcher checkout" >&2; exit 1; }

# The App's identity is the proof it is set up: the token script reads only the config for it.
if ! identity="$(GH_APP_DIR= WATCHER_APP_PROJECT=desk "$TOKEN" desk author --identity 2>&1)"; then
  echo "mac-setup: the desk App is not set up on this Mac ($identity)" >&2
  echo "  first: python3 $WATCHER_HOME/deploy/app-setup.py desk --all-repos" >&2
  exit 1
fi
name="${identity%% <*}"
email="${identity#*<}"; email="${email%>}"

[ "$dry" = 1 ] && echo "mac-setup: dry run, writing nothing"
echo "mac-setup: the desk App is $name"

# The settings file and the profile are edited by Python: a JSON merge that keeps every other
# key, and one marked line in the profile replaced or appended, each written whole and atomically.
MAC_SETUP_NAME="$name" MAC_SETUP_EMAIL="$email" MAC_SETUP_HOME="$WATCHER_HOME" MAC_SETUP_DRY="$dry" \
MAC_SETUP_SETTINGS="$SETTINGS" MAC_SETUP_ZSHENV="$ZSHENV" python3 - <<'PY'
import json, os, sys

settings, zshenv = os.environ["MAC_SETUP_SETTINGS"], os.environ["MAC_SETUP_ZSHENV"]
home, dry = os.environ["MAC_SETUP_HOME"], os.environ["MAC_SETUP_DRY"] == "1"
name, email = os.environ["MAC_SETUP_NAME"], os.environ["MAC_SETUP_EMAIL"]


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".new"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# -- settings.json: the env keys a session needs, nothing else touched
try:
    with open(settings) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("not an object")
except FileNotFoundError:
    data = {}
except ValueError as e:
    sys.exit(f"mac-setup: {settings} is not valid JSON ({e}); fix it by hand, nothing written")
env = data.get("env")
if not isinstance(env, dict):
    env = {}
want = {
    "WATCHER_APP_PROJECT": "desk",
    "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
    "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": f"!{home}/bin/shim/gh-credential",
}
changes = [k for k, v in want.items() if env.get(k) != v]
if "GH_CONFIG_DIR" in env:
    changes.append("-GH_CONFIG_DIR")
if changes:
    new_env = {k: v for k, v in env.items() if k != "GH_CONFIG_DIR"}
    new_env.update(want)
    data["env"] = new_env
    print(f"settings: {settings}: " + ", ".join(changes))
    if not dry:
        write(settings, json.dumps(data, indent=2) + "\n")
else:
    print(f"settings: {settings}: unchanged")

# -- .zshenv: the shim first on PATH, only for a session that names an App
marker = "# watcher card #288: a Claude session's gh is the App shim"
line = f'if [ -n "${{WATCHER_APP_PROJECT:-}}" ]; then export PATH="{home}/bin/shim:$PATH"; fi  {marker}'
try:
    with open(zshenv) as f:
        lines = f.read().split("\n")
except FileNotFoundError:
    lines = [""]
have = [i for i, l in enumerate(lines) if marker in l]
if have and lines[have[0]] == line and len(have) == 1:
    print(f"profile: {zshenv}: unchanged")
else:
    kept = [l for i, l in enumerate(lines) if i not in have]
    if kept and kept[-1] == "":
        kept.pop()
    kept.append(line)
    print(f"profile: {zshenv}: " + ("the shim line replaced" if have else "the shim line added"))
    if not dry:
        write(zshenv, "\n".join(kept) + "\n")
PY

if [ "$dry" = 1 ]; then
  echo "mac-setup: dry run done; nothing written"
else
  echo "mac-setup: done. A Claude session started from now on writes to GitHub as $name;"
  echo "  the one you are in keeps its old environment until it is restarted. Your own terminal's gh is unchanged."
fi
