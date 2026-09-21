#!/usr/bin/env python3
"""Drive the GitHub Projects board from the command line.

The watcher (`/watch`) uses this so a tick never re-derives GraphQL. Fields are
resolved by NAME, not by hard-coded id, so this survives a board edit and can be
copied to another project by changing OWNER/NUMBER.

    python3 scripts/board.py list ["In progress"]   # cards in a column, as TSV
    python3 scripts/board.py move <issue> "<status>" [--why "<one line>"]
    python3 scripts/board.py stage <issue> "<stage>"     # "-" empties it
    python3 scripts/board.py show <issue>
    python3 scripts/board.py unclaim <issue>        # drop a leaked in-flight label
    python3 scripts/board.py notify <issue> shipped|closed|released ["<detail>"]

Discord: when DISCORD_WEBHOOK_URL is set (see notify.py), `move` and `stage` post the
change to the channel after it lands, a move to Stuck posts the alert (with the ask taken
from the card's latest `Needs Steve` comment, or from `--why`), and `notify` posts the
events only the spawner sees (a card shipped, a run that died). A failed post is one
stderr line, never a non-zero exit — the board is the record, Discord is the pager.

Every failure exits 1 with a one-line `board.py: ...` message on stderr; only a
successful command exits 0. A caller may therefore treat any non-zero exit as "do not
proceed" — in particular `list` exits 1 for a column name the board does not have,
rather than printing nothing and looking like an empty column.
"""
import json
import os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify  # noqa: E402  (bin/notify.py, beside this file)

OWNER = os.environ.get("BOARD_OWNER") or "roloc"
NUMBER = int(os.environ.get("BOARD_NUMBER") or 1)
STAGE_FIELD = "Stage"
STATUS_FIELD = "Status"
STUCK_STATUS = "Stuck - Needs help"
BOARD_URL = f"https://github.com/users/{OWNER}/projects/{NUMBER}"


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
    return opt["name"], it


# --- Discord ---------------------------------------------------------------------------
# Every function here is best-effort: a lookup that fails returns nothing and the post
# goes out without that detail. None of them may raise past the board command.

def pr_for(issue):
    """The card's pull request, found by its branch (`card-<n>`, worktree.sh's naming).
    Returns {"url", "state"} or None. One `gh pr list`, so only called for the events
    where a PR link is worth showing."""
    n = issue_num(issue)
    try:
        r = subprocess.run(["gh", "pr", "list", "--head", f"card-{n}", "--state", "all",
                            "--json", "url,state", "--limit", "1"],
                           capture_output=True, text=True, timeout=20)
        prs = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except Exception:
        return None
    return prs[0] if prs else None


def needs_steve_ask(issue):
    """The first line of the card's newest `Needs Steve` comment, or "". The tick posts
    that comment before it moves the card to Stuck (watch.md §4), so the alert can carry
    the ask; the spawner passes its own reason with `--why` instead."""
    n = issue_num(issue)
    try:
        r = subprocess.run(["gh", "issue", "view", str(n), "--json", "comments"],
                           capture_output=True, text=True, timeout=20)
        comments = json.loads(r.stdout).get("comments", []) if r.returncode == 0 else []
    except Exception:
        return ""
    for c in reversed(comments):
        first = (c.get("body") or "").strip().splitlines()
        first = first[0].strip() if first else ""
        if "Needs Steve:" in first:
            return first.replace("🤖", "", 1).strip()
    return ""


def announce(kind, card, detail="", with_pr=False, pr=None):
    if not notify.enabled():
        return
    if with_pr and pr is None:
        pr = pr_for(card["number"])
    notify.post(kind, card, detail=detail, pr=pr,
                board_url=BOARD_URL if kind == "stuck" else "")


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


def cmd_move(issue, status, *rest):
    """`move <issue> "<status>" [--why "<one line>"]`. `--why` is only read for a move to
    Stuck: it becomes the ask in the Discord alert (the spawner uses it, since its own
    comment goes out after the move). Without it the alert quotes the card's newest
    `Needs Steve` comment."""
    why = ""
    rest = list(rest)
    if rest and rest[0] == "--why":
        why = rest[1] if len(rest) > 1 else ""
        rest = rest[2:]
    if rest:
        sys.exit(f"board.py: move: unexpected arguments {rest!r}")
    name, it = set_option(issue, STATUS_FIELD, status)
    print(f"#{issue} → {name}")
    if name == STUCK_STATUS:
        announce("stuck", it, detail=why or needs_steve_ask(issue), with_pr=True)
    else:
        announce("status", it, detail=name, with_pr=name in ("In review", "Done"))
    return 0


# Stage values whose Discord post would only repeat a status move that already posted.
QUIET_STAGES = {"Parked"}


def clear_stage(issue):
    """Empty the card's Stage. The spawner calls it when a card closes: a Done card still
    reading "Merging" or "Claimed" sits in the wrong group on the Stage-grouped view Steve
    works from (riffmap#425, #454, September 21, 2026). Quiet — no Discord post."""
    proj, fields = project()
    f = fields.get(STAGE_FIELD)
    if not f:
        return 0
    it = next((i for i in items() if i["number"] == issue_num(issue)), None)
    if not it:
        sys.exit(f"board.py: issue #{issue} is not on the board")
    if it.get("stage"):
        m = """mutation($p:ID!,$i:ID!,$f:ID!){ clearProjectV2ItemFieldValue(input:{
          projectId:$p,itemId:$i,fieldId:$f}){ projectV2Item{ id } } }"""
        gh(m, p=proj["id"], i=it["item"], f=f["id"])
    print(f"#{issue} stage → (none)")
    return 0


def cmd_stage(issue, stage):
    """What the tick is doing right now: Claimed | Designing | Building | Testing |
    Reviewing | PR open | Merging | Parked. Status says who owns the card; Stage says
    where it is. A board view grouped by Stage shows the columns Steve asked for.
    `-` empties it (clear_stage)."""
    if stage == "-":
        return clear_stage(issue)
    name, it = set_option(issue, STAGE_FIELD, stage)
    print(f"#{issue} stage → {name}")
    if name not in QUIET_STAGES:
        announce("stage", it, detail=name, with_pr=name in ("PR open", "Merging"))
    return 0


def cmd_notify(issue, kind, detail=""):
    """Post an event the board itself never sees (the spawner's exit handling):
    `shipped` (closed by a merged PR), `closed` (closed without one), `released` (a run
    died holding the card). Exits 0 whether or not Discord is configured or reachable."""
    if kind not in ("shipped", "closed", "released"):
        sys.exit(f"board.py: notify: unknown event {kind!r} (shipped | closed | released)")
    if not notify.enabled():
        return 0
    try:
        it = next((i for i in items() if i["number"] == issue_num(issue)), None)
    except SystemExit:  # a board outage must not fail a notification; post without a title
        it = None
    if not it:
        it = {"number": issue_num(issue), "title": "", "url": ""}
    pr = None
    if kind in ("shipped", "closed"):
        pr = pr_for(issue)
        # The spawner only knows the card closed. "Shipped" means a merged PR did it;
        # a card Steve closed by hand (option C on a park) is reported as what it is.
        if kind == "shipped" and (pr or {}).get("state") != "MERGED":
            kind = "closed"
    announce(kind, it, detail=detail, pr=pr)
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
    if not a or a[0] not in ("list", "move", "show", "unclaim", "stage", "queue", "notify"):
        sys.exit(__doc__)
    try:
        sys.exit({"list": lambda: cmd_list(*a[1:2]),
                  "move": lambda: cmd_move(a[1], a[2], *a[3:]),
                  "stage": lambda: cmd_stage(a[1], a[2]),
                  "queue": lambda: cmd_queue(),
                  "show": lambda: cmd_show(a[1]),
                  "notify": lambda: cmd_notify(a[1], a[2], *a[3:4]),
                  "unclaim": lambda: cmd_unclaim(a[1])}[a[0]]())
    except IndexError:
        sys.exit(__doc__)
