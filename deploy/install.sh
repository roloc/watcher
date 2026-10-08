#!/usr/bin/env bash
# Run as root on the box, idempotent. Assumes the base tools are present (git, gh, node,
# python3, a JDK if the project needs one, and `npm i -g @anthropic-ai/claude-code`).
#
#   /opt/watcher                  this repo (git clone or pull)
#   /etc/watcher/env              CLAUDE_CODE_OAUTH_TOKEN=…  (root:watcher 640; written by hand)
#   /home/watcher/.claude/        commands/ and agents/ symlinked from the repo, so every
#                                 project on the box gets /watch and the reviewer
#   /home/watcher/projects/<p>    each project's main checkout (cloned by `install.sh <p>`)
set -euo pipefail
REPO_URL="${WATCHER_REPO:-https://github.com/roloc/watcher.git}"
id watcher >/dev/null 2>&1 || useradd -m -s /bin/bash watcher

if [ -d /opt/watcher/.git ] && [ "${INSTALL_NO_PULL:-0}" = 1 ]; then
  # deploy/autodeploy.sh has already moved the checkout to the exact commit CI and the
  # replay suite passed; a pull here could take it past that commit to one nothing checked.
  :
elif [ -d /opt/watcher/.git ]; then
  # As the watcher user: it holds the gh login, and the checkout is its own.
  su - watcher -c "git -C /opt/watcher pull -q --ff-only" || echo "warning: /opt/watcher pull failed (watcher user not logged in to gh yet?) — using what is there" >&2
elif [ ! -d /opt/watcher ]; then
  git clone -q "$REPO_URL" /opt/watcher || { echo "clone failed — rsync the repo to /opt/watcher instead" >&2; exit 1; }
fi
chown -R watcher:watcher /opt/watcher
chmod +x /opt/watcher/bin/*.sh /opt/watcher/bin/shim/* /opt/watcher/deploy/*.sh
# The chmod above must never read as a local change to git: the deployer refuses to update
# a checkout with changes of its own (deploy/autodeploy.sh sets this too).
su - watcher -c "git -C /opt/watcher config core.fileMode false" 2>/dev/null || true

install -d -m 750 -o root -g watcher /etc/watcher
[ -f /etc/watcher/env ] || { echo "CLAUDE_CODE_OAUTH_TOKEN=" > /etc/watcher/env; chown root:watcher /etc/watcher/env; chmod 640 /etc/watcher/env; }
# Discord is optional: an empty DISCORD_WEBHOOK_URL is the off switch. The line is added
# once so the file documents itself; existing values are never touched.
grep -q '^DISCORD_WEBHOOK_URL=' /etc/watcher/env || echo "DISCORD_WEBHOOK_URL=" >> /etc/watcher/env

install -d -o watcher -g watcher /home/watcher/.claude /home/watcher/projects
ln -sfn /opt/watcher/commands /home/watcher/.claude/commands
ln -sfn /opt/watcher/agents   /home/watcher/.claude/agents
chown -h watcher:watcher /home/watcher/.claude/commands /home/watcher/.claude/agents

# The canary checkout (card #46): a lane whose project file sets LANE_CHANNEL=canary runs
# from a second, independent clone — bin/spawn.sh's own shim re-execs into it — so a bad
# prompt reaches only the board that opted in before it reaches every other lane on the
# box. Created here, once; every later move onto a new commit is deploy/autodeploy.sh's
# own job (the canary stage), exactly as /opt/watcher itself is never pulled here again
# after its first clone (INSTALL_NO_PULL, above).
if grep -qsx 'LANE_CHANNEL=canary' /opt/watcher/projects/*.env 2>/dev/null; then
  # As root, same as /opt/watcher's own first clone above: the watcher user may not have a
  # working gh/git login yet at first boot, and a public HTTPS clone needs none.
  [ -d /opt/watcher-canary ] || git clone -q "$REPO_URL" /opt/watcher-canary \
    || echo "warning: could not clone the canary checkout to /opt/watcher-canary" >&2
  if [ -d /opt/watcher-canary/.git ]; then
    chown -R watcher:watcher /opt/watcher-canary
    chmod +x /opt/watcher-canary/bin/*.sh /opt/watcher-canary/bin/shim/* /opt/watcher-canary/deploy/*.sh 2>/dev/null || true
    su - watcher -c "git -C /opt/watcher-canary config core.fileMode false" 2>/dev/null || true
    install -d -o watcher -g watcher /home/watcher/.claude-canary
    ln -sfn /opt/watcher-canary/commands /home/watcher/.claude-canary/commands
    ln -sfn /opt/watcher-canary/agents   /home/watcher/.claude-canary/agents
    chown -h watcher:watcher /home/watcher/.claude-canary/commands /home/watcher/.claude-canary/agents
  fi
fi

cat > /usr/local/bin/board <<'B'
#!/usr/bin/env bash
exec python3 /opt/watcher/bin/board.py "$@"
B
chmod 755 /usr/local/bin/board

install -m 644 /opt/watcher/deploy/watcher@.service /etc/systemd/system/watcher@.service
# The deployer (deploy/autodeploy.sh): a timer that ships main by itself when the lanes are
# idle. Enabled here, so a box set up from scratch deploys itself from its first boot.
install -m 644 /opt/watcher/deploy/watcher-deploy.service /etc/systemd/system/watcher-deploy.service
install -m 644 /opt/watcher/deploy/watcher-deploy.timer /etc/systemd/system/watcher-deploy.timer
# The detector (bin/detect.sh, card #36): the unit is installed for every box, but an
# instance only starts for a project that carries its own projects/<name>.detect.env —
# merging that file is what turns detection on, below, the same as a lane's own .env.
install -m 644 /opt/watcher/deploy/watcher-detect@.service /etc/systemd/system/watcher-detect@.service
install -m 644 /opt/watcher/deploy/watcher-detect@.timer /etc/systemd/system/watcher-detect@.timer
systemctl daemon-reload
# Only the first time: rollout.sh stops the timer while a person drives, and a re-install
# from inside it must not start it again behind their back.
systemctl is-enabled --quiet watcher-deploy.timer 2>/dev/null \
  || systemctl enable --now watcher-deploy.timer >/dev/null 2>&1 \
  || echo "warning: could not enable watcher-deploy.timer" >&2
for f in /opt/watcher/projects/*.detect.env; do
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
install -m 644 /opt/watcher/deploy/watcher-floor.service /etc/systemd/system/watcher-floor.service
systemctl daemon-reload
systemctl enable --now watcher-floor.service >/dev/null 2>&1 \
  && systemctl restart watcher-floor.service \
  || echo "warning: could not start watcher-floor.service" >&2

NOT_STARTED=()
for p in "$@"; do
  f="/opt/watcher/projects/$p.env"
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
    su - watcher -c "PROJECT_REPO='$repo' PROJECT_ROOT='$PROJECT_ROOT' BOARD_OWNER='$BOARD_OWNER' BOARD_NUMBER='$BOARD_NUMBER' /opt/watcher/bin/doctor.sh" \
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
su - watcher -c "python3 /opt/watcher/bin/browse.py find" >/dev/null 2>&1 || MISSING+=("  Browser:    apt install chromium   (ticks look at pages with bin/browse.py, which needs a
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
