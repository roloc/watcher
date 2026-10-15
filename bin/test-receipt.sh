#!/usr/bin/env bash
#
# Tests for bin/receipt-check.sh. No network — `gh` is a stub under $TMP/bin that reads
# fixed scenario files and answers exactly the two calls receipt-check.sh makes (`pr view
# --json headRefOid` and `gh api --paginate .../files` or `.../comments`). Each case writes
# its own scenario, runs the real script against it, and checks the exit code and the one
# line it prints.
#
#   bash bin/test-receipt.sh                  # exit 0 = all cases hold; 1 = a case failed
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/receipt-check.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"
mkdir -p "$BIN" "$TMP/scenario"

fail=0; n=0
say()   { printf '  %-78s %s\n' "$1" "$2"; }
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }

cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
# Stub gh: answers exactly the shapes receipt-check.sh calls. S is the scenario dir.
set -u
S="$TEST_RECEIPT_SCENARIO"
case "$1 $2" in
  "pr view")
    json=""
    shift 3
    while [ $# -gt 0 ]; do
      case "$1" in --json) json="$2"; shift 2 ;; *) shift ;; esac
    done
    case "$json" in
      headRefOid) cat "$S/sha" ;;
      *) echo "stub gh: unhandled --json $json" >&2; exit 1 ;;
    esac
    ;;
  "api "*)
    case "$*" in
      *"/files"*) cat "$S/files" ;;
      *"/comments"*) cat "$S/comments" ;;
      *) echo "stub gh: unhandled api call: $*" >&2; exit 1 ;;
    esac
    ;;
  *)
    echo "stub gh: unhandled call: $*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$BIN/gh"
export PATH="$BIN:$PATH"

SHA="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OLD_SHA="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

# scenario SHA FILES COMMENTS: writes the three fixture files the stub `gh` reads.
scenario() {
  printf '%s' "$1" > "$TMP/scenario/sha"
  printf '%s\n' "$2" > "$TMP/scenario/files"
  printf '%s\n' "$3" > "$TMP/scenario/comments"
}

run() {
  OUT="$(TEST_RECEIPT_SCENARIO="$TMP/scenario" RECEIPT_GATE_PATHS="${RECEIPT_GATE_PATHS:-}" RECEIPT_SCREEN_PATHS="${RECEIPT_SCREEN_PATHS:-}" bash "$SCRIPT" 7 2>&1)"
  RC=$?
}

# 1. A matching receipt: the newest reviewer comment names the PR's current head SHA,
# clean verdict. Passes.
scenario "$SHA" "bin/spawn.sh" \
  "🤖 reviewer: Reviewed $SHA: no blocking findings"
run
if [ "$RC" = 0 ]; then pass "matching receipt: exits 0"; else flunk "matching receipt: exits 0" "rc=$RC: $OUT"; fi

# 2. A stale receipt: the newest reviewer comment names an OLDER sha — an amend after
# the review. Fails, and names the stale sha it found.
scenario "$SHA" "bin/spawn.sh" \
  "🤖 reviewer: Reviewed $OLD_SHA: no blocking findings"
run
if [ "$RC" != 0 ]; then pass "stale receipt: exits non-zero"; else flunk "stale receipt: exits non-zero" "rc=$RC: $OUT"; fi
if printf '%s' "$OUT" | grep -q "$OLD_SHA"; then
  pass "stale receipt: names the stale sha it found"
else
  flunk "stale receipt: names the stale sha it found" "$OUT"
fi

# 3. No receipt at all: a code change with no reviewer comment on the PR. Fails, and
# says none was found.
scenario "$SHA" "bin/spawn.sh" ""
run
if [ "$RC" != 0 ]; then pass "no receipt: exits non-zero"; else flunk "no receipt: exits non-zero" "rc=$RC: $OUT"; fi
if printf '%s' "$OUT" | grep -q "found: none"; then
  pass "no receipt: says none found"
else
  flunk "no receipt: says none found" "$OUT"
fi

# 4. A blocking verdict for the current sha: the receipt names the right commit, but the
# verdict itself is "blocking" — never a pass.
scenario "$SHA" "bin/spawn.sh" \
  "🤖 reviewer: Reviewed $SHA: blocking"
run
if [ "$RC" != 0 ]; then pass "blocking verdict: exits non-zero"; else flunk "blocking verdict: exits non-zero" "rc=$RC: $OUT"; fi

# 5. A screen change (RECEIPT_SCREEN_PATHS covers the changed file) with a clean reviewer
# receipt but no usability receipt at all. Fails on the missing usability receipt.
RECEIPT_SCREEN_PATHS="bin/floor.py" scenario "$SHA" "bin/floor.py" \
  "🤖 reviewer: Reviewed $SHA: no blocking findings"
RECEIPT_SCREEN_PATHS="bin/floor.py" run
if [ "$RC" != 0 ]; then pass "screen change, no usability receipt: exits non-zero"; else flunk "screen change, no usability receipt: exits non-zero" "rc=$RC: $OUT"; fi
if printf '%s' "$OUT" | grep -qi usability; then
  pass "screen change, no usability receipt: names usability as the gap"
else
  flunk "screen change, no usability receipt: names usability as the gap" "$OUT"
fi

# 5b. The same screen change, now with both receipts clean. Passes.
RECEIPT_SCREEN_PATHS="bin/floor.py" scenario "$SHA" "bin/floor.py" \
  "$(printf '%s\n%s' "🤖 reviewer: Reviewed $SHA: no blocking findings" "🤖 usability: Usability $SHA: no blocking findings")"
RECEIPT_SCREEN_PATHS="bin/floor.py" run
if [ "$RC" = 0 ]; then pass "screen change, both receipts clean: exits 0"; else flunk "screen change, both receipts clean: exits 0" "rc=$RC: $OUT"; fi

# 6. Docs-only: every changed file is a `.md` outside the gate. Exempt — passes with no
# comments at all, never looking for a receipt.
scenario "$SHA" "docs/talking-to-steve.md" ""
run
if [ "$RC" = 0 ]; then pass "docs-only: exits 0 with no receipt"; else flunk "docs-only: exits 0 with no receipt" "rc=$RC: $OUT"; fi
if printf '%s' "$OUT" | grep -qi "docs-only"; then
  pass "docs-only: says why"
else
  flunk "docs-only: says why" "$OUT"
fi

# 7. A `.md` file that IS part of the merge gate still needs a receipt, even though it
# looks like plain prose.
scenario "$SHA" "commands/watch.md" ""
run
if [ "$RC" != 0 ]; then pass "gate markdown file: still needs a receipt"; else flunk "gate markdown file: still needs a receipt" "rc=$RC: $OUT"; fi

# 8. The newest of two reviewer comments is the one that counts: an older blocking verdict
# followed by a newer clean one for the current sha passes.
scenario "$SHA" "bin/spawn.sh" \
  "$(printf '%s\n%s' "🤖 reviewer: Reviewed $SHA: blocking" "🤖 reviewer: Reviewed $SHA: no blocking findings")"
run
if [ "$RC" = 0 ]; then pass "newest receipt wins over an older blocking one"; else flunk "newest receipt wins over an older blocking one" "rc=$RC: $OUT"; fi

# 9. RECEIPT_GATE_PATHS honors an explicit override: a `.md` file outside the custom gate
# list is exempt even though it would be in-gate under the script's own default.
RECEIPT_GATE_PATHS="only/this/path" scenario "$SHA" "commands/watch.md" ""
RECEIPT_GATE_PATHS="only/this/path" run
if [ "$RC" = 0 ]; then pass "RECEIPT_GATE_PATHS override: a non-gate .md file is exempt"; else flunk "RECEIPT_GATE_PATHS override: a non-gate .md file is exempt" "rc=$RC: $OUT"; fi

echo
if [ "$fail" = 0 ]; then
  echo "test-receipt.sh: $n checks, all passed"
else
  echo "test-receipt.sh: $n checks, SOME FAILED"
fi
exit $fail
