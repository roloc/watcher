#!/usr/bin/env bash
#
# bootstrap.sh — the apt and npm steps from the README's "Set up a box", as one idempotent
# script: run as root on a fresh Debian 12 VM or LXC, before deploy/install.sh ever runs.
# Checks each tool before installing it, so running it again (a later base image, a
# transient network failure, a box that already had some of this) only touches what is
# still missing, and prints one line per tool saying which.
#
#   deploy/bootstrap.sh
#
# Installs: git, python3, curl, gh (GitHub's own apt repo — Debian's own archive does not
# carry it), chromium (the browser bin/browse.py and every tick's reviewer pass need), Node
# 22 (NodeSource's setup script — Debian 12's own archive ships Node 18),
# `@anthropic-ai/claude-code` (npm -g, the CLI every tick runs as), and the pinned Godot 4
# plus Xvfb and the engine's shared libraries (for projects like twreborn whose tests and
# screenshots run the engine headless). Card #160.
#
# What it does not do: write /etc/watcher/env, create the watcher user, or start any unit —
# that is all deploy/install.sh, run right after this.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "bootstrap.sh: run as root (sudo deploy/bootstrap.sh)" >&2; exit 1; }

# Overridable so bin/test-deploy.sh's stubbed case never touches the real box's /etc/apt.
APT_KEYRING_DIR="${APT_KEYRING_DIR:-/etc/apt/keyrings}"
APT_SOURCES_DIR="${APT_SOURCES_DIR:-/etc/apt/sources.list.d}"

have() { command -v "$1" >/dev/null 2>&1; }
say()  { printf 'bootstrap: %s\n' "$*"; }

APT_UPDATED=0
apt_update_once() {
  [ "$APT_UPDATED" = 1 ] && return 0
  apt-get update -qq
  APT_UPDATED=1
}

# gh's keyring and NodeSource's setup script both need a working `curl … | gpg --dearmor`
# path, which a minimal Debian image does not ship (unlike git/python3/curl/chromium, it is
# rarely already there, so this is never folded into the plain-package loop below).
ensure_transport_deps() {
  have gpg && have update-ca-certificates && return 0
  apt_update_once
  apt-get install -y -qq ca-certificates gnupg
  say "installed ca-certificates, gnupg"
}

# --- plain apt packages: binary name == package name for all four ------------------------
for pkg in git python3 curl chromium; do
  if have "$pkg"; then
    say "already have $pkg"
  else
    apt_update_once
    apt-get install -y -qq "$pkg"
    say "installed $pkg"
  fi
done

# --- gh: Debian's own archive does not carry it; GitHub's apt repo does ------------------
if have gh; then
  say "already have gh"
else
  ensure_transport_deps
  install -d -m 0755 "$APT_KEYRING_DIR"
  if [ ! -f "$APT_KEYRING_DIR/githubcli-archive-keyring.gpg" ]; then
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o "$APT_KEYRING_DIR/githubcli-archive-keyring.gpg"
    chmod go+r "$APT_KEYRING_DIR/githubcli-archive-keyring.gpg"
  fi
  if [ ! -f "$APT_SOURCES_DIR/github-cli.list" ]; then
    install -d -m 0755 "$APT_SOURCES_DIR"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=$APT_KEYRING_DIR/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > "$APT_SOURCES_DIR/github-cli.list"
  fi
  apt-get update -qq
  apt-get install -y -qq gh
  APT_UPDATED=1
  say "installed gh"
fi

# --- Node 22: Debian 12's own archive ships Node 18; NodeSource's setup script adds the
# repo and runs its own apt-get update, so this skips apt_update_once's cache entirely ----
if have node && node -v | grep -q '^v22\.'; then
  say "already have node $(node -v)"
else
  ensure_transport_deps
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
  say "installed node $(node -v)"
fi

# --- the CLI every tick runs as ----------------------------------------------------------
if have claude; then
  say "already have claude"
else
  npm i -g @anthropic-ai/claude-code
  say "installed @anthropic-ai/claude-code"
fi

# --- Godot 4 (pinned) + Xvfb, for projects whose tests and screenshots run the engine ----
# twreborn (Trade Wars Reborn, the Godot prototype) runs its GUT suites through the engine
# on every tick, and its screenshot script renders under a virtual display. The official
# linux binary loads X11/GL shared libraries even with --headless, so on a minimal LXC
# they come with it; xvfb is the virtual display, unzip unpacks the release. The project's
# own scripts/godot.sh prefers a matching godot4 on the PATH and otherwise downloads the
# same pin per-user, so this step is what saves every lane on the box that download — bump
# GODOT_VERSION here and in that script (and its version test) together.
GODOT_VERSION="${GODOT_VERSION:-4.7.2-stable}"
GODOT_INSTALL_DIR="${GODOT_INSTALL_DIR:-/usr/local}"
GODOT_BIN="$GODOT_INSTALL_DIR/bin/godot4"
if [ -x "$GODOT_BIN" ]; then
  say "already have godot4"
else
  apt_update_once
  apt-get install -y -qq xvfb unzip libfontconfig1 libx11-6 libxcursor1 libxinerama1 \
    libxrandr2 libxi6 libgl1 libegl1 libgl1-mesa-dri
  say "installed xvfb, unzip, and Godot's shared libraries"
  case "$(dpkg --print-architecture)" in
    arm64) GODOT_ASSET="linux.arm64" ;;
    *)     GODOT_ASSET="linux.x86_64" ;;
  esac
  GODOT_LIB_DIR="$GODOT_INSTALL_DIR/lib/godot-$GODOT_VERSION"
  install -d -m 0755 "$GODOT_LIB_DIR" "$GODOT_INSTALL_DIR/bin"
  # This script's command surface has no mktemp or rm (the offline suite stubs it, same
  # as apt), so the zip lands next to the unpacked binary and stays there — ~60 MB, once
  # per box, overwritten on a version bump.
  curl -fsSL "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}/Godot_v${GODOT_VERSION}_${GODOT_ASSET}.zip" \
    -o "$GODOT_LIB_DIR/godot.zip"
  unzip -qo "$GODOT_LIB_DIR/godot.zip" -d "$GODOT_LIB_DIR"
  install -m 0755 "$GODOT_LIB_DIR/Godot_v${GODOT_VERSION}_${GODOT_ASSET}" "$GODOT_BIN"
  say "installed godot4 ($GODOT_VERSION) at $GODOT_BIN"
fi

say "done — next: deploy/install.sh"
