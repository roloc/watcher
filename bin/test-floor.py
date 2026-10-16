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

# riffmap: a card checked out right now -> working, AND a PAUSE file beside it (card #110:
# a deploy or a hand pause mid-tick never means idle) -> still working, plus a pausing flag
riffmap_dir = lane_dir("riffmap")
current_path = os.path.join(riffmap_dir, "current")
with open(current_path, "w") as f:
    f.write("501\n")
set_mtime(current_path, 17)
with open(os.path.join(riffmap_dir, "PAUSE"), "w") as f:
    f.write("deploy abc1234\n")

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
        # a second Stuck card no lane's own last tick points at — the design lane ran this
        # one earlier and moved on to #777 since (card #96's "why nothing shows today"):
        # #778 must still show up on the board's own Needs Steve strip.
        {"number": 778, "title": "an earlier pick, still unanswered", "url": "https://github.com/roloc/riffmap/issues/778",
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
ROUNDS_CALLS = []


def stub_ask(issue, project_repo):
    ASK_CALLS.append((issue, project_repo))
    return {777: "Needs Steve: which of two mockups did you mean?",
            778: "Needs Steve: pick a direction for the earlier thing"}.get(issue, "")


def stub_images(issue, project_repo):
    IMAGES_CALLS.append((issue, project_repo))
    return {777: ["https://example.com/mockup-a.png", "https://example.com/mockup-b.png"]}.get(issue, [])


def stub_rounds(issue, project_repo):
    ROUNDS_CALLS.append((issue, project_repo))
    return {778: [{"when": "2026-09-28T10:00:00Z", "images": [
        {"url": "https://example.com/778-a.png", "label": "desktop", "letter": "A", "recommended": True}]}]}.get(issue, [])


def stub_ratelimit():
    return {"remaining": 4312, "resetAt": "2026-10-09T20:00:00Z", "limit": 5000}


PLAN_CALLS = []


def stub_plan(issue, project_repo):
    PLAN_CALLS.append((issue, project_repo))
    return ""  # nobody has posted one in this fixture — riffmap's working lane (17m) is late


print("5. lane_desk: each of the five pills, from its own fixture")
state = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                           ask_fn=stub_ask, images_fn=stub_images, rounds_fn=stub_rounds,
                           plan_fn=stub_plan,
                           now=NOW, box_name="test-box", ratelimit_fn=stub_ratelimit)
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
check("card #110: a PAUSE file beside a held card never flips the pill — still working",
      d["pill"] == "working" and d.get("pausing") is True, d)
check("card #110: the pausing reason is the PAUSE file's own first line",
      d.get("pausing_reason") == "deploy abc1234", d)
check("card #103: 17 minutes in, no plan comment on file — plan_posted False, plan_late True",
      d.get("plan_posted") is False and d.get("plan_late") is True, d)
check("the plan lookup was resolved against riffmap's OWN repo, not whatever repo this "
      "process happens to run in", (501, "roloc/riffmap") in PLAN_CALLS, PLAN_CALLS)

d = lanes_by_name[("watcher", "watcher")]
check("card #110: a PAUSE file with no card held is still a plain paused desk, no pausing flag",
      "pausing" not in d and "pausing_reason" not in d, d)

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

print("5b. _pause_reason: the PAUSE file's own first line, never more, '' for a hand-touch")
reason_dir = tempfile.mkdtemp(prefix="watcher-floor-pause-")
hand = os.path.join(reason_dir, "HAND")
open(hand, "w").close()
check("a hand-touched (empty) PAUSE file: ''", floor._pause_reason(hand) == "")
multi = os.path.join(reason_dir, "MULTI")
with open(multi, "w") as f:
    f.write("deploy c920516\nsome second line that must never surface\n")
check("only the first line, trimmed", floor._pause_reason(multi) == "deploy c920516")
check("a path that does not exist: '', never a crash",
      floor._pause_reason(os.path.join(reason_dir, "MISSING")) == "")

print("5c. lane_desk: plan_posted/plan_late directly (card #103)")
plan_dir = lane_dir("plan-fixture")
plan_current = os.path.join(plan_dir, "current")
with open(plan_current, "w") as f:
    f.write("600\n")
set_mtime(plan_current, 17)  # past PLAN_LATE_MIN
plan_lane = {"lane": "x", "unit": "watcher@x", "project_repo": "roloc/x",
             "state_dir": plan_dir, "lane_agents": "", "lane_command": "/watch"}
plan_items = {600: {"url": "https://github.com/roloc/x/issues/600", "stage": "Building"}}


def plan_present(issue, project_repo):
    return "2026-09-28T11:50:00Z"


def plan_absent(issue, project_repo):
    return ""


def plan_stale(issue, project_repo):
    return "2026-09-28T11:30:00Z"  # before this lane's own launch (11:43) — a prior tick's


d = floor.lane_desk(plan_lane, plan_items, "https://board", NOW, plan=plan_present)
check("a posted plan: plan_posted True, never late, however many minutes have passed",
      d.get("plan_posted") is True and d.get("plan_late") is False, d)

d = floor.lane_desk(plan_lane, plan_items, "https://board", NOW, plan=plan_stale)
check("a plan comment that predates this lane's own launch (a card a second tick resumed, "
      "still carrying the first tick's plan): reads as not yet posted, not borrowed from "
      "a tick that isn't this one (reviewer finding)",
      d.get("plan_posted") is False and d.get("plan_late") is True, d)

d = floor.lane_desk(plan_lane, plan_items, "https://board", NOW, plan=plan_absent)
check("no plan after 10+ minutes: plan_posted False, plan_late True — the lane's own warning",
      d.get("plan_posted") is False and d.get("plan_late") is True, d)

young_dir = lane_dir("plan-fixture-young")
young_current = os.path.join(young_dir, "current")
with open(young_current, "w") as f:
    f.write("601\n")
set_mtime(young_current, 4)  # inside the grace window
young_lane = {"lane": "y", "unit": "watcher@y", "project_repo": "roloc/y",
              "state_dir": young_dir, "lane_agents": "", "lane_command": "/watch"}
young_items = {601: {"url": "https://github.com/roloc/y/issues/601", "stage": "Claimed"}}
d = floor.lane_desk(young_lane, young_items, "https://board", NOW, plan=plan_absent)
check("no plan yet but still inside the grace window: plan_posted False, not late",
      d.get("plan_posted") is False and d.get("plan_late") is False, d)

d = floor.lane_desk(plan_lane, plan_items, "https://board", NOW)
check("no plan callable at all (an older caller): neither field present, never a guessed "
      "'no plan' on a lane this was never asked about",
      "plan_posted" not in d and "plan_late" not in d, d)

print("5d. lane_desk: the Waiting line (card #165) — a working desk whose newest tool call "
      "has sat open past WAITING_GRACE_MIN, read off the same session Peek decodes")
CPD = os.path.join(ROOT, "claude-projects")
os.makedirs(CPD, exist_ok=True)
WAIT_WORKTREE_BASE = "/repo-worktrees"
WAIT_CARD_DIR = os.path.join(WAIT_WORKTREE_BASE, "card-700")


def write_wait_session(*lines):
    d = os.path.join(CPD, tail.encode_dir(WAIT_CARD_DIR))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "s1.jsonl"), "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def assistant_tool_use(minutes_ago, desc, tool_id="t1"):
    return {"type": "assistant", "timestamp": ts_iso(minutes_ago),
            "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
                                      "input": {"description": desc}}]}}


def user_tool_result(minutes_ago, tool_id="t1"):
    return {"type": "user", "timestamp": ts_iso(minutes_ago),
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def ts_iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


wait_dir = lane_dir("wait-fixture")
wait_current = os.path.join(wait_dir, "current")
with open(wait_current, "w") as f:
    f.write("700\n")
set_mtime(wait_current, 5)
wait_lane = {"lane": "w", "unit": "watcher@w", "project_repo": "roloc/w",
             "state_dir": wait_dir, "lane_agents": "", "lane_command": "/watch",
             "workdir_mode": "card", "worktree_base": WAIT_WORKTREE_BASE}
wait_items = {700: {"url": "https://github.com/roloc/w/issues/700", "stage": "Building"}}

d = floor.lane_desk(wait_lane, wait_items, "https://board", NOW, claude_projects_dir=[CPD])
check("no session on file at all: no waiting field", "waiting" not in d, d)

write_wait_session(assistant_tool_use(5, "Check debug trial progress"))
d = floor.lane_desk(wait_lane, wait_items, "https://board", NOW, claude_projects_dir=[CPD])
check("an open call past the grace: the waiting line names it and how long",
      d.get("waiting") == {"on": "$ Check debug trial progress", "minutes": 5}, d)

write_wait_session(assistant_tool_use(1, "Quick call"))
d = floor.lane_desk(wait_lane, wait_items, "https://board", NOW, claude_projects_dir=[CPD])
check("an open call still inside the grace: no waiting field yet",
      "waiting" not in d, d)

write_wait_session(assistant_tool_use(5, "Check debug trial progress"), user_tool_result(0))
d = floor.lane_desk(wait_lane, wait_items, "https://board", NOW, claude_projects_dir=[CPD])
check("the call already returned: no waiting field, whatever its own age",
      "waiting" not in d, d)

d = floor.lane_desk(plan_lane, plan_items, "https://board", NOW, plan=plan_absent)
check("no claude_projects_dir given at all (an older caller): never computed, no field, "
      "no crash even though this lane has no workdir_mode/worktree_base of its own",
      "waiting" not in d, d)

check("'waiting' only ever reaches the page via the allowed fields",
      {"waiting", "on"} <= floor.ALLOWED_FIELDS)

# build_state itself: the plumbing from main()'s own claude_projects_dir argument down to
# lane_desk, exercised end to end rather than only at lane_desk's own level above — riffmap's
# existing working-on-#501 fixture (section 5, below) gets a session showing an open call.
riffmap_card_dir = os.path.join(os.path.dirname("/x"), os.path.basename("/x") + "-worktrees", "card-501")
d_ = os.path.join(CPD, tail.encode_dir(riffmap_card_dir))
os.makedirs(d_, exist_ok=True)
with open(os.path.join(d_, "s1.jsonl"), "w") as f:
    f.write(json.dumps(assistant_tool_use(6, "Soaking the canary release")) + "\n")
state_wait = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                                ask_fn=stub_ask, images_fn=stub_images, rounds_fn=stub_rounds,
                                plan_fn=stub_plan, now=NOW, box_name="test-box",
                                ratelimit_fn=stub_ratelimit, claude_projects_dir=[CPD])
d = next(l for p in state_wait["projects"] for l in p["lanes"] if l["lane"] == "riffmap")
check("build_state wires claude_projects_dir down to lane_desk's own waiting field",
      d.get("waiting") == {"on": "$ Soaking the canary release", "minutes": 6}, d)

print("6. header and counts")
check("needs_steve_total counts every Stuck CARD on every board (riffmap: #777 and #778; "
      "fourth: #99) — not how many lane pills happen to read needs_steve, so #778 (no "
      "lane's own last tick) is still counted, and #777 is counted once, not once per lane",
      state["header"]["needs_steve_total"] == 3, state["header"])
riffmap_counts = by_pname_state = next(p for p in state["projects"] if p["name"] == "riffmap")["counts"]
check("riffmap counts: 1 in progress, Stuck cards not counted as in progress, 1 backlog",
      riffmap_counts == {"ready": 0, "in_progress": 1, "backlog": 1}, riffmap_counts)
second_counts = next(p for p in state["projects"] if p["name"] == "second")["counts"]
check("second counts: 1 ready", second_counts == {"ready": 1, "in_progress": 0, "backlog": 0}, second_counts)
check("card #98: the account's GraphQL budget rides in the header, from ratelimit_fn",
      state["header"]["github_remaining"] == 4312
      and state["header"]["github_reset"] == "2026-10-09T20:00:00Z"
      and state["header"]["github_limit"] == 5000, state["header"])

print("6c. card #98: no ratelimit_fn, or nothing cached yet — None, never a crash or a "
      "guess")
state_norl = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                                ask_fn=stub_ask, now=NOW, box_name="test-box")
check("no ratelimit_fn given: every github_* field is None",
      state_norl["header"]["github_remaining"] is None
      and state_norl["header"]["github_reset"] is None
      and state_norl["header"]["github_limit"] is None, state_norl["header"])
state_emptyrl = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                                   ask_fn=stub_ask, now=NOW, box_name="test-box",
                                   ratelimit_fn=lambda: {})
check("ratelimit_fn returning {} (nothing cached on the box yet): the same, no crash",
      state_emptyrl["header"]["github_remaining"] is None, state_emptyrl["header"])


def failing_ratelimit():
    raise RuntimeError("board.py: no writable cache directory")


state_failrl = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                                  ask_fn=stub_ask, now=NOW, box_name="test-box",
                                  ratelimit_fn=failing_ratelimit)
check("ratelimit_fn raising: the page still renders, just with no budget line",
      state_failrl["header"]["github_remaining"] is None, state_failrl["header"])

print("6d. card #114: this hour's GraphQL spend by caller rides in the header too, from "
      "ledger_fn — a list sorted by spend, never a dict with caller names as keys (every "
      "key in the page's JSON has to be one all_keys-walked, card #9 below)")


def stub_ledger():
    return {"points_per_min": 1.5, "by_caller": {"lane:riffmap": 60, "tick:riffmap:501": 30}}


state_ledger = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                                  ask_fn=stub_ask, now=NOW, box_name="test-box",
                                  ratelimit_fn=stub_ratelimit, ledger_fn=stub_ledger)
check("points_per_min rides straight through", state_ledger["header"]["github_points_per_min"] == 1.5,
      state_ledger["header"])
check("spend is a list, highest caller first",
      state_ledger["header"]["github_spend"] ==
      [{"caller": "lane:riffmap", "points": 60}, {"caller": "tick:riffmap:501", "points": 30}],
      state_ledger["header"]["github_spend"])

state_noledger = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                                    ask_fn=stub_ask, now=NOW, box_name="test-box")
check("no ledger_fn given: points_per_min is None and spend is an empty list, never a crash",
      state_noledger["header"]["github_points_per_min"] is None
      and state_noledger["header"]["github_spend"] == [], state_noledger["header"])


def failing_ledger():
    raise RuntimeError("board.py: no writable cache directory")


state_failledger = floor.build_state(PROJECTS, STATE, stub_board_fetcher, unit_active_fn=stub_unit_active,
                                      ask_fn=stub_ask, now=NOW, box_name="test-box",
                                      ledger_fn=failing_ledger)
check("ledger_fn raising: the page still renders, just with no spend line",
      state_failledger["header"]["github_points_per_min"] is None, state_failledger["header"])

print("6b. the Needs Steve strip (card #96): every Stuck card on the board, independent "
      "of which lane last touched it, with the round pictures it has been asked")
riffmap_proj = next(p for p in state["projects"] if p["name"] == "riffmap")
stuck_numbers = [s["card"]["number"] for s in riffmap_proj["stuck"]]
check("both Stuck cards on riffmap's board, #778 included though no lane's desk shows it",
      stuck_numbers == [777, 778], stuck_numbers)
s777 = next(s for s in riffmap_proj["stuck"] if s["card"]["number"] == 777)
check("#777's ask, read the same way the lane pill's is",
      "which of two mockups" in s777["ask"], s777)
s778 = next(s for s in riffmap_proj["stuck"] if s["card"]["number"] == 778)
check("#778's own ask, even with no lane pointing at it",
      "pick a direction for the earlier thing" in s778["ask"], s778)
check("#778's rounds, newest first, lettered pictures and all",
      s778["rounds"] == [{"when": "2026-09-28T10:00:00Z", "images": [
          {"url": "https://example.com/778-a.png", "label": "desktop", "letter": "A",
           "recommended": True}]}], s778)
check("the rounds lookup was resolved against riffmap's OWN repo",
      (778, "roloc/riffmap") in ROUNDS_CALLS, ROUNDS_CALLS)
fourth_proj = next(p for p in state["projects"] if p["name"] == "fourth")
check("fourth's own Stuck card shows up on its own strip too",
      [s["card"]["number"] for s in fourth_proj["stuck"]] == [99], fourth_proj["stuck"])
check("a board with nothing Stuck has an empty strip, never a missing key",
      next(p for p in state["projects"] if p["name"] == "second")["stuck"] == [])

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
    for d_ in ("held", "evals"):
        for f_ in os.listdir(os.path.join(DS, d_)): os.remove(os.path.join(DS, d_, f_))
reset()
check("the box on main's newest commit is live",
      floor.deploy_status("/x", DS, "roloc/watcher", git=fake_git(L, L))["state"] == "live")
check("behind with nothing recorded yet is checking", dstate()["state"] == "checking")
open(os.path.join(DS, "evals", T), "w").write("fail 1")
check("one suite failure is testing (the retry)", dstate()["state"] == "testing")
# Card #106: there is no "waiting for idle lanes"/"swapping" state any more — a release is
# cloned, installed and the pointer flipped inside one run, never split across a pause.
open(os.path.join(DS, "held", T), "w").write("58")
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

print("17a0. card #105: deploy_service_running reads a oneshot unit's real states — a review")
print("      finding: systemctl_is_active's 'is-active --quiet' only succeeds for 'active',")
print("      which a Type=oneshot unit with no RemainAfterExit never reaches while running.")
FAKE_BIN = os.path.join(ROOT, "fake-systemctl-bin")
os.makedirs(FAKE_BIN, exist_ok=True)
def fake_systemctl(state):
    # Real `systemctl is-active` exits 0 for exactly "active" and non-zero for every other
    # state (reviewer, round 2: a fixture that always exits 0 never covers the actual
    # production shape — `activating` on stdout with a non-zero exit code, found live on
    # the box). `deploy_service_running` must read `activating` as running from stdout
    # alone, the exit code notwithstanding.
    path = os.path.join(FAKE_BIN, "systemctl")
    rc = 0 if state == "active" else 3
    with open(path, "w") as f:
        f.write(f"#!/bin/sh\necho {state}\nexit {rc}\n")
    os.chmod(path, 0o755)
_old_path = os.environ.get("PATH", "")
os.environ["PATH"] = FAKE_BIN + os.pathsep + _old_path
try:
    fake_systemctl("activating")
    check("a oneshot unit's whole run reads as running — not just an instant of 'active'",
          floor.deploy_service_running("watcher-deploy.service") is True)
    fake_systemctl("active")
    check("'active' counts too (a long-lived unit, or the brief instant a oneshot can reach it)",
          floor.deploy_service_running("watcher-deploy.service") is True)
    fake_systemctl("reloading")
    check("'reloading' counts as running", floor.deploy_service_running("watcher-deploy.service") is True)
    fake_systemctl("inactive")
    check("between runs ('inactive'): not running", floor.deploy_service_running("watcher-deploy.service") is False)
    fake_systemctl("failed")
    check("a run systemd gave up on ('failed'): not running, never stuck 'running' forever",
          floor.deploy_service_running("watcher-deploy.service") is False)
finally:
    os.environ["PATH"] = _old_path

print("17a. card #105: which step, how fresh, who it's waiting on, and lands-by — the Ship")
print("     desk's own facts, from the deployer's step/lock files and a busy canary lane's")
print("     own PAUSE/current/project files. Today's incident (the card's own 'How'): two")
print("     lanes mid-tick holding a deploy, with no sign on the floor page of either.")
reset()

def deploy_now():
    return floor.deploy_status("/x", DS, "roloc/watcher", git=fake_git(L, T), lanes=WAIT_LANES,
                                now=NOW, running_fn=lambda: RUNNING)

RUNNING = False
WAIT_LANES = []
# No lock file at all yet: this box has never run the deployer.
check("never having run at all: no alive line, not a crash", deploy_now()["alive"] is None)
open(os.path.join(DS, "lock"), "w").close()
os.utime(os.path.join(DS, "lock"), (NOW.timestamp() - 1680, NOW.timestamp() - 1680))
check("no last-run stamp yet (a box whose deployer predates card #129): falls back to the lock's own mtime",
      deploy_now()["alive"] == {"running": False, "since_sec": 1680, "next_in_sec": 0, "stale": True},
      deploy_now()["alive"])
# Card #129's own incident: a 27-minute run leaves the lock (touched at its START, by the
# `exec` that opens it) 28 minutes stale the moment it finishes, 2 minutes after that.
# `last-run`, touched on exit, is what "checked ago" reads between runs now — the lock's
# own mtime stops mattering once a run has ended.
open(os.path.join(DS, "last-run"), "w").close()
os.utime(os.path.join(DS, "last-run"), (NOW.timestamp() - 120, NOW.timestamp() - 120))
check("between runs: checked 2m ago (last-run's own mtime, from a run that ended 2m ago) — not 28m stale from the lock's own start time",
      deploy_now()["alive"] == {"running": False, "since_sec": 120, "next_in_sec": 180, "stale": False},
      deploy_now()["alive"])
# RUNNING here stands for deploy_service_running()'s own answer, true for the WHOLE length
# of a run: watcher-deploy.service is Type=oneshot with no RemainAfterExit, so systemd
# calls it "activating", never "active", for as long as autodeploy.sh is executing (found
# in review, against the live box) — deploy_service_running (not systemctl_is_active) is
# what tells "mid-run" from "between runs", a fact that section 17b below checks directly.
os.utime(os.path.join(DS, "lock"), (NOW.timestamp() - 30, NOW.timestamp() - 30))
RUNNING = True
check("mid-run (the oneshot unit is activating): running now, timed from THIS run's own start (the lock) — last-run's older stamp from the PREVIOUS run is ignored",
      deploy_now()["alive"] == {"running": True, "since_sec": 30}, deploy_now()["alive"])
RUNNING = False
os.utime(os.path.join(DS, "last-run"), (NOW.timestamp() - 900, NOW.timestamp() - 900))
check("older than two timer periods: stale",
      deploy_now()["alive"] == {"running": False, "since_sec": 900, "next_in_sec": 0, "stale": True})
os.utime(os.path.join(DS, "last-run"), (NOW.timestamp() - 120, NOW.timestamp() - 120))

check("no step file yet: no step, not a crash", deploy_now()["step"] is None)
with open(os.path.join(DS, "step"), "w") as f:
    f.write("4\n5\nSoak on the canary lane(s)\nsoaking: 1 of 3 clean tick(s) so far\n")
check("the step file's own four lines, read as the Ship desk's one fact",
      deploy_now()["step"] == {"number": 4, "total": 5, "name": "Soak on the canary lane(s)",
                               "detail": "soaking: 1 of 3 clean tick(s) so far"})

WAIT_LANES = []
check("nobody paused for this deploy: waiting_for is empty, lands_by is None (landing on the next check)",
      deploy_now()["waiting_for"] == [] and deploy_now()["lands_by"] is None)

WAITDIR = os.path.join(ROOT, "deploy-wait-lanes")
def wait_lane(name, project_repo, tick_timeout, launched_minutes_ago, card):
    d = os.path.join(WAITDIR, name); os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "PAUSE"), "w") as f:
        f.write(f"deploy {T}\n")
    cpath = os.path.join(d, "current")
    with open(cpath, "w") as f:
        f.write(str(card))
    t = (NOW - timedelta(minutes=launched_minutes_ago)).timestamp()
    os.utime(cpath, (t, t))
    return {"lane": name, "project_repo": project_repo, "state_dir": d, "tick_timeout": tick_timeout}

# The Azeroth Rewind shape from the card's own incident: two lanes mid-tick, one launched
# well before the other, each with its own watchdog ceiling — the Ship desk's "lands by"
# is the LATER of the two deadlines, plus one timer period, never the first one it finds.
WAIT_LANES = [
    wait_lane("watcher", "roloc/watcher", 150, 30, 42),    # deadline: NOW + 120m
    wait_lane("riffmap", "roloc/riffmap", 60, 10, 777),    # deadline: NOW +  50m — not the latest
]
waiting = deploy_now()["waiting_for"]
check("every busy, paused-for-deploy lane is listed, project and card included",
      {(w["project"], w["lane"], w["card"]["number"]) for w in waiting}
      == {("watcher", "watcher", 42), ("riffmap", "riffmap", 777)}, waiting)
check("each one links straight to its own card",
      {w["card"]["url"] for w in waiting}
      == {"https://github.com/roloc/watcher/issues/42", "https://github.com/roloc/riffmap/issues/777"})
w42 = next(w for w in waiting if w["card"]["number"] == 42)
check("its own watchdog deadline: launched + that lane's own TICK_TIMEOUT, not a shared default",
      w42["deadline_at"] == (NOW + timedelta(minutes=120)).isoformat(), w42)
check("lands_by is the LATEST deadline, plus one timer period — never the first lane found",
      deploy_now()["lands_by"] == (NOW + timedelta(minutes=120, seconds=floor.DEPLOY_TIMER_SEC)).isoformat(),
      deploy_now()["lands_by"])

# A lane with a PAUSE file Steve made by hand (never "deploy <sha>") is not this — only the
# canary stage's own pause ever blames a deploy, card #106.
WAIT_LANES = [wait_lane("hand-paused", "roloc/watcher", 150, 5, 99)]
with open(os.path.join(WAITDIR, "hand-paused", "PAUSE"), "w") as f:
    f.write("")  # overwrite wait_lane's default: Steve's own empty, hand-made PAUSE
check("a hand-made PAUSE is never read as the deployer waiting on it",
      deploy_now()["waiting_for"] == [])

# Once the deploy is actually live, none of this applies — only the alive line does.
check("live: no step, nobody waited on, no lands-by — only the alive line, so the desk never looks frozen",
      floor.deploy_status("/x", DS, "roloc/watcher", git=fake_git(L, L), lanes=WAIT_LANES, now=NOW,
                          running_fn=lambda: False)
      == {"live": L[:7], "target": L[:7], "state": "live",
          "alive": {"running": False, "since_sec": 120, "next_in_sec": 180, "stale": False},
          "step": None, "waiting_for": [], "lands_by": None})
# Leave DS exactly as section 17 above left it (held/T="58", evals/T="fail 1") for the
# `dstate()`/`state17` code below, which this block's own `reset()` and lock/last-run/step
# files must not disturb.
reset()
os.remove(os.path.join(DS, "lock")); os.remove(os.path.join(DS, "last-run")); os.remove(os.path.join(DS, "step"))
open(os.path.join(DS, "evals", T), "w").write("fail 1")
open(os.path.join(DS, "held", T), "w").write("58")

# Card #106: a lane's own $WATCHER_STATE/release (bin/spawn.sh writes it at process start)
# is read for the Ship desk's per-lane view, box-wide — "riffmap" is on the live release,
# "riffmap-design" is still on the one before it, "watcher" has never polled since one was
# adopted at all (no file).
os.makedirs(os.path.join(STATE, "riffmap"), exist_ok=True)
os.makedirs(os.path.join(STATE, "riffmap-design"), exist_ok=True)
open(os.path.join(STATE, "riffmap", "release"), "w").write(L[:12])
open(os.path.join(STATE, "riffmap-design", "release"), "w").write(T[:12])

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
lane_releases = {l["lane"]: l["release"] for l in rm17["ship"]["lanes"]}
check("the ship desk names every lane on the box, not just this project's own",
      {"watcher", "riffmap", "riffmap-design", "riffmap-triage", "second", "third"} <= set(lane_releases),
      lane_releases)
check("a lane already on the live release names it", lane_releases["riffmap"] == L[:12])
check("a lane still on an older one names that instead", lane_releases["riffmap-design"] == T[:12])
check("a lane that has never recorded one is None, not a crash", lane_releases["watcher"] is None)
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

print("19b. card #154: OWNER_LOGIN — defaults to BOARD_OWNER, a fork can name its own")
OL_ROOT = tempfile.mkdtemp(prefix="watcher-floor-ownerlogin-test-")
OL_PROJECTS = os.path.join(OL_ROOT, "projects")
OL_STATE = os.path.join(OL_ROOT, "state")
os.makedirs(OL_PROJECTS)
os.makedirs(OL_STATE)


def ol_env(name, body):
    with open(os.path.join(OL_PROJECTS, f"{name}.env"), "w") as f:
        f.write(body)


ol_env("no-override", f"""\
PROJECT_NAME=no-override
PROJECT_ROOT={OL_ROOT}/repo
BOARD_OWNER=roloc
PROJECT_REPO=roloc/no-override
BOARD_NUMBER=20
""")
ol_env("forked", f"""\
PROJECT_NAME=forked
PROJECT_ROOT={OL_ROOT}/repo2
BOARD_OWNER=roloc
OWNER_LOGIN=octocat
PROJECT_REPO=roloc/forked
BOARD_NUMBER=21
""")

ol_lanes = {l["lane"]: l for l in floor.discover_lanes(OL_PROJECTS, OL_STATE)}
check("no OWNER_LOGIN line at all: falls back to BOARD_OWNER (the old form every board "
      "already has) rather than an empty login",
      ol_lanes["no-override"]["owner_login"] == "roloc", ol_lanes["no-override"])
check("OWNER_LOGIN set: a fork's own login, independent of BOARD_OWNER",
      ol_lanes["forked"]["owner_login"] == "octocat", ol_lanes["forked"])

ol_dir = lane_dir("forked")
write_log(ol_dir, 98, 5, ended_in="Stuck - Needs help", needs_steve="yes — pick one")
ol_lanes["forked"]["state_dir"] = ol_dir
ol_tick = metrics.load_lane(ol_dir)[-1]
ol_items = {98: {"number": 98, "url": "https://github.com/roloc/forked/issues/98",
                 "status": "Stuck - Needs help", "state": "OPEN"}}
ol_desk = floor.lane_desk(ol_lanes["forked"], ol_items, "https://board", NOW,
                           last_tick=ol_tick, ask=lambda n, r: "Needs @octocat: pick one",
                           images=lambda n, r: [])
check("a needs_steve desk carries the lane's own owner_login, for the badge to read "
      "'Needs @octocat' instead of the fixed word 'Steve'",
      ol_desk["pill"] == "needs_steve" and ol_desk["owner_login"] == "octocat", ol_desk)

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

print("20a. card #164: the canary lane's session is under its own config dir's projects/")
TS_CANARY = os.path.join(TS_ROOT, "claude-canary-projects")
os.makedirs(TS_CANARY)
check("tail.projects_dirs: the default dir, then spawn.sh's own canary config dir's projects/",
      tail.projects_dirs({"CLAUDE_PROJECTS_DIR": "/p"}) == ["/p", "/home/watcher/.claude-canary/projects"]
      and tail.projects_dirs({"CLAUDE_PROJECTS_DIR": "/p", "WATCHER_CANARY_CLAUDE_CONFIG_DIR": "/c"})
      == ["/p", "/c/projects"]
      and tail.projects_dirs({"CLAUDE_PROJECTS_DIR": "/p", "CLAUDE_CANARY_PROJECTS_DIR": "/x"}) == ["/p", "/x"]
      and tail.projects_dirs({"CLAUDE_PROJECTS_DIR": "/p", "CLAUDE_CANARY_PROJECTS_DIR": "/p"}) == ["/p"])
with open(os.path.join(TS_STATE, "current"), "w") as f:
    f.write("78\n")
ts_canary_dir = os.path.join(TS_CANARY,
                             tail.encode_dir(os.path.join(ts_info["worktree_base"], "card-78")))
os.makedirs(ts_canary_dir)
ts_canary_transcript = os.path.join(ts_canary_dir, "session-c.jsonl")
with open(ts_canary_transcript, "w") as f:
    f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"on the canary"}]}}\n')
check("looking only under the default dir, the canary lane reads as waiting — the bug",
      floor.tail_status(ts_info, TS_CLAUDE) == {"status": "waiting", "card": 78})
r10 = floor.tail_status(ts_info, [TS_CLAUDE, TS_CANARY])
check("looking under both, its live session is found and decoded",
      r10["status"] == "ok" and r10["card"] == 78 and r10["live"] is True
      and r10["lines"] == ["on the canary"] and r10["file"] == "session-c.jsonl", r10)
# The same card, worked earlier on the stable release: an OLDER session under the default
# dir must not shadow the live one on the canary.
ts_stable_dir = os.path.join(TS_CLAUDE, tail.encode_dir(os.path.join(ts_info["worktree_base"], "card-78")))
os.makedirs(ts_stable_dir)
ts_stable_transcript = os.path.join(ts_stable_dir, "session-s.jsonl")
with open(ts_stable_transcript, "w") as f:
    f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"on stable, earlier"}]}}\n')
os.utime(ts_stable_transcript, (1_600_000_000, 1_600_000_000))
r11 = floor.tail_status(ts_info, [TS_CLAUDE, TS_CANARY])
check("the newest session across both dirs wins, so the canary's live one still shows",
      r11["file"] == "session-c.jsonl" and r11["lines"] == ["on the canary"], r11)
# Idle after a tick on the canary: the last session is looked for under both dirs too.
os.remove(os.path.join(TS_STATE, "current"))
os.makedirs(os.path.join(TS_STATE, "logs"), exist_ok=True)
canary_log = os.path.join(TS_STATE, "logs", "card-78-20261015T120000Z.json")
with open(canary_log, "w") as f:
    f.write('{"subtype": "success"}')
ended78 = os.path.getmtime(canary_log)
os.utime(ts_canary_transcript, (ended78 - 3, ended78 - 3))
r12 = floor.tail_status(ts_info, [TS_CLAUDE, TS_CANARY])
check("idle after that tick: its last session is the canary one, marked not live",
      r12["status"] == "ok" and r12["card"] == 78 and r12["live"] is False
      and r12["file"] == "session-c.jsonl", r12)
check("...and with only the default dir it would read as idle with nothing to show",
      floor.tail_status(ts_info, TS_CLAUDE) == {"status": "idle"})
shutil.rmtree(os.path.join(TS_STATE, "logs"))  # 20b makes it itself
shutil.rmtree(ts_stable_dir)
with open(os.path.join(TS_STATE, "current"), "w") as f:
    f.write("77\n")

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
          'newest + "\\n" + pre.textContent', 'newest + "\n" + pre.textContent')))

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

print("25. card #98: Cache idle-skip — the background loop stops paying for a rebuild "
      "once nobody has asked in idle_after_sec, and the next get() pays exactly one "
      "synchronous read to catch back up")
build_calls = []


def counting_build():
    build_calls.append(1)
    return {"n": len(build_calls)}


fake_now = [1000.0]
real_time = floor.time.time
floor.time.time = lambda: fake_now[0]
try:
    c = floor.Cache(counting_build, refresh_sec=10, idle_after_sec=30)
    check("constructing the cache does exactly one build", len(build_calls) == 1, build_calls)

    fake_now[0] += 5  # a request 5s in — still well inside the idle window
    c.get()
    fake_now[0] += 20  # 25s since that request, still under idle_after_sec (30)
    c._maybe_background_rebuild()
    check("not idle yet: the background loop still rebuilds on its own clock",
          len(build_calls) == 2, build_calls)

    fake_now[0] += 40  # now 40s since the last get() — past idle_after_sec
    c._maybe_background_rebuild()
    check("idle past idle_after_sec: the background loop skips the rebuild, no GraphQL "
          "spent on a floor nobody is watching", len(build_calls) == 2, build_calls)

    # Someone opens the page again. The cached state (built at the last background
    # rebuild) is now older than refresh_sec, so this get() pays one synchronous read —
    # "the first request after a quiet spell pays one read" — rather than serving
    # whatever the idle background loop left behind indefinitely.
    got = c.get()
    check("the first request after the quiet spell triggers exactly one rebuild",
          len(build_calls) == 3, build_calls)
    check("...and returns the freshly rebuilt state, not the stale one", got == {"n": 3}, got)

    fake_now[0] += 1  # well inside refresh_sec of that rebuild
    c.get()
    check("a second get() right after does not rebuild again", len(build_calls) == 3, build_calls)
finally:
    floor.time.time = real_time

print("26. card #98: idle_after_sec=None (every cache but the board one) never skips — "
      "the old unconditional-background-refresh behavior, unchanged")
build_calls2 = []


def counting_build2():
    build_calls2.append(1)
    return {}


fake_now2 = [2000.0]
floor.time.time = lambda: fake_now2[0]
try:
    c2 = floor.Cache(counting_build2, refresh_sec=10)
    fake_now2[0] += 10000  # however long nobody has asked, idle_after_sec=None never skips
    c2._maybe_background_rebuild()
    check("idle_after_sec=None: the background loop still rebuilds, however long it has "
          "been since the last request", len(build_calls2) == 2, build_calls2)
finally:
    floor.time.time = real_time

print("26. the server room (October 9, 2026): the box, the lab's boxes, the tokens the lanes moved")
bs = floor.box_stats()
check("box_stats reads what it can and never raises; cpus is always there",
      isinstance(bs, dict) and bs["cpus"] and set(bs) == {"cpus", "load1", "mem_total", "mem_used", "disk_total", "disk_used", "uptime"}, bs)
check("every number it returns is a number (a Mac has no /proc, so some are None)",
      all(v is None or isinstance(v, (int, float)) for v in bs.values()), bs)
pve = floor.proxmox_summary([
    {"type": "node", "node": "proxmox", "status": "online", "cpu": 0.0096, "maxcpu": 8, "mem": 1, "maxmem": 2,
     "diskwrite": 999, "netin": 999},
    {"type": "lxc", "name": "agents", "vmid": 107, "status": "running", "cpu": 0.0083, "maxcpu": 4, "mem": 3, "maxmem": 4},
    {"type": "qemu", "name": "VM 100", "vmid": 100, "status": "running", "cpu": 0.0007, "maxcpu": 4, "mem": 5, "maxmem": 6},
    {"type": "storage", "storage": "local-lvm", "disk": 1}, {"type": "sdn", "sdn": "localnetwork"}])
check("proxmox_summary keeps nodes and guests, the node first then by vmid, and drops storage and sdn",
      [(g["type"], g["name"]) for g in pve] == [("node", "proxmox"), ("qemu", "VM 100"), ("lxc", "agents")], pve)
check("...and only the fields the page shows: no disk or network bytes",
      all(set(g) == {"name", "vmid", "type", "status", "cpu", "maxcpu", "mem", "maxmem"} for g in pve), pve)
check("fetch_proxmox is opt-in: with no PROXMOX_URL it is [] and never opens a socket",
      floor.fetch_proxmox(url="", token="") == [])
os.makedirs(os.path.join(STATE, "watcher", "logs"), exist_ok=True)
with open(os.path.join(STATE, "watcher", "logs", "card-41-20260101T000000Z.json"), "w") as f:
    json.dump({"subtype": "success", "total_cost_usd": 2.5, "num_turns": 30, "duration_ms": 1000,
               "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 1500, "service_tier": "x"},
               "result": "- Card: #41 x\n- Outcome: merged\n- Ended in: Done\n"}, f)
with open(os.path.join(STATE, "watcher", "logs", "card-42-20260101T000000Z.json"), "w") as f:
    json.dump({"subtype": "success", "total_cost_usd": 0.5, "num_turns": 3, "duration_ms": 1000,
               "usage": {"input_tokens": 100, "output_tokens": 100},
               "result": "- Card: #42 x\n- Outcome: parked\n- Ended in: Stuck - Needs help\n"}, f)
old_log = os.path.join(STATE, "watcher", "logs", "card-40-20200101T000000Z.json")
with open(old_log, "w") as f:
    json.dump({"subtype": "success", "total_cost_usd": 9, "num_turns": 9, "duration_ms": 1000,
               "usage": {"input_tokens": 5000, "output_tokens": 5000},
               "result": "- Card: #40 x\n- Outcome: merged\n- Ended in: Done\n"}, f)
os.utime(old_log, (0, 946684800))  # written in 2000: outside every window
boards26 = {("roloc", 2): [{"number": 41, "state": "CLOSED", "status": "Done", "title": "The thing shipped",
                            "labels": [], "url": "https://github.com/roloc/watcher/issues/41"}]}
state26 = floor.build_state(PROJECTS, STATE, lambda o, n: boards26.get((o, n), []), box_name="test-box",
                            box_stats_fn=lambda: {"cpus": 2, "load1": 0.5, "mem_total": 10, "mem_used": 5,
                                                  "disk_total": 20, "disk_used": 10, "uptime": 3600},
                            proxmox_fn=lambda: pve)
srv26 = state26["server"]
check("the state carries the box's numbers as given, and the lab's boxes",
      srv26["box"]["load1"] == 0.5 and srv26["proxmox"] == pve, srv26)
tok = srv26["tokens"]
check("tokens: every token the last day's ticks moved, per board and per hour — a tick from "
      "2000 counts for nothing, and a non-number in usage is skipped",
      tok["hours"] == 24 and tok["total"] == 3200 and tok["per_hour"] == 133
      and tok["per_project"] == [{"name": "watcher", "tokens": 3200}], tok)
wins26 = state26["wins"]
check("wins: the card that ended in Done this week, with its title from the board — not the one "
      "that parked, not the one from 2000",
      wins26["days"] == 7 and [w["card"]["number"] for w in wins26["items"]] == [41]
      and wins26["items"][0]["title"] == "The thing shipped" and wins26["items"][0]["project"] == "watcher", wins26)
check("a proxmox read that fails leaves the room without the lab, with one journal line, never a crash",
      floor.build_state(PROJECTS, STATE, lambda o, n: [], box_name="b",
                        proxmox_fn=lambda: (_ for _ in ()).throw(RuntimeError("down")))["server"]["proxmox"] == [])
check("no proxmox_fn, no box_stats_fn: the room is empty but present",
      floor.build_state(PROJECTS, STATE, lambda o, n: [], box_name="b")["server"]["box"] == {})
check("the new fields are all allowed ones", set(keys(state26)) <= floor.ALLOWED_FIELDS,
      set(keys(state26)) - floor.ALLOWED_FIELDS)
for n in (40, 41, 42):
    for f in glob.glob(os.path.join(STATE, "watcher", "logs", f"card-{n}-*.json")):
        os.remove(f)

print("27. the bulletin board's Peek: /api/card.json, a card read off GitHub, cached a minute")
calls27 = []
def fake_card(number, repo):
    calls27.append((number, repo))
    return {"title": f"Card {number}", "body": "b", "state": "OPEN", "url": "u", "labels": [], "comments": [],
            "created": "", "closed": ""} if number == 7 else None
clock27 = [1000.0]
look27 = floor.make_card_lookup(lambda: {"alpha": "roloc/alpha"}, fetcher=fake_card, cache_sec=60, clock=lambda: clock27[0])
c = look27("alpha", 7)
check("a card comes back with its project and number added", c["title"] == "Card 7" and c["project"] == "alpha" and c["number"] == 7, c)
look27("alpha", 7)
check("the second read within a minute is the cache, not gh", calls27 == [(7, "roloc/alpha")], calls27)
clock27[0] += 61
look27("alpha", 7)
check("after a minute it reads again", len(calls27) == 2, calls27)
check("an unknown project is None without a gh call", look27("nope", 7) is None and len(calls27) == 2)
check("a card gh cannot read is None", look27("alpha", 9) is None)
srv27 = ThreadingHTTPServer(("127.0.0.1", 0), floor.make_handler(FixedCache(), None, None, look27))
port27 = srv27.server_address[1]
threading.Thread(target=srv27.serve_forever, daemon=True).start()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port27}/api/card.json?project=alpha&number=7", timeout=5) as r:
        check("/api/card.json serves the card", json.loads(r.read())["title"] == "Card 7")
    with urllib.request.urlopen(f"http://127.0.0.1:{port27}/api/card.json?project=alpha&number=9", timeout=5) as r:
        check("a card it cannot read is a 200 that says so (a 404 would read as the page breaking)",
              json.loads(r.read()) == {"unreadable": True})
    with urllib.request.urlopen(f"http://127.0.0.1:{port27}/api/card.json?project=alpha&number=x", timeout=5) as r:
        check("a number that is not one is the same answer, not a traceback", json.loads(r.read()) == {"unreadable": True})
finally:
    srv27.shutdown()
    srv27.server_close()
srv27b = ThreadingHTTPServer(("127.0.0.1", 0), floor.make_handler(FixedCache()))
port27b = srv27b.server_address[1]
threading.Thread(target=srv27b.serve_forever, daemon=True).start()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port27b}/api/card.json?project=alpha&number=7", timeout=5) as r:
        check("with no card lookup wired, the endpoint says unreadable rather than 404", json.loads(r.read()) == {"unreadable": True})
finally:
    srv27b.shutdown()
    srv27b.server_close()

print("28. card #158: projects/ holding only the shipped examples draws an empty floor, not an error")
EXAMPLES_ONLY = tempfile.mkdtemp(prefix="watcher-floor-examples-")
EXAMPLES_STATE = tempfile.mkdtemp(prefix="watcher-floor-examples-state-")
REPO_PROJECTS_DIR = os.path.join(os.path.dirname(HERE), "projects")
for fn in ("example.env.txt", "example-triage.env.txt", "example.detect.env.txt", "README.md"):
    shutil.copy(os.path.join(REPO_PROJECTS_DIR, fn), os.path.join(EXAMPLES_ONLY, fn))
lanes28 = floor.discover_lanes(EXAMPLES_ONLY, EXAMPLES_STATE)
check("the .txt-named examples (and the README) are never read as a lane", lanes28 == [], lanes28)


def _no_board_calls(*_a, **_k):
    raise AssertionError("board_fetcher must never be called with zero lanes")


state28 = floor.build_state(EXAMPLES_ONLY, EXAMPLES_STATE, _no_board_calls)
check("build_state draws an empty floor rather than raising", state28["projects"] == [], state28)
check("...with a sane, empty header, not a crash or a stale value",
      state28["header"]["needs_steve_total"] == 0 and state28["header"]["owner_login"] == "",
      state28["header"])
shutil.rmtree(EXAMPLES_ONLY, ignore_errors=True)
shutil.rmtree(EXAMPLES_STATE, ignore_errors=True)

shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(WD_ROOT, ignore_errors=True)
shutil.rmtree(TS_ROOT, ignore_errors=True)

print(f"{count} cases, {'all passed' if not fails else 'FAILURES'}")
sys.exit(fails)
