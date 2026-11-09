#!/usr/bin/env bash
#
# gh-app-token.sh [project] <author|reviewer> [--identity]   (card #255)
#
# Prints a GitHub App installation token for the project's author or reviewer App, minted
# from the App's private key: a JWT signed with `openssl dgst -sha256 -sign`, exchanged at
# the installation's access_tokens endpoint, cached until five minutes before its hour is
# up. `--identity` prints the App's git identity instead: `<slug>[bot] <id+slug[bot]@users.noreply.github.com>`.
#
# Where the App lives (written by deploy/app-setup.py, never by hand, never in git):
#   $GH_APP_DIR, else /etc/watcher/apps/<project>/ when it exists (the box),
#   else ~/.config/roloc-apps/<project>/ (a Mac)
#     author.pem, reviewer.pem   the private keys
#     config                     AUTHOR_APP_ID=… AUTHOR_INSTALLATION_ID=… AUTHOR_SLUG=… AUTHOR_BOT_ID=…
#                                REVIEWER_… the same four
# The project defaults to $WATCHER_APP_PROJECT, then $PROJECT_NAME (the lane's), then the
# name of the git remote's repository.
#
# Exit 0 with the token (or identity) on stdout and nothing else there; exit 1 with one line
# on stderr when the App is not set up or GitHub refused; exit 2 on a bad call.
# Test seams: GH_APP_API (the API base), GH_APP_CACHE (the token cache folder), GH_APP_NOW
# (the clock, epoch seconds).
set -euo pipefail

project=""
if [ $# -ge 2 ] && [ "$2" != --identity ]; then project="$1"; shift; fi
role="${1:-}"
mode="${2:-token}"
case "$role" in author|reviewer) ;; *) echo "usage: gh-app-token.sh [project] <author|reviewer> [--identity]" >&2; exit 2 ;; esac
case "$mode" in token|--identity) ;; *) echo "usage: gh-app-token.sh [project] <author|reviewer> [--identity]" >&2; exit 2 ;; esac

if [ -z "$project" ]; then
  project="${WATCHER_APP_PROJECT:-${PROJECT_NAME:-}}"
  if [ -z "$project" ]; then
    project="$(git remote get-url origin 2>/dev/null | sed -E 's#\.git$##; s#.*/##' || true)"
  fi
fi
[ -n "$project" ] || { echo "gh-app-token: no project (pass one, or run inside its checkout)" >&2; exit 2; }

dir="${GH_APP_DIR:-}"
if [ -z "$dir" ]; then
  if [ -d "/etc/watcher/apps/$project" ]; then dir="/etc/watcher/apps/$project"; else dir="$HOME/.config/roloc-apps/$project"; fi
fi
[ -r "$dir/config" ] || { echo "gh-app-token: no App set up for $project (no $dir/config)" >&2; exit 1; }

ROLE="$(printf '%s' "$role" | tr '[:lower:]' '[:upper:]')"
# The config is plain KEY=value lines; read it in a subshell so its names never leak.
get() { ( set +u; . "$dir/config" >/dev/null 2>&1; eval "printf '%s' \"\${${ROLE}_$1:-}\"" ); }
app_id="$(get APP_ID)"; inst="$(get INSTALLATION_ID)"; slug="$(get SLUG)"; bot_id="$(get BOT_ID)"
[ -n "$app_id" ] && [ -n "$inst" ] || { echo "gh-app-token: $dir/config has no ${ROLE}_APP_ID / ${ROLE}_INSTALLATION_ID" >&2; exit 1; }

if [ "$mode" = --identity ]; then
  [ -n "$slug" ] || slug="roloc-$project-$role"
  if [ -n "$bot_id" ]; then
    printf '%s[bot] <%s+%s[bot]@users.noreply.github.com>\n' "$slug" "$bot_id" "$slug"
  else
    printf '%s[bot] <%s[bot]@users.noreply.github.com>\n' "$slug" "$slug"
  fi
  exit 0
fi

now="${GH_APP_NOW:-$(date +%s)}"
cache_dir="${GH_APP_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/watcher-apps}"
cache="$cache_dir/$project-$role.token"
if [ -r "$cache" ]; then
  { read -r exp; read -r tok; } < "$cache" || true
  if [ -n "${tok:-}" ] && [ "${exp:-0}" -gt $((now + 300)) ] 2>/dev/null; then
    printf '%s\n' "$tok"; exit 0
  fi
fi

key="$dir/$role.pem"
[ -r "$key" ] || { echo "gh-app-token: cannot read $key" >&2; exit 1; }
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
header="$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)"
payload="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 480)) "$app_id" | b64url)"
sig="$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$key" | b64url)" \
  || { echo "gh-app-token: could not sign with $key" >&2; exit 1; }
jwt="$header.$payload.$sig"

api="${GH_APP_API:-https://api.github.com}"
reply="$(curl -fsS -X POST -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" "$api/app/installations/$inst/access_tokens" 2>&1)" \
  || { echo "gh-app-token: GitHub refused the token for $project/$role: $(printf '%s' "$reply" | head -c 200)" >&2; exit 1; }
parsed="$(printf '%s' "$reply" | python3 -c '
import sys, json, calendar, time
d = json.load(sys.stdin)
t = d.get("token")
if not t: sys.exit(1)
e = d.get("expires_at", "")
try: exp = calendar.timegm(time.strptime(e, "%Y-%m-%dT%H:%M:%SZ"))
except ValueError: exp = 0
print(exp); print(t)')" || { echo "gh-app-token: GitHub answered without a token for $project/$role" >&2; exit 1; }

umask 077
mkdir -p "$cache_dir"
printf '%s\n' "$parsed" > "$cache.$$" && mv "$cache.$$" "$cache"
printf '%s\n' "$parsed" | sed -n 2p
