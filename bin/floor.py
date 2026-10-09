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
    # the Needs Steve strip (card #96): every Stuck card on the board, whichever lane last
    # touched it, with every round it has been asked, newest first
    "stuck", "rounds", "label", "letter", "recommended",
    # the account's GraphQL budget (card #98), read from board.py's own cache — never a
    # call of its own
    "github_remaining", "github_reset", "github_limit",
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
                 images_fn=None, rounds_fn=None, now=None, box_name=None, ci_fetcher=None,
                 deploy_fn=None, deploy_repo=None, ratelimit_fn=None):
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
            lane_out.append(desk)

            for t in ticks[-RECENT_PER_LANE:]:
                url = f"https://github.com/{lane['project_repo']}/issues/{t.issue}"
                recent.append({
                    "project": p["name"], "lane": lane["lane"],
                    "card": {"number": t.issue, "url": url},
                    "when": t.mtime.isoformat(), "quota": _fmt_quota(t.cost),
                })

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

    return {
        "header": {"box": box_name, "time": now.isoformat(),
                   "needs_steve_total": needs_steve_total,
                   "github_remaining": rl.get("remaining"),
                   "github_reset": rl.get("resetAt"),
                   "github_limit": rl.get("limit")},
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


def board_rounds(issue, project_repo, board_py=None, timeout=20):
    """Every `Needs Steve` round for this card, newest first — `board.py images
    --all-rounds`, which wraps `needs_steve_rounds` — each with its own pictures, lettered
    by direction (card #96: the Needs Steve strip's carousel, which flips through every
    round a card has been asked, not only the lane's own single-thumbnail desk pill).
    Same `GH_REPO` pinning as `board_ask`/`board_images`, for the same reason. `[]` on any
    failure — a card the strip then shows with its ask and no pictures, never a crash."""
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
  /* The Needs Steve strip (card #96): every card Stuck on this board, whichever lane last
     touched it — a mockup to approve right here, flipped through with a carousel, never
     only a link off to the card. */
  .needs-strip { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
  .needs-strip.compact { gap: 6px; }
  .needs-card {
    border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px;
    background: color-mix(in srgb, var(--needs) 5%, transparent); flex: 1 1 220px; max-width: 320px;
  }
  .needs-strip.compact .needs-card { flex: 0 0 auto; max-width: 150px; padding: 4px 6px; font-size: 11px; }
  .needs-ask {
    display: block; font-size: 13px; font-weight: 600; color: inherit; text-decoration: none;
    margin-bottom: 6px;
  }
  .needs-strip.compact .needs-ask { font-size: 10px; margin-bottom: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .carousel { }
  .car-frame {
    position: relative; display: flex; align-items: center; justify-content: center;
    background: var(--bg); border-radius: 6px; overflow: hidden; min-height: 60px;
  }
  .car-img { display: block; max-width: 100%; max-height: 160px; width: 100%; object-fit: contain; }
  .carousel.compact .car-img { max-height: 70px; }
  .car-btn {
    position: absolute; top: 50%; transform: translateY(-50%); z-index: 2;
    border: none; background: rgba(0, 0, 0, .45); color: #fff; width: 26px; height: 26px;
    border-radius: 999px; font-size: 16px; line-height: 1; cursor: pointer; padding: 0;
  }
  .carousel.compact .car-btn { width: 18px; height: 18px; font-size: 12px; }
  .car-prev { left: 4px; } .car-next { right: 4px; }
  .car-chip {
    position: absolute; top: 4px; right: 4px; background: var(--working); color: #fff;
    font-size: 11px; font-weight: 700; padding: 1px 6px; border-radius: 999px;
  }
  .carousel.compact .car-chip { font-size: 9px; padding: 0 4px; }
  .car-caption { font-size: 12px; color: var(--dim); margin-top: 4px; }
  .carousel.compact .car-caption { font-size: 10px; }
  .car-counter { font-size: 11px; color: var(--dim); }
  /* The studio's own row gets the same strip in miniature, plus a chip list so a crowded
     row still names every card even where there is no room to draw its pictures. */
  .row-stuck {
    position: absolute; right: 2%; display: flex; flex-direction: column; align-items: flex-end;
    gap: 4px; max-width: 46%; pointer-events: auto;
  }
  .row-stuck-chips { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
  /* In the office the same strip is the in-tray on Steve's desk: a paper tray in front of
     it, chips grouped by board, the carousel inside. */
  .row-stuck.in-tray {
    right: auto; max-width: none; box-sizing: border-box; align-items: stretch; padding: 4px 6px;
    border: 1.5px solid var(--line); border-radius: 4px;
    background: color-mix(in srgb, var(--needs) 8%, var(--panel));
  }
  .row-stuck.in-tray .row-stuck-chips { justify-content: flex-start; align-items: center; }
  .row-stuck.in-tray .carousel { align-self: center; width: min(100%, 150px); }
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
  #peek-close {
    background: none; border: none; color: var(--dim); font-size: 20px; line-height: 1;
    cursor: pointer; padding: 0 4px;
  }
  #peek-lines {
    margin: 0; padding: 12px 14px; overflow-y: auto; flex: 1;
    font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    white-space: pre-wrap; word-break: break-word;
  }
  /* ---- The studio: an office the lanes walk around in (card #88; an office with rooms
     since October 9, 2026). The floor page above is untouched; this is a second view of
     the same data, toggled by one button. ---- */
  .view-toggle {
    font-size: 13px; font-weight: 600; padding: 6px 14px; border-radius: 999px;
    border: 1px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer;
  }
  .view-toggle:hover { border-color: var(--dim); }
  #studio { display: none; }
  #studio.open { display: block; }
  #studio-head { display: flex; justify-content: center; margin-bottom: 10px; }
  #room {
    position: relative; width: 100%; max-width: 960px; margin: 0 auto;
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px; overflow: hidden;
    /* Carpet. The height is the script's: as tall as its rooms need, never a fixed shape. */
    background-image: radial-gradient(color-mix(in srgb, var(--dim) 16%, transparent) 1px, transparent 1px);
    background-size: 14px 14px;
  }
  /* Rooms. Every board has one (a desk per lane, its counts on the whiteboard), and the
     building has two of its own: the break room, where an idle lane wanders and a lane
     asleep after a failure naps on the couch, and Steve's office, where a lane goes to wave
     when it needs him. Each has a door onto the hallway, and the hallway has the bulletin
     board. The first studio was rows, one per board, and read as the floor page drawn
     smaller (October 9, 2026: "this should look like an office with workers wandering
     around"). */
  .office-room {
    position: absolute; box-sizing: border-box; border: 2px solid var(--line);
    border-radius: 6px; background: var(--panel);
  }
  .office-room.team { background: color-mix(in srgb, var(--working) 5%, var(--panel)); }
  .office-room.break { background: color-mix(in srgb, var(--paused) 8%, var(--panel)); }
  .office-room.boss { background: color-mix(in srgb, var(--needs) 5%, var(--panel)); }
  .room-name {
    position: absolute; top: 3px; left: 8px; font-size: 11px; font-weight: 700;
    color: var(--ink); text-decoration: none; white-space: nowrap;
  }
  a.room-name:hover { text-decoration: underline; }
  .room-door {
    /* A gap in the wall on the hallway side: a mat the color of the hall. */
    position: absolute; width: 24px; height: 8px; border-radius: 2px;
    background: color-mix(in srgb, var(--dim) 40%, var(--panel));
  }
  .prop { position: absolute; pointer-events: none; }
  .prop svg { display: block; }
  @keyframes steam { 0% { opacity: 0; transform: translateY(0); } 50% { opacity: .8; }
                     100% { opacity: 0; transform: translateY(-5px); } }
  .prop .steam { animation: steam 2s infinite; }
  .whiteboard {
    position: absolute; top: 4px; right: 6px; font-size: 9px; color: var(--dim); font-weight: 600;
    background: var(--panel); border: 1.5px solid var(--line); border-radius: 2px; padding: 2px 5px;
    white-space: nowrap; max-width: calc(100% - 90px); overflow: hidden; text-overflow: ellipsis;
  }
  .couch {
    position: absolute; height: 22px; border-radius: 8px 8px 4px 4px;
    background: color-mix(in srgb, var(--sleep) 30%, var(--panel)); border: 1.5px solid var(--line);
  }
  .couch::before {
    content: ""; position: absolute; left: 8%; right: 8%; top: 6px; height: 0;
    border-top: 1.5px dashed color-mix(in srgb, var(--line) 70%, transparent);
  }
  .hall-label {
    position: absolute; font-size: 9px; color: var(--dim); font-weight: 700;
    text-transform: uppercase; letter-spacing: .06em; white-space: nowrap;
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
  /* An empty desk still says whose it is, so a room with everyone out still reads. */
  .desk-tag {
    position: absolute; transform: translate(-50%, 2px); font-size: 9px; font-weight: 600;
    color: var(--dim); opacity: .75; white-space: nowrap; line-height: 1.2;
  }
  /* The bulletin board in the hallway: cork, a pin in every card, every card whole. It
     scrolls sideways on a narrow screen rather than clipping a card in half. */
  .pin-wall {
    position: absolute; box-sizing: border-box; display: flex; gap: 8px; align-items: center;
    overflow-x: auto; overflow-y: hidden; padding: 4px 8px; scrollbar-width: none;
    background: color-mix(in srgb, var(--paused) 14%, var(--panel)); border-radius: 4px;
    border: 2px solid color-mix(in srgb, var(--paused) 35%, var(--line));
  }
  .pin-wall::-webkit-scrollbar { display: none; }
  /* More cards than fit: the strip scrolls sideways, and its right edge fades to say so,
     instead of showing a card cut in half. */
  .pin-wall.scrolls { mask-image: linear-gradient(to right, #000 88%, transparent); -webkit-mask-image: linear-gradient(to right, #000 88%, transparent); }
  .pin-card {
    position: relative; flex: 0 0 auto; font-size: 10px; line-height: 1.3; background: var(--panel);
    border: 1px solid var(--line); border-radius: 2px; padding: 4px 6px; text-decoration: none;
    color: inherit; box-shadow: 0 1px 2px rgba(0, 0, 0, .18); display: block; white-space: nowrap;
    transform: rotate(-1.2deg);
  }
  .pin-card:nth-child(even) { transform: rotate(1.4deg); }
  .pin-card::before {
    content: ""; position: absolute; top: -3px; left: 50%; width: 6px; height: 6px;
    border-radius: 50%; background: var(--needs); transform: translateX(-50%);
    box-shadow: 0 1px 1px rgba(0, 0, 0, .3);
  }
  .pin-card:nth-child(3n)::before { background: #58a6ff; }
  .pin-card:nth-child(3n+1)::before { background: var(--working); }
  .pin-card b { font-weight: 600; }
  #room-rooms, #room-desks, #room-characters, #room-stuck { position: absolute; inset: 0; pointer-events: none; }
  #room-rooms .room-name { pointer-events: auto; }
  .character {
    /* As wide as its own plate (not a fixed 30px that the plate overflowed to the right),
       so translate(-50%) centers the figure and the words together over the spot. Never
       wider than a standing spot, so two neighbours never touch. */
    position: absolute; width: max-content; max-width: 70px; transform: translate(-50%, -100%);
    transition: left 1s ease, top 1s ease; cursor: pointer; text-align: center;
    pointer-events: auto;
  }
  @keyframes character-wave { 0%, 100% { transform: rotate(-8deg); } 50% { transform: rotate(8deg); } }
  .character-wave { animation: character-wave .6s ease-in-out infinite; }
  /* Walking: a bob for as long as the position transition runs. */
  @keyframes walk-bob { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-2px); } }
  .character.walking svg { animation: walk-bob .32s ease-in-out infinite; }
  @media (prefers-reduced-motion: reduce) {
    .character { transition: none; }
    .character-wave, .character.walking svg, .prop .steam { animation: none; }
  }
  .character svg { width: 30px; height: 34px; display: block; margin: 0 auto; }
  .character.ship svg { width: 40px; }
  /* Held up over a waving character's head — the same mockup the floor's own desk shows
     (card #91), shrunk to fit the room. */
  .character-mockup { margin-bottom: 2px; }
  .character-mockup .mockup-thumb {
    display: block; margin: 0 auto; max-width: 46px; max-height: 34px; border-radius: 3px;
  }
  .character .plate {
    font-size: 9px; color: var(--ink); font-weight: 600; white-space: nowrap; margin-top: 2px;
    line-height: 1.2; overflow: hidden; text-overflow: ellipsis;
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
  <div class="muted" id="github-budget"></div>
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
    <div class="pin-wall" id="pin-wall"></div>
    <div id="beacon" class="beacon-green"></div>
    <div id="room-desks"></div>
    <div id="room-stuck"></div>
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

// The Needs Steve carousel (card #96): every picture a Stuck card's Needs Steve comments
// have shown, newest round first, lettered by direction when the comment named one —
// shared by the floor's per-project strip and the studio's per-row strip, so the two
// views never disagree about what a card's carousel holds. The slide a viewer left a
// card on persists across refreshes (keyed by card number, not re-read from the server),
// so a poll mid-flip never snaps it back to the first picture.
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

function carouselHTML(entry, compact) {
  const {slides, idx, slide} = currentSlide(entry);
  if (!slide) return "";
  const n = entry.card.number;
  const chip = slide.letter
    ? `<span class="car-chip">${esc(slide.letter)}${slide.recommended ? " ★" : ""}</span>` : "";
  const cap = [slide.label, slide.dated ? relTime(slide.when) : ""].filter(Boolean).map(esc).join(" · ");
  const arrows = slides.length > 1
    ? `<button class="car-btn car-prev" type="button" data-car-prev="${n}" aria-label="Previous picture">&lsaquo;</button>
       <button class="car-btn car-next" type="button" data-car-next="${n}" aria-label="Next picture">&rsaquo;</button>`
    : "";
  return `<div class="carousel${compact ? " compact" : ""}" data-carousel="${n}" tabindex="0">
    <div class="car-frame">
      ${arrows}
      <img class="car-img" src="${esc(slide.url)}" alt="${esc(slide.label || "Mockup to approve")}"
           loading="lazy" onerror="this.style.display='none'">
      ${chip}
    </div>
    <div class="car-caption">${cap}</div>
    <div class="car-counter">${idx + 1} of ${slides.length}</div>
  </div>`;
}

// Every card Stuck on this board, whichever lane last touched it (card #96's "why
// nothing shows today": a lane's own desk only ever shows the one card it last worked,
// so a design lane that parked three cards and moved on to a fourth showed none of the
// three). `compact` is the studio's own miniature version of the same strip.
function stuckStripHTML(stuck, compact) {
  if (!stuck || !stuck.length) return "";
  return `<div class="needs-strip${compact ? " compact" : ""}">${stuck.map(s => `
    <div class="needs-card">
      <a class="needs-ask" href="${esc(s.card.url)}" target="_blank" rel="noopener">#${s.card.number}: ${esc(s.ask) || "needs a decision"}</a>
      ${carouselHTML(s, compact)}
    </div>`).join("")}</div>`;
}

function stepCarousel(cardNum, dir) {
  carouselSlide[cardNum] = (carouselSlide[cardNum] || 0) + dir;
  if (lastState) { render(lastState); renderStudio(lastState); }
  // render()/renderStudio() replace the carousel's own DOM node (a fresh innerHTML each
  // time), so a re-render drops focus to <body> and every arrow key after the first one
  // stops doing anything — the strip exists twice (floor and studio), and only the
  // currently-visible copy can actually take focus back.
  for (const el of document.querySelectorAll(`[data-carousel="${cardNum}"]`)) {
    el.focus({preventScroll: true});
    if (document.activeElement === el) break;
  }
}

document.addEventListener("click", (e) => {
  const prev = e.target.closest("[data-car-prev]"), next = e.target.closest("[data-car-next]");
  if (!prev && !next) return;
  e.preventDefault(); e.stopPropagation();
  const btn = prev || next;
  stepCarousel(Number(btn.dataset.carPrev || btn.dataset.carNext), prev ? -1 : 1);
});
// Arrow keys and a swipe, for a carousel that has focus (phone-first: `tabindex` above
// makes a tap on the frame itself focus it, no separate "activate" step needed).
document.addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const el = document.activeElement && document.activeElement.closest("[data-carousel]");
  if (!el) return;
  e.preventDefault();
  stepCarousel(Number(el.dataset.carousel), e.key === "ArrowLeft" ? -1 : 1);
});
let swipeStartX = null, swipeCard = null;
document.addEventListener("touchstart", (e) => {
  const el = e.target.closest("[data-carousel]");
  if (!el) { swipeCard = null; return; }
  swipeCard = Number(el.dataset.carousel);
  swipeStartX = e.changedTouches[0].clientX;
}, {passive: true});
document.addEventListener("touchend", (e) => {
  if (swipeCard == null || swipeStartX == null) return;
  const dx = e.changedTouches[0].clientX - swipeStartX;
  if (Math.abs(dx) > 40) stepCarousel(swipeCard, dx < 0 ? 1 : -1);
  swipeCard = swipeStartX = null;
}, {passive: true});

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

let lastState = null;

function render(state) {
  lastState = state;
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

// ---- The studio: an office the lanes walk around in (card #88) -----------------------
// A second view of the same data `render()` already has: no new endpoint, no new field.
// Toggled by one button; `render()` above is untouched, `refresh()` just hands it the
// same state it was already given.
//
// Rooms, not rows. Every board has a room of its own: a desk per lane, a counter for its
// triage lane, a shelf for its ship station, and its counts on the whiteboard. A lane that
// is working or paused is at its desk. An idle lane leaves for the break room and mills
// about there — every so often one gets up and walks to another spot. A lane asleep after
// a failure naps on the break room couch. A lane that needs Steve walks to his office and
// waves in front of his desk, mockup held up, and the light over his door turns red. The
// hallway between has the bulletin board: the last cards worked. (The first studio was
// rows, one per board, and read as the floor page drawn smaller — October 9, 2026: "this
// should look like an office with workers wandering around".)

function showStudio() {
  document.getElementById("floor-view").style.display = "none";
  document.getElementById("studio").classList.add("open");
  document.getElementById("studio-btn").style.display = "none";
  // Drawn hidden, the room had no width to lay itself out by; draw it again now it has one.
  if (lastState) renderStudio(lastState);
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
  // Idle: in the break room with a mug, waiting for a card.
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
  window: `<svg width="34" height="24" viewBox="0 0 34 24"><rect x="1" y="1" width="32" height="22" rx="2" fill="#9ecbff" opacity=".55" stroke="var(--line)" stroke-width="2"/>
    <path d="M17 1 v22 M1 12 h32" stroke="var(--line)" stroke-width="2"/><circle cx="26" cy="7" r="3" fill="#ffd33d" opacity=".9"/></svg>`,
  picture: `<svg width="18" height="14" viewBox="0 0 18 14"><rect x="1" y="1" width="16" height="12" fill="#f0c8a0" stroke="#8a5a2b" stroke-width="2"/>
    <path d="M3 11 l4 -5 l3 3 l2 -2 l3 4z" fill="#3fb950"/></svg>`,
  // Steve's desk: the chair behind it, a monitor, a mug, and the nameplate.
  bossdesk: `<svg width="100" height="34" viewBox="0 0 100 34"><rect x="36" y="0" width="28" height="12" rx="4" fill="var(--dim)"/>
    <rect x="0" y="12" width="100" height="18" rx="3" fill="#8a5a2b"/><rect x="0" y="12" width="100" height="4" rx="2" fill="#a86f3a"/>
    <rect x="8" y="17" width="13" height="9" rx="1" fill="var(--line)"/><rect x="26" y="21" width="6" height="5" rx="1" fill="#f3f4f6"/>
    <rect x="68" y="18" width="26" height="9" rx="1" fill="#f3f4f6"/>
    <text x="81" y="25" text-anchor="middle" font-size="6.5" font-weight="700" fill="#1b1f24" font-family="sans-serif">STEVE</text></svg>`,
};
function prop(name, x, y) {
  return `<div class="prop" style="left:${x}px;top:${y}px">${PROPS[name]}</div>`;
}

// Where things stand. The whole office is laid out in pixels from the room's own width, so
// a figure never shares a spot with another and a room is as tall as the people in it: a
// break room with six idle lanes grows a second row rather than standing them on each
// other (the first studio sent every idle lane to five fixed spots, and on a box with six
// idle lanes the sixth stood on the first). Across is a percentage, down is pixels, and a
// character's move between the two is one CSS transition — a walk.
const CHAR_W = 72;                 // one standing spot: a figure and its plate never exceed it (.character max-width)
const EDGE = 10, GAP = 8, HALL_H = 64, ROOM_PAD = 6;
// padTop is the first row's feet, measured from the room's top: room name, props on the
// back wall and the figure's own height (34px; plus a held-up mockup in Steve's office;
// plus a desk and monitor in a team room) all fit above it. rowH is feet to feet.
// padBottom is the plate under the last row's feet.
const BREAK = {padTop: 86, rowH: 64, padBottom: 28};
const BOSS = {padTop: 138, rowH: 100, padBottom: 28};
const BOSS_EMPTY_H = 72;           // nobody waiting: just his desk
const TRAY_CHIP_H = 34, TRAY_PIC_H = 146;   // the in-tray: chips alone, or chips and a compact carousel
const TEAM = {padTop: 80, rowH: 68, padBottom: 20};
const BREAK_EXTRA = 2;             // free spots in the break room, so a wanderer has somewhere to go

function roomWidthPx() {
  // Hidden (every 30s poll draws the studio while the floor is showing), the room has no
  // width; use the one it will have, and `showStudio` draws again with the real one.
  const el = document.getElementById("room");
  return el.clientWidth || Math.min(960, Math.max(280, window.innerWidth - 34));
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
function wander() {
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

function computeStudio(state) {
  const W = roomWidthPx(), inner = W - 2 * EDGE;
  const projects = state.projects || [];
  const lanes = [];
  projects.forEach(p => (p.lanes || []).forEach(l => lanes.push({...l, project: p.name})));
  const breakers = lanes.filter(l => l.pill === "idle").sort((a, b) => a.lane.localeCompare(b.lane));
  const sleepers = lanes.filter(l => l.pill === "sleeping");
  const needs = lanes.filter(l => l.pill === "needs_steve");
  const rooms = [];

  // The top band: the break room and Steve's office, side by side and as tall as the
  // fuller — or, on a phone, the break room over the office, each its own height.
  const narrow = inner < 420;
  const breakW = narrow ? inner : Math.round(inner * 0.58) - GAP / 2;
  const bossW = narrow ? inner : inner - breakW - GAP;
  const nBreak = breakers.length + sleepers.length + BREAK_EXTRA;
  const breakH = roomHeight(nBreak, breakW, BREAK);
  // The in-tray sits in front of his desk, between it and anyone waiting: as tall as a
  // row of chips, or the chips and the compact carousel when a card has a picture.
  const stuck = projects.flatMap(p => p.stuck || []);
  // Chips wrap on a narrow tray: one more line of height per extra row of them.
  const chipRows = Math.max(1, Math.ceil((stuck.length * 46 + projects.filter(p => (p.stuck || []).length).length * 40 + 48)
                                          / Math.max(80, bossW - 2 * ROOM_PAD - 16)));
  const trayH = !stuck.length ? 0
    : (stuck.some(s => stuckSlides(s).length) ? TRAY_PIC_H : TRAY_CHIP_H) + (chipRows - 1) * 18;
  const bossSpec = {...BOSS, padTop: BOSS.padTop + trayH};
  const bossH = needs.length ? roomHeight(needs.length, bossW, bossSpec) : BOSS_EMPTY_H + trayH + (trayH ? 14 : 0);
  const bandH = Math.max(breakH, bossH);
  const breakRoom = {kind: "break", name: "Break room", x: EDGE, y: EDGE, w: breakW,
                     h: narrow ? breakH : bandH, door: narrow ? "left" : "bottom"};
  const boss = {kind: "boss", name: "Steve's office", w: bossW, door: "bottom",
                x: narrow ? EDGE : EDGE + breakW + GAP, y: narrow ? EDGE + breakH + GAP : EDGE,
                h: narrow ? bossH : bandH, spec: bossSpec,
                tray: trayH ? {x: ROOM_PAD + 2, y: BOSS_EMPTY_H - 8, w: bossW - 2 * ROOM_PAD - 4, h: trayH} : null};
  rooms.push(breakRoom, boss);
  const hall = {x: EDGE, y: boss.y + boss.h + GAP, w: inner, h: HALL_H};

  // The team rooms: up to three across, one per board, each as tall as its own desks.
  const cols = Math.max(1, Math.min(3, Math.floor(inner / 300), projects.length || 1));
  const teamW = (inner - (cols - 1) * GAP) / cols;
  let y = hall.y + HALL_H + GAP, rowH = 0;
  const teams = projects.map((p, i) => {
    const col = i % cols;
    if (col === 0 && i > 0) { y += rowH + GAP; rowH = 0; }
    const n = (p.lanes || []).length + 1;      // the ship shelf is the last station
    const h = roomHeight(n, teamW, TEAM);
    rowH = Math.max(rowH, h);
    const r = {kind: "team", name: p.name, board_url: p.board_url, counts: p.counts || {},
               x: EDGE + col * (teamW + GAP), y, w: teamW, h, project: p};
    rooms.push(r);
    return r;
  });
  const total = (teams.length ? y + rowH : hall.y + HALL_H) + EDGE;

  // Spots. A lane's home is its desk; where it stands now depends on its pill.
  const ships = [];
  const pct = x => x / W * 100;
  for (const r of teams) {
    const ordered = [...(r.project.lanes || [])].sort(
      (a, b) => (a.kind === "Triage" ? 0 : 1) - (b.kind === "Triage" ? 0 : 1));
    const {spots} = gridSpots(r, ordered.length + 1, TEAM);
    ordered.forEach((l, i) => {
      const me = lanes.find(x => x.lane === l.lane);
      me.home = {x: pct(spots[i].x), y: spots[i].y};
      me.pos = me.home; me.away = false; me.room = r.name;
    });
    const s = spots[ordered.length];
    ships.push({name: r.name, ship: r.project.ship, pos: {x: pct(s.x), y: s.y}});
  }
  const bg = gridSpots(breakRoom, nBreak, BREAK);
  const {seatOf, couch} = seatBreakRoom(breakers, sleepers, bg.spots, bg.perRow, bg.rows);
  for (const l of [...breakers, ...sleepers]) {
    const s = bg.spots[seatOf[l.lane]];
    l.pos = {x: pct(s.x), y: s.y}; l.away = true; l.room = breakRoom.name;
  }
  if (couch.length) {
    const first = bg.spots[couch[0]], last = bg.spots[couch[couch.length - 1]];
    breakRoom.couch = {x: first.x - CHAR_W / 2 + 6 - breakRoom.x, w: last.x - first.x + CHAR_W - 12,
                       y: first.y - 24 - breakRoom.y};
  }
  const vg = gridSpots(boss, needs.length, boss.spec);
  needs.forEach((l, i) => {
    const s = vg.spots[i];
    l.pos = {x: pct(s.x), y: s.y}; l.away = true; l.room = boss.name;
  });
  return {lanes, ships, rooms, hall, boss, total};
}

function roomHTML(r) {
  const name = r.kind === "team"
    ? `<a class="room-name" href="${esc(r.board_url)}" target="_blank" rel="noopener">${esc(r.name)}</a>`
    : `<span class="room-name">${esc(r.name)}</span>`;
  // The door is a gap in the wall on the hallway side.
  const door = r.kind === "team"
    ? `<div class="room-door" style="left:${Math.round(r.w / 2 - 12)}px;top:-2px"></div>`
    : r.door === "left"
      ? `<div class="room-door" style="left:-2px;top:${Math.round(r.h - 40)}px;width:8px;height:24px"></div>`
      : `<div class="room-door" style="left:${Math.round(r.w / 2 - 12)}px;bottom:-2px"></div>`;
  let props = "";
  if (r.kind === "break") {
    props = prop("coffee", 84, 10) + prop("vending", 116, 6) + prop("cooler", 146, 8) + prop("plant", r.w - 30, 10)
      + (r.w > 260 ? prop("window", r.w - 76, 8) : "")
      + (r.couch ? `<div class="couch" style="left:${r.couch.x}px;top:${r.couch.y}px;width:${r.couch.w}px"></div>` : "");
  } else if (r.kind === "boss") {
    props = prop("bossdesk", Math.round(r.w / 2 - 50), 22) + prop("picture", r.w - 26, 6)
      + (r.w > 220 ? prop("window", r.w - 70, 6) : "") + prop("plant", 8, 20);
  } else {
    const c = r.counts;
    props = `<div class="whiteboard" title="The board right now">Ready ${c.ready ?? "?"} · Doing ${c.in_progress ?? "?"} · Backlog ${c.backlog ?? "?"}</div>`
      + prop("plant", 6, 20);
  }
  return `<div class="office-room ${r.kind}" data-room="${esc(r.name)}"
    style="left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px">${name}${props}${door}</div>`;
}

function renderRooms({rooms, hall, boss, total}) {
  const room = document.getElementById("room");
  room.style.height = total + "px";
  document.getElementById("room-rooms").innerHTML = rooms.map(roomHTML).join("")
    + `<div class="hall-label" style="left:${hall.x + 4}px;top:${hall.y + 2}px">Bulletin board — the last cards worked</div>`;
  const wall = document.getElementById("pin-wall");
  wall.style.left = hall.x + "px"; wall.style.top = (hall.y + 14) + "px";
  wall.style.width = hall.w + "px"; wall.style.height = (hall.h - 16) + "px";
  const beacon = document.getElementById("beacon");
  beacon.style.left = (boss.x + boss.w / 2) + "px";
  beacon.style.top = (boss.y + boss.h - 14) + "px";
}

function renderRoomDesks(lanes) {
  const furniture = lanes.map(l => (l.kind === "Triage"
    ? `<div class="dispatch-counter" style="left:${l.home.x}%;top:${l.home.y}px"></div>`
    : `<div class="desk-furniture" style="left:${l.home.x}%;top:${l.home.y}px"></div>
       <div class="desk-monitor${l.pill === "working" ? " lit" : ""}" data-lane="${esc(l.lane)}"
            style="left:${l.home.x}%;top:${l.home.y}px"><div class="scanline"></div></div>`)
    + (l.away ? `<div class="desk-tag" data-lane="${esc(l.lane)}" style="left:${l.home.x}%;top:${l.home.y}px">${esc(l.kind)} · out</div>` : ""));
  document.getElementById("room-desks").innerHTML = furniture.join("");
}

// The plate under a character: what it is (Build, Design, Triage), and — only when it has
// left its room for the break room or Steve's office — whose it is.
function plateHTML(kind, project, away) {
  return `<div class="plate">${esc(kind)}${away ? `<br><span class="where">${esc(project)}</span>` : ""}</div>`;
}

// Characters keep the same DOM node across renders, so moving `left`/`top` is a CSS
// transition — a walk — rather than a jump cut (`prefers-reduced-motion` drops it to a
// jump cut on purpose). While the walk runs the figure bobs.
// The mockup held up over a waving character's head — the carousel's own current slide
// when this card has one (card #96: so flipping it in Steve's in-tray flips it here
// too), the old single thumbnail otherwise (a demo fixture with no `rounds` of its own).
function heldMockupHTML(l, stuckByCard) {
  const entry = l.card && stuckByCard && stuckByCard[l.card.number];
  if (entry) {
    const {slide} = currentSlide(entry);
    if (slide) {
      return `<div class="character-mockup"><img class="mockup-thumb" src="${esc(slide.url)}"
        alt="${esc(slide.label || "Mockup to approve")}" loading="lazy" onerror="this.style.display='none'"></div>`;
    }
    return "";
  }
  return mockupThumbsHTML((l.images || []).slice(0, 1), "character-mockup");
}

const charEls = {};
function newCharacter(container, cls) {
  const el = document.createElement("div");
  el.className = cls;
  el.addEventListener("transitionrun", () => el.classList.add("walking"));
  el.addEventListener("transitionend", () => el.classList.remove("walking"));
  el.addEventListener("transitioncancel", () => el.classList.remove("walking"));
  container.appendChild(el);
  return el;
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
    // Held up over the waving character, the same mockup the floor's own desk shows — the
    // room Steve walks instead of the card he'd otherwise click through to (card #91).
    const held = l.pill === "needs_steve" ? heldMockupHTML(l, stuckByCard) : "";
    el.innerHTML = held + characterSVG(l.pill, laneLook(l.lane)) + plateHTML(l.kind, l.project, l.away);
    el.style.left = l.pos.x + "%";
    el.style.top = l.pos.y + "px";
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
    el.innerHTML = shipSVG(st.mode, st.pill) + plateHTML("Ship", s.name, false);
    el.style.left = s.pos.x + "%";
    el.style.top = s.pos.y + "px";
  }
  for (const name of Object.keys(shipEls)) if (!seen.has(name)) { shipEls[name].remove(); delete shipEls[name]; }
}

function renderPinWall(recent) {
  // Newest first: `state.recent` already sorts newest-first, and the board runs left to
  // right in that same order. Each card names the issue and the project it is on.
  const wall = document.getElementById("pin-wall");
  wall.innerHTML = (recent || []).slice(0, 8).map(r => `
    <a class="pin-card" href="${esc(r.card.url)}" target="_blank" rel="noopener"><b>#${r.card.number}</b>
      · ${esc(r.project)}<br>${relTime(r.when)}</a>`).join("");
  markWallScroll();
}
// Whether the board overflows is only measurable while the studio is showing (a hidden
// strip is 0 wide), so this runs again the moment it opens, not just on each redraw.
function markWallScroll() {
  const wall = document.getElementById("pin-wall");
  wall.classList.toggle("scrolls", wall.scrollWidth > wall.clientWidth + 1);
}

// The in-tray on Steve's desk (card #96: "in each board's row, on the floor page and in
// the studio" — in the office, the cards waiting on him are on his desk): every Stuck
// card's chip, always, grouped by board, and one carousel for whichever of them actually
// has a picture to show (a direction linked only as a `.html` mockup has none yet, and
// still gets its chip with no frame beside it).
function renderRoomStuck(projects, boss) {
  const groups = [];
  for (const p of projects || []) if ((p.stuck || []).length) groups.push(p);
  if (!groups.length || !boss.tray) { document.getElementById("room-stuck").innerHTML = ""; return; }
  const chips = groups.map(p => `<span class="tray-board">${esc(p.name)}</span>` + p.stuck.map(s =>
    `<a class="row-stuck-chip" href="${esc(s.card.url)}" target="_blank" rel="noopener">#${s.card.number}</a>`
  ).join("")).join("");
  const withPics = groups.flatMap(p => p.stuck).filter(s => stuckSlides(s).length);
  const pic = withPics.length ? carouselHTML(withPics[0], true) : "";
  document.getElementById("room-stuck").innerHTML = `<div class="row-stuck in-tray"
      style="left:${boss.x + boss.tray.x}px;top:${boss.y + boss.tray.y}px;width:${boss.tray.w}px">
    <div class="row-stuck-chips"><span class="tray-label">In-tray</span>${chips}</div>
    ${pic}
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
      for (const l of p.lanes || []) if (l.pill === "paused" || l.pill === "sleeping") amber = true;
      if (shipState(p.ship).pill === "blocked") amber = true;
    }
    color = amber ? "amber" : "green";
  }
  document.getElementById("beacon").className = "beacon-" + color;
}

function renderStudio(state) {
  lastState = state;
  const office = computeStudio(state);
  renderRooms(office);
  const stuckByCard = {};
  for (const p of state.projects || []) for (const s of p.stuck || []) stuckByCard[s.card.number] = s;
  renderRoomDesks(office.lanes);
  renderRoomStuck(state.projects, office.boss);
  renderCharacters(office.lanes, stuckByCard);
  renderShipDock(office.ships);
  renderPinWall(state.recent);
  renderBeacon(state);
}
// The office is laid out from its own width, so a turned phone or a resized window lays
// it out again.
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (lastState) renderStudio(lastState); }, 150);
});

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
  return [
    {header: {box: "demo", time: now, needs_steve_total: 0},
     projects: [proj("alpha", [L("alpha", "Build", "working", {card: {number: 10, url: "#"}, stage: "Building", minutes: 4}),
                               L("alpha-design", "Design", "idle", {reason: "Nothing to pick up right now."}),
                               L("alpha-triage", "Triage", "idle", {reason: "Nothing to pick up right now."})],
                "passing"),
                proj("beta", [L("beta", "Build", "idle", {reason: "Nothing to pick up right now."})], "passing")],
     recent: [{project: "alpha", lane: "alpha", card: {number: 10, url: "#"}, when: now, quota: "~$0.40 equivalent (subscription — not billed)"}]},
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
                            images_fn=board_images, rounds_fn=board_rounds,
                            ci_fetcher=fetch_ci, deploy_repo=deploy_repo,
                            deploy_fn=lambda: deploy_status(REPO_ROOT, deploy_state, deploy_repo),
                            ratelimit_fn=board_ratelimit)

    def build_lanes():
        # Peek's own lookup, kept separate from `build()`'s state so a lane's worktree
        # layout is never one of the fields `/api/state.json` serves (ALLOWED_FIELDS).
        return {l["lane"]: l for l in discover_lanes(projects_dir, state_root)}

    # idle_after_sec: once nobody has loaded the page for this long, the background loop
    # stops spending `fetch_board`'s GraphQL reads on a floor nobody is watching (card
    # #98) — `lane_cache` reads only the local filesystem, nothing GraphQL, so it keeps
    # refreshing on a plain timer.
    idle_after_sec = max(refresh_sec * 5, 300)
    cache = Cache(build, refresh_sec, idle_after_sec=idle_after_sec)
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
