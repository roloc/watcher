#!/usr/bin/env python3
"""Offline tests for bin/board-export.py. No network: `collect()` takes the GraphQL
function as an argument and these cases hand it a stub that serves canned pages.

    python3 bin/test-board-export.py      # exit 0 = all cases hold
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("board_export", os.path.join(HERE, "board-export.py"))
bx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bx)

fails = 0
count = 0


def check(name, cond, detail=""):
    global fails, count
    count += 1
    print("  %-78s %s" % (name, "ok" if cond else "FAIL " + str(detail)))
    if not cond:
        fails = 1


def issue(repo, n, title, status, labels=(), assignees=(), kind="Issue", state="OPEN"):
    return {"updatedAt": "2026-10-30T12:00:00Z",
            "status": {"name": status} if status else None,
            "content": {"__typename": kind, "number": n, "title": title,
                        "url": "https://github.com/%s/issues/%d" % (repo, n), "state": state,
                        "repository": {"nameWithOwner": repo},
                        "labels": {"nodes": [{"name": l} for l in labels]},
                        "assignees": {"nodes": [{"login": a} for a in assignees]}}}


BOARDS = {"user": {"projectsV2": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
    {"number": 2, "title": "Watcher", "url": "https://github.com/users/roloc/projects/2",
     "closed": False, "field": {"options": [{"name": "Backlog"}, {"name": "Ready"},
                                            {"name": "In progress"}, {"name": "Done"}]}},
    {"number": 1, "title": "Riffmap", "url": "https://github.com/users/roloc/projects/1",
     "closed": False, "field": {"options": [{"name": "Backlog"}, {"name": "Ready"},
                                            {"name": "In progress"}, {"name": "Stuck"},
                                            {"name": "Done"}]}},
    {"number": 9, "title": "Old", "url": "https://github.com/users/roloc/projects/9",
     "closed": True, "field": None},
]}}}

# Riffmap: two pages of items, in an order that is not sorted; one unreadable, one draft,
# one PR, one card with no status at all.
RIFFMAP_PAGES = [
    {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [
        issue("roloc/riffmap", 811, "Portal taste pass", "In progress", ["agent: design"], ["riffmap-bot"]),
        issue("roloc/riffmap", 778, "Owner thread outranks body", "Done"),
        {"updatedAt": "2026-10-01T00:00:00Z", "status": {"name": "Ready"}, "content": None},
    ]},
    {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
        issue("roloc/riffmap", 700, "A pull request", "Ready", kind="PullRequest"),
        {"updatedAt": "2026-10-02T00:00:00Z", "status": {"name": "Backlog"},
         "content": {"__typename": "DraftIssue", "title": "Just an idea"}},
        issue("roloc/riffmap", 5, "No column yet", None),
        issue("roloc/riffmap", 812, "Second ready card", "Ready", ["bug"]),
    ]},
]
WATCHER_PAGES = [
    {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
        issue("roloc/watcher", 63, "Tail from the floor", "Done"),
        issue("roloc/watcher", 210, "Export the board", "In progress", [], ["roloc"]),
    ]},
]

calls = []


def stub(query, variables):
    calls.append((query, dict(variables)))
    if "projectsV2" in query:
        return BOARDS
    pages = {1: RIFFMAP_PAGES, 2: WATCHER_PAGES}[variables["number"]]
    idx = {None: 0, "c1": 1}[variables["cursor"]]
    return {"user": {"projectV2": {"items": pages[idx]}}}


print("collect():")
data = bx.collect("roloc", stub)
check("one boards query, three item pages", len(calls) == 4, len(calls))
check("the closed board is skipped", [b["number"] for b in data["boards"]] == [1, 2],
      [b["number"] for b in data["boards"]])
check("boards come out sorted by number", data["boards"][0]["title"] == "Riffmap")
rm = data["boards"][0]
check("both pages of riffmap items are kept", len(rm["items"]) == 7, len(rm["items"]))
check("items sorted by repo then number, unreadable last",
      [i["number"] for i in rm["items"]] == [5, 700, 778, 811, 812, None, None],
      [i["number"] for i in rm["items"]])
check("columns come from the board's own Status field",
      rm["columns"] == ["Backlog", "Ready", "In progress", "Stuck", "Done"])
by_num = {i["number"]: i for i in rm["items"] if i["number"]}
check("labels and assignees are recorded", by_num[811]["labels"] == ["agent: design"]
      and by_num[811]["assignees"] == ["riffmap-bot"])
check("a PR item is typed pr", by_num[700]["type"] == "pr")
check("a card with no status has status null", by_num[5]["status"] is None)
types = sorted(i["type"] for i in rm["items"] if i["number"] is None)
check("the draft and the unreadable card are kept and typed", types == ["draft", "unreadable"], types)
check("updatedAt is carried", by_num[811]["updatedAt"] == "2026-10-30T12:00:00Z")

print("render_md():")
md = bx.render_md(data)
check("a heading per open board", md.count("\n## ") == 2, md.count("\n## "))
check("counts per column in the board's order",
      md.index("| Backlog | 1 |") < md.index("| Ready | 3 |") < md.index("| In progress | 1 |")
      < md.index("| Done | 1 |"))
check("a column with no cards is left out", "| Stuck |" not in md)
check("a card with no status is counted", "| (no status) | 1 |" in md)
check("the unreadable count is spelled out", "1 card(s) unreadable" in md)
check("Ready cards are listed with a link", "- [roloc/riffmap#812](https://github.com/roloc/riffmap/issues/812) Second ready card — bug" in md, md)
check("In progress cards carry labels and assignees",
      "Portal taste pass — agent: design; @riffmap-bot" in md)
check("Done cards are not listed card by card", "Owner thread outranks body" not in md)
check("Backlog drafts are not listed card by card", "Just an idea" not in md)
check("the only timestamps are the cards' own updatedAt (none in the markdown)",
      "2026-10-30" not in md)

print("write():")
with tempfile.TemporaryDirectory() as d:
    bx.write(data, d)
    bx.write(data, d)  # twice: the second write must be byte-identical
    js = open(os.path.join(d, "status.json")).read()
    check("status.json parses back to the same structure", json.loads(js) == data)
    check("status.json is sorted and indented (stable bytes)", js == json.dumps(data, indent=2, sort_keys=True) + "\n")
    check("STATUS.md is what render_md gives", open(os.path.join(d, "STATUS.md")).read() == md)

print("errors:")
try:
    bx.collect("nobody", lambda q, v: {"user": None})
    check("an unknown user is a clean exit", False)
except SystemExit as e:
    check("an unknown user is a clean exit", "no user 'nobody'" in str(e), e)

try:
    bx.main(["--owner", "roloc", "--out", "/nonexistent"])
    check("no token is a clean exit, before any call", False)
except SystemExit as e:
    check("no token is a clean exit, before any call", "BOARD_READ_TOKEN is not set" in str(e), e)

print("%d cases, %s" % (count, "all ok" if not fails else "FAILURES"))
sys.exit(fails)
