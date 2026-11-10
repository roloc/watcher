#!/usr/bin/env python3
"""deploy/move-project.py's offline suite (card #268): no network, `gh` is a stub that logs one
line per call to $CALLS and keeps a tiny GitHub of its own under $TMP — the repos and who owns
them, main's protection, the boards and their items — and applies the script's own mutations
to it, so a case can read what the script did and what the boards look like afterward.

  python3 bin/test-move-project.py      # exit 0 = every case holds
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "deploy", "move-project.py")

fail = False


def check(what, ok, detail=""):
    global fail
    print(f"  {what:<78} {'ok' if ok else 'FAIL' + (' — ' + str(detail)[:300] if detail else '')}")
    if not ok:
        fail = True


STUB = r'''#!/usr/bin/env python3
import json, os, re, sys
T = os.environ["TMP"]
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as f:
    f.write("gh " + " ".join(a.replace("\n", " ")[:160] for a in args) + "\n")

def load(name, default):
    p = os.path.join(T, name)
    return json.load(open(p)) if os.path.exists(p) else default

def save(name, data):
    json.dump(data, open(os.path.join(T, name), "w"))

def board_file(owner, number):
    return f"board-{owner}-{number}.json"

def out(obj):
    print(json.dumps(obj)); sys.exit(0)

if args[0] == "api" and args[1] == "graphql":
    q = next(a for a in args if a.startswith("query=")).split("=", 1)[1]
    var = {}
    for i, a in enumerate(args):
        if a in ("-f", "-F") and i + 1 < len(args) and not args[i + 1].startswith("query="):
            k, _, v = args[i + 1].partition("="); var[k] = v
    if q.lstrip().startswith("mutation"):
        left = load("fail-5xx.json", 0)   # answer this many mutations with a 502 before applying anything
        if left:
            save("fail-5xx.json", left - 1)
            print("gh: HTTP 502", file=sys.stderr); sys.exit(1)
        if load("fail-4xx.json", 0):
            save("fail-4xx.json", 0)
            print("gh: HTTP 422: Validation Failed", file=sys.stderr); sys.exit(1)
        result = {}
        adds = re.findall(r'(a\d+): addProjectV2ItemById\(input:\{projectId:"([^"]+)", contentId:"([^"]+)"', q)
        partial = load("fail-5xx-partial.json", 0)   # apply the first half of this batch, then answer 502
        for k, (alias, pid, cid) in enumerate(adds):
            if partial and k >= (len(adds) + 1) // 2:
                save("fail-5xx-partial.json", partial - 1)
                print("gh: HTTP 502", file=sys.stderr); sys.exit(1)
            owner, number = load("board-by-id.json", {})[pid]
            b = load(board_file(owner, number), None)
            have = next((it for it in b["items"] if it["content"].get("id") == cid), None)
            if have:   # GitHub returns the existing item for content already on the board
                result[alias] = {"item": {"id": have["id"]}}
                continue
            contents = load("contents.json", {})
            c = contents[cid]
            item_id = f"ITEM_{number}_{len(b['items'])}"
            if cid in load("never-add.json", []):   # accepted, answered with an item, never kept
                result[alias] = {"item": {"id": "LOST_" + cid}}
                continue
            b["items"].append({"id": item_id, "type": "ISSUE", "content": {"__typename": "Issue", "id": cid,
                               "number": c["number"], "repository": {"nameWithOwner": c["repo"]}}, "values": {}})
            save(board_file(owner, number), b)
            result[alias] = {"item": {"id": item_id}}
        sets = re.findall(r'(s\d+): updateProjectV2ItemFieldValue\(input:\{projectId:"([^"]+)", itemId:"([^"]+)", fieldId:"([^"]+)", value:\{singleSelectOptionId:"([^"]+)"', q)
        drift = os.path.exists(os.path.join(T, "drift-once"))
        for alias, pid, item_id, field_id, opt_id in sets:
            owner, number = load("board-by-id.json", {})[pid]
            b = load(board_file(owner, number), None)
            field = next(f for f in b["fields"] if f["id"] == field_id)
            opt = next(o["name"] for o in field["options"] if o["id"] == opt_id)
            if item_id.startswith("LOST_"):
                result[alias] = {"projectV2Item": {"id": item_id}}
                continue
            it = next(i for i in b["items"] if i["id"] == item_id)
            it["values"][field["name"]] = opt
            if drift and field["name"] == "Status":
                it["values"]["Status"] = "Backlog"   # the board's own Item-added workflow, a moment late
            save(board_file(owner, number), b)
            result[alias] = {"projectV2Item": {"id": item_id}}
        if sets and drift:
            os.remove(os.path.join(T, "drift-once"))
        out({"data": result})
    owner, number = var["owner"], var["number"]
    b = load(board_file(owner, number), None)
    if b is None:
        out({"data": {"repositoryOwner": {"projectV2": None}}})
    if "{ title }" in q:
        out({"data": {"repositoryOwner": {"projectV2": {"title": b["title"]}}}})
    nodes = []
    for it in b["items"]:
        fv = [{"name": v, "field": {"name": k}} for k, v in it.get("values", {}).items()]
        nodes.append({"id": it["id"], "type": it["type"], "content": it["content"], "fieldValues": {"nodes": fv}})
    ghosts = load("ghost-once.json", {})   # {board number: [item, ...]}: shown on the first read of that board only
    if ghosts.get(number):
        nodes += [{"id": "GHOST_" + g["content"]["id"], "type": "ISSUE", "content": g["content"],
                   "fieldValues": {"nodes": [{"name": v, "field": {"name": k}} for k, v in g["values"].items()]}} for g in ghosts.pop(number)]
        save("ghost-once.json", ghosts)
    out({"data": {"repositoryOwner": {"projectV2": {"id": b["id"], "fields": {"nodes": b["fields"]},
         "items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}})

if args[0] == "api":
    method = "GET"
    if "-X" in args:
        method = args[args.index("-X") + 1]
    path = next(a for a in args[1:] if not a.startswith("-") and a not in (method,) and "/" in a)
    repos = load("repos.json", {})
    m = re.match(r"repos/([^/]+/[^/]+)/transfer$", path)
    if m and method == "POST":
        new_owner = next(a.split("=", 1)[1] for a in args if a.startswith("new_owner="))
        name = m.group(1).split("/")[1]
        repos[f"{new_owner}/{name}"] = new_owner
        save("repos.json", repos)
        out({"id": 1})
    m = re.match(r"repos/([^/]+/[^/]+)/branches/main/protection$", path)
    if m:
        if method == "PUT":
            if os.path.exists(os.path.join(T, "protection-refused")):
                print("HTTP 403: Upgrade to GitHub Team or make this repository public to enable this feature. (https://api.github.com/...)", file=sys.stderr)
                sys.exit(1)
            out({})
        p = load("protection.json", None)
        if p is None or m.group(1) not in repos:
            print("HTTP 404", file=sys.stderr); sys.exit(1)
        out(p)
    m = re.match(r"repos/([^/]+/[^/]+)$", path)
    if m:
        owner = repos.get(m.group(1))
        if owner is None:
            # a transferred repo's old address redirects to the new one
            name = m.group(1).split("/")[1]
            hit = [o for r, o in repos.items() if r.split("/")[1] == name]
            if not hit:
                print("HTTP 404", file=sys.stderr); sys.exit(1)
            owner = hit[0]
        out({"owner": {"login": owner}})
    print("stub: unknown api call " + " ".join(args), file=sys.stderr); sys.exit(2)

if args[0] == "project":
    owner = args[args.index("--owner") + 1] if "--owner" in args else None
    if args[1] == "list":
        out({"projects": load(f"projects-{owner}.json", [])})
    if args[1] == "copy":
        target = args[args.index("--target-owner") + 1]
        title = args[args.index("--title") + 1]
        number = load("next-board-number.json", 9)
        src_owner = args[args.index("--source-owner") + 1]; src_number = args[2]
        tpl = load(board_file(src_owner, src_number), None)
        b = {"id": f"PVT_{target}_{number}", "title": title, "fields": tpl["fields"], "items": []}
        save(board_file(target, number), b)
        by_id = load("board-by-id.json", {}); by_id[b["id"]] = [target, str(number)]; save("board-by-id.json", by_id)
        lst = load(f"projects-{target}.json", []); lst.append({"number": number, "title": title, "closed": False}); save(f"projects-{target}.json", lst)
        out({"number": number})
    if args[1] == "link":
        sys.exit(0)
print("stub: unknown call " + " ".join(args), file=sys.stderr); sys.exit(2)
'''

FIELDS = [
    {"id": "F_STATUS", "name": "Status", "options": [{"id": "O_BACKLOG", "name": "Backlog"}, {"id": "O_READY", "name": "Ready"},
                                                     {"id": "O_INPROG", "name": "In progress"}, {"id": "O_DONE", "name": "Done"},
                                                     {"id": "O_STUCK", "name": "Stuck - Needs help"}]},
    {"id": "F_PRIO", "name": "Priority", "options": [{"id": "O_P0", "name": "P0"}, {"id": "O_P1", "name": "P1"}, {"id": "O_P2", "name": "P2"}]},
    {"id": "F_STAGE", "name": "Stage", "options": [{"id": "O_WAIT", "name": "Waiting"}, {"id": "O_PROPEN", "name": "PR open"}]},
    {},  # a field that is not single-select comes back as an empty node
]

OLD_BOARD = {
    "id": "PVT_roloc_4", "title": "thing", "fields": FIELDS,
    "items": [
        {"id": "OLD_1", "type": "ISSUE", "content": {"__typename": "Issue", "id": "I_1", "number": 1, "repository": {"nameWithOwner": "roloc/thing"}},
         "values": {"Status": "Done", "Priority": "P1"}},
        {"id": "OLD_2", "type": "ISSUE", "content": {"__typename": "Issue", "id": "I_2", "number": 2, "repository": {"nameWithOwner": "roloc/thing"}},
         "values": {"Status": "Ready", "Stage": "PR open"}},
        {"id": "OLD_3", "type": "ISSUE", "content": {"__typename": "Issue", "id": "I_3", "number": 3, "repository": {"nameWithOwner": "roloc/thing"}},
         "values": {"Status": "Backlog", "Size": "XL"}},
        {"id": "OLD_4", "type": "ISSUE", "content": {"__typename": "Issue", "id": "I_9", "number": 9, "repository": {"nameWithOwner": "roloc/other"}},
         "values": {"Status": "Ready"}},
        {"id": "OLD_5", "type": "PULL_REQUEST", "content": {"__typename": "PullRequest", "id": "PR_1", "number": 5}, "values": {}},
        {"id": "OLD_6", "type": "DRAFT_ISSUE", "content": {"__typename": "DraftIssue", "id": "DI_1"}, "values": {}},
    ],
}

ENV_TEXT = textwrap.dedent("""\
    # Sourced by bin/spawn.sh.
    PROJECT_NAME=thing
    PROJECT_ROOT=/home/watcher/projects/thing
    BOARD_OWNER=roloc
    PROJECT_REPO=roloc/thing
    BOARD_NUMBER=4
    WORKTREE_COPY_DIRS=""
    READY_PULL=1
    """)


class World:
    """One stub GitHub per case."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(self.root, "projects"))
        self.env_path = os.path.join(self.root, "projects", "thing.env")
        with open(self.env_path, "w") as f:
            f.write(ENV_TEXT)
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        self.gh = os.path.join(bindir, "gh")
        with open(self.gh, "w") as f:
            f.write(STUB)
        os.chmod(self.gh, 0o755)
        self.calls = os.path.join(self.tmp, "calls")
        self.put("repos.json", {"roloc/thing": "roloc", "lonestarprod/watcher": "lonestarprod"})
        self.put("protection.json", {"required_status_checks": {"contexts": ["verify", "review-receipt"]},
                                     "required_pull_request_reviews": {"required_approving_review_count": 0}})
        self.put("board-roloc-4.json", OLD_BOARD)
        self.put("board-lonestarprod-1.json", {"id": "PVT_lonestarprod_1", "title": "Watcher", "fields": FIELDS, "items": []})
        self.put("board-by-id.json", {"PVT_roloc_4": ["roloc", "4"], "PVT_lonestarprod_1": ["lonestarprod", "1"]})
        self.put("projects-lonestarprod.json", [{"number": 1, "title": "Watcher", "closed": False}])
        self.put("contents.json", {"I_1": {"number": 1, "repo": "lonestarprod/thing"}, "I_2": {"number": 2, "repo": "lonestarprod/thing"},
                                   "I_3": {"number": 3, "repo": "lonestarprod/thing"}, "I_9": {"number": 9, "repo": "roloc/other"}})

    def put(self, name, data):
        json.dump(data, open(os.path.join(self.tmp, name), "w"))

    def get(self, name):
        p = os.path.join(self.tmp, name)
        return json.load(open(p)) if os.path.exists(p) else None

    def run(self, *extra):
        if os.path.exists(self.calls):
            os.remove(self.calls)
        env = dict(os.environ, WATCHER_GH=self.gh, WATCHER_ROOT=self.root, TMP=self.tmp, CALLS=self.calls)
        return subprocess.run([sys.executable, SCRIPT, "thing", "--to", "lonestarprod", "--template", "lonestarprod/1", *extra],
                              capture_output=True, text=True, env=env, timeout=120)

    def called(self):
        return open(self.calls).read().splitlines() if os.path.exists(self.calls) else []

    def env_text(self):
        return open(self.env_path).read()

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def touch(w, name):
    open(os.path.join(w.tmp, name), "w").close()


def main():
    # the script sleeps between read-back passes; a case has no time for that
    os.environ["MOVE_PROJECT_SLEEP"] = "0"

    print("1. a full move: transfer, copy, carry, protect, env")
    w = World()
    r = w.run()
    calls = w.called()
    check("exits 0", r.returncode == 0, r.stderr[-400:] + r.stdout[-400:])
    check("transfers the repo to the organization", any("-X POST repos/roloc/thing/transfer" in c and "new_owner=lonestarprod" in c for c in calls))
    check("reads main's protection before the transfer",
          next(i for i, c in enumerate(calls) if "branches/main/protection" in c and "PUT" not in c) < next(i for i, c in enumerate(calls) if "transfer" in c))
    check("copies the template board with the old board's title",
          any("project copy 1 --source-owner lonestarprod --target-owner lonestarprod --title thing" in c for c in calls))
    check("links the new board to the moved repo", any("project link 9 --owner lonestarprod --repo lonestarprod/thing" in c for c in calls))
    new = w.get("board-lonestarprod-9.json")
    carried = {it["content"]["id"]: it["values"] for it in new["items"]}
    check("carries the three issues of this repo and nothing else", sorted(carried) == ["I_1", "I_2", "I_3"], sorted(carried))
    check("Status, Priority and Stage come across by option", carried["I_1"] == {"Status": "Done", "Priority": "P1"}
          and carried["I_2"] == {"Status": "Ready", "Stage": "PR open"}, carried)
    check("a field the new board lacks (Size) is left out, not an error", carried["I_3"] == {"Status": "Backlog"}, carried.get("I_3"))
    check("... and says so", "not carried, the new board has no such field or option: Size=XL on #3" in r.stdout, r.stdout)
    check("says what it did not carry", "1 pull requests not carried" in r.stdout and "1 drafts not carried" in r.stdout
          and "1 from other repos left alone" in r.stdout, r.stdout)
    put = [c for c in calls if "-X PUT repos/lonestarprod/thing/branches/main/protection" in c]
    check("protects main on the new repo with the old checks", len(put) == 1 and "contexts][]=verify" in put[0]
          and "contexts][]=review-receipt" in put[0] and "required_approving_review_count]=0" in put[0], put)
    env = w.env_text()
    check("rewrites PROJECT_REPO, BOARD_OWNER, BOARD_NUMBER in place",
          "PROJECT_REPO=lonestarprod/thing\n" in env and "BOARD_OWNER=lonestarprod\n" in env and "BOARD_NUMBER=9\n" in env, env)
    check("adds OWNER_LOGIN (the old owner) right after BOARD_OWNER, with its comment",
          "BOARD_OWNER=lonestarprod\n# The person who answers Stuck cards" in env and "\nOWNER_LOGIN=roloc\n" in env, env)
    check("keeps every other line", "# Sourced by bin/spawn.sh.\n" in env and 'WORKTREE_COPY_DIRS=""\n' in env and "READY_PULL=1\n" in env, env)
    check("tells the person what is left: Apps, docs, the old board",
          "app-setup.py thing --org lonestarprod --repos lonestarprod/thing" in r.stdout and "gh project close 4 --owner roloc" in r.stdout, r.stdout[-600:])

    print("2. a rerun after a full move: nothing to do, nothing written")
    before = w.env_text()
    r = w.run()
    check("exits 0", r.returncode == 0, r.stderr[-400:])
    check("says the project is already under the organization", "already under lonestarprod" in r.stdout, r.stdout)
    check("made no gh call at all", w.called() == [], w.called())
    check("env unchanged", w.env_text() == before)
    w.close()

    print("3. a rerun of a move that stopped after the transfer and the copy")
    w = World()
    w.put("repos.json", {"roloc/thing": "roloc", "lonestarprod/thing": "lonestarprod", "lonestarprod/watcher": "lonestarprod"})
    w.put("projects-lonestarprod.json", [{"number": 1, "title": "Watcher", "closed": False}, {"number": 9, "title": "thing", "closed": False}])
    w.put("board-lonestarprod-9.json", {"id": "PVT_lonestarprod_9", "title": "thing", "fields": FIELDS, "items": [
        {"id": "N_1", "type": "ISSUE", "content": {"__typename": "Issue", "id": "I_1", "number": 1, "repository": {"nameWithOwner": "lonestarprod/thing"}},
         "values": {"Status": "Done", "Priority": "P1"}}]})
    by_id = w.get("board-by-id.json"); by_id["PVT_lonestarprod_9"] = ["lonestarprod", "9"]; w.put("board-by-id.json", by_id)
    r = w.run()
    calls = w.called()
    check("exits 0", r.returncode == 0, r.stderr[-400:] + r.stdout[-400:])
    check("does not transfer again", not any("transfer" in c for c in calls))
    check("reuses the board by title, no copy", not any("project copy" in c for c in calls) and "already exists" in r.stdout)
    check("adds only the two cards that were missing", len(w.get("board-lonestarprod-9.json")["items"]) == 3)
    check("keeps the checks the moved repo already requires, never a guessed 'verify' alone",
          any("PUT repos/lonestarprod/thing/branches/main/protection" in c and "contexts][]=verify" in c and "contexts][]=review-receipt" in c for c in calls)
          and "already requires" in r.stdout, [c for c in calls if "PUT" in c])
    check("writes the env file", "BOARD_NUMBER=9\n" in w.env_text())
    w.close()

    print("3b. ... and when no protection can be read anywhere, it stops and asks rather than guess")
    w = World()
    w.put("repos.json", {"roloc/thing": "roloc", "lonestarprod/thing": "lonestarprod", "lonestarprod/watcher": "lonestarprod"})
    os.remove(os.path.join(w.tmp, "protection.json"))
    r = w.run()
    check("exits 1 asking for --required-checks", r.returncode == 1 and "--required-checks" in r.stderr, r.stderr)
    check("no PUT, env untouched", not any("PUT" in c for c in w.called()) and w.env_text() == ENV_TEXT)
    r = w.run("--required-checks", "verify,review-receipt")
    check("--required-checks names them explicitly", r.returncode == 0
          and any("PUT" in c and "contexts][]=review-receipt" in c for c in w.called()), r.stderr[-300:])
    w.close()

    print("4. protection refused (a private repo in a Free organization) stops before the env file")
    w = World()
    touch(w, "protection-refused")
    r = w.run()
    check("exits 1", r.returncode == 1, r.returncode)
    check("names the choices", "Team" in r.stderr and "--no-protection" in r.stderr, r.stderr)
    check("the cards were carried (the step before)", len(w.get("board-lonestarprod-9.json")["items"]) == 3)
    check("env untouched", w.env_text() == ENV_TEXT)
    print("   ... and --no-protection on the rerun finishes the move")
    os.remove(os.path.join(w.tmp, "protection-refused"))
    r = w.run("--no-protection")
    check("exits 0", r.returncode == 0, r.stderr[-300:])
    check("no PUT", not any("PUT" in c for c in w.called()))
    check("says it skipped on purpose", "skipped on purpose" in r.stdout)
    check("env written", "PROJECT_REPO=lonestarprod/thing\n" in w.env_text())
    w.close()

    print("5. --dry-run reads everything and writes nothing")
    w = World()
    r = w.run("--dry-run")
    calls = w.called()
    check("exits 0", r.returncode == 0, r.stderr[-300:])
    check("no transfer, copy, link, add, set or PUT", not any(x in c for c in calls for x in ("transfer", "project copy", "project link", "mutation", "PUT")), calls)
    check("says what it would do", "would transfer roloc/thing" in r.stdout and "would copy lonestarprod/1" in r.stdout
          and "would add 3 cards" in r.stdout and "would set PROJECT_REPO=lonestarprod/thing" in r.stdout, r.stdout)
    check("env untouched", w.env_text() == ENV_TEXT)
    check("old board untouched", w.get("board-roloc-4.json") == OLD_BOARD)
    w.close()

    print("6. the board's own Item-added workflow resets a Status a moment late: the read-back sets it again")
    w = World()
    touch(w, "drift-once")
    r = w.run()
    check("exits 0", r.returncode == 0, r.stderr[-300:])
    sets = [c for c in w.called() if "updateProjectV2ItemFieldValue" in c]
    check("two set passes, not one", len(sets) == 2, len(sets))
    check("says it read back", "read-back pass" in r.stdout, r.stdout)
    new = {it["content"]["id"]: it["values"] for it in w.get("board-lonestarprod-9.json")["items"]}
    check("every Status ends where the old board had it", new["I_1"]["Status"] == "Done" and new["I_2"]["Status"] == "Ready", new)
    w.close()

    print("8. GitHub answers 502 to a mutation: the call is retried, the move finishes in one run")
    w = World()
    w.put("fail-5xx.json", 2)
    r = w.run()
    check("exits 0", r.returncode == 0, r.stderr[-300:] + r.stdout[-300:])
    check("says it retried, once per 502", r.stdout.count("GitHub returned 502; retrying in") == 2, r.stdout)
    check("ends in done", "== done ==" in r.stdout, r.stdout[-200:])
    new = {it["content"]["id"]: it["values"] for it in w.get("board-lonestarprod-9.json")["items"]}
    check("every card carried once, with its values", sorted(new) == ["I_1", "I_2", "I_3"]
          and new["I_1"] == {"Status": "Done", "Priority": "P1"} and new["I_2"] == {"Status": "Ready", "Stage": "PR open"}, new)
    check("env written", "BOARD_NUMBER=9\n" in w.env_text())
    w.close()

    print("8b. ... four 502s in a row is a failure, the same as before")
    w = World()
    w.put("fail-5xx.json", 4)
    r = w.run()
    check("exits 1 naming the 502", r.returncode == 1 and "HTTP 502" in r.stderr, r.stderr)
    check("three retries, then stops", r.stdout.count("retrying in") == 3, r.stdout)
    check("env untouched", w.env_text() == ENV_TEXT)
    w.close()

    print("8c. a 4xx is not retried")
    w = World()
    w.put("fail-4xx.json", 1)
    r = w.run()
    check("exits 1 at once", r.returncode == 1 and "HTTP 422" in r.stderr and "retrying" not in r.stdout, r.stderr + r.stdout[-200:])
    w.close()

    print("9. cards go five adds and ten sets to a request (the sizes a 375-card board survived)")
    w = World()
    many = {f"I_{n}": {"number": n, "repo": "roloc/thing"} for n in range(1, 13)}
    contents = w.get("contents.json"); contents.update(many); w.put("contents.json", contents)
    old = w.get("board-roloc-4.json")
    old["items"] = [{"id": f"O_{n}", "type": "ISSUE", "content": {"__typename": "Issue", "id": f"I_{n}", "number": n,
                     "repository": {"nameWithOwner": "roloc/thing"}}, "values": {"Status": "Backlog", "Priority": "P2"}} for n in range(1, 13)]
    w.put("board-roloc-4.json", old)
    r = w.run()
    adds = [c for c in w.called() if "addProjectV2ItemById" in c]
    sets = [c for c in w.called() if "updateProjectV2ItemFieldValue" in c]
    check("exits 0", r.returncode == 0, r.stderr[-300:])
    check("12 cards in 3 add requests", len(adds) == 3, len(adds))
    check("24 values in 3 set requests", len(sets) == 3, len(sets))
    check("all 12 carried", len(w.get("board-lonestarprod-9.json")["items"]) == 12)
    w.close()

    print("9b. a batch GitHub applied by half before the 502 (November 9's shape): the retry re-sends it, no card twice")
    w = World()
    contents = w.get("contents.json"); contents.update(many); w.put("contents.json", contents)
    w.put("board-roloc-4.json", old)
    w.put("fail-5xx-partial.json", 1)
    r = w.run()
    check("exits 0", r.returncode == 0, r.stderr[-300:])
    check("retried once", r.stdout.count("retrying in") == 1, r.stdout)
    items = w.get("board-lonestarprod-9.json")["items"]
    check("12 cards, each once", len(items) == 12 and len({it["content"]["id"] for it in items}) == 12, len(items))
    check("every card has its values", all(it["values"] == {"Status": "Backlog", "Priority": "P2"} for it in items),
          [it["values"] for it in items if it["values"] != {"Status": "Backlog", "Priority": "P2"}])
    w.close()

    print("10. the new-board read shows a card that is not there, once (November 9's moment-ahead read)")
    w = World()
    w.put("ghost-once.json", {"9": [{"content": {"__typename": "Issue", "id": "I_2", "number": 2,
                                                  "repository": {"nameWithOwner": "lonestarprod/thing"}},
                                     "values": {"Status": "Ready", "Stage": "PR open"}}]})
    r = w.run()
    check("exits 0", r.returncode == 0, r.stderr[-300:] + r.stdout[-300:])
    check("the first read counted the card as there, the comparison found it missing and added it again",
          "1 missing and 0 different" in r.stdout, r.stdout)
    new = {it["content"]["id"]: it["values"] for it in w.get("board-lonestarprod-9.json")["items"]}
    check("every card is on the new board with its values", sorted(new) == ["I_1", "I_2", "I_3"]
          and new["I_2"] == {"Status": "Ready", "Stage": "PR open"}, new)
    check("says reconciled: 3 cards, 3 match", "reconciled: 3 cards, 3 match" in r.stdout, r.stdout)
    check("ends in done, env written", "== done ==" in r.stdout and "BOARD_NUMBER=9\n" in w.env_text())
    w.close()

    print("10b. a card that cannot be placed: named by number, exit 1, env untouched")
    w = World()
    w.put("never-add.json", ["I_2"])
    r = w.run()
    check("exits 1", r.returncode == 1, r.returncode)
    check("prints the line with the card missing", "reconciled: 3 cards, 1 missing (#2), 0 differ" in r.stdout, r.stdout)
    check("names the card on stderr", "1 card(s) could not be placed" in r.stderr, r.stderr)
    check("tried once more, not forever: the carry's add and one repair add", len([c for c in w.called() if "addProjectV2ItemById" in c]) == 2, w.called())
    check("env untouched, no protection PUT, no done", w.env_text() == ENV_TEXT and not any("PUT" in c for c in w.called())
          and "== done ==" not in r.stdout)
    w.close()

    print("10c. a Status the board's own workflow resets late still ends reconciled")
    w = World()
    touch(w, "drift-once")
    r = w.run()
    check("exits 0 with every card matching", r.returncode == 0 and "reconciled: 3 cards, 3 match" in r.stdout,
          r.stdout[-300:])
    w.close()

    print("11. --recheck --from roloc/4 on a moved project reads both boards and writes nothing")
    w = World()
    w.run()
    env_after = w.env_text()
    r = w.run("--recheck", "--from", "roloc/4")
    calls = w.called()
    check("exits 0", r.returncode == 0, r.stderr[-300:] + r.stdout[-300:])
    check("reconciled: 3 cards, 3 match", "reconciled: 3 cards, 3 match" in r.stdout, r.stdout)
    check("read the old board and the new one", any("owner=roloc" in c and "number=4" in c for c in calls)
          and any("owner=lonestarprod" in c and "number=9" in c for c in calls), calls)
    check("no mutation, no transfer, no copy, no PUT", not any(x in c for c in calls for x in ("mutation", "transfer", "project copy", "PUT")), calls)
    check("env unchanged", w.env_text() == env_after)
    new = w.get("board-lonestarprod-9.json")
    new["items"] = [it for it in new["items"] if it["content"]["id"] != "I_3"]
    new["items"][0]["values"]["Status"] = "Backlog"
    w.put("board-lonestarprod-9.json", new)
    r = w.run("--recheck", "--from", "roloc/4")
    check("a card gone and a value off: exit 1, both named, still nothing written",
          r.returncode == 1 and "reconciled: 3 cards, 1 missing (#3), 1 differ" in r.stdout and "#1 Status: old board Done, new board Backlog" in r.stdout
          and not any("mutation" in c for c in w.called()), r.stdout + r.stderr)
    r = w.run("--recheck")
    check("without --from it refuses", r.returncode == 2 and "--from" in r.stderr, r.stderr)
    w.close()

    print("7. a wrong env file is refused before anything is touched")
    w = World()
    w.put("repos.json", {"lonestarprod/watcher": "lonestarprod"})
    r = w.run()
    check("exits 1 when the old repo is not where the env file says", r.returncode == 1 and "nothing moved" in r.stderr, r.stderr)
    check("no transfer", not any("transfer" in c for c in w.called()))
    w.close()

    print()
    print("FAIL" if fail else "all cases hold")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
