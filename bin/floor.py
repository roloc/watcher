#!/usr/bin/env python3
"""A live, read-only page for the agents box: what every lane is doing right now, without
ssh or journalctl (card #40 — mocked up with Steve on September 28, 2026).

    python3 bin/floor.py [--host 0.0.0.0] [--port 8787]
                          [--projects-dir DIR] [--state-root DIR] [--refresh-sec N]

One page for the whole box, not one per project: it groups every lane in `projects/*.env`
by the board it works, and for each lane shows one of exactly five states — **working**
(a card, its Stage, minutes since launch, and whether its plan comment is up yet — amber,
the same as Ship reads when something is blocked, once it has worked past
`PLAN_LATE_MIN` minutes with none; card #103), **idle** (why), **needs Steve** (the card, the
first line of its ask, and — when its Stuck comment points at a mockup or a screenshot —
a Review button that opens it full-window, zoomable and flippable, on the floor and in
the studio alike, so approving it never means clicking through to the card first; card
#91, reshaped by card #100), **paused** (a `PAUSE` file, and no card held), or **sleeping**
(a died tick, inside the spawner's own ten-minute retry sleep — `how-it-works.md`,
"step 8"). A lane holding a card still reads **working** even with a `PAUSE` file beside
it — paused means it takes nothing new, never that it is idle — and carries a short
"pausing" line saying why (card #110). A "Recent
ticks" list, across every project, closes the loop: project, lane, card, when, quota. A
**Peek** button on **every** lane's desk opens a panel decoded from that lane's own Claude
Code session transcript, the same way `bin/tail.sh` decodes it on the command line (card
#63: "view these things from the floor page ... what their claude code session looks
like") — live while the lane is working, its last session when it is not — polled only
while a human has it open, never while the panel is closed. (Until October 1, 2026 the
button existed only on a desk that was working at that moment, so on a quiet floor there
was no sign the feature had shipped at all.) The **ship station** has a Peek too: every
step CI ran on main's newest commit, the commits before it, and — on the board whose repo
this box deploys — what the deployer is doing and the last things it said. While a PR's own
check is still running — the wait before every merge, and the one window a push-only
history can never show, since the PR's commit is not on main yet — that run leads instead
(card #77: "the CI windows [...] doesn't work for those peeks").

Where the data comes from (`how-it-works.md`, "State on disk" and "Lanes"):
- `projects/*.env`, sourced with bash exactly as `spawn.sh` sources them (they use shell
  interpolation — `riffmap.env`'s `LANE_AGENTS="!${DESIGN_AGENTS},-"` — so this reads them
  as bash, never re-implements the syntax), for which lanes exist and their board/repo.
- each lane's state directory for `current`, `PAUSE`, and `logs/card-*.json` (reusing
  `bin/metrics.py`'s log loader — no second parser for the same file shape).
- one `python3 bin/board.py dump` per **board** (several lanes can share one — riffmap's
  three do), cached and refreshed every `--refresh-sec` (default 60), like the spawner's
  own poll. Never one call per page view.
- `systemctl is-active <unit>` for whether the lane's unit is up at all.
- for Peek, the lane's own session transcript under `~/.claude/projects/<cwd>/<session>.jsonl`
  (`CLAUDE_PROJECTS_DIR` to override), decoded by `bin/tail.py`'s own functions — reused,
  never re-implemented, so the two never drift apart on what a line means.

Read-only, and nothing but: no endpoint here can change a card, a `PAUSE` file, or
anything else. The JSON endpoint (`/api/state.json`) serves exactly the fields in
`ALLOWED_FIELDS` below — never a raw file, an env value, a log's `result` text, or stderr.
`/api/tail.json` (Peek) is a separate, narrower contract: `tail_status`'s own docstring
says exactly what it returns — one short decoded line at a time, the same ones `tail.py`
would print, never a raw file or a tool's full output. `/api/ship.json` (the ship
station's Peek) is `ship_lines`'s output: names, marks and times of CI's steps from
GitHub's REST API (two or three calls, shared by every poll for `SHIP_CACHE_SEC`), and the
deployer's own `history` file — never a CI log, and only for a project already on the page.

`bin/test-floor.py` is the offline suite: fixture state directories and a stubbed board,
no network, no real systemd. It checks the Python. **`bin/test-floor-browser.py` checks
the page**: it loads `INDEX_HTML` in a real headless browser and clicks it. Everything
below `INDEX_HTML = ` is a program that only ever runs in a browser, so that suite is the
only one that runs it — #70 shipped a `<script>` block that did not parse, with all 115
checks of the other suite green. Run both after touching this file.
"""
import glob
import json
import os
import calendar
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import metrics  # noqa: E402  (bin/metrics.py, beside this file — reused for the log parser)
import tail  # noqa: E402  (bin/tail.py, beside this file — reused for the Peek decode)

STUCK_STATUS = "Stuck - Needs help"  # board.py's own constant; kept as a literal here so
                                      # this file never imports board.py (which reads
                                      # BOARD_OWNER/BOARD_NUMBER as module-level globals —
                                      # wrong for a page that talks to several boards).
SLEEP_AFTER_FAILURE_MIN = 10  # spawn.sh: `wsleep 600` after any non-success exit
PLAN_LATE_MIN = 10  # watch.md §2: the plan comment is the tick's first act — a working
                     # desk still with none past this many minutes reads as the lane's own
                     # warning, not a hung lane with no sign to tell the two apart (card #103)
RECENT_PER_LANE = 20   # ticks kept per lane before the global list is trimmed
RECENT_TOTAL = 15      # ticks shown on the page, across every project

ENV_VARS = ("PROJECT_NAME", "PROJECT_ROOT", "BOARD_OWNER", "PROJECT_REPO", "BOARD_NUMBER",
            "LANE_AGENTS", "LANE_COMMAND", "WATCHER_STATE", "LANE_WORKDIR",
            # card #105: bin/spawn.sh's own watchdog ceiling, in minutes — read so the Ship
            # desk's "lands by" can compute a busy canary lane's own deadline the same way
            # the spawner would, rather than assuming every lane uses the 150-minute default.
            "TICK_TIMEOUT")

# Every key the JSON endpoint may ever emit. `test-floor.py` walks a real response and
# fails if it finds one outside this set — the rule from the card ("serves only the fields
# above") checked in code, not just read.
ALLOWED_FIELDS = {
    "header", "box", "time", "needs_steve_total",
    "projects", "name", "board_url", "counts", "ready", "in_progress", "backlog",
    "lanes", "board_error",
    "lane", "kind", "unit", "unit_active", "pill", "link", "card", "stage", "minutes",
    "reason", "ask", "images", "number", "url",
    # card #103: whether the tick's plan comment is up yet, and whether it has gone past
    # PLAN_LATE_MIN with none
    "plan_posted", "plan_late",
    "recent", "project", "when", "quota",
    # the ship station (after the lanes): CI on main, and the box's own deployer
    "ship", "ci", "deploy", "state", "sha", "failing", "live", "target",
    # card #106: which release each lane on the box is pinned to, box-wide
    "release",
    # card #105: which step a deploy is on, how fresh the deployer's last check was, which
    # canary lane(s) it is waiting on (with a real link to each one's card), and when it
    # lands at the latest
    "step", "detail", "alive", "running", "since_sec", "next_in_sec", "stale",
    "waiting_for", "launched_at", "deadline_at", "lands_by",
    # the Needs Steve strip (card #96): every Stuck card on the board, whichever lane last
    # touched it, with every round it has been asked, newest first
    "stuck", "rounds", "label", "letter", "recommended",
    # the account's GraphQL budget (card #98), read from board.py's own cache — never a
    # call of its own
    "github_remaining", "github_reset", "github_limit",
    # this hour's spend by caller (card #114), from the ledger — same free ride
    "github_points_per_min", "github_spend", "caller", "points",
    # the server room (October 9, 2026): the box itself, the tokens the lanes moved, and —
    # when the floor is given a Proxmox token — the lab's other boxes
    "server", "cpus", "load1", "mem_total", "mem_used", "disk_total", "disk_used", "uptime",
    "tokens", "hours", "total", "per_hour", "per_project",
    "proxmox", "vmid", "type", "status", "cpu", "maxcpu", "mem", "maxmem",
    # the wins board: every card a lane took to Done this week, with its title from the board
    "wins", "title", "days", "items",
    # card #110: a working lane that is also pausing (a PAUSE file sits beside a held card)
    "pausing", "pausing_reason",
}

WINS_DAYS = 7       # the wins board's window
TOKENS_HOURS = 24   # the server room's tokens-per-hour window


# --- reading projects/*.env, the way spawn.sh does --------------------------------------

def read_env_file(path, timeout=10):
    """Source `path` in bash (`set -a` first, exactly as `spawn.sh` sources a project file)
    and print back only the vars this page needs. A project file is shell — interpolation,
    a leading `. "$(dirname …)/riffmap.env"` — so this is the only way to read one that
    does not drift from what actually runs."""
    script = (
        'set -a\n'
        'source "$1" >/dev/null 2>&1\n'
        + "".join(f'printf \'%s\\x1f%s\\x1e\' "{v}" "${{{v}:-}}"\n' for v in ENV_VARS)
    )
    try:
        r = subprocess.run(["bash", "-c", script, "_", path],
                            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for rec in (r.stdout or "").split("\x1e"):
        if "\x1f" not in rec:
            continue
        k, _, v = rec.partition("\x1f")
        out[k] = v
    return out


def discover_lanes(projects_dir, state_root):
    """One entry per `projects/*.env` file — one entry per systemd instance (`watcher@<name>`),
    which is what `how-it-works.md` calls a lane. A file that sources another one (the
    design and triage lanes source their project's own file) still reads its own
    `PROJECT_NAME`/`LANE_AGENTS`, because bash re-sources each file from scratch."""
    lanes = []
    for path in sorted(glob.glob(os.path.join(projects_dir, "*.env"))):
        # A lane file is `<name>.env` with no dot in `<name>`, the deployer's rule
        # (deploy/autodeploy.sh, is_lane_file): `<name>.detect.env` and the like are a
        # project's own config, never a lane.
        if "." in os.path.basename(path)[: -len(".env")]:
            continue
        env = read_env_file(path)
        if not env.get("PROJECT_REPO") or not env.get("BOARD_OWNER"):
            continue  # not a real project file
        # PROJECT_NAME, not the filename: `spawn.sh` keys WATCHER_STATE and the systemd
        # instance (`watcher@<PROJECT_NAME>`) on it, and every project file's own comment
        # requires the two to match — but the filename is what a tick would guess wrong if
        # that ever slipped.
        name = env.get("PROJECT_NAME") or os.path.splitext(os.path.basename(path))[0]
        try:
            board_number = int(env.get("BOARD_NUMBER") or 1)
        except ValueError:
            board_number = 1
        # Same derivation as `tail.sh`: a bad or missing LANE_WORKDIR reads as "card" rather
        # than failing the whole page — `tail.sh` is strict about this because a human typed
        # the lane name, but one bad project file here must not take down every other lane's
        # desk.
        project_root = env.get("PROJECT_ROOT") or ""
        workdir_mode = env.get("LANE_WORKDIR") or "card"
        if workdir_mode not in ("card", "root"):
            workdir_mode = "card"
        if workdir_mode == "root":
            worktree_base = project_root
        else:
            worktree_base = os.path.join(os.path.dirname(project_root),
                                          os.path.basename(project_root) + "-worktrees")
        try:
            tick_timeout = int(env.get("TICK_TIMEOUT") or 150)
        except ValueError:
            tick_timeout = 150
        lanes.append({
            "lane": name,
            "unit": f"watcher@{name}",
            "project_repo": env["PROJECT_REPO"],
            "board_owner": env["BOARD_OWNER"],
            "board_number": board_number,
            "lane_agents": env.get("LANE_AGENTS") or "",
            "lane_command": env.get("LANE_COMMAND") or "/watch",
            "state_dir": env.get("WATCHER_STATE") or os.path.join(state_root, name),
            "workdir_mode": workdir_mode,
            "worktree_base": worktree_base,
            "tick_timeout": tick_timeout,
        })
    return lanes


def lane_kind(lane):
    """Build / Design / Triage, from the same two knobs `how-it-works.md`'s "Lanes" table
    reads (`LANE_COMMAND`, `LANE_AGENTS`) — never a project's own names, so this stays
    generic the way `commands/watch.md` has to."""
    if lane["lane_command"] == "/triage":
        return "Triage"
    spec = lane["lane_agents"]
    if spec and not spec.startswith("!"):
        return "Design"
    return "Build"


# SDLC order on the page: the dispatcher first, then design, then build (and the ship
# station after them all, which is not a lane).
KIND_ORDER = {"Triage": 0, "Design": 1, "Build": 2}


def lane_takes(agent, spec):
    """Port of `spawn.sh`'s `lane_takes`: is a card with this `agent:` label (`"-"` for
    none — `board list`'s own convention, which `spawn.sh` compares against directly) this
    lane's to pick? Kept in lockstep with that function's rules — unset `spec` takes
    everything, a `!`-prefixed list takes everything NOT named, otherwise exactly the
    names listed."""
    agent = agent or "-"
    if not spec:
        return True
    negate = spec.startswith("!")
    if negate:
        spec = spec[1:]
    names = {n.strip() for n in spec.split(",") if n.strip()}
    listed = agent in names
    return listed != negate


def agent_of(labels):
    """Mirrors `board.py`'s own `agent_of`, except for the empty case: `spawn.sh` never
    sees `""` for "no label", only the `-` `board list` prints, so `lane_takes` above has
    to be handed the same sentinel."""
    for l in labels or []:
        if l.startswith("agent: "):
            return l.split(": ", 1)[1]
    return "-"


def group_by_project(lanes):
    projects = {}
    for lane in lanes:
        key = (lane["board_owner"], lane["board_number"])
        p = projects.setdefault(key, {
            "name": lane["project_repo"].split("/")[-1],
            "board_owner": lane["board_owner"],
            "board_number": lane["board_number"],
            "board_url": f"https://github.com/users/{lane['board_owner']}/projects/{lane['board_number']}",
            "project_repo": lane["project_repo"],
            "lanes": [],
        })
        p["lanes"].append(lane)
    return sorted(projects.values(), key=lambda p: p["name"])


# --- the board, one call per project --------------------------------------------------

def fetch_board(owner, number, board_py=None, timeout=60):
    """`python3 bin/board.py dump`, with BOARD_OWNER/BOARD_NUMBER set for this project —
    one GraphQL round trip for every field any lane's pill needs. Raises on failure; the
    caller renders that project with an empty board rather than crashing the whole page."""
    board_py = board_py or os.path.join(HERE, "board.py")
    # WATCHER_LEDGER_CALLER="floor" (card #114): the one board.py invocation this file
    # makes that can actually trigger a live GraphQL call (the others below are cache or
    # `gh issue view` reads that never touch the ledger at all) — so this is the one
    # caller name the ledger needs from here.
    env = dict(os.environ, BOARD_OWNER=owner, BOARD_NUMBER=str(number), WATCHER_LEDGER_CALLER="floor")
    r = subprocess.run([sys.executable, board_py, "dump"], capture_output=True, text=True,
                        env=env, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "board.py dump failed").strip()[:300])
    return json.loads(r.stdout)


def project_counts(items):
    counts = {"ready": 0, "in_progress": 0, "backlog": 0}
    for it in items:
        if it.get("state") != "OPEN":
            continue
        st = (it.get("status") or "").strip().lower()
        if st == "ready":
            counts["ready"] += 1
        elif st == "in progress":
            counts["in_progress"] += 1
        elif st == "backlog":
            counts["backlog"] += 1
    return counts


# --- the ship station: CI on main, and the box's own deployer ----------------------------

def _gh_api(path, timeout=30):
    """One `gh api` GET — REST only, never GraphQL, which the board dump already spends.
    Raises on any failure, without gh's own error text in the message."""
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("gh api failed")
    return json.loads(r.stdout)


def fetch_ci(repo, branch="main", timeout=30, get=None):
    """The newest commit on `branch` and its check runs. Raises on any failure; the caller
    shows "unknown" and never the error text."""
    get = get or (lambda path: _gh_api(path, timeout))
    commit = get(f"repos/{repo}/commits/{branch}")
    runs = get(f"repos/{repo}/commits/{commit['sha']}/check-runs?per_page=100").get("check_runs", [])
    return {"sha": commit["sha"],
            "runs": [(x.get("name", ""), x.get("status", ""), x.get("conclusion")) for x in runs]}


def ci_summary(ci, repo):
    """passing | running | failing | none | unknown. A failed check wins over running ones:
    red is what someone needs to see first."""
    if not ci:
        return {"state": "unknown"}
    runs = ci["runs"]
    failing = sorted({n for n, st, c in runs
                      if st == "completed" and c not in ("success", "skipped", "neutral")})
    if failing:
        state = "failing"
    elif any(st != "completed" for _, st, _ in runs):
        state = "running"
    elif runs:
        state = "passing"
    else:
        state = "none"
    out = {"state": state, "sha": ci["sha"][:7],
           "url": f"https://github.com/{repo}/commit/{ci['sha']}"}
    if failing:
        out["failing"] = failing[:5]
    return out


def _git(repo_dir, *args):
    try:
        r = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def watcher_repo(repo_dir, git=_git):
    """owner/repo of the checkout this page runs from — the one the box's deployer ships."""
    url = git(repo_dir, "remote", "get-url", "origin")
    for prefix in ("git@github.com:", "https://github.com/"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url[:-4] if url.endswith(".git") else url


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# Card #105: deploy/watcher-deploy.service is a Type=oneshot unit with no RemainAfterExit,
# so for the whole length of a run `systemctl is-active` reports "activating", never
# "active" (found in review, checked against the live box mid-run: `ActiveState=activating`
# for the entire replay suite) — `systemctl_is_active`'s `is-active --quiet` succeeds only
# for "active" and so is always False here, the opposite of what the Ship desk needs.
# `deploy_service_running`, below, is the dedicated check: "activating" (or "reloading")
# counts as running, same as "active" would for a long-lived unit. The lock file's own
# mtime (`exec 9> "$DSTATE/lock"` touches it at the START of every run, whether or not that
# run finds anything new to do) is "when it was last checked" either way.
DEPLOY_SERVICE_UNIT = "watcher-deploy.service"
# deploy/watcher-deploy.timer's own OnUnitInactiveSec. A constant, not a live systemd read:
# the timer's own schedule only changes with a commit to that file, which this mirrors.
DEPLOY_TIMER_SEC = int(os.environ.get("DEPLOY_TIMER_SEC") or 300)


def deploy_service_running(unit=DEPLOY_SERVICE_UNIT, timeout=5):
    """Whether deploy/autodeploy.sh's own oneshot unit is actually executing right now.
    Never `systemctl_is_active` (below): that one's `is-active --quiet` only succeeds for
    the single state "active", which a Type=oneshot unit with no RemainAfterExit reaches
    only for an instant, if ever — it reports "activating" for the whole length of a run
    (found in review, against the live box). `systemctl is-active <unit>`, without
    `--quiet`, prints that state word to stdout regardless of its exit code; "activating"
    and "reloading" both count as running, same as a long-lived unit's own "active"."""
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (r.stdout or "").strip() in ("active", "activating", "reloading")


def deploy_alive(deploy_state, now, running_fn, period_sec=DEPLOY_TIMER_SEC):
    """live | between runs, for the Ship desk's freshness line — None when the deployer has
    never run on this box at all (no lock file yet). `since_sec` is always how long ago the
    lock file was last touched; `next_in_sec`/`stale` only mean anything between runs (mid-
    run, there is no "next" to count down to — the desk reads "running now" instead).
    `running_fn` is `() -> bool|None` — `deploy_service_running`, above, in production."""
    try:
        mtime = os.path.getmtime(os.path.join(deploy_state, "lock"))
    except OSError:
        return None
    running = bool(running_fn()) if running_fn else False
    since = max(now.timestamp() - mtime, 0)
    out = {"running": running, "since_sec": int(since)}
    if not running:
        out["next_in_sec"] = max(int(period_sec - since), 0)
        out["stale"] = since > 2 * period_sec
    return out


def read_deploy_step(deploy_state):
    """What deploy/autodeploy.sh last wrote to `$DSTATE/step` on entering it — card #105's
    one fact for "which step, out of how many" — or None between deploys (cleared once the
    target is live or held) and for a write caught mid-rename (the next read sees the
    finished one; `write_step` renames into place so there is nothing else to catch)."""
    try:
        with open(os.path.join(deploy_state, "step")) as f:
            lines = f.read().split("\n")
    except OSError:
        return None
    if len(lines) < 4:
        return None
    try:
        number, total = int(lines[0]), int(lines[1])
    except ValueError:
        return None
    return {"number": number, "total": total, "name": lines[2], "detail": lines[3]}


def deploy_status(repo_dir, deploy_state, repo, git=_git, lanes=(), now=None, running_fn=None):
    """What deploy/autodeploy.sh is doing, from the files it keeps (read-only):
    live | checking | testing | held. Card #106 means an ordinary release never pauses or
    waits for a lane — it clones the target, installs it, and flips the pointer inside one
    run — so `live`/`target` already say most of it, and `lane_release`/the page's per-lane
    lines (below) say which lanes have already followed the pointer there.

    Card #105 adds the rest of the Ship desk's Peek: `step` (`read_deploy_step`, above —
    None when `live`), `alive` (`deploy_alive`, above — always present, even when `live`,
    so the desk never looks frozen between deploys), `waiting_for` (every lane a PAUSE file
    blames on "deploy <sha>" that is still mid-tick — only ever the canary stage's own
    pause, card #46, since card #106; an ordinary release waits on nothing), and `lands_by`
    (the latest such lane's own watchdog deadline, plus one timer period — None when
    nothing is being waited on, which reads as "landing on the next check")."""
    now = now or datetime.now(timezone.utc)
    live = git(repo_dir, "rev-parse", "HEAD")
    target = git(repo_dir, "rev-parse", "origin/main")
    if not live:
        return None
    out = {"live": live[:7], "target": (target or live)[:7]}
    held = _read(os.path.join(deploy_state, "held", target)) if target else None
    evals = _read(os.path.join(deploy_state, "evals", target)) if target else None
    # Live comes before held: a commit shipped with deploy-anyway keeps its own held/<sha>
    # file (the deployer only clears the OTHER held cards), and the page must not call a
    # live commit Blocked, or count it as needing Steve.
    if not target or live == target:
        out["state"] = "live"
    elif held is not None:
        out["state"] = "held"
        if held.isdigit():
            out["card"] = {"number": int(held), "url": f"https://github.com/{repo}/issues/{held}"}
    elif evals and evals.startswith("fail"):
        out["state"] = "testing"
    else:
        out["state"] = "checking"

    out["alive"] = deploy_alive(deploy_state, now, running_fn)
    out["step"] = read_deploy_step(deploy_state) if out["state"] != "live" else None
    waiting = []
    if out["state"] != "live":
        for lane in lanes:
            reason = _pause_reason(os.path.join(lane["state_dir"], "PAUSE"))
            if not reason.startswith("deploy "):
                continue
            current_path = os.path.join(lane["state_dir"], "current")
            n = _read_current(current_path)
            if n is None:
                continue
            try:
                launched = datetime.fromtimestamp(os.path.getmtime(current_path), tz=timezone.utc)
            except OSError:
                continue
            deadline = launched + timedelta(minutes=lane.get("tick_timeout") or 150)
            waiting.append({
                "project": lane["project_repo"].split("/")[-1], "lane": lane["lane"],
                "card": {"number": n, "url": f"https://github.com/{lane['project_repo']}/issues/{n}"},
                "launched_at": launched.isoformat(), "deadline_at": deadline.isoformat(),
            })
    out["waiting_for"] = waiting
    out["lands_by"] = (
        (max(datetime.fromisoformat(w["deadline_at"]) for w in waiting)
         + timedelta(seconds=DEPLOY_TIMER_SEC)).isoformat()
        if waiting else None)
    return out


def lane_release(lane):
    """The release folder (its bare directory name, a commit's short sha) this lane's own
    process last said it was pinned to — bin/spawn.sh's own $WATCHER_STATE/release, written
    once at process start (see its comment there). None when the lane has never run under
    a release layout (a box, or a project, not using one) or has not polled since."""
    return _read(os.path.join(lane["state_dir"], "release"))


# --- one lane's desk ---------------------------------------------------------------------

def _read_current(path):
    try:
        with open(path) as f:
            s = f.read().strip()
    except OSError:
        return None
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _minutes_since(when, now):
    return max(int((now - when).total_seconds() // 60), 0)


def _idle_reason(lane, items_by_number, last_tick):
    """Plain words for why the desk is empty right now — never the internal name of
    anything (`docs/talking-to-steve.md`, rule 8). Best-effort, not a re-implementation of
    the spawner's own pick: it only has to be honest about what it can see."""
    pending = any(
        it.get("state") == "OPEN"
        and lane_takes(agent_of(it.get("labels")), lane["lane_agents"])
        and (it.get("status") or "").strip().lower() in ("in progress", "ready")
        for it in items_by_number.values()
    )
    if pending:
        return "Something is ready to pick up."
    if last_tick is not None and last_tick.ended_in == "Done":
        return f"Last shipped #{last_tick.issue}."
    return "Nothing to pick up right now."


def _pause_reason(path):
    """The first line of a `PAUSE` file, or `""` for an empty (hand-touched) one — never
    more than that line, so a multi-line file can never leak past it (there is no reason
    one would have more than one line; this is a floor not a parser)."""
    text = _read(path)
    if not text:
        return ""
    return text.splitlines()[0].strip()


def _plan_posted_since(stamp, launched):
    """True only when a plan comment's own timestamp (`board.py plan`'s answer — "" for
    none) is at or after `launched`, this lane's own pick-up of its current card. Without
    this, a card a second tick resumes (Stuck, or a crash) still carries the FIRST tick's
    plan comment, and the resuming tick — which has not posted its own yet — reads as
    already covered by one that is not its own (card #103, reviewer finding). `launched`
    of `None` (the state directory's own `current` file was unreadable) or a stamp this
    cannot parse both trust the plan at face value, rather than turning a read error into
    a false "no plan"."""
    if not stamp:
        return False
    if launched is None:
        return True
    try:
        ts = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return True
    return ts >= launched


def lane_desk(lane, items_by_number, board_url, now, last_tick=None, unit_active=None, ask=None,
              images=None, plan=None):
    """One lane's pill: exactly one of working | idle | needs_steve | paused | sleeping.
    `last_tick` is the lane's own newest log entry (a `metrics.Tick`, or None — loaded
    once by the caller, not re-read here: a lane's own `Cache` refresh used to parse every
    log file twice, once for this and once for the recent-ticks list). `ask` is a callable
    `(issue, project_repo) -> str` (board.py's `needs_steve_ask`, injected so tests never
    touch the network); `images` is the same shape for `needs_steve_images` — a mockup or
    a screenshot to show right on the desk (card #91), never required to be called for a
    pill other than needs_steve; `unit_active` is `systemctl is-active`'s answer, already
    looked up by the caller (None when it could not be checked). `plan` is the same shape
    for `board.py plan` (card #103): called only for a working desk, and only when given —
    a caller with nothing to inject (an older demo fixture, a test of something else) gets
    the old shape back, no `plan_posted`/`plan_late` fields at all, rather than a guessed
    "no plan" on a lane this was never asked about.

    A held card always wins the pill (card #110): a `PAUSE` file beside a held card never
    means idle — it means the lane will not pick up anything new once this tick ends. So
    `current` is read before `PAUSE`, and a lane holding a card gets `pill: working`
    whether or not `PAUSE` sits beside it; `PAUSE` only adds `pausing: true` and
    `pausing_reason` (the file's own first line — "deploy <sha>" for the deployer and the
    canary stage, "" for a hand-touched one) for the page script to turn into plain
    words. Only a lane holding nothing reads as `paused`."""
    base = {"lane": lane["lane"], "kind": lane_kind(lane), "unit": lane["unit"]}
    if unit_active is not None:
        base["unit_active"] = unit_active
    state_dir = lane["state_dir"]
    pause_path = os.path.join(state_dir, "PAUSE")
    paused = os.path.exists(pause_path)

    current_path = os.path.join(state_dir, "current")
    n = _read_current(current_path)
    if n is not None:
        launched = None
        try:
            launched = datetime.fromtimestamp(os.path.getmtime(current_path), tz=timezone.utc)
            minutes = _minutes_since(launched, now)
        except OSError:
            minutes = 0
        it = items_by_number.get(n) or {}
        url = it.get("url") or board_url
        desk = {**base, "pill": "working", "link": url, "card": {"number": n, "url": url},
                "stage": it.get("stage") or "", "minutes": minutes}
        if plan is not None:
            posted = _plan_posted_since(plan(n, lane["project_repo"]), launched)
            desk["plan_posted"] = posted
            desk["plan_late"] = (not posted) and minutes >= PLAN_LATE_MIN
        if paused:
            desk["pausing"] = True
            desk["pausing_reason"] = _pause_reason(pause_path)
        return desk

    if paused:
        return {**base, "pill": "paused", "link": board_url}

    # The board is authoritative over the lane's own log: a card the SPAWNER parked (two
    # died ticks, no Stuck-shaped comment from the tick itself) is just as much "needs
    # Steve" as one the tick parked itself, so this is checked before — not only when —
    # the last tick "died" is false.
    if last_tick is not None:
        it = items_by_number.get(last_tick.issue) or {}
        if (it.get("status") or "").strip() == STUCK_STATUS:
            url = it.get("url") or board_url
            text = ask(last_tick.issue, lane["project_repo"]) if ask else ""
            pics = images(last_tick.issue, lane["project_repo"]) if images else []
            return {**base, "pill": "needs_steve", "link": url,
                    "card": {"number": last_tick.issue, "url": url}, "ask": text or "",
                    "images": pics or []}

    if last_tick is not None and last_tick.died:
        age = _minutes_since(last_tick.mtime, now)
        if age < SLEEP_AFTER_FAILURE_MIN:
            return {**base, "pill": "sleeping", "link": board_url,
                    "reason": f"Card #{last_tick.issue} did not finish; trying again shortly."}

    return {**base, "pill": "idle", "link": board_url,
            "reason": _idle_reason(lane, items_by_number, last_tick)}


# --- Peek: one lane's session, decoded on demand -------------------------------------------

PEEK_REPLAY_LINES = 100  # a first look's worth of history; the panel scrolls
_LOG_NAME = re.compile(r"^card-(\d+)-(\d{8}T\d{6}Z)\.json$")


# How far a session file's last write may sit from the end of the tick that wrote it. The
# tick's result lands in its log as the process exits, seconds after the session's last
# line; this is slack for a slow exit, not a search window.
LAST_SESSION_SLACK_SEC = 900


def _last_tick(state_dir):
    """(card, when it ended, when it started if it died) for this lane's newest tick log,
    or None — from the file names (`card-<n>-<UTC stamp>.json`, the stamp sorts as text)
    and one stat, never by parsing a log: this runs on every poll of an open Peek panel on
    a lane that is not working. A tick that was killed never wrote its result: its log is
    empty and still carries the time it was created, which is the launch, not the end —
    so for that one the third value is the launch time and the caller looks forward from
    it; for a tick that finished it is None."""
    newest = None
    logs = os.path.join(state_dir, "logs")
    try:
        names = os.listdir(logs)
    except OSError:
        return None
    for name in names:
        m = _LOG_NAME.match(name)
        if m and (newest is None or m.group(2) > newest[0]):
            newest = (m.group(2), int(m.group(1)), name)
    if newest is None:
        return None
    try:
        st = os.stat(os.path.join(logs, newest[2]))
    except OSError:
        return None
    launched = None
    if st.st_size == 0:
        launched = calendar.timegm(time.strptime(newest[0], "%Y%m%dT%H%M%SZ"))
    return newest[1], st.st_mtime, launched


def _session_ended_near(session_dir, when, launched=None):
    """The session file in `session_dir` last written closest to `when`, within
    `LAST_SESSION_SLACK_SEC` — or None. With `launched` (a tick that died, so `when` is
    not its end): the first session to end after that launch instead. `tail.newest_jsonl` is right for a lane that is
    working (its session is the one being written), and wrong for "this lane's last tick":
    a project's lanes share one worktree per card, and a triage lane runs every tick in
    the project root, so the newest file in the folder can be another lane's later session
    on the same card, shown under this lane's name."""
    best = None
    try:
        names = [n for n in os.listdir(session_dir) if n.endswith(".jsonl")]
    except OSError:
        return None
    for name in names:
        path = os.path.join(session_dir, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue  # pruned between the listing and the stat
        if launched is not None:
            if mtime < launched:
                continue
            gap = mtime - launched
        else:
            gap = abs(mtime - when)
            if gap > LAST_SESSION_SLACK_SEC:
                continue
        if best is None or gap < best[0]:
            best = (gap, path)
    return best[1] if best else None


def tail_status(info, claude_projects_dir, card=None, file=None, pos=None):
    """One step of `bin/tail.py`'s own decode, for the floor page's Peek panel: given a
    lane's resolved layout (`state_dir`, `workdir_mode`, `worktree_base` — the fields
    `discover_lanes` already computes for every lane), say what its session shows. The
    same decode `tail.py` uses and nothing more: never a raw transcript line, a tool's
    full output, or any path beyond the session file's own basename (`tail.py`'s
    `describe_tool_use` already caps what it returns at ~200 characters — this hands that
    back unchanged, not a second, looser copy of the same limit).

    A lane holding a card is `live`. A lane that is not — idle, paused, waiting on Steve —
    shows the session of its **last** tick instead, `live: false`: a desk with nobody at it
    still has something worth peeking at, and a button that only exists while a lane is
    working reads, the rest of the time, as a feature that never shipped (card #63, third
    visit).

    `card`/`file`/`pos` are the caller's last-seen cursor — `None` on a fresh Peek, or
    whenever the card or the session file it names has moved on since. Passing them back
    unchanged on the next call is what turns this into `tail.py`'s own `read_new_lines`
    (a `drain`) instead of a full `replay` every poll.

    Returns one of:
      {"status": "idle"}                       - not working, and no session of its last
                                                 tick on file (never ran, or pruned since)
      {"status": "waiting", "card": <n>}       - working, its session not written yet
      {"status": "ok", "card": <n>, "live": bool, "file": <name>, "pos": <n>,
       "lines": [...], "reset": bool}
    """
    # `_read_current` (above), not `tail.read_current` — this file's own helper returns an
    # int or None; `tail.py`'s returns the raw string, which would never equal the `card`
    # cursor's int below and force a full replay on every single poll.
    n = _read_current(os.path.join(info["state_dir"], "current"))
    live = n is not None
    ended = launched = None
    if not live:
        last = _last_tick(info["state_dir"])
        if last is None:
            return {"status": "idle"}
        n, ended, launched = last
    session_dir = os.path.join(
        claude_projects_dir,
        tail.encode_dir(tail.card_dir(n, info["workdir_mode"], info["worktree_base"])))
    f = (tail.newest_jsonl(session_dir) if live
         else _session_ended_near(session_dir, ended, launched))
    if f is None:
        # The CLI prunes old transcripts on its own schedule, so a lane's last session can
        # be gone: that is "nothing to show", not a session about to appear.
        return {"status": "waiting", "card": n} if live else {"status": "idle"}
    fname = os.path.basename(f)
    # A negative pos (never produced by this endpoint itself, but a client can send
    # anything) would reach `fh.seek()` and raise — treat it the same as "no cursor yet"
    # rather than letting a malformed request 500 instead of replaying.
    if pos is not None and pos < 0:
        pos = None
    if card != n or file != fname or pos is None:
        lines, new_pos = tail.replay_lines(f, PEEK_REPLAY_LINES)
        return {"status": "ok", "card": n, "live": live, "file": fname, "pos": new_pos,
                "lines": lines, "reset": True}
    lines, new_pos = tail.read_new_lines(f, pos)
    return {"status": "ok", "card": n, "live": live, "file": fname, "pos": new_pos,
            "lines": lines, "reset": False}


# --- Peek at the ship station: what CI did last, and the box's deployer ---------------------

SHIP_RUNS = 6            # workflow runs on main fetched for one Peek
SHIP_EARLIER = 4         # earlier commits listed under the newest one
SHIP_HISTORY_LINES = 12  # deployer lines shown, newest first
SHIP_CACHE_SEC = 20      # one fetch serves every poll of an open panel for this long
# Steps every job has and nobody needs to read on a phone.
_HOUSEKEEPING = ("Set up job", "Complete job", "Post ", "Run actions/")


def _fetch_live_pr_run(repo, get):
    """The newest pull_request-triggered run that has not finished, shaped like one of
    `fetch_ci_runs`'s own entries — or None. A PR's own commit never reaches `branch` until
    it merges, so the push history below can never show it: this is the window Steve meant
    by "the CI windows" (card #77) — the wait before every merge, which is most of the time
    CI is actually running. Best-effort: a failure here never stops the push history from
    being read, since watching a PR's check is a nicety on top of it, not the main job."""
    try:
        raw = get(f"repos/{repo}/actions/runs?event=pull_request&per_page=5").get("workflow_runs", [])
    except Exception as e:
        print(f"floor: a PR's live CI run for {repo} unreachable: {e}", file=sys.stderr, flush=True)
        return None
    # GitHub empties `pull_requests` once the PR it ran for is closed (observed on this
    # box), so a run with no number here is one whose PR is already gone — not a window
    # worth leading the panel with, and not safely labeled "on main" either.
    live = next((r for r in raw
                if (r.get("status") or "") != "completed" and r.get("pull_requests")), None)
    if live is None:
        return None
    pr = live["pull_requests"][0]["number"]
    return {"id": live.get("id"), "name": live.get("name") or "", "sha": live.get("head_sha") or "",
            "title": " ".join((live.get("display_title") or "").split()), "status": live.get("status") or "",
            "conclusion": live.get("conclusion"), "started": live.get("run_started_at") or live.get("created_at"),
            "updated": live.get("updated_at"), "pr": pr, "branch": live.get("head_branch") or ""}


def fetch_ci_runs(repo, branch="main", n=SHIP_RUNS, get=_gh_api):
    """The push-triggered workflow runs on `branch`, newest first — with a PR's own run
    leading them when one is still running (`_fetch_live_pr_run`) — and jobs and steps for
    whichever leads the list. Three or four REST calls. Raises only when the push query
    itself fails; the live-PR check is best-effort (see `_fetch_live_pr_run`)."""
    raw = get(f"repos/{repo}/actions/runs?branch={branch}&event=push&per_page={n}")
    # A title is one line here, whatever it was: the panel puts each list entry on a line.
    runs = [{"id": r.get("id"), "name": r.get("name") or "", "sha": r.get("head_sha") or "",
             "title": " ".join((r.get("display_title") or "").split()), "status": r.get("status") or "",
             "conclusion": r.get("conclusion"), "started": r.get("run_started_at") or r.get("created_at"),
             "updated": r.get("updated_at")}
            for r in raw.get("workflow_runs", [])]
    live = _fetch_live_pr_run(repo, get)
    if live is not None:
        runs = [live] + runs
    for run in runs:
        if run["sha"] != runs[0]["sha"]:
            continue
        jobs = get(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=50").get("jobs", [])
        run["jobs"] = [{"name": j.get("name") or "", "status": j.get("status") or "",
                        "conclusion": j.get("conclusion"), "started": j.get("started_at"),
                        "completed": j.get("completed_at"),
                        "steps": [{"name": st.get("name") or "", "status": st.get("status") or "",
                                   "conclusion": st.get("conclusion"), "started": st.get("started_at"),
                                   "completed": st.get("completed_at")} for st in j.get("steps") or []]}
                       for j in jobs]
    return runs


def read_deploy_history(deploy_state, n=SHIP_HISTORY_LINES):
    """The last `n` lines `deploy/autodeploy.sh` said, oldest first: [(when, text)]. Its own
    `history` file — the journal is not readable by this page's user. Missing is empty."""
    try:
        with open(os.path.join(deploy_state, "history")) as f:
            raw = f.read().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in raw:
        stamp, _, text = line.partition(" ")
        try:
            when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if text.strip():
            out.append((when, text.strip()[:200]))
    return out


def _when(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _ago(when, now):
    if when is None:
        return ""
    d = max((now - when).total_seconds(), 0)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{round(d / 60)}m ago"
    if d < 86400:
        return f"{round(d / 3600)}h ago"
    return f"{round(d / 86400)}d ago"


def _took(started, completed):
    a, b = _when(started), _when(completed)
    if a is None or b is None or (b - a).total_seconds() < 1:
        return ""
    sec = int((b - a).total_seconds())
    return f" · {sec}s" if sec < 60 else f" · {sec // 60}m {sec % 60:02d}s"


def _mark(status, conclusion):
    """One character for where a run, job or step stands."""
    if status != "completed":
        return "…"   # queued or running
    if conclusion == "success":
        return "✓"
    if conclusion in ("skipped", "neutral", "cancelled"):
        return "–"
    return "✗"


def _worst(marks):
    for m in ("✗", "…", "✓"):
        if m in marks:
            return m
    return "–"


_DEPLOY_WORDS = {
    "live": "live on the box: {live}", "checking": "checking {target}: CI and tests",
    "testing": "retesting {target}", "held": "held: {target}",
}


def ship_lines(runs, now, deploy=None, history=None, has_deployer=False):
    """What the ship station's Peek shows, as plain lines of text (the page puts them in a
    `<pre>` with `textContent` — nothing here is ever HTML): the newest commit on main and
    every step CI ran on it, the commits before it in one line each, and — on the board
    whose repo this box deploys — what the deployer is doing and the last things it said.
    When a PR's own check is still running, that leads instead (it is `runs[0]`, same as
    any other entry) and the header reads "CI on PR #<n>" rather than "CI on main".
    `runs` is `fetch_ci_runs`'s list, or None when CI could not be reached. Pure: no I/O.

    Returns {"sub": <one line for the panel's head>, "live": <something is running now>,
    "lines": [...]}."""
    lines, sub, live = [], "CI could not be reached", False
    if runs is None:
        lines.append("CI could not be reached right now.")
    elif not runs:
        sub = "no CI runs on main yet"
        lines.append("No CI runs on main yet.")
    else:
        newest = [r for r in runs if r["sha"] == runs[0]["sha"]]
        overall = _worst([_mark(r["status"], r["conclusion"]) for r in newest])
        ago = _ago(_when(newest[0]["started"]), now)
        failing = [j["name"] for r in newest for j in r.get("jobs", [])
                   if _mark(j["status"], j["conclusion"]) == "✗"]
        live = overall == "…"
        on_pr = newest[0].get("pr")  # the "live window" entry _fetch_live_pr_run adds
        where = f"PR #{on_pr}" if on_pr else "main"
        sub = {"✓": f"CI on {where}: passed {ago}".rstrip(), "…": f"CI on {where}: running now",
               "✗": f"CI on {where}: failing — " + (", ".join(failing[:3]) or "see below"),
               "–": f"CI on {where}: nothing ran {ago}".rstrip()}[overall]
        lines.append(f"{newest[0]['sha'][:7]}  {newest[0]['title'][:90]}"
                     + (f"  · {newest[0]['branch']}" if on_pr else ""))
        for r in newest:
            began = _ago(_when(r["started"]), now)
            lines.append(f"{_mark(r['status'], r['conclusion'])} {r['name']}"
                         + (f" · started {began}" if began else ""))
            for j in r.get("jobs", []):
                jm = _mark(j["status"], j["conclusion"])
                if jm == "–":
                    lines.append(f"  – {j['name']} (skipped)")
                    continue
                lines.append(f"  {jm} {j['name']}{_took(j['started'], j['completed'])}")
                for st in j["steps"]:
                    if st["name"].startswith(_HOUSEKEEPING):
                        continue
                    lines.append(f"    {_mark(st['status'], st['conclusion'])} {st['name']}"
                                 f"{_took(st['started'], st['completed'])}")
        earlier, seen = [], {runs[0]["sha"]}
        for r in runs:
            if r["sha"] in seen:
                continue
            seen.add(r["sha"])
            same = [x for x in runs if x["sha"] == r["sha"]]
            earlier.append(f"  {_worst([_mark(x['status'], x['conclusion']) for x in same])} "
                           f"{r['sha'][:7]}  {r['title'][:60]} · {_ago(_when(r['started']), now)}")
        if earlier:
            # "Before that" is literally true for a push: each one is an older commit on
            # the same branch. A live PR run hasn't merged yet, so what follows it is what
            # already shipped, not what came before it.
            heading = "Already on main" if on_pr else "Before that on main"
            lines += ["", heading] + earlier[:SHIP_EARLIER]

    if has_deployer:
        lines += ["", "The box's deployer"]
        if deploy:
            words = _DEPLOY_WORDS.get(deploy.get("state"), "")
            if words:
                card = deploy.get("card")
                lines.append("  now: " + words.format(live=deploy.get("live", ""), target=deploy.get("target", ""))
                             + (f" (#{card['number']})" if card and deploy.get("state") == "held" else ""))
            # Card #105's step/alive/waiting-for/lands-by fields are NOT rendered into these
            # plain-text lines: the page's own script renders them once, richly — with a
            # real link to a waiting lane's card, and local-time clocks — in `#peek-deploy`
            # (`renderDeployDetail`, below `INDEX_HTML`). A second, UTC, text-only copy of
            # the same facts right here read as two different deadlines on the same panel
            # (caught in the usability pass).
        if history:
            for when, text in reversed(history):
                lines.append(f"  {_ago(when, now):>8}  {text}")
        else:
            lines.append("  Nothing on file yet: its history starts with the next deploy.")
    return {"sub": sub, "live": live, "lines": lines}


def make_ship_lookup(repos_fn, runs_fetcher, deploy_repo=None, deploy_fn=None, history_fn=None,
                     now_fn=None, cache_sec=SHIP_CACHE_SEC):
    """`(project name) -> dict | None` for `/api/ship.json`. `repos_fn()` maps each project
    on the page to its `owner/repo`; a name that is not one of them is None (a 404), so the
    endpoint can only ever be asked about a repo this box already works. One fetch per
    project serves every poll for `cache_sec`: an open panel polls, and GitHub's API is
    not the place to spend a call per poll. CI that cannot be reached still answers — with
    the deployer's part, where there is one."""
    cache, lock = {}, threading.Lock()

    def lookup(name):
        repo = repos_fn().get(name)
        if repo is None:
            return None
        with lock:
            hit = cache.get(name)
            if hit and time.monotonic() - hit[0] < cache_sec:
                return hit[1]
        try:
            runs = runs_fetcher(repo)
        except Exception as e:
            runs = None
            print(f"floor: CI runs for {repo} unreachable: {e}", file=sys.stderr, flush=True)
        deploys = bool(deploy_repo) and repo == deploy_repo
        deploy = history = None
        if deploys:
            try:
                deploy = deploy_fn() if deploy_fn else None
                history = history_fn() if history_fn else None
            except Exception as e:
                print(f"floor: deployer state unreadable: {e}", file=sys.stderr, flush=True)
        now = now_fn() if now_fn else datetime.now(timezone.utc)
        # Card #105: `deploy` verbatim, alongside `ship_lines`'s own plain-text rendering of
        # the same fields — the page's script wants real links (a waiting lane's card) that
        # a `<pre>`'s `textContent` can never carry (bin/floor.py's `renderDeployDetail`).
        result = {"status": "ok", "deploy": deploy if deploys else None,
                  **ship_lines(runs, now, deploy, history, has_deployer=deploys)}
        with lock:
            cache[name] = (time.monotonic(), result)
        return result

    return lookup


def _fmt_quota(cost):
    if cost is None:
        return "unknown"
    return f"~${cost:.2f} equivalent (subscription — not billed)"


# --- assembling the page's data -----------------------------------------------------------

# --- the server room: this box, the lab's other boxes, and the tokens the lanes move ----

def box_stats():
    """What this box is doing right now, from the kernel's own files: load, memory, disk,
    uptime. Any piece that cannot be read (a Mac has no /proc) is None — the server room
    shows a dash for it, never a crash."""
    out = {"cpus": os.cpu_count(), "load1": None, "mem_total": None, "mem_used": None,
           "disk_total": None, "disk_used": None, "uptime": None}
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0]) * 1024
        out["mem_total"] = info.get("MemTotal")
        if "MemTotal" in info and "MemAvailable" in info:
            out["mem_used"] = info["MemTotal"] - info["MemAvailable"]
    except (OSError, ValueError, IndexError):
        pass
    try:
        st = os.statvfs("/")
        out["disk_total"] = st.f_frsize * st.f_blocks
        out["disk_used"] = st.f_frsize * (st.f_blocks - st.f_bavail)
    except OSError:
        pass
    try:
        with open("/proc/uptime") as f:
            out["uptime"] = int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    return out


def proxmox_summary(data):
    """Proxmox's `/cluster/resources` rows, cut down to what the server room shows: every
    node and guest, with its CPU share and memory. Nothing else of the API's row (disk
    I/O, network bytes, template flags) reaches the page."""
    out = []
    for d in data or []:
        if d.get("type") not in ("node", "lxc", "qemu"):
            continue
        out.append({"name": d.get("name") or d.get("node") or "", "vmid": d.get("vmid"),
                    "type": d["type"], "status": d.get("status") or "",
                    "cpu": round(float(d.get("cpu") or 0), 4), "maxcpu": d.get("maxcpu"),
                    "mem": d.get("mem"), "maxmem": d.get("maxmem")})
    out.sort(key=lambda g: (0 if g["type"] == "node" else 1, g["vmid"] or 0))
    return out


def fetch_proxmox(url=None, token=None, verify=None, timeout=10):
    """The lab's other boxes, from Proxmox's own API. Opt-in: `PROXMOX_URL`
    (https://host:8006) and `PROXMOX_TOKEN` (user@realm!tokenid=secret) in the floor
    service's own environment file — the box's /etc/watcher/floor.env, which only
    watcher-floor.service reads (never /etc/watcher/env, every lane's), never git;
    `PROXMOX_VERIFY=0` accepts the lab's self-signed certificate. [] when unconfigured. Raises when configured and
    unreachable; `build_state` logs that and shows the room without the other boxes."""
    url = url if url is not None else os.environ.get("PROXMOX_URL", "")
    token = token if token is not None else os.environ.get("PROXMOX_TOKEN", "")
    if not url or not token:
        return []
    import ssl
    import urllib.request
    if verify is None:
        verify = os.environ.get("PROXMOX_VERIFY", "1") != "0"
    ctx = None
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url.rstrip("/") + "/api2/json/cluster/resources",
                                 headers={"Authorization": "PVEAPIToken=" + token})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        data = json.loads(r.read()).get("data") or []
    return proxmox_summary(data)


def fetch_card(number, project_repo, timeout=20):
    """The card itself, for the bulletin board's Peek (October 9, 2026): title, body,
    state, labels and its last three comments, from `gh issue view`. None when gh
    cannot — the popup then says the card could not be read, never a crash."""
    try:
        r = subprocess.run(["gh", "issue", "view", str(number), "--repo", project_repo, "--json",
                            "title,body,state,url,labels,comments,createdAt,closedAt"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    comments = [{"author": (c.get("author") or {}).get("login") or "", "when": c.get("createdAt") or "",
                 "body": (c.get("body") or "")[:4000]} for c in (d.get("comments") or [])[-3:]]
    return {"title": d.get("title") or "", "body": (d.get("body") or "")[:12000],
            "state": d.get("state") or "", "url": d.get("url") or "",
            "labels": [l.get("name") or "" for l in d.get("labels") or []],
            "comments": comments, "created": d.get("createdAt") or "", "closed": d.get("closedAt") or ""}


def make_card_lookup(repos_fn, fetcher=fetch_card, cache_sec=60, clock=time.time):
    """`(project name, card number) -> dict | None` for `/api/card.json`, cached a minute
    per card so a Peek opened twice costs one `gh` call. `repos_fn` maps a project's name
    to its repo (the state's own `name`, the way the page knows a card)."""
    cache = {}

    def lookup(project, number):
        repo = (repos_fn() or {}).get(project)
        if not repo or not number:
            return None
        key = (repo, number)
        hit = cache.get(key)
        if hit and clock() - hit[0] < cache_sec:
            return hit[1]
        card = fetcher(number, repo)
        if card is None:
            return None
        card = dict(card, project=project, number=number)
        cache[key] = (clock(), card)
        return card
    return lookup


def build_state(projects_dir, state_root, board_fetcher, unit_active_fn=None, ask_fn=None,
                 images_fn=None, rounds_fn=None, now=None, box_name=None, ci_fetcher=None,
                 deploy_fn=None, deploy_repo=None, ratelimit_fn=None, box_stats_fn=None,
                 proxmox_fn=None, ledger_fn=None, plan_fn=None):
    now = now or datetime.now(timezone.utc)
    box_name = box_name or socket.gethostname()
    lanes = discover_lanes(projects_dir, state_root)
    projects = group_by_project(lanes)

    out_projects = []
    needs_steve_total = 0
    recent = []
    wins = {}            # (project, card) -> the newest tick that took it to Done this week
    tokens_by_project = {}
    token_since = now.timestamp() - TOKENS_HOURS * 3600
    wins_since = now.timestamp() - WINS_DAYS * 86400

    for p in projects:
        try:
            items = board_fetcher(p["board_owner"], p["board_number"])
            board_error = None
        except Exception as e:
            items = []
            # The page says only "unreachable". The reason is gh's stderr, which the card
            # keeps off the page (roloc/watcher#40, "never ... stderr"), so it goes to the
            # service's journal instead.
            board_error = True
            print(f"floor: board {p['board_owner']}/{p['board_number']} unreachable: {e}",
                  file=sys.stderr, flush=True)
        items_by_number = {it["number"]: it for it in items if it.get("number") is not None}

        lane_out = []
        for lane in sorted(p["lanes"], key=lambda l: KIND_ORDER.get(lane_kind(l), 9)):
            ticks = metrics.load_lane(lane["state_dir"])  # one parse of this lane's logs
            active = unit_active_fn(lane["unit"]) if unit_active_fn else None
            desk = lane_desk(lane, items_by_number, p["board_url"], now,
                              last_tick=ticks[-1] if ticks else None,
                              unit_active=active, ask=ask_fn, images=images_fn, plan=plan_fn)
            lane_out.append(desk)

            for t in ticks[-RECENT_PER_LANE:]:
                url = f"https://github.com/{lane['project_repo']}/issues/{t.issue}"
                recent.append({
                    "project": p["name"], "lane": lane["lane"],
                    "card": {"number": t.issue, "url": url},
                    "when": t.mtime.isoformat(), "quota": _fmt_quota(t.cost),
                })
            for t in ticks:
                ended = t.mtime.timestamp()
                if t.tokens and ended >= token_since:
                    tokens_by_project[p["name"]] = tokens_by_project.get(p["name"], 0) + int(t.tokens)
                if t.ended_in == "Done" and ended >= wins_since:
                    key = (p["name"], t.issue)
                    if key not in wins or wins[key]["when"] < t.mtime.isoformat():
                        it = items_by_number.get(t.issue) or {}
                        wins[key] = {"project": p["name"], "lane": lane["lane"],
                                     "card": {"number": t.issue,
                                              "url": it.get("url") or f"https://github.com/{lane['project_repo']}/issues/{t.issue}"},
                                     "title": it.get("title") or "", "when": t.mtime.isoformat()}

        # Every card Stuck on this board, whichever lane last touched it — a lane's own
        # desk only shows the one card that lane itself last worked (`lane_desk`, above),
        # so a design lane that ran five cards back to back and parked three of them would
        # otherwise show only the last of the three, or none at all if its very last card
        # wasn't one of them (card #96's "why nothing shows today"). Counted once per card,
        # never once per lane: `needs_steve_total` comes from this list, not from how many
        # lane pills happen to read `needs_steve`, so the same Stuck card is never counted
        # twice and a Stuck card no lane's last tick points at is still counted once.
        stuck_out = []
        for it in items:
            if it.get("state") != "OPEN" or (it.get("status") or "").strip() != STUCK_STATUS:
                continue
            num = it.get("number")
            url = it.get("url") or p["board_url"]
            ask_text = ask_fn(num, p["project_repo"]) if ask_fn else ""
            rounds = rounds_fn(num, p["project_repo"]) if rounds_fn else []
            stuck_out.append({"card": {"number": num, "url": url}, "ask": ask_text or "",
                               "rounds": rounds})
        stuck_out.sort(key=lambda s: s["card"]["number"])
        needs_steve_total += len(stuck_out)

        ci = None
        if ci_fetcher:
            try:
                ci = ci_fetcher(p["project_repo"])
            except Exception as e:
                print(f"floor: CI for {p['project_repo']} unreachable: {e}", file=sys.stderr, flush=True)
        ship = {"ci": ci_summary(ci, p["project_repo"])}
        if deploy_fn and deploy_repo and p["project_repo"] == deploy_repo:
            try:
                dep = deploy_fn()
            except Exception as e:
                dep = None
                print(f"floor: deployer state unreadable: {e}", file=sys.stderr, flush=True)
            if dep:
                ship["deploy"] = dep
                if dep["state"] == "held":
                    needs_steve_total += 1
                # Card #106: every lane on the box runs this same deploy's code, whichever
                # project it works — so the one Ship desk that shows the deployer's own
                # state (the watcher's) is where "which release each lane is on" belongs
                # too, box-wide, never only this project's own lanes.
                ship["lanes"] = [
                    {"project": proj["name"], "lane": l["lane"], "release": lane_release(l)}
                    for proj in projects for l in proj["lanes"]
                ]

        entry = {"name": p["name"], "board_url": p["board_url"],
                  "counts": project_counts(items), "lanes": lane_out, "ship": ship,
                  "stuck": stuck_out}
        if board_error:
            entry["board_error"] = board_error
        out_projects.append(entry)

    recent.sort(key=lambda r: r["when"], reverse=True)

    rl = {}
    if ratelimit_fn:
        try:
            rl = ratelimit_fn() or {}
        except Exception as e:
            print(f"floor: ratelimit unreadable: {e}", file=sys.stderr, flush=True)

    # The hour's GraphQL spend by caller (card #114), beside the remaining-points line
    # above: a failed read leaves the line blank, never a crash — the same shape every
    # other best-effort reader on this page already has.
    ledger = {}
    if ledger_fn:
        try:
            ledger = ledger_fn() or {}
        except Exception as e:
            print(f"floor: ledger unreadable: {e}", file=sys.stderr, flush=True)

    # The server room. The box itself always; the lab's other boxes only when the floor
    # was given a Proxmox token, and a failed read leaves them out with one journal line.
    proxmox = []
    if proxmox_fn:
        try:
            proxmox = proxmox_fn() or []
        except Exception as e:
            print(f"floor: proxmox unreachable: {e}", file=sys.stderr, flush=True)
    total_tokens = sum(tokens_by_project.values())
    server = {"box": (box_stats_fn or (lambda: {}))(),
              "tokens": {"hours": TOKENS_HOURS, "total": total_tokens,
                         "per_hour": round(total_tokens / TOKENS_HOURS),
                         "per_project": [{"name": n, "tokens": v} for n, v in sorted(tokens_by_project.items())]},
              "proxmox": proxmox}

    return {
        "header": {"box": box_name, "time": now.isoformat(),
                   "needs_steve_total": needs_steve_total,
                   "github_remaining": rl.get("remaining"),
                   "github_reset": rl.get("resetAt"),
                   "github_limit": rl.get("limit"),
                   "github_points_per_min": ledger.get("points_per_min"),
                   "github_spend": [{"caller": c, "points": p} for c, p in
                                     sorted((ledger.get("by_caller") or {}).items(),
                                            key=lambda kv: kv[1], reverse=True)]},
        "projects": out_projects,
        "recent": recent[:RECENT_TOTAL],
        "wins": {"days": WINS_DAYS, "items": sorted(wins.values(), key=lambda w: w["when"], reverse=True)},
        "server": server,
    }


def systemctl_is_active(unit, timeout=5):
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=timeout)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def board_ask(issue, project_repo, board_py=None, timeout=20):
    """The first line of the card's newest `Needs Steve` comment (`board.py ask`, which
    wraps `needs_steve_ask` — a plain `gh issue view`, no board/GraphQL involved). `gh`
    itself still has to be told which repo, though: with several projects on one box,
    `GH_REPO` is what `spawn.sh` sets before every repo-scoped `gh` call (its own comment:
    "GH_REPO pins every repo-scoped gh call here AND in board.py"). Without it here, every
    lane's ask would resolve against whatever repo this process's cwd happens to be in —
    this repo, for every project, silently."""
    board_py = board_py or os.path.join(HERE, "board.py")
    env = dict(os.environ, GH_REPO=project_repo)
    try:
        r = subprocess.run([sys.executable, board_py, "ask", str(issue)],
                            capture_output=True, text=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def board_images(issue, project_repo, board_py=None, timeout=20):
    """Direct image links — a mockup or a screenshot — under the card's newest `Needs
    Steve` comment (`board.py images`, which wraps `needs_steve_images`): the floor
    page's inline preview (card #91), so a reader never has to open the card to find the
    picture its own ask is about. Same `GH_REPO` pinning as `board_ask`, for the same
    reason — several projects share this one box."""
    board_py = board_py or os.path.join(HERE, "board.py")
    env = dict(os.environ, GH_REPO=project_repo)
    try:
        r = subprocess.run([sys.executable, board_py, "images", str(issue)],
                            capture_output=True, text=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def board_rounds(issue, project_repo, board_py=None, timeout=20):
    """Every `Needs Steve` round for this card, newest first — `board.py images
    --all-rounds`, which wraps `needs_steve_rounds` — each with its own pictures, lettered
    by direction (card #96: the Needs Steve strip's Review overlay, card #100, which flips
    through every round a card has been asked, not only the lane's own single-thumbnail
    desk pill). Same `GH_REPO` pinning as `board_ask`/`board_images`, for the same reason.
    `[]` on any failure — a card the strip then shows with its ask and no pictures, never
    a crash."""
    board_py = board_py or os.path.join(HERE, "board.py")
    env = dict(os.environ, GH_REPO=project_repo)
    try:
        r = subprocess.run([sys.executable, board_py, "images", str(issue), "--all-rounds"],
                            capture_output=True, text=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def board_plan(issue, project_repo, board_py=None, timeout=20):
    """The timestamp of the card's plan comment, or "" if none yet (`board.py plan`,
    which wraps `plan_posted_at` — a plain `gh issue view`, no board/GraphQL involved):
    the Working desk's "plan posted" / "no plan yet" line (card #103). Same `GH_REPO`
    pinning as `board_ask`, for the same reason — several projects share this one box."""
    board_py = board_py or os.path.join(HERE, "board.py")
    env = dict(os.environ, GH_REPO=project_repo)
    try:
        r = subprocess.run([sys.executable, board_py, "plan", str(issue)],
                            capture_output=True, text=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def board_ratelimit(board_py=None, timeout=10):
    """The account's GraphQL budget, remaining/reset/limit — `board.py ratelimit`, which
    only ever reads the last real query's own rateLimit field back out of board.py's cache
    (card #98): never a GraphQL call of its own, so the floor page's header can show it on
    every poll for free. `{}` if nothing has been cached yet or the lookup itself failed —
    the header then shows nothing, never a crash or a stale guess."""
    board_py = board_py or os.path.join(HERE, "board.py")
    try:
        r = subprocess.run([sys.executable, board_py, "ratelimit"],
                            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def board_ledger(board_py=None, timeout=10):
    """This hour's GraphQL spend by caller — `board.py ledger`, which only ever reads the
    ledger file every real call already appended to (card #114): never a call of its own.
    `{}` on any failure, the same shape `board_ratelimit` has — the header line then shows
    nothing beside the remaining-points line, never a crash."""
    board_py = board_py or os.path.join(HERE, "board.py")
    try:
        r = subprocess.run([sys.executable, board_py, "ledger"],
                            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


# --- the HTTP server -----------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The floor</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --ink: #1b1f24; --dim: #5b6470; --line: #e2e5ea;
    --working: #1a7f37; --idle: #5b6470; --needs: #cf222e; --paused: #9a6700; --sleep: #6639ba;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --panel: #161b22; --ink: #e6edf3; --dim: #8b949e; --line: #30363d;
      --working: #3fb950; --idle: #8b949e; --needs: #ff7b72; --paused: #d29922; --sleep: #a371f7;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: var(--bg); color: var(--ink);
    font: 16px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  h1 { font-size: 20px; margin: 0; }
  .muted { color: var(--dim); font-size: 14px; }
  .alert { color: var(--needs); font-weight: 600; }
  section.project {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 16px; margin-bottom: 16px;
  }
  .project h2 { font-size: 17px; margin: 0 0 4px; }
  .project h2 a { color: inherit; }
  .counts { color: var(--dim); font-size: 14px; margin-bottom: 10px; }
  .desks { display: flex; flex-wrap: wrap; gap: 10px; }
  .desk {
    border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; min-width: 180px;
    flex: 1 1 200px; text-decoration: none; color: inherit; display: block;
  }
  /* A lane's desk is a box holding a link (the picture and the words, to the card or the
     board) and, beside it, the Peek button. The button is never inside the link: a
     button in an anchor is a click that some browsers also follow. */
  .desk-link { display: block; text-decoration: none; color: inherit; }
  .desk .name { font-weight: 600; }
  .desk .unit { color: var(--dim); font-size: 12px; }
  .pill {
    display: inline-block; font-size: 12px; font-weight: 600; padding: 2px 8px;
    border-radius: 999px; margin-top: 6px;
  }
  .pill.working { background: color-mix(in srgb, var(--working) 18%, transparent); color: var(--working); }
  /* card #103: a working desk still with no plan comment past PLAN_LATE_MIN minutes —
     the lane's own warning, amber like the pausing line above, so a hung lane and one
     that simply has not posted yet read differently from a phone. */
  .pill.working.plan-late { background: color-mix(in srgb, var(--paused) 18%, transparent); color: var(--paused); }
  .pill.idle { background: color-mix(in srgb, var(--idle) 18%, transparent); color: var(--idle); }
  .pill.needs_steve { background: color-mix(in srgb, var(--needs) 18%, transparent); color: var(--needs); }
  .pill.paused { background: color-mix(in srgb, var(--paused) 18%, transparent); color: var(--paused); }
  .pill.sleeping { background: color-mix(in srgb, var(--sleep) 18%, transparent); color: var(--sleep); }
  .detail { font-size: 13px; margin-top: 6px; color: var(--dim); }
  /* card #110: a working desk that is also pausing — a second fact, not a replacement
     for the Working pill, so it reads small and amber beside the detail line. */
  .pausing { font-size: 12px; margin-top: 2px; color: var(--paused); }
  /* A mockup or a screenshot a Needs Steve comment pointed at, shown right on the desk
     instead of making Steve click through to the card to find it (card #91). */
  .mockups { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .mockup-thumb {
    max-width: 120px; max-height: 90px; border-radius: 6px; border: 1px solid var(--line);
    object-fit: cover;
  }
  /* The Needs Steve strip (card #96, reshaped by #100): every card Stuck on this board,
     whichever lane last touched it — its ask, the directions it offers, and a Review
     button that opens the full-window overlay (below) rather than a sliver inline. */
  .needs-strip { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
  .needs-card {
    border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px;
    background: color-mix(in srgb, var(--needs) 5%, transparent); flex: 1 1 220px; max-width: 320px;
  }
  .needs-ask {
    display: block; font-size: 13px; font-weight: 600; color: inherit; text-decoration: none;
    margin-bottom: 6px;
  }
  .direction-chips { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
  .direction-chip {
    font-size: 11px; font-weight: 700; color: var(--dim);
    background: color-mix(in srgb, var(--dim) 14%, transparent);
    border-radius: 999px; padding: 1px 7px;
  }
  .direction-chip.recommended {
    color: var(--working); background: color-mix(in srgb, var(--working) 16%, transparent);
  }
  .review-btn {
    font-size: 12px; font-weight: 600; padding: 5px 12px; border-radius: 999px;
    border: 1px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer;
  }
  .review-btn:hover { border-color: var(--dim); }
  .review-btn.tray { font-size: 10px; padding: 1px 7px; margin-left: 3px; vertical-align: middle; }
  /* The studio's own row gets the same strip in miniature: a chip list so a crowded row
     still names every card even where there is no room for its ask, each with its own
     Review button when it has a picture behind it. */
  .row-stuck {
    position: absolute; right: 2%; display: flex; flex-direction: column; align-items: flex-end;
    gap: 4px; max-width: 46%; pointer-events: auto;
  }
  .row-stuck-chips { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
  /* In the office the same strip is the in-tray on Steve's desk: a paper tray in front of
     it, chips grouped by board. */
  .row-stuck.in-tray {
    right: auto; max-width: none; box-sizing: border-box; align-items: stretch; padding: 4px 6px;
    border: 1.5px solid var(--line); border-radius: 4px;
    background: color-mix(in srgb, var(--needs) 8%, var(--panel));
  }
  .row-stuck.in-tray .row-stuck-chips { justify-content: flex-start; align-items: center; }
  .tray-label, .tray-board {
    font-size: 9px; font-weight: 700; color: var(--dim); text-transform: uppercase; letter-spacing: .05em;
  }
  .tray-board { margin-left: 4px; text-transform: none; letter-spacing: 0; }
  .row-stuck-chip {
    font-size: 10px; font-weight: 600; color: var(--needs); background: color-mix(in srgb, var(--needs) 14%, transparent);
    border-radius: 999px; padding: 1px 6px; text-decoration: none;
  }
  /* The characters (card #40: "a small character at a desk"): one pose per pill. */
  .sprite { display: block; width: 100%; max-width: 150px; height: 84px; margin-bottom: 4px; }
  .sprite .desk-top, .sprite .monitor { fill: var(--dim); }
  .sprite .skin { fill: #e8a27c; }
  .sprite .hair { fill: #3b3a36; }
  .sprite .zz { fill: var(--dim); font: 700 12px sans-serif; }
  @keyframes type { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-2px); } }
  @keyframes zz { 0% { opacity: 0; transform: translate(0, 0); } 50% { opacity: 1; }
                  100% { opacity: 0; transform: translate(6px, -10px); } }
  @keyframes wave { 0%, 100% { transform: rotate(-10deg); } 50% { transform: rotate(10deg); } }
  @keyframes breathe { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(1px); } }
  .sprite .arm-typing { animation: type .45s infinite; transform-box: fill-box; }
  .sprite .zz { animation: zz 2.4s infinite; }
  .sprite .zz.late { animation-delay: 1.2s; }
  .sprite .flag { animation: wave 1s infinite; transform-origin: bottom left; transform-box: fill-box; }
  .sprite .body-breathe { animation: breathe 3s infinite; }
  /* The dispatcher (the triage lane) is smaller: it routes cards, it does not work them. */
  .desk.dispatcher { flex: 0 1 150px; min-width: 150px; }
  .desk.dispatcher .sprite { height: 66px; }
  .desk.ship { border-style: dashed; }
  @keyframes sort { 0% { opacity: 0; transform: translate(0, 0); } 15% { opacity: 1; }
                    85% { opacity: 1; } 100% { opacity: 0; transform: translate(58px, -6px); } }
  @keyframes push { 0%, 100% { transform: translateX(0); } 50% { transform: translateX(4px); } }
  @keyframes blink { 0%, 100% { opacity: .2; } 50% { opacity: 1; } }
  @keyframes flip { 0%, 40% { transform: rotate(0); } 50%, 90% { transform: rotate(180deg); } 100% { transform: rotate(360deg); } }
  .sprite .sorting { animation: sort 1.6s infinite; }
  .sprite .pushing { animation: push .9s infinite; }
  .sprite .dot { animation: blink 1.2s infinite; }
  .sprite .dot.d2 { animation-delay: .3s; } .sprite .dot.d3 { animation-delay: .6s; }
  .sprite .glass { animation: flip 3s infinite; transform-origin: center; transform-box: fill-box; }
  @media (prefers-reduced-motion: reduce) { .sprite * { animation: none !important; } }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-weight: 600; }
  a { color: inherit; }
  /* Peek: a lane's live session, decoded the same way bin/tail.sh prints it. */
  .peek-btn {
    margin-top: 8px; font-size: 13px; font-weight: 600; padding: 6px 14px;
    border-radius: 999px; border: 1px solid var(--line); background: var(--panel);
    color: var(--ink); cursor: pointer;
  }
  .peek-btn:hover { border-color: var(--dim); }
  #peek-overlay {
    position: fixed; inset: 0; background: rgba(0, 0, 0, .5); z-index: 10;
    display: none; align-items: center; justify-content: center; padding: 16px;
  }
  #peek-overlay.open { display: flex; }
  #peek-panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    width: min(720px, 100%); max-height: 80vh; display: flex; flex-direction: column;
  }
  #peek-panel .peek-head {
    padding: 10px 14px; border-bottom: 1px solid var(--line);
    display: flex; justify-content: space-between; align-items: center;
  }
  #peek-panel .peek-head strong { font-size: 14px; }
  #peek-sub { font-size: 13px; color: var(--dim); }
  #peek-sub.live { color: var(--working); font-weight: 600; }
  #peek-close, .peek-close-btn {
    background: none; border: none; color: var(--dim); font-size: 20px; line-height: 1;
    cursor: pointer; padding: 0 4px;
  }
  #peek-lines {
    margin: 0; padding: 12px 14px; overflow-y: auto; flex: 1;
    font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    white-space: pre-wrap; word-break: break-word;
  }
  /* The ship station's own structured detail (card #105) — step, freshness, the canary
     lane(s) it is waiting on with real links to their cards, and the lands-by time. Empty
     (and so invisible) for a lane's own Peek, which never sets this div at all. */
  #peek-deploy { padding: 10px 14px 0; font-size: 13px; }
  #peek-deploy:empty { padding: 0; }
  #peek-deploy .deploy-step { font-weight: 600; margin-bottom: 2px; }
  #peek-deploy .deploy-detail { color: var(--dim); margin-bottom: 6px; }
  #peek-deploy .deploy-fresh { margin-bottom: 6px; }
  #peek-deploy .deploy-fresh.stale { color: var(--needs); font-weight: 600; }
  #peek-deploy ul.deploy-waiting { list-style: none; margin: 0 0 6px; padding: 0; }
  #peek-deploy ul.deploy-waiting li { margin-bottom: 2px; }
  #peek-deploy .deploy-lands { color: var(--dim); margin-bottom: 8px; }
  #peek-deploy a { color: inherit; text-decoration: underline; }
  /* Review (card #100): a card's pictures, full-window, over everything else — zoom,
     flip, and a way out to the browser's own zoom via a new tab. The stage is sized in
     viewport units so even a tiny fixture image fills it (`object-fit: contain` only
     shrinks the drawn picture inside that box; the element itself stays full size). */
  #review-overlay {
    position: fixed; inset: 0; background: rgba(0, 0, 0, .92); z-index: 30;
    display: none; align-items: center; justify-content: center;
  }
  #review-overlay.open { display: flex; }
  body.review-open { overflow: hidden; }
  #review-stage {
    width: 92vw; height: 74vh; display: flex; align-items: center; justify-content: center;
    overflow: hidden; touch-action: none;
  }
  #review-img {
    width: 100%; height: 100%; object-fit: contain; user-select: none; -webkit-user-drag: none;
  }
  .review-nav {
    position: absolute; top: 50%; transform: translateY(-50%); z-index: 2;
    border: none; background: rgba(255, 255, 255, .15); color: #fff; width: 48px; height: 48px;
    border-radius: 999px; font-size: 26px; line-height: 1; cursor: pointer;
  }
  .review-nav:hover { background: rgba(255, 255, 255, .28); }
  #review-prev { left: 10px; } #review-next { right: 10px; }
  #review-close {
    position: absolute; top: 8px; right: 10px; z-index: 3; border: none; background: none;
    color: #fff; font-size: 32px; line-height: 1; cursor: pointer; padding: 4px 12px;
  }
  #review-caption {
    position: absolute; left: 10px; right: 10px; bottom: 54px; text-align: center; color: #fff;
    font-size: 14px;
  }
  #review-counter {
    position: absolute; left: 0; right: 0; bottom: 34px; text-align: center;
    color: rgba(255, 255, 255, .7); font-size: 12px;
  }
  #review-newtab {
    position: absolute; left: 50%; bottom: 8px; transform: translateX(-50%); color: #fff;
    font-size: 13px; font-weight: 600; text-decoration: none; border: 1px solid rgba(255, 255, 255, .4);
    border-radius: 999px; padding: 5px 14px;
  }
  #review-newtab:hover { background: rgba(255, 255, 255, .12); }
  /* ---- The studio: an office the lanes walk around in (card #88; rooms since October 9,
     2026, a floor plan with hallways, a server room and the boards in Steve's office the
     same day). The floor page above is untouched; this is a second view of the same data,
     toggled by one button. It is drawn for a desk's browser: at least 960px wide, and a
     phone pans it. ---- */
  .view-toggle {
    font-size: 13px; font-weight: 600; padding: 6px 14px; border-radius: 999px;
    border: 1px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer;
  }
  .view-toggle:hover { border-color: var(--dim); }
  #studio { display: none; }
  #studio.open { display: block; }
  #studio-head { display: flex; justify-content: center; margin-bottom: 10px; }
  :root {
    --wall: color-mix(in srgb, var(--dim) 55%, var(--panel));
    --wood-a: #8a5a2b; --wood-b: #7a4e24; --rug: #5b3a6e;
    --tile-a: color-mix(in srgb, var(--dim) 10%, var(--panel)); --tile-line: color-mix(in srgb, var(--dim) 28%, transparent);
    --sky: #9ecbff; --sky-dark: #5d8fc4;
  }
  #room {
    position: relative; width: 100%; min-width: 960px; margin: 0 auto; box-sizing: border-box;
    background: var(--tile-a); border: 1px solid var(--line); border-radius: 14px;
    /* The hallways' tiles: everything not a room is floor you can walk. The height is the
       script's, as tall as its rooms need. */
    background-image: linear-gradient(var(--tile-line) 1px, transparent 1px),
                      linear-gradient(90deg, var(--tile-line) 1px, transparent 1px);
    background-size: 24px 24px;
  }
  /* Rooms: walls with a little depth, and a floor each — carpet for a team, planks in the
     break room, a rug in Steve's office, raised tiles in the server room. */
  .office-room {
    position: absolute; box-sizing: border-box; border: 3px solid var(--wall); border-radius: 4px;
    box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--ink) 8%, transparent), 0 2px 6px rgba(0, 0, 0, .25);
  }
  .office-room.team {
    background: repeating-linear-gradient(45deg, color-mix(in srgb, var(--working) 9%, var(--panel)) 0 6px,
                                          color-mix(in srgb, var(--working) 13%, var(--panel)) 6px 12px);
  }
  .office-room.break {
    background: repeating-linear-gradient(90deg, color-mix(in srgb, var(--wood-a) 55%, var(--panel)) 0 26px,
                                          color-mix(in srgb, var(--wood-b) 70%, var(--panel)) 26px 28px);
  }
  .office-room.boss {
    background: repeating-linear-gradient(90deg, color-mix(in srgb, var(--wood-b) 70%, var(--panel)) 0 26px,
                                          color-mix(in srgb, #4e2f14 80%, var(--panel)) 26px 28px);
  }
  .office-room.boss .rug {
    position: absolute; left: 10%; right: 10%; top: 60px; bottom: 10px; border-radius: 6px;
    background: color-mix(in srgb, var(--rug) 55%, var(--panel)); border: 3px double color-mix(in srgb, #d4b06a 70%, transparent);
    pointer-events: none;
  }
  .office-room.server {
    background: color-mix(in srgb, #1b3a5c 70%, var(--panel));
    background-image: linear-gradient(rgba(255, 255, 255, .06) 1px, transparent 1px),
                      linear-gradient(90deg, rgba(255, 255, 255, .06) 1px, transparent 1px);
    background-size: 16px 16px; cursor: help;
  }
  .office-room.server:hover { box-shadow: inset 0 0 0 2px color-mix(in srgb, #58a6ff 60%, transparent), 0 2px 10px rgba(88, 166, 255, .4); }
  .room-name {
    position: absolute; top: 3px; left: 8px; font-size: 11px; font-weight: 700; z-index: 2;
    color: var(--ink); text-decoration: none; white-space: nowrap; padding: 1px 6px; border-radius: 3px;
    background: color-mix(in srgb, var(--panel) 80%, transparent);
  }
  a.room-name:hover { text-decoration: underline; }
  .room-door {
    /* A gap in the wall onto the hallway, with its mat. */
    position: absolute; width: 28px; height: 9px; border-radius: 2px; z-index: 2;
    background: color-mix(in srgb, var(--wood-a) 60%, var(--panel)); box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .25);
  }
  .prop { position: absolute; pointer-events: none; }
  .prop svg { display: block; }
  .prop.clickable { pointer-events: auto; cursor: pointer; }
  .prop.clickable:hover svg { filter: brightness(1.15); }
  @keyframes steam { 0% { opacity: 0; transform: translateY(0); } 50% { opacity: .8; }
                     100% { opacity: 0; transform: translateY(-5px); } }
  .prop .steam { animation: steam 2s infinite; }
  @keyframes led { 0%, 100% { opacity: .25; } 50% { opacity: 1; } }
  .prop .led { animation: led 1.4s infinite; }
  .prop .led.l2 { animation-duration: 2.1s; animation-delay: .4s; }
  .prop .led.l3 { animation-duration: .9s; animation-delay: .2s; }
  .prop .led.l4 { animation-duration: 1.7s; animation-delay: .9s; }
  .whiteboard {
    position: absolute; top: 4px; right: 6px; font-size: 9px; color: var(--dim); font-weight: 600; z-index: 2;
    background: var(--panel); border: 1.5px solid var(--line); border-radius: 2px; padding: 2px 5px;
    white-space: nowrap; max-width: calc(100% - 90px); overflow: hidden; text-overflow: ellipsis;
  }
  .couch {
    position: absolute; height: 24px; border-radius: 8px 8px 4px 4px;
    background: color-mix(in srgb, var(--sleep) 40%, var(--panel)); border: 2px solid color-mix(in srgb, var(--sleep) 60%, var(--line));
  }
  .couch::before {
    content: ""; position: absolute; left: 8%; right: 8%; top: 6px; height: 0;
    border-top: 1.5px dashed color-mix(in srgb, var(--panel) 60%, transparent);
  }
  .hall-label {
    position: absolute; font-size: 9px; color: var(--dim); font-weight: 700;
    text-transform: uppercase; letter-spacing: .08em; white-space: nowrap; opacity: .8;
  }
  #beacon {
    /* The light over Steve's door, inside #room's own box (which clips: `overflow:
       hidden`) — a light placed outside that box is a light nobody sees. */
    position: absolute; transform: translate(-50%, -50%); z-index: 4;
    width: 12px; height: 12px; border-radius: 50%;
  }
  .beacon-green { background: var(--working); box-shadow: 0 0 6px var(--working); }
  .beacon-amber { background: var(--paused); box-shadow: 0 0 6px var(--paused); }
  .beacon-red { background: var(--needs); box-shadow: 0 0 8px var(--needs); animation: blink 1.2s infinite; }
  /* Desks: a top, a monitor whose screen scans while the desk is lit, a keyboard, a mug,
     and the chair in front, where its lane stands. */
  .desk-set { position: absolute; transform: translate(-50%, -100%); pointer-events: none; }
  .desk-set svg { display: block; }
  @keyframes scan { 0% { transform: translateY(0); } 100% { transform: translateY(7px); } }
  .desk-set .scanline { opacity: 0; }
  .desk-set.lit .scanline { opacity: .7; animation: scan 1.1s linear infinite; }
  /* An empty desk still says whose it is, so a room with everyone out still reads. */
  .desk-tag {
    position: absolute; transform: translate(-50%, 2px); font-size: 9px; font-weight: 600;
    color: var(--dim); opacity: .8; white-space: nowrap; line-height: 1.2;
    background: color-mix(in srgb, var(--panel) 70%, transparent); padding: 0 3px; border-radius: 2px;
  }
  #room-rooms, #room-desks, #room-characters, #room-stuck { position: absolute; inset: 0; pointer-events: none; }
  #room-rooms .room-name, #room-rooms .office-room.server { pointer-events: auto; }
  .character {
    /* As wide as its own plate, so translate(-50%) centers the figure and the words together
       over the spot; never wider than a standing spot, so two neighbours never touch. The
       walk is a Web Animation the script drives along the hallways, not a CSS transition. */
    position: absolute; width: max-content; max-width: 70px; transform: translate(-50%, -100%);
    cursor: pointer; text-align: center; pointer-events: auto;
  }
  @keyframes character-wave { 0%, 100% { transform: rotate(-8deg); } 50% { transform: rotate(8deg); } }
  .character-wave { animation: character-wave .6s ease-in-out infinite; }
  @keyframes walk-bob { 0%, 100% { transform: translateY(0) rotate(-2deg); } 50% { transform: translateY(-2px) rotate(2deg); } }
  .character.walking svg { animation: walk-bob .32s ease-in-out infinite; }
  .character.walking .plate { opacity: .6; }
  @media (prefers-reduced-motion: reduce) {
    .character-wave, .character.walking svg, .prop .steam, .prop .led,
    .character.ship .ship-push, .character.ship .dot { animation: none; }
  }
  .character svg { width: 30px; height: 34px; display: block; margin: 0 auto; }
  .character.ship svg { width: 40px; }
  /* Cookie time (October 9, 2026, promised to Chloe): click the vending machine and it
     spews cookies; everyone in the break room runs over and eats one for a bit. */
  #room-cookies { position: absolute; inset: 0; pointer-events: none; }
  .cookie { position: absolute; width: 11px; height: 11px; transform: translate(-50%, -50%); }
  .cookie svg { display: block; width: 100%; height: 100%; }
  .cookie-shout {
    position: absolute; transform: translate(-50%, -100%); font-size: 11px; font-weight: 800;
    color: var(--ink); background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 2px 7px; white-space: nowrap; animation: cookie-shout .5s ease-out;
  }
  @keyframes cookie-shout { 0% { transform: translate(-50%, -60%) scale(.4); opacity: 0; } 100% { transform: translate(-50%, -100%) scale(1); opacity: 1; } }
  @keyframes nom { 0%, 100% { transform: rotate(0); } 25% { transform: rotate(-4deg) translateY(-1px); } 75% { transform: rotate(4deg); } }
  .character.eating svg { animation: nom .5s ease-in-out infinite; }
  .character.eating.walking svg { animation: walk-bob .32s ease-in-out infinite; }
  @media (prefers-reduced-motion: reduce) { .character.eating svg, .cookie-shout { animation: none; } }
  .character.ship .ship-push { animation: push .9s infinite; }
  .character.ship .dot { animation: blink 1.2s infinite; }
  .character.ship .dot.d2 { animation-delay: .3s; } .character.ship .dot.d3 { animation-delay: .6s; }
  .character-mockup { margin-bottom: 2px; }
  .character-mockup .mockup-thumb {
    display: block; margin: 0 auto; max-width: 46px; max-height: 34px; border-radius: 3px;
  }
  .character .plate {
    font-size: 9px; color: var(--ink); font-weight: 600; white-space: nowrap; margin-top: 2px;
    line-height: 1.2; overflow: hidden; text-overflow: ellipsis;
    background: color-mix(in srgb, var(--panel) 75%, transparent); border-radius: 2px; padding: 0 2px;
  }
  .character .plate .where { color: var(--dim); font-weight: 500; }
  .bubble {
    position: absolute; transform: translate(-50%, calc(-100% - 10px));
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 4px 8px; font-size: 11px; max-width: 160px;
    box-shadow: 0 2px 6px rgba(0, 0, 0, .18); cursor: pointer; z-index: 5;
  }
  .bubble::after {
    content: ""; position: absolute; left: 50%; bottom: -5px; transform: translateX(-50%);
    border: 5px solid transparent; border-top-color: var(--panel);
  }
  /* The server room's readout, shown while the pointer is over the room (or after a tap). */
  #server-tip {
    position: absolute; z-index: 6; display: none; width: 340px; box-sizing: border-box;
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
    font-size: 12px; box-shadow: 0 4px 14px rgba(0, 0, 0, .3); pointer-events: none;
  }
  #server-tip.open { display: block; }
  #server-tip h4 { margin: 0 0 4px; font-size: 12px; color: var(--ink); }
  #server-tip h4 + h4, #server-tip .tip-row + h4 { margin-top: 10px; }
  .tip-row { display: flex; align-items: center; gap: 6px; color: var(--dim); line-height: 1.5; }
  .tip-row .k { flex: 0 0 96px; color: var(--ink); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tip-row .v { white-space: nowrap; }
  .tip-row.stopped { opacity: .5; }
  .meter { flex: 0 0 60px; height: 6px; border-radius: 3px; background: color-mix(in srgb, var(--dim) 20%, transparent); overflow: hidden; }
  .meter i { display: block; height: 100%; background: var(--working); }
  .meter.hot i { background: var(--needs); }
  .meter.warm i { background: var(--paused); }
  /* The boards' popups: the bulletin board (every card being worked and recently worked,
     Peek on any for the card itself) and the wins board. The same panel as Peek, so a
     reader already knows how it closes. */
  #office-modal {
    position: fixed; inset: 0; background: rgba(0, 0, 0, .45); display: none; z-index: 20;
    align-items: center; justify-content: center; padding: 16px;
  }
  #office-modal.open { display: flex; }
  #office-panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    width: min(760px, 100%); max-height: 84vh; display: flex; flex-direction: column;
  }
  #office-panel .peek-head { padding: 10px 14px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  #office-panel .peek-head strong { font-size: 14px; }
  #office-body { overflow-y: auto; padding: 12px 14px; font-size: 13px; }
  #office-body h3 { margin: 10px 0 6px; font-size: 13px; color: var(--dim); text-transform: uppercase; letter-spacing: .05em; }
  #office-body h3:first-child { margin-top: 0; }
  .board-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--line); }
  .board-row:last-child { border-bottom: none; }
  .board-row .num { font-weight: 700; min-width: 48px; }
  .board-row .num a { color: inherit; text-decoration: none; }
  .board-row .num a:hover { text-decoration: underline; }
  .board-row .what { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .board-row .meta { color: var(--dim); font-size: 12px; white-space: nowrap; }
  .board-row .pill { margin-left: 0; }
  .empty-note { color: var(--dim); font-style: italic; }
  .card-view .labels { display: flex; gap: 6px; flex-wrap: wrap; margin: 6px 0 10px; }
  .card-view .label { font-size: 11px; border: 1px solid var(--line); border-radius: 999px; padding: 1px 8px; color: var(--dim); }
  .card-body {
    white-space: pre-wrap; word-break: break-word; margin: 0; font: 13px/1.5 inherit;
    font-family: inherit; background: color-mix(in srgb, var(--dim) 6%, transparent); padding: 10px 12px; border-radius: 6px;
  }
  .card-comment { margin-top: 10px; border-left: 3px solid var(--line); padding-left: 10px; }
  .card-comment .who { font-size: 12px; color: var(--dim); margin-bottom: 2px; }
  .win-count { font-weight: 700; color: var(--working); }
</style>
</head>
<body>
<header>
  <h1 id="box">The floor</h1>
  <div class="muted" id="clock"></div>
  <div class="muted" id="github-budget"></div>
  <div class="muted" id="github-spend"></div>
  <button class="view-toggle" id="studio-btn" type="button">Studio</button>
</header>
<div class="alert" id="load-error" style="display:none">Can't reach the floor's own data right now — retrying&hellip;</div>
<div class="alert" id="script-error" style="display:none">This page's own code hit an error, so what is below may be empty or out of date. If a reload does not clear this line, the page is broken and needs a card.</div>
<script>
// A block of its own, first, and as small as it can be: if the page's main script below
// fails — even fails to parse, which stops every line of it — this still runs and says so.
// Card #71: the main script did not parse, and the page sat there blank with nothing to
// say why.
window.addEventListener("error", function () {
  document.getElementById("script-error").style.display = "block";
});
window.addEventListener("unhandledrejection", function () {
  document.getElementById("script-error").style.display = "block";
});
</script>
<div id="floor-view">
<div id="projects"></div>
<section class="project">
  <h2>Recent ticks</h2>
  <div class="table-wrap"><table id="recent"><thead><tr><th>Project</th><th>Lane</th><th>Card</th><th>When</th><th>Quota</th></tr></thead><tbody></tbody></table></div>
</section>
</div>
<div id="studio">
  <div id="studio-head"><button class="view-toggle" id="floor-btn" type="button">Floor</button></div>
  <div id="room">
    <div id="room-rooms"></div>
    <div id="beacon" class="beacon-green"></div>
    <div id="room-desks"></div>
    <div id="room-stuck"></div>
    <div id="room-cookies"></div>
    <div id="room-characters"></div>
    <div id="server-tip"></div>
  </div>
</div>
<div id="office-modal">
  <div id="office-panel">
    <div class="peek-head">
      <div><strong id="office-title"></strong> <span id="office-sub" class="muted"></span></div>
      <div><button id="office-back" class="view-toggle" type="button">&larr; Back</button>
        <button id="office-close" class="peek-close-btn" aria-label="Close">&times;</button></div>
    </div>
    <div id="office-body"></div>
  </div>
</div>
<div id="peek-overlay">
  <div id="peek-panel">
    <div class="peek-head">
      <div><strong id="peek-title"></strong> <span id="peek-sub"></span></div>
      <button id="peek-close" aria-label="Close">&times;</button>
    </div>
    <div id="peek-deploy"></div>
    <pre id="peek-lines"></pre>
  </div>
</div>
<div id="review-overlay">
  <div id="review-stage"><img id="review-img" alt=""></div>
  <button class="review-nav" id="review-prev" type="button" aria-label="Previous picture">&lsaquo;</button>
  <button class="review-nav" id="review-next" type="button" aria-label="Next picture">&rsaquo;</button>
  <button id="review-close" type="button" aria-label="Close">&times;</button>
  <div id="review-caption"></div>
  <div id="review-counter"></div>
  <a id="review-newtab" href="#" target="_blank" rel="noopener">Open in new tab ↗</a>
</div>
<script id="main">
const PILL_LABEL = {working: "Working", idle: "Idle", needs_steve: "Needs Steve",
                     paused: "Paused", sleeping: "Sleeping"};

// Card #110: a working lane can also be pausing (a PAUSE file sits beside its held card).
// The backend hands back the file's own first line as `pausing_reason` — "deploy <sha>"
// for the deployer and the canary stage, "" for a hand-touched one — and this turns it
// into plain words, never the sha or the file's path (`talking-to-steve.md`, rule 8).
function pausingLine(d) {
  if (!d.pausing) return "";
  const forDeploy = (d.pausing_reason || "").startsWith("deploy");
  return `<div class="pausing">pausing after this card${forDeploy ? " for a deploy" : ""}</div>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

function relTime(iso) {
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 90) return "just now";
  if (d < 3600) return Math.round(d / 60) + "m ago";
  if (d < 86400) return Math.round(d / 3600) + "h ago";
  return Math.round(d / 86400) + "d ago";
}

// One lane's character. Its shirt and screen take the pill's own colour, so the picture
// and the pill always agree; the pose says the same thing the pill does.
const PILL_COLOR = {working: "--working", idle: "--idle", needs_steve: "--needs",
                    paused: "--paused", sleeping: "--sleep"};
function spriteSVG(pill) {
  const c = `var(${PILL_COLOR[pill] || "--idle"})`;
  const screenLit = pill === "working" || pill === "needs_steve";
  // Head position: upright at work, drooping when idle, face down on the desk when
  // sleeping (drawn after the body then, or the body would hide it).
  const headAt = {idle: [0, 4], sleeping: [16, 24]}[pill] || [0, 0];
  const head = `<g transform="translate(${headAt[0]},${headAt[1]})">
      <rect class="skin" x="24" y="22" width="16" height="16" rx="3"/>
      <rect class="hair" x="24" y="20" width="16" height="6" rx="2"/></g>`;
  const arm = pill === "working"
    ? `<rect class="arm-typing" x="42" y="46" width="14" height="5" rx="2" style="fill:${c}"/>`
    : `<rect x="40" y="52" width="12" height="5" rx="2" style="fill:${c}"/>`;
  let extra = "";
  if (pill === "idle" || pill === "sleeping")
    extra = `<text class="zz" x="44" y="${pill === "sleeping" ? 34 : 22}">z</text>
             <text class="zz late" x="52" y="${pill === "sleeping" ? 26 : 14}">z</text>`;
  if (pill === "needs_steve")
    extra = `<g class="flag"><rect x="12" y="6" width="2" height="30" class="monitor"/>
             <rect x="14" y="6" width="14" height="10" style="fill:${c}"/></g>`;
  if (pill === "paused")
    extra = `<rect x="27" y="4" width="3" height="11" rx="1" style="fill:${c}"/>
             <rect x="34" y="4" width="3" height="11" rx="1" style="fill:${c}"/>`;
  return `<svg class="sprite" viewBox="0 0 100 80" aria-hidden="true">
    <rect class="desk-top" x="10" y="62" width="80" height="6" rx="1"/>
    <rect class="monitor" x="54" y="38" width="30" height="22" rx="2"/>
    <rect x="57" y="41" width="24" height="16" style="fill:${screenLit ? c : "var(--line)"};opacity:${screenLit ? .55 : 1}"/>
    <rect class="monitor" x="66" y="60" width="6" height="3"/>
    <g class="${pill === "idle" ? "body-breathe" : ""}">${pill === "sleeping" ? "" : head}
      <rect x="20" y="40" width="24" height="22" rx="3" style="fill:${c}"/>${arm}
      ${pill === "sleeping" ? head : ""}</g>
    ${extra}</svg>`;
}

// The triage lane is the dispatcher: a smaller figure at a counter with an inbox tray,
// sending cards on to the lanes after it.
function dispatcherSVG(pill) {
  const c = `var(${PILL_COLOR[pill] || "--idle"})`;
  const headAt = {sleeping: [8, 16]}[pill] || [0, 0];
  const head = `<g transform="translate(${headAt[0]},${headAt[1]})">
      <rect class="skin" x="44" y="24" width="13" height="13" rx="3"/>
      <rect class="hair" x="44" y="22" width="13" height="5" rx="2"/></g>`;
  let extra = "";
  if (pill === "working")
    extra = `<rect class="sorting" x="14" y="46" width="10" height="7" rx="1" style="fill:${c}"/>`;
  if (pill === "needs_steve")
    extra = `<g class="flag"><rect x="60" y="8" width="16" height="12" rx="1" style="fill:${c}"/>
             <text x="64" y="18" style="fill:var(--panel);font:700 10px sans-serif">?</text></g>`;
  if (pill === "paused")
    extra = `<rect x="46" y="8" width="3" height="10" rx="1" style="fill:${c}"/>
             <rect x="52" y="8" width="3" height="10" rx="1" style="fill:${c}"/>`;
  if (pill === "idle" || pill === "sleeping")
    extra = `<text class="zz" x="62" y="${pill === "sleeping" ? 34 : 22}">z</text>`;
  return `<svg class="sprite" viewBox="0 0 100 80" aria-hidden="true">
    <rect class="desk-top" x="6" y="62" width="88" height="6" rx="1"/>
    <rect class="monitor" x="8" y="54" width="22" height="8" rx="1"/>
    <path d="M78 44 l6 5 l-6 5" style="fill:none;stroke:var(--dim);stroke-width:2"/>
    <path d="M86 44 l6 5 l-6 5" style="fill:none;stroke:var(--dim);stroke-width:2"/>
    <g class="${pill === "idle" ? "body-breathe" : ""}">${pill === "sleeping" ? "" : head}
      <rect x="40" y="39" width="21" height="23" rx="3" style="fill:${c}"/>
      ${pill === "sleeping" ? head : ""}</g>
    ${extra}</svg>`;
}

// The ship station, after the lanes: CI on main, and for the watcher's own board the box's
// deployer too. Not a lane — nothing here works a card.
const SHIP_PILL = {ok: ["working", "All green"], busy: ["paused", "Shipping"],
                   blocked: ["needs_steve", "Blocked"], unknown: ["idle", "Unknown"]};
// laneReleaseLines (card #106) — one line per project naming how many of its lanes have
// already followed the pointer to the live release, and which have not (still finishing a
// tick on an older one — never disturbed, just not there yet). `live` is the deployer's
// own 7-char sha; a lane's own release name is 12 — compared on the 7 both share, which is
// exactly as precise as every other short-sha the page already shows.
function laneReleaseLines(dep, lanes) {
  if (!dep || !lanes || !lanes.length) return [];
  const byProject = {};
  lanes.forEach(l => { (byProject[l.project] = byProject[l.project] || []).push(l); });
  return Object.keys(byProject).sort().map(name => {
    const group = byProject[name];
    const behind = group.filter(l => !l.release || l.release.slice(0, 7) !== dep.live).map(l => l.lane);
    const onIt = group.length - behind.length;
    return behind.length
      ? `${esc(name)}: ${onIt} of ${group.length} lane(s) on it (${behind.map(esc).join(", ")} still finishing on an older one)`
      : `${esc(name)}: all ${group.length} lane(s) on it`;
  });
}
function shipState(ship) {
  const ci = (ship && ship.ci) || {state: "unknown"};
  const dep = ship && ship.deploy;
  const lines = [];
  const ciLine = {passing: "all checks passed", running: "checks running",
                  failing: "failing: " + (ci.failing || []).join(", "), none: "no checks"}[ci.state];
  lines.push(ci.state === "unknown" ? "CI unreachable" : `main ${esc(ci.sha)}: ${esc(ciLine)}`);
  if (dep) {
    lines.push({live: `live on the box: ${esc(dep.live)}`,
                checking: `checking ${esc(dep.target)}: CI and tests`,
                testing: `retesting ${esc(dep.target)}`,
                held: `held: ${esc(dep.target)}` + (dep.card ? ` (#${dep.card.number})` : "")}[dep.state] || "");
    lines.push(...laneReleaseLines(dep, ship.lanes));
  }
  let pill = "ok", mode = "done";
  if ((dep && dep.state === "held") || ci.state === "failing") { pill = "blocked"; mode = "blocked"; }
  else if ((dep && dep.state !== "live") || ci.state === "running") { pill = "busy"; mode = "checking"; }
  else if (ci.state === "unknown" && !dep) { pill = "unknown"; mode = "idle"; }
  const link = (dep && dep.card && dep.card.url) || ci.url || "";
  return {pill, mode, lines, link, deploys: !!dep};
}
function shipSVG(mode, pill) {
  const c = `var(--${{working: "working", paused: "paused", needs_steve: "needs", idle: "idle"}[SHIP_PILL[pill][0]]})`;
  let sign = "";
  if (mode === "done") sign = `<path d="M58 22 l6 6 l12 -14" style="fill:none;stroke:${c};stroke-width:4"/>`;
  if (mode === "checking") sign = `<circle class="dot" cx="58" cy="24" r="3" style="fill:${c}"/>
      <circle class="dot d2" cx="67" cy="24" r="3" style="fill:${c}"/><circle class="dot d3" cx="76" cy="24" r="3" style="fill:${c}"/>`;
  if (mode === "waiting") sign = `<g class="glass"><path d="M60 12 h14 l-7 10 l7 10 h-14 l7 -10 z" style="fill:${c}"/></g>`;
  if (mode === "blocked") sign = `<g class="flag"><rect x="12" y="8" width="2" height="28" class="monitor"/>
      <rect x="14" y="8" width="14" height="10" style="fill:${c}"/></g>`;
  return `<svg class="sprite" viewBox="0 0 100 80" aria-hidden="true">
    <rect class="desk-top" x="10" y="62" width="80" height="6" rx="1"/>
    <g class="${mode === "pushing" ? "pushing" : ""}">
      <rect x="52" y="38" width="30" height="24" rx="2" style="fill:${c};opacity:.35"/>
      <rect x="52" y="38" width="30" height="24" rx="2" style="fill:none;stroke:${c};stroke-width:2"/>
      <path d="M52 46 h30" style="stroke:${c};stroke-width:2"/></g>
    <rect class="skin" x="24" y="22" width="16" height="16" rx="3"/>
    <rect class="hair" x="24" y="20" width="16" height="6" rx="2"/>
    <rect x="20" y="40" width="24" height="22" rx="3" style="fill:${c}"/>
    <rect class="${mode === "pushing" ? "arm-typing" : ""}" x="42" y="46" width="10" height="5" rx="2" style="fill:${c}"/>
    ${sign}</svg>`;
}
function shipHTML(ship, project) {
  const st = shipState(ship);
  const [cls, label] = SHIP_PILL[st.pill];
  // The same shape as a lane's desk: a link, and Peek beside it — what CI did on the
  // newest commit, step by step, the commits before it, and the deployer's last lines.
  return `<div class="desk ship">
    <a class="desk-link" href="${esc(st.link)}" target="_blank" rel="noopener">
      ${shipSVG(st.mode, st.pill)}
      <div class="name">Ship</div>
      <div class="unit">${st.deploys ? "CI and the box's deployer" : "CI on main"}</div>
      <span class="pill ${cls}">${label}</span>
      ${st.lines.map(l => `<div class="detail">${l}</div>`).join("")}
    </a>
    <button class="peek-btn" type="button" data-peek-ship="${esc(project)}" data-peek-name="Ship (${esc(project)})">Peek</button>
  </div>`;
}

// A mockup or a screenshot a Needs Steve card's own Stuck comment pointed at (card #91),
// shown right here instead of making Steve click through to the card to find it. Several
// thumbnails stack in one row; a broken or slow image collapses to nothing rather than a
// visible placeholder box, since a missing picture is not itself news.
function mockupThumbsHTML(images, cls) {
  if (!images || !images.length) return "";
  return `<div class="${cls || "mockups"}">${images.map(u => `
    <img class="mockup-thumb" src="${esc(u)}" alt="Mockup to approve" loading="lazy"
         onerror="this.style.display='none'">`).join("")}</div>`;
}

// A card's own `Needs Steve:` ask, read back from the comment that made it (board #96's
// first line keeps the comment's own `**...**` wrapping) — rendered, not shown raw, so no
// `**` ever reaches the page (card #100: "the ask line shows raw ** markdown"). `esc()`
// first, so the regexes only ever see the three markers, never anything a title injected.
function renderAsk(text) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/_(.+?)_/g, "<em>$1</em>");
}

// The Needs Steve carousel's own state (card #96): every picture a Stuck card's Needs
// Steve comments have shown, newest round first, lettered by direction when the comment
// named one — shared by the strip, the studio's in-tray, the podium's held thumbnail and
// the Review overlay (#100), so none of them ever disagree about which picture a card is
// "on". The slide a viewer left a card on persists across refreshes (keyed by card
// number, not re-read from the server), so a poll mid-flip never snaps it back to the
// first picture.
const carouselSlide = {};

function stuckSlides(entry) {
  const slides = [];
  (entry.rounds || []).forEach((r, i) => {
    (r.images || []).forEach(img => slides.push({...img, when: r.when, dated: i !== 0}));
  });
  return slides;
}

// The slide a card's carousel is on right now — clamped into range, so a prev/next past
// either end wraps instead of pointing at nothing.
function currentSlide(entry) {
  const slides = stuckSlides(entry);
  if (!slides.length) return {slides, idx: 0, slide: null};
  const n = entry.card.number;
  let idx = carouselSlide[n] || 0;
  if (idx < 0) idx = slides.length - 1;
  if (idx >= slides.length) idx = 0;
  carouselSlide[n] = idx;
  return {slides, idx, slide: slides[idx]};
}

function stepCarousel(cardNum, dir) {
  carouselSlide[cardNum] = (carouselSlide[cardNum] || 0) + dir;
  if (lastState) { render(lastState); renderStudio(lastState); }
}

// Every card Stuck on this board, whichever lane last touched it (card #96's "why
// nothing shows today": a lane's own desk only ever shows the one card it last worked, so
// a design lane that parked three cards and moved on to a fourth showed none of the
// three) — card number, its ask rendered (never raw `**`), the directions it offers as
// chips, and a Review button that opens the full-window overlay (#100: the old inline
// sliver carousel "fits a full-page screenshot into a short box, so every picture is a
// sliver you cannot read").
function directionChipsHTML(slides) {
  const letters = new Map();
  for (const s of slides) {
    if (!s.letter) continue;
    letters.set(s.letter, letters.get(s.letter) || s.recommended);
  }
  if (!letters.size) return "";
  return `<div class="direction-chips">${[...letters.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    .map(([letter, rec]) => `<span class="direction-chip${rec ? " recommended" : ""}">${esc(letter)}${rec ? " ★" : ""}</span>`)
    .join("")}</div>`;
}

function stuckStripHTML(stuck) {
  if (!stuck || !stuck.length) return "";
  return `<div class="needs-strip">${stuck.map(s => {
    const slides = stuckSlides(s);
    return `
    <div class="needs-card">
      <a class="needs-ask" href="${esc(s.card.url)}" target="_blank" rel="noopener">#${s.card.number}: ${s.ask ? renderAsk(s.ask) : "needs a decision"}</a>
      ${slides.length ? `${directionChipsHTML(slides)}
        <button class="review-btn" type="button" data-review="${s.card.number}" aria-label="Review the pictures for #${s.card.number}">Review</button>` : ""}
    </div>`;
  }).join("")}</div>`;
}

// Every stuck card currently drawn anywhere on the page, by card number — built fresh at
// the top of every `render()` (which always runs before `renderStudio()` on the same
// state), so the strip, the in-tray, the podium and a lane's own desk tile all resolve a
// Review button against the one shared answer for "what does this card have to show".
let stuckByCard = {};

// A lane's needs_steve pill carries only a flat list of image URLs (`images_fn`, card
// #91) — no letters, no rounds — because that is all `lane_desk` ever needed before the
// overlay existed. `stuckByCard` has the richer shape whenever this card is also the
// board's own Stuck card, which is always, except a demo fixture with no `stuck` entry of
// its own; that gets a one-round entry synthesized from the flat list so its Review button
// still works. `images` wrapped in nothing is a card with no picture to show at all.
function resolveStuckEntry(num, images) {
  if (num == null) return null;
  const entry = stuckByCard[num];
  if (entry && stuckSlides(entry).length) return entry;
  if (images && images.length) {
    return {card: {number: num},
            rounds: [{when: null, images: images.map(u => ({url: u, label: "", letter: null, recommended: false}))}]};
  }
  return null;
}

function reviewButtonHTML(num, images, cls) {
  if (!resolveStuckEntry(num, images)) return "";
  const fallback = stuckByCard[num] ? "" : ` data-review-images='${esc(JSON.stringify(images || []))}'`;
  return `<button class="review-btn${cls ? " " + cls : ""}" type="button" data-review="${num}"${fallback}
    aria-label="Review the pictures for #${num}">Review</button>`;
}

function openReviewFromEl(el) {
  let images = null;
  if (el.dataset.reviewImages) { try { images = JSON.parse(el.dataset.reviewImages); } catch (err) { images = null; } }
  openReview(Number(el.dataset.review), images);
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-review]");
  if (!btn) return;
  e.preventDefault(); e.stopPropagation();
  openReviewFromEl(btn);
});

// card #103: a working desk whose plan comment has not landed yet — "no plan yet" once
// `plan_posted` has been injected at all (older callers that never pass a `plan` into
// lane_desk get neither field, and neither this suffix nor the amber warning below).
// `plan_late` (the lane's own warning, amber like Ship reads when something is blocked
// once it has gone PLAN_LATE_MIN minutes with none) is computed server-side, in Python.
function planSuffix(d) {
  if (d.plan_posted === undefined) return "";
  return d.plan_posted ? " · plan posted" : " · no plan yet";
}
function deskHTML(d) {
  let detail = "";
  const planLate = d.pill === "working" && d.plan_late === true;
  if (d.pill === "working") detail = `#${d.card.number} · ${d.stage ? esc(d.stage) : "—"} · ${d.minutes}m${planSuffix(d)}`;
  else if (d.pill === "needs_steve") detail = `#${d.card.number}: ${d.ask ? renderAsk(d.ask) : "needs a decision"}`;
  else if (d.pill === "idle" || d.pill === "sleeping") detail = esc(d.reason || "");
  const dispatcher = d.kind === "Triage";
  // Peek is on every lane's desk, whatever its pill: live while the lane works, its last
  // session when it does not. (It used to exist only on a working desk — so on a quiet
  // floor, which is most of the time, the feature was nowhere to be seen.)
  return `<div class="desk${dispatcher ? " dispatcher" : ""}">
    <a class="desk-link" href="${esc(d.link)}" target="_blank" rel="noopener">
      ${dispatcher ? dispatcherSVG(d.pill) : spriteSVG(d.pill)}
      <div class="name">${esc(d.kind)}</div>
      <div class="unit">${esc(d.unit)}</div>
      <span class="pill ${esc(d.pill)}${planLate ? " plan-late" : ""}">${PILL_LABEL[d.pill] || esc(d.pill)}</span>
      <div class="detail">${detail}</div>
      ${pausingLine(d)}
      ${d.pill === "needs_steve" ? mockupThumbsHTML(d.images) : ""}
      ${d.pill === "needs_steve" ? reviewButtonHTML(d.card.number, d.images) : ""}
    </a>
    <button class="peek-btn" type="button" data-peek-lane="${esc(d.lane)}" data-peek-name="${esc(d.kind)} (${esc(d.lane)})">Peek</button>
  </div>`;
}

let lastState = null;

function render(state) {
  lastState = state;
  stuckByCard = {};
  for (const p of state.projects || []) for (const s of p.stuck || []) stuckByCard[s.card.number] = s;
  document.getElementById("box").textContent =
    "The floor — " + state.header.box +
    (state.header.needs_steve_total > 0 ? ` — ${state.header.needs_steve_total} need Steve` : "");
  document.getElementById("clock").textContent = new Date(state.header.time).toLocaleString();

  // The account's GraphQL budget (card #98), read from board.py's own cache — nothing
  // here costs a call of its own. Blank until the first real board read has happened
  // anywhere on the box; never a stale guess.
  const ghEl = document.getElementById("github-budget");
  if (state.header.github_remaining != null) {
    const resetTxt = state.header.github_reset
      ? new Date(state.header.github_reset).toLocaleTimeString() : "?";
    const limitTxt = state.header.github_limit != null ? state.header.github_limit.toLocaleString() : "?";
    ghEl.textContent = `GitHub: ${state.header.github_remaining.toLocaleString()} / ${limitTxt} · resets ${resetTxt}`;
  } else {
    ghEl.textContent = "";
  }

  // This hour's spend by caller (card #114), from the ledger — same free ride as the
  // line above. Red when the current pace would exhaust the remaining points before the
  // hour resets (the October 9, 2026 outage: nothing on the page said where 5,000 points
  // went in nineteen minutes).
  const spendEl = document.getElementById("github-spend");
  const spend = state.header.github_spend || [];
  if (spend.length && state.header.github_points_per_min != null) {
    const ppm = state.header.github_points_per_min;
    // textContent (not innerHTML) below, so a caller name is never HTML-escaped: esc()
    // here would show literal "&amp;" for a caller containing "&", the way every other
    // plain-text line in this header is already written (ghEl, two lines up).
    const parts = spend.map(s => `${s.caller} ${Math.round(s.points)}`);
    spendEl.textContent = `This hour: ${parts.join(" · ")} (${ppm.toFixed(1)} pts/min)`;
    let hot = false;
    if (ppm > 0 && state.header.github_remaining != null && state.header.github_reset) {
      const minsToReset = (new Date(state.header.github_reset) - new Date(state.header.time)) / 60000;
      const minsToExhaust = state.header.github_remaining / ppm;
      hot = minsToExhaust < minsToReset;
    }
    spendEl.classList.toggle("alert", hot);
  } else {
    spendEl.textContent = "";
    spendEl.classList.remove("alert");
  }

  const projects = document.getElementById("projects");
  projects.innerHTML = state.projects.map(p => `
    <section class="project">
      <h2><a href="${esc(p.board_url)}" target="_blank" rel="noopener">${esc(p.name)}</a></h2>
      <div class="counts">Ready ${p.counts.ready} · In progress ${p.counts.in_progress} · Backlog ${p.counts.backlog}
        ${p.board_error ? `<span class="alert"> — board unreachable</span>` : ""}</div>
      <div class="desks">${p.lanes.map(deskHTML).join("")}${shipHTML(p.ship, p.name)}</div>
      ${stuckStripHTML(p.stuck)}
    </section>`).join("");

  const tbody = document.querySelector("#recent tbody");
  tbody.innerHTML = state.recent.map(r => `
    <tr><td>${esc(r.project)}</td><td>${esc(r.lane)}</td>
      <td><a href="${esc(r.card.url)}" target="_blank" rel="noopener">#${r.card.number}</a></td>
      <td>${relTime(r.when)}</td><td>${esc(r.quota)}</td></tr>`).join("");
}

async function refresh() {
  try {
    const r = await fetch("/api/state.json", {cache: "no-store"});
    const state = await r.json();
    render(state);
    renderStudio(state);
    document.getElementById("load-error").style.display = "none";
  } catch (e) {
    // The first load has no "last good state" to fall back on, so a failed fetch would
    // otherwise leave the static, data-less shell up with nothing saying it is still
    // trying — the exact shape of "nothing loads" (card #71). Say so, out loud, and keep
    // retrying; a later success clears it above.
    console.error("floor: refresh failed", e);
    document.getElementById("load-error").style.display = "block";
  }
}

// Peek: a lane's session, decoded the same way bin/tail.sh prints it on the command line —
// live while the lane works, its last session when it does not. Polls only while the panel
// is open — never in the background, never per card.
let peekLane = null, peekCard = null, peekFile = null, peekPos = null, peekTimer = null;
let peekShip = null; // the project whose ship station is open, when it is one of those

function peekSub(text, live) {
  const sub = document.getElementById("peek-sub");
  sub.textContent = text;
  sub.className = live ? "live" : "";
}

function peekStatus(sub, text) {
  peekSub(sub, false);
  document.getElementById("peek-lines").textContent = text;
  peekCard = null; peekFile = null; peekPos = null;
}

async function pollPeek() {
  const lane = peekLane;
  if (!lane) return;
  const params = new URLSearchParams({lane});
  if (peekCard != null) params.set("card", peekCard);
  if (peekFile != null) params.set("file", peekFile);
  if (peekPos != null) params.set("pos", peekPos);
  let data;
  try {
    const r = await fetch("/api/tail.json?" + params.toString(), {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    data = await r.json();
  } catch (e) {
    // Say so in the panel: an open panel that silently stays empty is the same blank
    // page, one click further in.
    if (peekLane === lane) peekSub("can't reach this lane's session right now — retrying", false);
    return;
  }
  if (peekLane !== lane) return; // closed, or switched to another lane, mid-fetch
  if (data.status === "idle") { peekStatus("idle", "Nothing to show yet: no session on file for this lane."); return; }
  if (data.status === "waiting") { peekStatus(`starting #${data.card}`, "The session has not written anything yet."); return; }
  peekSub(data.live ? `working on #${data.card} — live` : `idle — its last session, #${data.card}`, data.live);
  const pre = document.getElementById("peek-lines");
  if (data.reset) pre.textContent = "";
  peekCard = data.card; peekFile = data.file; peekPos = data.pos;
  if (!data.lines.length) return; // nothing new: leave any manual scroll-back alone
  for (const line of data.lines) pre.textContent += (pre.textContent ? "\\n" : "") + line;
  pre.scrollTop = pre.scrollHeight;
}

// A phone-friendly local time ("3:48 PM") for an ISO timestamp the server sent — never the
// raw UTC string, which is what `ship_lines`'s own plain-text fallback (for a reader with
// no browser) has to use instead.
function fmtTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
}

// The Ship desk's own structured detail (card #105): which step, how fresh the last check
// was, which canary lane(s) it is waiting on — with a real, one-tap link to each card — and
// when it lands at the latest. Built from `data.deploy` (the raw dict `deploy_status`
// returns), never from `ship_lines`'s own text lines: a `<pre>`'s `textContent` cannot
// carry a link. `esc()` throughout, since `#peek-deploy` is set via `innerHTML`.
function renderDeployDetail(dep) {
  if (!dep) return "";
  const parts = [];
  if (dep.step) {
    parts.push(`<div class="deploy-step">Step ${dep.step.number} of ${dep.step.total}: ${esc(dep.step.name)}</div>`);
    if (dep.step.detail) parts.push(`<div class="deploy-detail">${esc(dep.step.detail)}</div>`);
  }
  if (dep.alive) {
    let text, stale = false;
    if (dep.alive.running) {
      text = `Running now (started ${Math.floor(dep.alive.since_sec / 60)}m ago)`;
    } else {
      stale = !!dep.alive.stale;
      text = `Checked ${Math.floor(dep.alive.since_sec / 60)}m ago, checks again in `
           + `${Math.floor((dep.alive.next_in_sec || 0) / 60)}m`
           + (stale ? " — overdue, the timer may have stopped" : "");
    }
    parts.push(`<div class="deploy-fresh${stale ? " stale" : ""}">${esc(text)}</div>`);
  }
  const waiting = dep.waiting_for || [];
  if (waiting.length) {
    parts.push(`<ul class="deploy-waiting">${waiting.map(w => `<li>Waiting on `
      + `<a href="${esc(w.card.url)}" target="_blank" rel="noopener">#${w.card.number}</a> `
      + `(${esc(w.project)}/${esc(w.lane)}) — its watchdog ends it by ${esc(fmtTime(w.deadline_at))}</li>`)
      .join("")}</ul>`);
  }
  if (dep.state !== "live") {
    parts.push(`<div class="deploy-lands">${dep.lands_by
      ? `Lands by ${esc(fmtTime(dep.lands_by))} at the latest` : "Landing on the next check"}</div>`);
  }
  return parts.join("");
}

// The ship station's Peek: what CI did on the newest commit, step by step, the commits
// before it, and (on the board this box deploys) the deployer's last lines. The server
// sends finished lines of text; this only shows them. Newest is at the top, so unlike a
// lane's session it never scrolls to the bottom.
async function pollShip() {
  const project = peekShip;
  if (!project) return;
  let data;
  try {
    const r = await fetch("/api/ship.json?" + new URLSearchParams({project}).toString(), {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    data = await r.json();
  } catch (e) {
    if (peekShip === project) peekSub("can't reach the ship station right now — retrying", false);
    return;
  }
  if (peekShip !== project) return; // closed, or switched, mid-fetch
  peekSub(data.sub, data.live);
  document.getElementById("peek-deploy").innerHTML = renderDeployDetail(data.deploy);
  const pre = document.getElementById("peek-lines");
  const text = data.lines.join("\\n");
  if (pre.textContent !== text) pre.textContent = text; // unchanged: leave the scroll alone
}

function openPeek(target, name) {
  peekLane = target.lane || null; peekShip = target.ship || null;
  peekCard = null; peekFile = null; peekPos = null;
  document.getElementById("peek-title").textContent = name || peekLane || peekShip;
  peekSub("", false);
  document.getElementById("peek-deploy").innerHTML = "";
  document.getElementById("peek-lines").textContent = "";
  document.getElementById("peek-overlay").classList.add("open");
  if (peekTimer) clearInterval(peekTimer);
  if (peekShip) { pollShip(); peekTimer = setInterval(pollShip, 15000); }
  else { pollPeek(); peekTimer = setInterval(pollPeek, 2000); }
}

function closePeek() {
  document.getElementById("peek-overlay").classList.remove("open");
  if (peekTimer) { clearInterval(peekTimer); peekTimer = null; }
  peekLane = null; peekShip = null;
}

document.getElementById("peek-close").addEventListener("click", closePeek);
document.getElementById("peek-overlay").addEventListener("click", (e) => {
  if (e.target.id === "peek-overlay") closePeek();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePeek(); });
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".peek-btn");
  if (!btn) return;
  openPeek({lane: btn.dataset.peekLane, ship: btn.dataset.peekShip}, btn.dataset.peekName);
});

// ---- Review (card #100): a card's pictures, full-window, over everything else --------
// Replaces the old inline sliver carousel. A click on any `[data-review]` element (the
// strip's button, the in-tray's, a lane desk's, the studio podium's held thumbnail) opens
// here, on whichever picture `carouselSlide` says this card is on — the same index the
// strip, the in-tray and the podium all read, so flipping here moves them too the next
// time they redraw. Zoom and pan are the overlay's own transform on the image; both reset
// on every flip, per the card's own ask.
let reviewEntry = null, reviewCard = null, reviewZoom = 1, reviewPanX = 0, reviewPanY = 0;

function reviewApplyTransform() {
  const img = document.getElementById("review-img");
  img.style.transform = `translate(${reviewPanX}px, ${reviewPanY}px) scale(${reviewZoom})`;
  img.style.cursor = reviewZoom > 1 ? "grab" : "zoom-in";
}

function resetReviewZoom() {
  reviewZoom = 1; reviewPanX = 0; reviewPanY = 0;
  reviewApplyTransform();
}

function reviewSetZoom(z) {
  reviewZoom = Math.min(4, Math.max(1, z));
  if (reviewZoom <= 1.01) { reviewZoom = 1; reviewPanX = 0; reviewPanY = 0; }
  reviewApplyTransform();
}

function renderReviewSlide() {
  if (!reviewEntry) return;
  const {slides, idx, slide} = currentSlide(reviewEntry);
  if (!slide) { closeReview(); return; }
  const img = document.getElementById("review-img");
  img.src = slide.url;
  img.alt = slide.label || "Mockup to approve";
  const parts = [slide.letter ? `${slide.letter}${slide.recommended ? " ★" : ""}` : null, slide.label].filter(Boolean);
  document.getElementById("review-caption").textContent = parts.join(" — ");
  document.getElementById("review-counter").textContent = `${idx + 1} of ${slides.length}`;
  document.getElementById("review-newtab").href = slide.url;
  const multi = slides.length > 1;
  document.getElementById("review-prev").style.display = multi ? "" : "none";
  document.getElementById("review-next").style.display = multi ? "" : "none";
}

function openReview(num, fallbackImages) {
  const entry = resolveStuckEntry(num, fallbackImages);
  if (!entry) return;
  reviewEntry = entry; reviewCard = num;
  resetReviewZoom();
  renderReviewSlide();
  document.getElementById("review-overlay").classList.add("open");
  document.body.classList.add("review-open");
}

function closeReview() {
  document.getElementById("review-overlay").classList.remove("open");
  document.body.classList.remove("review-open");
  reviewEntry = null; reviewCard = null;
}

function reviewStep(dir) {
  if (reviewCard == null) return;
  stepCarousel(reviewCard, dir);
  resetReviewZoom();
  renderReviewSlide();
}

document.getElementById("review-prev").addEventListener("click", () => reviewStep(-1));
document.getElementById("review-next").addEventListener("click", () => reviewStep(1));
document.getElementById("review-close").addEventListener("click", closeReview);
document.getElementById("review-overlay").addEventListener("click", (e) => {
  if (e.target.id === "review-overlay") closeReview();
});
document.addEventListener("keydown", (e) => {
  if (!document.getElementById("review-overlay").classList.contains("open")) return;
  if (e.key === "Escape") closeReview();
  else if (e.key === "ArrowLeft") reviewStep(-1);
  else if (e.key === "ArrowRight") reviewStep(1);
});

// Zoom: scroll-wheel or double-click/double-tap on a desktop, pinch on the phone; drag to
// pan once zoomed in. A swipe while at 1x (not zoomed) flips to the next/previous picture
// instead — the same gesture means different things depending on whether there's anywhere
// to pan to.
const reviewImgEl = document.getElementById("review-img");
reviewImgEl.addEventListener("wheel", (e) => {
  e.preventDefault();
  reviewSetZoom(reviewZoom - e.deltaY * 0.0015);
}, {passive: false});
reviewImgEl.addEventListener("dblclick", () => reviewSetZoom(reviewZoom > 1 ? 1 : 2.5));

let reviewDrag = null;
reviewImgEl.addEventListener("mousedown", (e) => {
  if (reviewZoom <= 1) return;
  reviewDrag = {x: e.clientX, y: e.clientY, panX: reviewPanX, panY: reviewPanY};
});
document.addEventListener("mousemove", (e) => {
  if (!reviewDrag) return;
  reviewPanX = reviewDrag.panX + (e.clientX - reviewDrag.x);
  reviewPanY = reviewDrag.panY + (e.clientY - reviewDrag.y);
  reviewApplyTransform();
});
document.addEventListener("mouseup", () => { reviewDrag = null; });

// Tap vs. swipe is decided at touchend, from how far the finger actually ended up from
// where it started — never from whether a `touchmove` happened to fire in between, which
// a very quick flick can skip. Double-tap zoom only ever fires for two touches that each
// ended close to where they started, so two swipes thrown quickly never zoom by mistake.
let reviewTouch = null, reviewLastTapTime = 0, reviewLastTapX = null, reviewLastTapY = null;
reviewImgEl.addEventListener("touchstart", (e) => {
  if (e.touches.length === 2) {
    const [a, b] = e.touches;
    reviewTouch = {mode: "pinch", dist: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), zoom: reviewZoom};
    return;
  }
  const t = e.touches[0];
  reviewTouch = reviewZoom > 1
    ? {mode: "pan", x: t.clientX, y: t.clientY, panX: reviewPanX, panY: reviewPanY}
    : {mode: "tap", x: t.clientX, y: t.clientY};
}, {passive: true});
reviewImgEl.addEventListener("touchmove", (e) => {
  if (!reviewTouch) return;
  if (reviewTouch.mode === "pinch" && e.touches.length === 2) {
    const [a, b] = e.touches;
    reviewSetZoom(reviewTouch.zoom * (Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) / reviewTouch.dist));
  } else if (reviewTouch.mode === "pan" && e.touches.length === 1) {
    reviewPanX = reviewTouch.panX + (e.touches[0].clientX - reviewTouch.x);
    reviewPanY = reviewTouch.panY + (e.touches[0].clientY - reviewTouch.y);
    reviewApplyTransform();
  }
}, {passive: true});
reviewImgEl.addEventListener("touchend", (e) => {
  if (!reviewTouch) return;
  if (reviewTouch.mode === "tap") {
    const end = e.changedTouches[0];
    const dx = end.clientX - reviewTouch.x, dy = end.clientY - reviewTouch.y;
    if (Math.abs(dx) > 40) {
      reviewStep(dx < 0 ? 1 : -1);
    } else {
      const now = Date.now();
      const dist = reviewLastTapX == null ? Infinity : Math.hypot(reviewTouch.x - reviewLastTapX, reviewTouch.y - reviewLastTapY);
      if (now - reviewLastTapTime < 300 && dist < 30) {
        reviewSetZoom(reviewZoom > 1 ? 1 : 2.5);
        reviewLastTapTime = 0;
      } else {
        reviewLastTapTime = now; reviewLastTapX = reviewTouch.x; reviewLastTapY = reviewTouch.y;
      }
    }
  }
  reviewTouch = null;
}, {passive: true});

// ---- The studio: an office the lanes walk around in (card #88) -----------------------
// A second view of the same data `render()` already has. Toggled by one button; `render()`
// above is untouched, `refresh()` just hands it the same state it was already given.
//
// A floor plan (October 9, 2026: "make this look like a real office"). Top row: the break
// room and Steve's office, a hallway between them. Across the middle, the main hallway.
// Bottom row: a room per board, hallways between them, and the server room at the end.
// A lane that is working or paused is at its desk, and an empty desk is tagged with
// whose it is. An idle lane walks out of its room, down the hallway and into the break
// room, where it mills about — every so often one gets up and walks to another spot. A
// lane asleep after a failure naps on the couch there. A lane that needs Steve walks to
// his office and waves in front of his desk with its picture held up; the light over his
// door turns red. His in-tray holds every Stuck card. On his wall, two boards to click:
// the bulletin board (every card being worked and recently worked, with a Peek into the
// card itself) and the wins board (what shipped this week). Hover the server room for the
// box's load, the tokens the lanes moved, and the lab's other boxes.

function showStudio() {
  document.getElementById("floor-view").style.display = "none";
  document.getElementById("studio").classList.add("open");
  document.getElementById("studio-btn").style.display = "none";
  // Drawn hidden, the room had no width to lay itself out by; draw it again now it has one.
  if (lastState) renderStudio(lastState);
}
function showFloor() {
  closeBubble();
  closeServerTip();
  document.getElementById("studio").classList.remove("open");
  document.getElementById("floor-view").style.display = "";
  document.getElementById("studio-btn").style.display = "";
}
document.getElementById("studio-btn").addEventListener("click", showStudio);
document.getElementById("floor-btn").addEventListener("click", showFloor);

// Each lane is a recognizable person: hair, skin tone and shirt pattern come from a hash
// of its name, so the same lane always looks the same and two different lanes (almost
// always) look different from each other — never from anything that changes tick to tick.
function laneHash(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (Math.imul(h, 31) + name.charCodeAt(i)) >>> 0;
  return h;
}
const HAIR = ["#3b3a36", "#8a5a2b", "#c6a15b", "#1b1f24", "#7a3b2e", "#5b4636"];
const SKIN = ["#e8a27c", "#c68642", "#f0c8a0", "#8d5524", "#ffdbac"];
const SHIRT_PATTERN = ["solid", "stripe", "dot"];
function laneLook(name) {
  const h = laneHash(name);
  return {hair: HAIR[h % HAIR.length], skin: SKIN[(h >> 3) % SKIN.length],
          pattern: SHIRT_PATTERN[(h >> 6) % SHIRT_PATTERN.length], glasses: (h >> 9) % 2 === 0};
}

// A standing figure, one pose per pill — the same colour-by-state `spriteSVG` uses, so the
// room and the desks never disagree about what a state means.
function characterSVG(pill, look) {
  const c = `var(${PILL_COLOR[pill] || "--idle"})`;
  const armsUp = pill === "paused" || pill === "needs_steve";
  const pattern = look.pattern === "stripe"
    ? `<rect x="7" y="15" width="12" height="2.5" style="fill:rgba(0,0,0,.15)"/>`
    : look.pattern === "dot" ? `<circle cx="13" cy="18" r="1.4" style="fill:rgba(0,0,0,.2)"/>` : "";
  const armL = armsUp
    ? `<rect x="2" y="7" width="3" height="8" rx="1" style="fill:${c}" transform="rotate(-35 3.5 15)"/>`
    : `<rect x="3" y="15" width="3" height="7" rx="1" style="fill:${c}"/>`;
  // Paused: both hands up, still. Needs Steve: it is waving — one arm animated (wrapped in
  // its own <g>, so the CSS animation's `transform` adds to the SVG attribute's rotation
  // instead of replacing it).
  const armRRaised = `<rect x="21" y="7" width="3" height="8" rx="1" style="fill:${c}" transform="rotate(35 22.5 15)"/>`;
  const armR = !armsUp ? `<rect x="20" y="15" width="3" height="7" rx="1" style="fill:${c}"/>`
    : pill === "needs_steve"
      ? `<g class="character-wave" style="transform-box:fill-box;transform-origin:50% 100%">${armRRaised}</g>`
      : armRRaised;
  const sleepy = pill === "sleeping" && !look.cookie
    ? `<text x="17" y="3" style="font:700 7px sans-serif;fill:var(--dim)">z</text>` : "";
  // Idle: in the break room with a mug, waiting for a card. At cookie time the mug is down
  // and the cookie is up at the mouth, a bite out of it — a sleeper wakes for one too (no
  // z's), which is why `sleepy` above looks at the cookie.
  const mug = look.cookie
    ? `<circle cx="19" cy="11.5" r="3.6" style="fill:#c9915a"/><circle cx="18" cy="10.6" r=".8" style="fill:#5b3a1a"/><circle cx="20.4" cy="12.6" r=".8" style="fill:#5b3a1a"/><circle cx="19.8" cy="10" r=".6" style="fill:#5b3a1a"/>
       <circle cx="16.6" cy="10" r="2" style="fill:${look.skin}"/>`
    : pill === "idle"
      ? `<rect x="20.5" y="19" width="5" height="5.5" rx="1" style="fill:#f3f4f6;opacity:.9"/>` : "";
  const glasses = look.glasses
    ? `<rect x="9" y="6" width="3.5" height="2.5" rx=".5" style="fill:none;stroke:#1b1f24;stroke-width:.6"/>
       <rect x="13.5" y="6" width="3.5" height="2.5" rx=".5" style="fill:none;stroke:#1b1f24;stroke-width:.6"/>
       <path d="M12.5 7.2 h1" style="stroke:#1b1f24;stroke-width:.6"/>` : "";
  return `<svg viewBox="0 0 26 30" aria-hidden="true">
    ${armL}${armR}
    <rect x="6" y="12" width="14" height="13" rx="4" style="fill:${c}"/>${pattern}
    <circle cx="13" cy="7" r="6" style="fill:${look.skin}"/>
    <path d="M7 5 a6 6 0 0 1 12 0 v1 h-12 z" style="fill:${look.hair}"/>${glasses}
    ${sleepy}${mug}</svg>`;
}

// The furniture. Small inline pictures, each the size it is drawn at.
const PROPS = {
  coffee: `<svg width="26" height="30" viewBox="0 0 26 30"><rect x="3" y="2" width="20" height="26" rx="2" fill="var(--dim)"/>
    <rect x="6" y="5" width="14" height="6" rx="1" fill="var(--panel)" opacity=".5"/>
    <rect x="9" y="18" width="8" height="7" rx="1" fill="#f3f4f6"/>
    <path class="steam" d="M13 16 q1.5 -2 0 -4" stroke="var(--ink)" fill="none" stroke-width="1"/></svg>`,
  vending: `<svg width="22" height="34" viewBox="0 0 22 34"><rect x="1" y="1" width="20" height="32" rx="2" fill="var(--dim)"/>
    <rect x="4" y="4" width="11" height="18" rx="1" fill="var(--panel)" opacity=".45"/>
    <rect x="5" y="6" width="3" height="3" fill="#ff7b72"/><rect x="9" y="6" width="3" height="3" fill="#3fb950"/>
    <rect x="5" y="11" width="3" height="3" fill="#d29922"/><rect x="9" y="11" width="3" height="3" fill="#a371f7"/>
    <rect x="5" y="16" width="3" height="3" fill="#58a6ff"/><rect x="9" y="16" width="3" height="3" fill="#ff7b72"/>
    <rect x="4" y="25" width="14" height="4" rx="1" fill="#1b1f24" opacity=".5"/></svg>`,
  cooler: `<svg width="16" height="32" viewBox="0 0 16 32"><rect x="4" y="1" width="8" height="12" rx="3" fill="#9ecbff" opacity=".85"/>
    <rect x="2" y="13" width="12" height="18" rx="2" fill="var(--dim)"/><rect x="5" y="19" width="2" height="4" fill="#58a6ff"/></svg>`,
  plant: `<svg width="20" height="30" viewBox="0 0 20 30"><path d="M10 18 c-6 -2 -8 -8 -6 -12 c4 2 6 6 6 12 c0 -6 2 -10 6 -12 c2 4 0 10 -6 12z" fill="#3fb950"/>
    <rect x="5" y="18" width="10" height="10" rx="2" fill="#b5651d"/></svg>`,
  tallplant: `<svg width="26" height="44" viewBox="0 0 26 44">
    <path d="M13 30 c-9 -3 -12 -12 -9 -20 c6 3 9 10 9 20z" fill="#2ea043"/>
    <path d="M13 30 c9 -3 12 -12 9 -20 c-6 3 -9 10 -9 20z" fill="#3fb950"/>
    <path d="M13 30 c-2 -10 -1 -18 0 -24 c1 6 2 14 0 24z" fill="#56d364"/>
    <rect x="6" y="30" width="14" height="12" rx="2" fill="#a86f3a"/><rect x="5" y="29" width="16" height="3" rx="1" fill="#b5651d"/></svg>`,
  window: `<svg width="48" height="32" viewBox="0 0 48 32"><defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="var(--sky)"/><stop offset="1" stop-color="var(--sky-dark)"/></linearGradient></defs>
    <rect x="1" y="1" width="46" height="30" rx="2" fill="url(#sky)" stroke="#f3f4f6" stroke-width="2"/>
    <path d="M24 1 v30 M1 16 h46" stroke="#f3f4f6" stroke-width="2"/>
    <circle cx="36" cy="9" r="4" fill="#ffd33d"/><ellipse cx="12" cy="12" rx="6" ry="2.5" fill="#fff" opacity=".8"/></svg>`,
  picture: `<svg width="18" height="14" viewBox="0 0 18 14"><rect x="1" y="1" width="16" height="12" fill="#f0c8a0" stroke="#8a5a2b" stroke-width="2"/>
    <path d="M3 11 l4 -5 l3 3 l2 -2 l3 4z" fill="#3fb950"/></svg>`,
  table: `<svg width="44" height="34" viewBox="0 0 44 34"><ellipse cx="22" cy="14" rx="20" ry="9" fill="#a86f3a" stroke="#7a4e24" stroke-width="2"/>
    <rect x="12" y="10" width="20" height="9" rx="1" fill="#e8c27a" stroke="#b5651d" stroke-width="1"/>
    <circle cx="18" cy="14" r="1.6" fill="#ff7b72"/><circle cx="24" cy="13" r="1.6" fill="#ff7b72"/><circle cx="27" cy="16" r="1.4" fill="#ff7b72"/>
    <rect x="6" y="24" width="10" height="7" rx="2" fill="var(--dim)"/><rect x="28" y="24" width="10" height="7" rx="2" fill="var(--dim)"/></svg>`,
  // Steve's desk: the chair behind it, a monitor, a mug, and the nameplate.
  bossdesk: `<svg width="120" height="40" viewBox="0 0 120 40"><rect x="44" y="0" width="32" height="14" rx="5" fill="var(--dim)"/>
    <rect x="0" y="12" width="120" height="24" rx="3" fill="#8a5a2b"/><rect x="0" y="12" width="120" height="5" rx="2" fill="#a86f3a"/>
    <rect x="10" y="18" width="16" height="11" rx="1" fill="var(--line)"/><rect x="12" y="20" width="12" height="7" fill="#58a6ff" opacity=".55"/>
    <rect x="34" y="24" width="7" height="6" rx="1" fill="#f3f4f6"/>
    <rect x="80" y="21" width="32" height="10" rx="1" fill="#f3f4f6"/>
    <text x="96" y="28.5" text-anchor="middle" font-size="7" font-weight="700" fill="#1b1f24" font-family="sans-serif">STEVE</text></svg>`,
  // The server room: a rack of blinking machines.
  rack: `<svg width="34" height="84" viewBox="0 0 34 84"><rect x="1" y="1" width="32" height="82" rx="2" fill="#0d1117" stroke="#30363d" stroke-width="2"/>
    ${[0, 1, 2, 3, 4, 5].map(i => `<rect x="4" y="${5 + i * 13}" width="26" height="10" rx="1" fill="#21262d"/>
      <circle class="led l${(i % 4) + 1}" cx="8" cy="${10 + i * 13}" r="1.6" fill="#3fb950"/>
      <circle class="led l${((i + 2) % 4) + 1}" cx="13" cy="${10 + i * 13}" r="1.6" fill="#58a6ff"/>
      <rect x="18" y="${8 + i * 13}" width="10" height="4" rx="1" fill="#30363d"/>`).join("")}</svg>`,
  aircon: `<svg width="30" height="40" viewBox="0 0 30 40"><rect x="1" y="1" width="28" height="38" rx="3" fill="#c9d1d9" stroke="#8b949e" stroke-width="2"/>
    <path d="M6 10 h18 M6 15 h18 M6 20 h18" stroke="#8b949e" stroke-width="2"/><circle class="led l2" cx="22" cy="31" r="2" fill="#58a6ff"/></svg>`,
};
function prop(name, x, y, extra) {
  return `<div class="prop${extra ? " " + extra : ""}" style="left:${x}px;top:${y}px">${PROPS[name]}</div>`;
}
// A wall clock set to the floor's own time.
function clockSVG(iso) {
  const d = iso ? new Date(iso) : new Date();
  const h = (d.getHours() % 12) * 30 + d.getMinutes() / 2, m = d.getMinutes() * 6;
  return `<svg width="26" height="26" viewBox="0 0 26 26"><circle cx="13" cy="13" r="12" fill="#f3f4f6" stroke="var(--dim)" stroke-width="2"/>
    <path d="M13 13 v-6" stroke="#1b1f24" stroke-width="2" transform="rotate(${h} 13 13)"/>
    <path d="M13 13 v-9" stroke="#1b1f24" stroke-width="1.2" transform="rotate(${m} 13 13)"/><circle cx="13" cy="13" r="1.2" fill="#1b1f24"/></svg>`;
}
// A desk: top, legs, monitor (scanning while lit), keyboard, mug, and the chair in front.
function deskSVG(lit) {
  return `<svg width="60" height="54" viewBox="0 0 60 54">
    <rect x="21" y="2" width="18" height="13" rx="1.5" fill="#30363d"/>
    <rect x="23" y="4" width="14" height="9" fill="${lit ? "var(--working)" : "#161b22"}" opacity="${lit ? ".55" : "1"}"/>
    <rect class="scanline" x="24" y="5" width="12" height="1.2" fill="#fff"/>
    <rect x="28" y="15" width="4" height="3" fill="#30363d"/>
    <rect x="2" y="18" width="56" height="12" rx="2" fill="var(--wood-a)"/><rect x="2" y="18" width="56" height="3" rx="1" fill="#a86f3a"/>
    <rect x="18" y="22" width="18" height="4" rx="1" fill="#c9d1d9"/><rect x="44" y="21" width="6" height="6" rx="1" fill="#f3f4f6"/>
    <rect x="5" y="30" width="3" height="10" fill="#5b3a1a"/><rect x="52" y="30" width="3" height="10" fill="#5b3a1a"/>
    <rect x="20" y="40" width="20" height="12" rx="4" fill="var(--dim)"/><rect x="24" y="36" width="12" height="6" rx="2" fill="color-mix(in srgb, var(--dim) 70%, #000)"/></svg>`;
}
// The triage counter by the door: a counter, an inbox tray, a bell.
// The ship station's own figure: a dock hand in a hard hat with a hand truck of crates —
// not the floor page's desk sprite shrunk to fit, which read as a person carrying a desk
// around (October 9, 2026). The crates say the state the way the floor's pill does: a
// check when main is green, blinking dots while checks run, an hourglass while the deployer
// waits for idle lanes, a waved flag when blocked, and the truck rolling while a release
// is being swapped in.
function shipCharacterSVG(mode, pill, look) {
  const c = `var(--${{working: "working", paused: "paused", needs_steve: "needs", idle: "idle"}[SHIP_PILL[pill][0]]})`;
  let sign = "";
  if (mode === "done") sign = `<path d="M30 4 l2.5 2.5 l5 -5" style="fill:none;stroke:${c};stroke-width:1.8"/>`;
  if (mode === "checking") sign = [0, 1, 2].map(i => `<circle class="dot d${i + 1}" cx="${30 + i * 4}" cy="4" r="1.4" style="fill:${c}"/>`).join("");
  if (mode === "waiting") sign = `<path d="M30 0.5 h8 l-4 4.5 l4 4.5 h-8 l4 -4.5 z" style="fill:${c}"/>`;
  if (mode === "blocked") sign = `<g class="character-wave" style="transform-box:fill-box;transform-origin:50% 100%">
    <rect x="29" y="0.5" width="1.5" height="10" style="fill:var(--dim)"/><rect x="30.5" y="0.5" width="7" height="5" style="fill:${c}"/></g>`;
  return `<svg viewBox="0 0 40 30" aria-hidden="true">
    <rect x="3" y="15" width="3" height="7" rx="1" style="fill:${c}"/>
    <rect x="17" y="14" width="8" height="2.6" rx="1" style="fill:${c}"/>
    <rect x="6" y="12" width="14" height="13" rx="4" style="fill:${c}"/>
    <circle cx="13" cy="7" r="6" style="fill:${look.skin}"/>
    <path d="M6.5 6 a6.5 5.5 0 0 1 13 0 z" style="fill:#ffd33d"/><rect x="5.5" y="5.5" width="15" height="1.6" rx=".8" style="fill:#d29922"/>
    <g${mode === "pushing" ? ' class="ship-push"' : ""}>
      <rect x="24" y="8" width="2" height="19" rx="1" style="fill:var(--dim)"/>
      <rect x="26" y="19" width="11" height="7" rx="1" style="fill:#a86f3a"/><rect x="26" y="12" width="11" height="7" rx="1" style="fill:#c9915a"/>
      <path d="M28 15.5 h7 M28 22.5 h7" style="stroke:#7a4e24;stroke-width:1"/>
      <circle cx="26" cy="27.5" r="2.2" style="fill:#30363d"/><rect x="24" y="26" width="13" height="1.5" style="fill:var(--dim)"/>
    </g>${sign}</svg>`;
}
// The ship shelf the dock hand stands in front of: two shelves of crates, a clipboard.
function shelfSVG() {
  return `<svg width="52" height="54" viewBox="0 0 52 54">
    <rect x="2" y="4" width="48" height="46" rx="2" fill="#5b3a1a"/><rect x="4" y="6" width="44" height="42" fill="#3b2410"/>
    <rect x="4" y="25" width="44" height="3" fill="#8a5a2b"/><rect x="4" y="46" width="44" height="3" fill="#8a5a2b"/>
    <rect x="7" y="12" width="12" height="13" rx="1" fill="#c9915a"/><rect x="21" y="15" width="10" height="10" rx="1" fill="#a86f3a"/>
    <rect x="34" y="10" width="11" height="15" rx="1" fill="#c9915a"/>
    <rect x="7" y="34" width="14" height="12" rx="1" fill="#a86f3a"/><rect x="23" y="37" width="9" height="9" rx="1" fill="#c9915a"/>
    <rect x="36" y="33" width="9" height="12" rx="1" fill="#f3f4f6"/><path d="M38 37 h5 M38 40 h5 M38 43 h3" stroke="#8b949e" stroke-width="1"/>
    <path d="M10 18 h6 M24 20 h4 M37 17 h5 M11 40 h6" stroke="#7a4e24" stroke-width="1"/></svg>`;
}

function counterSVG() {
  return `<svg width="60" height="54" viewBox="0 0 60 54">
    <rect x="4" y="26" width="52" height="16" rx="2" fill="#a86f3a"/><rect x="4" y="26" width="52" height="4" rx="1" fill="#c9915a"/>
    <rect x="10" y="18" width="18" height="8" rx="1" fill="#c9d1d9"/><rect x="12" y="14" width="14" height="4" fill="#f3f4f6"/>
    <path d="M44 24 a5 5 0 0 1 10 0 z" fill="#ffd33d"/><rect x="42" y="24" width="14" height="2" fill="#d29922"/>
    <rect x="20" y="42" width="20" height="10" rx="4" fill="var(--dim)"/></svg>`;
}
// The two boards on Steve's wall, each clickable: the bulletin board with its pinned
// notes, and the wins board with this week's count on it.
function bulletinSVG(n) {
  const pins = [[8, 8], [26, 6], [44, 10], [12, 24], [32, 22], [48, 26]].slice(0, Math.max(1, Math.min(6, n || 1)));
  return `<svg width="64" height="46" viewBox="0 0 64 46"><rect x="1" y="1" width="62" height="44" rx="2" fill="#c9a06c" stroke="#7a4e24" stroke-width="2"/>
    ${pins.map(([x, y], i) => `<rect x="${x}" y="${y}" width="12" height="9" fill="${["#fff8c4", "#d6f5d6", "#ffe0e0", "#e0ecff"][i % 4]}" transform="rotate(${(i % 2 ? 4 : -3)} ${x + 6} ${y + 4})"/>
      <circle cx="${x + 6}" cy="${y + 1.5}" r="1.5" fill="${["#ff7b72", "#58a6ff", "#3fb950"][i % 3]}"/>`).join("")}
    <text x="32" y="42" text-anchor="middle" font-size="6" font-weight="700" fill="#4e2f14" font-family="sans-serif">BULLETIN</text></svg>`;
}
function winsSVG(n) {
  return `<svg width="62" height="46" viewBox="0 0 62 46"><rect x="1" y="1" width="60" height="44" rx="2" fill="#1f2a3a" stroke="#d4b06a" stroke-width="2"/>
    <path d="M24 10 h14 v8 a7 7 0 0 1 -14 0 z" fill="#ffd33d"/><path d="M22 11 h2 v6 a4 4 0 0 1 -2 -6 M40 11 h-2 v6 a4 4 0 0 0 2 -6" fill="#ffd33d"/>
    <rect x="29" y="25" width="4" height="4" fill="#d29922"/><rect x="25" y="29" width="12" height="3" rx="1" fill="#d29922"/>
    <text x="31" y="41" text-anchor="middle" font-size="6.5" font-weight="700" fill="#ffd33d" font-family="sans-serif">WINS · ${n}</text></svg>`;
}

// Where things stand. The office is laid out in pixels from its own width, so a figure
// never shares a spot with another and a room is as tall as the people in it: a break room
// with six idle lanes grows a second row rather than standing them on each other.
const CHAR_W = 72;                 // one standing spot: a figure and its plate never exceed it (.character max-width)
const EDGE = 12, ROOM_PAD = 6;
const CORR_H = 60;                 // the main hallway, and the one between team rows
const VHALL = 48;                  // between the break room and Steve's office
const THALL = 28;                  // between team rooms, and before the server room
const SERVER_W = 220;
const WALK_SPEED = 180;            // px per second
// padTop is the first row's feet, measured from the room's top: room name, props on the
// back wall and the figure's own height (34px; plus a held-up mockup in Steve's office;
// plus a desk and monitor in a team room) all fit above it. rowH is feet to feet.
// padBottom is the plate under the last row's feet.
const BREAK = {padTop: 104, rowH: 68, padBottom: 30};
const BOSS = {padTop: 200, rowH: 100, padBottom: 30};
const BOSS_EMPTY_H = 130;          // nobody waiting: the wall, the desk
const TRAY_CHIP_H = 34;   // the in-tray: one row of chips, each with its own Review button
const TEAM = {padTop: 108, rowH: 76, padBottom: 24};
const BREAK_EXTRA = 2;             // free spots in the break room, so a wanderer has somewhere to go

function roomWidthPx() {
  // Hidden (every 30s poll draws the studio while the floor is showing), the room has no
  // width; use the one it will have, and `showStudio` draws again with the real one.
  const el = document.getElementById("room");
  return el.clientWidth || Math.max(960, window.innerWidth - 34);
}
function perRowIn(w) { return Math.max(1, Math.floor((w - 2 * ROOM_PAD) / CHAR_W)); }
function rowsFor(n, w) { return Math.max(1, Math.ceil(n / perRowIn(w))); }
function roomHeight(n, w, spec) { return spec.padTop + (rowsFor(n, w) - 1) * spec.rowH + spec.padBottom; }
// `n` spots in a grid inside the room, each row centered, in room pixels.
function gridSpots(r, n, spec) {
  const perRow = perRowIn(r.w), spots = [];
  for (let i = 0; i < n; i++) {
    const row = Math.floor(i / perRow), col = i % perRow;
    const inRow = Math.min(perRow, n - row * perRow);
    spots.push({x: r.x + r.w / 2 + (col - (inRow - 1) / 2) * CHAR_W, y: r.y + spec.padTop + row * spec.rowH});
  }
  return {spots, perRow, rows: rowsFor(n, r.w)};
}

// The break room's seating is remembered across redraws, so a wander is one lane's walk
// to a free spot and never a reshuffle of everyone. Spot indices, per lane.
let breakSpot = {};
let lastBreak = {n: 0, held: new Set()};
let wanderStep = 0;
function seatBreakRoom(breakers, sleepers, spots, perRow, rows) {
  const n = spots.length, held = new Set();
  // The couch is under the first row's first seats; the sleepers get those, and an idle
  // lane may sit there too while they are free.
  const couch = [];
  for (let i = 0; i < n && couch.length < 3; i++) couch.push(i);
  const seatOf = {};
  for (const l of sleepers) {
    let k = couch.find(i => !held.has(i));
    if (k === undefined) { k = 0; while (held.has(k)) k++; }
    held.add(k); seatOf[l.lane] = k;
  }
  // An idle lane keeps its seat while it is still there and still free; otherwise the
  // first free one.
  const names = breakers.map(l => l.lane);
  for (const nm of Object.keys(breakSpot))
    if (!names.includes(nm) || breakSpot[nm] >= n || held.has(breakSpot[nm])) delete breakSpot[nm];
  for (const nm of Object.keys(breakSpot)) held.add(breakSpot[nm]);
  for (const nm of names) {
    if (nm in breakSpot) continue;
    let k = 0; while (held.has(k)) k++;
    breakSpot[nm] = k; held.add(k);
  }
  for (const nm of names) seatOf[nm] = breakSpot[nm];
  lastBreak = {n, held: new Set(Object.values(seatOf))};
  return {seatOf, couch};
}
// One idle lane gets up and walks to a free spot. On a timer while the studio is showing;
// `window.__studioWander` for the suite. Which lane and which spot both follow the step
// count, so the same floor always wanders the same way.
// Cookie time. `partyUntil` is when it ends; while it runs, computeStudio stands every
// break-room lane around the vending machine instead of at its seat, and renderCharacters
// hands each a cookie. `window.__cookiePartyMs` lets the suite keep it short.
let partyUntil = 0, partyTimer = null;
const COOKIE_PARTY_MS = 14000;
const VENDING = {x: 124, y: 20, w: 22, h: 34};   // where the machine stands, in break-room pixels
const partyOn = () => Date.now() < partyUntil;
function cookieSVG() {
  return `<svg viewBox="0 0 12 12"><circle cx="6" cy="6" r="5.5" fill="#c9915a"/><circle cx="4" cy="4.5" r="1" fill="#5b3a1a"/>
    <circle cx="7.5" cy="4" r="1" fill="#5b3a1a"/><circle cx="6.5" cy="8" r="1" fill="#5b3a1a"/><circle cx="3.5" cy="8" r=".8" fill="#5b3a1a"/></svg>`;
}
function cookieTime() {
  const br = roomsByName["Break room"];
  if (!br || !lastState) return;
  partyUntil = Date.now() + (window.__cookiePartyMs || COOKIE_PARTY_MS);
  const layer = document.getElementById("room-cookies");
  layer.innerHTML = "";
  const slot = {x: br.x + VENDING.x + VENDING.w / 2, y: br.y + VENDING.y + VENDING.h - 4};
  const shout = document.createElement("div");
  shout.className = "cookie-shout";
  shout.textContent = "Cookies!";
  shout.style.left = slot.x + "px"; shout.style.top = (br.y + VENDING.y - 2) + "px";
  layer.appendChild(shout);
  // A dozen cookies out of the slot, each on its own little arc to a spot on the floor.
  for (let i = 0; i < 12; i++) {
    const c = document.createElement("div");
    c.className = "cookie";
    c.innerHTML = cookieSVG();
    const dx = ((i * 37) % 140) - 70 + (i % 2 ? 8 : -8), dy = 28 + (i * 23) % 40;
    const land = {x: slot.x + dx, y: slot.y + dy};
    c.style.left = land.x + "px"; c.style.top = land.y + "px";
    layer.appendChild(c);
    if (!reducedMotion()) {
      c.animate([{left: slot.x + "px", top: slot.y + "px", transform: "translate(-50%,-50%) rotate(0)"},
                 {left: (slot.x + dx / 2) + "px", top: (slot.y - 18) + "px", transform: "translate(-50%,-50%) rotate(180deg)", offset: .45},
                 {left: land.x + "px", top: land.y + "px", transform: "translate(-50%,-50%) rotate(360deg)"}],
                {duration: 650, delay: i * 70, easing: "ease-out", fill: "backwards"});
    }
  }
  renderStudio(lastState);
  clearTimeout(partyTimer);
  partyTimer = setTimeout(() => {
    partyUntil = 0;
    for (const el of layer.querySelectorAll(".cookie, .cookie-shout")) {
      if (reducedMotion()) el.remove();
      else el.animate([{opacity: 1}, {opacity: 0}], {duration: 600, fill: "forwards"}).onfinish = () => el.remove();
    }
    if (lastState) renderStudio(lastState);
  }, window.__cookiePartyMs || COOKIE_PARTY_MS);
}
window.__cookieTime = cookieTime;
// Where everyone stands at cookie time: a huddle in front of the machine, at most four
// across and as many rows as it takes, a seat's width apart (the same spacing as
// everywhere, so nobody stands on anybody — review of roloc/watcher#119 caught a tighter
// crowd with its plates stacked, and the usability pass caught a single row that looked
// like nobody had moved). Each row is shifted into the room whole rather than clamped
// spot by spot. Rows are counted so the room can grow to hold them for the party.
function cookieSpots(br, n) {
  const cx = br.x + VENDING.x + VENDING.w / 2 + 40;
  const minX = br.x + ROOM_PAD + CHAR_W / 2, maxX = br.x + br.w - ROOM_PAD - CHAR_W / 2;
  const perRow = Math.max(1, Math.min(4, Math.floor((maxX - minX) / CHAR_W) + 1));
  const spots = [];
  for (let i = 0; i < n; i++) {
    const row = Math.floor(i / perRow), col = i % perRow, inRow = Math.min(perRow, n - row * perRow);
    const width = (inRow - 1) * CHAR_W;
    const start = Math.max(minX, Math.min(maxX - width, cx - width / 2));
    spots.push({x: start + col * CHAR_W, row});
  }
  return {spots, rows: Math.max(1, Math.ceil(n / perRow))};
}

function wander() {
  if (partyOn()) return;   // nobody leaves a cookie party
  const names = Object.keys(breakSpot).sort();
  if (!names.length || !lastState) return;
  const mover = names[wanderStep % names.length];
  const free = [];
  for (let i = 0; i < lastBreak.n; i++) if (!lastBreak.held.has(i)) free.push(i);
  wanderStep++;
  if (!free.length) return;
  breakSpot[mover] = free[(wanderStep * 7) % free.length];
  renderStudio(lastState);
}
window.__studioWander = wander;

// The rooms by name, with their doors, for routing a walk (filled by computeStudio).
let roomsByName = {};
let connectX = 0;

function computeStudio(state) {
  const W = roomWidthPx(), inner = W - 2 * EDGE;
  const projects = state.projects || [];
  const lanes = [];
  projects.forEach(p => (p.lanes || []).forEach(l => lanes.push({...l, project: p.name})));
  const breakers = lanes.filter(l => l.pill === "idle").sort((a, b) => a.lane.localeCompare(b.lane));
  const sleepers = lanes.filter(l => l.pill === "sleeping");
  const needs = lanes.filter(l => l.pill === "needs_steve");
  // A ship station with nothing to watch — CI green, no deploy running — is idle the same
  // way a lane with nothing to pick up is, so it goes on break too (card #126). Counted
  // here, off `projects` directly, since the room sizing below needs the count before the
  // `ships` array itself exists (built later, alongside each team room); the count has to
  // stay the same whether or not a party is on, or the room's seats (`seatBreakRoom`'s own
  // `breakSpot`, keyed by seat index) would shuffle themselves every time one starts or ends.
  // Same count the `idleShips` filter below gets from the `ships` array: `teams` (and so
  // `ships`) is built from this same `projects` list, one team room per project, in order.
  const idleShipCount = projects.filter(p => shipState(p.ship).pill === "ok").length;
  const rooms = [];

  // The top row: the break room and Steve's office, a hallway between, as tall as the fuller.
  const breakW = Math.round((inner - VHALL) * 0.52), bossW = inner - VHALL - breakW;
  const nBreak = breakers.length + sleepers.length + idleShipCount + BREAK_EXTRA;
  const crowd = partyOn() ? cookieSpots({x: EDGE, w: breakW}, breakers.length + sleepers.length + idleShipCount) : null;
  const breakRows = Math.max(rowsFor(nBreak, breakW), crowd ? crowd.rows : 1);
  const breakH = BREAK.padTop + (breakRows - 1) * BREAK.rowH + BREAK.padBottom;
  // The in-tray sits in front of his desk, between it and anyone waiting: as tall as a
  // row of chips, each with its own Review button when a card has a picture (#100).
  const stuck = projects.flatMap(p => p.stuck || []);
  const chipRows = Math.max(1, Math.ceil((stuck.length * 46 + projects.filter(p => (p.stuck || []).length).length * 40 + 48)
                                          / Math.max(80, bossW - 2 * ROOM_PAD - 16)));
  const trayH = !stuck.length ? 0 : TRAY_CHIP_H + (chipRows - 1) * 18;
  const bossSpec = {...BOSS, padTop: BOSS.padTop + trayH};
  const bossH = needs.length ? roomHeight(needs.length, bossW, bossSpec) : BOSS_EMPTY_H + trayH + (trayH ? 14 : 0);
  const topH = Math.max(breakH, bossH, 170);
  const corrY = EDGE + topH + CORR_H / 2;    // the main hallway's center line
  const breakRoom = {kind: "break", name: "Break room", x: EDGE, y: EDGE, w: breakW, h: topH, side: "bottom", corrY};
  const boss = {kind: "boss", name: "Steve's office", x: EDGE + breakW + VHALL, y: EDGE, w: bossW, h: topH,
                side: "bottom", corrY, spec: bossSpec,
                tray: trayH ? {x: ROOM_PAD + 2, y: BOSS_EMPTY_H - 12, w: bossW - 2 * ROOM_PAD - 4, h: trayH} : null};
  rooms.push(breakRoom, boss);

  // The bottom row: a room per board, hallways between, the server room at the end.
  // More boards than fit across wrap to a second row, with a hallway between the rows
  // and the one before the server room joining them to the main hallway.
  const teamsW = inner - SERVER_W - THALL;
  const cols = Math.max(1, Math.min(projects.length || 1, Math.floor((teamsW + THALL) / (270 + THALL))));
  const teamW = (teamsW - (cols - 1) * THALL) / cols;
  let y = EDGE + topH + CORR_H, rowH = 0;
  const teams = projects.map((p, i) => {
    const col = i % cols;
    if (col === 0 && i > 0) { y += rowH + CORR_H; rowH = 0; }
    const n = (p.lanes || []).length + 1;      // the ship shelf is the last station
    const h = roomHeight(n, teamW, TEAM);
    rowH = Math.max(rowH, h);
    const r = {kind: "team", name: p.name, board_url: p.board_url, counts: p.counts || {},
               x: EDGE + col * (teamW + THALL), y, w: teamW, h, project: p, side: "top", corrY: y - CORR_H / 2};
    rooms.push(r);
    return r;
  });
  const bottomEnd = teams.length ? y + rowH : EDGE + topH + CORR_H + 150;
  const server = {kind: "server", name: "Server room", x: EDGE + inner - SERVER_W, y: EDGE + topH + CORR_H,
                  w: SERVER_W, h: bottomEnd - (EDGE + topH + CORR_H), side: "top", corrY};
  rooms.push(server);
  connectX = server.x - THALL / 2;
  const total = bottomEnd + EDGE;

  roomsByName = {};
  for (const r of rooms) {
    const doorX = r.x + r.w / 2;
    r.door = {x: doorX, inside: {x: doorX, y: r.side === "bottom" ? r.y + r.h - 16 : r.y + 44},
              outside: {x: doorX, y: r.corrY}};
    roomsByName[r.name] = r;
  }

  // Spots. A lane's home is its desk; where it stands now depends on its pill.
  const ships = [];
  for (const r of teams) {
    const ordered = [...(r.project.lanes || [])].sort(
      (a, b) => (a.kind === "Triage" ? 0 : 1) - (b.kind === "Triage" ? 0 : 1));
    const {spots} = gridSpots(r, ordered.length + 1, TEAM);
    ordered.forEach((l, i) => {
      const me = lanes.find(x => x.lane === l.lane);
      me.home = {x: spots[i].x, y: spots[i].y};
      me.pos = me.home; me.away = false; me.room = r.name;
    });
    const s = spots[ordered.length];
    const home = {x: s.x, y: s.y};
    const idle = shipState(r.project.ship).pill === "ok";
    // "ship:" + name is this ship's own seat key in the break room's bookkeeping, apart
    // from any lane's — never a DOM dataset, only how `seatBreakRoom` and `wander` tell
    // occupants apart.
    ships.push({name: r.name, ship: r.project.ship, home, pos: home, room: r.name, away: false, idle, lane: "ship:" + r.name});
  }
  const bg = gridSpots(breakRoom, nBreak, BREAK);
  const idleShips = ships.filter(s => s.idle);
  const {seatOf, couch} = seatBreakRoom([...breakers, ...idleShips], sleepers, bg.spots, bg.perRow, bg.rows);
  const guests = [...breakers, ...sleepers];
  guests.forEach((l, i) => {
    const s = crowd ? {x: crowd.spots[i].x, y: breakRoom.y + BREAK.padTop + crowd.spots[i].row * BREAK.rowH}
                    : bg.spots[seatOf[l.lane]];
    l.pos = {x: s.x, y: s.y}; l.away = true; l.room = breakRoom.name;
    l.cookie = !!crowd;
  });
  // A ship stands with the crowd at cookie time too — its own seat is no longer kept
  // clear of the huddle once everyone leaves it for the vending machine (review of #126:
  // the dock hand and a lane crowding the machine stood in the same spot) — but it never
  // gets a cookie: there is no art for one in its hand, so `cookie` is left unset.
  idleShips.forEach((s, i) => {
    const j = guests.length + i;
    const spot = crowd ? {x: crowd.spots[j].x, y: breakRoom.y + BREAK.padTop + crowd.spots[j].row * BREAK.rowH}
                       : bg.spots[seatOf[s.lane]];
    s.pos = {x: spot.x, y: spot.y}; s.away = true; s.room = breakRoom.name;
  });
  if (couch.length) {
    const first = bg.spots[couch[0]], last = bg.spots[couch[couch.length - 1]];
    breakRoom.couch = {x: first.x - CHAR_W / 2 + 6 - breakRoom.x, w: last.x - first.x + CHAR_W - 12,
                       y: first.y - 24 - breakRoom.y};
  }
  const vg = gridSpots(boss, needs.length, boss.spec);
  needs.forEach((l, i) => {
    const s = vg.spots[i];
    l.pos = {x: s.x, y: s.y}; l.away = true; l.room = boss.name;
  });
  return {lanes, ships, rooms, boss, server, total};
}

function roomHTML(r, state) {
  const name = r.kind === "team"
    ? `<a class="room-name" href="${esc(r.board_url)}" target="_blank" rel="noopener">${esc(r.name)}</a>`
    : `<span class="room-name">${esc(r.name)}</span>`;
  // The door is a gap in the wall on the hallway side.
  const door = r.side === "top"
    ? `<div class="room-door" style="left:${Math.round(r.w / 2 - 14)}px;top:-3px"></div>`
    : `<div class="room-door" style="left:${Math.round(r.w / 2 - 14)}px;bottom:-3px"></div>`;
  let props = "";
  if (r.kind === "break") {
    props = prop("coffee", 92, 24)
      + `<div class="prop clickable" data-cookies title="Chloe's cookies: click" style="left:${VENDING.x}px;top:${VENDING.y}px">${PROPS.vending}</div>`
      + prop("cooler", 154, 22)
      + `<div class="prop" style="left:184px;top:16px">${clockSVG(state.header && state.header.time)}</div>`
      + (r.w > 520 ? prop("table", Math.round(r.w / 2 + 40), 20) : "")
      + prop("window", r.w - 150, 16) + prop("window", r.w - 96, 16) + prop("tallplant", r.w - 40, 10)
      + prop("plant", 8, r.h - 40)
      + (r.couch ? `<div class="couch" style="left:${r.couch.x}px;top:${r.couch.y}px;width:${r.couch.w}px"></div>` : "");
  } else if (r.kind === "boss") {
    const nRecent = ((state.recent || []).length) + (state.projects || []).reduce((a, p) => a + (p.lanes || []).filter(l => l.card).length, 0);
    const nWins = ((state.wins || {}).items || []).length;
    props = `<div class="rug"></div>` + prop("window", 14, 18) + prop("window", 68, 18)
      + `<div class="prop clickable" data-board="bulletin" title="The bulletin board: every card being worked, and recently worked" style="left:${r.w - 150}px;top:16px">${bulletinSVG(nRecent)}</div>`
      + `<div class="prop clickable" data-board="wins" title="The wins board: what shipped this week" style="left:${r.w - 80}px;top:16px">${winsSVG(nWins)}</div>`
      + prop("bossdesk", Math.round(r.w / 2 - 60), 68) + prop("tallplant", 10, r.h - 56) + prop("plant", r.w - 30, r.h - 44);
  } else if (r.kind === "server") {
    const racks = Math.max(1, Math.min(4, Math.floor((r.w - 60) / 44)));
    props = Array.from({length: racks}, (_, i) => prop("rack", 18 + i * 44, 34)).join("")
      + prop("aircon", r.w - 42, r.h - 52)
      + `<div class="hall-label" style="left:10px;top:${r.h - 20}px">hover: load · tokens · lab</div>`;
  } else {
    const c = r.counts;
    props = `<div class="whiteboard" title="The board right now">Ready ${c.ready ?? "?"} · Doing ${c.in_progress ?? "?"} · Backlog ${c.backlog ?? "?"}</div>`
      + prop("window", Math.round(r.w / 2 - 24), 18) + prop("plant", 8, r.h - 40) + prop("tallplant", r.w - 36, 14);
  }
  return `<div class="office-room ${r.kind}" data-room="${esc(r.name)}"
    style="left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px">${name}${props}${door}</div>`;
}

function renderRooms(office, state) {
  const room = document.getElementById("room");
  room.style.height = office.total + "px";
  const {boss, server} = office;
  document.getElementById("room-rooms").innerHTML = office.rooms.map(r => roomHTML(r, state)).join("")
    + `<div class="hall-label" style="left:${EDGE + 6}px;top:${boss.y + boss.h + 4}px">Main hallway</div>`;
  const beacon = document.getElementById("beacon");
  beacon.style.left = (boss.x + boss.w / 2) + "px";
  beacon.style.top = (boss.y + boss.h - 14) + "px";
  // The readout hangs beside the server room, from the hallway down; it may run past
  // the floor's bottom edge (the floor does not clip), never be cut off by it.
  const tip = document.getElementById("server-tip");
  tip.style.left = (server.x - 352) + "px";
  tip.style.top = (server.y - CORR_H + 6) + "px";
}

function renderRoomDesks(lanes, ships) {
  const shelves = (ships || []).map(s =>
    `<div class="desk-set shelf" data-ship="${esc(s.name)}" style="left:${s.home.x}px;top:${s.home.y + 10}px">${shelfSVG()}</div>`
    + (s.away ? `<div class="desk-tag" data-ship="${esc(s.name)}" style="left:${s.home.x}px;top:${s.home.y}px">Ship · out</div>` : ""));
  const furniture = lanes.map(l => (l.kind === "Triage"
    ? `<div class="desk-set" style="left:${l.home.x}px;top:${l.home.y + 10}px">${counterSVG()}</div>`
    : `<div class="desk-set${l.pill === "working" ? " lit" : ""}" data-lane="${esc(l.lane)}"
            style="left:${l.home.x}px;top:${l.home.y + 10}px">${deskSVG(l.pill === "working")}</div>`)
    + (l.away ? `<div class="desk-tag" data-lane="${esc(l.lane)}" style="left:${l.home.x}px;top:${l.home.y}px">${esc(l.kind)} · out</div>` : ""));
  document.getElementById("room-desks").innerHTML = shelves.join("") + furniture.join("");
}

// The plate under a character: what it is (Build, Design, Triage), and — only when it has
// left its room for the break room or Steve's office — whose it is.
function plateHTML(kind, project, away) {
  return `<div class="plate">${esc(kind)}${away ? `<br><span class="where">${esc(project)}</span>` : ""}</div>`;
}

// Walking. A character keeps the same DOM node across renders, and a change of spot is a
// walk: within a room a straight line; between rooms out of the door, along the hallway
// (and up the one by the server room when the rooms are on different hallways), and in at
// the other door. A Web Animation along those waypoints, at a walking pace, with a bob
// while it runs; `prefers-reduced-motion` makes it a jump cut.
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const inRoom = (p, r) => r && p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h;
function routePath(from, fromRoom, to, toRoom) {
  const A = roomsByName[fromRoom], B = roomsByName[toRoom];
  if (!A || !B) return [from, to];
  if (fromRoom === toRoom && inRoom(from, A)) return [from, to];
  const pts = [from];
  let corr;
  if (inRoom(from, A)) { pts.push(A.door.inside, A.door.outside); corr = A.corrY; }
  else {
    // Already out in a hallway (a walk interrupted by a redraw): the nearest hallway line.
    const lines = [...new Set(Object.values(roomsByName).map(r => r.corrY))];
    corr = lines.reduce((best, y) => Math.abs(y - from.y) < Math.abs(best - from.y) ? y : best, lines[0]);
    pts.push({x: from.x, y: corr});
  }
  if (corr !== B.corrY) pts.push({x: connectX, y: corr}, {x: connectX, y: B.corrY});
  pts.push(B.door.outside, B.door.inside, to);
  return pts;
}
function place(el, p) { el.style.left = p.x + "px"; el.style.top = p.y + "px"; }
function moveCharacter(el, to, room) {
  const prev = el._pos;
  if (!prev || reducedMotion()) {
    if (el._anim) el._anim.cancel();
    place(el, to); el._pos = to; el._room = room;
    return;
  }
  if (prev.x === to.x && prev.y === to.y && !el._anim) { el._room = room; return; }
  // Mid-walk already: start from where it actually is, not where it was going — and
  // from the hallway if that is where it is, never a straight line through a wall.
  const cs = getComputedStyle(el);
  const from = el._anim ? {x: parseFloat(cs.left), y: parseFloat(cs.top)} : prev;
  if (el._anim) el._anim.cancel();
  const path = routePath(from, el._room, to, room);
  const segs = [];
  let total = 0;
  for (let i = 1; i < path.length; i++) {
    const d = Math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y);
    segs.push(d); total += d;
  }
  if (total < 1) { place(el, to); el._pos = to; el._room = room; return; }
  const frames = [{left: from.x + "px", top: from.y + "px", offset: 0}];
  let acc = 0;
  path.slice(1).forEach((p, i) => { acc += segs[i]; frames.push({left: p.x + "px", top: p.y + "px", offset: Math.min(1, acc / total)}); });
  el.classList.add("walking");
  const anim = el.animate(frames, {duration: Math.max(300, total / WALK_SPEED * 1000), easing: "linear", fill: "forwards"});
  // `_room` stays the room it set out from until it arrives, so a redraw mid-walk routes
  // the rest of the way along the hallways rather than straight at the new spot.
  el._anim = anim; el._pos = to;
  anim.onfinish = () => { place(el, to); el._room = room; if (el._anim === anim) el._anim = null; anim.cancel(); el.classList.remove("walking"); };
  anim.oncancel = () => { if (el._anim === anim) el._anim = null; el.classList.remove("walking"); };
}

const charEls = {};
function newCharacter(container, cls) {
  const el = document.createElement("div");
  el.className = cls;
  container.appendChild(el);
  return el;
}
// The mockup held up over a waving character's head — the carousel's own current slide
// when this card has one (card #96: so flipping it in Steve's in-tray flips it here too),
// the old single thumbnail otherwise (a demo fixture with no `rounds` of its own). Either
// way it is tappable: the same overlay as the Review button, never a click through to the
// card (card #100's own Detail note: "the podium figure keeps holding the first picture
// as a thumbnail; tapping it opens the same overlay").
function heldMockupHTML(l, stuckByCard) {
  if (!l.card) return "";
  const entry = stuckByCard && stuckByCard[l.card.number];
  let img;
  if (entry) {
    const {slide} = currentSlide(entry);
    if (!slide) return "";
    img = `<img class="mockup-thumb" src="${esc(slide.url)}"
      alt="${esc(slide.label || "Mockup to approve")}" loading="lazy" onerror="this.style.display='none'">`;
  } else {
    const first = (l.images || [])[0];
    if (!first) return "";
    img = `<img class="mockup-thumb" src="${esc(first)}" alt="Mockup to approve" loading="lazy" onerror="this.style.display='none'">`;
  }
  const fallback = entry ? "" : ` data-review-images='${esc(JSON.stringify((l.images || []).slice(0, 1)))}'`;
  return `<div class="character-mockup" data-review="${l.card.number}"${fallback}>${img}</div>`;
}
function renderCharacters(lanes, stuckByCard) {
  const container = document.getElementById("room-characters");
  const seen = new Set();
  for (const l of lanes) {
    seen.add(l.lane);
    let el = charEls[l.lane];
    if (!el) {
      el = newCharacter(container, "character");
      el.dataset.lane = l.lane;
      charEls[l.lane] = el;
    }
    el.dataset.kind = l.kind;
    el.dataset.pill = l.pill;
    el.dataset.project = l.project;
    el.dataset.room = l.room;
    const held = l.pill === "needs_steve" ? heldMockupHTML(l, stuckByCard) : "";
    el.innerHTML = held + characterSVG(l.pill, {...laneLook(l.lane), cookie: !!l.cookie}) + plateHTML(l.kind, l.project, l.away);
    el.classList.toggle("eating", !!l.cookie);
    moveCharacter(el, l.pos, l.room);
  }
  for (const name of Object.keys(charEls)) if (!seen.has(name)) { charEls[name].remove(); delete charEls[name]; }
}

const shipEls = {};
function renderShipDock(ships) {
  const container = document.getElementById("room-characters");
  const seen = new Set();
  for (const s of ships) {
    seen.add(s.name);
    let el = shipEls[s.name];
    if (!el) {
      el = newCharacter(container, "character ship");
      el.dataset.ship = s.name;
      shipEls[s.name] = el;
    }
    const st = shipState(s.ship);
    el.innerHTML = shipCharacterSVG(st.mode, st.pill, laneLook("ship:" + s.name)) + plateHTML("Ship", s.name, s.away);
    moveCharacter(el, s.pos, s.room);
  }
  for (const name of Object.keys(shipEls)) if (!seen.has(name)) { shipEls[name].remove(); delete shipEls[name]; }
}

// The in-tray on Steve's desk (card #96: "in each board's row, on the floor page and in
// the studio" — in the office, the cards waiting on him are on his desk): every Stuck
// card's chip, always, grouped by board, each with its own Review button (#100) when it
// has a picture behind it — a direction linked only as a `.html` mockup has none yet, and
// still gets its chip with no button beside it.
function renderRoomStuck(projects, boss) {
  const groups = [];
  for (const p of projects || []) if ((p.stuck || []).length) groups.push(p);
  if (!groups.length || !boss.tray) { document.getElementById("room-stuck").innerHTML = ""; return; }
  const chips = groups.map(p => `<span class="tray-board">${esc(p.name)}</span>` + p.stuck.map(s =>
    `<a class="row-stuck-chip" href="${esc(s.card.url)}" target="_blank" rel="noopener">#${s.card.number}</a>${reviewButtonHTML(s.card.number, null, "tray")}`
  ).join("")).join("");
  document.getElementById("room-stuck").innerHTML = `<div class="row-stuck in-tray"
      style="left:${boss.x + boss.tray.x}px;top:${boss.y + boss.tray.y}px;width:${boss.tray.w}px">
    <div class="row-stuck-chips"><span class="tray-label">In-tray</span>${chips}</div>
  </div>`;
}

// The light over Steve's door: red while anyone is waiting in his office or a card sits in
// his in-tray, amber when a lane is paused or asleep or a build is blocked, green otherwise.
function renderBeacon(state) {
  let color = "green";
  if ((state.header.needs_steve_total || 0) > 0) {
    color = "red";
  } else {
    let amber = false;
    for (const p of state.projects || []) {
      for (const l of p.lanes || [])
        if (l.pill === "paused" || l.pill === "sleeping" || l.plan_late === true) amber = true;
      if (shipState(p.ship).pill === "blocked") amber = true;
    }
    color = amber ? "amber" : "green";
  }
  document.getElementById("beacon").className = "beacon-" + color;
}

// The server room's readout: this box, the tokens the lanes moved in the last day, and
// the lab's other boxes when the floor has a Proxmox token.
function fmtBytes(n) {
  if (n == null) return "—";
  return (n / 1073741824).toFixed(1) + " GB";
}
function fmtTokens(n) {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "k";
  return String(n);
}
function fmtUptime(s) {
  if (s == null) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return d ? `${d}d ${h}h` : `${h}h`;
}
// A meter: red past 85% and amber past 60% when it measures use of a limit; a plain bar
// when it measures a share (one board's tokens against the busiest board's — the busiest
// is not an alarm).
function meterHTML(frac, share) {
  const f = Math.max(0, Math.min(1, frac || 0));
  const cls = share ? "" : f > .85 ? " hot" : f > .6 ? " warm" : "";
  return `<span class="meter${cls}"><i style="width:${Math.round(f * 100)}%"></i></span>`;
}
function tipRow(k, v, frac, cls) {
  return `<div class="tip-row${cls ? " " + cls : ""}"><span class="k">${esc(k)}</span>${frac == null ? "" : meterHTML(frac, cls === "share")}<span class="v">${v}</span></div>`;
}
function renderServerTip(state) {
  const s = state.server || {};
  const box = s.box || {}, tok = s.tokens || {}, pve = s.proxmox || [];
  let html = `<h4>This box · ${esc(state.header.box)}</h4>`;
  html += tipRow("Load", box.load1 == null ? "—" : `${box.load1} on ${box.cpus || "?"} cpus`, box.cpus ? box.load1 / box.cpus : null);
  html += tipRow("Memory", `${fmtBytes(box.mem_used)} of ${fmtBytes(box.mem_total)}`, box.mem_total ? box.mem_used / box.mem_total : null);
  html += tipRow("Disk", `${fmtBytes(box.disk_used)} of ${fmtBytes(box.disk_total)}`, box.disk_total ? box.disk_used / box.disk_total : null);
  html += tipRow("Up", fmtUptime(box.uptime));
  html += `<h4>Tokens, last ${tok.hours || 24}h</h4>`;
  html += tipRow("All lanes", `${fmtTokens(tok.total)} · ~${fmtTokens(tok.per_hour)}/h`);
  const maxTok = Math.max(1, ...(tok.per_project || []).map(p => p.tokens));
  for (const p of tok.per_project || []) html += tipRow(p.name, fmtTokens(p.tokens), p.tokens / maxTok, "share");
  if (!(tok.per_project || []).length) html += `<div class="tip-row"><span class="v">Nothing moved yet today.</span></div>`;
  html += `<h4>The lab · Proxmox</h4>`;
  if (!pve.length) {
    html += `<div class="tip-row"><span class="v">Not connected: PROXMOX_URL and PROXMOX_TOKEN in the box's /etc/watcher/floor.env turn this on.</span></div>`;
  } else {
    for (const g of pve) {
      const label = g.type === "node" ? `${g.name} (host)` : `${g.name} · ${g.type} ${g.vmid}`;
      const stopped = g.status && g.status !== "running" && g.status !== "online";
      const cpu = stopped ? "stopped" : `cpu ${Math.round((g.cpu || 0) * 100)}% · ${fmtBytes(g.mem)} of ${fmtBytes(g.maxmem)}`;
      html += tipRow(label, cpu, stopped ? null : (g.maxmem ? g.mem / g.maxmem : null), stopped ? "stopped" : "");
    }
  }
  document.getElementById("server-tip").innerHTML = html;
}
function openServerTip() { document.getElementById("server-tip").classList.add("open"); }
function closeServerTip() { document.getElementById("server-tip").classList.remove("open"); }
document.getElementById("room-rooms").addEventListener("mouseover", (e) => {
  if (e.target.closest(".office-room.server")) openServerTip();
});
document.getElementById("room-rooms").addEventListener("mouseout", (e) => {
  const to = e.relatedTarget;
  if (e.target.closest(".office-room.server") && !(to && to.closest && to.closest(".office-room.server"))) closeServerTip();
});
document.getElementById("room-rooms").addEventListener("click", (e) => {
  if (e.target.closest(".office-room.server")) {
    document.getElementById("server-tip").classList.toggle("open");
    e.stopPropagation();
    return;
  }
  if (e.target.closest("[data-cookies]")) { cookieTime(); e.stopPropagation(); return; }
  const board = e.target.closest("[data-board]");
  if (board) { openBoard(board.dataset.board); e.stopPropagation(); }
});

function renderStudio(state) {
  lastState = state;
  const office = computeStudio(state);
  renderRooms(office, state);
  renderRoomDesks(office.lanes, office.ships);
  renderRoomStuck(state.projects, office.boss);
  renderCharacters(office.lanes, stuckByCard);
  renderShipDock(office.ships);
  renderBeacon(state);
  renderServerTip(state);
}
// The office is laid out from its own width, so a resized window lays it out again.
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (lastState && document.getElementById("studio").classList.contains("open")) renderStudio(lastState); }, 150);
});

// ---- The boards on Steve's wall --------------------------------------------------------
// Click the bulletin board for every card being worked right now and every card recently
// worked, each with a Peek that reads the card itself off GitHub (title, body, labels,
// its last comments). Click the wins board for what shipped this week, by board.
function openModal(title, sub, bodyHTML, showBack) {
  document.getElementById("office-title").textContent = title;
  document.getElementById("office-sub").textContent = sub || "";
  document.getElementById("office-body").innerHTML = bodyHTML;
  document.getElementById("office-back").style.display = showBack ? "" : "none";
  document.getElementById("office-modal").classList.add("open");
}
function closeModal() { document.getElementById("office-modal").classList.remove("open"); }
let modalBoard = null;
function openBoard(which) {
  modalBoard = which;
  if (!lastState) return;
  if (which === "wins") openModal("The wins board", `shipped in the last ${(lastState.wins || {}).days || 7} days`, winsHTML(lastState), false);
  else openModal("The bulletin board", "every card being worked, and recently worked", bulletinHTML(lastState), false);
}
function peekBtn(project, number) {
  return `<button class="peek-btn" type="button" data-card-project="${esc(project)}" data-card-number="${number}">Peek</button>`;
}
function cardNum(c) {
  return `<span class="num"><a href="${esc(c.url)}" target="_blank" rel="noopener">#${c.number}</a></span>`;
}
function bulletinHTML(state) {
  const now = [];
  for (const p of state.projects || []) for (const l of p.lanes || []) {
    if (!l.card) continue;
    if (l.pill !== "working" && l.pill !== "needs_steve") continue;
    const what = l.pill === "working" ? `${esc(l.stage || "—")} · ${l.minutes}m${planSuffix(l)}` : `needs Steve: ${esc(l.ask || "a decision")}`;
    now.push(`<div class="board-row">${cardNum(l.card)}<span class="what">${esc(p.name)} · ${esc(l.kind)} · ${what}</span>
      <span class="pill ${esc(l.pill)}">${PILL_LABEL[l.pill]}</span>${peekBtn(p.name, l.card.number)}</div>`);
  }
  const recent = (state.recent || []).map(r => `<div class="board-row">${cardNum(r.card)}
      <span class="what">${esc(r.project)} · ${esc(r.lane)}</span><span class="meta">${relTime(r.when)} · ${esc(r.quota)}</span>${peekBtn(r.project, r.card.number)}</div>`);
  return `<h3>Being worked right now</h3>${now.join("") || `<div class="empty-note">Nobody is on a card right now.</div>`}
    <h3>Recently worked</h3>${recent.join("") || `<div class="empty-note">No ticks yet.</div>`}`;
}
function winsHTML(state) {
  const items = ((state.wins || {}).items || []);
  const byProject = {};
  for (const p of state.projects || []) byProject[p.name] = [];
  for (const w of items) (byProject[w.project] = byProject[w.project] || []).push(w);
  const names = Object.keys(byProject).sort();
  if (!names.length) return `<div class="empty-note">No boards on this floor.</div>`;
  return `<div class="board-row"><span class="what"><span class="win-count">${items.length}</span> shipped this week across every board</span></div>`
    + names.map(n => {
      const ws = byProject[n];
      return `<h3>${esc(n)} <span class="win-count">${ws.length}</span></h3>` + (ws.length
        ? ws.map(w => `<div class="board-row">${cardNum(w.card)}<span class="what">${esc(w.title || "(no title on the board)")}</span>
            <span class="meta">${esc(w.lane)} · ${relTime(w.when)}</span>${peekBtn(w.project, w.card.number)}</div>`).join("")
        : `<div class="empty-note">Nothing shipped yet this week.</div>`);
    }).join("");
}
async function openCard(project, number) {
  openModal(`#${number}`, project, `<div class="empty-note">Reading the card…</div>`, true);
  let card = null;
  try {
    const r = await fetch("/api/card.json?" + new URLSearchParams({project, number}).toString(), {cache: "no-store"});
    if (r.ok) card = await r.json();
  } catch (e) { card = null; }
  if (!card || card.unreadable) {
    document.getElementById("office-body").innerHTML = `<div class="empty-note">The card could not be read right now.</div>`;
    return;
  }
  document.getElementById("office-title").textContent = `#${number} · ${card.title || ""}`;
  const labels = (card.labels || []).map(l => `<span class="label">${esc(l)}</span>`).join("");
  const comments = (card.comments || []).map(c => `<div class="card-comment"><div class="who">${esc(c.author)} · ${relTime(c.when)}</div><pre class="card-body">${esc(c.body)}</pre></div>`).join("");
  document.getElementById("office-body").innerHTML = `<div class="card-view">
    <div class="labels"><span class="label">${esc(card.state || "")}</span>${labels}
      <a class="label" href="${esc(card.url)}" target="_blank" rel="noopener">open on GitHub ↗</a></div>
    <pre class="card-body">${esc(card.body || "(no body)")}</pre>${comments}</div>`;
}
document.getElementById("office-body").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-card-number]");
  if (btn) openCard(btn.dataset.cardProject, parseInt(btn.dataset.cardNumber, 10));
});
document.getElementById("office-back").addEventListener("click", () => openBoard(modalBoard));
document.getElementById("office-close").addEventListener("click", closeModal);
document.getElementById("office-modal").addEventListener("click", (e) => { if (e.target.id === "office-modal") closeModal(); });

// Tap a character for a speech bubble with the last line of what it is doing (the same
// line Peek shows); tap the bubble to open the full Peek. It polls only while open, same
// as Peek itself — never in the background, never per character.
let bubbleEl = null, bubbleTimer = null, bubbleLane = null, bubbleShip = null, bubbleTarget = null;
function closeBubble() {
  if (bubbleEl) { bubbleEl.remove(); bubbleEl = null; }
  if (bubbleTimer) { clearInterval(bubbleTimer); bubbleTimer = null; }
  bubbleLane = null; bubbleShip = null; bubbleTarget = null;
}
async function pollBubble() {
  if (!bubbleEl) return;
  const lane = bubbleLane, ship = bubbleShip;
  // The character it is pinned to may have walked since the last poll (its state changed
  // under it); follow, rather than leaving the bubble pointing at an empty desk.
  if (bubbleTarget) { bubbleEl.style.left = bubbleTarget.style.left; bubbleEl.style.top = bubbleTarget.style.top; }
  let text = "…";
  try {
    if (lane) {
      const r = await fetch("/api/tail.json?" + new URLSearchParams({lane}).toString(), {cache: "no-store"});
      const data = await r.json();
      const lines = data.lines || [];
      text = lines.length ? lines[lines.length - 1]
           : data.status === "waiting" ? `Starting #${data.card}…`
           : "Nothing to show yet.";
    } else if (ship) {
      const r = await fetch("/api/ship.json?" + new URLSearchParams({project: ship}).toString(), {cache: "no-store"});
      const data = await r.json();
      text = data.sub || "…";
    }
  } catch (e) {
    text = "can't reach it right now — retrying";
  }
  if (bubbleLane === lane && bubbleShip === ship && bubbleEl) bubbleEl.firstChild.textContent = text;
}
function openBubble(el, lane, ship, name) {
  closeBubble();
  bubbleLane = lane; bubbleShip = ship; bubbleTarget = el;
  bubbleEl = document.createElement("div");
  bubbleEl.className = "bubble";
  bubbleEl.appendChild(document.createTextNode("…"));
  bubbleEl.style.left = el.style.left;
  bubbleEl.style.top = el.style.top;
  bubbleEl.addEventListener("click", (e) => {
    e.stopPropagation();
    openPeek({lane, ship}, name);
    closeBubble();
  });
  document.getElementById("room").appendChild(bubbleEl);
  pollBubble();
  // A lane's session can write a new line at any time; the ship station only changes as
  // often as the real Peek panel polls it (its own server-side cache is this long too).
  bubbleTimer = setInterval(pollBubble, ship ? 15000 : 2000);
}
document.getElementById("room-characters").addEventListener("click", (e) => {
  // The held mockup is tappable on its own (card #100): the overlay, never the bubble.
  const mockup = e.target.closest("[data-review]");
  if (mockup) { e.stopPropagation(); openReviewFromEl(mockup); return; }
  const el = e.target.closest(".character");
  if (!el) return;
  e.stopPropagation();
  const lane = el.dataset.lane || null, ship = el.dataset.ship || null;
  if (bubbleEl && bubbleLane === lane && bubbleShip === ship) { closeBubble(); return; }
  const name = lane ? `${el.dataset.kind} (${lane})` : `Ship (${ship})`;
  openBubble(el, lane, ship, name);
});
document.getElementById("room").addEventListener("click", (e) => {
  if (!e.target.closest(".character") && !e.target.closest(".bubble")) closeBubble();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeBubble(); closeModal(); closeServerTip(); } });

// A canned fixture for ?demo=1 — every pill, every ship state, cycled on a timer, for
// designing the room and for the screenshot script to catch every pose even on a quiet day.
// A tiny real PNG (a few grey pixels) — enough for the browser to actually draw an <img>,
// never a network fetch, so the demo and the screenshot script work with no network.
const DEMO_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABgAAAASCAIAAADOjonJAAAAG0lEQVR4nGPIrvlPFcQwatCoQaMGjRo03AwCAEzKNE7D7YuQAAAAAElFTkSuQmCC";
// Three directions of four shots each (commands/watch.md's own words for the fixture a
// screen change ships with) — one Needs Steve round, lettered and recommended like a
// real one (card #96).
function demoDirectionShots() {
  const labels = ["phone light", "phone dark", "desktop light", "desktop dark"];
  const dir = (letter, recommended) => labels.map(label => ({url: DEMO_PNG, label, letter, recommended}));
  return [...dir("A", true), ...dir("B", false), ...dir("C", false)];
}

function buildDemoStates() {
  const now = new Date().toISOString();
  const proj = (name, lanes, ciState, dep, stuck) => ({
    name, board_url: "#", counts: {ready: 2, in_progress: 1, backlog: 3},
    lanes, stuck: stuck || [],
    ship: {ci: {state: ciState, sha: "abc1234", url: "#"}, ...(dep ? {deploy: dep} : {})},
  });
  const L = (lane, kind, pill, extra) => ({lane, kind, unit: `watcher@${lane}`, pill, link: "#", ...extra});
  const server = {box: {cpus: 4, load1: 0.42, mem_total: 8589934592, mem_used: 2894311424, disk_total: 50407927808,
                        disk_used: 33793540096, uptime: 2786044},
                  tokens: {hours: 24, total: 1240000, per_hour: 51667,
                           per_project: [{name: "alpha", tokens: 840000}, {name: "beta", tokens: 400000}]},
                  proxmox: [{name: "proxmox", vmid: null, type: "node", status: "online", cpu: 0.0096, maxcpu: 8, mem: 14646714368, maxmem: 67314438144},
                            {name: "ollama", vmid: 101, type: "lxc", status: "stopped", cpu: 0, maxcpu: 4, mem: 0, maxmem: 21474836480},
                            {name: "lonestar-web", vmid: 104, type: "lxc", status: "running", cpu: 0.0006, maxcpu: 2, mem: 1641701376, maxmem: 8589934592},
                            {name: "agents", vmid: 107, type: "lxc", status: "running", cpu: 0.0083, maxcpu: 4, mem: 2894311424, maxmem: 8589934592},
                            {name: "ci", vmid: 108, type: "lxc", status: "running", cpu: 0.00002, maxcpu: 4, mem: 2403749888, maxmem: 8589934592}]};
  const wins = {days: 7, items: [
    {project: "alpha", lane: "alpha", card: {number: 8, url: "#"}, title: "The floor gets a studio", when: now},
    {project: "alpha", lane: "alpha-design", card: {number: 6, url: "#"}, title: "A mockup to approve, right on the floor", when: new Date(Date.now() - 86400000 * 2).toISOString()},
    {project: "beta", lane: "beta", card: {number: 1, url: "#"}, title: "Hello, world", when: new Date(Date.now() - 86400000 * 5).toISOString()}]};
  return [
    {header: {box: "demo", time: now, needs_steve_total: 0},
     projects: [proj("alpha", [L("alpha", "Build", "working", {card: {number: 10, url: "#"}, stage: "Building", minutes: 4}),
                               L("alpha-design", "Design", "idle", {reason: "Nothing to pick up right now."}),
                               L("alpha-triage", "Triage", "idle", {reason: "Nothing to pick up right now."})],
                "passing"),
                proj("beta", [L("beta", "Build", "idle", {reason: "Nothing to pick up right now."})], "passing")],
     recent: [{project: "alpha", lane: "alpha", card: {number: 10, url: "#"}, when: now, quota: "~$0.40 equivalent (subscription — not billed)"}],
     server, wins},
    {header: {box: "demo", time: now, needs_steve_total: 2},
     projects: [proj("alpha", [L("alpha", "Build", "needs_steve", {card: {number: 11, url: "#"}, ask: "Pick A or B",
                               images: [DEMO_PNG]}),
                               L("alpha-design", "Design", "paused", {}),
                               L("alpha-triage", "Triage", "working", {card: {number: 12, url: "#"}, stage: "Claimed", minutes: 1})],
                "failing", null,
                // #11 is this lane's own card (also the podium's held picture); #13 is a
                // card no lane's last tick points at — the design lane parked it earlier
                // and has since moved on (card #96's "why nothing shows today"), and it
                // still has to show up here.
                [{card: {number: 11, url: "#"}, ask: "Pick A or B",
                  rounds: [{when: now, images: demoDirectionShots()}]},
                 {card: {number: 13, url: "#"}, ask: "Which weekend fixture should the home page use",
                  rounds: [{when: now, images: []},
                           {when: "2026-09-20T00:00:00Z",
                            images: [{url: DEMO_PNG, label: "the only shot on file", letter: null, recommended: false}]}]}]),
                proj("beta", [L("beta", "Build", "sleeping", {reason: "Card #2 did not finish; trying again shortly."})], "passing")],
     recent: [{project: "alpha", lane: "alpha", card: {number: 11, url: "#"}, when: now, quota: "~$0.90 equivalent (subscription — not billed)"}],
     server, wins},
    {header: {box: "demo", time: now, needs_steve_total: 0},
     projects: [proj("alpha", [L("alpha", "Build", "sleeping", {reason: "Card #9 did not finish; trying again shortly."}),
                               L("alpha-design", "Design", "idle", {reason: "Nothing to pick up right now."}),
                               L("alpha-triage", "Triage", "idle", {reason: "Nothing to pick up right now."})],
                "running"),
                proj("beta", [L("beta", "Build", "working", {card: {number: 2, url: "#"}, stage: "Building", minutes: 12})], "running")],
     recent: [], server, wins},
  ];
}

// Demo mode (?demo=1): cycles the fixture above through every state on a timer, for
// designing the studio and for the screenshot script — never the real data, and never
// both at once. Last in the script, after every const and function it calls.
const DEMO_STATES = buildDemoStates();
let demoIndex = 0;
function demoTick() {
  render(DEMO_STATES[demoIndex]);
  renderStudio(DEMO_STATES[demoIndex]);
  demoIndex = (demoIndex + 1) % DEMO_STATES.length;
}
window.__demoGoto = function (i) {
  demoIndex = ((i % DEMO_STATES.length) + DEMO_STATES.length) % DEMO_STATES.length;
  render(DEMO_STATES[demoIndex]);
  renderStudio(DEMO_STATES[demoIndex]);
  demoIndex = (demoIndex + 1) % DEMO_STATES.length;
};

if (new URLSearchParams(location.search).get("demo")) {
  demoTick();
  window.__demoTimer = setInterval(demoTick, 4000);  // the suite stops it to step by hand
} else {
  refresh();
  setInterval(refresh, 30000);
  // A backgrounded tab has its timers throttled by the browser, so a tab opened and then
  // left in the background can sit well past one failed load before its next retry fires.
  // Refresh the moment it is looked at again, rather than waiting on the interval.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}
// Someone in the break room gets up and walks to another spot every so often — only
// while the office is being looked at; a hidden room has nobody watching it wander.
// (`window.__studioWanderTimer` so the suite can stop it and measure a still room.)
window.__studioWanderTimer = setInterval(() => {
  if (!document.hidden && document.getElementById("studio").classList.contains("open")) wander();
}, 9000);
</script>
</body>
</html>
"""


class Cache:
    """`idle_after_sec`, when set, is card #98's "stop reading boards nobody is looking
    at": the background `loop()` skips its own rebuild once this long has passed since the
    last `get()` — a quiet floor costs nothing, not even the account's GraphQL budget
    (`fetch_board`, inside `build`). `get()` itself still rebuilds synchronously, bypassing
    `refresh_sec`, the moment the cached state is older than `refresh_sec` AND idle skipped
    it — "the first request after a quiet spell pays one read," never a stale page shown
    forever because nobody asked in time. `None` (the default — every OTHER cache this
    file builds, `lane_cache` included) keeps the old unconditional-background-refresh
    behavior exactly, since only `fetch_board` costs an API call worth saving."""

    def __init__(self, build, refresh_sec, idle_after_sec=None):
        self.build = build
        self.refresh_sec = refresh_sec
        self.idle_after_sec = idle_after_sec
        self.lock = threading.Lock()         # guards state/built_at/last_request only
        self.build_lock = threading.Lock()   # ensures only one `build()` runs at a time
        self.state = build()
        now = time.time()
        self.built_at = now
        self.last_request = now

    def get(self):
        with self.lock:
            self.last_request = time.time()
            stale = self.idle_after_sec is not None and time.time() - self.built_at > self.refresh_sec
        if stale:
            self._rebuild()
        with self.lock:
            return self.state

    def _rebuild(self):
        """Builds at most once at a time: `self.lock` is never held across `self.build()`
        (review round 1 — a slow `fetch_board` used to hold up every concurrent
        `/api/state.json` response behind it), and `build_lock` keeps two callers racing
        the same stale state (`get()`'s synchronous catch-up and the background `loop()`,
        or two requests arriving in the same instant) from both paying for a read: the
        second one in re-checks staleness after the first already refreshed, and finds
        nothing left to do."""
        with self.build_lock:
            with self.lock:
                still_stale = time.time() - self.built_at > self.refresh_sec
            if not still_stale:
                return
            try:
                new_state = self.build()
            except Exception as e:
                sys.stderr.write(f"floor.py: refresh failed: {e}\n")
                return
            with self.lock:
                self.state = new_state
                self.built_at = time.time()

    def _maybe_background_rebuild(self):
        """One background-loop tick's worth of decision, pulled out of `loop()` so a test
        can drive it directly without threads or real sleeps."""
        with self.lock:
            idle = (self.idle_after_sec is not None
                    and time.time() - self.last_request > self.idle_after_sec)
        if idle:
            return  # nobody has looked in a while; skip the read
        self._rebuild()

    def loop(self, stop_event):
        while not stop_event.is_set():
            if stop_event.wait(self.refresh_sec):
                return
            self._maybe_background_rebuild()


def make_handler(cache, tail_lookup=None, ship_lookup=None, card_lookup=None):
    """`tail_lookup`, when given, is `(lane, card, file, pos) -> dict | None` — the Peek
    endpoint's `/api/tail.json`, built on `tail_status` above. `None` (the default) keeps
    every existing caller — `bin/test-floor.py`'s `FixedCache` included — working exactly
    as before: that path just 404s. `ship_lookup` is the same for the ship station's Peek:
    `(project name) -> dict | None`, served at `/api/ship.json`."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # never log a request path to stdout/journal — it is nobody's business

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state.json":
                body = json.dumps(cache.get()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/tail.json":
                self._serve_tail(parsed.query)
            elif path == "/api/ship.json":
                names = parse_qs(parsed.query).get("project")
                result = ship_lookup(names[0] if names else "") if ship_lookup else None
                self._send_json(result)
            elif path == "/api/card.json":
                # The bulletin board's Peek: the card itself, by project name and number.
                qs = parse_qs(parsed.query)
                try:
                    number = int((qs.get("number") or ["0"])[0])
                except ValueError:
                    number = 0
                project = (qs.get("project") or [""])[0]
                # A card gh cannot read is a 200 that says so, not a 404 the page's own
                # error line would count as the page breaking.
                card = card_lookup(project, number) if card_lookup and number else None
                self._send_json(card if card is not None else {"unreadable": True})
            else:
                self.send_response(404)
                self.end_headers()

        def _serve_tail(self, query):
            if tail_lookup is None:
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(query)

            def one(key):
                vs = qs.get(key)
                return vs[0] if vs else None

            def one_int(key):
                v = one(key)
                try:
                    return int(v) if v is not None else None
                except ValueError:
                    return None

            self._send_json(tail_lookup(one("lane") or "", one_int("card"), one("file"), one_int("pos")))

        def _send_json(self, result):
            if result is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main(argv):
    host = "0.0.0.0"  # the box has no public interface; off-site reach is over WireGuard
    port = 8787
    projects_dir = os.path.join(REPO_ROOT, "projects")
    state_root = os.environ.get("WATCHER_STATE_ROOT") or os.path.expanduser("~/.local/state/watcher")
    refresh_sec = 60

    i = 0
    try:
        while i < len(argv):
            a = argv[i]
            if a == "--host":
                i += 1; host = argv[i]
            elif a == "--port":
                i += 1; port = int(argv[i])
            elif a == "--projects-dir":
                i += 1; projects_dir = argv[i]
            elif a == "--state-root":
                i += 1; state_root = argv[i]
            elif a == "--refresh-sec":
                i += 1; refresh_sec = int(argv[i])
            elif a in ("-h", "--help"):
                sys.exit(__doc__)
            else:
                sys.exit(f"floor.py: unknown argument {a!r}")
            i += 1
    except IndexError:
        sys.exit(f"floor.py: {a} needs a value")
    except ValueError:
        sys.exit(f"floor.py: {a} wants a number, got {argv[i]!r}")

    deploy_state = os.environ.get("DEPLOY_STATE") or "/var/lib/watcher-deploy"
    deploy_repo = watcher_repo(REPO_ROOT)

    claude_projects_dir = os.environ.get("CLAUDE_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")

    def build_lanes():
        # Peek's own lookup, kept separate from `build()`'s state so a lane's worktree
        # layout is never one of the fields `/api/state.json` serves (ALLOWED_FIELDS).
        return {l["lane"]: l for l in discover_lanes(projects_dir, state_root)}

    # idle_after_sec: once nobody has loaded the page for this long, the background loop
    # stops spending `fetch_board`'s GraphQL reads on a floor nobody is watching (card
    # #98) — `lane_cache` reads only the local filesystem, nothing GraphQL, so it keeps
    # refreshing on a plain timer. Built before `cache`: `Cache.__init__` calls `build()`
    # eagerly, and `build()` (below) reads `lane_cache` — card #105, the deployer's own
    # `waiting_for` needs every lane's state, not only this project's own.
    lane_cache = Cache(build_lanes, refresh_sec)

    def deploy_lookup():
        return deploy_status(REPO_ROOT, deploy_state, deploy_repo,
                              lanes=list(lane_cache.get().values()),
                              running_fn=deploy_service_running)

    def build():
        return build_state(projects_dir, state_root, fetch_board,
                            unit_active_fn=systemctl_is_active, ask_fn=board_ask,
                            images_fn=board_images, rounds_fn=board_rounds, plan_fn=board_plan,
                            ci_fetcher=fetch_ci, deploy_repo=deploy_repo,
                            deploy_fn=deploy_lookup,
                            ratelimit_fn=board_ratelimit, ledger_fn=board_ledger, box_stats_fn=box_stats,
                            proxmox_fn=fetch_proxmox)

    idle_after_sec = max(refresh_sec * 5, 300)
    cache = Cache(build, refresh_sec, idle_after_sec=idle_after_sec)
    stop_event = threading.Event()
    t = threading.Thread(target=cache.loop, args=(stop_event,), daemon=True)
    t.start()
    lt = threading.Thread(target=lane_cache.loop, args=(stop_event,), daemon=True)
    lt.start()

    def tail_lookup(lane, card, file, pos):
        info = lane_cache.get().get(lane)
        if info is None:
            return None
        return tail_status(info, claude_projects_dir, card, file, pos)

    ship_lookup = make_ship_lookup(
        lambda: {l["project_repo"].split("/")[-1]: l["project_repo"] for l in lane_cache.get().values()},
        fetch_ci_runs, deploy_repo, deploy_lookup,
        lambda: read_deploy_history(deploy_state))

    card_lookup = make_card_lookup(
        lambda: {p["name"]: p["project_repo"] for p in group_by_project(lane_cache.get().values())})

    server = ThreadingHTTPServer((host, port), make_handler(cache, tail_lookup, ship_lookup, card_lookup))
    print(f"floor.py: serving on {host}:{port}, refreshing every {refresh_sec}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main(sys.argv[1:])
