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
import time
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

# Card #106: each lane's own $WATCHER_STATE/release — alpha and alpha-triage already
# followed the pointer to the live release; alpha-design is still finishing a tick on the
# one before it.
open(os.path.join(STATE, "alpha", "release"), "w").write("aaaaaaaaaaaa")
open(os.path.join(STATE, "alpha-design", "release"), "w").write("bbbbbbbbbbbb")
open(os.path.join(STATE, "alpha-triage", "release"), "w").write("aaaaaaaaaaaa")

# A tiny real PNG, inline — no network, same one bin/floor.py's own demo fixture uses.
TINY_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABgAAAASCAIAAADOjonJAAAAG0lEQ"
            "VR4nGPIrvlPFcQwatCoQaMGjRo03AwCAEzKNE7D7YuQAAAAAElFTkSuQmCC")

BOARDS = {
    ("roloc", 1): [{"number": 7, "state": "OPEN", "status": "In progress", "stage": "Building",
                    "labels": [], "url": "https://github.com/roloc/alpha/issues/7"},
                   # #778 is Stuck but no lane's own last tick points at it (alpha's last
                   # card is #7, alpha-design's is #5) — exactly card #96's "why nothing
                   # shows today": a lane that moved on must not make this card invisible.
                   {"number": 778, "state": "OPEN", "status": "Stuck - Needs help", "stage": "Parked",
                    "labels": [], "url": "https://github.com/roloc/alpha/issues/778"}],
    ("roloc", 2): [],
}

ROUNDS = {778: [{"when": "2026-10-09T08:00:00Z", "images": [
    {"url": TINY_PNG, "label": "phone", "letter": "A", "recommended": True},
    {"url": TINY_PNG, "label": "desktop", "letter": "A", "recommended": True},
    {"url": TINY_PNG, "label": "phone", "letter": "B", "recommended": False},
    {"url": TINY_PNG, "label": "desktop", "letter": "B", "recommended": False},
]}]}
ASKS = {778: "Needs Steve: pick a direction for the earlier thing"}


class LiveCache:
    """Builds on every request, so a case can change the files and see the page follow."""
    broken = False

    def get(self):
        if self.broken:
            raise RuntimeError("the fixture's data endpoint is switched off")
        return floor.build_state(PROJECTS, STATE, lambda o, n: BOARDS.get((o, n), []),
                                 unit_active_fn=lambda u: True,
                                 ask_fn=lambda i, r: ASKS.get(i, ""),
                                 rounds_fn=lambda i, r: ROUNDS.get(i, []),
                                 box_name="test-box",
                                 ratelimit_fn=lambda: {"remaining": 4312, "resetAt":
                                                        "2026-10-09T20:00:00Z", "limit": 5000},
                                 # Card #106: alpha is "the board this box deploys" — the
                                 # Ship desk's per-lane view belongs here, box-wide.
                                 deploy_repo="roloc/alpha",
                                 deploy_fn=lambda: {"state": "live", "live": "aaaaaaa", "target": "aaaaaaa"})


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
# Someone in the break room may be mid-walk (the page wanders one lane every few seconds),
# and two walkers may cross: the check is only read once nobody is moving, in the same
# synchronous call, so a timer cannot start a walk between the look and the measure.
NO_OVERLAP = """(() => {
  const chars = [...document.querySelectorAll('.character')];
  if (chars.some(e => e.getAnimations().some(a => a instanceof CSSTransition && a.playState === 'running'))) return null;
  const boxes = chars.map(e => [e.dataset.lane || ('ship:' + e.dataset.ship), e.getBoundingClientRect()]);
  const hits = [];
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const [na, a] = boxes[i], [nb, b] = boxes[j];
    if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) hits.push(na + ' on ' + nb);
  }
  return hits;
})()"""


def overlaps(b):
    """The pairs of characters standing on each other, read once everyone has stopped."""
    for _ in range(40):
        hits = b.eval(NO_OVERLAP)
        if hits is not None:
            return hits
        time.sleep(0.1)
    return ["still walking after four seconds"]


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
        check("card #98: the account's GraphQL budget is drawn in the header, from the "
              "fixture's ratelimit_fn — remaining, limit, and a reset time",
              b.eval("document.getElementById('github-budget').textContent")
              == "GitHub: 4,312 / 5,000 · resets " + b.eval(
                  "new Date('2026-10-09T20:00:00Z').toLocaleTimeString()"))
        shot(b, "floor-desktop.png")

        print("2b. the Needs Steve strip (card #96): a mockup to approve right on the "
              "floor, for a card Stuck on the board even though no lane's own desk shows it")
        check("#778's ask is on the strip, though no lane's last tick is #778",
              "pick a direction for the earlier thing" in text, text[:800])
        slide = "document.querySelector('.carousel[data-carousel=\"778\"]')"
        check("the carousel is drawn, with a picture", b.wait_for(f"!!{slide}?.querySelector('.car-img')"))
        check("it opens on direction A, recommended, 1 of 4",
              b.eval(f"{slide}.querySelector('.car-chip').textContent") == "A ★"
              and b.eval(f"{slide}.querySelector('.car-caption').textContent") == "phone"
              and b.eval(f"{slide}.querySelector('.car-counter').textContent") == "1 of 4")
        b.click('[data-car-next="778"]')
        check("next: still A, now the desktop shot, 2 of 4",
              b.eval(f"{slide}.querySelector('.car-chip').textContent") == "A ★"
              and b.eval(f"{slide}.querySelector('.car-caption').textContent") == "desktop"
              and b.eval(f"{slide}.querySelector('.car-counter').textContent") == "2 of 4")
        b.click('[data-car-next="778"]')
        check("next again: direction B, not recommended, phone, 3 of 4",
              b.eval(f"{slide}.querySelector('.car-chip').textContent") == "B"
              and b.eval(f"{slide}.querySelector('.car-caption').textContent") == "phone"
              and b.eval(f"{slide}.querySelector('.car-counter').textContent") == "3 of 4")
        b.click('[data-car-prev="778"]')
        check("previous: back to direction A's desktop shot, 2 of 4",
              b.eval(f"{slide}.querySelector('.car-chip').textContent") == "A ★"
              and b.eval(f"{slide}.querySelector('.car-caption').textContent") == "desktop"
              and b.eval(f"{slide}.querySelector('.car-counter').textContent") == "2 of 4")
        check("no uncaught exception after flipping through the carousel", b.errors == [], b.errors)

        # Arrow keys, pressed more than once in a row: a re-render replaces the carousel's
        # own DOM node on every step, which used to drop focus to <body> after the first
        # key — so the first ArrowRight worked and every one after it silently did nothing.
        b.eval(f"{slide}.focus()")
        check("the carousel can take keyboard focus", b.eval(f"document.activeElement === {slide}"))
        b.eval("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', "
               "{key: 'ArrowRight', bubbles: true}))")
        check("first ArrowRight: direction B's phone shot, 3 of 4",
              b.eval(f"{slide}.querySelector('.car-counter').textContent") == "3 of 4")
        b.eval("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', "
               "{key: 'ArrowRight', bubbles: true}))")
        check("second ArrowRight in a row still moves it: direction B's desktop shot, 4 of 4",
              b.eval(f"{slide}.querySelector('.car-counter').textContent") == "4 of 4")
        b.eval("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', "
               "{key: 'ArrowLeft', bubbles: true}))")
        check("ArrowLeft steps back: 3 of 4",
              b.eval(f"{slide}.querySelector('.car-counter').textContent") == "3 of 4")

        # A swipe: the same prev/next, from a touch gesture rather than a tap on an arrow.
        swipe = """((dx) => {
          const el = document.querySelector('.carousel[data-carousel="778"]');
          const r = el.getBoundingClientRect(), y = r.top + r.height / 2, x = r.left + r.width / 2;
          const touch = (cx) => new Touch({identifier: 1, target: el, clientX: cx, clientY: y});
          el.dispatchEvent(new TouchEvent('touchstart', {touches: [touch(x)], changedTouches: [touch(x)], bubbles: true}));
          el.dispatchEvent(new TouchEvent('touchend', {touches: [], changedTouches: [touch(x + dx)], bubbles: true}));
        })"""
        b.eval(f"{swipe}(-80)")  # a swipe left: forward, same as next
        check("a swipe left moves forward: direction B's desktop shot, 4 of 4",
              b.eval(f"{slide}.querySelector('.car-counter').textContent") == "4 of 4")
        b.eval(f"{swipe}(80)")  # a swipe right: back
        check("a swipe right moves back: 3 of 4",
              b.eval(f"{slide}.querySelector('.car-counter').textContent") == "3 of 4")
        check("no uncaught exception after the keyboard and swipe round trip", b.errors == [], b.errors)
        shot(b, "floor-needs-strip.png")

        print("2c. card #106: the Ship desk names which release each lane on the box is on")
        ship_text = b.eval("document.querySelector('.desk.ship').textContent")
        check("the project that deploys names its own lanes: 2 of 3 on the live release",
              "alpha: 2 of 3 lane(s) on it" in ship_text, ship_text)
        check("...and names the one still finishing on an older release",
              "alpha-design still finishing on an older one" in ship_text, ship_text)
        check("a lane box-wide, in a different project, shows up here too — one deploy, every lane",
              "beta: 0 of 1 lane(s) on it (beta still finishing on an older one)" in ship_text, ship_text)
        check("no uncaught exception with the per-lane release line drawn", b.errors == [], b.errors)
        shot(b, "ship-desk-releases.png", full_page=False)

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
        check("the Needs Steve carousel fills the width on a phone, one picture at a time, "
              "and its arrows are big enough for a thumb",
              b.eval("""(() => {
                const c = document.querySelector('.carousel[data-carousel="778"]').getBoundingClientRect();
                const btn = document.querySelector('[data-car-next="778"]').getBoundingClientRect();
                return c.right <= window.innerWidth + 1 && btn.width >= 20 && btn.height >= 20;
              })()"""))
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
    ROOM_NAMES = "[...document.querySelectorAll('.office-room')].map(r => r.dataset.room)"

    # Which room a character's feet are in (its box's bottom edge, which is where it stands;
    # the plate hangs below the feet, so the box bottom is the plate's bottom), and its plate.
    def info(b, lane):
        return b.eval(f"""(() => {{
          const e = document.querySelector('.character[data-lane="{lane}"]');
          const r = e.getBoundingClientRect(), feet = r.bottom - e.querySelector('.plate').getBoundingClientRect().height;
          const room = [...document.querySelectorAll('.office-room')].find(z => {{
            const q = z.getBoundingClientRect();
            return r.left >= q.left - 1 && r.right <= q.right + 1 && feet > q.top && feet <= q.bottom + 1; }});
          return [e.dataset.pill, room ? room.dataset.room : null, e.querySelector('.plate').innerText.trim()]; }})()""")

    print("11. the studio: an office the lanes walk around in (card #88)")
    with browse.Browser(width=1280, height=900) as b:
        b.goto(URL)
        b.wait_for(DESKS_DRAWN)
        check("the floor page itself still looks as it does today: the Studio button, nothing else new",
              b.eval("getComputedStyle(document.getElementById('studio-btn')).display") != "none")
        b.click("#studio-btn")
        check("the studio opens and the floor's own sections hide",
              b.wait_for(STUDIO_OPEN)
              and b.eval("getComputedStyle(document.getElementById('floor-view')).display") == "none")
        # The page wanders one idle lane every nine seconds on its own. This section measures
        # where people stand, so it wanders them itself (12c) and stops the page doing so.
        b.eval("clearInterval(window.__studioWanderTimer); true")
        check("the Studio button hides once the room is open", b.wait_for(
              "getComputedStyle(document.getElementById('studio-btn')).display === 'none'"))
        check("no uncaught exception, console.error or failed request", b.errors == [], b.errors)
        check("one character per lane, plus one per ship station: six, same as the desks",
              b.wait_for(CHAR_COUNT + " === 6"))
        settled(b)
        shot(b, "studio-desktop.png")

        print("12. an office: a break room, Steve's office, and a room per board")
        rooms = b.eval(ROOM_NAMES)
        check("the break room and Steve's office, then one room per board in the board's order",
              rooms == ["Break room", "Steve's office", "alpha", "beta"], rooms)
        check("a board's room has its name on the door, a link to the board in a new tab",
              b.eval("[...document.querySelectorAll('.office-room.team .room-name')]"
                     ".every(a => a.href && a.target === '_blank')"))
        check("the board's counts are on the room's whiteboard",
              b.eval("document.querySelector('.office-room[data-room=\"alpha\"] .whiteboard').textContent")
              == "Ready 0 · Doing 1 · Backlog 0")
        check("alpha (working) is at its desk in alpha's room, its plate saying what it is",
              info(b, "alpha") == ["working", "alpha", "Build"], info(b, "alpha"))
        check("beta (paused) is at its own desk in beta's room, hands up, not off anywhere",
              info(b, "beta") == ["paused", "beta", "Build"], info(b, "beta"))
        check("alpha-design (idle) has gone to the break room, mug in hand, its plate saying whose it is",
              info(b, "alpha-design") == ["idle", "Break room", "Design\nalpha"], info(b, "alpha-design"))
        check("alpha-triage (idle) is in the break room too",
              info(b, "alpha-triage")[:2] == ["idle", "Break room"], info(b, "alpha-triage"))
        check("the mug is drawn (the pose says idle, not just the dark screen)",
              b.eval("document.querySelector('.character[data-lane=\"alpha-design\"] svg').innerHTML.includes('f3f4f6')"))
        check("their desks are still in alpha's room, each tagged as theirs and out",
              b.eval("[...document.querySelectorAll('.desk-tag')].map(t => t.dataset.lane + ':' + t.textContent).sort()")
              == ["alpha-design:Design · out", "alpha-triage:Triage · out"])
        check("the break room has a couch, a coffee machine (steaming) and a vending machine",
              b.eval("""(() => { const r = document.querySelector('.office-room.break');
                      return !!r.querySelector('.couch') && !!r.querySelector('.prop .steam')
                          && r.querySelectorAll('.prop').length >= 4; })()"""))
        check("Steve's desk has his name on it", b.eval(
              "document.querySelector('.office-room.boss').textContent.includes('STEVE')"))
        check("nobody stands on anybody", overlaps(b) == [], overlaps(b))

        print("12a. the in-tray on Steve's desk (card #96): #778 is there, named for alpha, "
              "even though no lane is in his office for it")
        check("#778's chip is in the in-tray, after its board's name",
              b.eval("document.querySelector('.row-stuck-chip')?.textContent") == "#778"
              and b.eval("document.querySelector('.tray-board')?.textContent") == "alpha")
        check("...and the tray's own carousel shows its picture too",
              b.wait_for("!!document.querySelector('#room-stuck .car-img')"))
        check("the tray is in Steve's office, in front of his desk, and the office grew to hold it",
              b.eval("""(() => { const t = document.querySelector('.in-tray').getBoundingClientRect();
                      const o = document.querySelector('.office-room.boss').getBoundingClientRect();
                      const d = document.querySelector('.office-room.boss .prop').getBoundingClientRect();
                      return t.left >= o.left && t.right <= o.right && t.top >= d.bottom - 12 && t.bottom <= o.bottom; })()"""))
        check("no uncaught exception", b.errors == [], b.errors)
        shot(b, "studio-needs-strip.png")

        print("12b. the floor Steve sent back on October 8, 2026: seven lanes, six idle, three boards")
        b.eval("render(" + LIVE_SHAPED + "); renderStudio(" + LIVE_SHAPED + "); true")
        settled(b)
        check("ten characters: seven lanes and three ship docks", b.eval(CHAR_COUNT) == 10)
        check("no two characters overlap (six idle lanes used to share five fixed spots, so the "
              "sixth stood on the first)", overlaps(b) == [], overlaps(b))
        rooms = b.eval(ROOM_NAMES)
        check("three team rooms, named for their boards",
              rooms == ["Break room", "Steve's office", "riffmap", "riffmap-site", "watcher"], rooms)
        check("all six idle lanes are in the break room, each plate naming its board",
              b.eval("""[...document.querySelectorAll('.character[data-pill="idle"]')]
                     .every(e => e.dataset.room === 'Break room' && e.querySelector('.where').textContent === e.dataset.project)"""))
        check("a ship shelf stands in each board's own room, on the same row as its desks",
              b.eval("""(() => { const s = document.querySelector('.character[data-ship="riffmap"]').getBoundingClientRect();
                      const d = document.querySelector('.character[data-lane="riffmap-site-design"]').getBoundingClientRect();
                      const r = document.querySelector('.office-room[data-room="riffmap"]').getBoundingClientRect();
                      return s.left >= r.left && s.right <= r.right && s.bottom <= r.bottom + 1 && Math.abs(s.bottom - d.bottom) < 2; })()"""))
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
        check("every pinned card on the bulletin board is whole, none cut off at its edge", cut == 0, cut)
        check("the first pinned card is in view without scrolling the board",
              b.eval("""(() => { const c = document.querySelector('.pin-card').getBoundingClientRect();
                      const w = document.getElementById('pin-wall').getBoundingClientRect();
                      return c.left >= w.left - 1 && c.right <= w.right + 1; })()"""))
        shot(b, "studio-live-shaped.png")

        print("12c. the break room wanders: someone gets up and walks to another spot, nobody else moves")
        before = b.eval("Object.fromEntries([...document.querySelectorAll('.character')]"
                        ".map(e => [e.dataset.lane || ('ship:' + e.dataset.ship), e.style.left + ' ' + e.style.top]))")
        b.eval("window.__studioWander(); true")
        settled(b)
        after = b.eval("Object.fromEntries([...document.querySelectorAll('.character')]"
                       ".map(e => [e.dataset.lane || ('ship:' + e.dataset.ship), e.style.left + ' ' + e.style.top]))")
        moved = sorted(k for k in before if before[k] != after[k])
        check("exactly one idle lane moved", len(moved) == 1
              and b.eval(f"document.querySelector('.character[data-lane=\"{moved[0]}\"]').dataset.pill") == "idle", moved)
        check("...to another spot in the break room", info(b, moved[0])[1] == "Break room", info(b, moved[0]))
        check("and still nobody stands on anybody", overlaps(b) == [], overlaps(b))
        check("a working lane never wanders (riffmap-site-design is where it was)",
              before["riffmap-site-design"] == after["riffmap-site-design"])
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

        print("13. the light over Steve's door reads the same state the pills do")
        check("a paused lane, a failing build, AND #778 sitting Stuck on alpha's board: red "
              "— a Stuck card outranks amber even with no lane pointing at it (card #96)",
              b.eval("document.getElementById('beacon').className") == "beacon-red")
        check("the light is actually visible — inside #room's own box, which clips "
              "(it used to sit mostly above it, clipped to a sliver) — and over his door",
              b.eval("""(() => {
                const beacon = document.getElementById("beacon").getBoundingClientRect();
                const room = document.getElementById("room").getBoundingClientRect();
                const office = document.querySelector(".office-room.boss").getBoundingClientRect();
                return beacon.top >= room.top && beacon.bottom <= room.bottom && beacon.height > 10
                    && beacon.left >= office.left && beacon.right <= office.right && beacon.bottom <= office.bottom + 2;
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

        print("15. the bulletin board in the hallway: recent ticks, newest first, each one a link")
        # Both fixture ticks are pinned; which sorts first depends on the real mtime of the
        # two log files this suite just wrote (`metrics.py` keys "when" on that, not the
        # stamp in the filename) — true to #5 and #3, never guess which ran "newer" here.
        hrefs = b.eval("[...document.querySelectorAll('.pin-card')].map(a => a.getAttribute('href'))")
        check("one pinned card per recent tick, each linking straight to its issue",
              sorted(hrefs) == ["https://github.com/roloc/alpha/issues/5",
                                "https://github.com/roloc/beta/issues/3"], hrefs)
        check("the board hangs in the hallway, between the top rooms and the team rooms",
              b.eval("""(() => { const w = document.getElementById('pin-wall').getBoundingClientRect();
                      const top = document.querySelector('.office-room.boss').getBoundingClientRect();
                      const team = document.querySelector('.office-room.team').getBoundingClientRect();
                      return w.top >= top.bottom && w.bottom <= team.top; })()"""))

        print("16. Floor goes back, and the floor page is exactly as it was")
        b.click("#floor-btn")
        check("the studio closes and the floor's sections return",
              b.wait_for("!" + STUDIO_OPEN)
              and b.eval("getComputedStyle(document.getElementById('floor-view')).display") != "none")
        check("the Studio button is back", b.eval(
              "getComputedStyle(document.getElementById('studio-btn')).display") != "none")
        check("the floor's own desks are still there, same as section 2", b.eval(CHAR_COUNT) >= 0
              and b.eval("document.querySelectorAll('.desk').length") == 6)

    print("17. on a phone: the office fits without scrolling sideways, and walking respects reduced motion")
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
              overlaps(b) == [], overlaps(b))
        check("...and the office still fits the screen",
              b.eval("document.documentElement.scrollWidth <= window.innerWidth"))
        check("on a phone the break room sits over Steve's office, each room as tall as it needs",
              b.eval("""(() => { const br = document.querySelector('.office-room.break').getBoundingClientRect();
                      const bo = document.querySelector('.office-room.boss').getBoundingClientRect();
                      return bo.top >= br.bottom && Math.abs(br.width - bo.width) < 2; })()"""))
        check("every room is inside the office, none runs off its bottom edge",
              b.eval("""(() => { const room = document.getElementById('room').getBoundingClientRect();
                      return [...document.querySelectorAll('.office-room')].every(z => { const q = z.getBoundingClientRect();
                        return q.bottom <= room.bottom + 1 && q.right <= room.right + 1; }); })()"""))
        check("eight cards on a phone-wide board: it scrolls, and its edge fades to say so",
              b.eval("document.getElementById('pin-wall').classList.contains('scrolls')"))
        shot(b, "studio-live-shaped-phone.png")
        # The same board drawn while the studio was closed (every 30s poll draws it hidden)
        # and then opened: the fade has to be there on the first open, not the next poll.
        b.click("#floor-btn")
        b.wait_for("!" + STUDIO_OPEN)
        b.eval("render(" + LIVE_SHAPED + "); renderStudio(" + LIVE_SHAPED + "); true")
        b.click("#studio-btn")
        b.wait_for(STUDIO_OPEN)
        check("a board drawn while the studio was hidden still fades the moment it opens",
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

    print("18. demo mode (?demo=1): cycles the fixture through every state, for designing the office")
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
        check("stepping through the fixture's states changes the light over Steve's door (never stuck on one color)",
              len(set(colors)) > 1, colors)
        b.eval("window.__demoGoto(1)")  # the demo state with a needs_steve lane
        check("a needs_steve lane waves, not just stands with its hands up",
              b.eval("!!document.querySelector("
                    "'.character[data-pill=\"needs_steve\"] .character-wave')"))
        check("...and holds up the mockup its card is waiting on an answer for (card #91), "
              "right there in the room — no click through to the card needed",
              b.eval("!!document.querySelector("
                    "'.character[data-pill=\"needs_steve\"] .character-mockup img')"))
        HELD_ALT = "document.querySelector('.character[data-pill=\"needs_steve\"] .character-mockup img')?.alt"
        check("it starts on direction A's first shot",
              b.eval(HELD_ALT) == "phone light", b.eval(HELD_ALT))
        b.eval("stepCarousel(11, 1)")
        check("stepping the shared carousel (card #96) moves what the podium holds too, "
              "not just the floor's own strip", b.eval(HELD_ALT) == "phone dark", b.eval(HELD_ALT))
        settled(b)
        check("it walked to Steve's office, and its plate says whose it is",
              info(b, "alpha") == ["needs_steve", "Steve's office", "Build\nalpha"], info(b, "alpha"))
        check("the light over his door is red while it waits there",
              b.eval("document.getElementById('beacon').className") == "beacon-red")
        check("the sleeping lane naps on the break room couch, named for its board too",
              info(b, "beta") == ["sleeping", "Break room", "Build\nbeta"]
              and b.eval("""(() => { const e = document.querySelector('.character[data-pill="sleeping"]').getBoundingClientRect();
                      const c = document.querySelector('.couch').getBoundingClientRect();
                      const x = (e.left + e.right) / 2; return x >= c.left && x <= c.right; })()"""),
              info(b, "beta"))
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
