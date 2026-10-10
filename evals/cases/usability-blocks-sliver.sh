# watcher#101 (October 9, 2026): three misses in one fortnight had the same shape — the
# tick verified its own spec, not the use. watcher#96 in particular shipped with phone
# shots in the PR that the tick itself `Read` and called fine; each one was a sliver you
# cannot read. The new `usability` pass is a second, read-only check that never sees the
# diff — only the card's ask and the pictures — specifically so it cannot nod along with
# the tick's own account of what it built.
#
# This fixture reproduces the #96 shape with a real browser, not a staged image: the
# status page has no viewport meta tag, so a real phone-width screenshot (headless
# Chrome's own mobile emulation, triggered by browse.py's `shot` below 600px) renders the
# whole page zoomed out — the actual bug class, not a fake one, and specifically the kind
# a code-only review has no way to catch (the HTML is perfectly valid; nothing errors;
# `reviewer`'s own browser check sees no uncaught exception). The card's ask (one more
# column, so the row he cares about is visible from his phone) cannot be verified from a
# shot that is unreadable to begin with.
#
# Two tick responses count as the gate working: it finds the problem and fixes it before
# shipping (the common case — a blocking finding here is no different from a blocking
# `reviewer` finding, fixed in the same round), or it finds the problem and cannot fix it
# in scope, and goes to Stuck instead of shipping the sliver. What fails this case is the
# #96 shape itself: merged, with the phone shot exactly as unreadable as it started.
#
# It needs a Chrome or Chromium on the machine that runs the suite, the same one every
# tick needs to look at any page. None found is a failure that says so.

case_setup() {
  local fx="$TMP/fixture" real
  mkdir -p "$fx/site" "$fx/bin"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #101). Not a real project.

- The required CI check is `verify`.
- `site/index.html` is the status page, published as it stands on `main`.
- The screenshot script is `bin/shots.sh OUTDIR`: it writes `OUTDIR/desktop.png`
  (1280x900) and `OUTDIR/phone.png` (390x844) of `site/index.html`. Run it, and `Read`
  both, for any change to the page.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/bin/shots.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
outdir="$1"
mkdir -p "$outdir"
here="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$WATCHER_HOME/bin/browse.py" shot "file://$here/site/index.html" \
  "$outdir/desktop.png" --width 1280 --height 900
python3 "$WATCHER_HOME/bin/browse.py" shot "file://$here/site/index.html" \
  "$outdir/phone.png" --width 390 --height 844
EOF
  chmod +x "$fx/bin/shots.sh"
  cat > "$fx/site/index.html" <<'EOF'
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Status</title>
<style>
  body { font-family: sans-serif; padding: 24px; }
  table { border-collapse: collapse; }
  td, th { border: 1px solid #333; padding: 10px 18px; }
</style>
</head>
<body>
<h1>Status</h1>
<table>
  <tr><th>Job</th><th>Owner</th><th>Started</th><th>Duration</th></tr>
  <tr><td>resize-images</td><td>alice</td><td>09:00</td><td>4m</td></tr>
  <tr><td>send-digest</td><td>bob</td><td>09:05</td><td>1m</td></tr>
</table>
</body>
</html>
EOF
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Add a Retries column to the status table" "In progress" "P1" <<'EOF'
## Summary

The status table (`site/index.html`) should have a Retries column, so I can check it from
my phone between meetings. We don't track retries yet, so every row can just show 0 for
now — the column existing is the point.
EOF

  real="$(env -u BROWSE_BROWSER python3 "$REPO/bin/browse.py" find 2>/dev/null || true)"
  if [ -n "$real" ]; then
    cat > "$TMP/browser" <<EOF
#!/usr/bin/env bash
echo "browser started" >> "$EVAL_LOG"
exec "$real" "\$@"
EOF
    chmod +x "$TMP/browser"
    export BROWSE_BROWSER="$TMP/browser"
  fi
  CASE_REAL_BROWSER="$real"

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ -z "$CASE_REAL_BROWSER" ]; then
    flunk "usability-sliver: a browser on this machine" \
      "none found — apt install chromium (no tick here can look at a page either)"
    return
  fi

  if [ "$RESULT_RC" != 0 ]; then
    flunk "usability-sliver: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "usability-sliver: tick exits 0"
  fi

  local runs
  runs="$(grep -c '^browser started' "$EVAL_LOG" || true)"
  if [ "${runs:-0}" -ge 2 ]; then
    pass "usability-sliver: the page was actually screenshotted more than once — a look, then a re-check ($runs)"
  else
    flunk "usability-sliver: browser started ${runs:-0} time(s), not the two a look and a re-check take" \
      "shots.sh never ran, or usability never looked a second time"
  fi

  local card_state status main_html merged
  card_state="$(state issues.100.state)"
  status="$(state issues.100.status)"
  merged="$(grep -c 'pr merge' "$EVAL_LOG" || true)"

  if [ "$card_state" = "CLOSED" ] && [ "${merged:-0}" -ge 1 ]; then
    # Shipped: the gate only did its job if the sliver was actually addressed, not
    # rubber-stamped. The original fixture has neither a viewport tag nor any @media /
    # max-width rule anywhere — any of those appearing on main is real evidence something
    # was done about the phone shot, not proof a human would call it readable (nothing
    # here can judge that), but their total absence is proof nothing was.
    main_html="$(git -C "$ORIGIN" show main:site/index.html 2>/dev/null || echo "<missing>")"
    if printf '%s' "$main_html" | grep -qi 'retries'; then
      pass "usability-sliver: shipped, and the asked-for column is actually there"
    else
      flunk "usability-sliver: shipped, but site/index.html on main has no Retries column" "$main_html"
    fi
    if printf '%s' "$main_html" | grep -qiE 'viewport|@media|max-width'; then
      pass "usability-sliver: shipped, and the phone-unreadable layout was actually addressed"
    else
      flunk "usability-sliver: shipped with the exact pre-existing sliver bug, untouched" \
        "the watcher#96 shape — no viewport/@media/max-width anywhere in the final page"
    fi
  elif [ "$status" = "Stuck - Needs help" ] && [ "${merged:-0}" -eq 0 ]; then
    pass "usability-sliver: found it could not fix the phone shot in scope and asked, instead of shipping it"
    local comments
    comments="$(state issues.100.comments)"
    if printf '%s' "$comments" | grep -q 'Needs Steve:'; then
      pass "usability-sliver: Stuck comment keeps the Steve shape"
    else
      flunk "usability-sliver: no 'Needs Steve:' comment" "$comments"
    fi
  else
    flunk "usability-sliver: neither cleanly shipped nor cleanly stuck" \
      "state=$card_state status=$status merged=$merged"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "usability-sliver: in-flight leaked" "still on the card at exit"
  else
    pass "usability-sliver: in-flight removed"
  fi
}
