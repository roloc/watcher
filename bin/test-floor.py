#!/usr/bin/env python3
"""Offline tests for bin/floor.py. No network, no real systemd, no real board — every case
builds its own fixture `projects/*.env` files and state directories under a throwaway
directory, and a stubbed board and `systemctl is-active`.

    python3 bin/test-floor.py      # exit 0 = all cases hold
"""
import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import floor  # noqa: E402
import metrics  # noqa: E402
import tail  # noqa: E402  (bin/tail.py — Peek's own decode, exercised directly below)

fails = 0
count = 0


def check(name, cond, detail=""):
    global fails, count
    count += 1
    print(f"  {name:<78} {'ok' if cond else 'FAIL ' + str(detail)}")
    if not cond:
        fails = 1


NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
ROOT = tempfile.mkdtemp(prefix="watcher-floor-test-")
PROJECTS = os.path.join(ROOT, "projects")
STATE = os.path.join(ROOT, "state")
os.makedirs(PROJECTS)
os.makedirs(STATE)


def write_env(name, body):
    with open(os.path.join(PROJECTS, f"{name}.env"), "w") as f:
        f.write(body)


# --- fixture projects, mirroring the repo's own shapes exactly, interpolation included ---

write_env("watcher", """\
PROJECT_NAME=watcher
PROJECT_ROOT=/x
BOARD_OWNER=roloc
PROJECT_REPO=roloc/watcher
BOARD_NUMBER=2
""")

write_env("riffmap", """\
PROJECT_NAME=riffmap
PROJECT_ROOT=/x
BOARD_OWNER=roloc
PROJECT_REPO=roloc/riffmap
BOARD_NUMBER=1
DESIGN_AGENTS=architect,designer
LANE_AGENTS="!${DESIGN_AGENTS},-"
""")

write_env("riffmap-design", """\
. "$(dirname "${BASH_SOURCE[0]}")/riffmap.env"
PROJECT_NAME=riffmap-design
LANE_AGENTS="${DESIGN_AGENTS}"
""")

write_env("riffmap-triage", """\
. "$(dirname "${BASH_SOURCE[0]}")/riffmap.env"
PROJECT_NAME=riffmap-triage
LANE_AGENTS="-"
LANE_COMMAND=/triage
""")

write_env("second", """\
PROJECT_NAME=second
PROJECT_ROOT=/x
BOARD_OWNER=roloc
PROJECT_REPO=roloc/second
BOARD_NUMBER=3
""")

write_env("third", """\
PROJECT_NAME=third
PROJECT_ROOT=/x
BOARD_OWNER=roloc
PROJECT_REPO=roloc/third
BOARD_NUMBER=4
""")

write_env("fourth", """\
PROJECT_NAME=fourth
PROJECT_ROOT=/x
BOARD_OWNER=roloc
PROJECT_REPO=roloc/fourth
BOARD_NUMBER=5
""")

# A file that is not a project file at all — must be silently skipped, not crash discovery.
write_env("scratch-notes", "SOME_VAR=hello\n")


print("1. discover_lanes reads projects/*.env as bash, interpolation and sourcing included")
lanes = floor.discover_lanes(PROJECTS, STATE)
by_name = {l["lane"]: l for l in lanes}
check("seven real lanes found, the stray file skipped",
      sorted(by_name) == ["fourth", "riffmap", "riffmap-design", "riffmap-triage",
                           "second", "third", "watcher"],
      str(sorted(by_name)))
check("riffmap-design's LANE_AGENTS is riffmap's own DESIGN_AGENTS, expanded",
      by_name["riffmap-design"]["lane_agents"] == "architect,designer",
      by_name["riffmap-design"]["lane_agents"])
check("riffmap's LANE_AGENTS keeps the interpolated ! form",
      by_name["riffmap"]["lane_agents"] == "!architect,designer,-",
      by_name["riffmap"]["lane_agents"])
check("riffmap-triage's PROJECT_REPO came from the sourced file, not left blank",
      by_name["riffmap-triage"]["project_repo"] == "roloc/riffmap")
check("watcher's LANE_AGENTS is empty (no LANE_AGENTS line): unset, not the string 'None'",
      by_name["watcher"]["lane_agents"] == "")

print("2. lane_kind reads LANE_COMMAND / LANE_AGENTS generically")
check("riffmap (negated list) is Build", floor.lane_kind(by_name["riffmap"]) == "Build")
check("riffmap-design (a plain list) is Design", floor.lane_kind(by_name["riffmap-design"]) == "Design")
check("riffmap-triage (/triage) is Triage", floor.lane_kind(by_name["riffmap-triage"]) == "Triage")
check("watcher (no LANE_AGENTS at all) is Build", floor.lane_kind(by_name["watcher"]) == "Build")

print("3. lane_takes matches spawn.sh's own rule, '-' as the no-label sentinel")
check("unset spec takes everything", floor.lane_takes("-", "") is True)
check("negated list takes an unlisted agent", floor.lane_takes("reviewer", "!architect,designer,-") is True)
check("negated list excludes a listed one", floor.lane_takes("architect", "!architect,designer,-") is False)
check("negated list excludes '-' (unlabeled goes to triage, not build)",
      floor.lane_takes("-", "!architect,designer,-") is False)
check("a plain list takes exactly its names", floor.lane_takes("designer", "architect,designer") is True)
check("a plain list excludes '-'", floor.lane_takes("-", "architect,designer") is False)
check("the triage list ('-') takes only the unlabeled sentinel", floor.lane_takes("-", "-") is True)
check("...and nothing else", floor.lane_takes("designer", "-") is False)

print("4. group_by_project groups riffmap's three lanes under one board, by owner+number")
projects = floor.group_by_project(lanes)
by_pname = {p["name"]: p for p in projects}
check("five projects (watcher, riffmap, second, third, fourth)",
      sorted(by_pname) == ["fourth", "riffmap", "second", "third", "watcher"])
check("riffmap's board holds all three of its lanes",
      sorted(l["lane"] for l in by_pname["riffmap"]["lanes"])
      == ["riffmap", "riffmap-design", "riffmap-triage"])
check("board_url is the owner/number GitHub Projects URL",
      by_pname["riffmap"]["board_url"] == "https://github.com/users/roloc/projects/1")

# --- state directories: one fixture per pill ---------------------------------------------

def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime(metrics.TS_FMT)


def lane_dir(name):
    d = os.path.join(STATE, name)
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    return d


def set_mtime(path, minutes_ago):
    t = (NOW - timedelta(minutes=minutes_ago)).timestamp()
    os.utime(path, (t, t))


def write_log(state_dir, issue, minutes_ago, **kw):
    path = os.path.join(state_dir, "logs", f"card-{issue}-{ts(minutes_ago)}.json")
    subtype = kw.get("subtype", "success")
    d = {"subtype": subtype, "total_cost_usd": kw.get("cost", 1.23), "num_turns": kw.get("turns", 5)}
    if subtype == "success":
        ended_in = kw.get("ended_in", "Done")
        needs = kw.get("needs_steve", "no")
        d["result"] = (f"- Card: #{issue} x\n- Outcome: {kw.get('outcome', 'merged')}\n"
                        f"- Ended in: {ended_in}\n- Quota: ~$1 equivalent\n- Needs Steve: {needs}\n")
    with open(path, "w") as f:
        json.dump(d, f)
    set_mtime(path, minutes_ago)
    return path


# watcher: PAUSE present -> paused
watcher_dir = lane_dir("watcher")
open(os.path.join(watcher_dir, "PAUSE"), "w").close()

# riffmap: a card checked out right now -> working
riffmap_dir = lane_dir("riffmap")
current_path = os.path.join(riffmap_dir, "current")
with open(current_path, "w") as f:
    f.write("501\n")
set_mtime(current_path, 17)

# riffmap-design: died 2 minutes ago -> sleeping (inside the 10-minute retry window)
design_dir = lane_dir("riffmap-design")
write_log(design_dir, 620, 2, subtype="error_max_turns")

# riffmap-triage: last tick parked a card that is now Stuck on the board -> needs_steve
triage_dir = lane_dir("riffmap-triage")
write_log(triage_dir, 777, 45, ended_in="Stuck - Needs help", needs_steve="yes — which mockup?")

# second: nothing at all -> idle, "nothing to pick up"
second_dir = lane_dir("second")

# third: last tick shipped -> idle, "last shipped"
third_dir = lane_dir("third")
write_log(third_dir, 42, 300, ended_in="Done", outcome="merged")

# fourth: last tick DIED 2 minutes ago (inside the sleep window) but the SPAWNER already
# parked the card (two failed attempts, no Stuck comment from the tick itself) -> the board
# is authoritative and this must read as needs_steve, never sleeping (review finding).
fourth_dir = lane_dir("fourth")
write_log(fourth_dir, 99, 2, subtype="error_max_turns")

# also feed WATCHER_STATE explicitly on one lane to prove it overrides the default guess
by_name["riffmap"]["state_dir"] = riffmap_dir
by_name["riffmap-design"]["state_dir"] = design_dir
by_name["riffmap-triage"]["state_dir"] = triage_dir
by_name["watcher"]["state_dir"] = watcher_dir
by_name["second"]["state_dir"] = second_dir
by_name["third"]["state_dir"] = third_dir
by_name["fourth"]["state_dir"] = fourth_dir
# the group_by_project result shares the same lane dicts, already correct: discover_lanes
# put a default state_dir under STATE/<lane> for every one of them, so no override is
# needed here beyond what discover_lanes already computed. Kept for clarity only.

# --- a stubbed board: one board per project ------------------------------------------------

BOARDS = {
    ("roloc", 1): [  # riffmap: build lane's card, and the card the triage lane parked
        {"number": 501, "title": "fix the thing", "url": "https://github.com/roloc/riffmap/issues/501",
         "state": "OPEN", "labels": [], "status": "In progress", "priority": "P1", "stage": "Building"},
        {"number": 777, "title": "ambiguous mockup", "url": "https://github.com/roloc/riffmap/issues/777",
         "state": "OPEN", "labels": [], "status": "Stuck - Needs help", "priority": "P2", "stage": "Parked"},
        {"number": 900, "title": "in ready", "url": "https://github.com/roloc/riffmap/issues/900",
         "state": "OPEN", "labels": [], "status": "Backlog", "priority": "P2", "stage": None},
    ],
    ("roloc", 2): [  # watcher's own board — empty, paused lane doesn't need it
        {"number": 40, "title": "the floor page", "url": "https://github.com/roloc/watcher/issues/40",
         "state": "OPEN", "labels": [], "status": "In progress", "priority": "P2", "stage": "Building"},
    ],
    ("roloc", 3): [  # second: one card sitting in Ready, matching its (unset) LANE_AGENTS
        {"number": 5, "title": "pending", "url": "https://github.com/roloc/second/issues/5",
         "state": "OPEN", "labels": [], "status": "Ready", "priority": "P1", "stage": None},
    ],
    ("roloc", 4): [],  # third: board is empty; the idle reason falls back to "last shipped"
    ("roloc", 5): [  # fourth: the spawner parked #99 after two died ticks
        {"number": 99, "title": "spawner-parked", "url": "https://github.com/roloc/fourth/issues/99",
         "state": "OPEN", "labels": [], "status": "Stuck - Needs help", "priority": "P2", "stage": "Parked"},
    ],
}


def stub_board_fetcher(owner, number):
    return BOARDS.get((owner, number), [])


def stub_unit_active(unit):
    return {"watcher@watcher": False, "watcher@riffmap": True}.get(unit)


ASK_CALLS = []
IMAGES_CALLS = []


def stub_ask(issue, project_repo):
    ASK_CALLS.append((issue, project_repo))
    return {777: "Needs Steve: which of two mockups did you mean?"}.get(issue, "")


def stub_images(issue, project_repo):
    IMAGES_CALLS.append((issue, project_repo))
    return {777: ["https://example.com/mockup-a.png", "https://example.com/mockup-b.png"]}.get(issue, [])


print("5. lane_desk: each of the five pills, from its own fixture")
state = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                           ask_fn=stub_ask, images_fn=stub_images, now=NOW, box_name="test-box")
lanes_by_name = {(p["name"], l["lane"]): l for p in state["projects"] for l in p["lanes"]}

d = lanes_by_name[("watcher", "watcher")]
check("watcher: paused", d["pill"] == "paused", d)
check("watcher: unit_active reported False", d.get("unit_active") is False)
check("watcher: links to its board (no single card)", d["link"] == "https://github.com/users/roloc/projects/2")

d = lanes_by_name[("riffmap", "riffmap")]
check("riffmap (build): working", d["pill"] == "working", d)
check("riffmap: card #501, its board Stage, ~17 minutes", d["card"]["number"] == 501
      and d["stage"] == "Building" and 16 <= d["minutes"] <= 18, d)
check("riffmap: links straight to the card", d["link"] == "https://github.com/roloc/riffmap/issues/501")
check("riffmap: unit_active reported True", d.get("unit_active") is True)

d = lanes_by_name[("riffmap", "riffmap-design")]
check("riffmap-design: sleeping (died 2 minutes ago, inside the 10-minute retry window)",
      d["pill"] == "sleeping", d)
check("riffmap-design: names the card that failed", "620" in d["reason"], d["reason"])
check("riffmap-design: unit_active omitted when the stub never named it", "unit_active" not in d, d)

d = lanes_by_name[("riffmap", "riffmap-triage")]
check("riffmap-triage: needs_steve (its last card is now Stuck on the board)",
      d["pill"] == "needs_steve", d)
check("riffmap-triage: card #777 and the ask from board.py's own comment reader",
      d["card"]["number"] == 777 and "which of two mockups" in d["ask"], d)
check("the ask was resolved against riffmap-triage's OWN repo, not whatever repo "
      "this process happens to run in — several projects share one box (review finding)",
      (777, "roloc/riffmap") in ASK_CALLS, ASK_CALLS)
check("riffmap-triage: the mockups its own Stuck comment pointed at, shown without a click "
      "through to the card (card #91)", d["images"] == ["https://example.com/mockup-a.png",
      "https://example.com/mockup-b.png"], d["images"])
check("the images lookup was resolved against riffmap-triage's OWN repo too",
      (777, "roloc/riffmap") in IMAGES_CALLS, IMAGES_CALLS)

d = lanes_by_name[("second", "second")]
check("second: idle, something of its own sitting in Ready",
      d["pill"] == "idle" and d["reason"] == "Something is ready to pick up.", d)

d = lanes_by_name[("third", "third")]
check("third: idle, names the last shipped card (empty board, no pending work)",
      d["pill"] == "idle" and d["reason"] == "Last shipped #42.", d)

d = lanes_by_name[("fourth", "fourth")]
check("fourth: needs_steve, not sleeping — the board (a spawner park, no Stuck-shaped "
      "comment from the tick) outranks a recently-died log (review finding)",
      d["pill"] == "needs_steve" and d["card"]["number"] == 99, d)
check("fourth: no mockup in its comment — an empty list, never a missing field",
      d["images"] == [], d)

print("6. header and counts")
check("needs_steve_total counts exactly the two lanes in that state (riffmap-triage, fourth)",
      state["header"]["needs_steve_total"] == 2, state["header"])
riffmap_counts = by_pname_state = next(p for p in state["projects"] if p["name"] == "riffmap")["counts"]
check("riffmap counts: 1 in progress, 1 stuck (not counted as in progress), 1 backlog",
      riffmap_counts == {"ready": 0, "in_progress": 1, "backlog": 1}, riffmap_counts)
second_counts = next(p for p in state["projects"] if p["name"] == "second")["counts"]
check("second counts: 1 ready", second_counts == {"ready": 1, "in_progress": 0, "backlog": 0}, second_counts)

print("7. recent ticks: project, lane, card link, when, quota — nothing else, newest first")
recent = state["recent"]
check("every lane's log shows up", {(r["project"], r["lane"]) for r in recent}
      >= {("riffmap", "riffmap-design"), ("riffmap", "riffmap-triage"), ("third", "third")}, recent)
whens = [r["when"] for r in recent]
check("sorted newest first", whens == sorted(whens, reverse=True), whens)
design_row = next(r for r in recent if r["lane"] == "riffmap-design")
check("quota is worded as quota, never cost or a bare number",
      "equivalent" in design_row["quota"] and "not billed" in design_row["quota"], design_row["quota"])
check("card link points at the project's own repo, not a generic one",
      design_row["card"]["url"] == "https://github.com/roloc/riffmap/issues/620", design_row)

print("8. a board that cannot be reached degrades that project, not the whole page")


def failing_fetcher(owner, number):
    if (owner, number) == ("roloc", 1):
        raise RuntimeError("gh: rate limited")
    return stub_board_fetcher(owner, number)


state2 = floor.build_state(PROJECTS, STATE, failing_fetcher, unit_active_fn=stub_unit_active,
                            ask_fn=stub_ask, now=NOW, box_name="test-box")
riffmap2 = next(p for p in state2["projects"] if p["name"] == "riffmap")
check("the failing project carries board_error", riffmap2.get("board_error") is True)
check("…and never the error text (gh's stderr stays off the page)",
      "rate limited" not in json.dumps(state2))
check("its counts are just empty, not a crash", riffmap2["counts"] == {"ready": 0, "in_progress": 0, "backlog": 0})
other_names = {p["name"] for p in state2["projects"]}
check("every other project still rendered",
      other_names == {"watcher", "riffmap", "second", "third", "fourth"})

print("9. the JSON endpoint carries no field outside what the card allows")


def all_keys(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            all_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            all_keys(v, out)


found = set()
all_keys(state, found)
extra = found - floor.ALLOWED_FIELDS
check("no key outside ALLOWED_FIELDS", not extra, str(extra))
check("ALLOWED_FIELDS isn't vacuous (the check would pass trivially otherwise)",
      len(floor.ALLOWED_FIELDS) > 10)

print("10. never a raw file, env value, or a log's own result/stderr text")
blob = json.dumps(state)
check("PROJECT_ROOT (an env value, /x) never leaks into the page", "/x" not in blob)
check("a log's free-text Outcome line never leaks (only Stage/pill/reason, never 'merged')",
      "merged" not in blob, blob)
check("no raw path from this test's own tempdir shows up", ROOT not in blob)

print("11. the HTTP server: / and /api/state.json, nothing else, no logging to stdout")


class FixedCache:
    def get(self):
        return state


srv = ThreadingHTTPServer(("127.0.0.1", 0), floor.make_handler(FixedCache()))
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
        check("/ serves the static HTML page", r.status == 200 and b"<html" in r.read())
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state.json", timeout=5) as r:
        body = json.loads(r.read())
        check("/api/state.json round-trips the same state the cache holds",
              body["header"]["box"] == "test-box")
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/etc/watcher/env", timeout=5)
        check("an arbitrary path is refused", False)
    except Exception as e:
        check("an arbitrary path 404s rather than serving a file",
              "404" in str(e), str(e))
finally:
    srv.shutdown()
    srv.server_close()

print("12. CLI: an unknown flag exits non-zero instead of silently ignoring it")
try:
    floor.main(["--nope"])
    check("unknown flag exits", False)
except SystemExit as e:
    check("unknown flag exits non-zero with a message", "unknown argument" in str(e))

print("13. CLI: a flag with no value after it exits with a message, not a raw traceback")
try:
    floor.main(["--host"])
    check("a value-less flag exits", False)
except SystemExit as e:
    check("...with a message naming the flag", "--host" in str(e), str(e))
except IndexError:
    check("a value-less flag exits with a message, not an uncaught IndexError (review finding)", False)

try:
    floor.main(["--port", "not-a-number"])
    check("a non-numeric --port exits", False)
except SystemExit as e:
    check("...with a message, not a bare ValueError (review finding)", "wants a number" in str(e), str(e))
except ValueError:
    check("a non-numeric --port exits with a message, not an uncaught ValueError", False)

print("14. every desk has its character (card #40: the picture was missing on first ship)")
html = floor.INDEX_HTML
check("the page draws a character on every desk",
      "dispatcher ? dispatcherSVG(d.pill) : spriteSVG(d.pill)" in html)
for pill in ("working", "idle", "needs_steve", "paused", "sleeping"):
    check(f"...with a colour for the {pill} pose", f"{pill}: \"--" in html.split("const PILL_COLOR")[1].split("};")[0])
check("working types", "arm-typing" in html and "@keyframes type" in html)
check("idle and sleeping doze", "@keyframes zz" in html)
check("needs Steve waves a flag", 'class="flag"' in html and "@keyframes wave" in html)
check("reduced motion stops every animation", "prefers-reduced-motion: reduce" in html)

print("15. SDLC order: the dispatcher, then design, then build, then the ship station")
state15 = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                            ask_fn=stub_ask, now=NOW, box_name="test-box")
rm = next(p for p in state15["projects"] if p["name"] == "riffmap")
check("riffmap's lanes read Triage, Design, Build", [l["kind"] for l in rm["lanes"]] == ["Triage", "Design", "Build"],
      [l["kind"] for l in rm["lanes"]])
check("every project has a ship station", all("ship" in p for p in state15["projects"]))
check("with no CI fetcher the ship station says unknown, not a crash",
      rm["ship"]["ci"]["state"] == "unknown")
check("the page puts the ship station after the lanes", "${p.lanes.map(deskHTML).join(\"\")}${shipHTML(p.ship, p.name)}" in floor.INDEX_HTML)
check("the triage lane is drawn as the dispatcher", "dispatcherSVG" in floor.INDEX_HTML and "desk.dispatcher" in floor.INDEX_HTML)

print("16. CI on main, summarized")
S = "a" * 40
def summ(runs):
    return floor.ci_summary({"sha": S, "runs": runs}, "roloc/x")
check("all green is passing", summ([("verify", "completed", "success"), ("web", "completed", "skipped")])["state"] == "passing")
check("a running check is running", summ([("verify", "in_progress", None), ("web", "completed", "success")])["state"] == "running")
f = summ([("verify", "completed", "failure"), ("web", "in_progress", None)])
check("a failed check beats a running one, and is named", f["state"] == "failing" and f["failing"] == ["verify"], f)
check("no checks at all is none", summ([])["state"] == "none")
check("the link is the commit", summ([])["url"] == f"https://github.com/roloc/x/commit/{S}")
check("an unreachable CI is unknown", floor.ci_summary(None, "roloc/x") == {"state": "unknown"})

print("17. the box's deployer, read from its own state files")
DS = os.path.join(ROOT, "deploy-state"); os.makedirs(os.path.join(DS, "held"), exist_ok=True)
os.makedirs(os.path.join(DS, "evals"), exist_ok=True)
L, T = "1" * 40, "2" * 40
def fake_git(live, target):
    return lambda d, *a: {("rev-parse", "HEAD"): live, ("rev-parse", "origin/main"): target}.get(tuple(a), "")
def dstate():
    return floor.deploy_status("/x", DS, "roloc/watcher", git=fake_git(L, T))
def reset():
    for f_ in ("draining", "ready"):
        try: os.remove(os.path.join(DS, f_))
        except OSError: pass
    for d_ in ("held", "evals"):
        for f_ in os.listdir(os.path.join(DS, d_)): os.remove(os.path.join(DS, d_, f_))
reset()
check("the box on main's newest commit is live",
      floor.deploy_status("/x", DS, "roloc/watcher", git=fake_git(L, L))["state"] == "live")
check("behind with nothing recorded yet is checking", dstate()["state"] == "checking")
open(os.path.join(DS, "evals", T), "w").write("fail 1")
check("one suite failure is testing (the retry)", dstate()["state"] == "testing")
open(os.path.join(DS, "ready"), "w").write(f"{L} 1790000000")
check("ready and behind is waiting for idle lanes", dstate()["state"] == "waiting")
open(os.path.join(DS, "draining"), "w").write(f"{T} {L} ")
check("a drain in progress is swapping", dstate()["state"] == "swapping")
open(os.path.join(DS, "held", T), "w").write("58")
check("...even when that commit was held and is shipping through deploy-anyway",
      dstate()["state"] == "swapping")
os.remove(os.path.join(DS, "draining"))
check("a held commit that is now live is live, not held (review finding)",
      floor.deploy_status("/x", DS, "roloc/watcher", git=fake_git(T, T))["state"] == "live")
h = dstate()
check("a held commit is held, with its card", h["state"] == "held" and h["card"]["number"] == 58
      and h["card"]["url"] == "https://github.com/roloc/watcher/issues/58", h)
def boom():
    raise subprocess.TimeoutExpired("git", 10)
state17c = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                             ask_fn=stub_ask, now=NOW, box_name="test-box",
                             deploy_fn=boom, deploy_repo="roloc/riffmap")
check("a deployer read that blows up leaves the page up, with no deploy block",
      all("deploy" not in p["ship"] for p in state17c["projects"]))
check("a checkout that is not a repo shows no deployer", floor.deploy_status("/x", DS, "r/w", git=lambda d, *a: "") is None)

state17 = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                            ask_fn=stub_ask, now=NOW, box_name="test-box",
                            ci_fetcher=lambda repo: {"sha": S, "runs": [("verify", "completed", "success")]},
                            deploy_fn=dstate, deploy_repo="roloc/riffmap")
rm17 = next(p for p in state17["projects"] if p["name"] == "riffmap")
others = [p for p in state17["projects"] if p["name"] != "riffmap"]
check("the deployer shows on the project whose repo it ships, only", "deploy" in rm17["ship"]
      and not any("deploy" in p["ship"] for p in others))
check("a held deploy counts toward 'need Steve'",
      state17["header"]["needs_steve_total"] == state15["header"]["needs_steve_total"] + 1)
def keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k; yield from keys(v)
    elif isinstance(o, list):
        for v in o: yield from keys(v)
check("the ship station emits only allowed fields", set(keys(state17)) <= floor.ALLOWED_FIELDS,
      set(keys(state17)) - floor.ALLOWED_FIELDS)
def failing_ci(repo):
    raise RuntimeError("gh: token ghp_notreal rejected")
state17b = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                             ask_fn=stub_ask, now=NOW, box_name="test-box", ci_fetcher=failing_ci)
check("a CI fetch that fails shows unknown, and its error text never reaches the page",
      all(p["ship"]["ci"] == {"state": "unknown"} for p in state17b["projects"])
      and "ghp_notreal" not in json.dumps(state17b))

print("18. every clickable link opens in a new tab (card #62)")
for snippet in (
    '<a class="desk-link" href="${esc(st.link)}" target="_blank" rel="noopener">',
    '<a class="desk-link" href="${esc(d.link)}" target="_blank" rel="noopener">',
    '<h2><a href="${esc(p.board_url)}" target="_blank" rel="noopener">${esc(p.name)}</a></h2>',
    '<td><a href="${esc(r.card.url)}" target="_blank" rel="noopener">#${r.card.number}</a></td>',
):
    check(f"...{snippet[:40]}...", snippet in floor.INDEX_HTML)
anchors = re.findall(r"<a\b[^>]*>", floor.INDEX_HTML)
check("...and every anchor on the page carries it, so a future link can't slip through",
      anchors and all('target="_blank"' in a for a in anchors), anchors)

print("19. discover_lanes: a lane's worktree layout for Peek (card vs root, a bad value)")
WD_ROOT = tempfile.mkdtemp(prefix="watcher-floor-wd-test-")
WD_PROJECTS = os.path.join(WD_ROOT, "projects")
WD_STATE = os.path.join(WD_ROOT, "state")
os.makedirs(WD_PROJECTS)
os.makedirs(WD_STATE)


def wd_env(name, body):
    with open(os.path.join(WD_PROJECTS, f"{name}.env"), "w") as f:
        f.write(body)


wd_env("card-mode", f"""\
PROJECT_NAME=card-mode
PROJECT_ROOT={WD_ROOT}/repo
BOARD_OWNER=roloc
PROJECT_REPO=roloc/card-mode
BOARD_NUMBER=9
""")
wd_env("root-mode", f"""\
PROJECT_NAME=root-mode
PROJECT_ROOT={WD_ROOT}/repo2
BOARD_OWNER=roloc
PROJECT_REPO=roloc/root-mode
BOARD_NUMBER=10
LANE_WORKDIR=root
""")
wd_env("bad-mode", f"""\
PROJECT_NAME=bad-mode
PROJECT_ROOT={WD_ROOT}/repo3
BOARD_OWNER=roloc
PROJECT_REPO=roloc/bad-mode
BOARD_NUMBER=11
LANE_WORKDIR=sideways
""")

wd_lanes = {l["lane"]: l for l in floor.discover_lanes(WD_PROJECTS, WD_STATE)}
check("default LANE_WORKDIR reads as 'card'", wd_lanes["card-mode"]["workdir_mode"] == "card")
check("card mode's worktree_base is <root>-worktrees, tail.sh's own derivation",
      wd_lanes["card-mode"]["worktree_base"] == f"{WD_ROOT}/repo-worktrees")
check("LANE_WORKDIR=root reads as 'root'", wd_lanes["root-mode"]["workdir_mode"] == "root")
check("root mode's worktree_base is PROJECT_ROOT itself",
      wd_lanes["root-mode"]["worktree_base"] == f"{WD_ROOT}/repo2")
check("a bad LANE_WORKDIR falls back to 'card', not a crashed page",
      wd_lanes["bad-mode"]["workdir_mode"] == "card")

print("20. tail_status: Peek's own decode (idle, waiting, a full replay, then a drain)")
TS_ROOT = tempfile.mkdtemp(prefix="watcher-floor-tail-test-")
TS_STATE = os.path.join(TS_ROOT, "state")
TS_CLAUDE = os.path.join(TS_ROOT, "claude-projects")
os.makedirs(TS_STATE)
os.makedirs(TS_CLAUDE)
ts_info = {"state_dir": TS_STATE, "workdir_mode": "card",
           "worktree_base": os.path.join(TS_ROOT, "repo-worktrees")}

check("no current card, and no tick ever logged: idle, nothing to show",
      floor.tail_status(ts_info, TS_CLAUDE) == {"status": "idle"})

with open(os.path.join(TS_STATE, "current"), "w") as f:
    f.write("77\n")
check("a card but no session written yet: waiting, names the card",
      floor.tail_status(ts_info, TS_CLAUDE) == {"status": "waiting", "card": 77})

ts_session_dir = os.path.join(TS_CLAUDE,
                               tail.encode_dir(os.path.join(ts_info["worktree_base"], "card-77")))
os.makedirs(ts_session_dir, exist_ok=True)
ts_transcript = os.path.join(ts_session_dir, "session-1.jsonl")
with open(ts_transcript, "w") as f:
    f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"line one"}]}}\n')
    f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"line two"}]}}\n')

r1 = floor.tail_status(ts_info, TS_CLAUDE)
check("first look: status ok, a fresh replay", r1["status"] == "ok" and r1["reset"] is True)
check("first look: both lines decoded, in order", r1["lines"] == ["line one", "line two"], r1["lines"])
check("first look: names the card and the session file",
      r1["card"] == 77 and r1["file"] == "session-1.jsonl")
check("first look: a lane holding a card is live", r1["live"] is True)

r2 = floor.tail_status(ts_info, TS_CLAUDE, card=r1["card"], file=r1["file"], pos=r1["pos"])
check("same card, same file, same pos: nothing new, not a re-replay",
      r2["status"] == "ok" and r2["lines"] == [] and r2["reset"] is False)

with open(ts_transcript, "a") as f:
    f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"line three"}]}}\n')
r3 = floor.tail_status(ts_info, TS_CLAUDE, card=r2["card"], file=r2["file"], pos=r2["pos"])
check("a line appended since: drains just the new one",
      r3["status"] == "ok" and r3["lines"] == ["line three"] and r3["reset"] is False)

r4 = floor.tail_status(ts_info, TS_CLAUDE, card=999, file=r3["file"], pos=r3["pos"])
check("a card number that moved on since: forces a fresh replay",
      r4["reset"] is True and r4["lines"] == ["line one", "line two", "line three"])

r5 = floor.tail_status(ts_info, TS_CLAUDE, card=r3["card"], file=r3["file"], pos=-1)
check("a negative pos (a malformed request, never one this endpoint produces itself): "
      "replays instead of crashing on fh.seek()",
      r5["status"] == "ok" and r5["reset"] is True and r5["lines"] == ["line one", "line two", "line three"])

print("20b. a lane that is not working shows its LAST session (card #63: no Peek on a quiet floor)")
os.remove(os.path.join(TS_STATE, "current"))
os.makedirs(os.path.join(TS_STATE, "logs"))
check("no tick log yet: still idle, nothing to show",
      floor.tail_status(ts_info, TS_CLAUDE) == {"status": "idle"})
for name in ("card-12-20260901T000000Z.json", "card-77-20260930T120000Z.json",
             "card-9-20260915T000000Z.json", "card-77-20260930T120000Z.stderr", "notes.txt"):
    with open(os.path.join(TS_STATE, "logs", name), "w") as f:
        f.write('{"subtype": "success"}')  # a finished tick wrote its result; a killed one did not
check("the last card is the newest log by its stamp, not by its number or its name's sort order",
      floor._last_tick(TS_STATE)[0] == 77, floor._last_tick(TS_STATE))
r6 = floor.tail_status(ts_info, TS_CLAUDE)
check("idle with a tick on file: that card's session, replayed",
      r6["status"] == "ok" and r6["card"] == 77
      and r6["lines"] == ["line one", "line two", "line three"], r6)
check("...and marked not live, so the page can say it is the last session", r6["live"] is False)
r7 = floor.tail_status(ts_info, TS_CLAUDE, card=r6["card"], file=r6["file"], pos=r6["pos"])
check("polling it again is a drain, not a re-replay", r7["lines"] == [] and r7["reset"] is False)
# A project's lanes share one worktree per card, so the card's folder can hold a LATER
# session that another lane wrote. This lane's last session is the one that ended when its
# own tick did, not whichever file is newest (review finding on #73).
ended77 = os.path.getmtime(os.path.join(TS_STATE, "logs", "card-77-20260930T120000Z.json"))
os.utime(ts_transcript, (ended77 - 4, ended77 - 4))
other_lane = os.path.join(ts_session_dir, "session-2-other-lane.jsonl")
with open(other_lane, "w") as f:
    f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"another lane, later"}]}}\n')
os.utime(other_lane, (ended77 + 3600, ended77 + 3600))
r9 = floor.tail_status(ts_info, TS_CLAUDE)
check("another lane's later session on the same card is not shown as this lane's last",
      r9["file"] == "session-1.jsonl" and "another lane, later" not in r9["lines"], r9)
os.utime(ts_transcript, (ended77 - 7200, ended77 - 7200))
check("no session that ended when the tick did (pruned): idle, not somebody else's",
      floor.tail_status(ts_info, TS_CLAUDE) == {"status": "idle"})
with open(os.path.join(TS_STATE, "current"), "w") as f:
    f.write("77\n")
check("while the lane is working, the newest file IS its session",
      floor.tail_status(ts_info, TS_CLAUDE)["file"] == "session-2-other-lane.jsonl")
os.remove(os.path.join(TS_STATE, "current"))
# A tick that was killed (the watchdog, a restart) never wrote its result: the log is empty
# and its mtime is the launch, not the end. Its session is the first one to end after that
# launch — however long the tick ran (review finding on #73, second round).
import calendar, time as _time
killed_log = os.path.join(TS_STATE, "logs", "card-77-20260930T130000Z.json")
open(killed_log, "w").close()
launch = calendar.timegm(_time.strptime("20260930T130000Z", "%Y%m%dT%H%M%SZ"))
os.utime(killed_log, (launch, launch))
os.utime(ts_transcript, (launch - 60, launch - 60))            # an earlier run of the card
killed_session = os.path.join(ts_session_dir, "session-killed.jsonl")
with open(killed_session, "w") as f:
    f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"killed mid-build"}]}}\n')
os.utime(killed_session, (launch + 5400, launch + 5400))       # ran ninety minutes
os.utime(other_lane, (launch + 9000, launch + 9000))           # another lane, later still
r10 = floor.tail_status(ts_info, TS_CLAUDE)
check("a killed tick's session is found from its launch, however long it ran",
      r10.get("file") == "session-killed.jsonl" and r10["lines"] == ["killed mid-build"], r10)
os.remove(killed_log)
with open(os.path.join(TS_STATE, "logs", "card-500-20261001T000000Z.json"), "w") as f:
    f.write('{"subtype": "success"}')
check("a last card whose transcript is gone (the CLI prunes them): idle, never 'waiting'",
      floor.tail_status(ts_info, TS_CLAUDE) == {"status": "idle"})
long_dir = os.path.join(TS_CLAUDE, tail.encode_dir(os.path.join(ts_info["worktree_base"], "card-500")))
os.makedirs(long_dir)
with open(os.path.join(long_dir, "s.jsonl"), "w") as f:
    for i in range(floor.PEEK_REPLAY_LINES + 30):
        f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"n%d"}]}}\n' % i)
r8 = floor.tail_status(ts_info, TS_CLAUDE)
check("a first look is capped at PEEK_REPLAY_LINES, the newest ones",
      len(r8["lines"]) == floor.PEEK_REPLAY_LINES
      and r8["lines"][-1] == "n%d" % (floor.PEEK_REPLAY_LINES + 29), len(r8["lines"]))

print("21. the HTTP server: /api/tail.json, wired to a lane lookup (and refused without one)")


def stub_tail_lookup(lane, card, file, pos):
    if lane == "known":
        return {"status": "ok", "card": 1, "file": "s.jsonl", "pos": 10, "lines": ["hi"], "reset": True}
    return None


srv2 = ThreadingHTTPServer(("127.0.0.1", 0), floor.make_handler(FixedCache(), stub_tail_lookup))
port2 = srv2.server_address[1]
t2 = threading.Thread(target=srv2.serve_forever, daemon=True)
t2.start()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port2}/api/tail.json?lane=known", timeout=5) as r:
        check("a known lane returns its lookup's own decode", json.loads(r.read())["lines"] == ["hi"])
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port2}/api/tail.json?lane=nope", timeout=5)
        check("an unknown lane is refused", False)
    except Exception as e:
        check("an unknown lane 404s", "404" in str(e), str(e))
finally:
    srv2.shutdown()
    srv2.server_close()

srv3 = ThreadingHTTPServer(("127.0.0.1", 0), floor.make_handler(FixedCache()))
port3 = srv3.server_address[1]
t3 = threading.Thread(target=srv3.serve_forever, daemon=True)
t3.start()
try:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port3}/api/tail.json?lane=known", timeout=5)
        check("no tail_lookup given at all: still refused, not a crash", False)
    except Exception as e:
        check("no tail_lookup given at all: 404s (every caller before this card)",
              "404" in str(e), str(e))
finally:
    srv3.shutdown()
    srv3.server_close()

print("22. the page's own <script> block still parses (card #71: it silently didn't)")


def script_quotes_ok(script):
    """True if every plain `'` or `"` string in `script` closes before a raw newline —
    a backtick template literal may span lines, a plain one may not. This is the exact
    shape of card #71: `INDEX_HTML` is a non-raw Python triple-quoted string, so a `\\n`
    written for the JS reader was silently eaten as a literal newline instead, leaving an
    unterminated string and the whole <script> block unparsed — the static page loaded,
    nothing it fetches ever did, and nothing said why. No `node` or other JS tool needed
    (this suite has to pass on a stock Mac): walk the text by hand, skipping `//` and
    `/* */` comments, regex literals (by the token before the `/`, same ambiguity a real
    JS tokenizer resolves the same way), and backtick strings — then require every plain
    string to close on the line it opened."""
    i, n, prev = 0, len(script), ''
    while i < n:
        c = script[i]
        if c == '/' and i + 1 < n and script[i + 1] == '/':
            j = script.find('\n', i)
            i = n if j == -1 else j
            continue
        if c == '/' and i + 1 < n and script[i + 1] == '*':
            j = script.find('*/', i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == '/' and prev in '([{,=:;!&|?\n':
            j, in_class, is_regex = i + 1, False, True
            while j < n:
                ch = script[j]
                if ch == '\\':
                    j += 2
                    continue
                if ch == '[':
                    in_class = True
                elif ch == ']':
                    in_class = False
                elif ch == '/' and not in_class:
                    break
                elif ch == '\n':
                    is_regex = False
                    break
                j += 1
            if is_regex and j < n:
                i = j + 1
                while i < n and script[i].isalpha():
                    i += 1
                prev = '/'
                continue
        if c == '`':
            j = i + 1
            while j < n and script[j] != '`':
                j += 2 if script[j] == '\\' else 1
            if j >= n:
                return False
            i, prev = j + 1, '`'
            continue
        if c in '"\'':
            quote, j = c, i + 1
            while j < n and script[j] != quote:
                if script[j] == '\\':
                    j += 2
                    continue
                if script[j] == '\n':
                    return False
                j += 1
            if j >= n:
                return False
            i, prev = j + 1, quote
            continue
        if not c.isspace():
            prev = c
        i += 1
    return True


# Every <script> block, each checked on its own (the page has a small first one that
# reports a failure of the main one). `bin/test-floor-browser.py` is the real check — a
# browser parsing and running the page; this one stays because it runs with no browser.
scripts22 = re.findall(r"<script[^>]*>(.*?)</script>", floor.INDEX_HTML, re.S)
script22 = max(scripts22, key=len)
check("the page has its two script blocks: the error reporter, then the main one",
      len(scripts22) == 2 and scripts22[1] is script22 and '"script-error"' in scripts22[0])
check("no plain-quoted string in the page's scripts spills onto a second line",
      all(script_quotes_ok(sc) for sc in scripts22))
check("...and the checker itself catches the real bug: a bare newline where \\n belonged",
      not script_quotes_ok(script22.replace(
          'pre.textContent ? "\\n" : ""', 'pre.textContent ? "\n" : ""')))

print("23. a failed first load says so, out loud, instead of sitting blank forever (card #71)")
check("the page carries a visible load-error banner, starting hidden",
      '<div class="alert" id="load-error" style="display:none">' in floor.INDEX_HTML)
check("a failed refresh shows that banner",
      'document.getElementById("load-error").style.display = "block"' in floor.INDEX_HTML)
check("a refresh that succeeds hides it again",
      'document.getElementById("load-error").style.display = "none"' in floor.INDEX_HTML)
check("a backgrounded tab's throttled timer is not the only way back: "
      "becoming visible again retries right away",
      'document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });'
      in floor.INDEX_HTML)

print("24. Peek at the ship station: what CI did last, and the deployer's last lines")
NOW24 = datetime(2026, 10, 1, 18, 0, 0, tzinfo=timezone.utc)


def step(name, conclusion="success", status="completed", sec=0):
    return {"name": name, "status": status, "conclusion": conclusion,
            "started": "2026-10-01T17:15:00Z", "completed": "2026-10-01T17:15:%02dZ" % sec}


def job(name, steps, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "started": "2026-10-01T17:15:00Z", "completed": "2026-10-01T17:17:05Z", "steps": steps}


def wrun(sha, title, conclusion="success", status="completed", jobs=None, name="CI",
         started="2026-10-01T17:15:00Z"):
    r = {"id": 1, "name": name, "sha": sha * 40, "title": title, "status": status,
         "conclusion": conclusion, "started": started, "updated": started}
    if jobs is not None:
        r["jobs"] = jobs
    return r


green = [wrun("a", "Newest commit", jobs=[
             job("verify", [step("Set up job"), step("Run actions/checkout@v4"),
                            step("The spawner suite", sec=28), step("Post Run actions/checkout@v4"),
                            step("Complete job")]),
             job("publish", [], conclusion="skipped")]),
         wrun("b", "The one before", started="2026-10-01T15:00:00Z"),
         wrun("c", "An older red one", conclusion="failure", started="2026-09-30T18:00:00Z")]
g = floor.ship_lines(green, NOW24)
check("a green run: the head line says passed, and how long ago",
      g["sub"] == "CI on main: passed 45m ago" and g["live"] is False, g["sub"])
check("the newest commit leads, short sha and title", g["lines"][0] == "aaaaaaa  Newest commit", g["lines"][0])
check("its job, with how long it took", "  ✓ verify · 2m 05s" in g["lines"], g["lines"])
check("its steps, each with its mark and time", "    ✓ The spawner suite · 28s" in g["lines"], g["lines"])
check("housekeeping steps every job has are left out",
      not any(w in l for l in g["lines"] for w in ("Set up job", "Complete job", "Post Run", "actions/checkout")))
check("a skipped job is one line, no steps", "  – publish (skipped)" in g["lines"])
check("earlier commits follow, one line each, with their own mark",
      "Before that on main" in g["lines"]
      and "  ✓ bbbbbbb  The one before · 3h ago" in g["lines"]
      and "  ✗ ccccccc  An older red one · 1d ago" in g["lines"], g["lines"])
check("no deployer section on a board this box does not deploy",
      not any("deployer" in l for l in g["lines"]))

red = [wrun("d", "Broke it", conclusion="failure", jobs=[
           job("verify", [step("The floor page suite", conclusion="failure", sec=2),
                          step("The lane-tail suite", conclusion="skipped")], conclusion="failure"),
           job("web", [step("Run npm test", sec=19)])])]
r24 = floor.ship_lines(red, NOW24)
check("a red run: the head line names the failing job", r24["sub"] == "CI on main: failing — verify", r24["sub"])
check("...and the failing step carries the cross",
      "    ✗ The floor page suite · 2s" in r24["lines"] and "    – The lane-tail suite" in r24["lines"])
running = [wrun("e", "In flight", conclusion=None, status="in_progress", jobs=[
               job("verify", [step("The spawner suite", sec=5),
                              step("The deployer suite", conclusion=None, status="in_progress")],
                   conclusion=None, status="in_progress")])]
u24 = floor.ship_lines(running, NOW24)
check("a run in flight: live, and the step it is on shows as running",
      u24["live"] is True and u24["sub"] == "CI on main: running now"
      and "    … The deployer suite" in u24["lines"], u24)
two = [wrun("f", "Two workflows", jobs=[job("verify", [])]),
       wrun("f", "Two workflows", conclusion="failure", name="Design preview", jobs=[job("publish", [], conclusion="failure")])]
check("two workflows on the newest commit: both listed, the worse one decides",
      floor.ship_lines(two, NOW24)["sub"] == "CI on main: failing — publish"
      and sum(l.startswith(("✓ CI", "✗ Design preview")) for l in floor.ship_lines(two, NOW24)["lines"]) == 2)
nostart = [wrun("9", "No start time", started=None, jobs=[job("verify", [])])]
n24 = floor.ship_lines(nostart, NOW24)
check("a run with no start time leaves no dangling 'started' or 'passed'",
      n24["sub"] == "CI on main: passed" and "✓ CI" in n24["lines"], n24)
got_t = []
def title_get(path):
    if "/jobs" in path:
        return {"jobs": []}
    return {"workflow_runs": [{"id": 1, "name": "CI", "head_sha": "a" * 40, "status": "completed",
                               "conclusion": "success", "display_title": "one\ntwo\n  three"}]}
check("a title is flattened to one line, so it cannot add lines to the panel",
      floor.fetch_ci_runs("r/w", get=title_get)[0]["title"] == "one two three")
check("CI that could not be reached says so in words, no crash",
      floor.ship_lines(None, NOW24)["lines"] == ["CI could not be reached right now."])
check("a repo with no runs yet says so", floor.ship_lines([], NOW24)["sub"] == "no CI runs on main yet")

hist = [(datetime(2026, 10, 1, 17, 57, tzinfo=timezone.utc), "replay suite passed on f0651c6"),
        (datetime(2026, 10, 1, 17, 59, tzinfo=timezone.utc), "deployed f0651c6; every lane un-paused")]
d24 = floor.ship_lines(green, NOW24, deploy={"state": "live", "live": "f0651c6", "target": "f0651c6"},
                       history=hist, has_deployer=True)["lines"]
at = d24.index("The box's deployer")
check("the deploying board gets the deployer: where it stands, then its lines, newest first",
      d24[at + 1] == "  now: live on the box: f0651c6"
      and d24[at + 2].endswith("deployed f0651c6; every lane un-paused") and "just now" in d24[at + 2]
      and d24[at + 3].endswith("replay suite passed on f0651c6") and "3m ago" in d24[at + 3], d24[at:])
h24 = floor.ship_lines(green, NOW24, deploy={"state": "held", "live": "aaa", "target": "bbb",
                                             "card": {"number": 58, "url": "x"}}, has_deployer=True)["lines"]
check("a held deploy names its card; no history on file yet says so",
      "  now: held: bbb (#58)" in h24 and any("Nothing on file yet" in l for l in h24), h24)
check("with CI unreachable the deployer's part still shows",
      "The box's deployer" in floor.ship_lines(None, NOW24, history=hist, has_deployer=True)["lines"])

HIST_DIR = os.path.join(ROOT, "hist"); os.makedirs(HIST_DIR)
check("no history file: empty, not a crash", floor.read_deploy_history(HIST_DIR) == [])
with open(os.path.join(HIST_DIR, "history"), "w") as f:
    f.write("2026-10-01T17:57:51Z replay suite passed on f0651c6\nnot a stamp at all\n\n"
            + "".join("2026-10-01T17:59:%02dZ line %d\n" % (i, i) for i in range(30)))
rh = floor.read_deploy_history(HIST_DIR)
check("the history file: the newest lines only, a malformed one skipped",
      len(rh) == floor.SHIP_HISTORY_LINES and rh[-1][1] == "line 29"
      and rh[-1][0] == datetime(2026, 10, 1, 17, 59, 29, tzinfo=timezone.utc), rh[-2:])

fetches = []
def stub_runs(repo):
    fetches.append(repo)
    if repo == "roloc/down":
        raise RuntimeError("gh: token ghp_notreal rejected")
    return green
clock = [NOW24]
look = floor.make_ship_lookup(lambda: {"watcher": "roloc/watcher", "riffmap": "roloc/riffmap", "down": "roloc/down"},
                              stub_runs, deploy_repo="roloc/watcher",
                              deploy_fn=lambda: {"state": "live", "live": "f0651c6", "target": "f0651c6"},
                              history_fn=lambda: hist, now_fn=lambda: clock[0])
check("a project that is not on the page is refused, and nothing is fetched for it",
      look("../../etc") is None and look("") is None and fetches == [])
w24 = look("watcher")
check("the deploying project: CI and the deployer", w24["status"] == "ok" and "The box's deployer" in w24["lines"])
check("any other project: CI only", "The box's deployer" not in look("riffmap")["lines"])
look("watcher"); look("watcher")
check("an open panel's polls share one fetch (GitHub is asked once per project)",
      fetches == ["roloc/watcher", "roloc/riffmap"], fetches)
dn = look("down")
check("CI that cannot be reached still answers, and gh's own words never reach the page",
      dn["lines"] == ["CI could not be reached right now."] and "ghp_notreal" not in json.dumps(dn))

got = []
def fake_get(path):
    got.append(path)
    if "/jobs" in path:
        return {"jobs": [{"name": "verify", "status": "completed", "conclusion": "success",
                          "started_at": "2026-10-01T17:15:00Z", "completed_at": "2026-10-01T17:16:00Z",
                          "steps": [{"name": "The spawner suite", "status": "completed", "conclusion": "success",
                                     "started_at": "2026-10-01T17:15:00Z", "completed_at": "2026-10-01T17:15:28Z"}]}]}
    if "event=pull_request" in path:
        return {"workflow_runs": []}  # nothing open right now
    return {"workflow_runs": [
        {"id": 11, "name": "CI", "head_sha": "a" * 40, "display_title": "Newest", "status": "completed",
         "conclusion": "success", "run_started_at": "2026-10-01T17:15:00Z", "updated_at": "x"},
        {"id": 10, "name": "CI", "head_sha": "b" * 40, "display_title": "Older", "status": "completed",
         "conclusion": "success", "run_started_at": "2026-10-01T15:00:00Z", "updated_at": "x"}]}
fr = floor.fetch_ci_runs("roloc/watcher", get=fake_get)
check("fetch_ci_runs asks for push runs on main, whether a PR has one running, and jobs "
      "for the newest commit only",
      got == ["repos/roloc/watcher/actions/runs?branch=main&event=push&per_page=6",
              "repos/roloc/watcher/actions/runs?event=pull_request&per_page=5",
              "repos/roloc/watcher/actions/runs/11/jobs?per_page=50"], got)
check("...and what it returns is what ship_lines reads",
      "    ✓ The spawner suite · 28s" in floor.ship_lines(fr, NOW24)["lines"] and "jobs" not in fr[1])

print("24b. the CI window a push-only history can't see: a PR's own check, while it runs (card #77)")
def pr_running_get(path):
    if "/jobs" in path:
        return {"jobs": [{"name": "verify", "status": "in_progress", "conclusion": None,
                          "started_at": "2026-10-01T17:50:00Z", "completed_at": None,
                          "steps": [{"name": "The spawner suite", "status": "in_progress", "conclusion": None,
                                     "started_at": "2026-10-01T17:50:00Z", "completed_at": None}]}]}
    if "event=pull_request" in path:
        return {"workflow_runs": [
            {"id": 21, "name": "CI", "head_sha": "d" * 40, "display_title": "A card in flight",
             "status": "in_progress", "conclusion": None, "run_started_at": "2026-10-01T17:50:00Z",
             "updated_at": "x", "head_branch": "card-77", "pull_requests": [{"number": 80}]}]}
    return {"workflow_runs": [
        {"id": 11, "name": "CI", "head_sha": "a" * 40, "display_title": "Last shipped", "status": "completed",
         "conclusion": "success", "run_started_at": "2026-10-01T17:15:00Z", "updated_at": "x"}]}
prr = floor.fetch_ci_runs("roloc/watcher", get=pr_running_get)
check("the live PR run leads the list, ahead of what already shipped",
      prr[0]["pr"] == 80 and prr[0]["branch"] == "card-77" and prr[1]["sha"] == "a" * 40, prr)
check("its own jobs were fetched, not the shipped commit's",
      prr[0]["jobs"][0]["name"] == "verify" and "jobs" not in prr[1])
spr = floor.ship_lines(prr, NOW24)
check("the header names the PR, not main, while its check runs",
      spr["sub"] == "CI on PR #80: running now" and spr["live"] is True, spr["sub"])
check("the commit line carries the PR's branch", spr["lines"][0] == "ddddddd  A card in flight  · card-77")
check("what already shipped follows under its own, non-'before', heading",
      "Already on main" in spr["lines"] and "  ✓ aaaaaaa  Last shipped · 45m ago" in spr["lines"], spr["lines"])

def pr_query_fails(path):
    if "event=pull_request" in path:
        raise RuntimeError("gh: rate limited")
    if "/jobs" in path:
        return {"jobs": []}
    return {"workflow_runs": [{"id": 11, "name": "CI", "head_sha": "a" * 40, "display_title": "Last shipped",
                               "status": "completed", "conclusion": "success",
                               "run_started_at": "2026-10-01T17:15:00Z", "updated_at": "x"}]}
check("a failed check for a live PR never costs the push history",
      floor.fetch_ci_runs("roloc/watcher", get=pr_query_fails)[0]["sha"] == "a" * 40)

def pr_closed_get(path):
    # GitHub empties `pull_requests` once the PR it ran for is closed — observed on this
    # box for superseded runs. Nothing to attribute it to, so it must not lead the panel
    # mislabeled as "on main" over a branch commit (reviewer's own finding on card #77).
    if "event=pull_request" in path:
        return {"workflow_runs": [{"id": 21, "name": "CI", "head_sha": "d" * 40, "display_title": "Superseded",
                                   "status": "in_progress", "conclusion": None, "head_branch": "old-branch",
                                   "run_started_at": "2026-10-01T17:58:00Z", "updated_at": "x",
                                   "pull_requests": []}]}
    if "/jobs" in path:
        return {"jobs": []}
    return {"workflow_runs": [{"id": 11, "name": "CI", "head_sha": "a" * 40, "display_title": "Last shipped",
                               "status": "completed", "conclusion": "success",
                               "run_started_at": "2026-10-01T17:15:00Z", "updated_at": "x"}]}
check("a running check with no PR to attribute it to is left out, not mislabeled as main",
      floor.fetch_ci_runs("roloc/watcher", get=pr_closed_get)[0]["sha"] == "a" * 40)

srv4 = ThreadingHTTPServer(("127.0.0.1", 0), floor.make_handler(FixedCache(), None, look))
t4 = threading.Thread(target=srv4.serve_forever, daemon=True); t4.start()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{srv4.server_address[1]}/api/ship.json?project=watcher", timeout=5) as r:
        check("/api/ship.json serves a known project's lines", "aaaaaaa  Newest commit" in json.loads(r.read())["lines"])
    for bad in ("?project=nope", ""):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{srv4.server_address[1]}/api/ship.json{bad}", timeout=5)
            check("an unknown or missing project is refused", False)
        except Exception as e:
            check(f"/api/ship.json{bad or ' with no project'}: 404", "404" in str(e), str(e))
finally:
    srv4.shutdown(); srv4.server_close()
check("the ship desk carries its own Peek button, beside its link and not inside it",
      'data-peek-ship="${esc(project)}"' in floor.INDEX_HTML)

shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(WD_ROOT, ignore_errors=True)
shutil.rmtree(TS_ROOT, ignore_errors=True)

print(f"{count} cases, {'all passed' if not fails else 'FAILURES'}")
sys.exit(fails)
