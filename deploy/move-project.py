#!/usr/bin/env python3
"""deploy/move-project.py <name> --to <org> --template <owner>/<n> [--dry-run] [--no-protection]
                          [--required-checks a,b] [--title T] [--env-file F]  (card #268)

Moves a project the watcher already works into an organization in one idempotent run: the repo, its
board, every card in the column it was in, main's protection, and projects/<name>.env. Run from
the owner's Mac, as themselves (`gh auth login`): transferring a repo and copying a board are
things only a person's own login may do. Standard library only, like app-setup.py; every GitHub
call is the `gh` CLI (GH_BIN names another one, bin/test-move-project.py's stub).

  1. Transfer the repo (POST /repos/<old>/transfer), then wait until it answers as the organization's.
  2. `gh project copy` of the template board (every field, view and workflow except Auto-add,
     which has no API; card #267 makes it unnecessary), `gh project link` to the repo. A board
     with that title already in the organization is reused.
  3. Every issue on the old board onto the new one (twenty per request), then every single-select
     value whose field and option exist on both (thirty per request), read back and set again
     until nothing differs: the board's "Item added" workflow sets Backlog a moment after an add
     and would otherwise win. Drafts and pull requests are counted, not carried.
  4. main's branch protection as the old repo had it, read before the transfer (the checks, a PR
     required, 0 approvals: deploy/add-project.sh's shape). A private repo in a Free organization
     answers 403: the run stops before the env file and says what the choices are.
     --no-protection and --required-checks are the explicit overrides.
  5. projects/<name>.env: PROJECT_REPO, BOARD_OWNER, BOARD_NUMBER rewritten in place, OWNER_LOGIN
     added (the old owner, the human).

Every step checks first, so a rerun does only what is missing. --dry-run reads everything and
writes nothing. Left for a person: the project's two Apps (deploy/app-setup.py) and closing the
old board. Exit 0 done, 1 an error, 3 stopped on main's protection.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

GH = os.environ.get("GH_BIN", "gh")
ADD_BATCH = 20
SET_BATCH = 30
READBACK_ROUNDS = 8


class Stop(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def say(msg):
    print(f"move-project: {msg}", flush=True)


def run(args, stdin=None):
    try:
        return subprocess.run([GH] + args, input=stdin, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise Stop("`gh` is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise Stop(f"gh timed out: {' '.join(args[:3])}")


def api(path, method="GET", body=None):
    """(http ok, parsed json or None, stderr). A non-2xx is data here, not an exception."""
    args = ["api", path, "-X", method]
    if body is not None:
        args += ["--input", "-"]
    r = run(args, json.dumps(body) if body is not None else None)
    out = None
    if r.stdout.strip():
        try:
            out = json.loads(r.stdout)
        except ValueError:
            pass
    return r.returncode == 0, out, (r.stderr or r.stdout).strip()


def gql(query):
    r = run(["api", "graphql", "-f", f"query={query}"])
    if r.returncode:
        raise Stop(f"GitHub refused a query: {(r.stderr or r.stdout).strip()[:300]}")
    d = json.loads(r.stdout)
    if d.get("errors"):
        raise Stop(f"GitHub refused a query: {d['errors'][0].get('message')}")
    return d["data"]


def q(s):
    return json.dumps(s)


# -- reading ---------------------------------------------------------------------------------

def read_env(path):
    values = {}
    for line in open(path):
        m = re.match(r"([A-Z_]+)=(.*)$", line.strip())
        if m:
            values[m.group(1)] = m.group(2).strip().strip('"')
    return values


def write_env(path, updates):
    """Rewrite the named keys in place (comments and order kept); a new key goes after BOARD_NUMBER."""
    lines = open(path).read().split("\n")
    pending = dict(updates)
    for i, line in enumerate(lines):
        m = re.match(r"([A-Z_]+)=", line)
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
    if pending:
        at = next((i + 1 for i, l in enumerate(lines) if l.startswith("BOARD_NUMBER=")), len(lines))
        for k in pending:
            lines.insert(at, f"{k}={pending[k]}")
            at += 1
    tmp = path + ".new"
    with open(tmp, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)


def old_board(owner, number):
    d = gql("{ repositoryOwner(login:%s){ ... on ProjectV2Owner { projectV2(number:%d){ id number title } } } }" % (q(owner), number))
    p = ((d.get("repositoryOwner") or {}).get("projectV2"))
    if not p:
        raise Stop(f"{owner}'s board {number} not found; has the move already been run, or is BOARD_NUMBER wrong?")
    return p


def board_detail(pid):
    d = gql("""{ node(id:%s){ ... on ProjectV2 { id number title
      repositories(first:50){ nodes{ nameWithOwner } }
      fields(first:50){ nodes{ ... on ProjectV2SingleSelectField { id name options{ id name } } } } } } }""" % q(pid))
    n = d["node"]
    n["fields"] = [f for f in n["fields"]["nodes"] if f.get("id")]
    n["repos"] = [r["nameWithOwner"].lower() for r in n["repositories"]["nodes"]]
    return n


def board_items(pid):
    """Every item: {id, type, number, content, values: {field name: option name}}."""
    out, cursor = [], None
    while True:
        after = f", after:{q(cursor)}" if cursor else ""
        d = gql("""{ node(id:%s){ ... on ProjectV2 { items(first:100%s){ pageInfo{ hasNextPage endCursor }
          nodes{ id content{ __typename ... on Issue { id number } }
            fieldValues(first:30){ nodes{ ... on ProjectV2ItemFieldSingleSelectValue { name field{ ... on ProjectV2SingleSelectField { name } } } } } } } } } }""" % (q(pid), after))
        page = d["node"]["items"]
        for n in page["nodes"]:
            c = n.get("content") or {}
            vals = {v["field"]["name"]: v["name"] for v in n["fieldValues"]["nodes"] if v.get("field")}
            out.append({"id": n["id"], "type": c.get("__typename") or "DraftIssue", "number": c.get("number"),
                        "content": c.get("id"), "values": vals})
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def protection(repo):
    """('none', None) for no protection, ('ok', checks) for a protected main."""
    ok, d, err = api(f"repos/{repo}/branches/main/protection")
    if ok:
        rsc = (d or {}).get("required_status_checks") or {}
        checks = {c.get("context") for c in (rsc.get("checks") or []) if c.get("context")} | set(rsc.get("contexts") or [])
        prr = (d or {}).get("required_pull_request_reviews")
        return "ok", {"checks": sorted(checks), "pr": prr is not None, "approvals": (prr or {}).get("required_approving_review_count")}
    if "404" in err or "not protected" in err.lower():
        return "none", None
    raise Stop(f"could not read main's protection on {repo}: {err[:200]}")


# -- the steps -------------------------------------------------------------------------------

def step_transfer(a, old_repo, new_repo):
    say("1. the repo")
    ok, d, err = api(f"repos/{new_repo}")
    if ok and (d or {}).get("full_name", "").lower() == new_repo.lower():
        say(f"   {new_repo} already answers as the organization's")
        return
    ok, d, err = api(f"repos/{old_repo}")
    if not ok:
        raise Stop(f"{old_repo} does not answer: {err[:200]}")
    if (d or {}).get("full_name", "").lower() == new_repo.lower():
        say(f"   {old_repo} now redirects to {new_repo}")
        return
    if a.dry_run:
        say(f"   would transfer {old_repo} to {a.to}")
        return
    ok, _, err = api(f"repos/{old_repo}/transfer", "POST", {"new_owner": a.to})
    if not ok:
        raise Stop(f"the transfer was refused: {err[:300]}")
    say(f"   transfer of {old_repo} started; waiting for {new_repo}")
    deadline = time.time() + a.wait
    while time.time() < deadline:
        ok, d, _ = api(f"repos/{new_repo}")
        if ok and (d or {}).get("full_name", "").lower() == new_repo.lower():
            say(f"   {new_repo} answers")
            return
        time.sleep(a.poll)
    raise Stop(f"{new_repo} did not answer within {a.wait} seconds; rerun to carry on from here")


def step_board(a, old, new_repo):
    """The organization's board for this project, or None in a dry run that would create it."""
    say("2. the board")
    title = a.title or old["title"]
    d = gql("{ organization(login:%s){ projectsV2(first:100){ nodes{ id number title } } } }" % q(a.to))
    found = [p for p in d["organization"]["projectsV2"]["nodes"] if p["title"] == title]
    if found:
        board = found[0]
        say(f"   {a.to}'s board {board['number']} \"{title}\" exists; reused")
    elif a.dry_run:
        say(f"   would copy {a.template} as \"{title}\" in {a.to}")
        return None
    else:
        tpl_owner, tpl_n = a.template.split("/")
        r = run(["project", "copy", tpl_n, "--source-owner", tpl_owner, "--target-owner", a.to,
                 "--title", title, "--format", "json"])
        if r.returncode:
            raise Stop(f"gh project copy failed: {(r.stderr or r.stdout).strip()[:300]}")
        board = json.loads(r.stdout)
        say(f"   copied {a.template} as board {board['number']} \"{title}\"")
    detail = board_detail(board["id"])
    if new_repo.lower() in detail["repos"]:
        say(f"   linked to {new_repo}")
    elif a.dry_run:
        say(f"   would link to {new_repo}")
    else:
        r = run(["project", "link", str(board["number"]), "--owner", a.to, "--repo", new_repo])
        if r.returncode:
            raise Stop(f"gh project link failed: {(r.stderr or r.stdout).strip()[:300]}")
        say(f"   linked to {new_repo}")
    return {"id": board["id"], "number": board["number"], "title": title}


def batches(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def step_cards(a, old, new):
    say("3. the cards")
    old_items = board_items(old["id"])
    issues = [i for i in old_items if i["type"] == "Issue"]
    other = len(old_items) - len(issues)
    say(f"   {len(issues)} issues on the old board; {other} drafts and pull requests counted, not carried")
    if new is None:
        say(f"   would add {len(issues)} issues and set their columns and fields")
        return
    have = {i["number"] for i in board_items(new["id"]) if i["type"] == "Issue"}
    todo = [i for i in issues if i["number"] not in have]
    if a.dry_run:
        say(f"   would add {len(todo)} issues; {len(issues) - len(todo)} already there")
    else:
        for chunk in batches(todo, ADD_BATCH):
            body = " ".join("a%d: addProjectV2ItemById(input:{projectId:%s, contentId:%s}){ item{ id } }"
                            % (k, q(new["id"]), q(i["content"])) for k, i in enumerate(chunk))
            gql("mutation{ %s }" % body)
        say(f"   added {len(todo)}; {len(issues) - len(todo)} already there")
    if a.dry_run and todo:
        say("   would set every column and field value after the add")
        return
    fields = {f["name"]: {"id": f["id"], "options": {o["name"]: o["id"] for o in f["options"]}}
              for f in board_detail(new["id"])["fields"]}
    old_vals = {i["number"]: i["values"] for i in issues}
    skipped = set()

    def wanted(number):
        out = {}
        for fname, oname in old_vals.get(number, {}).items():
            f = fields.get(fname)
            if f and oname in f["options"]:
                out[fname] = (f["id"], f["options"][oname], oname)
            else:
                skipped.add(f"{fname}={oname}")
        return out

    if todo and not a.dry_run:
        time.sleep(a.settle)
    sets = 0
    for rnd in range(READBACK_ROUNDS):
        now = {i["number"]: i for i in board_items(new["id"]) if i["type"] == "Issue"}
        diff = []
        for number in old_vals:
            if number not in now:
                continue
            for fname, (fid, oid, oname) in wanted(number).items():
                if now[number]["values"].get(fname) != oname:
                    diff.append((now[number]["id"], fid, oid))
        if not diff:
            say(f"   every value matches the old board" + (f" (after {sets} sets)" if sets else ""))
            break
        if a.dry_run:
            say(f"   would set {len(diff)} values")
            break
        for chunk in batches(diff, SET_BATCH):
            body = " ".join("s%d: updateProjectV2ItemFieldValue(input:{projectId:%s, itemId:%s, fieldId:%s, value:{singleSelectOptionId:%s}}){ projectV2Item{ id } }"
                            % (k, q(new["id"]), q(iid), q(fid), q(oid)) for k, (iid, fid, oid) in enumerate(chunk))
            gql("mutation{ %s }" % body)
        sets += len(diff)
        time.sleep(a.settle)
    else:
        raise Stop(f"values still differ after {READBACK_ROUNDS} read-backs; rerun to try again")
    if skipped:
        say("   not carried (no such field or option on the new board): " + ", ".join(sorted(skipped)))


def step_protection(a, new_repo, captured):
    say("4. main's protection")
    if a.no_protection:
        say("   skipped (--no-protection)")
        return
    state, cur = protection(new_repo) if not a.dry_run or a.transferred else ("none", None)
    checks = sorted(set(a.required_checks.split(",")) - {""}) if a.required_checks else (captured or {}).get("checks")
    if checks is None and a.transferred and state != "ok":
        # An earlier run moved the repo, and the protection did not come with it: what the old
        # repo required can no longer be read, so a guess here would be the script's, not the owner's.
        raise Stop(f"main on {new_repo} is unprotected and the checks the old repo required are gone with the move.\n"
                   f"  Rerun with --required-checks <a,b> to say what to require, or --no-protection to leave main open.", 3)
    if checks is None:
        say("   the old repo had none, and --required-checks names none: nothing to set")
        return
    if state == "ok" and cur["pr"] and cur["approvals"] == 0 and set(checks) <= set(cur["checks"]):
        say(f"   already set: {', '.join(checks) or 'no checks'}, PR required, 0 approvals")
        return
    if a.dry_run:
        say(f"   would set: {', '.join(checks) or 'no checks'}, PR required, 0 approvals")
        return
    body = {"required_status_checks": {"strict": False, "contexts": sorted(set(checks) | set((cur or {}).get("checks") or []))},
            "enforce_admins": False,
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "restrictions": None}
    ok, _, err = api(f"repos/{new_repo}/branches/main/protection", "PUT", body)
    if ok:
        say(f"   set: {', '.join(checks) or 'no checks'}, PR required, 0 approvals")
        return
    if "403" in err:
        raise Stop(
            f"GitHub will not protect main on {new_repo} (403: a private repo in a Free organization cannot).\n"
            f"  The repo, board and cards are moved; projects/{a.name}.env is NOT yet rewritten. Choose:\n"
            f"  A. Upgrade the organization's plan (or make the repo public), then rerun this command.\n"
            f"  B. Rerun with --no-protection: main stays unprotected, and merges rely on the lane's own rules.\n"
            f"  The checks the old repo required were: {', '.join(checks) or 'none'}.", 3)
    raise Stop(f"setting main's protection failed: {err[:300]}")


def step_env(a, env_path, env, old_owner, new_repo, new):
    say(f"5. {os.path.relpath(env_path)}")
    want = {"PROJECT_REPO": new_repo, "BOARD_OWNER": a.to, "BOARD_NUMBER": str(new["number"]) if new else "?"}
    if old_owner.lower() != a.to.lower() and not env.get("OWNER_LOGIN"):
        want["OWNER_LOGIN"] = old_owner
    changes = {k: v for k, v in want.items() if env.get(k) != v}
    if not changes:
        say("   already names the organization")
        return
    if a.dry_run:
        say("   would write " + ", ".join(f"{k}={v}" for k, v in changes.items()))
        return
    write_env(env_path, changes)
    say("   wrote " + ", ".join(f"{k}={v}" for k, v in changes.items()))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Move a project's repo, board and cards into an organization.")
    ap.add_argument("name")
    ap.add_argument("--to", required=True, help="the organization's login")
    ap.add_argument("--template", required=True, help="<owner>/<number> of the board to copy (the watcher's own)")
    ap.add_argument("--title", help="the new board's title (default: the old board's)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-protection", action="store_true")
    ap.add_argument("--required-checks", help="comma-separated checks to require, instead of the old repo's")
    ap.add_argument("--env-file", help="default: projects/<name>.env in this checkout")
    ap.add_argument("--wait", type=int, default=180, help="seconds to wait for the transfer")
    ap.add_argument("--poll", type=float, default=3)
    ap.add_argument("--settle", type=float, default=4, help="seconds to let the board's workflows run before a read-back")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[^/\s]+/\d+", a.template):
        sys.exit("move-project: --template is <owner>/<number>, e.g. lonestarprod/1")
    env_path = a.env_file or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "projects", f"{a.name}.env")
    try:
        env = read_env(env_path)
        old_repo = env["PROJECT_REPO"]
        old_owner, repo_name = old_repo.split("/")
        new_repo = f"{a.to}/{repo_name}"
        a.new_repo = new_repo
        board_owner, board_n = env["BOARD_OWNER"], int(env["BOARD_NUMBER"])
    except (OSError, KeyError, ValueError) as e:
        sys.exit(f"move-project: cannot read {env_path}: {e!r}")
    if a.dry_run:
        say("dry run: reading everything, writing nothing")
    try:
        old = old_board(board_owner, board_n)
        # Read before the transfer: the protection moves with the repo or is lost to it.
        ok, d, _ = api(f"repos/{old_repo}")
        a.transferred = ok and (d or {}).get("full_name", "").lower() == new_repo.lower()
        state, captured = protection(old_repo)
        step_transfer(a, old_repo, new_repo)
        new = step_board(a, old, new_repo)
        step_cards(a, old, new)
        step_protection(a, new_repo, captured)
        step_env(a, env_path, env, old_owner, new_repo, new)
    except Stop as e:
        print(f"move-project: {e}", file=sys.stderr)
        sys.exit(e.code)
    say("done." + ("" if a.dry_run else f" Left for you: the project's two Apps (deploy/app-setup.py {a.name} --org {a.to}) "
                   "and closing the old board."))


if __name__ == "__main__":
    main()
