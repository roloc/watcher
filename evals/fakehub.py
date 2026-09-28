#!/usr/bin/env python3
"""The fake GitHub + board a replay case runs a real `claude -p /watch <n>` against.

`bin/eval.sh` points a case's `gh` and `board` (the two stub scripts in
`evals/stub-bin/`) at one instance of this file via `FAKEHUB_STATE` (a JSON store: issues,
PRs, label set) and `FAKEHUB_ORIGIN` (a real bare git repo standing in for GitHub, so
`git push`, `gh pr merge`'s squash and the additive-conflict cases run on real git, not a
guess about what git would do). No network, ever: everything here is a local file and a
local repo, and any `gh`/`board` shape neither stub script nor this file recognizes is a
loud failure, never a silent success and never a fall-through to the real `gh` (see the
stub scripts) — a tick under test runs with real GitHub credentials sitting right there on
the box, and this is the only thing standing between it and the real roloc/watcher repo.

    python3 fakehub.py gh <argv...>       # a `gh` subcommand, argv exactly as typed
    python3 fakehub.py board <argv...>    # a `board` subcommand, same

Every call is appended to $FAKEHUB_LOG as one line (newlines in a body collapsed to
spaces), the same convention `bin/test-spawn.sh` uses, so a case's `expect()` can grep the
whole thing on one line.
"""
import json
import os
import re
import shutil
import subprocess
import sys

STATE_PATH = os.environ["FAKEHUB_STATE"]
ORIGIN = os.environ["FAKEHUB_ORIGIN"]
LOG_PATH = os.environ["FAKEHUB_LOG"]
CI_FAIL_ONCE = os.environ.get("FAKEHUB_CI_FAIL_ONCE") == "1"


def log(*argv):
    with open(LOG_PATH, "a") as f:
        f.write(re.sub(r"\s+", " ", " ".join(argv)) + "\n")


def load():
    with open(STATE_PATH) as f:
        return json.load(f)


def save(d):
    with open(STATE_PATH, "w") as f:
        json.dump(d, f, indent=2)


def die(msg):
    sys.stderr.write(f"fakehub: {msg}\n")
    sys.exit(1)


def issue(d, n):
    it = d["issues"].get(str(n))
    if not it:
        die(f"no such issue #{n}")
    return it


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        die(f"git failed: {' '.join(cmd)}\n{r.stderr}")
    return r


# --- flag parsing (gh's own CLI shape: --flag value, --flag=value, bare positionals) ------

def parse_flags(argv, known_bool=()):
    """Returns (positionals, {flag: value}). `--flag`/`-f` with no value and no `=` is
    treated as boolean-true when it is in `known_bool`. Handles both `gh`'s long flags
    (`--body-file x`) and its short ones (`gh api -X POST -F issue_id=1`, both used by
    watch.md's/triage.md's dependency-recording call) — a short flag repeated (`-F` can
    appear more than once) just overwrites; nothing here needs more than one value back."""
    pos, flags = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            name = a[2:]
            if "=" in name:
                name, val = name.split("=", 1)
                flags[name] = val
            elif name in known_bool:
                flags[name] = True
            else:
                i += 1
                flags[name] = argv[i] if i < len(argv) else ""
        elif a.startswith("-") and len(a) > 1 and a != "-":
            name = a[1:]
            if name in known_bool:
                flags[name] = True
            else:
                i += 1
                flags[name] = argv[i] if i < len(argv) else ""
        else:
            pos.append(a)
        i += 1
    return pos, flags


def body_of(flags):
    if "body-file" in flags:
        with open(flags["body-file"]) as f:
            return f.read()
    return flags.get("body", "")


# --- gh issue ------------------------------------------------------------------------------

def gh_issue_comment(argv):
    pos, flags = parse_flags(argv)
    n = pos[0]
    it = issue(load(), n)
    d = load()
    it = issue(d, n)
    it["comments"].append(body_of(flags))
    save(d)


def gh_issue_edit(argv):
    pos, flags = parse_flags(argv)
    n = pos[0]
    d = load()
    it = issue(d, n)
    if "add-label" in flags:
        if flags["add-label"] not in it["labels"]:
            it["labels"].append(flags["add-label"])
    if "remove-label" in flags:
        it["labels"] = [l for l in it["labels"] if l != flags["remove-label"]]
    save(d)


FIELD_MAP = {
    "labels": lambda it: [{"name": l} for l in it["labels"]],
    "comments": lambda it: [{"body": c} for c in it["comments"]],
    "body": lambda it: it["body"],
    "title": lambda it: it["title"],
    "state": lambda it: it["state"],
}


def gh_issue_view(argv):
    pos, flags = parse_flags(argv)
    n = pos[0]
    it = issue(load(), n)
    fields = flags.get("json", "").split(",") if flags.get("json") else list(FIELD_MAP)
    print(json.dumps({f: FIELD_MAP[f.strip()](it) for f in fields if f.strip() in FIELD_MAP}))


def gh_issue_create(argv):
    pos, flags = parse_flags(argv)
    d = load()
    n = d["next_issue"]
    d["next_issue"] += 1
    labels = flags.get("label", "")
    d["issues"][str(n)] = {
        "number": n, "title": flags.get("title", ""), "body": body_of(flags),
        "state": "OPEN", "labels": [labels] if labels else [], "status": "Backlog",
        "stage": "-", "priority": "-", "comments": [],
    }
    save(d)
    print(f"file://{ORIGIN}/issues/{n}")


def gh_issue_list(argv):
    # A defensive lookup ("has this already been filed?"). A fresh fixture truthfully has
    # none, so an empty match list is the correct answer, not a shortcut.
    print("[]")


# --- gh pr ---------------------------------------------------------------------------------

def current_branch(cwd):
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).stdout.strip()


def gh_pr_create(argv, cwd):
    pos, flags = parse_flags(argv)
    d = load()
    n = d["next_pr"]
    d["next_pr"] += 1
    head = flags.get("head") or current_branch(cwd)
    base = flags.get("base") or "main"
    d["prs"][str(n)] = {
        "number": n, "title": flags.get("title", ""), "body": body_of(flags),
        "head": head, "base": base, "state": "OPEN", "url": f"file://{ORIGIN}/pull/{n}",
        "comments": [],
    }
    save(d)
    print(f"file://{ORIGIN}/pull/{n}")


PR_FIELD_MAP = {
    "number": lambda pr: pr["number"], "url": lambda pr: pr["url"],
    "body": lambda pr: pr["body"], "title": lambda pr: pr["title"],
    "state": lambda pr: pr["state"], "headRefName": lambda pr: pr["head"],
}


def gh_pr_list(argv, cwd):
    pos, flags = parse_flags(argv)
    d = load()
    prs = list(d["prs"].values())
    if "head" in flags:
        prs = [p for p in prs if p["head"] == flags["head"]]
    state = flags.get("state", "open")
    if state != "all":
        want = {"open": "OPEN", "merged": "MERGED", "closed": "CLOSED"}[state]
        prs = [p for p in prs if p["state"] == want]
    if "limit" in flags:
        prs = prs[: int(flags["limit"])]
    fields = flags.get("json", "").split(",") if flags.get("json") else list(PR_FIELD_MAP)
    print(json.dumps([{f: PR_FIELD_MAP[f.strip()](p) for f in fields if f.strip() in PR_FIELD_MAP} for p in prs]))


def gh_pr_comment(argv, cwd):
    pos, flags = parse_flags(argv)
    d = load()
    if pos and pos[0].lstrip("#").isdigit():
        pr = d["prs"][pos[0].lstrip("#")]
    else:
        branch = current_branch(cwd)
        cands = [p for p in d["prs"].values() if p["head"] == branch]
        if not cands:
            die(f"no pull request found for branch {branch!r}")
        pr = cands[0]
    pr.setdefault("comments", []).append(body_of(flags))
    save(d)


def gh_pr_checks(argv):
    if CI_FAIL_ONCE and not os.environ.get("FAKEHUB_CI_FAILED_ONCE_MARKER_SEEN"):
        # Not wired to a real second run across processes; today's cases never set
        # FAKEHUB_CI_FAIL_ONCE. Left explicit rather than silently unsupported.
        die("some checks were not successful (simulated)")
    print("All checks were successful")


CLOSES_RE = re.compile(
    r"\b(?:closes?|fixes?|resolves?)\s*:?\s*(?:\[#(\d+)\]\([^)]*\)|#(\d+))", re.I
)


def closed_issue_numbers(body):
    return [int(a or b) for a, b in CLOSES_RE.findall(body)]


def gh_pr_merge(argv, cwd):
    pos, flags = parse_flags(argv, known_bool=("squash", "delete-branch"))
    d = load()
    if pos:
        pr = d["prs"][pos[0].lstrip("#")]
    else:
        branch = current_branch(cwd)
        cands = [p for p in d["prs"].values() if p["head"] == branch and p["state"] == "OPEN"]
        if not cands:
            die(f"no open pull requests found for branch {branch!r}")
        pr = cands[0]

    # A unique scratch dir per attempt, always removed in `finally` below: a refused merge
    # (conflict) must not leave a directory behind that the tick's own NEXT `gh pr merge` —
    # after it resolves the conflict on its own branch and pushes again — would collide with.
    scratch = STATE_PATH + f".merge-scratch-{pr['number']}-{os.getpid()}-{id(pr)}"
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        run(["git", "clone", "-q", ORIGIN, scratch])
        run(["git", "checkout", "-q", pr["base"]], cwd=scratch)
        r = run(["git", "merge", "--squash", f"origin/{pr['head']}"], cwd=scratch, check=False)
        if r.returncode:
            run(["git", "merge", "--abort"], cwd=scratch, check=False)
            die(f"pull request #{pr['number']} is not mergeable: the base branch has moved "
                "and this branch has not merged it (simulated GitHub mergeability check)")
        msg = pr["title"] + ("\n\n" + pr["body"] if pr["body"] else "")
        run(["git", "-c", "user.email=fakehub@example.com", "-c", "user.name=fakehub",
             "commit", "-q", "-m", msg], cwd=scratch)
        run(["git", "push", "-q", "origin", f"HEAD:{pr['base']}"], cwd=scratch)
        if "delete-branch" in flags:
            run(["git", "push", "-q", "origin", "--delete", pr["head"]], cwd=scratch, check=False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    pr["state"] = "MERGED"
    for n in closed_issue_numbers(pr["body"]):
        it = d["issues"].get(str(n))
        if it:
            it["state"] = "CLOSED"
            it["status"] = "Done"
    save(d)
    print(f"Merged pull request #{pr['number']} ({pr['head']} -> {pr['base']})")


# --- gh api (the dependency-recording net, kept minimal) ------------------------------------

def gh_api(argv):
    pos, flags = parse_flags(argv)  # -X takes a value (POST); it is not a boolean flag
    path = pos[0] if pos else ""
    m = re.match(r"repos/[^/]+/[^/]+/issues/(\d+)/dependencies/blocked_by", path)
    if m and flags.get("X") == "POST":
        print("[]")  # recorded; nothing reads this back within a case
        return
    m = re.match(r"repos/[^/]+/[^/]+/issues/(\d+)$", path)
    if m:
        it = issue(load(), m.group(1))
        jq = flags.get("jq", "")
        if jq == ".body":
            print(it["body"])
        elif jq == ".state":
            print(it["state"].lower())
        elif jq == ".id":
            print(f"issue-node-{it['number']}")
        else:
            print(json.dumps(it))
        return
    die(f"unhandled gh api call: {path}")


def gh_label_list(argv):
    # No `agent: *` label in this fixture's set — no design lane to route to (watch.md §2,
    # "never route in a project with no design agent to route to").
    for name in ("bug", "enhancement", "in-flight"):
        print(name)


GH_DISPATCH = {
    ("issue", "comment"): lambda argv, cwd: gh_issue_comment(argv),
    ("issue", "edit"): lambda argv, cwd: gh_issue_edit(argv),
    ("issue", "view"): lambda argv, cwd: gh_issue_view(argv),
    ("issue", "create"): lambda argv, cwd: gh_issue_create(argv),
    ("issue", "list"): lambda argv, cwd: gh_issue_list(argv),
    ("pr", "create"): lambda argv, cwd: gh_pr_create(argv, cwd),
    ("pr", "list"): lambda argv, cwd: gh_pr_list(argv, cwd),
    ("pr", "comment"): lambda argv, cwd: gh_pr_comment(argv, cwd),
    ("pr", "checks"): lambda argv, cwd: gh_pr_checks(argv),
    ("pr", "merge"): lambda argv, cwd: gh_pr_merge(argv, cwd),
    ("label", "list"): lambda argv, cwd: gh_label_list(argv),
    ("api", None): lambda argv, cwd: gh_api(argv),
}


def dispatch_gh(argv, cwd):
    if not argv:
        die("empty gh call")
    if argv[0] == "api":
        return gh_api(argv[1:])
    if len(argv) < 2:
        die(f"unhandled gh command: {argv}")
    key = (argv[0], argv[1])
    fn = GH_DISPATCH.get(key)
    if not fn:
        die(f"unhandled gh command: {' '.join(argv[:2])} — fakehub does not simulate this; "
            "either the case does not need it or fakehub.py needs a handler")
    fn(argv[2:], cwd)


# --- board -----------------------------------------------------------------------------

PRI_ORDER = {"P0": 0, "P1": 1, "P2": 2, "-": 3}


def board_list(argv):
    status = argv[0] if argv else "In progress"
    d = load()
    rows = [i for i in d["issues"].values() if i["status"] == status and i["state"] == "OPEN"]
    rows.sort(key=lambda r: (PRI_ORDER.get(r["priority"], 3), r["number"]))
    for r in rows:
        flags = ",".join(sorted(set(r["labels"]) & {"in-flight", "blocked", "steve-decision"})) or "-"
        agent = next((l.split(": ", 1)[1] for l in r["labels"] if l.startswith("agent: ")), "-")
        print(f"{r['number']}\t{r['priority']}\t{agent}\t{flags}\t{r['stage']}\t{r['title']}")


def board_show(argv):
    it = issue(load(), argv[0])
    print(json.dumps({
        "item": f"item-{it['number']}", "number": it["number"], "title": it["title"],
        "url": f"file://{ORIGIN}/issues/{it['number']}", "state": it["state"],
        "labels": it["labels"], "status": it["status"], "priority": it["priority"],
        "stage": it["stage"],
    }, indent=2))


def board_move(argv):
    n = argv[0]
    status = argv[1]
    d = load()
    it = issue(d, n)
    it["status"] = status
    save(d)
    print(f"#{n} → {status}")


def board_stage(argv):
    n, stage = argv[0], argv[1]
    d = load()
    it = issue(d, n)
    it["stage"] = "-" if stage == "-" else stage
    save(d)
    print(f"#{n} stage → {it['stage']}")


def board_unclaim(argv):
    n = argv[0]
    d = load()
    it = issue(d, n)
    it["labels"] = [l for l in it["labels"] if l != "in-flight"]
    save(d)
    print(f"#{n} unclaimed")


def board_notify(argv):
    pass  # Discord is out of scope for a replay case; logged like everything else.


BOARD_DISPATCH = {
    "list": board_list, "show": board_show, "move": board_move,
    "stage": board_stage, "unclaim": board_unclaim, "notify": board_notify,
    "queue": lambda argv: None,
}


def dispatch_board(argv):
    if not argv or argv[0] not in BOARD_DISPATCH:
        die(f"unhandled board command: {argv}")
    BOARD_DISPATCH[argv[0]](argv[1:])


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    rest = sys.argv[2:]
    log(kind, *rest)
    cwd = os.environ.get("FAKEHUB_CWD", os.getcwd())
    if kind == "gh":
        dispatch_gh(rest, cwd)
    elif kind == "board":
        dispatch_board(rest)
    else:
        die(f"usage: fakehub.py gh|board <args...> (got {kind!r})")
