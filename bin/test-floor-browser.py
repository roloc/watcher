#!/usr/bin/env python3
"""The floor page, in a real browser. `bin/test-floor.py` checks the Python that builds the
page's data; this loads the page itself in headless Chrome (`bin/browse.py`) and uses it
the way Steve does: looks at the desks, clicks Peek, watches a line arrive, closes it.

    python3 bin/test-floor-browser.py                # exit 0 = all cases hold
    python3 bin/test-floor-browser.py --shots DIR    # ...and write PNGs of the page to DIR

Why this exists (card #63, and #71 which it caused): the page is a program that runs in a
browser, and until October 1, 2026 nothing ever ran it. Every check was a string looked for
in the Python source. #70 shipped a `<script>` block that did not parse — the page loaded
its frame and nothing else — with 115 of those checks green and a review passed; the fix
(#72) shipped a Peek button that only exists while a lane is working, so on a quiet floor
there was still nothing to see. Case 9 below puts #70's bug back into today's page and
fails on it.

`--shots` is the picture `commands/watch.md` asks for on any change to a screen ("a change
to a screen ships with its picture"): run it, `Read` the PNGs, say what they show.

No network: a fixture floor on 127.0.0.1, a stubbed board, real state directories and real
session transcripts on disk. It needs a Chrome or Chromium binary; `bin/browse.py` says
where it looks. No browser is exit 2 with how to get one — a failure, never a skip.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import browse  # noqa: E402
import floor  # noqa: E402
import tail  # noqa: E402

fails = 0
count = 0


def check(name, cond, detail=""):
    global fails, count
    count += 1
    print(f"  {name:<86} {'ok' if cond else 'FAIL ' + str(detail)}")
    if not cond:
        fails = 1


shots_dir = None
if len(sys.argv) > 1:
    if sys.argv[1] != "--shots" or len(sys.argv) != 3:
        sys.exit("usage: test-floor-browser.py [--shots DIR]")
    shots_dir = sys.argv[2]
    os.makedirs(shots_dir, exist_ok=True)

print("1. browse.py finds a browser: the named one, the PATH, an app, then the newest cached")
no = dict(which=lambda n: None, exists=lambda p: False, globber=lambda p: [])
check("nothing anywhere is None, so the caller can say so",
      browse.find_browser(env={}, **no) is None)
check("$BROWSE_BROWSER wins when it exists",
      browse.find_browser(env={"BROWSE_BROWSER": "/x/chrome"},
                          **dict(no, exists=lambda p: p == "/x/chrome")) == "/x/chrome")
check("$BROWSE_BROWSER naming a file that is not there is None, never a quiet fallback",
      browse.find_browser(env={"BROWSE_BROWSER": "/x/gone"},
                          **dict(no, which=lambda n: "/usr/bin/chromium")) is None)
check("then the PATH",
      browse.find_browser(env={}, **dict(no, which=lambda n: "/usr/bin/chromium"
                                         if n == "chromium" else None)) == "/usr/bin/chromium")
check("the newest Playwright revision reads as newest",
      browse._revision("/c/ms-playwright/chromium-1243/chrome-linux64/chrome") == 1243
      and browse._revision("/c/ms-playwright/chromium_headless_shell-1193/x/headless_shell") == 1193)

if not browse.find_browser():
    print(browse.NO_BROWSER, file=sys.stderr)
    sys.exit(2)

# `browse.py find`: how the installer and doctor.sh ask, from a shell, whether a tick on
# this box can look at a page at all.
import subprocess
_found = subprocess.run([sys.executable, os.path.join(HERE, "browse.py"), "find"],
                        capture_output=True, text=True)
check("`browse.py find` prints the browser it would use and exits 0",
      _found.returncode == 0 and _found.stdout.strip() == browse.find_browser(), _found.stdout)
_none = subprocess.run([sys.executable, os.path.join(HERE, "browse.py"), "find"], capture_output=True,
                       text=True, env=dict(os.environ, BROWSE_BROWSER="/no/such/browser"))
check("...and with none it exits 1 and says how to get one, on stderr",
      _none.returncode == 1 and _none.stdout == "" and "apt install chromium" in _none.stderr, _none.stderr)

# --- a fixture floor: two boards, four lanes, real files -------------------------------

ROOT = tempfile.mkdtemp(prefix="watcher-floor-browser-")
PROJECTS = os.path.join(ROOT, "projects")
STATE = os.path.join(ROOT, "state")
CLAUDE = os.path.join(ROOT, "claude-projects")
for d in (PROJECTS, STATE, CLAUDE):
    os.makedirs(d)


def write_env(name, body):
    with open(os.path.join(PROJECTS, f"{name}.env"), "w") as f:
        f.write(body)


write_env("alpha", f"""\
PROJECT_NAME=alpha
PROJECT_ROOT={ROOT}/alpha
BOARD_OWNER=roloc
PROJECT_REPO=roloc/alpha
BOARD_NUMBER=1
DESIGN_AGENTS=architect,designer
LANE_AGENTS="!${{DESIGN_AGENTS}},-"
""")
write_env("alpha-design", """\
. "$(dirname "${BASH_SOURCE[0]}")/alpha.env"
PROJECT_NAME=alpha-design
LANE_AGENTS="${DESIGN_AGENTS}"
""")
write_env("alpha-triage", """\
. "$(dirname "${BASH_SOURCE[0]}")/alpha.env"
PROJECT_NAME=alpha-triage
LANE_AGENTS="-"
LANE_COMMAND=/triage
LANE_WORKDIR=root
""")
write_env("beta", f"""\
PROJECT_NAME=beta
PROJECT_ROOT={ROOT}/beta
BOARD_OWNER=roloc
PROJECT_REPO=roloc/beta
BOARD_NUMBER=2
""")
LANES = ("alpha", "alpha-design", "alpha-triage", "beta")
for lane in LANES:
    os.makedirs(os.path.join(STATE, lane, "logs"))


def say(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def run(description):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"description": description, "command": "x"}}]}}) + "\n"


def transcript(worktree_base, card, lines):
    d = os.path.join(CLAUDE, tail.encode_dir(os.path.join(worktree_base, f"card-{card}")))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "session-1.jsonl")
    with open(path, "w") as f:
        f.writelines(lines)
    return path


def tick_log(lane, card, stamp):
    with open(os.path.join(STATE, lane, "logs", f"card-{card}-{stamp}.json"), "w") as f:
        json.dump({"subtype": "success", "total_cost_usd": 1.25, "num_turns": 12, "duration_ms": 1000,
                   "result": f"- Card: #{card} x\n- Outcome: merged\n- Ended in: Done\n"}, f)


# alpha (Build): working on #7 right now, with a session being written.
with open(os.path.join(STATE, "alpha", "current"), "w") as f:
    f.write("7\n")
LIVE = transcript(f"{ROOT}/alpha-worktrees", 7,
                  [say("Reading the card before touching anything."), run("Run the spawner suite")])
# alpha-design: idle. Its last tick was #5, and that session is still on disk.
tick_log("alpha-design", 5, "20260928T110000Z")
transcript(f"{ROOT}/alpha-worktrees", 5, [say("Mockup merged; card five is done.")])
# alpha-triage: idle, and has never worked a card. Nothing to show.
# beta: paused, with a last session from #3.
open(os.path.join(STATE, "beta", "PAUSE"), "w").close()
tick_log("beta", 3, "20260927T090000Z")
transcript(f"{ROOT}/beta-worktrees", 3, [say("Parked: the build needs a decision.")])

BOARDS = {
    ("roloc", 1): [{"number": 7, "state": "OPEN", "status": "In progress", "stage": "Building",
                    "labels": [], "url": "https://github.com/roloc/alpha/issues/7"}],
    ("roloc", 2): [],
}


class LiveCache:
    """Builds on every request, so a case can change the files and see the page follow."""
    broken = False

    def get(self):
        if self.broken:
            raise RuntimeError("the fixture's data endpoint is switched off")
        return floor.build_state(PROJECTS, STATE, lambda o, n: BOARDS.get((o, n), []),
                                 unit_active_fn=lambda u: True, ask_fn=lambda i, r: "",
                                 box_name="test-box")


def tail_lookup(lane, card, file, pos):
    info = {l["lane"]: l for l in floor.discover_lanes(PROJECTS, STATE)}.get(lane)
    return None if info is None else floor.tail_status(info, CLAUDE, card, file, pos)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # case 10 breaks the data endpoint on purpose; its traceback is not news


# The ship stations: canned CI runs through the real `ship_lines`. alpha's CI is green and
# alpha is the repo "this box deploys", so it also gets the deployer's lines; beta's is red.
def ci_step(name, conclusion="success", sec=0):
    return {"name": name, "status": "completed", "conclusion": conclusion,
            "started": "2026-10-01T17:15:00Z", "completed": "2026-10-01T17:15:%02dZ" % sec}


def ci_run(sha, title, steps, conclusion="success"):
    return {"id": 1, "name": "CI", "sha": sha * 40, "title": title, "status": "completed",
            "conclusion": conclusion, "started": "2026-10-01T17:15:00Z", "updated": "x",
            "jobs": [{"name": "verify", "status": "completed", "conclusion": conclusion,
                      "started": "2026-10-01T17:15:00Z", "completed": "2026-10-01T17:16:01Z", "steps": steps}]}


CI_RUNS = {
    "roloc/alpha": [ci_run("a", "Peek on every desk", [ci_step("Set up job"), ci_step("The spawner suite", sec=28),
                                                         ci_step("The floor page, in a real browser", sec=7)]),
                    {"id": 2, "name": "CI", "sha": "b" * 40, "title": "The commit before", "status": "completed",
                     "conclusion": "success", "started": "2026-10-01T15:00:00Z", "updated": "x"}],
    "roloc/beta": [ci_run("c", "Broke the build", [ci_step("Run npm test", "failure", sec=19)], "failure")],
}
ship_lookup = floor.make_ship_lookup(
    lambda: {"alpha": "roloc/alpha", "beta": "roloc/beta"}, lambda repo: CI_RUNS[repo],
    deploy_repo="roloc/alpha", deploy_fn=lambda: {"state": "live", "live": "aaaaaaa", "target": "aaaaaaa"},
    history_fn=lambda: [(floor.datetime(2026, 10, 1, 17, 59, tzinfo=floor.timezone.utc),
                         "deployed aaaaaaa; every lane un-paused")],
    cache_sec=0)

cache = LiveCache()
server = QuietServer(("127.0.0.1", 0), floor.make_handler(cache, tail_lookup, ship_lookup))
URL = f"http://127.0.0.1:{server.server_address[1]}/"
threading.Thread(target=server.serve_forever, daemon=True).start()

DESKS_DRAWN = "document.querySelectorAll('.desk').length > 0"
PANEL_OPEN = "document.getElementById('peek-overlay').classList.contains('open')"
PANEL_TEXT = "document.getElementById('peek-lines').textContent"
PANEL_SUB = "document.getElementById('peek-sub').textContent"
PAGE_URL = "location.href"


def peek_button(lane):
    return f'.peek-btn[data-peek-lane="{lane}"]'


def shot(b, name, full_page=True):
    if shots_dir:
        print("  wrote " + b.screenshot(os.path.join(shots_dir, name), full_page=full_page))


try:
    with browse.Browser(width=1280, height=900) as b:
        print("2. the page loads, its own script runs, and it draws every desk")
        b.goto(URL)
        check("the desks get drawn (the page's script ran and its data arrived)",
              b.wait_for(DESKS_DRAWN))
        check("no uncaught exception, console.error or failed request", b.errors == [], b.errors)
        check("six desks: four lanes and a ship station per board",
              b.eval("document.querySelectorAll('.desk').length") == 6)
        text = b.text()
        check("the working lane says what it is on: card, stage, minutes",
              "#7 · Building · " in text, text[:400])
        check("each pill is there in words", all(w in text for w in ("Working", "Idle", "Paused")))
        check("neither warning line is showing",
              b.eval("getComputedStyle(document.getElementById('load-error')).display") == "none"
              and b.eval("getComputedStyle(document.getElementById('script-error')).display") == "none")
        check("every link on the drawn page opens in a new tab (card #62)",
              b.eval("[...document.querySelectorAll('a')].every(a => a.target === '_blank')"))
        shot(b, "floor-desktop.png")

        print("3. Peek is on every lane's desk, whatever the lane is doing (card #63)")
        for lane in LANES:
            check(f"{lane}: a Peek button, drawn and big enough to hit",
                  b.eval(f"""(() => {{ const r = document.querySelector('{peek_button(lane)}')
                          ?.getBoundingClientRect(); return !!r && r.width >= 40 && r.height >= 24; }})()"""))
        check("each ship station has one too: what CI did last (asked for after card #63)",
              b.eval("document.querySelectorAll('.desk.ship .peek-btn[data-peek-ship]').length") == 2)
        check("no Peek button sits inside a link, where a click could also follow the link",
              b.eval("[...document.querySelectorAll('.peek-btn')].every(x => !x.closest('a'))"))

        print("4. clicking Peek on a working lane opens its live session, on this page")
        b.click(peek_button("alpha"))
        check("the panel opens", b.wait_for(PANEL_OPEN))
        check("it shows what the session has done so far, decoded one line each",
              b.wait_for(PANEL_TEXT + ".includes('$ Run the spawner suite')")
              and "Reading the card before touching anything." in b.eval(PANEL_TEXT), b.eval(PANEL_TEXT))
        check("it names the lane and says it is live, on which card",
              "Build (alpha)" in b.eval("document.getElementById('peek-title').textContent")
              and b.eval(PANEL_SUB) == "working on #7 — live", b.eval(PANEL_SUB))
        check("the click opened no new tab and left the page where it was",
              b.new_tabs == [] and b.eval(PAGE_URL) == URL, (b.new_tabs, b.eval(PAGE_URL)))
        shot(b, "peek-desktop.png", full_page=False)  # the panel is fixed to the screen

        print("5. it is live: a line the session writes next arrives by itself")
        with open(LIVE, "a") as f:
            f.write(say("Opening the pull request now."))
        check("the new line shows up without touching anything",
              b.wait_for(PANEL_TEXT + ".includes('Opening the pull request now.')", timeout=8),
              b.eval(PANEL_TEXT))
        check("...added under the earlier lines, which are still there, once each",
              b.eval(PANEL_TEXT).split("\n") == ["Reading the card before touching anything.",
                                                 "$ Run the spawner suite",
                                                 "Opening the pull request now."], b.eval(PANEL_TEXT))
        check("the page redrawing its desks under the panel leaves the panel open",
              b.eval("refresh().then(() => true)") and b.eval(PANEL_OPEN)
              and b.eval("document.querySelectorAll('.peek-btn').length") == 6)

        print("6. the panel closes: Escape, the close button, a click beside it")
        b.key("Escape")
        check("Escape closes it", b.wait_for("!" + PANEL_OPEN, timeout=3))
        b.click(peek_button("alpha"))
        b.wait_for(PANEL_OPEN)
        b.click("#peek-close")
        check("the close button closes it", b.wait_for("!" + PANEL_OPEN, timeout=3))
        b.click(peek_button("alpha"))
        b.wait_for(PANEL_OPEN)
        b.click_at(6, 6)
        check("a click outside the panel closes it (a phone has no Escape key)",
              b.wait_for("!" + PANEL_OPEN, timeout=3))

        print("7. Peek on a lane that is not working shows its last session, and says so")
        b.click(peek_button("alpha-design"))
        check("an idle lane: the session of its last card",
              b.wait_for(PANEL_TEXT + ".includes('Mockup merged; card five is done.')"), b.eval(PANEL_TEXT))
        check("...labeled as its last session, not as live",
              b.eval(PANEL_SUB) == "idle — its last session, #5", b.eval(PANEL_SUB))
        b.key("Escape")
        b.click(peek_button("beta"))
        check("a paused lane: the same",
              b.wait_for(PANEL_TEXT + ".includes('Parked: the build needs a decision.')")
              and b.eval(PANEL_SUB) == "idle — its last session, #3", b.eval(PANEL_SUB))
        b.key("Escape")
        b.click(peek_button("alpha-triage"))
        check("a lane with no session on file says there is nothing yet, in words",
              b.wait_for(PANEL_TEXT + ".includes('Nothing to show yet')"), b.eval(PANEL_TEXT))
        b.key("Escape")
        check("still no errors after all of that", b.errors == [], b.errors)

        print("7b. Peek on a ship station: what CI did on the newest commit, and what came before")
        b.click('.peek-btn[data-peek-ship="alpha"]')
        check("the panel opens with the newest commit and each step CI ran, marked",
              b.wait_for(PANEL_TEXT + ".includes('The floor page, in a real browser')")
              and "aaaaaaa  Peek on every desk" in b.eval(PANEL_TEXT)
              and "    ✓ The spawner suite · 28s" in b.eval(PANEL_TEXT), b.eval(PANEL_TEXT))
        check("it is headed Ship, and says in words how CI stands",
              b.eval("document.getElementById('peek-title').textContent") == "Ship (alpha)"
              and b.eval(PANEL_SUB).startswith("CI on main: passed"), b.eval(PANEL_SUB))
        check("the commit before it is there, one line",
              "Before that on main" in b.eval(PANEL_TEXT) and "bbbbbbb  The commit before" in b.eval(PANEL_TEXT))
        check("the board this box deploys also shows the deployer's last lines",
              "The box's deployer" in b.eval(PANEL_TEXT)
              and "deployed aaaaaaa; every lane un-paused" in b.eval(PANEL_TEXT))
        check("each step is on its own line (the line break reached the page as a line break)",
              len(b.eval(PANEL_TEXT).split("\n")) >= 8, b.eval(PANEL_TEXT))
        check("the click opened no new tab", b.new_tabs == [] and b.eval(PAGE_URL) == URL)
        shot(b, "ship-peek-desktop.png", full_page=False)
        b.key("Escape")
        b.click('.peek-btn[data-peek-ship="beta"]')
        check("a red build: the head says which job is failing, the step carries the cross",
              b.wait_for(PANEL_TEXT + ".includes('✗ Run npm test')")
              and b.eval(PANEL_SUB) == "CI on main: failing — verify", (b.eval(PANEL_SUB), b.eval(PANEL_TEXT)))
        check("a board this box does not deploy shows no deployer",
              "deployer" not in b.eval(PANEL_TEXT))
        b.key("Escape")
        b.click(peek_button("alpha"))
        check("going from a ship station back to a lane shows the lane's session, not CI",
              b.wait_for(PANEL_TEXT + ".includes('Opening the pull request now.')")
              and "Before that on main" not in b.eval(PANEL_TEXT), b.eval(PANEL_TEXT))
        b.key("Escape")
        check("still no errors", b.errors == [], b.errors)

        print("7c. the CI window a push-only history can't see: a PR's own check, live (card #77)")
        alpha_shipped = CI_RUNS["roloc/alpha"]
        CI_RUNS["roloc/alpha"] = [{
            "id": 99, "name": "CI", "sha": "d" * 40, "title": "A card in flight", "status": "in_progress",
            "conclusion": None, "started": "2026-10-01T17:58:00Z", "updated": "x",
            "pr": 80, "branch": "card-77",
            "jobs": [{"name": "verify", "status": "in_progress", "conclusion": None,
                      "started": "2026-10-01T17:58:00Z", "completed": None,
                      "steps": [{"name": "The spawner suite", "status": "in_progress", "conclusion": None,
                                 "started": "2026-10-01T17:58:00Z", "completed": None}]}]},
        ] + alpha_shipped
        b.click('.peek-btn[data-peek-ship="alpha"]')
        check("the panel leads with the PR's own run while it is still going, not the last merge",
              b.wait_for(PANEL_SUB + " === 'CI on PR #80: running now'")
              and "ddddddd  A card in flight  · card-77" in b.eval(PANEL_TEXT), b.eval(PANEL_TEXT))
        check("what already shipped still follows, under its own heading (not 'before' — it hasn't merged)",
              "Already on main" in b.eval(PANEL_TEXT) and "aaaaaaa  Peek on every desk" in b.eval(PANEL_TEXT),
              b.eval(PANEL_TEXT))
        shot(b, "ship-peek-pr-live.png", full_page=False)
        b.key("Escape")
        CI_RUNS["roloc/alpha"] = alpha_shipped
        check("still no errors", b.errors == [], b.errors)

        print("8. on a phone: nothing runs off the side, and Peek still works")
        b.resize(390, 844)
        b.goto(URL)
        b.wait_for(DESKS_DRAWN)
        check("the page is no wider than the screen",
              b.eval("document.documentElement.scrollWidth <= window.innerWidth"),
              b.eval("[document.documentElement.scrollWidth, window.innerWidth]"))
        shot(b, "floor-phone.png")
        b.click(peek_button("alpha"))
        check("Peek opens and fills at phone width",
              b.wait_for(PANEL_TEXT + ".includes('Opening the pull request now.')"))
        check("the panel fits the screen",
              b.eval("""(() => { const r = document.getElementById('peek-panel').getBoundingClientRect();
                      return r.left >= 0 && r.right <= window.innerWidth && r.bottom <= window.innerHeight; })()"""))
        shot(b, "peek-phone.png", full_page=False)  # the panel is fixed to the screen
        b.key("Escape")
        b.click('.peek-btn[data-peek-ship="alpha"]')
        check("the ship station's Peek fits the phone too",
              b.wait_for(PANEL_TEXT + ".includes('The spawner suite')")
              and b.eval("""(() => { const r = document.getElementById('peek-panel').getBoundingClientRect();
                      return r.left >= 0 && r.right <= window.innerWidth && r.bottom <= window.innerHeight; })()"""))
        shot(b, "ship-peek-phone.png", full_page=False)
        b.key("Escape")

    print("9. this suite fails on the page #70 shipped (card #71): a script that does not parse")
    good = floor.INDEX_HTML
    marker = 'pre.textContent ? "\\n" : ""'
    check("the line #70 broke is still where this case expects it", good.count(marker) == 1)
    floor.INDEX_HTML = good.replace(marker, 'pre.textContent ? "\n" : ""')
    try:
        with browse.Browser() as b:
            b.goto(URL)
            check("no desk is ever drawn", not b.wait_for(DESKS_DRAWN, timeout=3))
            check("the browser reports the syntax error (curl saw a 200 and a full page)",
                  any("SyntaxError" in e for e in b.errors), b.errors)
            check("and the page says so itself, instead of sitting blank",
                  b.eval("getComputedStyle(document.getElementById('script-error')).display") == "block")
    finally:
        floor.INDEX_HTML = good

    print("10. data that cannot be fetched says so on the page, and clears when it comes back")
    cache.broken = True
    with browse.Browser() as b:
        b.goto(URL)
        check("the page shows it cannot reach its data",
              b.wait_for("getComputedStyle(document.getElementById('load-error')).display === 'block'"))
        cache.broken = False
        check("and recovers on the next refresh",
              b.eval("refresh().then(() => true)") and b.wait_for(DESKS_DRAWN)
              and b.eval("getComputedStyle(document.getElementById('load-error')).display") == "none")
except browse.BrowseError as e:
    print(e, file=sys.stderr)
    fails = 1
finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(ROOT, ignore_errors=True)

print(f"{count} cases, {'all passed' if not fails else 'FAILURES'}")
sys.exit(fails)
