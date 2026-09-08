#!/usr/bin/env python3
"""Drive the GitHub Projects board from the command line.

The watcher (`/watch`) uses this so a tick never re-derives GraphQL. Fields are
resolved by NAME, not by hard-coded id, so this survives a board edit and can be
copied to another project by changing OWNER/NUMBER.

    python3 scripts/board.py list ["In progress"]   # cards in a column, as TSV
    python3 scripts/board.py move <issue> "<status>"
    python3 scripts/board.py show <issue>
    python3 scripts/board.py unclaim <issue>        # drop a leaked in-flight label

Every failure exits 1 with a one-line `board.py: ...` message on stderr; only a
successful command exits 0. A caller may therefore treat any non-zero exit as "do not
proceed" — in particular `list` exits 1 for a column name the board does not have,
rather than printing nothing and looking like an empty column.
"""
import json
import os, subprocess, sys

OWNER = os.environ.get("BOARD_OWNER") or "roloc"
NUMBER = int(os.environ.get("BOARD_NUMBER") or 1)
STAGE_FIELD = "Stage"
STATUS_FIELD = "Status"


def gh(query, **vars):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in vars.items():
        cmd += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("board.py: `gh` is not installed or not on PATH")
    if r.returncode:
        sys.exit(f"board.py: gh failed: {r.stderr.strip()[:400]}")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.exit(f"board.py: gh returned no JSON: {(r.stdout or r.stderr)[:200]!r}")
    if "errors" in d:
        sys.exit(f"board.py: {d['errors'][0]['message']}")
    return d["data"]


def project():
    q = """query($owner:String!,$number:Int!){ user(login:$owner){ projectV2(number:$number){
      id title
      fields(first:40){ nodes{ ... on ProjectV2SingleSelectField{ name id options{ id name } } } } } } }"""
    u = gh(q, owner=OWNER, number=NUMBER).get("user")
    p = u.get("projectV2") if u else None
    if not p:
        sys.exit(f"board.py: no project {OWNER}/#{NUMBER} — if it moved to an org, "
                 "this script's user(login:) query needs to become organization(login:)")
    fields = {f["name"]: f for f in p["fields"]["nodes"] if f.get("name")}
    return p, fields


def items():
    q = """query($owner:String!,$number:Int!,$cursor:String){ user(login:$owner){ projectV2(number:$number){
      items(first:100, after:$cursor){ pageInfo{ hasNextPage endCursor }
        nodes{ id
          fieldValues(first:20){ nodes{ ... on ProjectV2ItemFieldSingleSelectValue{
            name field{ ... on ProjectV2SingleSelectField{ name } } } } }
          content{ ... on Issue{ number title url state
            labels(first:20){ nodes{ name } } } } } } } } }"""
    out, cursor = [], None
    while True:
        u = gh(q, owner=OWNER, number=NUMBER, cursor=cursor or "").get("user")
        p = u.get("projectV2") if u else None
        if not p:
            sys.exit(f"board.py: no project {OWNER}/#{NUMBER} (see `project()`)")
        page = p["items"]
        for n in page["nodes"]:
            c = n.get("content") or {}
            if not c.get("number"):
                continue
            sel = {}
            for fv in n["fieldValues"]["nodes"]:
                if fv and fv.get("field"):
                    sel[fv["field"]["name"]] = fv.get("name")
            out.append({
                "item": n["id"], "number": c["number"], "title": c["title"],
                "url": c["url"], "state": c["state"],
                "labels": [l["name"] for l in c["labels"]["nodes"]],
                "status": sel.get(STATUS_FIELD), "priority": sel.get("Priority"),
                "stage": sel.get(STAGE_FIELD),
            })
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def agent_of(labels):
    found = [l.split(": ", 1)[1] for l in labels if l.startswith("agent: ")]
    if len(found) > 1:
        sys.exit(f"board.py: two `agent:` labels ({', '.join(found)}) — a router must not "
                 "guess which agent spends the money; fix the labels")
    return found[0] if found else ""


PRI_ORDER = {"P0": 0, "P1": 1, "P2": 2, None: 3}


def resolve_status(name):
    """Map a user-supplied column name to the board's exact option name, or exit 1.

    `list` used to compare the raw string, so a case slip or a renamed column returned
    zero rows and exit 0 — indistinguishable from an empty column, which the watcher
    reads as "nothing to do" and reports as a successful quiet tick, forever.
    """
    _, fields = project()
    opts = [o["name"] for o in fields[STATUS_FIELD]["options"]]
    for o in opts:
        if o.lower() == name.lower():
            return o
    sys.exit(f"board.py: no '{STATUS_FIELD}' option {name!r}. Have: " + " | ".join(opts))


def cmd_list(status="In progress"):
    status = resolve_status(status)
    rows = [i for i in items() if i["status"] == status and i["state"] == "OPEN"]
    rows.sort(key=lambda r: (PRI_ORDER.get(r["priority"], 3), r["number"]))
    for r in rows:
        flags = ",".join(sorted(set(r["labels"]) & {"in-flight", "blocked", "steve-decision"})) or "-"
        print(f"{r['number']}\t{r['priority'] or '-'}\t{agent_of(r['labels']) or '-'}\t{flags}\t"
              f"{r['stage'] or '-'}\t{r['title']}")
    return 0


def set_option(issue, field_name, value):
    """Set a single-select field (Status, Stage) on the card to the option named `value`."""
    proj, fields = project()
    f = fields.get(field_name)
    if not f:
        sys.exit(f"board.py: the board has no '{field_name}' field. Have: "
                 + " | ".join(sorted(fields)))
    opt = next((o for o in f["options"] if o["name"].lower() == value.lower()), None)
    if not opt:
        sys.exit(f"board.py: no '{field_name}' option {value!r}. Have: "
                 + " | ".join(o["name"] for o in f["options"]))
    it = next((i for i in items() if i["number"] == issue_num(issue)), None)
    if not it:
        sys.exit(f"board.py: issue #{issue} is not on the board")
    m = """mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){ updateProjectV2ItemFieldValue(input:{
      projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){ projectV2Item{ id } } }"""
    gh(m, p=proj["id"], i=it["item"], f=f["id"], o=opt["id"])
    return opt["name"]


QUEUE_STAGE = "Waiting"


def cmd_queue():
    """Cards Steve dragged into the `Waiting` stage from any column but In progress, Done
    or Stuck — the spawner moves the one it picks to In progress before launching, so
    Status stays truthful and the Stage-grouped board view is the only thing he touches."""
    rows = [i for i in items() if i["state"] == "OPEN" and i["stage"] == QUEUE_STAGE
            and i["status"] not in ("In progress", "Done", "Stuck - Needs help")]
    rows.sort(key=lambda r: (PRI_ORDER.get(r["priority"], 3), r["number"]))
    for r in rows:
        flags = ",".join(sorted(set(r["labels"]) & {"in-flight", "blocked", "steve-decision"})) or "-"
        print(f"{r['number']}\t{r['priority'] or '-'}\t{agent_of(r['labels']) or '-'}\t{flags}\t"
              f"{r['stage'] or '-'}\t{r['title']}")
    return 0


def cmd_move(issue, status):
    name = set_option(issue, STATUS_FIELD, status)
    print(f"#{issue} → {name}")
    return 0


def cmd_stage(issue, stage):
    """What the tick is doing right now: Claimed | Designing | Building | Testing |
    Reviewing | PR open | Merging | Parked. Status says who owns the card; Stage says
    where it is. A board view grouped by Stage shows the columns Steve asked for."""
    name = set_option(issue, STAGE_FIELD, stage)
    print(f"#{issue} stage → {name}")
    return 0


def issue_num(v):
    try:
        return int(str(v).lstrip("#"))
    except ValueError:
        sys.exit(f"board.py: {v!r} is not an issue number")


def cmd_unclaim(issue):
    """Drop a leaked `in-flight` label. A tick that dies mid-claim strands its card."""
    n = issue_num(issue)
    r = subprocess.run(["gh", "issue", "edit", str(n), "--remove-label", "in-flight"],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"board.py: could not unclaim #{n}: {r.stderr.strip()[:200]}")
    print(f"#{n} unclaimed")
    return 0


def cmd_show(issue):
    it = next((i for i in items() if i["number"] == issue_num(issue)), None)
    if not it:
        sys.exit(f"board.py: issue #{issue} is not on the board")
    print(json.dumps(it, indent=2))
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in ("list", "move", "show", "unclaim", "stage", "queue"):
        sys.exit(__doc__)
    try:
        sys.exit({"list": lambda: cmd_list(*a[1:2]),
                  "move": lambda: cmd_move(a[1], a[2]),
                  "stage": lambda: cmd_stage(a[1], a[2]),
                  "queue": lambda: cmd_queue(),
                  "show": lambda: cmd_show(a[1]),
                  "unclaim": lambda: cmd_unclaim(a[1])}[a[0]]())
    except IndexError:
        sys.exit(__doc__)
