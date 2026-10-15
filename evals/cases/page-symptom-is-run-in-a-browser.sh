# watcher#71 (October 1, 2026): "nothing loads" on a page whose script did not parse. The
# tick fetched it five times with curl, got five clean responses, and went to Stuck to ask
# Steve what he had seen. A browser says "syntax error" on the first load. watch.md §2 now
# says a page that draws itself is looked at by running it, and — the part this case is
# for — that EVERY project has a browser to do it with: "$WATCHER_HOME/bin/browse.py",
# shipped with the box, whether or not the project brings a tool of its own.
#
# So this fixture is a project that brings nothing: a page that draws itself, a CLAUDE.md
# that names no screenshot script and no browser, and a symptom card. The page's script has
# a quote that never closes, so the page shows its heading and nothing else. The right tick
# runs the page in the watcher's browser (before deciding what is wrong, and again before
# the card closes), fixes it, and ships. The case knows the browser really ran because
# BROWSE_BROWSER points at a wrapper that writes one line to the call log and then becomes
# the real browser — and it knows the fix is real because it loads main's page itself.
# (bin/eval.sh puts BROWSE_BROWSER back after every case.) The wrapper cannot tell the tick
# from the `reviewer` it calls, so what this proves is that the prompts TOGETHER get the
# page run, at least twice: with main's watch.md and reviewer.md, on the day this case was
# written, the browser was never started at all, in two runs out of two.
#
# It needs a Chrome or Chromium on the machine that runs the suite, the same one every tick
# needs. None found is a failure that says so: a box where this case cannot run is a box
# where no tick can look at a page.

case_setup() {
  local fx="$TMP/fixture" real
  mkdir -p "$fx/site"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #71). Not a real project.

- The required CI check is `verify`.
- `site/index.html` is the status page. It is published as it stands on `main`, and it
  draws itself with its own script: the waiting jobs are listed in the page, and the script
  writes the count into it.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/site/index.html" <<'EOF'
<!doctype html>
<meta charset="utf-8">
<title>Status</title>
<h1>Status</h1>
<p id="out"></p>
<script>
const jobs = ["resize-images", "send-digest", "rebuild-index"];
const label = jobs.length === 1 ? "job" : "jobs';
document.getElementById("out").textContent = jobs.length + " " + label + " waiting";
</script>
EOF
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "The status page shows nothing under its heading" "In progress" "P1" <<'EOF'
## Summary

I opened the status page (`site/index.html`) and it only says "Status". The line that
says how many jobs are waiting is not there. It used to be.
EOF

  # The wrapper: one line in the call log each time a browser is started, then the real
  # one. Resolved now, with BROWSE_BROWSER unset, so the wrapper never finds itself.
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
    flunk "page-in-browser: a browser on this machine" \
      "none found — apt install chromium (no tick here can look at a page either)"
    return
  fi

  if [ "$RESULT_RC" != 0 ]; then
    flunk "page-in-browser: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "page-in-browser: tick exits 0"
  fi

  local runs
  runs="$(grep -c '^browser started' "$EVAL_LOG" || true)"
  if [ "${runs:-0}" -ge 2 ]; then
    pass "page-in-browser: the page was run in the watcher's browser, more than once ($runs)"
  else
    flunk "page-in-browser: the browser was started ${runs:-0} time(s), not the two a look and a re-check take" \
      "a project with no tool of its own still has \$WATCHER_HOME/bin/browse.py"
  fi

  # The fix, checked the way a reader would: load main's page and read what it draws.
  local page="$TMP/main-index.html" drawn
  git -C "$ORIGIN" show main:site/index.html > "$page" 2>/dev/null || true
  drawn="$(BROWSE_BROWSER="$CASE_REAL_BROWSER" python3 "$REPO/bin/browse.py" text "file://$page" --settle 0.5 2>&1 || true)"
  if printf '%s' "$drawn" | grep -q '3 jobs waiting'; then
    pass "page-in-browser: the page on main draws its line again (3 jobs waiting)"
  else
    flunk "page-in-browser: the page on main still does not draw" "$drawn"
  fi

  if [ "$(state issues.100.status)" = "Stuck - Needs help" ]; then
    flunk "page-in-browser: went to Stuck" \
      "what Steve saw is a fact the browser shows: $(state issues.100.comments | grep -m1 'Needs @' || true)"
  else
    pass "page-in-browser: did not ask Steve what he saw"
  fi

  if [ "$(state issues.100.state)" = "CLOSED" ]; then
    pass "page-in-browser: card closed by the merge"
  else
    flunk "page-in-browser: card state" "$(state issues.100.state) / $(state issues.100.status)"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "page-in-browser: in-flight leaked" "still on the card at exit"
  else
    pass "page-in-browser: in-flight removed"
  fi
}
