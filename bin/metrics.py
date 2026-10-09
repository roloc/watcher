#!/usr/bin/env python3
"""Read-only report: how the loop is actually doing, per lane, per week.

Every tick writes its own log under the lane's state directory
(`logs/card-<n>-<ts>.json` — `subtype`, `total_cost_usd`, `num_turns`, `result`, the
tick's own five-line report as free text — `how-it-works.md`, "State on disk"). Nobody
adds those up, so whether a rule change helped is answered from memory, not from data
(watcher#37). This script only reads; it posts nothing anywhere, on its own or otherwise —
a weekly Discord digest is a separate decision.

    python3 bin/metrics.py <state-dir> [<state-dir> ...] [--weeks N]

    python3 bin/metrics.py ~/.local/state/watcher/riffmap
    python3 bin/metrics.py ~/.local/state/watcher/riffmap ~/.local/state/watcher/riffmap-design --weeks 4

Each `<state-dir>` is one lane's `WATCHER_STATE` (`~/.local/state/watcher/<project>`).
Giving it several prints one section each, so a weekly digest across a project's lanes is
one command. `--weeks` (default 8) sets how many calendar weeks the weekly table covers.

What it reports, per lane:

- **First-pass merge rate** — of the cards that ever reached Done, how many did on their
  very first tick.
- **Park rate** — Stuck moves split by who made them: the tick itself, with a question
  (`Ended in: Stuck - Needs help` and `Needs Steve: yes`), vs. the spawner's own two-attempt
  rule. There is no "parked by the spawner" value to read directly (`watch.md`'s Outcome
  enum has none), so this is inferred after the fact: two consecutive ticks on the same
  card that neither shipped nor moved it anywhere, the same shape `bump_attempts` and
  `park_stuck` in `spawn.sh` use to decide it live.
- **Deaths** — ticks whose own `subtype` was not `success` (a killed tick still writes one;
  `how-it-works.md`, "How a tick ends" — `error_max_turns`, `error_max_budget_usd`,
  `error_during_execution`), grouped by that subtype. A log file that is missing, empty or
  will not parse counts here too, under `unknown` — the shape a genuinely killed process
  leaves when it dies before flushing any result at all.
- **Turns and quota per shipped card** — median and worst, summed across every tick a card
  took (not just its last — a card parked once and finished on a second tick counts both),
  for every card that reached Done.
- **Time from In progress to Done** — median and worst, from the first tick's launch to the
  tick that reached Done finishing (`duration_ms` when the log has it, the file's own mtime
  otherwise). This includes any time the card sat waiting on Steve in Stuck along the way,
  since that is real elapsed time too — a lane with a lot of Stuck round-trips will show it
  here as well as in the park rate.

A tick's own report (`watch.md`, "## Report") is the source for `Outcome`, `Ended in` and
`Needs Steve`; a log whose `subtype` is not `success` has none of those and counts only
toward Deaths and the raw tick count. `/triage` ticks write a different report shape (no
`Ended in`) and so never count toward anything but the raw tick count and Deaths either —
this report is shaped for a build/watch lane.
"""
import glob
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone

LOG_RE = re.compile(r'^card-(\d+)-(\d{8}T\d{6}Z)\.json$')
TS_FMT = '%Y%m%dT%H%M%SZ'

OUTCOME_RE = re.compile(r'^- Outcome:\s*(.+)$', re.M)
ENDED_RE = re.compile(r'^- Ended in:\s*(.+)$', re.M)
NEEDS_RE = re.compile(r'^- Needs Steve:\s*(.+)$', re.M)

# `watch.md`'s "Ended in" enum. None of these five is a prefix of another (they all
# diverge by the 4th character), so match order does not matter. Real logs routinely
# append an explanatory parenthetical —
# "Done (issue closed by merge)", "Stuck - Needs help (awaiting Steve's approval reply)",
# "In progress (routed onward — the one non-failure In progress exit)" — so this is a
# prefix match, never `==`, against the raw text; a card whose report used one of these
# reads as its column regardless of what a tick added after it.
ENDED_IN_KINDS = ("Stuck - Needs help", "In review", "In progress", "Backlog", "Done")


def classify_ended_in(raw):
    if raw is None:
        return None
    for kind in ENDED_IN_KINDS:
        if raw.startswith(kind):
            return kind
    return raw  # an unrecognized value: kept verbatim so it counts nowhere silently


class Tick:
    __slots__ = ("issue", "ts", "mtime", "subtype", "cost", "turns", "duration_ms",
                 "outcome", "ended_in", "needs_steve")

    def __init__(self, issue, ts, mtime):
        self.issue = issue
        self.ts = ts
        self.mtime = mtime
        self.subtype = "unknown"
        self.cost = None
        self.turns = None
        self.duration_ms = None
        self.outcome = None
        self.ended_in = None
        self.needs_steve = None

    @property
    def died(self):
        return self.subtype != "success"

    @property
    def end_time(self):
        if self.duration_ms is not None:
            return self.ts + timedelta(milliseconds=self.duration_ms)
        return self.mtime

    @property
    def non_terminal(self):
        """A tick that leaves the card no better off than before it ran — the shape
        `spawn.sh`'s attempts counter treats as one of the two failures that get a card
        parked. A died tick always qualifies; a `success` tick only when it left the card
        In progress under THIS lane — neither routing to another lane nor labeling the
        card `awaiting-rebuild` (card #85) is a failure, and `spawn.sh`'s own
        `handle_exit` resets the attempt count for both the same way."""
        if self.died:
            return True
        if self.ended_in != "In progress":
            return False
        outcome = (self.outcome or "").lower()
        return "routed to" not in outcome and "awaiting rebuild" not in outcome


def load_log(path):
    m = LOG_RE.match(os.path.basename(path))
    if not m:
        return None
    issue = int(m.group(1))
    ts = datetime.strptime(m.group(2), TS_FMT).replace(tzinfo=timezone.utc)
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    except OSError:
        mtime = ts
    t = Tick(issue, ts, mtime)
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return t  # missing, empty or corrupt — a died tick that never flushed a result
    t.subtype = d.get("subtype") or "unknown"
    t.cost = d.get("total_cost_usd")
    t.turns = d.get("num_turns")
    t.duration_ms = d.get("duration_ms")
    if t.subtype == "success":
        result = d.get("result") or ""
        m = OUTCOME_RE.search(result)
        t.outcome = m.group(1).strip() if m else None
        m = ENDED_RE.search(result)
        t.ended_in = classify_ended_in(m.group(1).strip()) if m else None
        m = NEEDS_RE.search(result)
        t.needs_steve = m.group(1).strip() if m else None
    return t


def load_lane(state_dir):
    ticks = []
    for path in sorted(glob.glob(os.path.join(state_dir, "logs", "card-*.json"))):
        t = load_log(path)
        if t is not None:
            ticks.append(t)
    ticks.sort(key=lambda t: t.ts)
    return ticks


def group_by_issue(ticks):
    out = {}
    for t in ticks:
        out.setdefault(t.issue, []).append(t)
    for lst in out.values():
        lst.sort(key=lambda t: t.ts)
    return out


def first_pass_merge_rate(issues):
    done, first_pass = [], []
    for n, recs in issues.items():
        done_at = next((r for r in recs if r.ended_in == "Done"), None)
        if done_at is None:
            continue
        done.append(n)
        if recs[0] is done_at:
            first_pass.append(n)
    return len(first_pass), len(done)


def park_counts(issues):
    """(parked by the tick itself with a question, parked by the spawner's two-attempt rule)."""
    tick_parked = spawner_parked = 0
    for recs in issues.values():
        streak = 0
        for r in recs:
            if r.ended_in == "Stuck - Needs help" and (r.needs_steve or "").lower().startswith("yes"):
                tick_parked += 1
            if r.non_terminal:
                streak += 1
                if streak == 2:
                    spawner_parked += 1
                    streak = 0
            else:
                streak = 0
    return tick_parked, spawner_parked


def death_counts(ticks):
    counts = {}
    for t in ticks:
        if t.died:
            counts[t.subtype] = counts.get(t.subtype, 0) + 1
    return counts


def shipped_stats(issues):
    """One entry per card that reached Done: turns and quota summed, and elapsed time
    measured, across every tick from the first one up to the tick that shipped it."""
    out = []
    for n, recs in issues.items():
        done_at = next((r for r in recs if r.ended_in == "Done"), None)
        if done_at is None:
            continue
        episode = [r for r in recs if r.ts <= done_at.ts]
        out.append({
            "issue": n,
            "turns": sum(r.turns for r in episode if r.turns is not None),
            "cost": sum(r.cost for r in episode if r.cost is not None),
            "elapsed": done_at.end_time - episode[0].ts,
        })
    return out


def median_worst(values):
    if not values:
        return None, None
    return statistics.median(values), max(values)


def fmt_td(td):
    total_min = max(int(td.total_seconds() // 60), 0)
    if total_min < 60:
        return f"{total_min}m"
    h, m = divmod(total_min, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def week_start(dt):
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def weekly_breakdown(ticks, weeks, now=None):
    """One pass over the FULL tick history, not a re-run of the summary metrics on each
    week's slice of ticks in isolation — a card's own story routinely spans a week
    boundary (a `no progress` tick Sunday night, its `Done` tick Monday morning), and
    slicing first would count that card as a same-week first-pass Done, or lose half of a
    spawner-park streak, in both cases changing the number in the flattering direction.
    Each fact is instead attributed to the week of the tick that actually decided it: a
    first-pass or a Done to the week of the Done tick, an inferred spawner-park to the
    week of the second of its two triggering ticks, a died tick or a raw tick count to its
    own week."""
    now = now or datetime.now(timezone.utc)
    starts = [week_start(now) - timedelta(weeks=i) for i in range(weeks)][::-1]

    def bucket(dt):
        if dt < starts[0]:
            return None
        for i in range(len(starts) - 1):
            if dt < starts[i + 1]:
                return i
        return len(starts) - 1

    rows = [{"ticks": 0, "died": 0, "done": 0, "first_pass": 0,
             "tick_parked": 0, "spawner_parked": 0} for _ in starts]

    for t in ticks:
        b = bucket(t.ts)
        if b is None:
            continue
        rows[b]["ticks"] += 1
        if t.died:
            rows[b]["died"] += 1

    for recs in group_by_issue(ticks).values():
        done_at = next((r for r in recs if r.ended_in == "Done"), None)
        if done_at is not None:
            b = bucket(done_at.ts)
            if b is not None:
                rows[b]["done"] += 1
                if recs[0] is done_at:
                    rows[b]["first_pass"] += 1
        for r in recs:
            if r.ended_in == "Stuck - Needs help" and (r.needs_steve or "").lower().startswith("yes"):
                b = bucket(r.ts)
                if b is not None:
                    rows[b]["tick_parked"] += 1
        streak = 0
        for r in recs:
            if r.non_terminal:
                streak += 1
                if streak == 2:
                    b = bucket(r.ts)  # the second failing tick is the one the spawner acts on
                    if b is not None:
                        rows[b]["spawner_parked"] += 1
                    streak = 0
            else:
                streak = 0

    return starts, rows


def report(lane, ticks, weeks, out=sys.stdout):
    p = lambda s="": print(s, file=out)
    p(f"## {lane}")
    if not ticks:
        p("  no logs found")
        p()
        return
    issues = group_by_issue(ticks)
    p(f"  Ticks: {len(ticks)} across {len(issues)} cards")

    deaths = death_counts(ticks)
    if deaths:
        for subtype, n in sorted(deaths.items(), key=lambda kv: -kv[1]):
            p(f"  Died ({subtype}): {n} ({100 * n / len(ticks):.0f}% of ticks)")
    else:
        p("  Died: 0")

    fp, done = first_pass_merge_rate(issues)
    if done:
        p(f"  First-pass merge rate: {fp}/{done} ({100 * fp / done:.0f}%)")
    else:
        p("  First-pass merge rate: no cards reached Done")

    tick_parked, spawner_parked = park_counts(issues)
    p(f"  Parked: {tick_parked} by the tick itself (with a question), "
      f"{spawner_parked} by the spawner (two failed attempts)")

    shipped = shipped_stats(issues)
    if shipped:
        turns_med, turns_worst = median_worst([s["turns"] for s in shipped])
        cost_med, cost_worst = median_worst([s["cost"] for s in shipped])
        el_med, el_worst = median_worst([s["elapsed"] for s in shipped])
        p(f"  Per shipped card ({len(shipped)}): turns median {turns_med:.0f}, "
          f"worst {turns_worst:.0f}; quota median ~${cost_med:.2f}, worst ~${cost_worst:.2f} "
          f"(subscription — not billed)")
        p(f"  Time from In progress to Done: median {fmt_td(el_med)}, worst {fmt_td(el_worst)}")
    else:
        p("  Per shipped card: no cards reached Done")
    p()

    p(f"  By week (last {weeks}):")
    p(f"  {'week of':<12}{'ticks':>7}{'1st-pass':>10}{'parked t/s':>12}{'died':>6}")
    starts, rows = weekly_breakdown(ticks, weeks)
    for wk_start, row in zip(starts, rows):
        fp_str = f"{row['first_pass']}/{row['done']}" if row["done"] else "-"
        parked_str = f"{row['tick_parked']}/{row['spawner_parked']}"
        p(f"  {wk_start.date().isoformat():<12}{row['ticks']:>7}{fp_str:>10}"
          f"{parked_str:>12}{row['died']:>6}")
    p()


def main(argv):
    weeks = 8
    dirs = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--weeks":
            i += 1
            if i >= len(argv):
                sys.exit("metrics.py: --weeks needs a number")
            try:
                weeks = int(argv[i])
            except ValueError:
                sys.exit(f"metrics.py: --weeks wants a number, got {argv[i]!r}")
            if weeks < 1:
                sys.exit("metrics.py: --weeks must be at least 1")
        elif a in ("-h", "--help"):
            sys.exit(__doc__)
        else:
            dirs.append(a)
        i += 1
    if not dirs:
        sys.exit(__doc__)
    for d in dirs:
        lane = os.path.basename(os.path.normpath(d))
        report(lane, load_lane(d), weeks)


if __name__ == "__main__":
    main(sys.argv[1:])
