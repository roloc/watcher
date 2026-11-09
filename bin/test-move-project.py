#!/usr/bin/env python3
"""Offline test for deploy/move-project.py (card #268): a full move, a rerun, a 403 on protection,
a dry run and a drifted Status, every one against a stub `gh` (a script written here, GH_BIN) that
keeps the repo, the boards and the items in a JSON file and logs every write it is asked to make.
Run: python3 bin/test-move-project.py (exit 0 = all hold)."""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "deploy", "move-project.py")

STUB = r'''#!/usr/bin/env python3
import json, os, re, sys
path = os.environ["STUB_STATE"]
S = json.load(open(path))
argv = sys.argv[1:]
stdin = None
def save():
    json.dump(S, open(path, "w"))
def fail(msg, code=1):
    save(); sys.stderr.write(msg + "\n"); sys.exit(code)
def out(obj):
    save(); print(json.dumps(obj)); sys.exit(0)
def board_by_id(i):
    return S["boards"][i]

def graphql(query):
    m = re.search(r'repositoryOwner\(login:"([^"]+)"\).*?projectV2\(number:(\d+)\)', query, re.S)
    if m:
        for i, b in S["boards"].items():
            if b["owner"].lower() == m.group(1).lower() and b["number"] == int(m.group(2)):
                return {"repositoryOwner": {"projectV2": {"id": i, "number": b["number"], "title": b["title"]}}}
        return {"repositoryOwner": {"projectV2": None}}
    m = re.search(r'organization\(login:"([^"]+)"\)', query)
    if m:
        return {"organization": {"projectsV2": {"nodes": [{"id": i, "number": b["number"], "title": b["title"]}
                for i, b in S["boards"].items() if b["owner"].lower() == m.group(1).lower()]}}}
    if "repositories(first" in query:
        b = board_by_id(re.search(r'node\(id:"([^"]+)"', query).group(1))
        return {"node": {"repositories": {"nodes": [{"nameWithOwner": r} for r in b["repos"]]},
                         "fields": {"nodes": b["fields"]}}}
    if "items(first" in query:
        b = board_by_id(re.search(r'node\(id:"([^"]+)"', query).group(1))
        nodes = []
        for it in b["items"]:
            if S.get("late_workflow") and "late" in it:
                if it["late"] in (1, 2):
                    it["values"]["Status"] = "Backlog"   # the board's "Item added" workflow, a moment late
                    it["late"] = 0 if it["late"] == 2 else 1
            c = {"__typename": it["type"]}
            if it["type"] == "Issue":
                c.update({"id": it["content"], "number": it["number"]})
            nodes.append({"id": it["id"], "content": c, "fieldValues": {"nodes": [
                {"name": v, "field": {"name": k}} for k, v in it["values"].items()]}})
        return {"node": {"items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}
    if "addProjectV2ItemById" in query:
        for pid, cid in re.findall(r'projectId:"([^"]+)", contentId:"([^"]+)"', query):
            b = board_by_id(pid)
            S["writes"].append({"add": cid})
            b["items"].append({"id": "ITEM_" + cid, "type": "Issue", "number": int(cid.split("_")[1]), "content": cid,
                               "values": {}, "late": 1 if S.get("late_workflow") else 0})
        return {}
    if "updateProjectV2ItemFieldValue" in query:
        for pid, iid, fid, oid in re.findall(r'projectId:"([^"]+)", itemId:"([^"]+)", fieldId:"([^"]+)", value:\{singleSelectOptionId:"([^"]+)"\}', query):
            b = board_by_id(pid)
            f = next(f for f in b["fields"] if f["id"] == fid)
            it = next(i for i in b["items"] if i["id"] == iid)
            it["values"][f["name"]] = next(o["name"] for o in f["options"] if o["id"] == oid)
            S["writes"].append({"set": [it["number"], f["name"], it["values"][f["name"]], oid]})
            if it.get("late") == 1:
                it["late"] = 2
        return {}
    fail("stub: unhandled query " + query[:80])

if argv[0] == "api" and argv[1] == "graphql":
    q = next(a[6:] for a in argv if a.startswith("query="))
    out({"data": graphql(q)})
if argv[0] == "api":
    p = argv[1]
    method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
    body = json.loads(sys.stdin.read()) if "--input" in argv else None
    m = re.match(r"repos/([^/]+/[^/]+)(/.*)?$", p)
    full, rest = m.group(1).lower(), m.group(2) or ""
    full = S["redirects"].get(full, full)
    repo = S["repos"].get(full)
    if rest == "/transfer":
        S["writes"].append({"transfer": [full, body["new_owner"]]})
        new = body["new_owner"].lower() + "/" + full.split("/")[1]
        S["repos"][new] = S["repos"].pop(full)
        S["repos"][new]["protection"] = None   # the move is assumed to lose it; the script sets it again
        S["redirects"][full] = new
        S["pending"] = S.get("transfer_pending", 0)
        out({})
    if repo is None or S.get("pending") and full == next(iter(S["redirects"].values()), None) and method == "GET" and not rest:
        if repo is not None:
            S["pending"] -= 1
        fail("gh: Not Found (HTTP 404)")
    if rest == "":
        out({"full_name": full})
    if rest == "/branches/main/protection":
        if method == "GET":
            if repo.get("protection") is None:
                fail("gh: Branch not protected (HTTP 404)")
            out(repo["protection"])
        if S.get("protection_403"):
            fail("gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)")
        S["writes"].append({"protect": body})
        repo["protection"] = {"required_status_checks": {"contexts": body["required_status_checks"]["contexts"], "checks": []},
                              "required_pull_request_reviews": body["required_pull_request_reviews"]}
        out({})
    fail("stub: unhandled api " + p)
if argv[:2] == ["project", "copy"]:
    S["writes"].append({"copy": argv[2:]})
    t = next(b for b in S["boards"].values() if b["owner"] == argv[argv.index("--source-owner") + 1])
    new = {"owner": argv[argv.index("--target-owner") + 1], "number": 9, "title": argv[argv.index("--title") + 1],
           "repos": [], "fields": t["fields"], "items": []}
    S["boards"]["PVT_new"] = new
    out({"id": "PVT_new", "number": 9, "title": new["title"]})
if argv[:2] == ["project", "link"]:
    S["writes"].append({"link": argv[2:]})
    S["boards"]["PVT_new"]["repos"].append(argv[argv.index("--repo") + 1])
    out({})
fail("stub: unhandled " + " ".join(argv))
'''

fails = 0
count = 0


def check(name, ok, detail=""):
    global fails, count
    count += 1
    print(f"  {name:<92} {'ok' if ok else 'FAIL — ' + str(detail)[:400]}")
    if not ok:
        fails += 1


tmp = tempfile.mkdtemp()
stub_path = os.path.join(tmp, "gh")
open(stub_path, "w").write(STUB)
os.chmod(stub_path, 0o755)

ENV = """# riffmap
PROJECT_NAME=demo
PROJECT_ROOT=/home/watcher/projects/demo
BOARD_OWNER=roloc
PROJECT_REPO=roloc/demo
BOARD_NUMBER=3
WORKTREE_COPY_DIRS=""
"""


def opts(*names):
    return [{"id": f"OPT_{n.replace(' ', '_')}", "name": n} for n in names]


def fresh(**extra):
    status = {"id": "F_status", "name": "Status", "options": opts("Backlog", "Ready", "In progress", "Done")}
    s = {
        "writes": [], "redirects": {}, "repos": {"roloc/demo": {"protection": {
            "required_status_checks": {"contexts": ["verify"], "checks": [{"context": "review-receipt"}]},
            "required_pull_request_reviews": {"required_approving_review_count": 0}}}},
        "boards": {
            "PVT_old": {"owner": "roloc", "number": 3, "title": "Demo", "repos": ["roloc/demo"],
                        "fields": status["id"] and [status, {"id": "F_stage", "name": "Stage", "options": opts("Building", "PR open")},
                                                    {"id": "F_prio", "name": "Priority", "options": opts("P0", "P1")}],
                        "items": [
                            {"id": "I1", "type": "Issue", "number": 101, "content": "ISSUE_101", "values": {"Status": "Done", "Priority": "P0"}},
                            {"id": "I2", "type": "Issue", "number": 102, "content": "ISSUE_102", "values": {"Status": "In progress", "Stage": "PR open"}},
                            {"id": "I3", "type": "Issue", "number": 103, "content": "ISSUE_103", "values": {"Status": "Ready"}},
                            {"id": "I4", "type": "PullRequest", "number": 7, "content": "PR_7", "values": {"Status": "Done"}},
                            {"id": "I5", "type": "DraftIssue", "number": None, "content": None, "values": {}}]},
            # the template: Status, and Stage with one option fewer; no Priority field at all
            "PVT_tpl": {"owner": "lonestarprod", "number": 1, "title": "Watcher", "repos": [],
                        "fields": [status, {"id": "F_stage", "name": "Stage", "options": opts("Building")}], "items": []},
        },
    }
    s.update(extra)
    return s


def run_move(state, env_text=ENV, *args):
    d = tempfile.mkdtemp()
    sp, ep = os.path.join(d, "state.json"), os.path.join(d, "demo.env")
    json.dump(state, open(sp, "w"))
    open(ep, "w").write(env_text)
    return d, sp, ep, lambda *more: _go(sp, ep, args + more)


def _go(sp, ep, args):
    env = dict(os.environ, GH_BIN=stub_path, STUB_STATE=sp)
    r = subprocess.run([sys.executable, SCRIPT, "demo", "--to", "lonestarprod", "--template", "lonestarprod/1",
                        "--env-file", ep, "--poll", "0", "--settle", "0", *args], env=env, capture_output=True, text=True)
    return r, json.load(open(sp)), open(ep).read()


def kinds(state):
    return [list(w)[0] for w in state["writes"]]


print("1. a full move")
d, sp, ep, go = run_move(fresh(transfer_pending=2))
r, st, env = go()
check("exits 0", r.returncode == 0, r.stdout + r.stderr)
check("the repo is transferred once, to the organization, and the move waited for it", st["writes"][0] == {"transfer": ["roloc/demo", "lonestarprod"]} and kinds(st).count("transfer") == 1, st["writes"][:2])
copy = next(w["copy"] for w in st["writes"] if "copy" in w)
check("the template board is copied into the organization under the old board's title",
      copy[0] == "1" and copy[copy.index("--source-owner") + 1] == "lonestarprod" and copy[copy.index("--target-owner") + 1] == "lonestarprod"
      and copy[copy.index("--title") + 1] == "Demo", copy)
check("…and linked to the transferred repo", st["boards"]["PVT_new"]["repos"] == ["lonestarprod/demo"], st["boards"]["PVT_new"]["repos"])
check("the three issues are added by content id; the PR and the draft are not",
      sorted(w["add"] for w in st["writes"] if "add" in w) == ["ISSUE_101", "ISSUE_102", "ISSUE_103"], st["writes"])
new_items = {i["number"]: i["values"] for i in st["boards"]["PVT_new"]["items"]}
check("every card sits in the column it was in", {n: v.get("Status") for n, v in new_items.items()} == {101: "Done", 102: "In progress", 103: "Ready"}, new_items)
check("the right option ids were set (Status Done → OPT_Done)", {"set": [101, "Status", "Done", "OPT_Done"]} in st["writes"], st["writes"])
check("a value whose option or field is missing on the new board is left out, and said",
      "Stage" not in new_items[102] and "Priority" not in new_items[101] and "Priority=P0" in r.stdout and "Stage=PR open" in r.stdout, r.stdout)
prot = next(w["protect"] for w in st["writes"] if "protect" in w)
check("main is protected with the old checks, a PR required, 0 approvals",
      prot["required_status_checks"]["contexts"] == ["review-receipt", "verify"]
      and prot["required_pull_request_reviews"] == {"required_approving_review_count": 0} and prot["enforce_admins"] is False, prot)
check("the env file: repo, owner, number rewritten in place, OWNER_LOGIN added after them, the rest kept",
      env == ENV.replace("BOARD_OWNER=roloc", "BOARD_OWNER=lonestarprod").replace("PROJECT_REPO=roloc/demo", "PROJECT_REPO=lonestarprod/demo")
      .replace("BOARD_NUMBER=3", "BOARD_NUMBER=9\nOWNER_LOGIN=roloc"), env)

print("2. a rerun writes nothing")
st["writes"] = []
json.dump(st, open(sp, "w"))
r, st2, env2 = go()
check("exits 0, no write of any kind", r.returncode == 0 and st2["writes"] == [], (r.stderr, st2["writes"]))
check("the env file is unchanged", env2 == env)
open(ep, "w").write(ENV)
r, st2, env2 = go()
check("a rerun from the old env file (the last step undone) writes only the env file", r.returncode == 0 and st2["writes"] == [] and env2 == env, (r.stdout, st2["writes"]))

print("3. a 403 on protection stops before the env file")
d, sp, ep, go = run_move(fresh(protection_403=True))
r, st, env = go()
check("exits 3", r.returncode == 3, r.returncode)
check("the env file is untouched", env == ENV)
check("the message names the choices and the old checks", "--no-protection" in r.stderr and "plan" in r.stderr and "review-receipt, verify" in r.stderr, r.stderr)
check("the repo, board and cards were moved before it stopped", "transfer" in kinds(st) and "copy" in kinds(st) and kinds(st).count("add") == 3 and "protect" not in kinds(st), kinds(st))
r, st, env = go()
check("a plain rerun then asks for --required-checks or --no-protection, never guesses",
      r.returncode == 3 and "--required-checks" in r.stderr and env == ENV and "protect" not in kinds(st), (r.returncode, r.stderr))
r, st, env = go("--no-protection")
check("rerun with --no-protection finishes: the env file written, no protection call", r.returncode == 0 and "PROJECT_REPO=lonestarprod/demo" in env and "protect" not in kinds(st), r.stderr)
check("…and it did not transfer or copy again", kinds(st).count("transfer") == 1 and kinds(st).count("copy") == 1, kinds(st))

print("4. a dry run writes nothing")
d, sp, ep, go = run_move(fresh())
r, st, env = go("--dry-run")
check("exits 0, no write to GitHub", r.returncode == 0 and st["writes"] == [] and "roloc/demo" in st["repos"], (r.stderr, st["writes"]))
check("the env file is untouched", env == ENV)
check("it says what it would do", "would transfer roloc/demo" in r.stdout and "would copy" in r.stdout and "would add 3 issues" in r.stdout and "would write" in r.stdout, r.stdout)

print("5. a drifted Status is set again on the read-back")
d, sp, ep, go = run_move(fresh(late_workflow=True))
r, st, env = go()
new_items = {i["number"]: i["values"].get("Status") for i in st["boards"]["PVT_new"]["items"]}
check("the workflow's late Backlog is overwritten: every card ends in its own column", r.returncode == 0 and new_items == {101: "Done", 102: "In progress", 103: "Ready"}, (new_items, r.stdout + r.stderr))
check("…which took more sets than one per card", sum(1 for w in st["writes"] if "set" in w and w["set"][1] == "Status") > 3, st["writes"])

print("6. no protection on the old repo, and --required-checks")
s = fresh()
s["repos"]["roloc/demo"]["protection"] = None
d, sp, ep, go = run_move(s)
r, st, env = go()
check("none on the old repo and nothing named: protection left alone", r.returncode == 0 and "protect" not in kinds(st), r.stdout)
d, sp, ep, go = run_move(s)
r, st, env = go("--required-checks", "ci,lint")
prot = next((w["protect"] for w in st["writes"] if "protect" in w), {})
check("--required-checks sets exactly those", prot.get("required_status_checks", {}).get("contexts") == ["ci", "lint"], st["writes"])

print(f"{count} cases, {'all passed' if not fails else 'FAILURES ABOVE'}")
sys.exit(1 if fails else 0)
