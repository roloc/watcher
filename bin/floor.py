#!/usr/bin/env python3
"""A live, read-only page for the agents box: what every lane is doing right now, without
ssh or journalctl (card #40 — mocked up with Steve on September 28, 2026).

    python3 bin/floor.py [--host 0.0.0.0] [--port 8787]
                          [--projects-dir DIR] [--state-root DIR] [--refresh-sec N]

One page for the whole box, not one per project: it groups every lane in `projects/*.env`
by the board it works, and for each lane shows one of exactly five states — **working**
(a card, its Stage, minutes since launch), **idle** (why), **needs Steve** (the card and
the first line of its ask), **paused** (a `PAUSE` file), or **sleeping** (a died tick,
inside the spawner's own ten-minute retry sleep — `how-it-works.md`, "step 8"). A "Recent
ticks" list, across every project, closes the loop: project, lane, card, when, quota.

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

Read-only, and nothing but: no endpoint here can change a card, a `PAUSE` file, or
anything else. The JSON endpoint (`/api/state.json`) serves exactly the fields in
`ALLOWED_FIELDS` below — never a raw file, an env value, a log's `result` text, or stderr.

`bin/test-floor.py` is the offline suite: fixture state directories and a stubbed board,
no network, no real systemd. Run it after touching this file.
"""
import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import metrics  # noqa: E402  (bin/metrics.py, beside this file — reused for the log parser)

STUCK_STATUS = "Stuck - Needs help"  # board.py's own constant; kept as a literal here so
                                      # this file never imports board.py (which reads
                                      # BOARD_OWNER/BOARD_NUMBER as module-level globals —
                                      # wrong for a page that talks to several boards).
SLEEP_AFTER_FAILURE_MIN = 10  # spawn.sh: `wsleep 600` after any non-success exit
RECENT_PER_LANE = 20   # ticks kept per lane before the global list is trimmed
RECENT_TOTAL = 15      # ticks shown on the page, across every project

ENV_VARS = ("PROJECT_NAME", "PROJECT_ROOT", "BOARD_OWNER", "PROJECT_REPO", "BOARD_NUMBER",
            "LANE_AGENTS", "LANE_COMMAND", "WATCHER_STATE")

# Every key the JSON endpoint may ever emit. `test-floor.py` walks a real response and
# fails if it finds one outside this set — the rule from the card ("serves only the fields
# above") checked in code, not just read.
ALLOWED_FIELDS = {
    "header", "box", "time", "needs_steve_total",
    "projects", "name", "board_url", "counts", "ready", "in_progress", "backlog",
    "lanes", "board_error",
    "lane", "kind", "unit", "unit_active", "pill", "link", "card", "stage", "minutes",
    "reason", "ask", "number", "url",
    "recent", "project", "when", "quota",
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
        lanes.append({
            "lane": name,
            "unit": f"watcher@{name}",
            "project_repo": env["PROJECT_REPO"],
            "board_owner": env["BOARD_OWNER"],
            "board_number": board_number,
            "lane_agents": env.get("LANE_AGENTS") or "",
            "lane_command": env.get("LANE_COMMAND") or "/watch",
            "state_dir": env.get("WATCHER_STATE") or os.path.join(state_root, name),
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
    anything (`docs/talking-to-steve.md`, rule 7). Best-effort, not a re-implementation of
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


def lane_desk(lane, items_by_number, board_url, now, last_tick=None, unit_active=None, ask=None):
    """One lane's pill: exactly one of working | idle | needs_steve | paused | sleeping.
    `last_tick` is the lane's own newest log entry (a `metrics.Tick`, or None — loaded
    once by the caller, not re-read here: a lane's own `Cache` refresh used to parse every
    log file twice, once for this and once for the recent-ticks list). `ask` is a callable
    `(issue, project_repo) -> str` (board.py's `needs_steve_ask`, injected so tests never
    touch the network); `unit_active` is `systemctl is-active`'s answer, already looked up
    by the caller (None when it could not be checked)."""
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
            return {**base, "pill": "needs_steve", "link": url,
                    "card": {"number": last_tick.issue, "url": url}, "ask": text or ""}

    if last_tick is not None and last_tick.died:
        age = _minutes_since(last_tick.mtime, now)
        if age < SLEEP_AFTER_FAILURE_MIN:
            return {**base, "pill": "sleeping", "link": board_url,
                    "reason": f"Card #{last_tick.issue} did not finish; trying again shortly."}

    return {**base, "pill": "idle", "link": board_url,
            "reason": _idle_reason(lane, items_by_number, last_tick)}


def _fmt_quota(cost):
    if cost is None:
        return "unknown"
    return f"~${cost:.2f} equivalent (subscription — not billed)"


# --- assembling the page's data -----------------------------------------------------------

def build_state(projects_dir, state_root, board_fetcher, unit_active_fn=None, ask_fn=None,
                 now=None, box_name=None):
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
        for lane in p["lanes"]:
            ticks = metrics.load_lane(lane["state_dir"])  # one parse of this lane's logs
            active = unit_active_fn(lane["unit"]) if unit_active_fn else None
            desk = lane_desk(lane, items_by_number, p["board_url"], now,
                              last_tick=ticks[-1] if ticks else None,
                              unit_active=active, ask=ask_fn)
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

        entry = {"name": p["name"], "board_url": p["board_url"],
                  "counts": project_counts(items), "lanes": lane_out}
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
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-weight: 600; }
  a { color: inherit; }
</style>
</head>
<body>
<header>
  <h1 id="box">The floor</h1>
  <div class="muted" id="clock"></div>
</header>
<div id="projects"></div>
<section class="project">
  <h2>Recent ticks</h2>
  <table id="recent"><thead><tr><th>Project</th><th>Lane</th><th>Card</th><th>When</th><th>Quota</th></tr></thead><tbody></tbody></table>
</section>
<script>
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

function deskHTML(d) {
  let detail = "";
  if (d.pill === "working") detail = `#${d.card.number} · ${d.stage ? esc(d.stage) : "—"} · ${d.minutes}m`;
  else if (d.pill === "needs_steve") detail = `#${d.card.number}: ${esc(d.ask) || "needs a decision"}`;
  else if (d.pill === "idle" || d.pill === "sleeping") detail = esc(d.reason || "");
  return `<a class="desk" href="${esc(d.link)}">
    <div class="name">${esc(d.kind)}</div>
    <div class="unit">${esc(d.unit)}</div>
    <span class="pill ${esc(d.pill)}">${PILL_LABEL[d.pill] || esc(d.pill)}</span>
    <div class="detail">${detail}</div>
  </a>`;
}

function render(state) {
  document.getElementById("box").textContent =
    "The floor — " + state.header.box +
    (state.header.needs_steve_total > 0 ? ` — ${state.header.needs_steve_total} need Steve` : "");
  document.getElementById("clock").textContent = new Date(state.header.time).toLocaleString();

  const projects = document.getElementById("projects");
  projects.innerHTML = state.projects.map(p => `
    <section class="project">
      <h2><a href="${esc(p.board_url)}">${esc(p.name)}</a></h2>
      <div class="counts">Ready ${p.counts.ready} · In progress ${p.counts.in_progress} · Backlog ${p.counts.backlog}
        ${p.board_error ? `<span class="alert"> — board unreachable</span>` : ""}</div>
      <div class="desks">${p.lanes.map(deskHTML).join("")}</div>
    </section>`).join("");

  const tbody = document.querySelector("#recent tbody");
  tbody.innerHTML = state.recent.map(r => `
    <tr><td>${esc(r.project)}</td><td>${esc(r.lane)}</td>
      <td><a href="${esc(r.card.url)}">#${r.card.number}</a></td>
      <td>${relTime(r.when)}</td><td>${esc(r.quota)}</td></tr>`).join("");
}

async function refresh() {
  try {
    const r = await fetch("/api/state.json", {cache: "no-store"});
    render(await r.json());
  } catch (e) { /* keep showing the last good state */ }
}
refresh();
setInterval(refresh, 30000);
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


def make_handler(cache):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # never log a request path to stdout/journal — it is nobody's business

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/state.json":
                body = json.dumps(cache.get()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

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

    def build():
        return build_state(projects_dir, state_root, fetch_board,
                            unit_active_fn=systemctl_is_active, ask_fn=board_ask)

    cache = Cache(build, refresh_sec)
    stop_event = threading.Event()
    t = threading.Thread(target=cache.loop, args=(stop_event,), daemon=True)
    t.start()

    server = ThreadingHTTPServer((host, port), make_handler(cache))
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
