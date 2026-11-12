#!/usr/bin/env bash
#
# Tests for bin/gh-app-token.sh and the two shims built on it, bin/shim/gh and
# bin/shim/gh-credential (card #255). Offline: the private key is a throwaway made here with
# openssl, `curl` is a stub that answers the one installation-token call and logs it, `gh` is
# a stub that prints the token it was handed.
#
#   bash bin/test-gh-app-token.sh      # exit 0 = every case holds
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TOKEN="$HERE/gh-app-token.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }
check() { if eval "$2"; then pass "$1"; else flunk "$1" "${3:-}"; fi; }

BIN="$TMP/bin"; mkdir -p "$BIN"
APPS="$TMP/apps/demo"; mkdir -p "$APPS"
openssl genrsa -out "$APPS/author.pem" 2048 2>/dev/null
openssl rsa -in "$APPS/author.pem" -pubout -out "$TMP/author.pub" 2>/dev/null
cp "$APPS/author.pem" "$APPS/reviewer.pem"
cat > "$APPS/config" <<'EOC'
AUTHOR_APP_ID=1111
AUTHOR_INSTALLATION_ID=2222
AUTHOR_SLUG=roloc-demo-author
AUTHOR_BOT_ID=3333
REVIEWER_APP_ID=4444
REVIEWER_INSTALLATION_ID=5555
REVIEWER_SLUG=roloc-demo-reviewer
EOC

CALLS="$TMP/curl-calls"; : > "$CALLS"
EXPIRES="2026-11-09T02:00:00Z"
cat > "$BIN/curl" <<'EOF2'
#!/usr/bin/env bash
echo "$*" >> "$CALLS"
[ -f "$TMP/curl-fails" ] && { echo "curl: (22) The requested URL returned error: 401" >&2; exit 22; }
auth=""; while [ $# -gt 0 ]; do [ "$1" = -H ] && case "$2" in "Authorization: Bearer "*) auth="${2#Authorization: Bearer }" ;; esac; shift; done
echo "$auth" > "$TMP/last-jwt"
printf '{"token":"ghs_fixture%s","expires_at":"%s"}' "$(wc -l < "$CALLS" | tr -d ' ')" "$EXPIRES"
EOF2
cat > "$BIN/gh" <<'EOF2'
#!/usr/bin/env bash
echo "real-gh token=${GH_TOKEN:-unset} args=$*"
EOF2
chmod +x "$BIN"/*
export CALLS TMP EXPIRES
export PATH="$BIN:$PATH"
export GH_APP_CACHE="$TMP/cache" GH_APP_API="https://api.test" GH_APP_DIR="$APPS"
unset GH_TOKEN GITHUB_TOKEN PROJECT_NAME WATCHER_APP_PROJECT

exp_epoch="$(python3 -c 'import calendar,time;print(calendar.timegm(time.strptime("2026-11-09T02:00:00Z","%Y-%m-%dT%H:%M:%SZ")))')"
NOW=$((exp_epoch - 3600))

echo "1. the token: a signed JWT goes out, the installation token comes back, nothing else on stdout"
OUT="$(GH_APP_NOW=$NOW "$TOKEN" demo author 2>"$TMP/err")"; RC=$?
check "exit 0 and the token alone" '[ "$RC" = 0 ] && [ "$OUT" = ghs_fixture1 ]' "rc=$RC [$OUT] $(cat "$TMP/err")"
check "it asked the installation's access_tokens endpoint" 'grep -q "https://api.test/app/installations/2222/access_tokens" "$CALLS"' "$(cat "$CALLS")"
jwt="$(cat "$TMP/last-jwt")"
h="${jwt%%.*}"; rest="${jwt#*.}"; p="${rest%%.*}"; s="${rest#*.}"
b64d() { local x="$1"; while [ $(( ${#x} % 4 )) -ne 0 ]; do x="$x="; done; printf '%s' "$x" | tr '_-' '/+' | openssl base64 -d -A; }
payload="$(b64d "$p")"
check "the JWT names the App and expires within ten minutes" \
  'printf "%s" "$payload" | python3 -c "import sys,json;d=json.load(sys.stdin);sys.exit(0 if d[\"iss\"]==\"1111\" and d[\"exp\"]-d[\"iat\"]<=600 else 1)"' "$payload"
b64d "$s" > "$TMP/sig.bin"
printf '%s.%s' "$h" "$p" > "$TMP/signed.txt"
check "its signature verifies against the App's public key" \
  'openssl dgst -sha256 -verify "$TMP/author.pub" -signature "$TMP/sig.bin" "$TMP/signed.txt" >/dev/null 2>&1'
check "the cached token file is private" '[ "$(ls -l "$GH_APP_CACHE/demo-author.token" | cut -c1-10)" = "-rw-------" ]'

echo "2. the cache: reused until five minutes before the hour is up"
GH_APP_NOW=$((NOW + 600)) "$TOKEN" demo author >"$TMP/out" 2>&1
check "ten minutes later: the same token, no second call" '[ "$(cat "$TMP/out")" = ghs_fixture1 ] && [ "$(wc -l < "$CALLS" | tr -d " ")" = 1 ]' "$(cat "$TMP/out")"
GH_APP_NOW=$((exp_epoch - 200)) "$TOKEN" demo author >"$TMP/out" 2>&1
check "inside the last five minutes: a fresh token is minted" '[ "$(cat "$TMP/out")" = ghs_fixture2 ]' "$(cat "$TMP/out")"

echo "3. failures say so and print no token"
rm -rf "$GH_APP_CACHE"; touch "$TMP/curl-fails"
OUT="$(GH_APP_NOW=$NOW "$TOKEN" demo author 2>"$TMP/err")"; RC=$?
check "GitHub refusing: exit 1, empty stdout, one line why" '[ "$RC" = 1 ] && [ -z "$OUT" ] && grep -q "refused the token" "$TMP/err"' "rc=$RC [$OUT] $(cat "$TMP/err")"
rm -f "$TMP/curl-fails"
OUT="$(GH_APP_DIR="$TMP/nowhere" "$TOKEN" demo author 2>"$TMP/err")"; RC=$?
check "no App set up: exit 1 and says so" '[ "$RC" = 1 ] && [ -z "$OUT" ] && grep -q "no App set up" "$TMP/err"'
OUT="$("$TOKEN" demo wizard 2>&1)"; RC=$?
check "an unknown role is a usage error" '[ "$RC" = 2 ]'
mv "$APPS/author.pem" "$APPS/author.pem.off"
OUT="$(GH_APP_NOW=$NOW "$TOKEN" demo author 2>"$TMP/err")"; RC=$?
check "a missing key: exit 1 and names the file" '[ "$RC" = 1 ] && grep -q "author.pem" "$TMP/err"'
mv "$APPS/author.pem.off" "$APPS/author.pem"

echo "4. the project: from the argument, the lane's PROJECT_NAME, or the git remote"
mkdir -p "$TMP/repo" && git -C "$TMP/repo" init -q && git -C "$TMP/repo" remote add origin https://github.com/lonestarprod/demo.git
rm -rf "$GH_APP_CACHE"
OUT="$(cd "$TMP/repo" && GH_APP_DIR= HOME="$TMP/home" GH_APP_NOW=$NOW "$TOKEN" author 2>&1)"; RC=$?
check "no project given, no App for the remote's name: exit 1 names that project" '[ "$RC" = 1 ] && printf "%s" "$OUT" | grep -q "no App set up for demo"' "$OUT"
mkdir -p "$TMP/home/.config/roloc-apps"; cp -R "$APPS" "$TMP/home/.config/roloc-apps/demo"
OUT="$(cd "$TMP/repo" && GH_APP_DIR= HOME="$TMP/home" GH_APP_NOW=$NOW "$TOKEN" author 2>&1)"; RC=$?
check "a Mac's ~/.config/roloc-apps/<project> is found from the remote's repo name" '[ "$RC" = 0 ] && [ "${OUT#ghs_}" != "$OUT" ]' "$OUT"
OUT="$(GH_APP_DIR= HOME="$TMP/home" PROJECT_NAME=demo GH_APP_NOW=$NOW "$TOKEN" author 2>&1)"
check "PROJECT_NAME (the lane's) names it too" '[ "${OUT#ghs_}" != "$OUT" ]' "$OUT"
# The Mac's desk App (card #288): WATCHER_APP_PROJECT wins over the checkout's remote, so a session
# in the riffmap checkout still mints the desk App, never riffmap's lane App.
mkdir -p "$TMP/home/.config/roloc-apps/desk"; cp "$APPS/author.pem" "$TMP/home/.config/roloc-apps/desk/author.pem"
cat > "$TMP/home/.config/roloc-apps/desk/config" <<'EOC'
AUTHOR_APP_ID=7777
AUTHOR_INSTALLATION_ID=8888
AUTHOR_SLUG=roloc-desk-author
AUTHOR_BOT_ID=9999
EOC
OUT="$(cd "$TMP/repo" && GH_APP_DIR= HOME="$TMP/home" WATCHER_APP_PROJECT=desk GH_APP_NOW=$NOW "$TOKEN" author 2>&1)"; RC=$?
check "WATCHER_APP_PROJECT=desk inside another project's checkout mints the desk App, not the remote's (card #288)" \
  '[ "$RC" = 0 ] && [ "${OUT#ghs_}" != "$OUT" ] && grep -q "installations/8888/access_tokens" "$CALLS"' "rc=$RC $OUT"
OUT="$(cd "$TMP/repo" && GH_APP_DIR= HOME="$TMP/home" WATCHER_APP_PROJECT=desk "$TOKEN" author --identity 2>&1)"
check "…and signs as roloc-desk-author[bot]" '[ "$OUT" = "roloc-desk-author[bot] <9999+roloc-desk-author[bot]@users.noreply.github.com>" ]' "$OUT"

echo "5. the git identity"
OUT="$("$TOKEN" demo author --identity)"
check "author: <slug>[bot] with the noreply address carrying the bot's id" '[ "$OUT" = "roloc-demo-author[bot] <3333+roloc-demo-author[bot]@users.noreply.github.com>" ]' "$OUT"
OUT="$("$TOKEN" demo reviewer --identity)"
check "no recorded id: the plain noreply address" '[ "$OUT" = "roloc-demo-reviewer[bot] <roloc-demo-reviewer[bot]@users.noreply.github.com>" ]' "$OUT"

echo "6. bin/shim/gh: the author token for the real gh, or the real gh untouched"
SHIM="$HERE/shim/gh"
rm -rf "$GH_APP_CACHE"
OUT="$(GH_APP_NOW=$NOW PROJECT_NAME=demo "$SHIM" api user)"
check "an App is set up: gh runs with the minted token and the arguments intact" '[ "${OUT#real-gh token=ghs_fixture}" != "$OUT" ] && [ "${OUT%args=api user}" != "$OUT" ]' "$OUT"
OUT="$(GH_APP_NOW=$NOW PROJECT_NAME=demo WATCHER_APP_ROLE=reviewer "$SHIM" api user)"
check "WATCHER_APP_ROLE=reviewer mints the reviewer's" '[ "$OUT" != "real-gh token=unset args=api user" ] && printf "%s" "$OUT" | grep -q "token=ghs_fixture"' "$OUT"
OUT="$(GH_APP_DIR="$TMP/nowhere" PROJECT_NAME=demo "$SHIM" api user)"
check "no App set up: gh runs exactly as before, no GH_TOKEN added" '[ "$OUT" = "real-gh token=unset args=api user" ]' "$OUT"
OUT="$(GH_TOKEN=mine GH_APP_NOW=$NOW PROJECT_NAME=demo "$SHIM" api user)"
check "a token the caller already set is kept" '[ "$OUT" = "real-gh token=mine args=api user" ]' "$OUT"
touch "$TMP/curl-fails"; rm -rf "$GH_APP_CACHE"
OUT="$(GH_APP_NOW=$NOW PROJECT_NAME=demo "$SHIM" api user 2>&1)"; RC=$?
check "an App set up but GitHub refusing: exit 1, and gh never runs (no quiet fall back to a machine account)" '[ "$RC" = 1 ] && ! printf "%s" "$OUT" | grep -q real-gh' "rc=$RC $OUT"
rm -f "$TMP/curl-fails"
OUT="$(env PATH="$HERE/shim:$TMP/nobin" "$SHIM" api user 2>&1)"; RC=$?
check "no real gh anywhere: exit 127, never an exec loop" '[ "$RC" = 127 ]' "rc=$RC $OUT"
OUT="$(PATH="$HERE/shim:$BIN:/usr/bin:/bin" GH_APP_NOW=$NOW PROJECT_NAME=demo gh api user)"
check "first on PATH, it finds the next gh and never itself" 'printf "%s" "$OUT" | grep -q "^real-gh token=ghs_"' "$OUT"

# Card #293: two releases' shims on one PATH (a long-lived lane keeps the old release's) must
# reach the real gh in one hop, and with no real gh exit 127 instead of exec'ing each other.
mkdir -p "$TMP/bashonly"; ln -sf "$(command -v bash)" "$TMP/bashonly/bash"
mkdir -p "$TMP/relA/bin/shim" "$TMP/relB/bin/shim"
cp "$SHIM" "$TMP/relA/bin/shim/gh"; cp "$SHIM" "$TMP/relB/bin/shim/gh"
OUT="$(env -u WATCHER_REAL_GH GH_TOKEN=mine PATH="$TMP/relA/bin/shim:$TMP/relB/bin/shim:$BIN:/usr/bin:/bin" "$TMP/relB/bin/shim/gh" api user 2>&1)"; RC=$?
check "the new release's shim, the old one's first on PATH: one hop to the real gh" '[ "$RC" = 0 ] && [ "$OUT" = "real-gh token=mine args=api user" ]' "rc=$RC $OUT"
OUT="$(env -u WATCHER_REAL_GH PATH="$TMP/relA/bin/shim:$TMP/relB/bin/shim:$TMP/bashonly" "$TMP/relB/bin/shim/gh" api user 2>&1)"; RC=$?
check "two shims and no real gh: exit 127, never a loop" '[ "$RC" = 127 ] && printf "%s" "$OUT" | grep -q "no real gh"' "rc=$RC $OUT"
cp "$SHIM" "$TMP/relA/gh-copy"
OUT="$(env WATCHER_REAL_GH="$TMP/relA/gh-copy" PATH="$TMP/bashonly" "$TMP/relB/bin/shim/gh" api user 2>&1)"; RC=$?
check "WATCHER_REAL_GH naming a file with the shim's marker is refused" '[ "$RC" = 127 ]' "rc=$RC $OUT"
OUT="$(env WATCHER_GH_SHIM_DEPTH=8 PATH="$BIN:/usr/bin:/bin" "$SHIM" api user 2>&1)"; RC=$?
check "the recursion guard: a shim called through eight shims exits 127 loudly" '[ "$RC" = 127 ] && printf "%s" "$OUT" | grep -q "refusing to loop"' "rc=$RC $OUT"

echo "7. bin/shim/gh-credential: git's helper protocol"
CRED="$HERE/shim/gh-credential"
rm -rf "$GH_APP_CACHE"
OUT="$(printf 'protocol=https\nhost=github.com\n\n' | GH_APP_NOW=$NOW PROJECT_NAME=demo "$CRED" get)"
check "get on github.com: x-access-token and the minted token" 'printf "%s" "$OUT" | grep -qx "username=x-access-token" && printf "%s" "$OUT" | grep -q "^password=ghs_fixture"' "$OUT"
OUT="$(printf 'protocol=https\nhost=example.com\n\n' | GH_APP_NOW=$NOW PROJECT_NAME=demo "$CRED" get)"
check "another host: nothing printed" '[ -z "$OUT" ]' "$OUT"
OUT="$(printf 'protocol=https\nhost=github.com\n\n' | GH_APP_DIR="$TMP/nowhere" PROJECT_NAME=demo "$CRED" get)"
check "no App set up: nothing printed, git falls through" '[ -z "$OUT" ]' "$OUT"
OUT="$(printf 'protocol=https\nhost=github.com\npassword=x\n\n' | PROJECT_NAME=demo "$CRED" store)"
check "store writes nothing down" '[ -z "$OUT" ]'

echo
if [ "$fail" = 0 ]; then echo "test-gh-app-token.sh: $n checks, all passed"; else echo "test-gh-app-token.sh: $n checks, SOME FAILED"; fi
exit $fail
