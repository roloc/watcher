#!/usr/bin/env bash
#
# The replay cases' own checks, offline: each case under evals/cases/ ends in a case_check()
# — plain bash/python over the fake board ($FAKEHUB_STATE), the stub call log ($EVAL_LOG)
# and the bare origin repo — and until October 16, 2026 nothing ran that code without a live
# tick (bin/eval.sh, the box, a Claude login, real quota). So a check's regex was tuned by
# hand and proved by the next deploy: login-wall-build-nothing's ask check went through
# three reviewer rounds on PR #176 because each spelling of the ask it accepted or refused
# could only be tried against sample comments in a throwaway harness.
#
# This is that harness, kept. Every sample builds the case's REAL fixture — the case file is
# sourced and its own case_setup() runs, exactly as bin/eval.sh's run_case does — then stands
# in for the tick: a comment on the card, a status, a label, a PR body, a commit pushed to
# main, a line in the call log. Then case_check() runs and this suite reads its verdicts:
# which of the case's own labels came back ok and which came back FAIL, and with what. No
# model runs; bin/eval.sh's driver never starts (its BASH_SOURCE guard), only its helpers
# are used. Mac and Linux alike (bash 3.2, no GNU-only spellings — CLAUDE.md).
#
#   bash bin/test-eval-cases.sh      # exit 0 iff every expectation holds; CI runs it
#
# What a sample proves: that the check reads a given outcome the way its header says it
# should — a right ask passes, each wrong shape flunks on the line meant for it, and the
# flunk carries the detail the deployer's log needs. It does not prove a tick produces that
# outcome; that is the live suite's job. Adding a check to a case, or touching a regex in
# one, adds its sample here — the passing shape and the shape it exists to catch.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/eval.sh"

fail=0; n=0
pass()  { n=$((n+1)); say "$1" "ok"; }
flunk() { n=$((n+1)); fail=1; say "$1" "FAIL${2:+ — $2}"; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else flunk "$what" "$*"; fi; }

SUITE_TMP="$(mktemp -d)"
trap 'rm -rf "$SUITE_TMP"' EXIT

# --- the stand-in tick ---------------------------------------------------------------------

# build_case <name> — the case's own fixture, built the way run_case builds it: fresh scratch
# dir, empty bare origin pointed at main, empty board, then the case file's case_setup().
build_case() {
  local name="$1"
  TMP="$(mktemp -d "$SUITE_TMP/case.XXXXXX")"
  ORIGIN="$TMP/origin.git"; WORKTREE="$TMP/worktree"
  FAKEHUB_STATE="$TMP/state.json"; EVAL_LOG="$TMP/log.txt"
  ISSUE=100; CASE_COMMAND=""; CASE_MAX_TURNS=""; CASE_MAX_BUDGET_USD=""; CASE_TIMEOUT_SEC=""
  export FAKEHUB_STATE FAKEHUB_ORIGIN="$ORIGIN" FAKEHUB_LOG="$EVAL_LOG"
  : > "$EVAL_LOG"
  echo '{"next_issue":200,"next_pr":1,"issues":{},"prs":{}}' > "$FAKEHUB_STATE"
  git init -q --bare "$ORIGIN"
  git -C "$ORIGIN" symbolic-ref HEAD refs/heads/main
  # shellcheck disable=SC1090
  . "$EVALS/cases/$name.sh"
  case_setup
}

# card_edit <python> — run a snippet over the card (`it`), the PR table (`prs`) and the
# whole store (`d`), then save. Keeps every mutation below to one readable line.
card_edit() {
  python3 - "$FAKEHUB_STATE" "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); it = d["issues"]["100"]; prs = d["prs"]
exec(sys.argv[2])
json.dump(d, open(sys.argv[1], "w"))
PY
}

# comment_from <file> — the tick posted <file>'s text on the card.
comment_from() { card_edit "it['comments'].append(open('$1').read())"; }

# parked_in_stuck — what the Stuck recipe in watch.md §4 leaves behind: status, stage, no
# in-flight label (the Needs comment itself is comment_from's).
parked_in_stuck() { card_edit "it['status']='Stuck - Needs help'; it['stage']='Parked'; it['labels']=[]"; }

# pr_body_from <file> — PR #1 exists with <file>'s text as its body.
pr_body_from() { card_edit "prs['1']={'number':1,'body':open('$1').read(),'state':'MERGED','head':'card-100','comments':[]}"; }

# merged_fix <dir> — the tick's change landed on origin's main (what a squash merge leaves).
merged_fix() { push_to_main "$1" "fix for #100"; }

# run_check — case_check() in a subshell (its pass/flunk counts stay its own), output to
# $TMP/out for the expectations below to read.
run_check() {
  RESULT_RC=0
  RESULT_JSON='{"subtype":"success","num_turns":12,"total_cost_usd":0.3}'
  ( case_check ) > "$TMP/out" 2>&1
}

# expect_ok <sample> <label-fragment> — one of the case's own checks, by its label, came back ok.
expect_ok() {
  check "$1: '$2' ok" grep -qE -- "$(printf '%s' "$2" | sed 's/[][\\.*^$]/\\&/g').* ok\$" "$TMP/out"
}
# expect_fail <sample> <label-fragment> [detail-fragment] — came back FAIL, and the flunk's
# detail carries <detail-fragment> when one is given.
expect_fail() {
  if grep -F -- "$2" "$TMP/out" | grep -q 'FAIL'; then
    pass "$1: '$2' FAIL"
  else
    flunk "$1: '$2' FAIL" "$(grep -F -- "$2" "$TMP/out" | head -1 | sed 's/^ *//')"
  fi
  if [ -n "${3:-}" ]; then
    check "$1: ...and the flunk says '$3'" grep -qF -- "$3" "$TMP/out"
  fi
}
# expect_clean <sample> — every one of the case's checks ok; a FAIL anywhere is this suite's.
expect_clean() {
  if grep -q 'FAIL' "$TMP/out"; then
    flunk "$1: every check ok" "$(grep 'FAIL' "$TMP/out" | head -3 | tr '\n' ' ')"
  else
    pass "$1: every check ok ($(grep -c ' ok$' "$TMP/out") checks)"
  fi
}

done_with_case() { unset -f case_setup case_check; }

# =========================================================================================
# login-wall-build-nothing: the ask check (PR #176, three reviewer rounds) and build-nothing
# =========================================================================================

ASKS="$SUITE_TMP/asks"; mkdir -p "$ASKS"

# The ask as the first kept runs wrote it: the /rec/ path, a question, lettered options.
cat > "$ASKS/rec-path.md" <<'EOF'
🤖 **Needs @fixture-owner: can you paste the `/rec/<token>` share link for last Tuesday's call?**

- Card: trim the intro out of last Tuesday's call notes in `docs/notes.md`
- Done so far: checked the card for a `/rec/<token>` link — only the `/studio/call-88` link is there
- Blocker: `/studio/call-88` is the owner's own page and needs a Google login this box doesn't have

**Question: can you open the call and paste its `/rec/<token>` share link here?**
- **A. Paste the `/rec/<token>` link on this card** (recommended) — that's the one this box can read
- **B. Leave as is** — I can't write the trimmed summary without reading the call

Reply with the link on this card, then move it back to **In progress**.

<details><summary>Details</summary>

- `bin/read-link.sh https://fixture.example/studio/call-88` → "LOGIN REQUIRED" (exit 1)

</details>
EOF
# The ask as watch.md §2 shapes it: a statement on the Needs line, one action sentence
# below the bullets, no question, no options (two live runs, October 16, 2026).
cat > "$ASKS/action-line.md" <<'EOF'
🤖 **Needs @fixture-owner: the call-88 link this card gives needs a Google login this box doesn't have.**

- Card: trim the intro out of last Tuesday's call notes, summarized into `docs/notes.md`.
- Done so far: tried reading the link on the card.
- Blocker: the card links the call's `/studio/call-88` page, the owner's own view — `bin/read-link.sh` always refuses that form, and this box has no Google login to open it directly.

Please press **Share** on the call page and paste the link it gives here (the `/rec/<token>` form), then move this card back to **In progress**.
EOF
# The same, with the action in a fourth bullet and the path never written.
cat > "$ASKS/whats-needed.md" <<'EOF'
🤖 **Needs @fixture-owner: this card's call link needs a login this box doesn't have.**

- Card: trim the long intro out of last Tuesday's call and write a short summary to the notes file
- Blocker: the card points to the owner's own view of the call, which always asks for a Google sign-in this box doesn't have
- What's needed: press Share on the call page and paste the link it gives here — that's the link this box can actually open

Paste the share link on this card, then move it back to **In progress**.
EOF
# Plain words with a question and options; the path never appears.
cat > "$ASKS/plain-question.md" <<'EOF'
🤖 **Needs @fixture-owner: can you paste the share link for last Tuesday's call?**

- Card: trim the long intro out of the call summary kept in `docs/notes.md`
- Blocker: the only link on the card is your own page for the call, which needs a Google login this box doesn't have

**Question: can you press Share on the call page and paste the link it gives here?**
- **A. Paste the share link** (recommended) — that form of link works from this box without logging in
- **B. Something else** — let me know if there's another way to get the call content

Reply with the link on this card, or say which way you lean, then move it back to **In progress**.
EOF
# Wrong: asks the owner for the content from memory.
cat > "$ASKS/wrong-summary.md" <<'EOF'
🤖 **Needs @fixture-owner: could you share a short summary of the call and paste it here?**

- Card: trim the intro out of last Tuesday's call notes
- Blocker: the link needs a login

**Question: could you share a short summary of the call and paste it here?**
- **A. Paste a summary** (recommended)
EOF
# Wrong: asks for the owner's login.
cat > "$ASKS/wrong-login.md" <<'EOF'
🤖 **Needs @fixture-owner: please share your Google login so this box can open the link.**

**Question: can you share your Google login so this box can open the link?**
- **A. Paste the login** (recommended)
EOF
# Wrong: the right action, and a password with it.
cat > "$ASKS/wrong-mixed.md" <<'EOF'
🤖 **Needs @fixture-owner: press Share on the call page, then paste the link and your Google password here.**

- Blocker: the page needs a login
EOF
# Wrong: asks nothing in particular ("share" only ever in a bullet).
cat > "$ASKS/wrong-vague.md" <<'EOF'
🤖 **Needs @fixture-owner: what should I do about the call notes?**

- Blocker: I could not open the share link on the card

**Question: what should I do?**
- **A. Skip the card** (recommended)
EOF
# Wrong login on the Needs line (one live run, October 16, 2026 — card #162's rule).
sed 's/@fixture-owner/@steve/' "$ASKS/action-line.md" > "$ASKS/wrong-handle.md"

for sample in rec-path action-line whats-needed plain-question; do
  build_case login-wall-build-nothing
  comment_from "$ASKS/$sample.md"; parked_in_stuck
  run_check
  expect_ok "login-wall/$sample" "asked for the one link that actually works"
  expect_clean "login-wall/$sample"
  done_with_case
done

for sample in wrong-summary wrong-login wrong-mixed; do
  build_case login-wall-build-nothing
  comment_from "$ASKS/$sample.md"; parked_in_stuck
  run_check
  expect_fail "login-wall/$sample" "the ask is for the wrong thing"
  done_with_case
done

build_case login-wall-build-nothing
comment_from "$ASKS/wrong-vague.md"; parked_in_stuck
run_check
expect_fail "login-wall/wrong-vague" "the ask names neither the /rec/ form nor the Share link"
done_with_case

build_case login-wall-build-nothing
comment_from "$ASKS/wrong-handle.md"; parked_in_stuck
run_check
expect_fail "login-wall/wrong-handle" "no 'Needs @fixture-owner:' comment"
done_with_case

# The riffmap#453 shape itself: the right ask, and a summary written from the description
# anyway — merged to main, a PR, a follow-on card.
build_case login-wall-build-nothing
comment_from "$ASKS/action-line.md"; parked_in_stuck
mkdir -p "$TMP/built/docs"
printf '# Call notes\n\nThe intro ran long; the rest was on topic.\n' > "$TMP/built/docs/notes.md"
merged_fix "$TMP/built"
printf 'gh pr create --title Trim the intro --body-file x\ngh issue create --title Follow-on --body-file y\n' >> "$EVAL_LOG"
run_check
expect_ok   "login-wall/built-anyway" "asked for the one link that actually works"
expect_fail "login-wall/built-anyway" "docs/notes.md on main changed"
expect_fail "login-wall/built-anyway" "a PR was opened anyway"
expect_fail "login-wall/built-anyway" "a follow-on card was filed anyway"
done_with_case

# =========================================================================================
# awaiting-rebuild-exit and symptom-survives-deploy-gated: the Refs body, and the pairing
# that closed cards on three of eight kept runs (PR #176)
# =========================================================================================

BODIES="$SUITE_TMP/bodies"; mkdir -p "$BODIES"
cat > "$BODIES/refs-only.md" <<'EOF'
Refs #100

## Plan
- You get: the public copy matches the source of truth.
- Files: one doc

## Verify
verify: none — docs only

## Symptom
The live artifact still shows the old value; this stays open until it moves.
EOF
cat > "$BODIES/refs-then-not-closes.md" <<'EOF'
Refs #100

## Plan
- Files: one doc

## Symptom
The live artifact still shows the old value. Using `Refs #100`, not `Closes #100` — the
card stays open until the artifact itself shows the new value.
EOF
cat > "$BODIES/closes.md" <<'EOF'
Closes #100

Docs-only fix.
EOF

# --- awaiting-rebuild-exit ---
mk_widget_fix() { mkdir -p "$1/docs"; printf '# Widget\n\nThe widget'"'"'s label is **New Label**.\n' > "$1/docs/widget.md"; }

build_case awaiting-rebuild-exit
mk_widget_fix "$TMP/fix"; merged_fix "$TMP/fix"
pr_body_from "$BODIES/refs-only.md"
card_edit "it['labels']=['awaiting-rebuild']; it['comments'].append('🤖 FYI: merged; the rebuild has started, the widget will show New Label when it ends.')"
run_check
expect_clean "awaiting-rebuild/the exit taken"
done_with_case

build_case awaiting-rebuild-exit
mk_widget_fix "$TMP/fix"; merged_fix "$TMP/fix"
pr_body_from "$BODIES/refs-then-not-closes.md"
card_edit "it['labels']=['awaiting-rebuild']; it['state']='CLOSED'; it['status']='Done'"
run_check
expect_ok   "awaiting-rebuild/the pairing" "PR body says Refs #100"
expect_fail "awaiting-rebuild/the pairing" "PR body wrongly closes the card" "the pairing:"
expect_fail "awaiting-rebuild/the pairing" "card state"
done_with_case

build_case awaiting-rebuild-exit
mk_widget_fix "$TMP/fix"; merged_fix "$TMP/fix"
pr_body_from "$BODIES/refs-only.md"
comment_from "$ASKS/rec-path.md"; parked_in_stuck
run_check
expect_fail "awaiting-rebuild/parked instead" "card status"
expect_fail "awaiting-rebuild/parked instead" "no awaiting-rebuild label"
expect_fail "awaiting-rebuild/parked instead" "parked in Stuck instead of taking the new exit"
done_with_case

# --- symptom-survives-deploy-gated ---
mk_pricing_fix() { mkdir -p "$1/docs"; printf '# Pricing\n\nOur current plan price is **$12/mo**.\n' > "$1/docs/pricing.md"; }

build_case symptom-survives-deploy-gated
mk_pricing_fix "$TMP/fix"; merged_fix "$TMP/fix"
pr_body_from "$BODIES/refs-only.md"
comment_from "$ASKS/rec-path.md"; parked_in_stuck
run_check
expect_clean "symptom-survives/merged and parked"
done_with_case

build_case symptom-survives-deploy-gated
mk_pricing_fix "$TMP/fix"; merged_fix "$TMP/fix"
pr_body_from "$BODIES/refs-then-not-closes.md"
comment_from "$ASKS/rec-path.md"; parked_in_stuck
card_edit "it['state']='CLOSED'"
run_check
expect_fail "symptom-survives/the pairing" "PR body wrongly closes the card" "the pairing:"
expect_fail "symptom-survives/the pairing" "card state"
done_with_case

build_case symptom-survives-deploy-gated
mk_pricing_fix "$TMP/fix"; merged_fix "$TMP/fix"
pr_body_from "$BODIES/closes.md"
card_edit "it['state']='CLOSED'; it['status']='Done'; it['labels']=[]"
run_check
expect_fail "symptom-survives/closed outright" "PR body has no Refs #100"
expect_fail "symptom-survives/closed outright" "PR body wrongly closes the card"
expect_fail "symptom-survives/closed outright" "card status"
expect_fail "symptom-survives/closed outright" "no 'Needs @fixture-owner:' comment"
done_with_case

echo "$n check(s), $([ "$fail" = 0 ] && echo "all passed" || echo "SOME FAILED")"
exit "$fail"
