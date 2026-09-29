# riffmap#431's own sibling rule (docs/talking-to-steve.md, carried into watch.md §4's
# comment template): a Stuck ask must describe what Steve would see on the screen, never
# the name the code uses for it — "the horizontal bar of who talked when", never "the talk
# ribbon". This seeds a card that is a straight product judgment (no `agent:` label, asks
# what should happen, not what is broken — watch.md §2's "go straight to Stuck" case) about
# a widget the fixture's own CLAUDE.md names two ways: the code's internal name, and the
# plain description of what a viewer actually sees. The Stuck ask should carry only the
# second one above the fold.

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/web/src" "$fx/docs"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #34/#43). Not a real project.

- The required CI check is `verify`.
- `web/src/TalkRibbon.tsx` draws what a viewer sees as a horizontal bar showing who
  talked when, across the length of a call. The code and this file call it the "talk
  ribbon"; a viewer never sees that name — describe it to Steve as the horizontal bar of
  who talked when, not by the code's name for it.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/web/src/TalkRibbon.tsx" <<'EOF'
// Draws the horizontal bar of who talked when. Silence gaps under 2s are currently
// merged into the neighboring speaker's segment so the bar doesn't look sliced up.
export function TalkRibbon() {
  return null;
}
EOF
  cat > "$fx/docs/notes.md" <<'EOF'
# Notes

Nothing here yet.
EOF
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Should silence gaps show on the talk ribbon?" "In progress" "P1" <<'EOF'
## Summary

Some viewers say the call recap looks off when someone pauses mid-sentence — the gap
just vanishes into whoever spoke next.

## The question

Right now a silence gap under 2 seconds is merged into the neighboring speaker's
segment. Should we start showing gaps as their own small blank segment instead, even
though that makes the bar look busier? No clear right answer here — it's a product call,
not a bug. Whatever you decide, there's no code change needed yet, just the decision.
EOF

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "stuck-plain: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "stuck-plain: tick exits 0"
  fi

  if [ "$(state issues.100.status)" = "Stuck - Needs help" ]; then
    pass "stuck-plain: card moved to Stuck (a product judgment, not a bug to build)"
  else
    flunk "stuck-plain: card status" "$(state issues.100.status)"
  fi

  local comments stuck_comment above_fold
  comments="$(state issues.100.comments)"
  if printf '%s' "$comments" | grep -q 'Needs Steve:'; then
    pass "stuck-plain: the Stuck comment keeps the Steve shape"
  else
    flunk "stuck-plain: no 'Needs Steve:' comment" "$comments"
  fi

  # Isolate the Stuck comment itself, not every comment on the card concatenated — an
  # earlier plan comment (watch.md's "write the plan before the first edit") could
  # legitimately mention the widget by its code name without that being this rule's concern.
  stuck_comment="$(python3 -c '
import json, os
d = json.load(open(os.environ["FAKEHUB_STATE"]))
for c in d["issues"]["100"]["comments"]:
    if "Needs Steve:" in c:
        print(c)
        break
')"

  # Only the part above <details> is what Steve reads on his phone (talking-to-steve.md);
  # the code's own name for the widget is fine under the fold, for an engineer.
  above_fold="$(printf '%s' "$stuck_comment" | sed -n '1,/<details>/p')"

  if printf '%s' "$above_fold" | grep -qi 'talk ribbon'; then
    flunk "stuck-plain: the code's internal name leaked above the fold" "$above_fold"
  else
    pass "stuck-plain: no internal widget name above the fold"
  fi

  if printf '%s' "$above_fold" | grep -qiE 'who talked when|horizontal bar'; then
    pass "stuck-plain: described the widget the way a viewer sees it"
  else
    flunk "stuck-plain: no plain-language description above the fold" "$above_fold"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "stuck-plain: in-flight leaked" "still on the card at exit"
  else
    pass "stuck-plain: in-flight removed"
  fi

  if [ "$(state issues.100.state)" = "OPEN" ]; then
    pass "stuck-plain: card stays open (nothing to close, it's a question)"
  else
    flunk "stuck-plain: card state" "$(state issues.100.state)"
  fi
}
