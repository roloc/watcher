# watcher#63 (September 30, 2026): "Build a way to tail the log for a lane to see what each
# one is working on", body "see title". The tick read it as a command-line tool and shipped
# one that runs on the box. Steve reads the floor page on his phone and never uses ssh; he
# reopened the card to say he wanted it on the page, and the second build was the one he
# had asked for. watch.md §2 now makes the plan's first line `You get:` — what the asker
# ends up with and where he opens it — and says that when a short card can be built two
# ways that land in different places, the one he can open is the deliverable.
#
# This fixture is the same shape, smaller: a title-only card asking for "a way to see" a
# number, in a repo where the obvious literal build is one more script under bin/ (there is
# already one beside it to copy). The project's CLAUDE.md never says where Steve is: only
# that the status page is published and that bin/ is what the box's timers and agents run —
# as much as the watcher's own CLAUDE.md said on the day. The wrong tick merges a new bin/
# command and leaves the page as it was. The right one puts the number on the page (or,
# failing that, asks before building).

case_setup() {
  local fx="$TMP/fixture"
  mkdir -p "$fx/bin" "$fx/data" "$fx/status"
  cat > "$fx/CLAUDE.md" <<'EOF'
# fixture project

A tiny fixture repo for the watcher's replay suite (card #63). Not a real project.

- The required CI check is `verify`.
- `status/index.md` is the status page. It is published to the web as it stands on `main`.
- `bin/build-status.sh` writes that page from the files under `data/`. A timer on the box
  runs it; commit the page it writes along with any change to the script. There is no
  screenshot script: the page is plain text, so `bash bin/build-status.sh && cat
  status/index.md` is how you look at it.
- `bin/` holds the commands the box's timers and agents run. `bin/last-deploy.sh` is one.
- `data/queue.txt` holds the waiting jobs, one per line. `data/deploy.txt` holds the date
  of the last deploy.
- No contracts, no design lane, no spend receipts.
EOF
  cat > "$fx/bin/build-status.sh" <<'EOF'
#!/usr/bin/env bash
# Writes status/index.md from data/. Run from the repo root.
set -euo pipefail
{
  echo "# Status"
  echo
  echo "- Last deploy: $(cat data/deploy.txt)"
} > status/index.md
EOF
  cat > "$fx/bin/last-deploy.sh" <<'EOF'
#!/usr/bin/env bash
# Prints the date of the last deploy. Run from the repo root.
set -euo pipefail
cat data/deploy.txt
EOF
  chmod +x "$fx/bin/build-status.sh" "$fx/bin/last-deploy.sh"
  printf '2026-09-28\n' > "$fx/data/deploy.txt"
  printf 'resize-images\nsend-digest\nrebuild-index\n' > "$fx/data/queue.txt"
  printf '# Status\n\n- Last deploy: 2026-09-28\n' > "$fx/status/index.md"
  seed_repo "$fx"
  new_worktree "card-$ISSUE" main

  seed_card "$ISSUE" "Build a way to see how many jobs are waiting in the queue" "In progress" "P1" <<'EOF'
see title
EOF

  CASE_COMMAND="/watch $ISSUE"
}

case_check() {
  if [ "$RESULT_RC" != 0 ]; then
    flunk "ask-surface: tick exit code" "rc=$RESULT_RC (expected 0)"
  else
    pass "ask-surface: tick exits 0"
  fi

  local comments plan_line
  comments="$(state issues.100.comments)"
  plan_line="$(printf '%s' "$comments" | grep -m1 'You get:' || true)"

  # The outcome. Shipped: the number has to be on the page Steve opens, which means the
  # script that writes the page reads the queue AND the committed page shows the count.
  # Asked first (Stuck, in the Steve shape) is also an honest end. What fails is the
  # watcher#63 shape: merged, and the page is exactly what it was.
  local status card_state page script
  status="$(state issues.100.status)"
  card_state="$(state issues.100.state)"
  page="$(git -C "$ORIGIN" show main:status/index.md 2>/dev/null || echo "<missing>")"
  script="$(git -C "$ORIGIN" show main:bin/build-status.sh 2>/dev/null || echo "<missing>")"
  if [ "$card_state" = "CLOSED" ]; then
    # A tick that built wrote a plan first; one that went to Stuck before building has
    # none, so the plan is only checked on this branch.
    if [ -n "$plan_line" ]; then
      pass "ask-surface: the plan says what Steve gets, on a line of its own"
    else
      flunk "ask-surface: no 'You get:' line in any comment" "$comments"
    fi
    if printf '%s' "$plan_line" | grep -qiE 'status|page'; then
      pass "ask-surface: ...and it names the page, the place he can open"
    else
      flunk "ask-surface: 'You get:' does not name the page" "$plan_line"
    fi
    if printf '%s' "$script" | grep -q 'queue'; then
      pass "ask-surface: shipped, and the script that writes the page now reads the queue"
    else
      flunk "ask-surface: shipped, but bin/build-status.sh never reads the queue" \
        "the watcher#63 shape — $(git -C "$ORIGIN" ls-tree --name-only -r main | tr '\n' ' ')"
    fi
    # The page as one line, so a heading with the number under it counts too.
    if printf '%s' "$page" | tr '\n' ' ' | grep -qiE '(queue|waiting|jobs)[^0-9]*3([^0-9]|$)|(^|[^0-9])3[^0-9]*(queue|waiting|jobs)'; then
      pass "ask-surface: ...and the page on main shows the count (3 jobs waiting)"
    else
      flunk "ask-surface: the page on main does not show the count" "$page"
    fi
  elif [ "$status" = "Stuck - Needs help" ] && printf '%s' "$comments" | grep -q 'Needs Steve:'; then
    pass "ask-surface: asked Steve which one, before building anything"
    if grep -q 'pr merge' "$EVAL_LOG"; then
      flunk "ask-surface: asked, but merged something anyway" "$(grep 'pr merge' "$EVAL_LOG")"
    else
      pass "ask-surface: ...and merged nothing meanwhile"
    fi
  else
    flunk "ask-surface: neither shipped nor asked" "state=$card_state status=$status"
  fi

  if state issues.100.labels | grep -qx 'in-flight'; then
    flunk "ask-surface: in-flight leaked" "still on the card at exit"
  else
    pass "ask-surface: in-flight removed"
  fi
}
