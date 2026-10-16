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
import struct
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
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

# --- browse.py shot: the page's whole height, never wider than the window -------------
#
# Cards #170, #172 (October 15, 2026): a 390px "phone" shot of a page whose table overflows
# sideways came out 495px wide — Chrome's captureBeyondViewport widens to the scroll width —
# so the usability pass was handed a picture that was not a phone and said so, costing the
# replay suite's screen case a round on every run. A phone clips; so does the shot now.

def _png_size(path):
    with open(path, "rb") as f:
        b = f.read(24)
    return struct.unpack(">II", b[16:24])


_wide_dir = tempfile.mkdtemp(prefix="watcher-browse-wide-")
with open(os.path.join(_wide_dir, "index.html"), "w") as f:
    f.write("<!doctype html><html><head><meta charset='utf-8'><style>"
            "td, th { border: 1px solid #333; padding: 10px 18px; white-space: nowrap; }"
            "</style></head><body><h1>Status</h1><table><tr>"
            + "".join(f"<th>Column {i}</th>" for i in range(8)) + "</tr><tr>"
            + "".join(f"<td>value {i}</td>" for i in range(8)) + "</tr></table>"
            + "<p style='height:2000px'>tall</p></body></html>")
with browse.Browser(width=390, height=844) as b:
    b.goto("file://" + os.path.join(_wide_dir, "index.html"))
    b.pump(0.3)
    _shot = b.screenshot(os.path.join(_wide_dir, "phone.png"))
_w, _h = _png_size(_shot)
check("a 390px-window shot of a page that overflows sideways is 390px wide, not the scroll width",
      _w == 390, (_w, _h))
check("...and still the page's whole height, not just the first screen", _h > 844, (_w, _h))
shutil.rmtree(_wide_dir, ignore_errors=True)

# --- a fixture floor: two boards, four lanes, real files -------------------------------

ROOT = tempfile.mkdtemp(prefix="watcher-floor-browser-")
PROJECTS = os.path.join(ROOT, "projects")
STATE = os.path.join(ROOT, "state")
CLAUDE = os.path.join(ROOT, "claude-projects")
# Card #164: the canary lane's own CLAUDE_CONFIG_DIR's projects/ (bin/spawn.sh), where ITS
# transcripts go — the fixture passes both to the page, the way `tail.projects_dirs` does.
CANARY = os.path.join(ROOT, "claude-canary-projects")
for d in (PROJECTS, STATE, CLAUDE, CANARY):
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


def open_call(description, minutes_ago, tool_id="t-wait"):
    """A `run()` with the two fields `tail.current_step` (card #165) actually needs: a real
    `id` (so it can be matched against a later `tool_result`, which this never writes — the
    call stays open) and a `timestamp` real minutes in the past, since the live fixture's
    own `now` is the wall clock (`LiveCache.get()` passes no `now=` override)."""
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({"type": "assistant", "timestamp": when, "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash",
         "input": {"description": description, "command": "x"}}]}}) + "\n"


def transcript(worktree_base, card, lines, under=CLAUDE):
    d = os.path.join(under, tail.encode_dir(os.path.join(worktree_base, f"card-{card}")))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "session-1.jsonl")
    with open(path, "w") as f:
        f.writelines(lines)
    return path


def tick_log(lane, card, stamp):
    with open(os.path.join(STATE, lane, "logs", f"card-{card}-{stamp}.json"), "w") as f:
        json.dump({"subtype": "success", "total_cost_usd": 1.25, "num_turns": 12, "duration_ms": 1000,
                   "usage": {"input_tokens": 600, "output_tokens": 200, "cache_read_input_tokens": 200},
                   "result": f"- Card: #{card} x\n- Outcome: merged\n- Ended in: Done\n"}, f)


# alpha (Build): working on #7 right now, with a session being written — AND a PAUSE file
# beside it (card #110: a deploy mid-tick never means idle, so this still reads working).
with open(os.path.join(STATE, "alpha", "current"), "w") as f:
    f.write("7\n")
with open(os.path.join(STATE, "alpha", "PAUSE"), "w") as f:
    f.write("deploy c920516\n")
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
                    "labels": [], "url": "https://github.com/roloc/alpha/issues/778"},
                   # #5 shipped: alpha-design's last tick took it to Done, and the board has
                   # its title — the wins board shows it by name.
                   {"number": 5, "state": "CLOSED", "status": "Done", "stage": "Done", "title": "Mockup for the earlier thing",
                    "labels": [], "url": "https://github.com/roloc/alpha/issues/5"}],
    ("roloc", 2): [],
}

# What the bulletin board's Peek reads off GitHub (`gh issue view`), canned: #7 has a body,
# labels and a comment; every other card is one gh cannot read.
CARDS = {("alpha", 7): {"title": "Build the thing", "body": "A card body, straight from GitHub.\n\n- one\n- two",
                        "state": "OPEN", "url": "https://github.com/roloc/alpha/issues/7", "labels": ["agent: builder"],
                        "comments": [{"author": "roloc", "when": "2026-10-09T10:00:00Z", "body": "Looks good, ship it."}],
                        "created": "2026-10-08T10:00:00Z", "closed": ""}}
card_lookup = floor.make_card_lookup(lambda: {"alpha": "roloc/alpha", "beta": "roloc/beta"},
                                     fetcher=lambda n, repo: CARDS.get((repo.split("/")[-1], n)))

BOX = {"cpus": 4, "load1": 0.42, "mem_total": 8589934592, "mem_used": 2894311424,
       "disk_total": 50407927808, "disk_used": 33793540096, "uptime": 2786044}
PVE = floor.proxmox_summary([
    {"type": "node", "node": "proxmox", "status": "online", "cpu": 0.0096, "maxcpu": 8, "mem": 14646714368, "maxmem": 67314438144},
    {"type": "lxc", "name": "ollama", "vmid": 101, "status": "stopped", "cpu": 0, "maxcpu": 4, "mem": 0, "maxmem": 21474836480},
    {"type": "lxc", "name": "agents", "vmid": 107, "status": "running", "cpu": 0.0083, "maxcpu": 4, "mem": 2894311424, "maxmem": 8589934592},
    {"type": "lxc", "name": "ci", "vmid": 108, "status": "running", "cpu": 0.00002, "maxcpu": 4, "mem": 2403749888, "maxmem": 8589934592},
    {"type": "storage", "storage": "local"}])

ROUNDS = {778: [{"when": "2026-10-09T08:00:00Z", "images": [
    {"url": TINY_PNG, "label": "phone", "letter": "A", "recommended": True},
    {"url": TINY_PNG, "label": "desktop", "letter": "A", "recommended": True},
    {"url": TINY_PNG, "label": "phone", "letter": "B", "recommended": False},
    {"url": TINY_PNG, "label": "desktop", "letter": "B", "recommended": False},
]}]}
# The real comment's first line keeps its own `**...**` wrapping (card #100: "the ask line
# shows raw ** markdown") — the fixture carries that so the rendering fix actually gets
# exercised, not just a pre-cleaned string.
ASKS = {778: "**Needs Steve: pick a direction for the earlier thing**"}


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
                                 ledger_fn=lambda: {"points_per_min": 1.5, "by_caller":
                                                     {"lane:alpha": 60, "tick:alpha:7": 30}},
                                 box_stats_fn=lambda: BOX, proxmox_fn=lambda: PVE,
                                 # Card #106: alpha is "the board this box deploys" — the
                                 # Ship desk's per-lane view belongs here, box-wide.
                                 deploy_repo="roloc/alpha",
                                 deploy_fn=lambda: {"state": "live", "live": "aaaaaaa", "target": "aaaaaaa"},
                                 # Card #165: the same two directories tail_lookup (below)
                                 # already searches for Peek — the desk's own "Waiting"
                                 # line reads the identical session, never a second lookup.
                                 claude_projects_dir=[CLAUDE, CANARY])


def tail_lookup(lane, card, file, pos):
    info = {l["lane"]: l for l in floor.discover_lanes(PROJECTS, STATE)}.get(lane)
    return None if info is None else floor.tail_status(info, [CLAUDE, CANARY], card, file, pos)


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
# Card #105: a plain dict a case can swap out (7d, below) to show a deploy mid-flight,
# the same way CI_RUNS["roloc/alpha"] is swapped above — restored afterward so every other
# case still sees the box simply "live".
SHIP_DEPLOY = {"state": "live", "live": "aaaaaaa", "target": "aaaaaaa", "alive": None,
               "step": None, "waiting_for": [], "lands_by": None}
ship_lookup = floor.make_ship_lookup(
    lambda: {"alpha": "roloc/alpha", "beta": "roloc/beta"}, lambda repo: CI_RUNS[repo],
    deploy_repo="roloc/alpha", deploy_fn=lambda: SHIP_DEPLOY,
    history_fn=lambda: [(floor.datetime(2026, 10, 1, 17, 59, tzinfo=floor.timezone.utc),
                         "deployed aaaaaaa; every lane un-paused")],
    cache_sec=0)

cache = LiveCache()
server = QuietServer(("127.0.0.1", 0), floor.make_handler(cache, tail_lookup, ship_lookup, card_lookup))
URL = f"http://127.0.0.1:{server.server_address[1]}/"
threading.Thread(target=server.serve_forever, daemon=True).start()

DESKS_DRAWN = "document.querySelectorAll('.desk').length > 0"
PANEL_OPEN = "document.getElementById('peek-overlay').classList.contains('open')"
PANEL_TEXT = "document.getElementById('peek-lines').textContent"
PANEL_SUB = "document.getElementById('peek-sub').textContent"
PANEL_DEPLOY = "document.getElementById('peek-deploy').textContent"
PAGE_URL = "location.href"


def peek_button(lane):
    return f'.peek-btn[data-peek-lane="{lane}"]'


def shot(b, name, full_page=True):
    if shots_dir:
        print("  wrote " + b.screenshot(os.path.join(shots_dir, name), full_page=full_page))


def settled(b):
    """Wait out every walk (a hallway walk is seconds long), so a check or a picture sees
    where everyone ended up, not where they were a tenth of a second in."""
    return b.wait_for("[...document.querySelectorAll('.character')]"
                      ".every(e => e.getAnimations().every(a => a.playState !== 'running'))", timeout=25)


# What no two of may overlap: every character's own box (figure and plate), as drawn.
# Someone in the break room may be mid-walk (the page wanders one lane every few seconds),
# and two walkers may cross: the check is only read once nobody is moving, in the same
# synchronous call, so a timer cannot start a walk between the look and the measure.
NO_OVERLAP = """(() => {
  const chars = [...document.querySelectorAll('.character')];
  if (chars.some(e => e.getAnimations().some(a => a.playState === 'running'))) return null;
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

# card #103: one working desk, 17 minutes in, with and without its plan comment — the same
# shape, used at both desktop and phone width.
LATE_STATE = """{header: {box: 'agents', time: new Date().toISOString(), needs_steve_total: 0},
  projects: [{name: 'gamma', board_url: '#', counts: {ready: 0, in_progress: 1, backlog: 0},
    lanes: [{lane: 'gamma', kind: 'Build', unit: 'watcher@gamma', pill: 'working', link: '#',
             card: {number: 42, url: 'https://example.test/42'}, stage: 'Building',
             minutes: 17, plan_posted: false, plan_late: true}],
    ship: {ci: {state: 'passing', sha: 'abc1234', url: '#'}}}], recent: []}"""
POSTED_STATE = """{header: {box: 'agents', time: new Date().toISOString(), needs_steve_total: 0},
  projects: [{name: 'gamma', board_url: '#', counts: {ready: 0, in_progress: 1, backlog: 0},
    lanes: [{lane: 'gamma', kind: 'Build', unit: 'watcher@gamma', pill: 'working', link: '#',
             card: {number: 42, url: 'https://example.test/42'}, stage: 'Building',
             minutes: 17, plan_posted: true, plan_late: false}],
    ship: {ci: {state: 'passing', sha: 'abc1234', url: '#'}}}], recent: []}"""


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
        check("card #110: alpha is holding #7 WITH a PAUSE file beside it, and still reads "
              "Working, not Paused — paused means it takes nothing new, not that it is idle",
              b.eval("document.querySelector('.desk-link[href*=\"/issues/7\"] .pill')"
                     ".className") == "pill working",
              b.eval("document.querySelector('.desk-link[href*=\"/issues/7\"] .pill')?.outerHTML"))
        check("card #110: a short pausing line sits under the card, in plain words — never "
              "the PAUSE file's path or its sha",
              b.eval("document.querySelector('.desk-link[href*=\"/issues/7\"] .pausing')"
                     ".textContent") == "pausing after this card for a deploy",
              b.eval("document.querySelector('.desk-link[href*=\"/issues/7\"] .pausing')?.textContent"))
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
        check("card #114: this hour's GraphQL spend by caller is drawn beside it, from "
              "the fixture's ledger_fn — highest spender first, points per minute too",
              b.eval("document.getElementById('github-spend').textContent")
              == "This hour: lane:alpha 60 · tick:alpha:7 30 (1.5 pts/min)",
              b.eval("document.getElementById('github-spend').textContent"))
        check("…and it is not drawn red: 1.5 pts/min would not exhaust 4,312 points "
              "before the hour resets",
              b.eval("document.getElementById('github-spend').classList.contains('alert')")
              is False)
        shot(b, "floor-desktop.png")

        print("2b. the Needs Steve strip (card #100): no raw markdown, a chip per "
              "direction, and a Review button that opens the pictures over the whole "
              "window instead of a sliver inline")
        check("#778's ask is on the strip, rendered, though no lane's last tick is #778",
              "pick a direction for the earlier thing" in text, text[:800])
        check("no raw ** markdown reached the page (card #100's own complaint about #96)",
              "**" not in b.text(), b.text()[:800])
        check("the ask is actually bold, not just stripped",
              b.eval("document.querySelector('#projects .needs-ask strong')") is not None)
        chips = "[...document.querySelectorAll('#projects .direction-chip')].map(c => c.textContent)"
        check("the strip names both directions it offers, A marked recommended",
              b.eval(chips) == ["A ★", "B"], b.eval(chips))
        check("the old inline sliver is gone; a Review button stands in its place",
              b.eval("document.querySelector('#projects .car-img')") is None
              and b.eval("document.querySelector('#projects [data-review=\"778\"]')") is not None)

        print("2c. tapping Review opens the pictures over the whole window (card #100)")
        OPEN = "document.getElementById('review-overlay').classList.contains('open')"
        IMG = "document.getElementById('review-img')"
        CAP = "document.getElementById('review-caption').textContent"
        CNT = "document.getElementById('review-counter').textContent"
        NEWTAB = "document.getElementById('review-newtab')"
        b.click('#projects [data-review="778"]')
        check("the overlay opens", b.wait_for(OPEN))
        check("the picture fills at least the viewport's width, not a sliver",
              b.eval(f"{IMG}.getBoundingClientRect().width") >= b.eval("window.innerWidth") * 0.85,
              b.eval(f"{IMG}.getBoundingClientRect().width"))
        check("it opens on direction A, recommended, the phone shot, 1 of 4",
              b.eval(CAP) == "A ★ — phone" and b.eval(CNT) == "1 of 4", (b.eval(CAP), b.eval(CNT)))
        shot(b, "review-overlay-desktop.png", full_page=False)
        check("the page behind does not scroll while it is open",
              b.eval("getComputedStyle(document.body).overflow") == "hidden")
        b.click("#review-next")
        check("next: still A, now the desktop shot, 2 of 4",
              b.eval(CAP) == "A ★ — desktop" and b.eval(CNT) == "2 of 4", (b.eval(CAP), b.eval(CNT)))
        b.click("#review-next")
        check("next again: direction B, not recommended, phone, 3 of 4",
              b.eval(CAP) == "B — phone" and b.eval(CNT) == "3 of 4", (b.eval(CAP), b.eval(CNT)))
        b.click("#review-prev")
        check("previous: back to direction A's desktop shot, 2 of 4",
              b.eval(CAP) == "A ★ — desktop" and b.eval(CNT) == "2 of 4", (b.eval(CAP), b.eval(CNT)))
        check("the open-in-new-tab control carries the current picture's own URL and opens in a new tab",
              b.eval(f"{NEWTAB}.href") == TINY_PNG and b.eval(f"{NEWTAB}.target") == "_blank")
        check("no uncaught exception after flipping through the overlay", b.errors == [], b.errors)

        print("2d. arrow keys and a swipe flip it too, same as the old carousel did")
        b.key("ArrowRight")
        check("ArrowRight: direction B's phone shot, 3 of 4", b.eval(CNT) == "3 of 4", b.eval(CNT))
        b.key("ArrowRight")
        check("ArrowRight again: direction B's desktop shot, 4 of 4", b.eval(CNT) == "4 of 4", b.eval(CNT))
        b.key("ArrowLeft")
        check("ArrowLeft steps back: 3 of 4", b.eval(CNT) == "3 of 4", b.eval(CNT))
        swipe = """((dx) => {
          const el = document.getElementById('review-img');
          const r = el.getBoundingClientRect(), y = r.top + r.height / 2, x = r.left + r.width / 2;
          const touch = (cx) => new Touch({identifier: 1, target: el, clientX: cx, clientY: y});
          el.dispatchEvent(new TouchEvent('touchstart', {touches: [touch(x)], changedTouches: [touch(x)], bubbles: true}));
          el.dispatchEvent(new TouchEvent('touchend', {touches: [], changedTouches: [touch(x + dx)], bubbles: true}));
        })"""
        b.eval(f"{swipe}(-80)")  # a swipe left: forward, same as next
        check("a swipe left moves forward: direction B's desktop shot, 4 of 4", b.eval(CNT) == "4 of 4", b.eval(CNT))
        b.eval(f"{swipe}(80)")  # a swipe right: back
        check("a swipe right moves back: 3 of 4", b.eval(CNT) == "3 of 4", b.eval(CNT))
        check("no uncaught exception after the keyboard and swipe round trip", b.errors == [], b.errors)

        print("2e. zoom: a double-click zooms in and resets when the picture flips")
        b.eval(f"{IMG}.dispatchEvent(new MouseEvent('dblclick', {{bubbles: true}}))")
        check("a double-click zooms in",
              "scale(2.5)" in b.eval(f"{IMG}.style.transform"), b.eval(f"{IMG}.style.transform"))
        b.key("ArrowRight")
        check("flipping to the next picture resets the zoom",
              "scale(1)" in b.eval(f"{IMG}.style.transform"), b.eval(f"{IMG}.style.transform"))
        check("no uncaught exception after zooming", b.errors == [], b.errors)

        print("2f. Escape closes the overlay, same as Peek's")
        b.key("Escape")
        check("the overlay closes", b.wait_for("!" + OPEN))
        check("the page behind can scroll again",
              b.eval("getComputedStyle(document.body).overflow") != "hidden")
        check("still no errors after the whole Review round trip", b.errors == [], b.errors)
        shot(b, "floor-needs-strip.png")

        print("2g. card #106: the Ship desk names which release each lane on the box is on")
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
        check("...added ABOVE the earlier lines, which are still there, once each — card "
              "#165: newest first, so the line that just arrived is the one on top",
              b.eval(PANEL_TEXT).split("\n") == ["Opening the pull request now.",
                                                 "$ Run the spawner suite",
                                                 "Reading the card before touching anything."], b.eval(PANEL_TEXT))
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

        print("6a. card #165: a working desk flags a tool call that has sat open past the "
              "grace, and Peek's own top line is that same call")
        # October 15, 2026's own incident: the desk said "Building, 106 min" the whole time
        # a tick sat mid-`sleep`, re-running the replay suite — alpha's own open call here
        # plays that back, old enough (well past WAITING_GRACE_MIN) that the test cannot
        # flake on how long the browser itself takes to load.
        with open(LIVE, "a") as f:
            f.write(open_call("Rerunning the replay suite's own canary case", 6))
        check("the page redrawing its desks shows a Waiting line naming the open call",
              b.eval("refresh().then(() => true)")
              and b.wait_for("document.querySelector('[data-peek-lane=\"alpha\"]').closest('.desk')"
                             ".querySelector('.waiting')?.textContent.includes("
                             "\"Rerunning the replay suite's own canary case\")"),
              b.eval("document.querySelector('[data-peek-lane=\"alpha\"]').closest('.desk')"
                     ".querySelector('.waiting')?.textContent"))
        shot(b, "waiting-desktop.png", full_page=False)
        b.click(peek_button("alpha"))
        check("the panel opens", b.wait_for(PANEL_OPEN))
        check("Peek's first line is the current step — the newest entry, pinned at the top, "
              "the same words the desk's own Waiting line just showed",
              b.wait_for(PANEL_TEXT + ".split('\\n')[0].includes("
                        "\"Rerunning the replay suite's own canary case\")"),
              b.eval(PANEL_TEXT))
        check("the session's earlier lines still follow, oldest last",
              b.eval(PANEL_TEXT).split("\n")[-1] == "Reading the card before touching anything.",
              b.eval(PANEL_TEXT))
        b.key("Escape")
        check("still no errors after all of that", b.errors == [], b.errors)

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

        print("7a. card #164: Peek on a lane running the canary release, whose session is under")
        print("    the canary config dir's projects/ and nowhere under the default one")
        # October 15, 2026: watcher@watcher on #155, `release` saying watcher-canary, the
        # claude process alive and its transcript growing under ~/.claude-canary — and Peek
        # saying "waiting" for the whole tick. alpha plays that lane for this one case.
        transcript(f"{ROOT}/alpha-worktrees", 8,
                   [say("Soaking on the canary: reading card eight."), run("Run the tail suite")],
                   under=CANARY)
        with open(os.path.join(STATE, "alpha", "current"), "w") as f:
            f.write("8\n")
        with open(os.path.join(STATE, "alpha", "release"), "w") as f:
            f.write("watcher-canary")
        b.click(peek_button("alpha"))
        check("the panel opens on the canary lane's live session, decoded, newest first",
              b.wait_for(PANEL_TEXT + ".includes('$ Run the tail suite')")
              and b.eval(PANEL_TEXT).split("\n") == ["$ Run the tail suite",
                                                     "Soaking on the canary: reading card eight."], b.eval(PANEL_TEXT))
        check("...and says it is live, on that card — not 'waiting for its session'",
              b.eval(PANEL_SUB) == "working on #8 — live", b.eval(PANEL_SUB))
        check("no uncaught exception", b.errors == [], b.errors)
        shot(b, "peek-canary-desktop.png", full_page=False)
        b.key("Escape")
        b.wait_for("!" + PANEL_OPEN, timeout=3)
        with open(os.path.join(STATE, "alpha", "current"), "w") as f:
            f.write("7\n")
        with open(os.path.join(STATE, "alpha", "release"), "w") as f:
            f.write("aaaaaaaaaaaa")

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

        print("7d. card #105: a deploy mid-flight — which step, how fresh, who it's waiting on, lands by")
        SHIP_DEPLOY.update({
            "state": "checking",
            "step": {"number": 4, "total": 5, "name": "Soak on the canary lane(s)",
                      "detail": "soaking: 1 of 3 clean tick(s) so far"},
            "alive": {"running": False, "since_sec": 120, "next_in_sec": 180, "stale": False},
            "waiting_for": [{"project": "watcher", "lane": "watcher",
                              "card": {"number": 42, "url": "https://github.com/roloc/watcher/issues/42"},
                              "launched_at": "2026-10-09T21:18:00+00:00",
                              "deadline_at": "2026-10-09T23:48:00+00:00"}],
            "lands_by": "2026-10-09T23:53:00+00:00",
        })
        b.click('.peek-btn[data-peek-ship="alpha"]')
        check("the step shows, plain words, out of how many",
              b.wait_for(PANEL_DEPLOY + ".includes('Step 4 of 5: Soak on the canary lane(s)')"))
        check("...with its detail underneath", "1 of 3 clean tick" in b.eval(PANEL_DEPLOY))
        check("the freshness line (not running, between runs)",
              "Checked 2m ago, checks again in 3m" in b.eval(PANEL_DEPLOY), b.eval(PANEL_DEPLOY))
        check("the lane it's waiting on is a real, one-tap link to its card — not plain text",
              b.eval("document.querySelector('#peek-deploy a[href=\"https://github.com/roloc/watcher/issues/42\"]')"
                     ".textContent") == "#42")
        check("...named by its project and lane, with its watchdog deadline",
              "watcher/watcher" in b.eval(PANEL_DEPLOY) and "watchdog ends it" in b.eval(PANEL_DEPLOY))
        check("the lands-by time shows, at the latest",
              "Lands by" in b.eval(PANEL_DEPLOY) and "at the latest" in b.eval(PANEL_DEPLOY))
        check("the link opens a new tab, not the panel's own navigation",
              b.eval("document.querySelector('#peek-deploy a').target") == "_blank")
        shot(b, "ship-peek-deploying.png", full_page=False)
        b.resize(390, 844)
        check("the same panel fits a phone, step and waiting-on-#42 link both readable",
              b.eval("""(() => { const r = document.getElementById('peek-panel').getBoundingClientRect();
                      return r.left >= 0 && r.right <= window.innerWidth; })()"""))
        shot(b, "ship-peek-deploying-phone.png", full_page=False)
        b.resize(1280, 900)
        b.key("Escape")
        b.click(peek_button("alpha"))
        check("opening a lane's own Peek next clears the deploy detail — it never shows there",
              b.wait_for(PANEL_TEXT + ".length > 0") and b.eval(PANEL_DEPLOY) == "")
        b.key("Escape")
        SHIP_DEPLOY.clear()
        SHIP_DEPLOY.update({"state": "live", "live": "aaaaaaa", "target": "aaaaaaa", "alive": None,
                            "step": None, "waiting_for": [], "lands_by": None})
        check("still no errors", b.errors == [], b.errors)

        print("8. on a phone: nothing runs off the side, and Peek still works")
        b.resize(390, 844)
        b.goto(URL)
        b.wait_for(DESKS_DRAWN)
        check("the page is no wider than the screen",
              b.eval("document.documentElement.scrollWidth <= window.innerWidth"),
              b.eval("[document.documentElement.scrollWidth, window.innerWidth]"))
        check("the Review button sits on the strip within the screen's width",
              b.eval("""(() => { const r = document.querySelector('#projects [data-review="778"]').getBoundingClientRect();
                      return r.right <= window.innerWidth + 1; })()"""))
        shot(b, "floor-phone.png")
        b.click('#projects [data-review="778"]')
        check("Review opens full-window on a phone too, the picture filling the width "
              "and the flip arrows thumb-sized",
              b.wait_for("document.getElementById('review-overlay').classList.contains('open')"))
        check("the picture fills the phone's width",
              b.eval("document.getElementById('review-img').getBoundingClientRect().width")
              >= b.eval("window.innerWidth") * 0.85)
        check("the flip arrows are big enough for a thumb",
              b.eval("""(() => { const r = document.getElementById('review-next').getBoundingClientRect();
                      return r.width >= 40 && r.height >= 40; })()"""))
        shot(b, "review-overlay-phone.png", full_page=False)
        b.key("Escape")
        check("it closes, same as on a desktop",
              b.wait_for("!document.getElementById('review-overlay').classList.contains('open')"))
        b.click(peek_button("alpha"))
        check("Peek opens and fills at phone width",
              b.wait_for(PANEL_TEXT + ".includes('Opening the pull request now.')"))
        check("the panel fits the screen",
              b.eval("""(() => { const r = document.getElementById('peek-panel').getBoundingClientRect();
                      return r.left >= 0 && r.right <= window.innerWidth && r.bottom <= window.innerHeight; })()"""))
        shot(b, "peek-phone.png", full_page=False)  # the panel is fixed to the screen
        b.key("Escape")

        # card #164: the picture that matters for this card is the canary state itself,
        # not just an ordinary live one — Steve opens the floor page from his phone, so
        # that is the width the canary-only session has to be shown decoded at too.
        transcript(f"{ROOT}/alpha-worktrees", 8,
                   [say("Soaking on the canary: reading card eight."), run("Run the tail suite")],
                   under=CANARY)
        with open(os.path.join(STATE, "alpha", "current"), "w") as f:
            f.write("8\n")
        with open(os.path.join(STATE, "alpha", "release"), "w") as f:
            f.write("watcher-canary")
        b.click(peek_button("alpha"))
        check("at phone width too, a session only under the canary config dir is found and decoded",
              b.wait_for(PANEL_TEXT + ".includes('Soaking on the canary: reading card eight.')"),
              b.eval(PANEL_TEXT))
        check("...and labeled live, the same as any other working lane, at this width",
              b.eval(PANEL_SUB) == "working on #8 — live", b.eval(PANEL_SUB))
        shot(b, "peek-canary-phone.png", full_page=False)
        b.key("Escape")
        with open(os.path.join(STATE, "alpha", "current"), "w") as f:
            f.write("7\n")
        with open(os.path.join(STATE, "alpha", "release"), "w") as f:
            f.write("aaaaaaaaaaaa")

        b.click('.peek-btn[data-peek-ship="alpha"]')
        check("the ship station's Peek fits the phone too",
              b.wait_for(PANEL_TEXT + ".includes('The spawner suite')")
              and b.eval("""(() => { const r = document.getElementById('peek-panel').getBoundingClientRect();
                      return r.left >= 0 && r.right <= window.innerWidth && r.bottom <= window.innerHeight; })()"""))
        shot(b, "ship-peek-phone.png", full_page=False)
        b.key("Escape")

    print("9. this suite fails on the page #70 shipped (card #71): a script that does not parse")
    good = floor.INDEX_HTML
    marker = 'newest + "\\n" + pre.textContent'
    check("the line #70 broke is still where this case expects it", good.count(marker) == 1)
    floor.INDEX_HTML = good.replace(marker, 'newest + "\n" + pre.textContent')
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
    MODAL_OPEN = "document.getElementById('office-modal').classList.contains('open')"
    MODAL_TEXT = "document.getElementById('office-body').textContent"
    TIP_OPEN = "document.getElementById('server-tip').classList.contains('open')"

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

    def positions(b):
        return b.eval("Object.fromEntries([...document.querySelectorAll('.character')]"
                      ".map(e => [e.dataset.lane || ('ship:' + e.dataset.ship), e.style.left + ' ' + e.style.top]))")

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

        print("12. an office: a break room, Steve's office, a room per board, the server room")
        rooms = b.eval(ROOM_NAMES)
        check("the break room and Steve's office, one room per board in the board's order, the server room last",
              rooms == ["Break room", "Steve's office", "alpha", "beta", "Server room"], rooms)
        check("a board's room has its name on the door, a link to the board in a new tab",
              b.eval("[...document.querySelectorAll('.office-room.team .room-name')]"
                     ".every(a => a.href && a.target === '_blank')"))
        check("the board's counts are on the room's whiteboard",
              b.eval("document.querySelector('.office-room[data-room=\"alpha\"] .whiteboard').textContent")
              == "Ready 0 · Doing 1 · Backlog 0")
        check("the rooms sit in two rows with the main hallway between them, every room on tiled floor",
              b.eval("""(() => { const br = document.querySelector('.office-room.break').getBoundingClientRect();
                      const t = document.querySelector('.office-room.team').getBoundingClientRect();
                      const s = document.querySelector('.office-room.server').getBoundingClientRect();
                      return t.top - br.bottom >= 50 && Math.abs(s.top - t.top) < 2 && s.left > t.right; })()"""))
        check("alpha (working) is at its desk in alpha's room, its plate saying what it is",
              info(b, "alpha") == ["working", "alpha", "Build"], info(b, "alpha"))
        check("...and the desk is drawn under it, lit, with its screen scanning",
              b.eval("getComputedStyle(document.querySelector('.desk-set[data-lane=\"alpha\"] .scanline')).opacity") != "0")
        check("beta (paused) is at its own desk in beta's room, hands up, not off anywhere",
              info(b, "beta") == ["paused", "beta", "Build"], info(b, "beta"))
        check("...and that desk's screen is dark",
              b.eval("getComputedStyle(document.querySelector('.desk-set[data-lane=\"beta\"] .scanline')).opacity") == "0")
        check("alpha-design (idle) has gone to the break room, mug in hand, its plate saying whose it is",
              info(b, "alpha-design") == ["idle", "Break room", "Design\nalpha"], info(b, "alpha-design"))
        check("alpha-triage (idle) is in the break room too",
              info(b, "alpha-triage")[:2] == ["idle", "Break room"], info(b, "alpha-triage"))
        check("the mug is drawn (the pose says idle, not just the dark screen)",
              b.eval("document.querySelector('.character[data-lane=\"alpha-design\"] svg').innerHTML.includes('f3f4f6')"))
        check("their desks are still in alpha's room, each tagged as theirs and out — and so "
              "is alpha's ship shelf, since its own CI has nothing to show right now and its "
              "dock hand has gone on break the same way (card #126)",
              b.eval("[...document.querySelectorAll('.desk-tag')]"
                     ".map(t => (t.dataset.lane || ('ship:' + t.dataset.ship)) + ':' + t.textContent).sort()")
              == ["alpha-design:Design · out", "alpha-triage:Triage · out", "ship:alpha:Ship · out"])
        check("the break room has a couch, a coffee machine (steaming), windows and plants",
              b.eval("""(() => { const r = document.querySelector('.office-room.break');
                      return !!r.querySelector('.couch') && !!r.querySelector('.prop .steam')
                          && r.querySelectorAll('.prop').length >= 7; })()"""))
        check("every room has a window and a plant (October 9, 2026: 'lean into that')",
              b.eval("""[...document.querySelectorAll('.office-room:not(.server)')].every(r =>
                      [...r.querySelectorAll('.prop svg')].length >= 2)"""))
        check("Steve's desk has his name on it, and his two boards hang on the wall", b.eval(
              "document.querySelector('.office-room.boss').textContent.includes('STEVE')"
              " && !!document.querySelector('.office-room.boss [data-board=\"bulletin\"]')"
              " && !!document.querySelector('.office-room.boss [data-board=\"wins\"]')"))
        check("the server room has racks with lights on them",
              b.eval("document.querySelectorAll('.office-room.server .led').length") >= 6)
        check("nobody stands on anybody", overlaps(b) == [], overlaps(b))

        print("12a. the in-tray on Steve's desk (card #96): #778 is there, named for alpha, "
              "even though no lane is in his office for it")
        check("#778's chip is in the in-tray, after its board's name",
              b.eval("document.querySelector('.row-stuck-chip')?.textContent") == "#778"
              and b.eval("document.querySelector('.tray-board')?.textContent") == "alpha")
        check("...and the tray's own Review button opens the same overlay (card #100), "
              "not an inline sliver",
              b.eval("document.querySelector('#room-stuck .car-img')") is None
              and b.eval("document.querySelector('#room-stuck [data-review=\"778\"]')") is not None)
        b.click('#room-stuck [data-review="778"]')
        check("tapping it opens the full-window overlay with #778's first picture",
              b.wait_for("document.getElementById('review-overlay').classList.contains('open')")
              and b.eval("document.getElementById('review-caption').textContent") == "A ★ — phone")
        b.key("Escape")
        check("the tray is in Steve's office, in front of his desk, and the office grew to hold it",
              b.eval("""(() => { const t = document.querySelector('.in-tray').getBoundingClientRect();
                      const o = document.querySelector('.office-room.boss').getBoundingClientRect();
                      const d = [...document.querySelectorAll('.office-room.boss .prop')].find(p => p.textContent.includes('STEVE')).getBoundingClientRect();
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
        check("three team rooms, named for their boards, side by side with hallways between",
              rooms == ["Break room", "Steve's office", "riffmap", "riffmap-site", "watcher", "Server room"]
              and b.eval("""(() => { const rs = [...document.querySelectorAll('.office-room.team')].map(r => r.getBoundingClientRect());
                      return rs[1].left - rs[0].right >= 24 && rs[2].left - rs[1].right >= 24 && Math.abs(rs[0].top - rs[2].top) < 2; })()"""),
              rooms)
        check("all six idle lanes are in the break room, each plate naming its board",
              b.eval("""[...document.querySelectorAll('.character[data-pill="idle"]')]
                     .every(e => e.dataset.room === 'Break room' && e.querySelector('.where').textContent === e.dataset.project)"""))
        check("the ship station is a dock hand with a hand truck of crates in front of a shelf — not the "
              "floor's desk sprite shrunk, which read as a person carrying a desk around (October 9, 2026)",
              b.eval("""['riffmap', 'riffmap-site', 'watcher'].every(n => {
                      const c = document.querySelector('.character[data-ship="' + n + '"] svg');
                      return c && !c.innerHTML.includes('desk-top') && c.innerHTML.includes('#ffd33d')
                          && !!document.querySelector('.desk-set.shelf[data-ship="' + n + '"]'); })"""))
        check("a ship's shelf still stands in its own board's room, even once its dock hand leaves it",
              b.eval("""['riffmap', 'riffmap-site', 'watcher'].every(n => {
                      const s = document.querySelector('.desk-set.shelf[data-ship="' + n + '"]').getBoundingClientRect();
                      const r = document.querySelector('.office-room[data-room="' + n + '"]').getBoundingClientRect();
                      return s.left >= r.left && s.right <= r.right && s.bottom <= r.bottom + 1 && s.top >= r.top; })"""))
        check("CI is green and nothing is shipping on any of the three, so every dock hand has gone "
              "on break instead of standing at an empty shelf all day (card #126)",
              b.eval("""['riffmap', 'riffmap-site', 'watcher'].every(n => {
                      const s = document.querySelector('.character[data-ship="' + n + '"]').getBoundingClientRect();
                      const r = document.querySelector('.office-room[data-room="Break room"]').getBoundingClientRect();
                      return s.left >= r.left && s.right <= r.right && s.bottom <= r.bottom + 1 && s.top >= r.top; })"""))
        check("...and its shelf is tagged as out, the same way an idle lane's desk is",
              b.eval("""['riffmap', 'riffmap-site', 'watcher'].every(n =>
                      document.querySelector('.desk-tag[data-ship="' + n + '"]')?.textContent === 'Ship · out')"""))
        check("a lane's plate is readable: no plate is covered by another character",
              b.eval("""(() => {
                const plates = [...document.querySelectorAll('.character .plate')].map(p => p.getBoundingClientRect());
                const chars = [...document.querySelectorAll('.character')].map(c => c.getBoundingClientRect());
                return plates.every(p => chars.filter(c => c.left < p.right && p.left < c.right
                                                            && c.top < p.bottom && p.top < c.bottom).length === 1);
              })()"""))
        shot(b, "studio-live-shaped.png")

        print("12c. the break room wanders: someone gets up and walks to another spot, nobody else moves")
        before = positions(b)
        b.eval("window.__studioWander(); true")
        settled(b)
        after = positions(b)
        moved = sorted(k for k in before if before[k] != after[k])
        check("exactly one occupant moved", len(moved) == 1, moved)
        # `positions()` keys a ship "ship:" + its name (never a real lane's own key), so a
        # wander that happens to land on one — it shares the break room's seats with every
        # idle lane now — is told apart the same way, not assumed to always be a lane.
        mover = moved[0] if moved else ""
        mover_ship = mover[len("ship:"):] if mover.startswith("ship:") else None
        check("...and it was idle (a lane with nothing to pick up, or a ship with nothing to watch)",
              bool(moved) and (
                  b.eval(f'document.querySelector(\'.character[data-ship="{mover_ship}"]\')') is not None
                  if mover_ship else
                  b.eval(f'document.querySelector(\'.character[data-lane="{mover}"]\').dataset.pill') == "idle"),
              mover)
        check("...to another spot in the break room",
              (b.eval(f"""(() => {{ const e = document.querySelector('.character[data-ship=\"{mover_ship}\"]');
                      const r = e.getBoundingClientRect(), br = document.querySelector('.office-room.break').getBoundingClientRect();
                      return r.left >= br.left - 1 && r.right <= br.right + 1 && r.bottom > br.top && r.bottom <= br.bottom + 1; }})()""")
               if mover_ship else info(b, mover)[1] == "Break room"),
              mover)
        check("and still nobody stands on anybody", overlaps(b) == [], overlaps(b))
        check("a working lane never wanders (riffmap-site-design is where it was)",
              before["riffmap-site-design"] == after["riffmap-site-design"])

        print("12cc. cookie time: click the vending machine and everyone in the break room runs over for a cookie")
        b.eval("render(" + LIVE_SHAPED + "); renderStudio(" + LIVE_SHAPED + "); window.__cookiePartyMs = 6000; true")
        settled(b)
        seats = b.eval("Object.fromEntries([...document.querySelectorAll('.character[data-room=\"Break room\"]')].map(e => [e.dataset.lane, e.style.left + ' ' + e.style.top]))")
        b.click(".office-room.break [data-cookies]")
        check("cookies spew out of the machine onto the floor, and the machine shouts",
              b.wait_for("document.querySelectorAll('#room-cookies .cookie').length === 12")
              and b.eval("document.querySelector('.cookie-shout')?.textContent") == "Cookies!")
        settled(b)
        check("every one of the six idle lanes is now eating: cookie in hand, nibbling, in a crowd by the machine",
              b.eval("""(() => { const m = document.querySelector('.office-room.break [data-cookies]').getBoundingClientRect();
                      const es = [...document.querySelectorAll('.character[data-room="Break room"]')];
                      return es.length === 6 && es.every(e => e.classList.contains('eating') && e.querySelector('svg').innerHTML.includes('#c9915a')
                          && Math.abs((e.getBoundingClientRect().left + e.getBoundingClientRect().right) / 2 - (m.left + m.right) / 2) < 200); })()"""))
        check("it is a huddle, not the seat row: the six stand in two rows, and the three who sat far from the machine walked over",
              b.eval("""(() => { const ys = new Set([...document.querySelectorAll('.character[data-room="Break room"]')].map(e => Math.round(e.getBoundingClientRect().bottom)));
                      return ys.size === 2; })()""")
              and b.eval("Object.entries(" + json.dumps(seats) + ").filter(([k, v]) => document.querySelector('.character[data-lane=\"' + k + '\"]').style.left + ' ' + document.querySelector('.character[data-lane=\"' + k + '\"]').style.top !== v).length") >= 5)
        check("a crowd, but nobody stands on anybody and every plate is readable (review of #119)",
              overlaps(b) == [], overlaps(b))
        check("the cookies landed on the break room floor, none outside the room",
              b.eval("""(() => { const r = document.querySelector('.office-room.break').getBoundingClientRect();
                      return [...document.querySelectorAll('.cookie')].every(c => { const q = c.getBoundingClientRect();
                        return q.left >= r.left && q.right <= r.right && q.top >= r.top && q.bottom <= r.bottom; }); })()"""))
        check("the working lane at its desk did not come (it is not in the break room)",
              not b.eval("document.querySelector('.character[data-lane=\"riffmap-site-design\"]').classList.contains('eating')"))
        shot(b, "studio-cookies.png")
        check("a redraw during the party keeps everyone at it",
              b.eval("renderStudio(lastState); [...document.querySelectorAll('.character[data-room=\"Break room\"]')].every(e => e.classList.contains('eating'))"))
        check("when the party ends the cookies are gone and everyone is back in their seat, mug in hand",
              b.wait_for("document.querySelectorAll('#room-cookies .cookie').length === 0", timeout=12)
              and settled(b)
              and b.eval("Object.fromEntries([...document.querySelectorAll('.character[data-room=\"Break room\"]')].map(e => [e.dataset.lane, e.style.left + ' ' + e.style.top]))") == seats
              and b.eval("[...document.querySelectorAll('.character[data-room=\"Break room\"]')].every(e => !e.classList.contains('eating'))"))
        check("no errors from the party", b.errors == [], b.errors)

        print("12d. a lane walks the hallways between rooms, never through a wall")
        # riffmap-site-design was working at its desk; now it is idle, so it leaves for the
        # break room: out of its door, along the main hallway, in at the break room's door.
        walked = b.eval("""(() => {
          const st = """ + LIVE_SHAPED + """;
          const l = st.projects[1].lanes[1]; l.pill = 'idle'; delete l.card; l.reason = 'Nothing to pick up right now.';
          render(st); renderStudio(st);
          const e = document.querySelector('.character[data-lane="riffmap-site-design"]');
          const hall = (() => { const br = document.querySelector('.office-room.break').getBoundingClientRect();
                                const t = document.querySelector('.office-room.team').getBoundingClientRect();
                                return [br.bottom, t.top]; })();
          const samples = [];
          return new Promise(res => {
            const t0 = performance.now();
            const tick = () => {
              const r = e.getBoundingClientRect();
              samples.push([r.left + r.width / 2, r.bottom - e.querySelector('.plate').getBoundingClientRect().height]);
              if (e.getAnimations().some(a => a.playState === 'running') && performance.now() - t0 < 20000) requestAnimationFrame(tick);
              else res({samples, hall, walking: e.classList.contains('walking')});
            };
            tick();
          });
        })()""")
        in_hall = [s for s in walked["samples"] if walked["hall"][0] <= s[1] <= walked["hall"][1]]
        check("the walk was animated over many frames, not a jump cut", len(walked["samples"]) > 20, len(walked["samples"]))
        check("on its way it was in the main hallway (feet between the top rooms and the team rooms)",
              len(in_hall) > 5, (len(in_hall), walked["hall"], walked["samples"][:3]))
        check("...and crossed most of the hallway's width, door to door",
              (max(s[0] for s in in_hall) - min(s[0] for s in in_hall)) > 120 if in_hall else False)
        check("the bob stops when the walk ends", not walked["walking"])
        check("it arrived in the break room", info(b, "riffmap-site-design") == ["idle", "Break room", "Design\nriffmap-site"],
              info(b, "riffmap-site-design"))
        check("still nobody stands on anybody", overlaps(b) == [], overlaps(b))
        b.eval("refresh().then(() => true)")
        settled(b)
        check("every lane keeps the same look across a redraw (the hash never changes)",
              b.eval("document.querySelector(\".character[data-lane='alpha']\").querySelector('svg').outerHTML")
              == b.eval("refresh().then(() => document.querySelector(\".character[data-lane='alpha']\")"
                        ".querySelector('svg').outerHTML)"))

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
        check("the bubble opens with the same last (newest) line Peek shows — section 6a's "
              "open call is the newest one on file now",
              b.wait_for("document.querySelector('.bubble')?.textContent.includes("
                        "\"Rerunning the replay suite's own canary case\")"))
        b.click(".bubble")
        check("tapping it opens the full Peek panel",
              b.wait_for(PANEL_OPEN)
              and "Build (alpha)" in b.eval("document.getElementById('peek-title').textContent"))
        check("the bubble itself is gone once the full panel is up",
              b.eval("document.querySelector('.bubble')") is None)
        shot(b, "studio-bubble-to-peek.png", full_page=False)
        b.key("Escape")
        check("still no errors after the bubble round trip", b.errors == [], b.errors)

        print("15. the server room: hover it for the box's load, the tokens the lanes moved, and the lab")
        b.hover(".office-room.server")
        check("the readout opens on hover", b.wait_for(TIP_OPEN))
        tip = b.eval("document.getElementById('server-tip').innerText")
        check("it names this box and its load, memory, disk and uptime",
              "This box · test-box" in tip and "0.42 on 4 cpus" in tip and "2.7 GB of 8.0 GB" in tip
              and "31.5 GB of 46.9 GB" in tip and "32d 5h" in tip, tip)
        check("the tokens the lanes moved in the last day, in all and per board (two fixture ticks: 1,000 each)",
              "Tokens, last 24h" in tip and "All lanes\n2k" in tip.replace("\t", "\n").replace("  ", "\n")
              or "2k · ~83/h" in tip, tip)
        check("the lab's boxes from Proxmox, with the stopped one marked",
              "agents · lxc 107" in tip and "cpu 1%" in tip and "ollama · lxc 101" in tip and "stopped" in tip, tip)
        check("the readout sits beside the room, inside the office",
              b.eval("""(() => { const t = document.getElementById('server-tip').getBoundingClientRect();
                      const s = document.querySelector('.office-room.server').getBoundingClientRect();
                      const room = document.getElementById('room').getBoundingClientRect();
                      return t.right <= s.left && t.left >= room.left && t.top >= room.top; })()"""))
        check("...and nothing of it is cut off: its last line is drawn in full",
              b.eval("""(() => { const t = document.getElementById('server-tip');
                      const last = t.lastElementChild.getBoundingClientRect(), tr = t.getBoundingClientRect();
                      return last.bottom <= tr.bottom && t.scrollHeight <= t.clientHeight + 1; })()"""))
        shot(b, "studio-server-room.png")
        b.hover(".office-room.break")
        check("and closes when the pointer leaves", b.wait_for("!" + TIP_OPEN))

        print("16. the bulletin board on Steve's wall: click it for every card being worked and recently worked")
        b.click('.office-room.boss [data-board="bulletin"]')
        check("the popup opens", b.wait_for(MODAL_OPEN))
        text = b.eval(MODAL_TEXT)
        check("alpha's #7 is under 'being worked right now', with its stage",
              "Being worked right now" in text and "#7" in text and "alpha · Build · Building" in text, text)
        check("the two recent ticks are under 'recently worked', newest first",
              "Recently worked" in text and text.index("Recently worked") < text.index("#5") and "#3" in text, text)
        shot(b, "studio-bulletin.png", full_page=False)
        b.click('#office-body [data-card-number="7"]')
        check("Peek on a card reads the card itself off the board: its title, body and labels",
              b.wait_for("document.getElementById('office-title').textContent.includes('Build the thing')")
              and "A card body, straight from GitHub." in b.eval(MODAL_TEXT)
              and "agent: builder" in b.eval(MODAL_TEXT), b.eval(MODAL_TEXT))
        check("...and its last comment", "Looks good, ship it." in b.eval(MODAL_TEXT))
        shot(b, "studio-card-peek.png", full_page=False)
        b.click("#office-back")
        check("Back returns to the board's list", b.wait_for(MODAL_TEXT + ".includes('Being worked right now')"))
        b.click('#office-body [data-card-number="3"]')
        check("a card the board cannot read says so, never a blank or a crash",
              b.wait_for(MODAL_TEXT + ".includes('could not be read')"))
        b.key("Escape")
        check("Escape closes the popup", b.wait_for("!" + MODAL_OPEN))

        print("16b. the wins board: what shipped this week, by board")
        b.click('.office-room.boss [data-board="wins"]')
        check("the popup opens", b.wait_for(MODAL_OPEN))
        text = b.eval(MODAL_TEXT)
        check("the two cards that ended in Done this week are there, alpha's #5 with its title from the board",
              "2 shipped this week" in text and "Mockup for the earlier thing" in text and "#3" in text, text)
        check("the wins board on the wall carries the same count", b.eval(
              "document.querySelector('.office-room.boss [data-board=\"wins\"]').textContent.includes('WINS · 2')"))
        shot(b, "studio-wins.png", full_page=False)
        b.click("#office-close")
        check("the × closes it", b.wait_for("!" + MODAL_OPEN))
        check("still no errors after the boards", b.errors == [], b.errors)

        print("17. Floor goes back, and the floor page is exactly as it was")
        b.click("#floor-btn")
        check("the studio closes and the floor's sections return",
              b.wait_for("!" + STUDIO_OPEN)
              and b.eval("getComputedStyle(document.getElementById('floor-view')).display") != "none")
        check("the Studio button is back", b.eval(
              "getComputedStyle(document.getElementById('studio-btn')).display") != "none")
        check("the floor's own desks are still there, same as section 2", b.eval(CHAR_COUNT) >= 0
              and b.eval("document.querySelectorAll('.desk').length") == 6)

    print("17b. the office is drawn for a desk's browser: on a phone it keeps its width and pans")
    with browse.Browser(width=390, height=844) as b:
        b.goto(URL)
        b.wait_for(DESKS_DRAWN)
        check("the floor view itself still fits a phone",
              b.eval("document.documentElement.scrollWidth <= window.innerWidth"))
        b.click("#studio-btn")
        b.wait_for(STUDIO_OPEN)
        settled(b)
        check("the office is at least 960 wide and nothing in it is squeezed",
              b.eval("document.getElementById('room').getBoundingClientRect().width") >= 960
              and overlaps(b) == [], overlaps(b))

    with browse.Browser(width=1280, height=900) as b:
        b.reduce_motion(True)
        b.goto(URL)
        b.wait_for(DESKS_DRAWN)
        b.click("#studio-btn")
        b.wait_for(STUDIO_OPEN)
        b.eval("clearInterval(window.__studioWanderTimer); true")
        before = positions(b)
        b.eval("window.__studioWander(); true")
        check("prefers-reduced-motion: reduce makes a walk a jump cut — nobody animates, someone moved",
              b.eval("[...document.querySelectorAll('.character')].every(e => e.getAnimations().length === 0)")
              and positions(b) != before)

    print("18. demo mode (?demo=1): cycles the fixture through every state, for designing the office")
    with browse.Browser(width=1280, height=900) as b:
        b.goto(URL + "?demo=1")
        b.wait_for(CHAR_COUNT + " > 0")
        check("no uncaught exception even with no real board behind it", b.errors == [], b.errors)
        b.click("#studio-btn")
        b.wait_for(STUDIO_OPEN)
        b.eval("clearInterval(window.__demoTimer); clearInterval(window.__studioWanderTimer); true")
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
        check("card #154: the desk's own pill names the configured login, not the fixed "
              "word 'Steve' — this fixture's own OWNER_LOGIN is 'octocat'",
              b.eval("document.querySelector('.desk .pill.needs_steve')?.textContent") == "Needs @octocat")
        HELD_ALT = "document.querySelector('.character[data-pill=\"needs_steve\"] .character-mockup img')?.alt"
        check("it starts on direction A's first shot",
              b.eval(HELD_ALT) == "phone light", b.eval(HELD_ALT))
        b.eval("stepCarousel(11, 1)")
        check("stepping the shared carousel (card #96) moves what the visitor holds too, "
              "not just the floor's own strip", b.eval(HELD_ALT) == "phone dark", b.eval(HELD_ALT))
        check("the podium figure keeps holding the picture, and it is tappable (card #100)",
              b.eval("!!document.querySelector('.character-mockup[data-review]')"))
        b.click(".character-mockup[data-review]")
        check("tapping the held mockup opens the same overlay, not the speech bubble",
              b.wait_for("document.getElementById('review-overlay').classList.contains('open')")
              and b.eval("document.querySelector('.bubble')") is None)
        b.key("Escape")
        settled(b)
        check("it walked to Steve's office, and its plate says whose it is",
              info(b, "alpha") == ["needs_steve", "Steve's office", "Build\nalpha"], info(b, "alpha"))
        check("the light over his door is red while it waits there",
              b.eval("document.getElementById('beacon').className") == "beacon-red")
        check("alpha's build is failing in this state, so its dock hand waves a flag; beta's is green, a check on the crates",
              b.eval("!!document.querySelector('.character[data-ship=\"alpha\"] .character-wave')")
              and b.eval("document.querySelector('.character[data-ship=\"beta\"] svg').innerHTML.includes('l5 -5')"))
        b.eval("window.__demoGoto(2)")  # alpha's checks running: dots on the crates
        check("...and while checks run the crates blink dots",
              b.eval("document.querySelectorAll('.character[data-ship=\"alpha\"] .dot').length") == 3)
        b.eval("window.__demoGoto(1)")
        settled(b)
        check("the sleeping lane naps on the break room couch, named for its board too",
              info(b, "beta") == ["sleeping", "Break room", "Build\nbeta"]
              and b.eval("""(() => { const e = document.querySelector('.character[data-pill="sleeping"]').getBoundingClientRect();
                      const c = document.querySelector('.couch').getBoundingClientRect();
                      const x = (e.left + e.right) / 2; return x >= c.left && x <= c.right; })()"""),
              info(b, "beta"))
        check("the demo's server room and wins board are populated too",
              b.eval("document.getElementById('server-tip').innerText").count("lxc") >= 3
              and b.eval("document.querySelector('[data-board=\"wins\"]').textContent.includes('WINS · 3')"))
        shot(b, "studio-demo.png")
        b.click("#floor-btn")
        b.wait_for("!" + STUDIO_OPEN)
        check("the floor's own desk shows the same mockup inline too, not just a link to the card",
              b.eval("!!document.querySelector('.desk .mockup-thumb')"))
        check("...and the design lane's own desk tile also carries the Review button (card #100)",
              b.eval("!!document.querySelector('.desk [data-review]')"))
        b.click(".desk [data-review]")
        check("tapping it from the desk opens the overlay too",
              b.wait_for("document.getElementById('review-overlay').classList.contains('open')"))
        b.key("Escape")
        shot(b, "floor-demo-mockup.png")
        check("still no errors after cycling demo states", b.errors == [], b.errors)

        print("19. card #103: a working desk past 10 minutes with no plan comment reads amber, "
              "the same way Ship reads when something is blocked")
        b.eval(f"render({LATE_STATE}); renderStudio({LATE_STATE}); true")
        settled(b)
        check("the desk line says 'no plan yet'", "no plan yet" in b.text(), b.text())
        check("the Working pill itself carries the late class, not just the detail text",
              b.eval("document.querySelector('.desk .pill.working')?.className") == "pill working plan-late")
        check("the light over Steve's door goes amber from this alone — nothing else is "
              "paused, asleep or blocked", b.eval("document.getElementById('beacon').className") == "beacon-amber")
        check("no uncaught exception", b.errors == [], b.errors)
        shot(b, "floor-plan-late.png")

        b.eval(f"render({POSTED_STATE}); renderStudio({POSTED_STATE}); true")
        settled(b)
        check("a posted plan says so instead, past the same 17 minutes",
              "plan posted" in b.text() and "no plan yet" not in b.text(), b.text())
        check("the pill carries no late class", b.eval("document.querySelector('.desk .pill.working')?.className")
              == "pill working")
        check("...and the light goes back to green", b.eval("document.getElementById('beacon').className") == "beacon-green")
        check("no uncaught exception", b.errors == [], b.errors)
        shot(b, "floor-plan-posted.png")

    print("19b. card #103, on a phone: the amber pill and the words both still read at 390px")
    with browse.Browser(width=390, height=844) as b:
        b.goto(URL)
        b.wait_for(DESKS_DRAWN)
        b.eval(f"render({LATE_STATE}); true")
        check("still fits a phone, no horizontal scroll",
              b.eval("document.documentElement.scrollWidth <= window.innerWidth"))
        check("'no plan yet' reads at phone width too", "no plan yet" in b.text(), b.text())
        check("the pill is still amber at this width",
              b.eval("document.querySelector('.desk .pill.working')?.className") == "pill working plan-late")
        check("no uncaught exception", b.errors == [], b.errors)
        shot(b, "floor-plan-late-phone.png")

        b.eval(f"render({POSTED_STATE}); true")
        check("'plan posted' reads at phone width too", "plan posted" in b.text(), b.text())
        check("no uncaught exception", b.errors == [], b.errors)
        shot(b, "floor-plan-posted-phone.png")
except browse.BrowseError as e:
    print(e, file=sys.stderr)
    fails = 1
finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(ROOT, ignore_errors=True)

print(f"{count} cases, {'all passed' if not fails else 'FAILURES'}")
sys.exit(fails)
