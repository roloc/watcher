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

if [ -d /opt/watcher/.git ]; then
  # As the watcher user: it holds the gh login, and the checkout is its own.
  su - watcher -c "git -C /opt/watcher pull -q --ff-only" || echo "warning: /opt/watcher pull failed (watcher user not logged in to gh yet?) — using what is there" >&2
elif [ ! -d /opt/watcher ]; then
  git clone -q "$REPO_URL" /opt/watcher || { echo "clone failed — rsync the repo to /opt/watcher instead" >&2; exit 1; }
fi
chown -R watcher:watcher /opt/watcher
chmod +x /opt/watcher/bin/*.sh /opt/watcher/deploy/*.sh

install -d -m 750 -o root -g watcher /etc/watcher
[ -f /etc/watcher/env ] || { echo "CLAUDE_CODE_OAUTH_TOKEN=" > /etc/watcher/env; chown root:watcher /etc/watcher/env; chmod 640 /etc/watcher/env; }
# Discord is optional: an empty DISCORD_WEBHOOK_URL is the off switch. The line is added
# once so the file documents itself; existing values are never touched.
grep -q '^DISCORD_WEBHOOK_URL=' /etc/watcher/env || echo "DISCORD_WEBHOOK_URL=" >> /etc/watcher/env

install -d -o watcher -g watcher /home/watcher/.claude /home/watcher/projects
ln -sfn /opt/watcher/commands /home/watcher/.claude/commands
ln -sfn /opt/watcher/agents   /home/watcher/.claude/agents
chown -h watcher:watcher /home/watcher/.claude/commands /home/watcher/.claude/agents
cat > /usr/local/bin/board <<'B'
#!/usr/bin/env bash
exec python3 /opt/watcher/bin/board.py "$@"
B
chmod 755 /usr/local/bin/board

install -m 644 /opt/watcher/deploy/watcher@.service /etc/systemd/system/watcher@.service
systemctl daemon-reload

for p in "$@"; do
  f="/opt/watcher/projects/$p.env"
  [ -f "$f" ] || { echo "no project file $f" >&2; exit 1; }
  # shellcheck disable=SC1090
  . "$f"
  if [ ! -d "$PROJECT_ROOT/.git" ]; then
    su - watcher -c "gh repo clone $BOARD_OWNER/$p '$PROJECT_ROOT' -- -q" \
      || { echo "clone failed — is the watcher user logged in to gh? (su - watcher; gh auth login)" >&2; exit 1; }
  fi
  echo "project $p at $PROJECT_ROOT"
done

cat <<'NEXT'

Next, by hand:
  1. Token:   claude setup-token   (on a machine with a browser); put the printed value in
              /etc/watcher/env as CLAUDE_CODE_OAUTH_TOKEN=…
  1b. Discord: optional; put the channel's webhook in the same file as DISCORD_WEBHOOK_URL=…
              (and DISCORD_MENTION=<@your-id> to get pinged on Stuck alerts)
  2. GitHub:  su - watcher; gh auth login   (the bot account); gh auth setup-git
  3. Project: install.sh <project>  clones it; drop its .env.local / private inputs in place.
  4. Start:   systemctl enable --now watcher@<project>; journalctl -u watcher@<project> -f
  Pause:      touch ~watcher/.local/state/watcher/<project>/PAUSE
NEXT
