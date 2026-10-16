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
    "baseRefName": lambda pr: pr["base"],
    "comments": lambda pr: [{"body": c} for c in pr.get("comments", [])],
    "headRefOid": lambda pr: run(["git", "-C", ORIGIN, "rev-parse", f"refs/heads/{pr['head']}"],
                                 check=False).stdout.strip(),
}


def _pr_by_ref(d, ref, cwd):
    """A PR by number ("1", "#1"), by head branch ("card-100"), by its own url, or — no ref
    at all — the one for the current branch: the lookups gh's own `pr view` and `pr diff`
    accept."""
    if ref is None:
        return _find_pr(d, [], cwd)
    r = ref.lstrip("#")
    if r.isdigit():
        pr = d["prs"].get(r)
        if not pr:
            die(f"no pull request #{r}")
        return pr
    for p in d["prs"].values():
        if p["head"] == ref or p["url"] == ref:
            return p
    die(f"no pull request found for {ref!r}")


def gh_pr_view(argv, cwd):
    # `gh pr view [<n>|<branch>|<url>] --json <fields> [--jq .<field>]`: what
    # commands/watch.md §3 and agents/reviewer.md name as the PR body's ground truth
    # (`--json body`), and what a tick reaches for to learn its own PR's number or url.
    # Until October 15, 2026 this was unhandled, so every such call died — and `reviewer`,
    # unable to read the body, blocked "plausibly" on a verify block it could not see: one
    # more round in a case already at the suite's ceilings (cards #170, #172).
    pos, flags = parse_flags(argv)
    pr = _pr_by_ref(load(), pos[0] if pos else None, cwd)
    if "json" not in flags:
        print(f"{pr['title']}\n{pr['state']} · {pr['head']} -> {pr['base']}\n\n{pr['body']}")
        return
    fields = [f.strip() for f in flags["json"].split(",") if f.strip()]
    unknown = [f for f in fields if f not in PR_FIELD_MAP]
    if unknown:
        die(f"gh pr view --json: fakehub does not simulate field(s) {unknown}")
    out = {f: PR_FIELD_MAP[f](pr) for f in fields}
    jq = flags.get("jq", "").strip()
    if jq:
        m = re.fullmatch(r"\.(\w+)", jq)
        if not m or m.group(1) not in out:
            die(f"gh pr view --jq: fakehub only simulates `.<field>` over the fields asked for, not {jq!r}")
        v = out[m.group(1)]
        print(v if isinstance(v, str) else json.dumps(v))
        return
    print(json.dumps(out))


def gh_pr_diff(argv, cwd):
    # `gh pr diff [<n>|<branch>] [--name-only]`: the PR's diff against its base, from the
    # bare origin — both refs are there once the branch is pushed, which is what makes it a
    # PR at all. What `reviewer` reads when it wants the change and not the author's account.
    pos, flags = parse_flags(argv, known_bool=("name-only",))
    pr = _pr_by_ref(load(), pos[0] if pos else None, cwd)
    cmd = ["git", "-C", ORIGIN, "diff"]
    if "name-only" in flags:
        cmd.append("--name-only")
    cmd.append(f"{pr['base']}...{pr['head']}")
    print(run(cmd).stdout, end="")


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


def gh_pr_edit(argv, cwd):
    pos, flags = parse_flags(argv)
    d = load()
    if pos and pos[0].lstrip("#").isdigit():
        n = pos[0].lstrip("#")
        if n not in d["prs"]:
            die(f"no such pull request #{n}")
        pr = d["prs"][n]
    else:
        branch = current_branch(cwd)
        cands = [p for p in d["prs"].values() if p["head"] == branch and p["state"] == "OPEN"]
        if not cands:
            die(f"no open pull requests found for branch {branch!r}")
        pr = cands[0]
    if "body" in flags or "body-file" in flags:
        pr["body"] = body_of(flags)
    if "title" in flags:
        pr["title"] = flags["title"]
    save(d)
    print(pr["url"])


# --- review-receipt (card #142) --------------------------------------------------------
# A Python-only re-implementation of bin/receipt-check.sh's own decision, close enough for
# the replay suite: the real script only ever runs inside real GitHub Actions, never here.
# `gh pr checks`'s simulated `review-receipt` bucket is recomputed fresh on every call —
# never cached — so a rerun (`gh run rerun`, below) has nothing to actually do: the next
# `gh pr checks` call already reflects whatever comment exists by then.

RECEIPT_GATE_DEFAULT = ("commands/watch.md agents/reviewer.md agents/usability.md "
                         "docs/how-it-works.md .github/workflows deploy evals bin/eval.sh")


def _changed_files(cwd):
    r = run(["git", "diff", "--name-only", "origin/main...HEAD"], cwd=cwd, check=False)
    return [l for l in r.stdout.splitlines() if l]


def _path_in(f, paths):
    return any(f == p or f.startswith(p + "/") for p in paths)


def review_receipt_bucket(pr, sha, cwd):
    gate = os.environ.get("RECEIPT_GATE_PATHS", RECEIPT_GATE_DEFAULT).split()
    screens = os.environ.get("RECEIPT_SCREEN_PATHS", "").split()
    files = _changed_files(cwd)
    needs_review = any(not f.endswith(".md") or _path_in(f, gate) for f in files)
    if not needs_review:
        return "pass"
    comments = pr.get("comments", [])
    reviewer_lines = [c for c in comments if c.startswith("🤖 reviewer:")]
    if not reviewer_lines or f"Reviewed {sha}: no blocking findings" not in reviewer_lines[-1]:
        return "fail"
    needs_usability = any(_path_in(f, screens) for f in files) or "## Screens" in (pr.get("body") or "")
    if needs_usability:
        usability_lines = [c for c in comments if c.startswith("🤖 usability:")]
        if not usability_lines or f"Usability {sha}: no blocking findings" not in usability_lines[-1]:
            return "fail"
    return "pass"


def _find_pr(d, pos, cwd):
    if pos and pos[0].lstrip("#").isdigit():
        return d["prs"][pos[0].lstrip("#")]
    branch = current_branch(cwd)
    cands = [p for p in d["prs"].values() if p["head"] == branch]
    if not cands:
        die(f"no pull request found for branch {branch!r}")
    return cands[0]


def gh_pr_checks(argv, cwd):
    pos, flags = parse_flags(argv)
    d = load()
    pr = _find_pr(d, pos, cwd)
    sha = run(["git", "rev-parse", "HEAD"], cwd=cwd).stdout.strip()
    checks = [
        {"name": "verify", "bucket": "pass"},
        {"name": "review-receipt", "bucket": review_receipt_bucket(pr, sha, cwd)},
    ]

    if "json" in flags:
        jq = flags.get("jq", "")
        m = re.match(r'\.\[\]\s*\|\s*select\(\.name=="([^"]+)"\)\s*\|\s*\.bucket', jq)
        if m:
            for c in checks:
                if c["name"] == m.group(1):
                    print(c["bucket"])
            return
        print(json.dumps(checks))
        return

    if CI_FAIL_ONCE and not os.environ.get("FAKEHUB_CI_FAILED_ONCE_MARKER_SEEN"):
        # Not wired to a real second run across processes; today's cases never set
        # FAKEHUB_CI_FAIL_ONCE. Left explicit rather than silently unsupported.
        die("some checks were not successful (simulated)")
    if any(c["bucket"] == "fail" for c in checks):
        die("some checks were not successful (simulated)")
    print("All checks were successful")


def gh_run_list(argv, cwd):
    print(json.dumps([{"databaseId": 1}]))


def gh_run_rerun(argv, cwd):
    print("rerun requested (simulated — review-receipt recomputes on the next check)")


def gh_run_view(argv, cwd):
    # `gh run view <id> --log | grep receipt-check`: what commands/watch.md §3 names for the
    # exact line bin/receipt-check.sh printed when `review-receipt` stays red. The one run
    # here is the current branch's PR; say in receipt-check's own words why its bucket is
    # what it is, so a tick that paraphrased a receipt learns what the check wanted. The
    # run id is ignored: a case has one PR, and its one run is that PR's.
    d = load()
    pr = _find_pr(d, [], cwd)
    sha = run(["git", "rev-parse", "HEAD"], cwd=cwd).stdout.strip()
    bucket = review_receipt_bucket(pr, sha, cwd)
    comments = pr.get("comments", [])
    reviewer = [c for c in comments if c.startswith("🤖 reviewer:")]
    usability = [c for c in comments if c.startswith("🤖 usability:")]
    print(f"review-receipt\treceipt-check\thead is {sha}")
    print(f"review-receipt\treceipt-check\tnewest reviewer comment: {reviewer[-1] if reviewer else '(none)'}")
    print(f"review-receipt\treceipt-check\tnewest usability comment: {usability[-1] if usability else '(none)'}")
    if bucket == "pass":
        print("review-receipt\treceipt-check\tok: a clean verdict names this exact head commit")
    else:
        print(f"review-receipt\treceipt-check\tFAIL: no `🤖 reviewer:` comment reads exactly "
              f"`Reviewed {sha}: no blocking findings` (the agent's own first line, verbatim)")


CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*(?:\[#(\d+)\]\([^)]*\)|#(\d+))", re.I
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
    # -X takes a value (POST); it is not a boolean flag. --paginate is one (bin/receipt-check.sh's
    # own shape, `gh api --paginate repos/…/issues/<n>/comments`), or it would eat the path.
    pos, flags = parse_flags(argv, known_bool=("paginate",))
    path = pos[0] if pos else ""
    m = re.match(r"repos/[^/]+/[^/]+/issues/(\d+)/comments$", path)
    if m:
        # The comments on an issue — or on a PR, which GitHub's REST API files under
        # /issues/ too, and which is how a tick (or bin/receipt-check.sh) reads receipts back.
        d = load()
        n = m.group(1)
        holder = d["issues"].get(n) or d["prs"].get(n)
        if holder is None:
            die(f"no such issue or pull request #{n}")
        comments = holder.get("comments", [])
        jq = flags.get("jq", "")
        if jq == ".[].body":
            print("\n".join(comments))
        elif jq:
            die(f"gh api …/comments --jq: fakehub only simulates `.[].body`, not {jq!r}")
        else:
            print(json.dumps([{"body": c} for c in comments]))
        return
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
    ("pr", "view"): lambda argv, cwd: gh_pr_view(argv, cwd),
    ("pr", "diff"): lambda argv, cwd: gh_pr_diff(argv, cwd),
    ("pr", "comment"): lambda argv, cwd: gh_pr_comment(argv, cwd),
    ("pr", "edit"): lambda argv, cwd: gh_pr_edit(argv, cwd),
    ("pr", "checks"): lambda argv, cwd: gh_pr_checks(argv, cwd),
    ("pr", "merge"): lambda argv, cwd: gh_pr_merge(argv, cwd),
    ("label", "list"): lambda argv, cwd: gh_label_list(argv),
    ("api", None): lambda argv, cwd: gh_api(argv),
    ("run", "list"): lambda argv, cwd: gh_run_list(argv, cwd),
    ("run", "rerun"): lambda argv, cwd: gh_run_rerun(argv, cwd),
    ("run", "view"): lambda argv, cwd: gh_run_view(argv, cwd),
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
