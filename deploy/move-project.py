#!/usr/bin/env python3
"""deploy/move-project.py <name> --to <org> --template <owner>/<number> [--title T] [--required-checks a,b]
                          [--no-protection] [--dry-run]                                   (card #268)

Moves a project the watcher already works — its repo and its board — into a GitHub
organization, so that project's GitHub Apps can reach the board (an App has no permission for
a user-owned Projects board; card #255). Standard library only, like deploy/app-setup.py. Run
from a checkout of this repo, authenticated as YOURSELF (`gh auth login`: the account that
owns the repo and the old board and is an owner of the organization), never as a bot or an
App. Every step checks first and does only what is missing, so a run that stops partway, or
one you simply run again later, is safe to run again. A 502 or 504 from GitHub mid-batch is
retried on the spot (card #274); a timeout GitHub reports as HTTP 200 with an `errors` body
("Something went wrong while executing your query") still stops the run — run it again.

  1. the repo        POST /repos/<old>/transfer to the organization; waits until GitHub says the
                     repo is the organization's. Issues, labels, PRs, secrets and runners come
                     along, and the old address redirects.
  2. the board       `gh project copy` of the template (the watcher's own board): every field,
                     every view and every workflow except Auto-add, which names a repo and has no
                     API (the watcher's own sweep makes it unnecessary, card #267). Then
                     `gh project link` to the moved repo. An open board with that title already in
                     the organization is reused (--title names another when that one is not it).
  3. the cards       every issue on the old board goes onto the new one with the same Status,
                     Priority, Stage (every single-select field whose name and option exist on
                     both). Read back until nothing differs: the board's own "Item added" workflow
                     sets Backlog a moment after an add, and would otherwise win the race. Draft
                     items and pull requests are not carried; the count of each is printed.
  4. protection      main's branch protection as the old repo had it (read before the transfer):
                     the same required checks, a PR required, 0 approvals, admins not enforced —
                     the shape deploy/add-project.sh sets. A private repo in a GitHub Free
                     organization cannot have it, and GitHub answers 403: this stops here, before
                     the lane is pointed at the new board, and says so. --no-protection skips the
                     step on purpose; --required-checks names the checks when the old repo's
                     protection can no longer be read (a rerun after the transfer).
  5. the env file    projects/<name>.env: PROJECT_REPO, BOARD_OWNER, BOARD_NUMBER rewritten, and
                     OWNER_LOGIN added (the old owner: the person who answers Stuck cards), so a
                     `Needs @…` comment still names a human. Commit and push it yourself; the
                     deployer ships it and the lane follows on its next idle poll.

It prints what is left for a person at the end: the project's Apps (deploy/app-setup.py), and
the project's own docs/board.md. The old board is left open for you to close when the new one
looks right. --dry-run reads everything and writes nothing. WATCHER_GH names the gh binary
(the offline suite's stub); WATCHER_ROOT the checkout (default: this script's parent);
MOVE_PROJECT_SLEEP the wait between read-back passes, in seconds.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("WATCHER_ROOT") or os.path.dirname(HERE)
GH = os.environ.get("WATCHER_GH", "gh")
CARRIED_FIELDS_SKIPPED = {"Title", "Assignees", "Labels", "Linked pull requests", "Milestone",
                          "Repository", "Reviewers", "Parent issue", "Sub-issues progress"}


class Stop(SystemExit):
    def __init__(self, msg):
        print(f"move-project: {msg}", file=sys.stderr)
        super().__init__(1)


def say(what):
    print(f"\n== {what} ==", flush=True)


def gh(*args, check=True, input=None):
    r = subprocess.run([GH, *args], capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"gh {' '.join(args)} failed").strip()[:400])
    return r


def gh_json(*args):
    out = gh(*args).stdout.strip()
    return json.loads(out) if out else None


RETRY_PAUSES = (5, 15, 30)  # seconds before the 2nd, 3rd and 4th try of a GraphQL call GitHub answered 5xx


def graphql(query, **variables):
    """One GraphQL call. A 5xx from GitHub (a 502 or 504 while a batched mutation runs, card #274)
    is retried three times with a growing pause; a 4xx is a real refusal and is raised at once.
    Safe for the mutations this script sends: addProjectV2ItemById returns the existing item for
    a card already on the board, and updateProjectV2ItemFieldValue sets the same value again."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += (["-F", f"{k}={v}"] if isinstance(v, int) else ["-f", f"{k}={v}"])
    for attempt in range(len(RETRY_PAUSES) + 1):
        r = gh(*args, check=False)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "gh api graphql failed").strip()
            code = re.search(r"\bHTTP (5\d\d)\b", err)
            if code and attempt < len(RETRY_PAUSES):
                wait = RETRY_PAUSES[attempt]
                print(f"  GitHub returned {code.group(1)}; retrying in {wait}s", flush=True)
                pause(wait)
                continue
            raise RuntimeError(err[:400])
        out = r.stdout.strip()
        data = json.loads(out) if out else None
        if data is None or data.get("errors"):
            raise RuntimeError(f"GraphQL: {json.dumps((data or {}).get('errors'))[:400]}")
        return data["data"]


def read_env(path):
    out = {}
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            k, sep, v = s.partition("=")
            if sep:
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def rewrite_env(path, updates, add_after):
    """Rewrites the KEY= lines named in `updates`, in place, keeping comments; a key that is
    not there yet goes after the key named in add_after[key] (or at the end), with its comment."""
    with open(path) as f:
        lines = f.read().splitlines()
    seen = set()
    out = []
    for line in lines:
        m = re.match(r"^([A-Z_]+)=", line)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    for key, value in updates.items():
        if key in seen:
            continue
        comment, after = add_after.get(key, ("", None))
        new = ([comment] if comment else []) + [f"{key}={value}"]
        idx = next((i for i, l in enumerate(out) if after and l.startswith(f"{after}=")), None)
        if idx is None:
            out += new
        else:
            out[idx + 1:idx + 1] = new
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


# --- the repo ---------------------------------------------------------------------------------

def repo_owner(repo):
    r = gh("api", f"repos/{repo}", check=False)
    if r.returncode != 0:
        return None
    return (json.loads(r.stdout).get("owner") or {}).get("login", "")


def read_protection(repo):
    r = gh("api", f"repos/{repo}/branches/main/protection", check=False)
    if r.returncode != 0:
        return None
    d = json.loads(r.stdout)
    rsc = d.get("required_status_checks") or {}
    checks = {c.get("context") for c in (rsc.get("checks") or []) if c.get("context")}
    checks |= set(rsc.get("contexts") or [])
    return sorted(checks)


def transfer_repo(old_repo, org, dry_run):
    name = old_repo.split("/", 1)[1]
    new_repo = f"{org}/{name}"
    owner_now = repo_owner(new_repo)
    if owner_now and owner_now.lower() == org.lower():
        print(f"  {new_repo} is already the organization's (unchanged)")
        return new_repo, None
    old_owner = repo_owner(old_repo)
    if not old_owner or old_owner.lower() != old_repo.split("/")[0].lower():
        raise Stop(f"{old_repo} is not where projects/<name>.env says it is (owner: {old_owner!r}); nothing moved")
    checks = read_protection(old_repo)
    print(f"  main's required checks on {old_repo} today: {', '.join(checks) if checks else 'none readable'}")
    if dry_run:
        print(f"  would transfer {old_repo} → {new_repo}")
        return new_repo, checks
    gh("api", "-X", "POST", f"repos/{old_repo}/transfer", "-f", f"new_owner={org}")
    deadline = time.time() + 180
    while time.time() < deadline:
        owner_now = repo_owner(new_repo)
        if owner_now and owner_now.lower() == org.lower():
            print(f"  transferred {old_repo} → {new_repo}")
            return new_repo, checks
        time.sleep(3)
    raise Stop(f"GitHub accepted the transfer of {old_repo} but {new_repo} is not there yet; run this again in a minute")


# --- the board --------------------------------------------------------------------------------

def find_board(owner, title):
    data = gh_json("project", "list", "--owner", owner, "--limit", "100", "--format", "json") or {}
    for p in data.get("projects", []):
        if p.get("title") == title and not p.get("closed"):
            return int(p["number"])
    return None


def board_title(owner, number):
    d = graphql("query($owner:String!,$number:Int!){ repositoryOwner(login:$owner){ ... on ProjectV2Owner{"
                " projectV2(number:$number){ title } } } }", owner=owner, number=number)
    p = (d.get("repositoryOwner") or {}).get("projectV2")
    if not p:
        raise Stop(f"no board #{number} under {owner}")
    return p["title"]


def copy_board(template, org, title, new_repo, dry_run):
    existing = find_board(org, title)
    if existing:
        print(f"  board \"{title}\" already exists under {org}: #{existing} (unchanged)")
        number = existing
    elif dry_run:
        print(f"  would copy {template} → {org}, titled \"{title}\"")
        return None
    else:
        t_owner, t_number = template.split("/", 1)
        number = int(gh_json("project", "copy", t_number, "--source-owner", t_owner, "--target-owner", org,
                             "--title", title, "--format", "json")["number"])
        print(f"  copied {template} → {org}/#{number}, titled \"{title}\"")
    if not dry_run:
        r = gh("project", "link", str(number), "--owner", org, "--repo", new_repo, check=False)
        print(f"  linked to {new_repo}" if r.returncode == 0 else f"  link to {new_repo}: {r.stderr.strip()[:120] or 'already linked'}")
    return number


# --- the cards --------------------------------------------------------------------------------

ITEMS_QUERY = """query($owner:String!,$number:Int!,$after:String){ repositoryOwner(login:$owner){ ... on ProjectV2Owner{
  projectV2(number:$number){ id
    fields(first:40){ nodes{ ... on ProjectV2SingleSelectField{ id name options{ id name } } } }
    items(first:100, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ id type
      content{ __typename ... on Issue{ id number repository{ nameWithOwner } } ... on PullRequest{ id number } }
      fieldValues(first:30){ nodes{ ... on ProjectV2ItemFieldSingleSelectValue{ name field{ ... on ProjectV2FieldCommon{ name } } } } } } } } } } }"""


def read_board(owner, number):
    """The board's id, its single-select fields ({name: {id, options: {name: id}}}) and its items:
    {content id: {item, number, repo, values: {field name: option name}}} for issues; drafts and
    PRs counted, not carried."""
    items, skipped, after = {}, {"drafts": 0, "pull requests": 0}, None
    fields = {}
    board_id = None
    while True:
        args = dict(owner=owner, number=number)
        if after:
            args["after"] = after
        d = graphql(ITEMS_QUERY, **args)
        p = (d.get("repositoryOwner") or {}).get("projectV2")
        if not p:
            raise Stop(f"no board #{number} under {owner}, or this login cannot read it")
        board_id = p["id"]
        fields = {f["name"]: {"id": f["id"], "options": {o["name"]: o["id"] for o in f["options"]}}
                  for f in p["fields"]["nodes"] if f.get("name")}
        for it in p["items"]["nodes"]:
            c = it.get("content") or {}
            if c.get("__typename") == "Issue":
                values = {v["field"]["name"]: v["name"] for v in it["fieldValues"]["nodes"] if v.get("name")}
                items[c["id"]] = {"item": it["id"], "number": c["number"],
                                  "repo": (c.get("repository") or {}).get("nameWithOwner", ""), "values": values}
            elif c.get("__typename") == "PullRequest":
                skipped["pull requests"] += 1
            else:
                skipped["drafts"] += 1
        page = p["items"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        after = page["endCursor"]
    return board_id, fields, items, skipped


def _lit(s):
    return json.dumps(s)


# How many aliased mutations go in one request. Twenty adds and thirty sets looked fine on a
# 21-card board and timed out (502, 504) on 32, 52 and 375 cards on November 9, 2026: GitHub
# ran out of time with the batch part-applied, every run landing about one batch (card #274).
# Five adds take a few seconds each; the retry in graphql() covers the rare one that still fails.
ADD_BATCH = 5
SET_BATCH = 10


def add_items(board_id, content_ids, dry_run):
    """addProjectV2ItemById for each, ADD_BATCH per request; returns {content id: item id}."""
    out = {}
    for i in range(0, len(content_ids), ADD_BATCH):
        chunk = content_ids[i:i + ADD_BATCH]
        if dry_run:
            continue
        parts = [f"a{j}: addProjectV2ItemById(input:{{projectId:{_lit(board_id)}, contentId:{_lit(cid)}}}){{ item{{ id }} }}"
                 for j, cid in enumerate(chunk)]
        d = graphql("mutation { " + " ".join(parts) + " }")
        for j, cid in enumerate(chunk):
            out[cid] = d[f"a{j}"]["item"]["id"]
    return out


def set_values(board_id, sets, dry_run):
    """sets: [(item id, field id, option id)], SET_BATCH per request."""
    for i in range(0, len(sets), SET_BATCH):
        chunk = sets[i:i + SET_BATCH]
        if dry_run:
            continue
        parts = [f"s{j}: updateProjectV2ItemFieldValue(input:{{projectId:{_lit(board_id)}, itemId:{_lit(item)}, "
                 f"fieldId:{_lit(field)}, value:{{singleSelectOptionId:{_lit(opt)}}}}}){{ projectV2Item{{ id }} }}"
                 for j, (item, field, opt) in enumerate(chunk)]
        graphql("mutation { " + " ".join(parts) + " }")


def wanted_values(old_item, new_fields):
    """{field name: option name} the new card should carry: every single-select the old card
    has a value for, when the new board has that field and that option."""
    out, dropped = {}, []
    for name, value in old_item["values"].items():
        f = new_fields.get(name)
        if f and value in f["options"] and name not in CARRIED_FIELDS_SKIPPED:
            out[name] = value
        elif name not in CARRIED_FIELDS_SKIPPED:
            dropped.append(f"{name}={value}")
    return out, dropped


def pause(seconds):
    """MOVE_PROJECT_SLEEP overrides every wait: between read-back passes and before a retried
    GraphQL call (the offline suite sets 0; a person setting it shortens the 5/15/30 s retries too)."""
    time.sleep(float(os.environ.get("MOVE_PROJECT_SLEEP", seconds)))


def carry_cards(old_owner, old_number, org, new_number, repos, dry_run):
    _, _, old_items, skipped = read_board(old_owner, old_number)
    mine = {cid: it for cid, it in old_items.items() if it["repo"].lower() in repos}
    foreign = len(old_items) - len(mine)
    print(f"  old board {old_owner}/#{old_number}: {len(mine)} issues to carry"
          + (f", {foreign} from other repos left alone" if foreign else "")
          + "".join(f", {n} {kind} not carried" for kind, n in skipped.items() if n))
    if new_number is None:  # dry run before the board exists
        print(f"  would add {len(mine)} cards with their Status, Priority and Stage")
        return
    new_id, new_fields, new_items, _ = read_board(org, new_number)
    missing = [cid for cid in mine if cid not in new_items]
    print(f"  new board {org}/#{new_number}: {len(new_items)} issues there already, {len(missing)} to add")
    added = add_items(new_id, missing, dry_run)
    for cid, item_id in added.items():
        new_items[cid] = {"item": item_id, "number": mine[cid]["number"], "repo": "", "values": {}}
    not_carried = {}
    for attempt in range(4):
        sets = []
        for cid, it in mine.items():
            new = new_items.get(cid)
            if not new:
                continue
            wanted, dropped = wanted_values(it, new_fields)
            for d in dropped:
                not_carried.setdefault(d, []).append(it["number"])
            for name, value in wanted.items():
                if new["values"].get(name) != value:
                    sets.append((new["item"], new_fields[name]["id"], new_fields[name]["options"][value]))
        if attempt == 0:
            for d, nums in sorted(not_carried.items()):
                print(f"  not carried, the new board has no such field or option: {d} on #{', #'.join(str(n) for n in nums)}")
        if not sets:
            print(f"  every carried card has its Status, Priority and Stage ({'read back ' + str(attempt) + 'x' if attempt else 'nothing to set'})")
            return
        print(f"  {'would set' if dry_run else 'setting'} {len(sets)} field values" + (" (read-back pass)" if attempt else ""))
        set_values(new_id, sets, dry_run)
        if dry_run:
            return
        pause(15 if attempt == 0 else 30)
        _, new_fields, new_items, _ = read_board(org, new_number)
    raise Stop(f"{len(sets)} field values still differ after four passes; run this again")


# --- protection -------------------------------------------------------------------------------

def protect(new_repo, checks, dry_run):
    """`checks` is what the old repo required, read before the transfer. On a rerun that
    reading is gone, so the moved repo's own protection (it comes across with the repo) is
    read instead; with neither and no --required-checks this stops rather than guess a check
    name — a guess could silently drop a required check (the merge gate, card #142)."""
    if not checks:
        checks = read_protection(new_repo)
        if checks:
            print(f"  {new_repo} main already requires {', '.join(checks)}; keeping those")
    if not checks:
        raise Stop(f"cannot tell which checks {new_repo} main must require (the old repo's protection could not be "
                   "read, and the moved repo has none): pass --required-checks <a,b>, or --no-protection on purpose")
    if dry_run:
        print(f"  would require {', '.join(checks)} on {new_repo} main (PR required, 0 approvals, admins not enforced)")
        return
    args = ["api", "-X", "PUT", f"repos/{new_repo}/branches/main/protection", "-H", "Accept: application/vnd.github+json",
            "-F", "required_status_checks[strict]=false"]
    for c in checks:
        args += ["-f", f"required_status_checks[contexts][]={c}"]
    args += ["-F", "enforce_admins=false", "-F", "required_pull_request_reviews[required_approving_review_count]=0",
             "-F", "restrictions=null"]
    r = gh(*args, check=False)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        hint = ""
        if "403" in err or "upgrade" in err.lower() or "Upgrade" in err:
            hint = (" — a private repo in a GitHub Free organization cannot have branch protection: upgrade the "
                    "organization to Team (one seat), make the repo public, or pass --no-protection to accept a "
                    "main with no required checks. The lane is still on the old board; nothing else changed.")
        raise Stop(f"could not protect {new_repo} main: {err[:300]}{hint}")
    print(f"  {new_repo} main: {', '.join(checks)} required, PR required, 0 approvals, admins not enforced")


# --- main -------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Move a project's repo and board into a GitHub organization.")
    ap.add_argument("name", help="the project: projects/<name>.env")
    ap.add_argument("--to", required=True, metavar="ORG", help="the organization")
    ap.add_argument("--template", required=True, metavar="OWNER/NUMBER",
                    help="the board to copy (the watcher's own, e.g. lonestarprod/1)")
    ap.add_argument("--title", help="the new board's title (default: the old board's)")
    ap.add_argument("--required-checks", metavar="A,B", help="main's required checks when the old repo's cannot be read")
    ap.add_argument("--no-protection", action="store_true", help="skip branch protection on purpose")
    ap.add_argument("--dry-run", action="store_true", help="read everything, write nothing")
    a = ap.parse_args(argv)
    if "/" not in a.template:
        ap.error("--template is <owner>/<number>")

    env_path = os.path.join(ROOT, "projects", f"{a.name}.env")
    if not os.path.exists(env_path):
        raise Stop(f"no {env_path}")
    env = read_env(env_path)
    for k in ("PROJECT_REPO", "BOARD_OWNER", "BOARD_NUMBER"):
        if not env.get(k):
            raise Stop(f"{env_path} has no {k}")
    old_repo, old_owner, old_number = env["PROJECT_REPO"], env["BOARD_OWNER"], int(env["BOARD_NUMBER"])
    org = a.to
    if old_owner.lower() == org.lower() and old_repo.lower().startswith(org.lower() + "/"):
        print(f"move-project: {a.name} is already under {org} ({old_repo}, board #{old_number}); nothing to do")
        return 0
    dry = a.dry_run
    if dry:
        print("move-project: dry run — reading everything, writing nothing")

    say("the repo")
    new_repo, checks = transfer_repo(old_repo, org, dry)
    if a.required_checks:
        checks = [c for c in a.required_checks.split(",") if c]

    say("the board")
    title = a.title or board_title(old_owner, old_number)
    new_number = copy_board(a.template, org, title, new_repo, dry)

    say("the cards")
    carry_cards(old_owner, old_number, org, new_number, {old_repo.lower(), new_repo.lower()}, dry)

    say("branch protection on main")
    if a.no_protection:
        print(f"  skipped on purpose (--no-protection): {new_repo} main has no required checks")
    else:
        protect(new_repo, checks, dry)

    say(f"projects/{a.name}.env")
    if dry or new_number is None:
        print(f"  would set PROJECT_REPO={new_repo} BOARD_OWNER={org} BOARD_NUMBER={new_number or '<new>'}"
              + ("" if env.get("OWNER_LOGIN") else f" and add OWNER_LOGIN={old_owner}"))
    else:
        updates = {"PROJECT_REPO": new_repo, "BOARD_OWNER": org, "BOARD_NUMBER": str(new_number)}
        if not env.get("OWNER_LOGIN"):
            updates["OWNER_LOGIN"] = old_owner
        rewrite_env(env_path, updates, {"OWNER_LOGIN": (
            "# The person who answers Stuck cards (BOARD_OWNER is the organization now, card #255).", "BOARD_OWNER")})
        print(f"  wrote {env_path}: PROJECT_REPO={new_repo} BOARD_OWNER={org} BOARD_NUMBER={new_number}"
              + (f" OWNER_LOGIN={old_owner}" if "OWNER_LOGIN" in updates else ""))

    say("done" if not dry else "dry run done")
    print(f"""Left for a person:
  - Commit and push projects/{a.name}.env (this script does not). The deployer ships it; the lane
    reads the new board on its next idle poll.
  - The project's Apps, on the box as root (then open the printed address through
    `ssh -L 8799:127.0.0.1:8799 root@<box>`): two Create clicks, two Install clicks, on {new_repo} only:
      python3 /opt/watcher/deploy/app-setup.py {a.name} --org {org} --repos {new_repo}
  - In {new_repo}: docs/board.md and CLAUDE.md name the board and repo —
      sed -i.bak -e 's|{old_repo}|{new_repo}|g' -e 's|users/{old_owner}/projects/{old_number}|orgs/{org}/projects/{new_number or '<new>'}|g' docs/board.md CLAUDE.md && rm -f docs/board.md.bak CLAUDE.md.bak
    (a card on the new board does as well; it is the first thing the project's lane does as its App).
  - The old board {old_owner}/#{old_number} is still open; close it once the new one looks right:
      gh project close {old_number} --owner {old_owner}
  - Self-hosted runners registered to the repo follow the transfer; check the next CI run anyway.""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:  # a gh or GraphQL call that failed: one line, not a traceback
        raise Stop(str(e))
