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


def stub_ask(issue, project_repo):
    ASK_CALLS.append((issue, project_repo))
    return {777: "Needs Steve: which of two mockups did you mean?"}.get(issue, "")


print("5. lane_desk: each of the five pills, from its own fixture")
state = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                           ask_fn=stub_ask, now=NOW, box_name="test-box")
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
check("the page puts the ship station after the lanes", "${p.lanes.map(deskHTML).join(\"\")}${shipHTML(p.ship)}" in floor.INDEX_HTML)
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

shutil.rmtree(ROOT, ignore_errors=True)

print(f"{count} cases, {'all passed' if not fails else 'FAILURES'}")
sys.exit(fails)
