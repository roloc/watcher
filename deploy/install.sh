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

# hygiene_opts_in <project-env-file> — true if that project should get a
# watcher-hygiene@<name>.timer (card #196): HYGIENE=1 in its own env, or shipping a
# hygiene.sh on its own origin/main (bin/ or scripts/). Probed against origin/main's own
# tree, never PROJECT_ROOT's checked-out working copy: that checkout can sit on an old
# commit, or detached off main entirely (a lane mid-build, or simply never fetched since
# this box was set up), and the working tree is then not evidence of what the repo ships
# on main. `cat-file -e <rev>:<path>` reads a blob's existence straight out of the object
# store, no checkout required, and a fetch first makes sure that ref itself is not stale.
# As the watcher user, like every other git call on a watcher-owned checkout in this file
# (the pull above, canary_clone): this script runs as root, and root's own git refuses a
# repo it does not own ("detected dubious ownership") — every PROJECT_ROOT on the box is
# watcher's. A function, not inline in the loop below, so bin/test-install-hygiene.sh can
# call it directly with INSTALL_LIB_ONLY=1, the same way bin/test-deploy.sh already does
# for canary_clone.
hygiene_opts_in() {
  local f="$1"
  unset PROJECT_ROOT HYGIENE
  # shellcheck disable=SC1090
  . "$f"
  [ "${HYGIENE:-0}" = 1 ] && return 0
  [ -n "${PROJECT_ROOT:-}" ] && [ -e "$PROJECT_ROOT/.git" ] || return 1
  su - watcher -c "git -C '$PROJECT_ROOT' fetch -q origin main 2>/dev/null; \
       git -C '$PROJECT_ROOT' cat-file -e origin/main:bin/hygiene.sh 2>/dev/null \
       || git -C '$PROJECT_ROOT' cat-file -e origin/main:scripts/hygiene.sh 2>/dev/null"
}

# install_desk — the desk (card #205): a read-only Remote Control session on this box for
# the owner's own Claude sessions (deploy/watcher-desk.service says why, and why it is its
# own user). Its own Unix user, watcher-desk, with its own home and its own logins; a small
# git repo at $DESK_HOME/desk (Remote Control wants a git root) stocked from
# deploy/desk/CLAUDE.md and deploy/desk/settings.json on every deploy; the unit installed
# and enabled. Restarted ONLY when one of those files actually changed — a deploy must not
# cut off a conversation the owner is having with the desk — so `cmp -s` decides, and the
# first install is the one `enable --now`. The login itself (`claude auth login` as
# watcher-desk, a browser flow of the owner's own account) is never done here: bin/desk.sh
# waits for it and says so in the journal. A function, not inline, so
# bin/test-install-desk.sh can call it with INSTALL_LIB_ONLY=1 and every root-only command
# stubbed; DESK_HOME and DESK_UNIT_DIR exist for that test alone.
install_desk() {
  local home="${DESK_HOME:-/home/watcher-desk}" unitdir="${DESK_UNIT_DIR:-/etc/systemd/system}"
  local desk="$home/desk" changed=0 groups=""
  if ! id watcher-desk >/dev/null 2>&1; then
    getent group systemd-journal >/dev/null 2>&1 && groups="systemd-journal"
    useradd -m -d "$home" -s /bin/bash ${groups:+-G "$groups"} watcher-desk \
      || { echo "warning: could not create the watcher-desk user; no desk on this box" >&2; return 1; }
  fi
  # The home, the desk folder and everything root puts in them (review of
  # roloc/watcher#218, three rounds): the allow and deny lists are what make the desk
  # read-only, so nothing running as watcher-desk may rewrite them, drop a
  # settings.local.json beside them, OR rename the folder that holds them away and start a
  # fresh one. A root-owned directory with the sticky bit and group write (1775,
  # group watcher-desk) is exactly that: the desk creates its own files there (claude's
  # login, projects and sessions under ~/.claude; the git repo under ~/desk) but cannot
  # unlink, rename or replace an entry root owns, because with the sticky bit only the
  # entry's owner, the directory's owner or root may — and the directory's owner is root.
  chown root:watcher-desk "$home" && chmod 1775 "$home"
  # The shell startup files are root's and empty: the Bash tool loads ~/.bashrc, so a file
  # the desk could write there would be code it runs on every command. Same for gh's
  # config.yml (its `browser:` setting is a program gh would run); gh's own hosts.yml,
  # which the login writes, is a new entry the desk creates itself.
  local f
  for f in .bashrc .bash_profile .bash_login .profile .bash_logout; do
    # -O: owned by whoever runs this (root on the box). No `stat -c`: GNU-only, and
    # bin/test-install-desk.sh runs this on a Mac.
    [ -f "$home/$f" ] && [ ! -s "$home/$f" ] && [ -O "$home/$f" ] \
      || install -m 644 -o root -g root /dev/null "$home/$f"
  done
  install -d -o root -g watcher-desk -m 1775 "$home/.config" "$home/.config/gh"
  if ! printf 'git_protocol: https\n' | cmp -s - "$home/.config/gh/config.yml"; then
    printf 'git_protocol: https\n' > "$home/.config/gh/config.yml.new" \
      && install -m 644 -o root -g root "$home/.config/gh/config.yml.new" "$home/.config/gh/config.yml" \
      && rm -f "$home/.config/gh/config.yml.new"
  fi
  install -d -o root -g watcher-desk -m 1775 "$desk"
  install -d -o root -g root -m 755 "$desk/.claude"
  if ! cmp -s "$WATCHER_DIR/deploy/desk/CLAUDE.md" "$desk/CLAUDE.md"; then
    install -m 644 -o root -g root "$WATCHER_DIR/deploy/desk/CLAUDE.md" "$desk/CLAUDE.md"; changed=1
  fi
  if ! cmp -s "$WATCHER_DIR/deploy/desk/settings.json" "$desk/.claude/settings.json"; then
    install -m 644 -o root -g root "$WATCHER_DIR/deploy/desk/settings.json" "$desk/.claude/settings.json"; changed=1
  fi
  # The USER-scope settings too (review of #218, round 2): allow lists merge across scopes,
  # so a ~/.claude/settings.json the desk could write would widen what the project file
  # closes. Root-owned `{}`, in a root-owned sticky folder (1775, group watcher-desk) that
  # itself sits in the root-owned sticky home above, so neither the file nor its folder
  # can be renamed away from underneath.
  install -d -o root -g watcher-desk -m 1775 "$home/.claude"
  if ! printf '{}\n' | cmp -s - "$home/.claude/settings.json"; then
    printf '{}\n' > "$home/.claude/settings.json.new" && install -m 644 -o root -g root "$home/.claude/settings.json.new" "$home/.claude/settings.json" && rm -f "$home/.claude/settings.json.new"
    changed=1
  fi
  # bin/desk.sh runs through the /opt/watcher pointer, so a new release's copy is what the
  # NEXT start runs — but the running desk is the old one. A copy of what was last
  # installed is kept so a changed desk.sh counts as a change and restarts it.
  if ! cmp -s "$WATCHER_DIR/bin/desk.sh" "$home/.desk.sh.installed"; then
    install -m 644 -o root -g root "$WATCHER_DIR/bin/desk.sh" "$home/.desk.sh.installed"; changed=1
  fi
  # git refuses a repository whose top folder another user owns ("dubious ownership"), and
  # $desk is root's now — so the desk user's own ~/.gitconfig names it safe, once.
  su - watcher-desk -c "git config --global --get-all safe.directory 2>/dev/null | grep -qx '$desk' || git config --global --add safe.directory '$desk'" \
    || echo "warning: could not mark $desk safe for git as watcher-desk" >&2
  [ -d "$desk/.git" ] || su - watcher-desk -c "git -C '$desk' init -q" \
    || { echo "warning: could not make $desk a git repo (Remote Control needs one)" >&2; return 1; }
  # Committed so the folder is a repository with a history, not just an init; nothing to
  # commit is the common case and not an error.
  su - watcher-desk -c "git -C '$desk' add CLAUDE.md .claude/settings.json && git -C '$desk' -c user.name=desk -c user.email=desk@localhost commit -q -m 'desk files'" >/dev/null 2>&1 || true
  if ! cmp -s "$WATCHER_DIR/deploy/watcher-desk.service" "$unitdir/watcher-desk.service"; then
    install -m 644 "$WATCHER_DIR/deploy/watcher-desk.service" "$unitdir/watcher-desk.service"; changed=1
    systemctl daemon-reload
  fi
  if ! systemctl is-enabled --quiet watcher-desk.service 2>/dev/null; then
    systemctl enable --now watcher-desk.service >/dev/null 2>&1 \
      || echo "warning: could not start watcher-desk.service" >&2
  elif [ "$changed" = 1 ]; then
    systemctl restart watcher-desk.service \
      || echo "warning: could not restart watcher-desk.service" >&2
  fi
}

# bin/test-deploy.sh sources this file with INSTALL_LIB_ONLY=1 to call canary_clone directly,
# with no root, no watcher user and no network — stopping here is what makes that possible.
# bin/test-install-hygiene.sh does the same to call hygiene_opts_in, bin/test-install-desk.sh
# to call install_desk.
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
# Discord and Slack are both optional: an empty *_WEBHOOK_URL is the off switch for each.
# The lines are added once so the file documents itself; existing values are never touched.
grep -q '^DISCORD_WEBHOOK_URL=' /etc/watcher/env || echo "DISCORD_WEBHOOK_URL=" >> /etc/watcher/env
grep -q '^SLACK_WEBHOOK_URL=' /etc/watcher/env || echo "SLACK_WEBHOOK_URL=" >> /etc/watcher/env
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
# The weekly hygiene sweep (bin/hygiene-run.sh, card #196): the unit is installed for
# every box, same as the detector above, but an instance only starts for a project that
# opts in — HYGIENE=1 in its own projects/<name>.env, or simply shipping a hygiene.sh on
# its own origin/main (bin/ or scripts/, whichever the project ships; checked against
# that ref, not PROJECT_ROOT's own working tree, below). One un-suffixed project file per
# project is the canonical one to check: a `-design`/`-triage` file is a second lane on
# the SAME checkout (PROJECT_ROOT, PROJECT_REPO, BOARD_OWNER, BOARD_NUMBER all identical),
# so checking it too would just enable the same sweep twice.
install -m 644 "$WATCHER_DIR/deploy/watcher-hygiene@.service" /etc/systemd/system/watcher-hygiene@.service
install -m 644 "$WATCHER_DIR/deploy/watcher-hygiene@.timer" /etc/systemd/system/watcher-hygiene@.timer
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
for f in "$WATCHER_DIR"/projects/*.env; do
  [ -f "$f" ] || continue
  case "$f" in *.detect.env) continue ;; esac
  p="$(basename "$f" .env)"
  case "$p" in *-design|*-triage) continue ;; esac
  if hygiene_opts_in "$f"; then
    systemctl is-enabled --quiet "watcher-hygiene@$p.timer" 2>/dev/null \
      || systemctl enable --now "watcher-hygiene@$p.timer" >/dev/null 2>&1 \
      || echo "warning: could not enable watcher-hygiene@$p.timer" >&2
  fi
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

# The desk (card #205): one per box, beside the floor page. Unlike the floor it is only
# restarted when its own files changed (install_desk, above).
install_desk || true

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
grep -qE '^SLACK_WEBHOOK_URL=.+' /etc/watcher/env 2>/dev/null || MISSING+=("  Slack:      optional; same alerts, to a channel's incoming webhook as SLACK_WEBHOOK_URL=…
              (and SLACK_MENTION=<@your-id> to get pinged on Stuck alerts)")
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
echo "  Hygiene:    on its own for a project that ships a hygiene.sh (card #196); set"
echo "              HYGIENE=1 in projects/<project>.env to opt in without one."
echo "  Desk:       once, so the box shows up in your own Claude sessions (card #205):"
echo "              sudo -u watcher-desk -H claude auth login; sudo -u watcher-desk -H gh auth login"
