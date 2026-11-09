#!/usr/bin/env python3
"""Offline test for deploy/app-setup.py (cards #255, #261): the manifests, then the whole flow for
two projects on one page against a stub GitHub (conversion, bot lookup, installation list, the
installation's permissions, the organization's id) that runs on a local port. The key is a
throwaway made here with openssl. Run: python3 bin/test-app-setup.py (exit 0 = all hold)."""
import http.server
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("app_setup", os.path.join(HERE, "..", "deploy", "app-setup.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fails = 0
count = 0


def check(name, ok, detail=""):
    global fails, count
    count += 1
    print(f"  {name:<86} {'ok' if ok else 'FAIL — ' + str(detail)}")
    if not ok:
        fails += 1


tmp = tempfile.mkdtemp()
key = os.path.join(tmp, "fixture.pem")
subprocess.run(["openssl", "genrsa", "-out", key, "2048"], capture_output=True, check=True)
PEM = open(key).read()
INSTALLED = {}             # slug -> installation id, once "the owner clicked Install"
PERMS = {}                 # installation id -> permissions GitHub reports
SEEN = {"jwt": None, "converted": []}
APP_IDS = {"demo-author": 1111, "demo-reviewer": 4444, "other-author": 5555, "other-reviewer": 6666}


class Stub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        m = re.match(r"/app-manifests/([a-z]+-[a-z]+)-code/conversions$", self.path)
        if not m:
            return self.reply({"message": "no"}, 404)
        which = m.group(1)                      # e.g. demo-author
        SEEN["converted"].append(which)
        self.reply({"id": APP_IDS[which], "slug": f"roloc-{which}", "pem": PEM,
                    "client_secret": "never-stored", "webhook_secret": "never-stored"}, 201)

    def do_GET(self):
        if self.path == "/orgs/lonestarprod":
            return self.reply({"id": 777, "login": "lonestarprod"})
        if self.path.startswith("/users/"):
            return self.reply({"id": 3333})
        if self.path == "/app/installations":
            SEEN["jwt"] = self.headers.get("Authorization")
            # which App asks is in the JWT's issuer; the stub answers every installed App's id
            iss = json.loads(_b64(self.headers.get("Authorization", "")))["iss"]
            slug = next((s for s, a in APP_IDS.items() if str(a) == iss), None)
            inst = INSTALLED.get(f"roloc-{slug}")
            return self.reply([{"id": inst, "account": {"login": "LoneStarProd"}}] if inst else [])
        m = re.match(r"/app/installations/(\d+)$", self.path)
        if m:
            return self.reply({"id": int(m.group(1)), "permissions": PERMS.get(int(m.group(1)), {})})
        self.reply({"message": "no"}, 404)


def _b64(auth):
    import base64
    body = auth.split(".")[1]
    return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()


stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()
mod.API = f"http://127.0.0.1:{stub.server_port}"
mod.WEB = "https://github.test"

print("1. the manifests (card #261: what a tick needs, nothing more)")
a = mod.build_manifest("demo", "author", "lonestarprod", 8799)
r = mod.build_manifest("demo", "reviewer", "lonestarprod", 8799)
check("the author App: Contents, Pull requests, Workflows, Issues, Projects, Actions write; Checks, Commit statuses read",
      a["default_permissions"] == {"contents": "write", "pull_requests": "write", "workflows": "write", "issues": "write",
                                   "organization_projects": "write", "actions": "write", "checks": "read", "statuses": "read"},
      a["default_permissions"])
check("the reviewer App: Pull requests write, Contents read — and no Actions",
      r["default_permissions"] == {"pull_requests": "write", "contents": "read"} and "actions" not in r["default_permissions"],
      r["default_permissions"])
check("named roloc-<repo name>-<role>, private, no webhook, back to this helper naming the project",
      a["name"] == "roloc-demo-author" and r["name"] == "roloc-demo-reviewer" and a["public"] is False
      and a["hook_attributes"]["active"] is False and a["redirect_url"] == "http://localhost:8799/created?project=demo&role=author", a)

print("2. a project's repo comes from its env file; the key folder is the repo's own name")
root = os.path.join(tmp, "root")
os.makedirs(os.path.join(root, "projects"))
open(os.path.join(root, "projects", "demo.env"), "w").write("# a project\nBOARD_OWNER=lonestarprod\nPROJECT_REPO=lonestarprod/demo\nREADY_PULL=1\n")
open(os.path.join(root, "projects", "nulltrader.env"), "w").write("PROJECT_REPO=lonestarprod/NullTrader\n")
open(os.path.join(root, "projects", "noline.env"), "w").write("BOARD_OWNER=lonestarprod\n")
check("PROJECT_REPO read from projects/<name>.env", mod.project_repo("demo", root) == "lonestarprod/demo", mod.project_repo("demo", root))
check("no file, or no PROJECT_REPO line: None", mod.project_repo("missing", root) is None and mod.project_repo("noline", root) is None)
apps_dir = os.path.join(tmp, "apps")
cased = mod.Project("nulltrader", [mod.project_repo("nulltrader", root)], apps_dir)
other = mod.Project("other", [], apps_dir)
check("the folder and App name follow the repo's case (bin/spawn.sh's ${PROJECT_REPO##*/}), not the env name",
      cased.key == "NullTrader" and cased.dir == os.path.join(apps_dir, "NullTrader") and mod.app_name(cased.key, "author") == "roloc-NullTrader-author"
      and other.key == "other", (cased.key, cased.dir))
# the flow's project: its env file names lonestarprod/demo, the slug the stub answers with. The command-line
# case below builds the same Project from that file in a fresh process, so the two must agree on the folder
# on a case-sensitive filesystem too (CI is Linux; a Mac would hide a Demo/demo mismatch).
demo = mod.Project("demo", [mod.project_repo("demo", root)], apps_dir)

print("3. the whole flow, two projects on one page")
setup = mod.Setup("lonestarprod", [demo, other], ["author", "reviewer"], 0)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod.handler(setup))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"


def get(path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(NoRedirect)


def created(project, role):
    try:
        opener.open(base + f"/created?project={project}&role={role}&code={project}-{role}-code&state={setup.state}", timeout=10)
        return None
    except urllib.error.HTTPError as e:
        return e.code


status, page = get("/")
check("one section per project, a Create form per App (four), each posting to the organization's new-App page",
      status == 200 and page.count("<h2>") == 2 and page.count("<form") == 4
      and page.count(f"https://github.test/organizations/lonestarprod/settings/apps/new?state={setup.state}") == 4, page[:300])
check("…whose manifest fields carry the App names", all(f"roloc-{k}" in page for k in APP_IDS))
check("a project with no env file says so on the page", "repo not known here (no projects/other.env)" in page)

status, body = get("/created?project=demo&role=author&code=x&state=wrong")
check("a callback with the wrong state is refused and writes nothing", status == 400 and not os.path.exists(os.path.join(demo.dir, "author.pem")), body)
status, body = get("/created?project=nobody&role=author&code=x&state=" + setup.state)
check("an unknown project is a 404", status == 404)
code = created("demo", "author")
check("the right state: the code is traded and the browser sent back to the main page", code == 302 and SEEN["converted"] == ["demo-author"], code)
pem_path = os.path.join(demo.dir, "author.pem")
check("the key is written as the App sent it, not readable by others",
      open(pem_path).read() == PEM and stat.S_IMODE(os.stat(pem_path).st_mode) & 0o077 == 0, oct(os.stat(pem_path).st_mode))
cfg = mod.read_config(demo.config_path)
check("config holds the App ID, slug and bot id — and no secret", cfg == {"AUTHOR_APP_ID": "1111", "AUTHOR_SLUG": "roloc-demo-author", "AUTHOR_BOT_ID": "3333"}, cfg)
check("the other project's folder is untouched", not os.path.exists(other.config_path))
status, page = get("/")
check("the page offers the install for that App, pointed at the organization, naming the repo; three Creates remain",
      "https://github.test/apps/roloc-demo-author/installations/new/permissions?target_id=777" in page
      and "pick lonestarprod/demo" in page and page.count("<form") == 3 and "never-stored" not in page, page[:600])
check("nothing the page says holds the key", "BEGIN" not in page)

status, body = get("/status?project=demo&role=author")
check("before the owner installs: not installed, a signed JWT was presented",
      json.loads(body) == {"installed": False, "missing": None} and (SEEN["jwt"] or "").startswith("Bearer ey"), body)
INSTALLED["roloc-demo-author"] = 2222
PERMS[2222] = {"contents": "write", "pull_requests": "write", "workflows": "write", "issues": "write",
               "organization_projects": "write", "metadata": "read"}   # the pre-#261 shape
status, body = get("/status?project=demo&role=author")
check("after: installed, the installation ID lands in config (login compared without case), and the gap is named",
      json.loads(body) == {"installed": True, "missing": ["actions", "checks", "statuses"]}
      and mod.read_config(demo.config_path).get("AUTHOR_INSTALLATION_ID") == "2222", body)
status, page = get("/")
check("the page names the gaps the way GitHub's settings page does and links where to add and accept them",
      "missing permissions" in page and "Actions: Read and write; Checks: Read-only; Commit statuses: Read-only" in page
      and "https://github.test/organizations/lonestarprod/settings/apps/roloc-demo-author/permissions" in page
      and "https://github.test/organizations/lonestarprod/settings/installations" in page, page[:900])
check("the run is not finished until every App of every project is installed", not setup.done.is_set())

for proj, role in (("demo", "reviewer"), ("other", "author"), ("other", "reviewer")):
    created(proj, role)
    INSTALLED[f"roloc-{proj}-{role}"] = {"demo-reviewer": 2223, "other-author": 2224, "other-reviewer": 2225}[f"{proj}-{role}"]
PERMS[2223] = {"pull_requests": "write", "contents": "read", "actions": "write", "metadata": "read"}  # extra Actions: not a gap
PERMS[2224] = mod.PERMISSIONS["author"]
PERMS[2225] = {"pull_requests": "write", "contents": "write"}                                     # write where read is asked: fine
get("/status?project=demo&role=reviewer")
get("/status?project=other&role=author")
status, body = get("/status?project=other&role=reviewer")
check("all four installed: the run is finished", setup.done.is_set() and setup.finished(), (mod.read_config(demo.config_path), mod.read_config(other.config_path)))
check("an App with more than asked (extra Actions, write for read) is not reported as missing anything",
      setup.missing_permissions(demo, "reviewer") == [] and setup.missing_permissions(other, "reviewer") == [] and setup.missing_permissions(other, "author") == [],
      (setup.missing_permissions(demo, "reviewer"), setup.missing_permissions(other, "reviewer")))
status, page = get("/")
body_only = page.split("<script>")[0]
check("the finished page: three installed ✓, one still flagged", body_only.count("installed ✓") == 3 and body_only.count("missing permissions") == 1, body_only[:900])
status, body = get("/created?project=demo&role=wizard&code=x&state=" + setup.state)
check("an unknown role is a 404", status == 404)

print("4. the command line")
env = dict(os.environ, GH_APP_API=mod.API, GH_APP_WEB=mod.WEB, WATCHER_ROOT=root)
script = os.path.join(HERE, "..", "deploy", "app-setup.py")
out = subprocess.run([sys.executable, script, "demo", "other", "--repos", "x/y", "--apps-dir", apps_dir], env=env, capture_output=True, text=True)
check("--repos with several projects is refused", out.returncode != 0 and "one project" in out.stderr, out.stderr)
open(os.path.join(root, "projects", "demo-triage.env"), "w").write('. "$(dirname "${BASH_SOURCE[0]}")/demo.env"\nPROJECT_NAME=demo-triage\n')
out = subprocess.run([sys.executable, script, "demo-triage", "--apps-dir", apps_dir], env=env, capture_output=True, text=True, timeout=30)
check("a lane file's name (an env with no PROJECT_REPO) is refused, not given Apps of its own",
      out.returncode != 0 and "lane file" in out.stderr and not os.path.exists(os.path.join(apps_dir, "demo-triage")), out.stderr)
out = subprocess.run([sys.executable, script, "demo", "other", "--apps-dir", apps_dir, "--roles", "author,reviewer", "--port", "0"],
                     env=env, capture_output=True, text=True, timeout=30)
check("every App already installed: reports each, flags the one with missing permissions, creates nothing, exits 0",
      out.returncode == 0 and "nothing to create" in out.stdout and "already set up" in out.stdout
      and out.stdout.count("MISSING permissions: Actions: Read and write; Checks: Read-only; Commit statuses: Read-only") == 1
      and "settings/apps/roloc-demo-author/permissions" in out.stdout, out.stdout + out.stderr)

print("5. the offline token script reads what this wrote")
env = dict(os.environ, GH_APP_DIR=demo.dir)
out = subprocess.run([os.path.join(HERE, "gh-app-token.sh"), "demo", "author", "--identity"], env=env, capture_output=True, text=True)
check("gh-app-token.sh --identity finds the config", out.stdout.strip() == "roloc-demo-author[bot] <3333+roloc-demo-author[bot]@users.noreply.github.com>", out.stdout + out.stderr)

stub.shutdown()
srv.shutdown()
print(f"{count} cases, {'all passed' if not fails else 'FAILURES ABOVE'}")
sys.exit(1 if fails else 0)
