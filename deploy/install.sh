#!/usr/bin/env bash
# Run as root on the box, idempotent. Assumes the base tools are present (git, gh, node,
# python3, a JDK if the project needs one, and `npm i -g @anthropic-ai/claude-code`).
#
#   /opt/watcher                  this repo (git clone or pull) — or, once deploy/autodeploy.sh
#                                 has shipped at least one commit (card #106), a symlink onto
#                                 one of its own release folders; see WATCHER_DIR below
#   /etc/watcher/env              CLAUDE_CODE_OAUTH_TOKEN=…  (root:watcher 640; written by hand)
#   /etc/watcher/floor.env        the floor page's own: PROXMOX_URL=… PROXMOX_TOKEN=… (optional;
#                                 read by watcher-floor.service only, never by a lane)
#   /home/watcher/.claude/        commands/ and agents/ symlinked from the repo, so every
#                                 project on the box gets /watch and the reviewer
#   /home/watcher/projects/<p>    each project's main checkout (cloned by `install.sh <p>`)
set -euo pipefail
# Card #106: deploy/autodeploy.sh's own release-folder swap runs this from a FRESH clone it
# just made, never from /opt/watcher itself — WATCHER_DIR is how it says so (every
# self-reference below reads $WATCHER_DIR, never the literal path). An ordinary by-hand run
# (no override) still means /opt/watcher, exactly as always.
WATCHER_DIR="${WATCHER_DIR:-/opt/watcher}"
# Same default deploy/autodeploy.sh's own DEPLOY_CANARY_DIR and bin/spawn.sh's own
# WATCHER_CANARY_DIR already use (card #46).
WATCHER_CANARY_DIR="${WATCHER_CANARY_DIR:-/opt/watcher-canary}"
# Card #157: no fork should ever clone `roloc/watcher` by default. When $WATCHER_DIR is
# already a git checkout (the ordinary by-hand install: clone your own fork there first,
# per README, then run this), its own `origin` IS the fork's URL, so that is read instead
# of asked for. Only when there is no checkout to read one from — nothing at $WATCHER_DIR
# yet, or a copy with no `.git` at all (README's "or rsync it in") — does this require
# WATCHER_REPO.
if [ -d "$WATCHER_DIR/.git" ]; then
  if [ -n "${WATCHER_REPO:-}" ]; then
    REPO_URL="$WATCHER_REPO"
  else
    # Every other git call on $WATCHER_DIR in this file runs as the watcher user (see
    # canary_clone's own comment below): git refuses to read a repo owned by someone
    # else ("dubious ownership"), and by the time the watcher user exists, $WATCHER_DIR
    # already is watcher's (autodeploy.sh's release folders always are — this ran as
    # root straight on one once and broke every deploy). On the very first run, before
    # `useradd` below has even happened, nothing has chowned anything yet, so whoever
    # is running this script (root, per the header above) still owns the checkout it
    # just made by hand — a plain call is correct there, and only there. `|| true` on
    # the git call itself: under `set -e`, a failing command substitution would kill
    # the whole script right here, for a value nothing downstream even reads in this
    # branch (next comment).
    if id watcher >/dev/null 2>&1; then
      REPO_URL="$(su - watcher -c "git -C '$WATCHER_DIR' remote get-url origin" 2>/dev/null || true)"
    else
      REPO_URL="$(git -C "$WATCHER_DIR" remote get-url origin 2>/dev/null || true)"
    fi
    # Never required here (no `:?`, unlike the else branch below): REPO_URL is read ONLY
    # at the one-time clone further down, which this branch's own `.git` check already
    # rules out — so a git/su hiccup deriving it for its own sake must never abort an
    # install that was never going to read it.
  fi
else
  REPO_URL="${WATCHER_REPO:?install.sh: set WATCHER_REPO=<the git URL of your fork> — $WATCHER_DIR is missing, or not a git checkout (a copy made by rsync has no origin to read), so there is nothing to derive it from}"
fi

# canary_clone: makes $WATCHER_CANARY_DIR a clone of $WATCHER_DIR — fast, local, no network
# and so no login root never has — with its own `origin` set to the TRUE upstream, read from
# $WATCHER_DIR's own remote before the clone touches anything (same shape
# deploy/autodeploy.sh's `release_new` already clones a release folder in). Card #120: the
# previous code cloned $REPO_URL straight over HTTPS as root, which fails every time for a
# private repo (no login), leaving the canary checkout permanently missing.
#
# Every git call on $WATCHER_DIR runs as the watcher user, same as every other git call on
# it elsewhere in this file (the pull above): git itself refuses to even read a repo owned
# by a different user than the one running it ("dubious ownership"), and by this point
# $WATCHER_DIR is watcher's. Root's own part is only making $WATCHER_CANARY_DIR exist and
# watcher-owned first — watcher cannot mkdir directly under root-owned /opt — with a plain
# mkdir fallback for when there is no watcher account at all (bin/test-deploy.sh's offline
# run, no root). A failed clone or origin fix removes the directory rather than leaving a
# half-made one the `-d` guard below would mistake for done.
canary_clone() {
  local real_origin
  real_origin="$(su - watcher -c "git -C '$WATCHER_DIR' remote get-url origin")" || return 1
  install -d -o watcher -g watcher "$WATCHER_CANARY_DIR" 2>/dev/null || mkdir -p "$WATCHER_CANARY_DIR"
  if su - watcher -c "git clone -q '$WATCHER_DIR' '$WATCHER_CANARY_DIR'" \
       && su - watcher -c "git -C '$WATCHER_CANARY_DIR' remote set-url origin '$real_origin'"; then
    return 0
  fi
  rm -rf "$WATCHER_CANARY_DIR"
  return 1
}

# bin/test-deploy.sh sources this file with INSTALL_LIB_ONLY=1 to call canary_clone directly,
# with no root, no watcher user and no network — stopping here is what makes that possible.
if [ "${INSTALL_LIB_ONLY:-0}" = 1 ]; then return 0 2>/dev/null || exit 0; fi

id watcher >/dev/null 2>&1 || useradd -m -s /bin/bash watcher

if [ -d "$WATCHER_DIR/.git" ] && [ "${INSTALL_NO_PULL:-0}" = 1 ]; then
  # deploy/autodeploy.sh has already cloned this exact commit CI and the replay suite
  # passed into $WATCHER_DIR; a pull here could take it past that commit to one nothing
  # checked.
  :
elif [ -d "$WATCHER_DIR/.git" ]; then
  # As the watcher user: it holds the gh login, and the checkout is its own.
  su - watcher -c "git -C '$WATCHER_DIR' pull -q --ff-only" || echo "warning: $WATCHER_DIR pull failed (watcher user not logged in to gh yet?) — using what is there" >&2
elif [ ! -d "$WATCHER_DIR" ]; then
  git clone -q "$REPO_URL" "$WATCHER_DIR" || { echo "clone failed — rsync the repo to $WATCHER_DIR instead" >&2; exit 1; }
fi
chown -R watcher:watcher "$WATCHER_DIR"
chmod +x "$WATCHER_DIR"/bin/*.sh "$WATCHER_DIR"/bin/shim/* "$WATCHER_DIR"/deploy/*.sh
# The chmod above must never read as a local change to git: the deployer refuses to update
# a checkout with changes of its own (deploy/autodeploy.sh sets this too).
su - watcher -c "git -C '$WATCHER_DIR' config core.fileMode false" 2>/dev/null || true

install -d -m 750 -o root -g watcher /etc/watcher
[ -f /etc/watcher/env ] || { echo "CLAUDE_CODE_OAUTH_TOKEN=" > /etc/watcher/env; chown root:watcher /etc/watcher/env; chmod 640 /etc/watcher/env; }
# Discord is optional: an empty DISCORD_WEBHOOK_URL is the off switch. The line is added
# once so the file documents itself; existing values are never touched.
grep -q '^DISCORD_WEBHOOK_URL=' /etc/watcher/env || echo "DISCORD_WEBHOOK_URL=" >> /etc/watcher/env
# The floor page's own file: the server room's Proxmox readout (optional). Its own file, not
# `env`, because `env` is every lane's environment and a Proxmox token has no business in a
# tick (watcher-floor.service says why).
[ -f /etc/watcher/floor.env ] || { printf 'PROXMOX_URL=\nPROXMOX_TOKEN=\nPROXMOX_VERIFY=1\n' > /etc/watcher/floor.env; chown root:watcher /etc/watcher/floor.env; chmod 640 /etc/watcher/floor.env; }

install -d -o watcher -g watcher /home/watcher/.claude /home/watcher/projects
ln -sfn "$WATCHER_DIR/commands" /home/watcher/.claude/commands
ln -sfn "$WATCHER_DIR/agents"   /home/watcher/.claude/agents
chown -h watcher:watcher /home/watcher/.claude/commands /home/watcher/.claude/agents

# bin/board.py's cache (card #98): one directory every lane AND the floor service can both
# write to, so a board read inside one poll's TTL is shared instead of re-fetched per
# process. Without this, board.py's own fallback (WATCHER_STATE_ROOT, or
# ~/.local/state/watcher) still works — it is just not the one every project on the box is
# guaranteed to agree on by default.
install -d -o watcher -g watcher /var/cache/watcher

# The canary checkout (card #46): a lane whose project file sets LANE_CHANNEL=canary runs
# from a second, independent clone — bin/spawn.sh's own shim re-execs into it — so a bad
# prompt reaches only the board that opted in before it reaches every other lane on the
# box. Created here, once; every later move onto a new commit is deploy/autodeploy.sh's
# own job (the canary stage), exactly as /opt/watcher itself is never pulled here again
# after its first clone (INSTALL_NO_PULL, above).
if grep -qsx 'LANE_CHANNEL=canary' "$WATCHER_DIR"/projects/*.env 2>/dev/null; then
  [ -d "$WATCHER_CANARY_DIR/.git" ] || canary_clone \
    || echo "warning: could not clone the canary checkout to $WATCHER_CANARY_DIR" >&2
  if [ -d "$WATCHER_CANARY_DIR/.git" ]; then
    chown -R watcher:watcher "$WATCHER_CANARY_DIR"
    chmod +x "$WATCHER_CANARY_DIR"/bin/*.sh "$WATCHER_CANARY_DIR"/bin/shim/* "$WATCHER_CANARY_DIR"/deploy/*.sh 2>/dev/null || true
    su - watcher -c "git -C '$WATCHER_CANARY_DIR' config core.fileMode false" 2>/dev/null || true
    install -d -o watcher -g watcher /home/watcher/.claude-canary
    ln -sfn "$WATCHER_CANARY_DIR/commands" /home/watcher/.claude-canary/commands
    ln -sfn "$WATCHER_CANARY_DIR/agents"   /home/watcher/.claude-canary/agents
    chown -h watcher:watcher /home/watcher/.claude-canary/commands /home/watcher/.claude-canary/agents
  fi
fi

# Card #106: `board` prefers the WATCHER_HOME a tick's own lane was launched with, so a
# card worked under one release keeps reading that release's own board.py for its whole
# run, and only falls back to the live pointer for anything run outside a tick (the
# deployer itself, the floor page, a person on the box).
cat > /usr/local/bin/board <<'B'
#!/usr/bin/env bash
exec python3 "${WATCHER_HOME:-/opt/watcher}/bin/board.py" "$@"
B
chmod 755 /usr/local/bin/board

install -m 644 "$WATCHER_DIR/deploy/watcher@.service" /etc/systemd/system/watcher@.service
# The deployer (deploy/autodeploy.sh): a timer that ships main by itself, as soon as it can.
# Enabled here, so a box set up from scratch deploys itself from its first boot.
install -m 644 "$WATCHER_DIR/deploy/watcher-deploy.service" /etc/systemd/system/watcher-deploy.service
install -m 644 "$WATCHER_DIR/deploy/watcher-deploy.timer" /etc/systemd/system/watcher-deploy.timer
# The Monday digest (bin/metrics.py --digest --post, card #139): the loop's own numbers,
# per lane, in Discord, once a week. Installed and enabled for every box, the same as
# watcher-deploy.timer above — DISCORD_WEBHOOK_URL unset in /etc/watcher/env is its own
# off switch, not a reason to leave the timer off.
install -m 644 "$WATCHER_DIR/deploy/watcher-metrics.service" /etc/systemd/system/watcher-metrics.service
install -m 644 "$WATCHER_DIR/deploy/watcher-metrics.timer" /etc/systemd/system/watcher-metrics.timer
# The detector (bin/detect.sh, card #36): the unit is installed for every box, but an
# instance only starts for a project that carries its own projects/<name>.detect.env —
# merging that file is what turns detection on, below, the same as a lane's own .env.
install -m 644 "$WATCHER_DIR/deploy/watcher-detect@.service" /etc/systemd/system/watcher-detect@.service
install -m 644 "$WATCHER_DIR/deploy/watcher-detect@.timer" /etc/systemd/system/watcher-detect@.timer
systemctl daemon-reload
# Only the first time: a hand-stopped timer (the box's own emergency fallback) stays off,
# and a re-install from inside one must not start it again behind their back.
systemctl is-enabled --quiet watcher-deploy.timer 2>/dev/null \
  || systemctl enable --now watcher-deploy.timer >/dev/null 2>&1 \
  || echo "warning: could not enable watcher-deploy.timer" >&2
systemctl is-enabled --quiet watcher-metrics.timer 2>/dev/null \
  || systemctl enable --now watcher-metrics.timer >/dev/null 2>&1 \
  || echo "warning: could not enable watcher-metrics.timer" >&2
for f in "$WATCHER_DIR"/projects/*.detect.env; do
  [ -f "$f" ] || continue
  p="$(basename "$f" .detect.env)"
  systemctl is-enabled --quiet "watcher-detect@$p.timer" 2>/dev/null \
    || systemctl enable --now "watcher-detect@$p.timer" >/dev/null 2>&1 \
    || echo "warning: could not enable watcher-detect@$p.timer" >&2
done

# The floor page (bin/floor.py, card #40): one instance per box, holds no card and no work
# in flight, so — unlike a `watcher@` lane — it is simply restarted here on every install,
# never drained or paused. `enable --now` covers the first install; a later one just picks
# up new code via the restart.
install -m 644 "$WATCHER_DIR/deploy/watcher-floor.service" /etc/systemd/system/watcher-floor.service
systemctl daemon-reload
systemctl enable --now watcher-floor.service >/dev/null 2>&1 \
  && systemctl restart watcher-floor.service \
  || echo "warning: could not start watcher-floor.service" >&2

NOT_STARTED=()
for p in "$@"; do
  f="$WATCHER_DIR/projects/$p.env"
  [ -f "$f" ] || { echo "no project file $f" >&2; exit 1; }
  # Clear what we read from the file before sourcing the next one: several projects share
  # this one shell, and a value left over from the previous project would silently stand in
  # for a file that simply does not set it.
  unset PROJECT_ROOT PROJECT_REPO BOARD_OWNER BOARD_NUMBER
  # shellcheck disable=SC1090
  . "$f"
  [ -n "${PROJECT_ROOT:-}" ] || { echo "$f sets no PROJECT_ROOT" >&2; exit 1; }
  # The repo to clone is PROJECT_REPO, NOT "$BOARD_OWNER/$p". The instance name is a
  # watcher-side label (a lane is `<project>-<lane>`), and the board's owner need not own
  # the code — an org board over a repo in a user account is ordinary. Fall back to the
  # old guess only when the project file names no repo.
  repo="${PROJECT_REPO:-${BOARD_OWNER:-}/$p}"
  if [ ! -d "$PROJECT_ROOT/.git" ]; then
    su - watcher -c "gh repo clone $repo '$PROJECT_ROOT' -- -q" \
      || { echo "clone of $repo failed — is the watcher user logged in to gh, and is that the right repo? (su - watcher; gh auth login)" >&2; exit 1; }
  fi
  echo "project $p at $PROJECT_ROOT ($repo)"
  # doctor.sh (card #39): read-only, run as the bot. Never fatal to install.sh itself —
  # one misconfigured project must not stop the others in the same run — so a failure here
  # only holds this project out of the "Next, by hand" list below: its own lines on stderr
  # are what say not to start the service yet.
  DOCTOR_OK=1
  if [ -n "${BOARD_OWNER:-}" ] && [ -n "${BOARD_NUMBER:-}" ]; then
    su - watcher -c "PROJECT_REPO='$repo' PROJECT_ROOT='$PROJECT_ROOT' BOARD_OWNER='$BOARD_OWNER' BOARD_NUMBER='$BOARD_NUMBER' '$WATCHER_DIR/bin/doctor.sh'" \
      || { DOCTOR_OK=0; echo "doctor found a problem with $p (above) — fix it, then re-run install.sh $p before starting watcher@$p" >&2; }
  fi
  if [ "$DOCTOR_OK" = 1 ] && ! systemctl is-enabled --quiet "watcher@$p" 2>/dev/null; then
    NOT_STARTED+=("$p")
  fi
done

MISSING=()
grep -qE '^CLAUDE_CODE_OAUTH_TOKEN=.+' /etc/watcher/env 2>/dev/null || MISSING+=("  Token:      claude setup-token   (on a machine with a browser); put the printed value in
              /etc/watcher/env as CLAUDE_CODE_OAUTH_TOKEN=…")
grep -qE '^DISCORD_WEBHOOK_URL=.+' /etc/watcher/env 2>/dev/null || MISSING+=("  Discord:    optional; put the channel's webhook in the same file as DISCORD_WEBHOOK_URL=…
              (and DISCORD_MENTION=<@your-id> to get pinged on Stuck alerts)")
su - watcher -c "gh auth status" >/dev/null 2>&1 || MISSING+=("  GitHub:     su - watcher; gh auth login   (the bot account); gh auth setup-git")
# A browser for bin/browse.py: how a tick on any project looks at a page (the reviewer
# too). Asked of browse.py itself, as the user the ticks run as, so this looks where a
# tick's own call will look.
su - watcher -c "python3 '$WATCHER_DIR/bin/browse.py' find" >/dev/null 2>&1 || MISSING+=("  Browser:    apt install chromium   (ticks look at pages with bin/browse.py, which needs a
              Chrome or Chromium; without one a tick cannot check a page it changed)")
if [ $# -eq 0 ]; then
  MISSING+=("  Project:    deploy/add-project.sh <name> <owner/repo>, then install.sh <project> clones it; drop its .env.local / private inputs in place.")
  MISSING+=("  Start:      systemctl enable --now watcher@<project>; journalctl -u watcher@<project> -f")
else
  for p in "${NOT_STARTED[@]}"; do
    MISSING+=("  Start:      systemctl enable --now watcher@$p; journalctl -u watcher@$p -f")
  done
fi

echo
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "Next, by hand:"
  printf '%s\n' "${MISSING[@]}"
else
  echo "Nothing left by hand: token, GitHub login and every named project's service are set."
fi
echo "  Pause:      touch ~watcher/.local/state/watcher/<project>/PAUSE"
echo "  Detector:   optional (card #36). Drop in projects/<project>.detect.env and re-run"
echo "              install.sh (or wait for the next deploy) — the timer starts on its own."
