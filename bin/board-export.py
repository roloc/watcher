#!/usr/bin/env python3
"""Export every GitHub Projects (v2) board one user owns to two files a reader with no
board access can open: `status.json` (everything) and `STATUS.md` (counts per column, then
the Ready and In progress cards). `.github/workflows/board-status.yml` runs this hourly and
commits the result to the `board-status` branch, so a cloud Claude that can read the repo
can read the boards.

    BOARD_READ_TOKEN=... python3 bin/board-export.py --owner roloc --out board-status/

Read-only: one GraphQL query for the list of boards, then one per page of 100 items. A
board that is closed is skipped. Org-owned boards are never seen — the query starts at
`user(login:)`, so an org's boards are not in the answer at all.

The output is deterministic — boards sorted by number, items by repo then number, no
timestamp anywhere in either file — so the workflow can tell "nothing changed" by
comparing bytes and skip the commit. Freshness is the date of the branch's last commit.

A card whose issue the token cannot see comes back with no content at all (GitHub returns
the item with `content: null`); it is counted under `unreadable` rather than dropped, so a
token missing a repository shows up as a number in the summary instead of as a card that
silently vanished.

`python3 bin/test-board-export.py` is the offline suite (CI runs it): `graphql()` is
replaced with a stub, nothing reaches the network.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com/graphql"
STATUS_FIELD = "Status"
# The two columns STATUS.md lists card by card (matched without regard to case).
LISTED_COLUMNS = ("Ready", "In progress")

BOARDS_QUERY = """query($owner:String!,$cursor:String){ rateLimit{ cost remaining }
  user(login:$owner){ projectsV2(first:50, after:$cursor){
    pageInfo{ hasNextPage endCursor }
    nodes{ number title url closed
      field(name:"%s"){ ... on ProjectV2SingleSelectField{ options{ name } } } } } } }""" % STATUS_FIELD

ITEMS_QUERY = """query($owner:String!,$number:Int!,$cursor:String){ rateLimit{ cost remaining }
  user(login:$owner){ projectV2(number:$number){
    items(first:100, after:$cursor){ pageInfo{ hasNextPage endCursor }
      nodes{ updatedAt
        status: fieldValueByName(name:"%s"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } }
        content{ __typename
          ... on Issue{ number title url state repository{ nameWithOwner }
            labels(first:20){ nodes{ name } } assignees(first:10){ nodes{ login } } }
          ... on PullRequest{ number title url state repository{ nameWithOwner }
            labels(first:20){ nodes{ name } } assignees(first:10){ nodes{ login } } }
          ... on DraftIssue{ title } } } } } } }""" % STATUS_FIELD


def graphql(query, variables, token):
    """One GraphQL call. Raises on HTTP or GraphQL errors; never prints the token."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": "bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "watcher-board-export",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit("board-export: GitHub answered HTTP %s — a bad or expired "
                         "BOARD_READ_TOKEN, most likely" % e.code)
    if out.get("errors"):
        raise SystemExit("board-export: GraphQL errors: " + json.dumps(out["errors"]))
    return out["data"]


def collect(owner, call, log=lambda s: None):
    """Every open board `owner` owns, with every item. `call(query, variables)` is the
    GraphQL function (the test stubs it). Returns the `status.json` structure."""
    boards, cursor = [], None
    while True:
        data = call(BOARDS_QUERY, {"owner": owner, "cursor": cursor})
        user = data.get("user")
        if user is None:
            raise SystemExit("board-export: no user %r (or the token cannot see them)" % owner)
        page = user["projectsV2"]
        boards.extend(n for n in page["nodes"] if n and not n["closed"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    boards.sort(key=lambda b: b["number"])

    out = []
    for b in boards:
        field = b.get("field") or {}
        columns = [o["name"] for o in field.get("options", [])]
        items, cursor = [], None
        while True:
            data = call(ITEMS_QUERY, {"owner": owner, "number": b["number"], "cursor": cursor})
            page = data["user"]["projectV2"]["items"]
            for n in page["nodes"]:
                items.append(_item(n))
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        items.sort(key=lambda i: (i["repo"] or "~", i["number"] or 0, i["title"]))
        log("%s (#%d): %d items" % (b["title"], b["number"], len(items)))
        out.append({"number": b["number"], "title": b["title"], "url": b["url"],
                    "columns": columns, "items": items})
    return {"owner": owner, "boards": out}


def _item(node):
    c = node.get("content") or {}
    kind = c.get("__typename")
    status = (node.get("status") or {}).get("name")
    base = {"status": status, "updatedAt": node.get("updatedAt")}
    if kind in ("Issue", "PullRequest"):
        return dict(base, type="issue" if kind == "Issue" else "pr",
                    repo=c["repository"]["nameWithOwner"], number=c["number"],
                    title=c["title"], url=c["url"], state=c["state"],
                    labels=sorted(l["name"] for l in c["labels"]["nodes"]),
                    assignees=sorted(a["login"] for a in c["assignees"]["nodes"]))
    if kind == "DraftIssue":
        return dict(base, type="draft", repo=None, number=None, title=c.get("title") or "",
                    url=None, state=None, labels=[], assignees=[])
    # No content at all: an issue in a repository this token cannot read.
    return dict(base, type="unreadable", repo=None, number=None, title="", url=None,
                state=None, labels=[], assignees=[])


def render_md(data):
    lines = ["# Board status — @%s" % data["owner"], "",
             "Every open Projects (v2) board this account owns, exported by "
             "`bin/board-export.py`. Fresh as of this branch's last commit. "
             "`status.json` beside this file has every card with its labels, "
             "assignees and last update.", ""]
    for b in data["boards"]:
        items = b["items"]
        lines += ["## %s (board #%d)" % (b["title"], b["number"]), "", b["url"], ""]
        counts = {}
        for i in items:
            counts[i["status"] or "(no status)"] = counts.get(i["status"] or "(no status)", 0) + 1
        order = list(b["columns"]) + sorted(k for k in counts if k not in b["columns"])
        lines.append("| Column | Cards |")
        lines.append("|---|---|")
        for col in order:
            if col in counts:
                lines.append("| %s | %d |" % (col, counts[col]))
        unreadable = sum(1 for i in items if i["type"] == "unreadable")
        if unreadable:
            lines.append("")
            lines.append("%d card(s) unreadable: their repository is not in the token's "
                         "repository list." % unreadable)
        for col in LISTED_COLUMNS:
            picked = [i for i in items if (i["status"] or "").lower() == col.lower()]
            if not picked:
                continue
            lines += ["", "### %s" % col, ""]
            for i in picked:
                lines.append("- " + _md_item(i))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _md_item(i):
    if i["type"] == "unreadable":
        return "(unreadable card)"
    if i["type"] == "draft":
        return "(draft) %s" % i["title"]
    ref = "[%s#%d](%s)" % (i["repo"], i["number"], i["url"])
    extra = []
    if i["labels"]:
        extra.append(", ".join(i["labels"]))
    if i["assignees"]:
        extra.append(" ".join("@" + a for a in i["assignees"]))
    return "%s %s%s" % (ref, i["title"], (" — " + "; ".join(extra)) if extra else "")


def write(data, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "status.json"), "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out_dir, "STATUS.md"), "w") as f:
        f.write(render_md(data))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--owner", required=True, help="the user whose boards to export")
    ap.add_argument("--out", required=True, help="directory for status.json and STATUS.md")
    args = ap.parse_args(argv)
    token = os.environ.get("BOARD_READ_TOKEN", "")
    if not token:
        sys.exit("board-export: BOARD_READ_TOKEN is not set")
    spent = {"cost": 0, "remaining": None}

    def call(query, variables):
        data = graphql(query, variables, token)
        rl = data.get("rateLimit") or {}
        spent["cost"] += rl.get("cost", 0)
        spent["remaining"] = rl.get("remaining", spent["remaining"])
        return data

    data = collect(args.owner, call, log=lambda s: print("board-export: " + s))
    write(data, args.out)
    print("board-export: %d board(s), %d GraphQL point(s) spent, %s remaining this hour"
          % (len(data["boards"]), spent["cost"], spent["remaining"]))


if __name__ == "__main__":
    main()
