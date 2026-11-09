#!/usr/bin/env bash
#
# Tests for bin/review-submit.sh (card #255). Offline: `gh` and the token script are stubs
# (the script's WATCHER_REVIEW_* seams) that log what they were asked.
#
#   bash bin/test-review-submit.sh      # exit 0 = every case holds
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/review-submit.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }
check() { if eval "$2"; then pass "$1"; else flunk "$1" "${3:-}"; fi; }

SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
OLD=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
CALLS="$TMP/calls"; export CALLS TMP
cat > "$TMP/gh" <<'EOF2'
#!/usr/bin/env bash
echo "gh token=${GH_TOKEN:-unset} repo=${GH_REPO:-unset} $*" >> "$CALLS"
case "$*" in
  "api repos/lonestarprod/demo/pulls/7 --jq .head.sha") cat "$TMP/head" ;;
  *"-X POST repos/lonestarprod/demo/pulls/7/reviews"*) echo "https://github.com/lonestarprod/demo/pull/7#pullrequestreview-1" ;;
  *) echo "stub gh: unhandled $*" >&2; exit 1 ;;
esac
EOF2
cat > "$TMP/mint" <<'EOF2'
#!/usr/bin/env bash
echo "mint $* dir=${GH_APP_DIR:-unset}" >> "$CALLS"
[ -f "$TMP/mint-fails" ] && { echo "no App" >&2; exit 1; }
echo ghs_reviewer_token
EOF2
chmod +x "$TMP/gh" "$TMP/mint"
export WATCHER_REVIEW_GH="$TMP/gh" WATCHER_REVIEW_MINT="$TMP/mint" WATCHER_REVIEW_CACHE="$TMP/cache"
export GH_REPO=lonestarprod/demo
echo "$SHA" > "$TMP/head"
printf 'Reviewed %s: no blocking findings\n' "$SHA" > "$TMP/body"

# run PR SHA VERDICT [BODYARG]: the body is whatever $BODY holds, on stdin.
BODY="$(cat "$TMP/body")"
run() { : > "$CALLS"; OUT="$(printf '%s\n' "$BODY" | "$SCRIPT" "$@" 2>&1)"; RC=$?; }

run 7 "$SHA" approve -
check "an approve on the PR's head: files an APPROVE review on that commit" '[ "$RC" = 0 ] && grep -q "commit_id=$SHA" "$CALLS" && grep -q "event=APPROVE" "$CALLS"' "rc=$RC $OUT $(cat "$CALLS")"
check "…with the reviewer App's token, minted for the repo's project" 'grep -q "gh token=ghs_reviewer_token" "$CALLS" && grep -q "mint demo reviewer" "$CALLS"'
check "…the body (read from stdin) is the first line, and the review URL is printed" 'grep -q "body=Reviewed $SHA: no blocking findings" "$CALLS" && [ "${OUT#https://github.com/}" != "$OUT" ]' "$OUT"
check "…GH_REPO is not passed on to gh (the path carries the repo)" '! grep -q "repo=lonestarprod" "$CALLS"'
run 7 "$SHA" request-changes -
check "request-changes files REQUEST_CHANGES" '[ "$RC" = 0 ] && grep -q "event=REQUEST_CHANGES" "$CALLS"' "$OUT"
echo "$OLD" > "$TMP/head"
run 7 "$SHA" approve -
check "a sha that is not the head now: refused, no review filed" '[ "$RC" = 1 ] && ! grep -q "X POST" "$CALLS" && printf "%s" "$OUT" | grep -q "not the head"' "rc=$RC $OUT"
echo "$SHA" > "$TMP/head"

run 7 "$SHA" merge -;                check "a verdict that is not approve/request-changes: usage error" '[ "$RC" = 2 ] && [ ! -s "$CALLS" ]'
run 7 "${SHA%?}" approve -;          check "a short sha: usage error, nothing called" '[ "$RC" = 2 ] && [ ! -s "$CALLS" ]'
run "7;id" "$SHA" approve -;         check "a PR number with anything else in it: usage error" '[ "$RC" = 2 ] && [ ! -s "$CALLS" ]'
run 7 "$SHA" approve;                          check "too few arguments: usage error" '[ "$RC" = 2 ]'
# The helper runs as the user that can read the reviewer key: a path as the body would have
# the key posted back as a review (found in review of #256). Only a literal - is accepted.
printf 'PRIVATE KEY MATERIAL\n' > "$TMP/secret"
run 7 "$SHA" approve "$TMP/secret"
check "a file path as the body: usage error, the file is never read, nothing called" '[ "$RC" = 2 ] && [ ! -s "$CALLS" ] && ! printf "%s" "$OUT" | grep -q "PRIVATE KEY"' "rc=$RC $OUT"
BODY=""; run 7 "$SHA" approve -; check "an empty body: refused" '[ "$RC" = 2 ] && [ ! -s "$CALLS" ]' "rc=$RC $OUT"
BODY="$(cat "$TMP/body")"
GH_REPO="x;y/z" run 7 "$SHA" approve -; check "a GH_REPO that is not owner/name: refused" '[ "$RC" = 2 ] && [ ! -s "$CALLS" ]'
GH_REPO= run 7 "$SHA" approve -;     check "no GH_REPO: refused" '[ "$RC" = 2 ]'
touch "$TMP/mint-fails"; run 7 "$SHA" approve -
check "no reviewer App for the project: exit 1, no review, no gh call" '[ "$RC" = 1 ] && ! grep -q "^gh" "$CALLS"' "rc=$RC $OUT"
rm -f "$TMP/mint-fails"
BODY="$(head -c 9000 /dev/zero | tr '\0' x)"; run 7 "$SHA" approve -
check "a huge body is cut at 4000 bytes" '[ "$(grep -o "body=x*" "$CALLS" | head -1 | wc -c | tr -d " ")" = 4006 ]'

echo
if [ "$fail" = 0 ]; then echo "test-review-submit.sh: $n checks, all passed"; else echo "test-review-submit.sh: $n checks, SOME FAILED"; fi
exit $fail
