#!/usr/bin/env python3
"""Drive the GitHub Projects board from the command line.

The watcher (`/watch`) uses this so a tick never re-derives GraphQL. Fields are
resolved by NAME, not by hard-coded id, so this survives a board edit and can be
copied to another project by changing OWNER/NUMBER.

    python3 scripts/board.py list ["In progress"]   # cards in a column, as TSV
    python3 scripts/board.py move <issue> "<status>" [--why "<one line>"]
    python3 scripts/board.py stage <issue> "<stage>"     # "-" empties it
    python3 scripts/board.py sweep-stages           # empty the Stage on closed cards
    python3 scripts/board.py show <issue>
    python3 scripts/board.py unclaim <issue>        # drop a leaked in-flight label
    python3 scripts/board.py notify <issue> released|breach ["<detail>"]
    python3 scripts/board.py dump                   # every open+closed item, as JSON (one call)
    python3 scripts/board.py ask <issue>             # first line of the newest "Needs Steve" comment, or ""
    python3 scripts/board.py images <issue>          # direct image links under its "Look:" section, one per line
    python3 scripts/board.py images <issue> --all-rounds  # every Needs Steve round, as JSON (bin/floor.py's carousel)

Discord: when DISCORD_WEBHOOK_URL is set (see notify.py), a move to Stuck posts the alert
after it lands (with the ask taken from the card's latest `Needs Steve` comment, or from
`--why`), and `notify` posts the two things that mean something broke (a run that died
holding a card, a product check the detector filed a card for). Every other `move`, and
every `stage`, is silent: the floor page shows those. A failed post is one stderr line,
never a non-zero exit — the board is the record, Discord is the pager.

Every failure exits 1 with a one-line `board.py: ...` message on stderr; only a
successful command exits 0. A caller may therefore treat any non-zero exit as "do not
proceed" — in particular `list` exits 1 for a column name the board does not have,
rather than printing nothing and looking like an empty column.
"""
import json
import os, re, subprocess, sys
from urllib.parse import urlsplit

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
        flags = ",".join(sorted(set(r["labels"]) & {"in-flight", "blocked", "steve-decision", "awaiting-rebuild"})) or "-"
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
    Returns {"url", "state"} or None. One `gh pr list`, so only called for the Stuck
    alert, where the PR is usually the thing Steve has to look at."""
    n = issue_num(issue)
    try:
        r = subprocess.run(["gh", "pr", "list", "--head", f"card-{n}", "--state", "all",
                            "--json", "url,state", "--limit", "1"],
                           capture_output=True, text=True, timeout=20)
        prs = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except Exception:
        return None
    return prs[0] if prs else None


def _comments(issue):
    """Every comment on the card, oldest first, straight from `gh issue view --json
    comments` — the one read both `_latest_needs_steve_comment` and `needs_steve_rounds`
    build on, so there is exactly one reader of "what did Steve get asked", not two that
    could disagree."""
    n = issue_num(issue)
    try:
        r = subprocess.run(["gh", "issue", "view", str(n), "--json", "comments"],
                           capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout).get("comments", []) if r.returncode == 0 else []
    except Exception:
        return []


def _latest_needs_steve_comment(issue):
    """The full body of the card's newest `Needs Steve`-shaped comment, or "" if it has
    none. Shared by `needs_steve_ask` (the first line) and `needs_steve_images` (the
    `**Look:**` section below it) so there is exactly one reader of "which comment is the
    ask", not two that could disagree."""
    for c in reversed(_comments(issue)):
        body = c.get("body") or ""
        first = body.strip().splitlines()
        first = first[0].strip() if first else ""
        if "Needs Steve:" in first:
            return body
    return ""


def needs_steve_ask(issue):
    """The first line of the card's newest `Needs Steve` comment, or "". The tick posts
    that comment before it moves the card to Stuck (watch.md §4), so the alert can carry
    the ask; the spawner passes its own reason with `--why` instead."""
    body = _latest_needs_steve_comment(issue)
    if not body:
        return ""
    return body.strip().splitlines()[0].replace("🤖", "", 1).strip()


# --- the `**Look:**` section: every direct image link under it, lettered by direction ---
#
# The section sits between the `**Look:**` heading and the next bold heading line
# (talking-to-steve.md rule 10: "one link per line, what it is in plain words and then the
# URL"). A regex anchored on the literal word "Question" used to find that end, which broke
# the moment a card asked more than one question (riffmap-site#19's `**Question 1:**` /
# `**Question 2:**`): the terminator never matched, and the "section" ran to the end of the
# comment, swallowing the question text, the reply line and the `<details>` fold with it.
# Scanning for the next line that starts with `**` and is not itself a Look bullet (which
# starts `- **`) stops at any heading, numbered or not, and needs no list of their names.

_IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp)(?:[?#]\S*)?$", re.I)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_BARE_LABELED_RE = re.compile(r"^-?\s*(.+?):\s*(https?://\S+)\s*$")
# A bullet's own leading words naming its direction: "Direction A", "A (recommended)", or
# "A —" / "A -" (em dash, en dash, or a plain hyphen).
_LETTER_PATTERNS = [
    re.compile(r"^Direction\s+([A-Za-z])\b", re.I),
    re.compile(r"^([A-Za-z])\s*\(recommended\)", re.I),
    re.compile(r"^([A-Za-z])\s*[—–-]"),
]
# The same letter, read off a mockup or screenshot's own filename ("invitation-a.html",
# "integrations-a-phone-light.png") when the bullet's words don't say it.
_URL_LETTER_RE = re.compile(r"[-_]([A-Za-z])[-_.]")
MAX_LOOK_IMAGES = 12  # enough for three directions of four shots (card #96)


def _look_section(body):
    """The raw text of the `**Look:**` section: every line after that heading, up to (not
    including) the next line that is itself a bold heading — `**Question:**`,
    `**Question 1:**`, or anything else of that shape. "" if the comment has no such
    heading at all."""
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "**Look:**":
            start = i + 1
            break
    if start is None:
        return ""
    out = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("**") and not stripped.startswith("- "):
            break
        out.append(line)
    return "\n".join(out)


def _look_entries(section_text):
    """Split the section into one block per top-level bullet (a line starting `- `),
    folding any wrapped continuation line (no leading `-`, the designer's own line break
    mid-thought — riffmap-site#18 puts each direction's shot links on the line below its
    bullet) into the bullet above it."""
    entries, cur = [], None
    for line in section_text.splitlines():
        if re.match(r"^\s*-\s", line):
            if cur is not None:
                entries.append(cur)
            cur = line.strip()
        elif cur is not None and line.strip():
            cur += " " + line.strip()
        elif cur is not None:
            entries.append(cur)
            cur = None
    if cur is not None:
        entries.append(cur)
    return entries


def _entry_letter(heading):
    for pat in _LETTER_PATTERNS:
        m = pat.match(heading)
        if m:
            return m.group(1).upper()
    return None


def _url_letter(url):
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    m = _URL_LETTER_RE.search(name)
    return m.group(1).upper() if m else None


def _entry_images(entry):
    """Every direct image link in one bullet block, as `(url, label)` — a markdown link's
    own anchor text as the label, or, for the older one-url-per-bullet shape ("- what it
    is: https://…"), the words before the colon."""
    images, md_spans = [], []
    for m in _MD_LINK_RE.finditer(entry):
        md_spans.append((m.start(), m.end()))
        label, url = m.group(1).strip(), m.group(2)
        if _IMAGE_EXT_RE.search(url):
            images.append((url, label))
    remainder = entry
    for start, end in sorted(md_spans, reverse=True):
        remainder = remainder[:start] + remainder[end:]
    remainder = remainder.strip()
    labeled = _BARE_LABELED_RE.match(remainder)
    if labeled:
        label, url = labeled.group(1).strip(" *"), labeled.group(2).rstrip(").,;:")
        if _IMAGE_EXT_RE.search(url):
            images.append((url, label))
    else:
        heading = entry.lstrip("- ").lstrip("*").strip()
        for m in _BARE_URL_RE.finditer(remainder):
            url = m.group(0).rstrip(").,;:")
            if _IMAGE_EXT_RE.search(url):
                images.append((url, heading))
    return images


def _parse_look_images(body, max_images=MAX_LOOK_IMAGES):
    """Every direct image link under `body`'s `**Look:**` section, each tagged with the
    direction letter and recommendation its own bullet carries — deduplicated, ordered A's
    then B's then C's then anything unlettered, capped at `max_images`. A picture that
    matches no letter sits last, unlabeled (card #96)."""
    out, seen = [], set()
    for entry in _look_entries(_look_section(body)):
        heading = entry.lstrip("- ").lstrip("*").strip()
        letter = _entry_letter(heading)
        recommended = "(recommended)" in entry.lower()
        images = _entry_images(entry)
        if letter is None:
            for url, _ in images:
                letter = _url_letter(url)
                if letter:
                    break
        for url, label in images:
            if url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "label": label, "letter": letter, "recommended": recommended})
    out.sort(key=lambda d: (0, d["letter"]) if d["letter"] else (1, ""))
    return out[:max_images]


def needs_steve_images(issue):
    """Direct image links — a mockup or a screenshot — under the newest `Needs Steve`
    comment's `**Look:**` section (card #91: bin/floor.py's inline preview, so a reader
    never has to open the card to find the picture its own ask is about)."""
    return [d["url"] for d in _parse_look_images(_latest_needs_steve_comment(issue))]


def needs_steve_rounds(issue):
    """Every `Needs Steve`-shaped comment — the same ones `_latest_needs_steve_comment`
    picks from, not only ones with a formal `**Question:**` heading (riffmap-site#18 asks
    with a plain "Reply with a letter…" line and still carries three directions of
    pictures) — newest first, as `{"when": <ISO timestamp>, "images": [{"url", "label",
    "letter", "recommended"}, …]}`. A status note with nothing to look at, like
    riffmap-site#3's "still blocked, no action needed from you", is a round with an empty
    `images` list, not one left out. For a reader that wants every round, not just the
    last (bin/floor.py's carousel, card #96's own `--all-rounds`)."""
    rounds = []
    for c in _comments(issue):
        body = c.get("body") or ""
        first = body.strip().splitlines()
        first = first[0].strip() if first else ""
        if "Needs Steve:" not in first:
            continue
        rounds.append({"when": c.get("createdAt") or "", "images": _parse_look_images(body)})
    rounds.sort(key=lambda r: r["when"], reverse=True)
    return rounds


def announce(kind, card, detail="", with_pr=False):
    if not notify.enabled():
        return
    pr = pr_for(card["number"]) if with_pr else None
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
        flags = ",".join(sorted(set(r["labels"]) & {"in-flight", "blocked", "steve-decision", "awaiting-rebuild"})) or "-"
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
    # Stuck is the only column that reaches Discord. Every other move is on the floor page.
    if name == STUCK_STATUS:
        announce("stuck", it, detail=why or needs_steve_ask(issue), with_pr=True)
    return 0


CLEAR_FIELD = """mutation($p:ID!,$i:ID!,$f:ID!){ clearProjectV2ItemFieldValue(input:{
  projectId:$p,itemId:$i,fieldId:$f}){ projectV2Item{ id } } }"""


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
        gh(CLEAR_FIELD, p=proj["id"], i=it["item"], f=f["id"])
    print(f"#{issue} stage → (none)")
    return 0


# How many stale Stages one `sweep-stages` run empties. A board that predates the sweep
# can hold a hundred of them, and a hundred mutations in a row is what GitHub's secondary
# limit is for; the rest go on the next run.
SWEEP_MAX = 20


def cmd_sweep_stages():
    """Empty the Stage on every CLOSED card that still has one. `clear_stage` only runs
    for the card a tick was holding when it closed; a card that closes with no tick on it
    — Steve merging a parked card's PR, or closing a card by hand — kept its last Stage
    for good and sat in Done reading "Parked" (watcher#35, #36, #40, #43 and #44,
    September 28, 2026). The spawner calls this every few minutes. Quiet — no Discord
    post. One `items()` read; nothing else is fetched unless a card needs clearing."""
    stale = [i for i in items() if i["state"] == "CLOSED" and i.get("stage")]
    if not stale:
        return 0
    proj, fields = project()
    f = fields.get(STAGE_FIELD)
    if not f:
        return 0
    for it in stale[:SWEEP_MAX]:
        gh(CLEAR_FIELD, p=proj["id"], i=it["item"], f=f["id"])
        print(f"#{it['number']} stage {it['stage']} → (none)")
    return 0


def cmd_stage(issue, stage):
    """What the tick is doing right now: Claimed | Designing | Building | Testing |
    Reviewing | PR open | Merging | Parked. Status says who owns the card; Stage says
    where it is. A board view grouped by Stage shows the columns Steve asked for, and the
    floor page shows it on the lane's desk. Quiet — no Discord post. `-` empties it
    (clear_stage)."""
    if stage == "-":
        return clear_stage(issue)
    name, it = set_option(issue, STAGE_FIELD, stage)
    print(f"#{issue} stage → {name}")
    return 0


def cmd_notify(issue, kind, detail=""):
    """Post an event that is not a move to Stuck but still means something broke:
    `released` (a run died holding the card; the spawner's exit handling) or `breach`
    (the product detector filed the card for a large breach; bin/detect.sh). Exits 0
    whether or not Discord is configured or reachable."""
    if kind not in ("released", "breach"):
        sys.exit(f"board.py: notify: unknown event {kind!r} (released | breach)")
    if not notify.enabled():
        return 0
    try:
        it = next((i for i in items() if i["number"] == issue_num(issue)), None)
    except SystemExit:  # a board outage must not fail a notification; post without a title
        it = None
    if not it:
        it = {"number": issue_num(issue), "title": "", "url": ""}
    announce(kind, it, detail=detail)
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


def cmd_dump():
    """Every open and closed item on the board, as one JSON array — the same shape `show`
    prints for one card. One `items()` call, so a caller that wants several fields across
    the whole board (bin/floor.py: per-project counts, and every lane's current card and
    Stage, from one board) never has to run `list` once per column."""
    print(json.dumps(items(), indent=2))
    return 0


def cmd_ask(issue):
    """The first line of the card's newest `Needs Steve` comment, or "" if it has none —
    `needs_steve_ask` on its own, for a caller that only wants the ask (bin/floor.py's
    Needs Steve pill) without moving the card."""
    print(needs_steve_ask(issue))
    return 0


def cmd_images(issue, *rest):
    """One image URL per line (board.py's `needs_steve_images`) — the same comment `ask`
    reads, for a caller that wants the mockup or screenshot it points at (bin/floor.py's
    inline preview, card #91) without moving the card. `--all-rounds` prints
    `needs_steve_rounds` instead, as one JSON array, for a caller that wants every round's
    pictures, lettered by direction (bin/floor.py's carousel, card #96)."""
    if rest == ("--all-rounds",):
        print(json.dumps(needs_steve_rounds(issue)))
        return 0
    if rest:
        sys.exit(f"board.py: images: unexpected arguments {list(rest)!r}")
    for u in needs_steve_images(issue):
        print(u)
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in ("list", "move", "show", "unclaim", "stage", "sweep-stages", "queue", "notify",
                            "dump", "ask", "images"):
        sys.exit(__doc__)
    try:
        sys.exit({"list": lambda: cmd_list(*a[1:2]),
                  "move": lambda: cmd_move(a[1], a[2], *a[3:]),
                  "stage": lambda: cmd_stage(a[1], a[2]),
                  "sweep-stages": lambda: cmd_sweep_stages(),
                  "queue": lambda: cmd_queue(),
                  "show": lambda: cmd_show(a[1]),
                  "notify": lambda: cmd_notify(a[1], a[2], *a[3:4]),
                  "unclaim": lambda: cmd_unclaim(a[1]),
                  "dump": lambda: cmd_dump(),
                  "ask": lambda: cmd_ask(a[1]),
                  "images": lambda: cmd_images(a[1], *a[2:])}[a[0]]())
    except IndexError:
        sys.exit(__doc__)
