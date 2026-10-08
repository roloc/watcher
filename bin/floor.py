#!/usr/bin/env python3
"""A live, read-only page for the agents box: what every lane is doing right now, without
ssh or journalctl (card #40 — mocked up with Steve on September 28, 2026).

    python3 bin/floor.py [--host 0.0.0.0] [--port 8787]
                          [--projects-dir DIR] [--state-root DIR] [--refresh-sec N]

One page for the whole box, not one per project: it groups every lane in `projects/*.env`
by the board it works, and for each lane shows one of exactly five states — **working**
(a card, its Stage, minutes since launch), **idle** (why), **needs Steve** (the card, the
first line of its ask, and — when its Stuck comment points at a mockup or a screenshot —
the picture itself, right there, on the floor and in the studio alike, so approving it
never means clicking through to the card first; card #91), **paused** (a `PAUSE` file), or
**sleeping** (a died tick,
inside the spawner's own ten-minute retry sleep — `how-it-works.md`, "step 8"). A "Recent
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
from datetime import datetime, timezone
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
RECENT_PER_LANE = 20   # ticks kept per lane before the global list is trimmed
RECENT_TOTAL = 15      # ticks shown on the page, across every project

ENV_VARS = ("PROJECT_NAME", "PROJECT_ROOT", "BOARD_OWNER", "PROJECT_REPO", "BOARD_NUMBER",
            "LANE_AGENTS", "LANE_COMMAND", "WATCHER_STATE", "LANE_WORKDIR")

# Every key the JSON endpoint may ever emit. `test-floor.py` walks a real response and
# fails if it finds one outside this set — the rule from the card ("serves only the fields
# above") checked in code, not just read.
ALLOWED_FIELDS = {
    "header", "box", "time", "needs_steve_total",
    "projects", "name", "board_url", "counts", "ready", "in_progress", "backlog",
    "lanes", "board_error",
    "lane", "kind", "unit", "unit_active", "pill", "link", "card", "stage", "minutes",
    "reason", "ask", "images", "number", "url",
    "recent", "project", "when", "quota",
    # the ship station (after the lanes): CI on main, and the box's own deployer
    "ship", "ci", "deploy", "state", "sha", "failing", "live", "target",
}


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
    env = dict(os.environ, BOARD_OWNER=owner, BOARD_NUMBER=str(number))
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


def deploy_status(repo_dir, deploy_state, repo, git=_git):
    """What deploy/autodeploy.sh is doing, from the files it keeps (read-only):
    live | checking | testing | waiting | swapping | held."""
    live = git(repo_dir, "rev-parse", "HEAD")
    target = git(repo_dir, "rev-parse", "origin/main")
    if not live:
        return None
    out = {"live": live[:7], "target": (target or live)[:7]}
    held = _read(os.path.join(deploy_state, "held", target)) if target else None
    draining = (_read(os.path.join(deploy_state, "draining")) or "").split()
    ready = (_read(os.path.join(deploy_state, "ready")) or "").split()
    evals = _read(os.path.join(deploy_state, "evals", target)) if target else None
    # Swapping and live come before held: a commit shipped with deploy-anyway keeps its own
    # held/<sha> file (the deployer only clears the OTHER held cards), and the page must not
    # call a live commit Blocked, or count it as needing Steve.
    if draining:
        out["state"], out["target"] = "swapping", draining[0][:7]
    elif not target or live == target:
        out["state"] = "live"
    elif held is not None:
        out["state"] = "held"
        if held.isdigit():
            out["card"] = {"number": int(held), "url": f"https://github.com/{repo}/issues/{held}"}
    elif ready and ready[0] == live:
        out["state"] = "waiting"
    elif evals and evals.startswith("fail"):
        out["state"] = "testing"
    else:
        out["state"] = "checking"
    return out


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


def lane_desk(lane, items_by_number, board_url, now, last_tick=None, unit_active=None, ask=None,
              images=None):
    """One lane's pill: exactly one of working | idle | needs_steve | paused | sleeping.
    `last_tick` is the lane's own newest log entry (a `metrics.Tick`, or None — loaded
    once by the caller, not re-read here: a lane's own `Cache` refresh used to parse every
    log file twice, once for this and once for the recent-ticks list). `ask` is a callable
    `(issue, project_repo) -> str` (board.py's `needs_steve_ask`, injected so tests never
    touch the network); `images` is the same shape for `needs_steve_images` — a mockup or
    a screenshot to show right on the desk (card #91), never required to be called for a
    pill other than needs_steve; `unit_active` is `systemctl is-active`'s answer, already
    looked up by the caller (None when it could not be checked)."""
    base = {"lane": lane["lane"], "kind": lane_kind(lane), "unit": lane["unit"]}
    if unit_active is not None:
        base["unit_active"] = unit_active
    state_dir = lane["state_dir"]

    if os.path.exists(os.path.join(state_dir, "PAUSE")):
        return {**base, "pill": "paused", "link": board_url}

    current_path = os.path.join(state_dir, "current")
    n = _read_current(current_path)
    if n is not None:
        try:
            launched = datetime.fromtimestamp(os.path.getmtime(current_path), tz=timezone.utc)
            minutes = _minutes_since(launched, now)
        except OSError:
            minutes = 0
        it = items_by_number.get(n) or {}
        url = it.get("url") or board_url
        return {**base, "pill": "working", "link": url, "card": {"number": n, "url": url},
                "stage": it.get("stage") or "", "minutes": minutes}

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
    "testing": "retesting {target}", "waiting": "{target} passed; waiting for idle lanes",
    "swapping": "swapping in {target}", "held": "held: {target}",
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
        result = {"status": "ok", **ship_lines(runs, now, deploy, history, has_deployer=deploys)}
        with lock:
            cache[name] = (time.monotonic(), result)
        return result

    return lookup


def _fmt_quota(cost):
    if cost is None:
        return "unknown"
    return f"~${cost:.2f} equivalent (subscription — not billed)"


# --- assembling the page's data -----------------------------------------------------------

def build_state(projects_dir, state_root, board_fetcher, unit_active_fn=None, ask_fn=None,
                 images_fn=None, now=None, box_name=None, ci_fetcher=None, deploy_fn=None,
                 deploy_repo=None):
    now = now or datetime.now(timezone.utc)
    box_name = box_name or socket.gethostname()
    lanes = discover_lanes(projects_dir, state_root)
    projects = group_by_project(lanes)

    out_projects = []
    needs_steve_total = 0
    recent = []

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
                              unit_active=active, ask=ask_fn, images=images_fn)
            if desk["pill"] == "needs_steve":
                needs_steve_total += 1
            lane_out.append(desk)

            for t in ticks[-RECENT_PER_LANE:]:
                url = f"https://github.com/{lane['project_repo']}/issues/{t.issue}"
                recent.append({
                    "project": p["name"], "lane": lane["lane"],
                    "card": {"number": t.issue, "url": url},
                    "when": t.mtime.isoformat(), "quota": _fmt_quota(t.cost),
                })

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

        entry = {"name": p["name"], "board_url": p["board_url"],
                  "counts": project_counts(items), "lanes": lane_out, "ship": ship}
        if board_error:
            entry["board_error"] = board_error
        out_projects.append(entry)

    recent.sort(key=lambda r: r["when"], reverse=True)

    return {
        "header": {"box": box_name, "time": now.isoformat(),
                   "needs_steve_total": needs_steve_total},
        "projects": out_projects,
        "recent": recent[:RECENT_TOTAL],
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
  .pill.idle { background: color-mix(in srgb, var(--idle) 18%, transparent); color: var(--idle); }
  .pill.needs_steve { background: color-mix(in srgb, var(--needs) 18%, transparent); color: var(--needs); }
  .pill.paused { background: color-mix(in srgb, var(--paused) 18%, transparent); color: var(--paused); }
  .pill.sleeping { background: color-mix(in srgb, var(--sleep) 18%, transparent); color: var(--sleep); }
  .detail { font-size: 13px; margin-top: 6px; color: var(--dim); }
  /* A mockup or a screenshot a Needs Steve comment pointed at, shown right on the desk
     instead of making Steve click through to the card to find it (card #91). */
  .mockups { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .mockup-thumb {
    max-width: 120px; max-height: 90px; border-radius: 6px; border: 1px solid var(--line);
    object-fit: cover;
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
  #peek-close {
    background: none; border: none; color: var(--dim); font-size: 20px; line-height: 1;
    cursor: pointer; padding: 0 4px;
  }
  #peek-lines {
    margin: 0; padding: 12px 14px; overflow-y: auto; flex: 1;
    font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    white-space: pre-wrap; word-break: break-word;
  }
  /* ---- The studio: one room the lanes walk around in (card #88). The floor page above
     is untouched; this is a second view of the same data, toggled by one button. ---- */
  .view-toggle {
    font-size: 13px; font-weight: 600; padding: 6px 14px; border-radius: 999px;
    border: 1px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer;
  }
  .view-toggle:hover { border-color: var(--dim); }
  #studio { display: none; }
  #studio.open { display: block; }
  #studio-head { display: flex; justify-content: center; margin-bottom: 10px; }
  #room {
    position: relative; width: 100%; max-width: 900px; margin: 0 auto;
    aspect-ratio: 3 / 4; background: var(--panel); border: 1px solid var(--line);
    border-radius: 14px; overflow: hidden;
  }
  @media (min-width: 700px) { #room { aspect-ratio: 16 / 10; } }
  /* The room is rows: one band per project, its name at the left edge, its stations
     (dispatch counter, a desk per lane, the ship dock) spread across it. A lane stays in
     its own project's row whatever it is doing — except on the couch (asleep after a
     failure) and at the podium (needs Steve), the two places that are the room's, not a
     project's. The first studio sent idle lanes to a shared lounge of five fixed spots;
     on a box with six idle lanes the sixth stood on the first, and nothing said which
     project any of them belonged to. */
  .room-label {
    position: absolute; font-size: 10px; color: var(--dim); font-weight: 600;
    text-transform: uppercase; letter-spacing: .04em; white-space: nowrap;
  }
  .room-door {
    position: absolute; left: 50%; top: 2%; transform: translate(-50%, 0);
    width: 60px; height: 8px; background: var(--line); border-radius: 2px;
  }
  #beacon {
    /* Inside #room's own box (which clips: `overflow: hidden`), not above it — a beacon
       placed outside that box is a beacon nobody sees. */
    position: absolute; left: 50%; top: 1%; transform: translate(-50%, 0);
    width: 12px; height: 12px; border-radius: 50%;
  }
  .beacon-green { background: var(--working); }
  .beacon-amber { background: var(--paused); }
  .beacon-red { background: var(--needs); animation: blink 1.2s infinite; }
  .podium {
    position: absolute; left: 80%; top: 13%; transform: translate(-50%, -100%);
    width: 22px; height: 14px; border-radius: 3px 3px 1px 1px;
    background: color-mix(in srgb, var(--dim) 30%, transparent); border: 1px solid var(--line);
  }
  .zone {
    position: absolute; left: 2%; right: 2%; border-radius: 8px;
    border: 1px dashed var(--line); background: color-mix(in srgb, var(--dim) 4%, transparent);
  }
  .zone-name {
    position: absolute; left: 3.5%; font-size: 12px; font-weight: 700; color: var(--ink);
    white-space: nowrap; text-decoration: none;
  }
  .zone-name:hover { text-decoration: underline; }
  #room-desks .zone-name { pointer-events: auto; }
  .desk-furniture {
    position: absolute; width: 44px; height: 18px; transform: translate(-50%, -155%);
    border: 1px solid var(--line); border-radius: 3px;
    background: color-mix(in srgb, var(--dim) 10%, transparent);
  }
  .dispatch-counter {
    position: absolute; width: 44px; height: 14px; transform: translate(-50%, -200%);
    border: 1px solid var(--line); border-radius: 3px;
    background: color-mix(in srgb, var(--dim) 14%, transparent);
  }
  .desk-monitor {
    position: absolute; width: 16px; height: 11px; transform: translate(50%, -345%);
    border-radius: 2px; background: var(--line); overflow: hidden;
  }
  .desk-monitor .scanline {
    position: absolute; left: 2px; right: 2px; top: 1px; height: 1.5px;
    background: var(--working); opacity: 0;
  }
  @keyframes scan { 0% { top: 1px; } 100% { top: 8px; } }
  /* An idle screen is dark (card: "an idle screen is dark") — the line only exists, and
     only moves, while the desk is lit. */
  .desk-monitor.lit .scanline { opacity: .6; animation: scan 1.1s linear infinite; }
  .couch {
    position: absolute; left: 50%; top: 81%; transform: translate(-50%, -50%);
    width: 30%; height: 7%; border-radius: 10px;
    background: color-mix(in srgb, var(--dim) 14%, transparent); border: 1px solid var(--line);
  }
  .coffee {
    position: absolute; left: 72%; top: 81%; transform: translate(-50%, -50%);
    width: 14px; height: 16px; border-radius: 2px 2px 6px 6px; background: var(--dim);
  }
  /* The wall by the door: a strip along the bottom, every card whole. It scrolls
     sideways on a narrow screen rather than clipping a card in half. */
  .pin-wall {
    position: absolute; left: 2%; right: 2%; top: 87%; bottom: 2%;
    display: flex; gap: 6px; align-items: flex-start; overflow-x: auto; overflow-y: hidden;
    padding: 0 2px; scrollbar-width: none;
  }
  .pin-wall::-webkit-scrollbar { display: none; }
  /* More cards than fit: the strip scrolls sideways, and its right edge fades to say so,
     instead of showing a card cut in half. */
  .pin-wall.scrolls { mask-image: linear-gradient(to right, #000 88%, transparent); -webkit-mask-image: linear-gradient(to right, #000 88%, transparent); }
  .pin-card {
    flex: 0 0 auto; font-size: 10px; line-height: 1.3; background: var(--panel);
    border: 1px solid var(--line); border-radius: 3px; padding: 4px 6px; text-decoration: none;
    color: inherit; box-shadow: 0 1px 2px rgba(0, 0, 0, .12); display: block; white-space: nowrap;
  }
  .pin-card b { font-weight: 600; }
  #room-desks, #room-characters { position: absolute; inset: 0; pointer-events: none; }
  .character {
    /* As wide as its own plate (not a fixed 30px that the plate overflowed to the right),
       so translate(-50%) centers the figure and the words together over the spot. */
    position: absolute; width: max-content; transform: translate(-50%, -100%);
    transition: left 1s ease, top 1s ease; cursor: pointer; text-align: center;
    pointer-events: auto;
  }
  @keyframes character-wave { 0%, 100% { transform: rotate(-8deg); } 50% { transform: rotate(8deg); } }
  .character-wave { animation: character-wave .6s ease-in-out infinite; }
  @media (prefers-reduced-motion: reduce) {
    .character { transition: none; }
    .character-wave { animation: none; }
  }
  .character svg { width: 30px; height: 34px; display: block; margin: 0 auto; }
  /* Held up over a waving character's head — the same mockup the floor's own desk shows
     (card #91), shrunk to fit the room. */
  .character-mockup { margin-bottom: 2px; }
  .character-mockup .mockup-thumb {
    display: block; margin: 0 auto; max-width: 46px; max-height: 34px; border-radius: 3px;
  }
  .character .plate {
    font-size: 9px; color: var(--ink); font-weight: 600; white-space: nowrap; margin-top: 2px; line-height: 1.2;
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
</style>
</head>
<body>
<header>
  <h1 id="box">The floor</h1>
  <div class="muted" id="clock"></div>
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
    <div id="beacon" class="beacon-green"></div>
    <div class="room-door"></div>
    <div class="room-label" style="right:2%;top:2%">Podium</div>
    <div class="podium"></div>
    <div class="couch" id="couch"></div>
    <div class="coffee" id="coffee"></div>
    <div class="pin-wall" id="pin-wall"></div>
    <div id="room-desks"></div>
    <div id="room-characters"></div>
  </div>
</div>
<div id="peek-overlay">
  <div id="peek-panel">
    <div class="peek-head">
      <div><strong id="peek-title"></strong> <span id="peek-sub"></span></div>
      <button id="peek-close" aria-label="Close">&times;</button>
    </div>
    <pre id="peek-lines"></pre>
  </div>
</div>
<script id="main">
const PILL_LABEL = {working: "Working", idle: "Idle", needs_steve: "Needs Steve",
                     paused: "Paused", sleeping: "Sleeping"};

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
function shipState(ship) {
  const ci = (ship && ship.ci) || {state: "unknown"};
  const dep = ship && ship.deploy;
  const lines = [];
  const ciLine = {passing: "all checks passed", running: "checks running",
                  failing: "failing: " + (ci.failing || []).join(", "), none: "no checks"}[ci.state];
  lines.push(ci.state === "unknown" ? "CI unreachable" : `main ${esc(ci.sha)}: ${esc(ciLine)}`);
  if (dep) lines.push({live: `live on the box: ${esc(dep.live)}`,
                       checking: `checking ${esc(dep.target)}: CI and tests`,
                       testing: `retesting ${esc(dep.target)}`,
                       waiting: `${esc(dep.target)} passed; waiting for idle lanes`,
                       swapping: `swapping in ${esc(dep.target)}`,
                       held: `held: ${esc(dep.target)}` + (dep.card ? ` (#${dep.card.number})` : "")}[dep.state] || "");
  let pill = "ok", mode = "done";
  if ((dep && dep.state === "held") || ci.state === "failing") { pill = "blocked"; mode = "blocked"; }
  else if (dep && dep.state === "waiting") { pill = "busy"; mode = "waiting"; }
  else if (dep && dep.state === "swapping") { pill = "busy"; mode = "pushing"; }
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

function deskHTML(d) {
  let detail = "";
  if (d.pill === "working") detail = `#${d.card.number} · ${d.stage ? esc(d.stage) : "—"} · ${d.minutes}m`;
  else if (d.pill === "needs_steve") detail = `#${d.card.number}: ${esc(d.ask) || "needs a decision"}`;
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
      <span class="pill ${esc(d.pill)}">${PILL_LABEL[d.pill] || esc(d.pill)}</span>
      <div class="detail">${detail}</div>
      ${d.pill === "needs_steve" ? mockupThumbsHTML(d.images) : ""}
    </a>
    <button class="peek-btn" type="button" data-peek-lane="${esc(d.lane)}" data-peek-name="${esc(d.kind)} (${esc(d.lane)})">Peek</button>
  </div>`;
}

function render(state) {
  document.getElementById("box").textContent =
    "The floor — " + state.header.box +
    (state.header.needs_steve_total > 0 ? ` — ${state.header.needs_steve_total} need Steve` : "");
  document.getElementById("clock").textContent = new Date(state.header.time).toLocaleString();

  const projects = document.getElementById("projects");
  projects.innerHTML = state.projects.map(p => `
    <section class="project">
      <h2><a href="${esc(p.board_url)}" target="_blank" rel="noopener">${esc(p.name)}</a></h2>
      <div class="counts">Ready ${p.counts.ready} · In progress ${p.counts.in_progress} · Backlog ${p.counts.backlog}
        ${p.board_error ? `<span class="alert"> — board unreachable</span>` : ""}</div>
      <div class="desks">${p.lanes.map(deskHTML).join("")}${shipHTML(p.ship, p.name)}</div>
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
  const pre = document.getElementById("peek-lines");
  const text = data.lines.join("\\n");
  if (pre.textContent !== text) pre.textContent = text; // unchanged: leave the scroll alone
}

function openPeek(target, name) {
  peekLane = target.lane || null; peekShip = target.ship || null;
  peekCard = null; peekFile = null; peekPos = null;
  document.getElementById("peek-title").textContent = name || peekLane || peekShip;
  peekSub("", false);
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

// ---- The studio: one room the lanes walk around in (card #88) -----------------------
// A second view of the same data `render()` already has: no new endpoint, no new field.
// Toggled by one button; `render()` above is untouched, `refresh()` just hands it the
// same state it was already given.

function showStudio() {
  document.getElementById("floor-view").style.display = "none";
  document.getElementById("studio").classList.add("open");
  document.getElementById("studio-btn").style.display = "none";
  markWallScroll();
}
function showFloor() {
  closeBubble();
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
  // instead of replacing it), the card's "walks to the podium and waves".
  const armRRaised = `<rect x="21" y="7" width="3" height="8" rx="1" style="fill:${c}" transform="rotate(35 22.5 15)"/>`;
  const armR = !armsUp ? `<rect x="20" y="15" width="3" height="7" rx="1" style="fill:${c}"/>`
    : pill === "needs_steve"
      ? `<g class="character-wave" style="transform-box:fill-box;transform-origin:50% 100%">${armRRaised}</g>`
      : armRRaised;
  const sleepy = pill === "sleeping"
    ? `<text x="17" y="3" style="font:700 7px sans-serif;fill:var(--dim)">z</text>` : "";
  // Idle: standing by its own station with a mug, waiting for a card.
  const mug = pill === "idle"
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

// Where things stand. The room is rows: one band per project between ROWS_TOP and
// ROWS_BOTTOM, the project's name at its left edge, its stations spread across it — the
// dispatch counter (a triage lane), a desk per other lane, the ship dock last. A lane
// stays in its own row whatever it is doing, so a row always shows its whole team: at its
// desk working, by it with a mug when idle, hands up when paused. Only two places are the
// room's rather than a project's — the couch (asleep after a failure) and the podium by
// the door (needs Steve) — and a plate there says which project the lane came from. (The
// first studio sent every idle lane to a shared lounge of five fixed spots: on a box with
// six idle lanes the sixth stood on the first, and nothing said whose they were.)
const ROWS_TOP = 17, ROWS_BOTTOM = 72, ROW_MAX = 20;
const PODIUM = {x: 80, y: 16, span: 36};
const COUCH = {x: 50, span: 24};

function rowBand(pIndex, pCount) {
  const h = Math.min(ROW_MAX, (ROWS_BOTTOM - ROWS_TOP) / Math.max(pCount, 1));
  return {top: ROWS_TOP + pIndex * h, height: h};
}
// Where the rows end: the couch sits just under the last one, the wall under that, so a
// box with one project is not a room with everything in its top third.
function rowsBottom(pCount) { return rowBand(Math.max(pCount, 1) - 1, pCount).top + rowBand(0, pCount).height; }
// `n` spots spread evenly across a span centered on `cx` — never a fixed list that wraps
// around and puts the (n+1)th on top of the first.
function spread(i, n, cx, span) {
  if (n <= 1) return cx;
  return cx - span / 2 + i * (span / (n - 1));
}
function stationX(i, n) { return spread(i, n, 51, 74); }

function computeStudio(state) {
  const projects = state.projects || [];
  const lanes = [], ships = [], rows = [];
  projects.forEach((p, pIndex) => {
    const band = rowBand(pIndex, projects.length);
    const feetY = band.top + band.height * 0.9;
    const ordered = [...(p.lanes || [])].sort(
      (a, b) => (a.kind === "Triage" ? 0 : 1) - (b.kind === "Triage" ? 0 : 1));
    const n = ordered.length + 1; // the ship dock is the last station
    rows.push({name: p.name, board_url: p.board_url, top: band.top, height: band.height});
    ordered.forEach((l, i) => lanes.push({...l, project: p.name, home: {x: stationX(i, n), y: feetY}}));
    ships.push({name: p.name, ship: p.ship, pos: {x: stationX(n - 1, n), y: feetY}});
  });
  const sleeping = lanes.filter(l => l.pill === "sleeping");
  const needs = lanes.filter(l => l.pill === "needs_steve");
  const couchY = rowsBottom(projects.length) + 9;
  for (const l of lanes) { l.pos = l.home; l.away = false; }
  sleeping.forEach((l, i) => {
    l.pos = {x: spread(i, sleeping.length, COUCH.x, Math.min(COUCH.span, 10 * (sleeping.length - 1))), y: couchY};
    l.away = true;
  });
  needs.forEach((l, i) => {
    l.pos = {x: spread(i, needs.length, PODIUM.x, Math.min(PODIUM.span, 12 * (needs.length - 1))), y: PODIUM.y};
    l.away = true;
  });
  return {lanes, ships, rows, bottom: rowsBottom(projects.length)};
}

function renderRoomDesks(lanes, rows) {
  const zones = rows.map(r => `
    <div class="zone" style="top:${r.top}%;height:${r.height - 1.5}%"></div>
    <a class="zone-name" style="top:${r.top + 1}%" href="${esc(r.board_url)}" target="_blank" rel="noopener">${esc(r.name)}</a>`);
  const furniture = lanes.map(l => l.kind === "Triage"
    ? `<div class="dispatch-counter" style="left:${l.home.x}%;top:${l.home.y}%"></div>`
    : `<div class="desk-furniture" style="left:${l.home.x}%;top:${l.home.y}%"></div>
       <div class="desk-monitor${l.pill === "working" ? " lit" : ""}" data-lane="${esc(l.lane)}"
            style="left:${l.home.x}%;top:${l.home.y}%"><div class="scanline"></div></div>`);
  document.getElementById("room-desks").innerHTML = zones.join("") + furniture.join("");
}

// The plate under a character: what it is (Build, Design, Triage), and — only when it has
// left its row for the couch or the podium — whose it is.
function plateHTML(kind, project, away) {
  return `<div class="plate">${esc(kind)}${away ? ` <span class="where">· ${esc(project)}</span>` : ""}</div>`;
}

// Characters keep the same DOM node across renders, so moving `left`/`top` is a CSS
// transition — a walk — rather than a jump cut (`prefers-reduced-motion` drops it to a
// jump cut on purpose).
const charEls = {};
function renderCharacters(lanes) {
  const container = document.getElementById("room-characters");
  const seen = new Set();
  for (const l of lanes) {
    seen.add(l.lane);
    let el = charEls[l.lane];
    if (!el) {
      el = document.createElement("div");
      el.className = "character";
      el.dataset.lane = l.lane;
      container.appendChild(el);
      charEls[l.lane] = el;
    }
    el.dataset.kind = l.kind;
    el.dataset.pill = l.pill;
    el.dataset.project = l.project;
    // Held up over the waving character, the same mockup the floor's own desk shows — the
    // room Steve walks instead of the card he'd otherwise click through to (card #91).
    const held = l.pill === "needs_steve" ? mockupThumbsHTML((l.images || []).slice(0, 1), "character-mockup") : "";
    el.innerHTML = held + characterSVG(l.pill, laneLook(l.lane)) + plateHTML(l.kind, l.project, l.away);
    el.style.left = l.pos.x + "%";
    el.style.top = l.pos.y + "%";
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
      el = document.createElement("div");
      el.className = "character";
      el.dataset.ship = s.name;
      container.appendChild(el);
      shipEls[s.name] = el;
    }
    const st = shipState(s.ship);
    el.innerHTML = shipSVG(st.mode, st.pill) + plateHTML("Ship", s.name, false);
    el.style.left = s.pos.x + "%";
    el.style.top = s.pos.y + "%";
  }
  for (const name of Object.keys(shipEls)) if (!seen.has(name)) { shipEls[name].remove(); delete shipEls[name]; }
}

function renderPinWall(recent) {
  // Newest first: `state.recent` already sorts newest-first, and the strip runs left to
  // right in that same order. Each card names the issue and the project it is on.
  const wall = document.getElementById("pin-wall");
  wall.innerHTML = (recent || []).slice(0, 8).map(r => `
    <a class="pin-card" href="${esc(r.card.url)}" target="_blank" rel="noopener"><b>#${r.card.number}</b>
      · ${esc(r.project)}<br>${relTime(r.when)}</a>`).join("");
  markWallScroll();
}
// Whether the wall overflows is only measurable while the studio is showing (a hidden
// strip is 0 wide), so this runs again the moment it opens, not just on each redraw.
function markWallScroll() {
  const wall = document.getElementById("pin-wall");
  wall.classList.toggle("scrolls", wall.scrollWidth > wall.clientWidth + 1);
}

function renderBeacon(state) {
  let color = "green";
  if ((state.header.needs_steve_total || 0) > 0) {
    color = "red";
  } else {
    let amber = false;
    for (const p of state.projects || []) {
      for (const l of p.lanes || []) if (l.pill === "paused" || l.pill === "sleeping") amber = true;
      if (shipState(p.ship).pill === "blocked") amber = true;
    }
    color = amber ? "amber" : "green";
  }
  document.getElementById("beacon").className = "beacon-" + color;
}

function renderStudio(state) {
  const {lanes, ships, rows, bottom} = computeStudio(state);
  for (const id of ["couch", "coffee"]) document.getElementById(id).style.top = (bottom + 9) + "%";
  document.getElementById("pin-wall").style.top = (bottom + 15) + "%";
  renderRoomDesks(lanes, rows);
  renderCharacters(lanes);
  renderShipDock(ships);
  renderPinWall(state.recent);
  renderBeacon(state);
}

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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeBubble(); });

// A canned fixture for ?demo=1 — every pill, every ship state, cycled on a timer, for
// designing the room and for the screenshot script to catch every pose even on a quiet day.
function buildDemoStates() {
  const now = new Date().toISOString();
  const proj = (name, lanes, ciState, dep) => ({
    name, board_url: "#", counts: {ready: 2, in_progress: 1, backlog: 3},
    lanes, ship: {ci: {state: ciState, sha: "abc1234", url: "#"}, ...(dep ? {deploy: dep} : {})},
  });
  const L = (lane, kind, pill, extra) => ({lane, kind, unit: `watcher@${lane}`, pill, link: "#", ...extra});
  return [
    {header: {box: "demo", time: now, needs_steve_total: 0},
     projects: [proj("alpha", [L("alpha", "Build", "working", {card: {number: 10, url: "#"}, stage: "Building", minutes: 4}),
                               L("alpha-design", "Design", "idle", {reason: "Nothing to pick up right now."}),
                               L("alpha-triage", "Triage", "idle", {reason: "Nothing to pick up right now."})],
                "passing"),
                proj("beta", [L("beta", "Build", "idle", {reason: "Nothing to pick up right now."})], "passing")],
     recent: [{project: "alpha", lane: "alpha", card: {number: 10, url: "#"}, when: now, quota: "~$0.40 equivalent (subscription — not billed)"}]},
    {header: {box: "demo", time: now, needs_steve_total: 1},
     projects: [proj("alpha", [L("alpha", "Build", "needs_steve", {card: {number: 11, url: "#"}, ask: "Pick A or B",
                               images: ["data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABgAAAASCAIAAADOjonJAAAAG0lEQVR4nGPIrvlPFcQwatCoQaMGjRo03AwCAEzKNE7D7YuQAAAAAElFTkSuQmCC"]}),
                               L("alpha-design", "Design", "paused", {}),
                               L("alpha-triage", "Triage", "working", {card: {number: 12, url: "#"}, stage: "Claimed", minutes: 1})],
                "failing"),
                proj("beta", [L("beta", "Build", "sleeping", {reason: "Card #2 did not finish; trying again shortly."})], "passing")],
     recent: [{project: "alpha", lane: "alpha", card: {number: 11, url: "#"}, when: now, quota: "~$0.90 equivalent (subscription — not billed)"}]},
    {header: {box: "demo", time: now, needs_steve_total: 0},
     projects: [proj("alpha", [L("alpha", "Build", "sleeping", {reason: "Card #9 did not finish; trying again shortly."}),
                               L("alpha-design", "Design", "idle", {reason: "Nothing to pick up right now."}),
                               L("alpha-triage", "Triage", "idle", {reason: "Nothing to pick up right now."})],
                "running"),
                proj("beta", [L("beta", "Build", "working", {card: {number: 2, url: "#"}, stage: "Building", minutes: 12})], "running")],
     recent: []},
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
  setInterval(demoTick, 4000);
} else {
  refresh();
  setInterval(refresh, 30000);
  // A backgrounded tab has its timers throttled by the browser, so a tab opened and then
  // left in the background can sit well past one failed load before its next retry fires.
  // Refresh the moment it is looked at again, rather than waiting on the interval.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}
</script>
</body>
</html>
"""


class Cache:
    def __init__(self, build, refresh_sec):
        self.build = build
        self.refresh_sec = refresh_sec
        self.lock = threading.Lock()
        self.state = build()

    def get(self):
        with self.lock:
            return self.state

    def loop(self, stop_event):
        while not stop_event.is_set():
            if stop_event.wait(self.refresh_sec):
                return
            try:
                new_state = self.build()
            except Exception as e:
                sys.stderr.write(f"floor.py: refresh failed: {e}\n")
                continue
            with self.lock:
                self.state = new_state


def make_handler(cache, tail_lookup=None, ship_lookup=None):
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

    def build():
        return build_state(projects_dir, state_root, fetch_board,
                            unit_active_fn=systemctl_is_active, ask_fn=board_ask,
                            images_fn=board_images,
                            ci_fetcher=fetch_ci, deploy_repo=deploy_repo,
                            deploy_fn=lambda: deploy_status(REPO_ROOT, deploy_state, deploy_repo))

    def build_lanes():
        # Peek's own lookup, kept separate from `build()`'s state so a lane's worktree
        # layout is never one of the fields `/api/state.json` serves (ALLOWED_FIELDS).
        return {l["lane"]: l for l in discover_lanes(projects_dir, state_root)}

    cache = Cache(build, refresh_sec)
    lane_cache = Cache(build_lanes, refresh_sec)
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
        fetch_ci_runs, deploy_repo,
        lambda: deploy_status(REPO_ROOT, deploy_state, deploy_repo),
        lambda: read_deploy_history(deploy_state))

    server = ThreadingHTTPServer((host, port), make_handler(cache, tail_lookup, ship_lookup))
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
