#!/usr/bin/env bash
# The desk (card #205): what watcher-desk.service runs. A read-only Remote Control session
# on the box for the owner's own Claude sessions — see deploy/watcher-desk.service's header
# for why it exists and why it is its own user.
#
#   bin/desk.sh            # on the box, as watcher-desk, from the unit; never by a tick
#
# Three things, in order:
#   1. Trust the desk folder in this user's own ~/.claude.json, so Remote Control never
#      waits on a trust dialog nobody can see (it has no terminal under systemd).
#   2. Wait for the login. Remote Control needs a full browser login of the owner's account
#      (`claude auth login`, done once as watcher-desk); until then this says so in the
#      journal once and checks again every DESK_WAIT_SEC, so the desk comes up on its own a
#      few minutes after the login lands, with nobody touching systemctl.
#   3. exec `claude remote-control` in the desk folder, named for the box, permission mode
#      `default` — a command outside the folder's allow list asks the owner, in the session,
#      on their phone, which is the right answer for a console; the deny list in
#      deploy/desk/settings.json refuses the loop's writes outright. The guard mod
#      (mods/guard, card #204) is loaded too when the deployed release has it.
#
# Overridable for bin/test-desk.sh (no network, `claude` and `gh` stubbed):
#   DESK_DIR          the folder (default $HOME/desk)      DESK_CLAUDE   the claude binary
#   DESK_WAIT_SEC     seconds between login checks (300)   DESK_NAME     the session name
#   DESK_MAX_WAITS    stop after this many waits, exit 3 (unset = forever)
#   WATCHER_DIR       the deployed release (/opt/watcher), for the guard mod
set -uo pipefail

DESK_DIR="${DESK_DIR:-$HOME/desk}"
DESK_CLAUDE="${DESK_CLAUDE:-claude}"
DESK_WAIT_SEC="${DESK_WAIT_SEC:-300}"
DESK_NAME="${DESK_NAME:-$(hostname -s 2>/dev/null || hostname) desk}"
DESK_CAPACITY="${DESK_CAPACITY:-4}"
WATCHER_DIR="${WATCHER_DIR:-/opt/watcher}"

say() { printf 'desk: %s\n' "$*"; }

[ -d "$DESK_DIR/.git" ] || { say "no desk folder at $DESK_DIR (deploy/install.sh makes it, as a git repo — Remote Control needs one)"; exit 1; }
cd "$DESK_DIR" || exit 1

# 1. trust — merged into whatever ~/.claude.json already holds, never overwritten.
python3 - "$HOME/.claude.json" "$DESK_DIR" <<'PY' || say "could not seed trust for $DESK_DIR in $HOME/.claude.json (continuing)"
import json, os, sys
path, folder = sys.argv[1], sys.argv[2]
try:
    with open(path) as f: data = json.load(f)
except (FileNotFoundError, ValueError):
    data = {}
projects = data.setdefault("projects", {})
entry = projects.setdefault(folder, {})
if entry.get("hasTrustDialogAccepted") is not True:
    entry["hasTrustDialogAccepted"] = True
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, indent=2)
    os.replace(tmp, path)
PY

# 2. the login
logged_in() { "$DESK_CLAUDE" auth status 2>/dev/null | grep -Eq '"loggedIn": *true'; }
waits=0; said=0
until logged_in; do
  if [ "$said" = 0 ]; then
    say "not logged in yet. Once, on the box: sudo -u watcher-desk -H claude auth login (a browser login of the owner's own account — the lanes' token is inference-only and Remote Control refuses it). Checking again every ${DESK_WAIT_SEC}s, quietly."
    said=1
  fi
  if [ -n "${DESK_MAX_WAITS:-}" ] && [ "$waits" -ge "$DESK_MAX_WAITS" ]; then exit 3; fi
  waits=$((waits + 1))
  sleep "$DESK_WAIT_SEC"
done
gh auth status >/dev/null 2>&1 || say "gh is not logged in for watcher-desk — board and gh reads will fail until, once: sudo -u watcher-desk -H gh auth login"

# 3. the session
if [ -d "$WATCHER_DIR/mods/guard" ]; then
  export CLAUDE_CODE_PLUGIN_DIRS="$WATCHER_DIR/mods/guard"
fi
say "starting Remote Control as '$DESK_NAME' in $DESK_DIR"
exec "$DESK_CLAUDE" remote-control --name "$DESK_NAME" --spawn same-dir --capacity "$DESK_CAPACITY" --permission-mode default
