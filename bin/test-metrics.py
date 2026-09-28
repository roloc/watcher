#!/usr/bin/env python3
"""Offline tests for bin/metrics.py. No network, no real state directory: every case
writes its own fixture log files under a throwaway directory and reads them back.

    python3 bin/test-metrics.py      # exit 0 = all cases hold
"""
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import metrics  # noqa: E402

fails = 0
count = 0


def check(name, cond, detail=""):
    global fails, count
    count += 1
    print(f"  {name:<78} {'ok' if cond else 'FAIL ' + detail}")
    if not cond:
        fails = 1


TS_FMT = metrics.TS_FMT
NOW = datetime.now(timezone.utc)


def ts(minutes_ago=0):
    return (NOW - timedelta(minutes=minutes_ago)).strftime(TS_FMT)


STATE = tempfile.mkdtemp(prefix="watcher-metrics-test-")
LOGS = os.path.join(STATE, "logs")
os.makedirs(LOGS, exist_ok=True)


def report_lines(issue, outcome, ended_in, needs_steve="no"):
    return (f"- Card: #{issue} a title\n"
            f"- Outcome: {outcome}\n"
            f"- Ended in: {ended_in}\n"
            f"- Quota: ~$1.00 equivalent (subscription — not billed); project API spend none\n"
            f"- Needs Steve: {needs_steve}\n")


def write_log(issue, when, subtype="success", cost=1.0, turns=5, duration_ms=60000,
              outcome=None, ended_in=None, needs_steve="no", corrupt=False, empty=False):
    path = os.path.join(LOGS, f"card-{issue}-{when}.json")
    if empty:
        open(path, "w").close()
        return path
    if corrupt:
        with open(path, "w") as f:
            f.write("{not json")
        return path
    d = {"subtype": subtype, "total_cost_usd": cost, "num_turns": turns,
         "duration_ms": duration_ms}
    if subtype == "success" and ended_in is not None:
        d["result"] = report_lines(issue, outcome, ended_in, needs_steve)
    elif subtype == "success":
        d["result"] = f"Quiet tick: nothing claimable in In progress."
    else:
        d["result"] = "Error."
    with open(path, "w") as f:
        json.dump(d, f)
    return path


# --- fixtures --------------------------------------------------------------------------
# #1: shipped first-pass, one tick
write_log(1, ts(120), outcome="merged", ended_in="Done")

# #2: shipped after a park round-trip — In review then Done, two ticks
write_log(2, ts(500), outcome="PR up, waiting on merge", ended_in="In review")
write_log(2, ts(400), outcome="merged", ended_in="Done")

# #3: the tick parks itself with a question
write_log(3, ts(300), outcome="stuck", ended_in="Stuck - Needs help",
           needs_steve="yes — which of two mockups did he mean?")

# #4: two consecutive no-progress ticks → the spawner's two-attempt park, inferred
write_log(4, ts(200), outcome="no progress", ended_in="In progress")
write_log(4, ts(190), outcome="no progress", ended_in="In progress")

# #5: routed to another lane — in progress, but not a failure
write_log(5, ts(150), outcome="routed to designer", ended_in="In progress")

# #6: declined — gates itself back to Backlog, not a park
write_log(6, ts(140), outcome="declined (gated)", ended_in="Backlog")

# #7: died ticks, one per documented subtype
write_log(7, ts(100), subtype="error_max_turns")
write_log(8, ts(90), subtype="error_max_budget_usd")
write_log(9, ts(80), subtype="error_during_execution")

# #10: a killed tick that never flushed a result at all
write_log(10, ts(70), empty=True)

# #11: a corrupt result file
write_log(11, ts(60), corrupt=True)

# real riffmap text: a tick's own parenthetical after the enum word, which a strict `==`
# against "Done"/"In progress" would silently drop from every count (found on real data:
# card-275, card-200 in ~/.local/state/watcher/riffmap*/logs)
write_log(13, ts(55), outcome="merged", ended_in="Done (issue closed via `Closes #262`)")
write_log(14, ts(52),
          outcome="architect decision written and merged; build work filed as #208 and routed to the renderer lane",
          ended_in="In progress (routed onward — the one non-failure In progress exit)")

# a stray file that does not match the log naming — must be ignored, not crash the loader.
# card-99-notatimestamp.json is the sharper case: it matches the `card-*.json` glob, so
# only the per-file filename regex (not the glob) is what has to reject it.
with open(os.path.join(LOGS, f"card-12-{ts(50)}.stderr"), "w") as f:
    f.write("some stderr\n")
with open(os.path.join(LOGS, "card-99-notatimestamp.json"), "w") as f:
    f.write("{}")

ticks = metrics.load_lane(STATE)
issues = metrics.group_by_issue(ticks)

print("1. loading tolerates every shape and ignores non-log files")
check("15 real ticks loaded (14 cards' worth, #2 has two)", len(ticks) == 15, str(len(ticks)))
check("a stray .stderr is skipped (no card #12)", 12 not in issues)
check("a card-*.json that fails the filename regex is skipped too (no card #99)", 99 not in issues)
check("empty file (#10) still counts as a died tick, subtype unknown",
      next(t for t in ticks if t.issue == 10).subtype == "unknown")
check("corrupt file (#11) also falls back to subtype unknown",
      next(t for t in ticks if t.issue == 11).subtype == "unknown")

print("1b. a parenthetical after the enum word still classifies (real riffmap text)")
check("'Done (issue closed via ...)' still reads as Done",
      issues[13][0].ended_in == "Done")
check("'In progress (routed onward ...)' still reads as In progress",
      issues[14][0].ended_in == "In progress")
check("...and its Outcome mentioning 'routed' (not `startswith`) marks it not-a-failure",
      issues[14][0].non_terminal is False)

print("2. first-pass merge rate")
fp, done = metrics.first_pass_merge_rate(issues)
check("3 cards reached Done (#1, #2, #13)", done == 3, str(done))
check("#1 and #13 did it on the first tick, #2 needed a second", fp == 2, str(fp))

print("3. park rate: tick-initiated vs. spawner-inferred")
tick_parked, spawner_parked = metrics.park_counts(issues)
check("#3's own Stuck-with-a-question counts once", tick_parked == 1, str(tick_parked))
check("#4's two consecutive no-progress ticks count as one spawner park",
      spawner_parked == 1, str(spawner_parked))
check("#5 (routed) is not a park", metrics.group_by_issue(ticks)[5][0].non_terminal is False)
check("#6 (declined/Backlog) is not a park", metrics.group_by_issue(ticks)[6][0].non_terminal is False)

print("4. deaths, grouped by subtype (missing/corrupt fold into 'unknown')")
deaths = metrics.death_counts(ticks)
check("error_max_turns: 1", deaths.get("error_max_turns") == 1)
check("error_max_budget_usd: 1", deaths.get("error_max_budget_usd") == 1)
check("error_during_execution: 1", deaths.get("error_during_execution") == 1)
check("unknown (empty + corrupt): 2", deaths.get("unknown") == 2, str(deaths.get("unknown")))
check("a shipped card's ticks are not deaths", "success" not in deaths)

print("5. turns/quota/elapsed per shipped card sums the whole episode")
shipped = {s["issue"]: s for s in metrics.shipped_stats(issues)}
check("#1: one tick's worth", shipped[1]["turns"] == 5 and shipped[1]["cost"] == 1.0)
check("#2: both ticks summed", shipped[2]["turns"] == 10 and shipped[2]["cost"] == 2.0)
check("#2's elapsed spans from the first tick's launch to the Done tick finishing",
      shipped[2]["elapsed"] >= timedelta(minutes=99))

print("6. fmt_td and week_start")
check("under an hour", metrics.fmt_td(timedelta(minutes=45)) == "45m")
check("hours and minutes", metrics.fmt_td(timedelta(hours=2, minutes=5)) == "2h05m")
check("days and hours", metrics.fmt_td(timedelta(days=1, hours=3)) == "1d03h")
check("week_start is always a Monday",
      metrics.week_start(datetime(2026, 9, 28, 15, tzinfo=timezone.utc)).weekday() == 0)

print("7. report() renders without crashing, empty lane included")
buf = io.StringIO()
metrics.report("riffmap", ticks, 4, out=buf)
text = buf.getvalue()
check("names the lane", "## riffmap" in text)
check("shows the first-pass rate", "First-pass merge rate: 2/3" in text)
check("shows the park split", "1 by the tick itself" in text and "1 by the spawner" in text)
check("shows a per-shipped-card line", "Per shipped card (3)" in text)
buf2 = io.StringIO()
metrics.report("empty-lane", [], 4, out=buf2)
check("an empty lane says so instead of crashing", "no logs found" in buf2.getvalue())

print("8. CLI: no args prints usage and exits non-zero")
try:
    metrics.main([])
    check("no args exits", False)
except SystemExit as e:
    check("no args exits with the docstring", "python3 bin/metrics.py" in str(e))
try:
    metrics.main(["some-dir", "--weeks", "not-a-number"])
    check("--weeks rejects a non-number", False)
except SystemExit as e:
    check("--weeks rejects a non-number", "wants a number" in str(e))

print("9. weekly breakdown attributes a fact to the week that decided it, not the week "
      "that merely holds one of its ticks (a card's story can span a week boundary)")
WSTATE = tempfile.mkdtemp(prefix="watcher-metrics-test-weekly-")
WLOGS = os.path.join(WSTATE, "logs")
os.makedirs(WLOGS, exist_ok=True)


def wwrite(issue, when, **kw):
    path = os.path.join(WLOGS, f"card-{issue}-{when}.json")
    d = {"subtype": kw.get("subtype", "success"), "total_cost_usd": kw.get("cost", 1.0),
         "num_turns": kw.get("turns", 5), "duration_ms": kw.get("duration_ms", 60000)}
    if d["subtype"] == "success":
        d["result"] = report_lines(issue, kw.get("outcome"), kw.get("ended_in"), kw.get("needs_steve", "no"))
    with open(path, "w") as f:
        json.dump(d, f)


# Anchored to metrics' own week_start, not a fixed "N days ago" — the distance from "now"
# to the previous calendar week varies with today's weekday (a fixed 10-day offset landed
# two weeks back, not one, whenever run early in the week — found in review).
LAST_WEEK = (metrics.week_start(NOW) - timedelta(days=1)).strftime(TS_FMT)  # last week, any day
THIS_WEEK = ts(0)

# #20: a "no progress" tick last week, its Done tick this week — took two ticks overall,
# so it must NOT read as a first-pass Done in either week's row.
wwrite(20, LAST_WEEK, outcome="no progress", ended_in="In progress")
wwrite(20, THIS_WEEK, outcome="merged", ended_in="Done")

# #21: the first of a two-tick spawner-park streak last week, the second (the one the
# spawner actually acts on) this week — the park belongs to THIS week, not nowhere.
wwrite(21, LAST_WEEK, outcome="no progress", ended_in="In progress")
wwrite(21, THIS_WEEK, outcome="no progress", ended_in="In progress")

wticks = metrics.load_lane(WSTATE)
w_starts, w_rows = metrics.weekly_breakdown(wticks, 3)
this_week_row = w_rows[-1]
last_week_row = w_rows[-2] if len(w_rows) > 1 else None

check("overall: #20 reached Done but not on its first tick",
      metrics.first_pass_merge_rate(metrics.group_by_issue(wticks)) == (0, 1))
check("this week's row does NOT count #20 as a first-pass Done",
      this_week_row["done"] == 1 and this_week_row["first_pass"] == 0,
      str(this_week_row))
if last_week_row is not None:
    check("last week's row has no Done at all for #20 (it wasn't decided that week)",
          last_week_row["done"] == 0, str(last_week_row))
check("the #21 park is attributed to the week of the SECOND failing tick (this week)",
      this_week_row["spawner_parked"] == 1, str(this_week_row))
if last_week_row is not None:
    check("...not split into or lost in the earlier week",
          last_week_row["spawner_parked"] == 0, str(last_week_row))

shutil.rmtree(WSTATE, ignore_errors=True)
shutil.rmtree(STATE, ignore_errors=True)

print(f"{count} cases, {'all passed' if not fails else 'FAILURES'}")
sys.exit(fails)
