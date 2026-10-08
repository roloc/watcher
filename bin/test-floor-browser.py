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


def settled(b):
    """Wait out a walk (the characters' 1s position transition), so a check or a picture
    sees where everyone ended up, not where they were a tenth of a second in."""
    return b.eval("new Promise(r => setTimeout(() => r(true), 1200))")


# What no two of may overlap: every character's own box (figure and plate), as drawn.
NO_OVERLAP = """(() => {
  const boxes = [...document.querySelectorAll('.character')].map(e => [e.dataset.lane || ('ship:' + e.dataset.ship), e.getBoundingClientRect()]);
  const hits = [];
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const [na, a] = boxes[i], [nb, b] = boxes[j];
    if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) hits.push(na + ' on ' + nb);
  }
  return hits;
})()"""

# A floor shaped like the real box on October 8, 2026 (three boards, seven lanes, six of
# them idle), fed straight to the page's own drawing code — the floor whose picture Steve
# sent back: characters standing on each other, their plates unreadable, no way to tell
# which project any of them worked for, and the wall's last card cut in half.
LIVE_SHAPED = """(() => {
  const now = new Date().toISOString();
  const L = (lane, kind, pill, extra) => ({lane, kind, unit: 'watcher@' + lane, pill, link: '#', ...extra});
  const idle = {reason: 'Nothing to pick up right now.'};
  const proj = (name, lanes) => ({name, board_url: '#', counts: {ready: 0, in_progress: 1, backlog: 2}, lanes,
                                  ship: {ci: {state: 'passing', sha: 'abc1234', url: '#'}}});
  const tick = (project, lane, n, minutesAgo) => ({project, lane, card: {number: n, url: 'https://example.test/' + n},
                                                   when: new Date(Date.now() - minutesAgo * 60000).toISOString(), quota: 'unknown'});
  return {header: {box: 'agents', time: now, needs_steve_total: 0},
          projects: [proj('riffmap', [L('riffmap-triage', 'Triage', 'idle', idle), L('riffmap-design', 'Design', 'idle', idle),
                                      L('riffmap', 'Build', 'idle', idle)]),
                     proj('riffmap-site', [L('riffmap-site-triage', 'Triage', 'idle', idle),
                                           L('riffmap-site-design', 'Design', 'working', {card: {number: 12, url: '#'}, stage: 'Building', minutes: 9}),
                                           L('riffmap-site', 'Build', 'idle', idle)]),
                     proj('watcher', [L('watcher', 'Build', 'idle', idle)])],
          recent: [tick('riffmap', 'riffmap', 626, 3), tick('riffmap-site', 'riffmap-site-design', 12, 11),
                   tick('riffmap', 'riffmap-design', 647, 12), tick('riffmap-site', 'riffmap-site-design', 12, 27),
                   tick('watcher', 'watcher', 91, 43), tick('riffmap', 'riffmap', 625, 50),
                   tick('riffmap', 'riffmap', 624, 70), tick('riffmap', 'riffmap', 623, 90)]};
})()"""


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

    STUDIO_OPEN = "document.getElementById('studio').classList.contains('open')"
    CHAR_COUNT = "document.querySelectorAll('.character').length"

    print("11. the studio: one room the lanes walk around in (card #88)")
    with browse.Browser(width=1280, height=900) as b:
        b.goto(URL)
        b.wait_for(DESKS_DRAWN)
        check("the floor page itself still looks as it does today: the Studio button, nothing else new",
              b.eval("getComputedStyle(document.getElementById('studio-btn')).display") != "none")
        b.click("#studio-btn")
        check("the studio opens and the floor's own sections hide",
              b.wait_for(STUDIO_OPEN)
              and b.eval("getComputedStyle(document.getElementById('floor-view')).display") == "none")
        check("the Studio button hides once the room is open", b.wait_for(
              "getComputedStyle(document.getElementById('studio-btn')).display === 'none'"))
        check("no uncaught exception, console.error or failed request", b.errors == [], b.errors)
        check("one character per lane, plus one per ship station: six, same as the desks",
              b.wait_for(CHAR_COUNT + " === 6"))
        settled(b)
        shot(b, "studio-desktop.png")

        print("12. a character stands in its own project's row, whatever it is doing")
        settled(b)
        info = lambda lane: b.eval(
            f'(() => {{ const e = document.querySelector(`.character[data-lane="{lane}"]`); '
            'const r = e.getBoundingClientRect(); const row = [...document.querySelectorAll(".zone")]'
            '.findIndex(z => { const q = z.getBoundingClientRect(); return r.bottom <= q.bottom + 1 && r.bottom > q.top; });'
            'return [e.dataset.pill, row, e.querySelector(".plate").textContent.trim()]; })()')
        rows = b.eval("[...document.querySelectorAll('.zone-name')].map(a => a.textContent)")
        check("one row per board, named after it, in the board's order", rows == ["alpha", "beta"], rows)
        check("alpha (working) stands at its desk in alpha's row, its plate saying what it is",
              info("alpha") == ["working", 0, "Build"], info("alpha"))
        check("alpha-triage (idle) stays at its counter in alpha's row — not off in a lounge "
              "where nothing said whose it was",
              info("alpha-triage") == ["idle", 0, "Triage"], info("alpha-triage"))
        check("an idle lane holds a mug (the pose says idle, not just the dark screen)",
              b.eval("document.querySelector('.character[data-lane=\"alpha-design\"] svg').innerHTML.includes('f3f4f6')"))
        check("beta (paused) stands by its own desk in beta's row, not at the podium",
              info("beta") == ["paused", 1, "Build"], info("beta"))
        check("the row's name is a link to its board, opening in a new tab",
              b.eval("[...document.querySelectorAll('.zone-name')].every(a => a.href && a.target === '_blank')"))
        check("nobody stands on anybody", b.eval(NO_OVERLAP) == [], b.eval(NO_OVERLAP))

        print("12b. the floor Steve sent back on October 8, 2026: seven lanes, six idle, three boards")
        b.eval("render(" + LIVE_SHAPED + "); renderStudio(" + LIVE_SHAPED + "); true")
        settled(b)
        check("ten characters: seven lanes and three ship docks", b.eval(CHAR_COUNT) == 10)
        check("no two characters overlap (six idle lanes used to share five fixed spots, so the "
              "sixth stood on the first)", b.eval(NO_OVERLAP) == [], b.eval(NO_OVERLAP))
        rows = b.eval("[...document.querySelectorAll('.zone-name')].map(a => a.textContent)")
        check("each board's name is on its row, so who works for whom is readable at a glance",
              rows == ["riffmap", "riffmap-site", "watcher"], rows)
        check("a lane's plate is readable: no plate is covered by another character",
              b.eval("""(() => {
                const plates = [...document.querySelectorAll('.character .plate')].map(p => p.getBoundingClientRect());
                const chars = [...document.querySelectorAll('.character')].map(c => c.getBoundingClientRect());
                return plates.every(p => chars.filter(c => c.left < p.right && p.left < c.right
                                                            && c.top < p.bottom && p.top < c.bottom).length === 1);
              })()"""))
        cut = b.eval("""(() => {
          const wall = document.getElementById('pin-wall').getBoundingClientRect();
          const room = document.getElementById('room').getBoundingClientRect();
          return [...document.querySelectorAll('.pin-card')].map(c => c.getBoundingClientRect())
            .filter(r => r.top < wall.top - 1 || r.bottom > wall.bottom + 1 || r.bottom > room.bottom + 1).length;
        })()""")
        check("every pinned card on the wall is whole, none cut off at the wall's edge", cut == 0, cut)
        check("the first pinned card is in view without scrolling the wall",
              b.eval("""(() => { const c = document.querySelector('.pin-card').getBoundingClientRect();
                      const w = document.getElementById('pin-wall').getBoundingClientRect();
                      return c.left >= w.left - 1 && c.right <= w.right + 1; })()"""))
        shot(b, "studio-live-shaped.png")
        b.eval("refresh().then(() => true)")
        settled(b)
        check("every lane keeps the same look across a redraw (the hash never changes)",
              b.eval("document.querySelector(\".character[data-lane='alpha']\").querySelector('svg').outerHTML")
              == b.eval("refresh().then(() => document.querySelector(\".character[data-lane='alpha']\")"
                        ".querySelector('svg').outerHTML)"))
        check("a working lane's monitor scrolls (the scanline is actually drawn)",
              b.eval("getComputedStyle(document.querySelector("
                    "'.desk-monitor[data-lane=\"alpha\"] .scanline')).opacity") != "0")
        check("an idle lane's monitor is dark",
              b.eval("getComputedStyle(document.querySelector("
                    "'.desk-monitor[data-lane=\"alpha-design\"] .scanline')).opacity") == "0")

        print("13. the beacon over the door reads the same state the pills do")
        check("a paused lane and a failing build: amber, not green",
              b.eval("document.getElementById('beacon').className") == "beacon-amber")
        check("the beacon is actually visible — inside #room's own box, which clips "
              "(it used to sit mostly above it, clipped to a sliver)",
              b.eval("""(() => {
                const beacon = document.getElementById("beacon").getBoundingClientRect();
                const room = document.getElementById("room").getBoundingClientRect();
                return beacon.top >= room.top && beacon.bottom <= room.bottom
                    && beacon.height > 10;
              })()"""))

        print("14. tap a character for a speech bubble; tap the bubble for the full Peek")
        b.click(".character[data-lane='alpha']")
        check("the bubble opens with the same last line Peek shows (section 5 appended it live)",
              b.wait_for("document.querySelector('.bubble')?.textContent.includes("
                        "'Opening the pull request now.')"))
        b.click(".bubble")
        check("tapping it opens the full Peek panel",
              b.wait_for(PANEL_OPEN)
              and "Build (alpha)" in b.eval("document.getElementById('peek-title').textContent"))
        check("the bubble itself is gone once the full panel is up",
              b.eval("document.querySelector('.bubble')") is None)
        shot(b, "studio-bubble-to-peek.png", full_page=False)
        b.key("Escape")
        check("still no errors after the bubble round trip", b.errors == [], b.errors)

        print("15. the wall by the door: recent ticks, newest on top, each one a link")
        # Both fixture ticks are pinned; which sorts first depends on the real mtime of the
        # two log files this suite just wrote (`metrics.py` keys "when" on that, not the
        # stamp in the filename) — true to #5 and #3, never guess which ran "newer" here.
        hrefs = b.eval("[...document.querySelectorAll('.pin-card')].map(a => a.getAttribute('href'))")
        check("one pinned card per recent tick, each linking straight to its issue",
              sorted(hrefs) == ["https://github.com/roloc/alpha/issues/5",
                                "https://github.com/roloc/beta/issues/3"], hrefs)

        print("16. Floor goes back, and the floor page is exactly as it was")
        b.click("#floor-btn")
        check("the studio closes and the floor's sections return",
              b.wait_for("!" + STUDIO_OPEN)
              and b.eval("getComputedStyle(document.getElementById('floor-view')).display") != "none")
        check("the Studio button is back", b.eval(
              "getComputedStyle(document.getElementById('studio-btn')).display") != "none")
        check("the floor's own desks are still there, same as section 2", b.eval(CHAR_COUNT) >= 0
              and b.eval("document.querySelectorAll('.desk').length") == 6)

    print("17. on a phone: the room fits without scrolling sideways, and walking respects reduced motion")
    with browse.Browser(width=390, height=844) as b:
        b.goto(URL)
        b.wait_for(DESKS_DRAWN)
        b.click("#studio-btn")
        b.wait_for(STUDIO_OPEN)
        check("no wider than the screen",
              b.eval("document.documentElement.scrollWidth <= window.innerWidth"),
              b.eval("[document.documentElement.scrollWidth, window.innerWidth]"))
        settled(b)
        shot(b, "studio-phone.png")
        b.eval("render(" + LIVE_SHAPED + "); renderStudio(" + LIVE_SHAPED + "); true")
        settled(b)
        check("the floor Steve sent back, on a phone: nobody stands on anybody",
              b.eval(NO_OVERLAP) == [], b.eval(NO_OVERLAP))
        check("...and the room still fits the screen",
              b.eval("document.documentElement.scrollWidth <= window.innerWidth"))
        check("eight cards on a phone-wide wall: it scrolls, and its edge fades to say so",
              b.eval("document.getElementById('pin-wall').classList.contains('scrolls')"))
        shot(b, "studio-live-shaped-phone.png")
        # The same wall drawn while the studio was closed (every 30s poll draws it hidden)
        # and then opened: the fade has to be there on the first open, not the next poll.
        b.click("#floor-btn")
        b.wait_for("!" + STUDIO_OPEN)
        b.eval("render(" + LIVE_SHAPED + "); renderStudio(" + LIVE_SHAPED + "); true")
        b.click("#studio-btn")
        b.wait_for(STUDIO_OPEN)
        check("a wall drawn while the studio was hidden still fades the moment it opens",
              b.eval("document.getElementById('pin-wall').classList.contains('scrolls')"))
        check("a character walks: the CSS transition is on by default",
              b.eval("getComputedStyle(document.querySelector('.character')).transitionDuration") != "0s")

    with browse.Browser(width=390, height=844) as b:
        b.reduce_motion(True)
        b.goto(URL)
        b.wait_for(DESKS_DRAWN)
        b.click("#studio-btn")
        b.wait_for(STUDIO_OPEN)
        check("prefers-reduced-motion: reduce drops the walk to an instant jump",
              b.eval("getComputedStyle(document.querySelector('.character')).transitionDuration") == "0s")

    print("18. demo mode (?demo=1): cycles the fixture through every state, for designing the room")
    with browse.Browser(width=390, height=844) as b:
        b.goto(URL + "?demo=1")
        b.wait_for(CHAR_COUNT + " > 0")
        check("no uncaught exception even with no real board behind it", b.errors == [], b.errors)
        b.click("#studio-btn")
        b.wait_for(STUDIO_OPEN)
        check("the demo's first state is drawn without ever calling the real endpoint",
              b.eval(CHAR_COUNT) > 0)
        colors = []
        for i in range(3):
            b.eval(f"window.__demoGoto({i})")  # synchronous: the redraw is done when this returns
            colors.append(b.eval("document.getElementById('beacon').className"))
        check("stepping through the fixture's states changes the beacon (never stuck on one color)",
              len(set(colors)) > 1, colors)
        b.eval("window.__demoGoto(1)")  # the demo state with a needs_steve lane
        check("a needs_steve lane waves, not just stands with its hands up",
              b.eval("!!document.querySelector("
                    "'.character[data-pill=\"needs_steve\"] .character-wave')"))
        check("...and holds up the mockup its card is waiting on an answer for (card #91), "
              "right there in the room — no click through to the card needed",
              b.eval("!!document.querySelector("
                    "'.character[data-pill=\"needs_steve\"] .character-mockup img')"))
        settled(b)
        check("it walked to the podium by the door, above every row, and its plate says whose it is",
              b.eval("""(() => { const e = document.querySelector('.character[data-pill="needs_steve"]');
                      const r = e.getBoundingClientRect(); const z = document.querySelector('.zone').getBoundingClientRect();
                      return r.bottom <= z.top + 1 && e.querySelector('.plate').textContent.includes('alpha'); })()"""))
        check("the sleeping lane is on the couch, below every row, named for its board too",
              b.eval("""(() => { const e = document.querySelector('.character[data-pill="sleeping"]');
                      const r = e.getBoundingClientRect(); const zs = [...document.querySelectorAll('.zone')];
                      const last = zs[zs.length - 1].getBoundingClientRect();
                      return r.top >= last.bottom - 1 && e.querySelector('.plate').textContent.includes('beta'); })()"""))
        shot(b, "studio-demo.png")
        b.click("#floor-btn")
        b.wait_for("!" + STUDIO_OPEN)
        check("the floor's own desk shows the same mockup inline too, not just a link to the card",
              b.eval("!!document.querySelector('.desk .mockup-thumb')"))
        shot(b, "floor-demo-mockup.png")
        check("still no errors after cycling demo states", b.errors == [], b.errors)
except browse.BrowseError as e:
    print(e, file=sys.stderr)
    fails = 1
finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(ROOT, ignore_errors=True)

print(f"{count} cases, {'all passed' if not fails else 'FAILURES'}")
sys.exit(fails)
