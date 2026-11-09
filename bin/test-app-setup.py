#!/usr/bin/env python3
"""Offline test for deploy/app-setup.py (card #255): the manifest, then the whole flow against
a stub GitHub (conversion, bot lookup, installation list) that runs on a local port. The key is
a throwaway made here with openssl. Run: python3 bin/test-app-setup.py (exit 0 = all hold)."""
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
INSTALLED = {"yes": False}
SEEN = {"jwt": None, "converted": []}


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
        m = re.match(r"/app-manifests/([a-z]+)-code/conversions$", self.path)
        if not m:
            return self.reply({"message": "no"}, 404)
        SEEN["converted"].append(m.group(1))
        role = m.group(1)
        self.reply({"id": 1111 if role == "author" else 4444, "slug": f"roloc-demo-{role}", "pem": PEM,
                    "client_secret": "never-stored", "webhook_secret": "never-stored"}, 201)

    def do_GET(self):
        if self.path.startswith("/users/"):
            return self.reply({"id": 3333})
        if self.path == "/app/installations":
            SEEN["jwt"] = self.headers.get("Authorization")
            return self.reply([{"id": 2222, "account": {"login": "LoneStarProd"}}] if INSTALLED["yes"] else [])
        self.reply({"message": "no"}, 404)


stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()
mod.API = f"http://127.0.0.1:{stub.server_port}"
mod.WEB = "https://github.test"

print("1. the manifest")
a = mod.build_manifest("demo", "author", "lonestarprod", 8799)
r = mod.build_manifest("demo", "reviewer", "lonestarprod", 8799)
check("the author App: Contents, Pull requests, Workflows, Issues, Projects — all write",
      a["default_permissions"] == {"contents": "write", "pull_requests": "write", "workflows": "write",
                                   "issues": "write", "organization_projects": "write"}, a["default_permissions"])
check("the reviewer App: Pull requests write, Contents read, Actions write",
      r["default_permissions"] == {"pull_requests": "write", "contents": "read", "actions": "write"}, r["default_permissions"])
check("named roloc-<project>-<role>, private, no webhook, back to this helper",
      a["name"] == "roloc-demo-author" and r["name"] == "roloc-demo-reviewer" and a["public"] is False
      and a["hook_attributes"]["active"] is False and a["redirect_url"] == "http://localhost:8799/created?role=author", a)

print("2. the whole flow")
d = os.path.join(tmp, "apps", "demo")
setup = mod.Setup("demo", "lonestarprod", ["lonestarprod/demo"], ["author", "reviewer"], d, 0)
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

status, page = get("/")
check("the main page has a Create form per App, posting to the organization's new-App page",
      status == 200 and page.count("<form") == 2 and f"https://github.test/organizations/lonestarprod/settings/apps/new?state={setup.state}" in page, page[:300])
check("…whose manifest field carries the App's name", "roloc-demo-author" in page and "roloc-demo-reviewer" in page)

status, body = get("/created?role=author&code=author-code&state=wrong")
check("a callback with the wrong state is refused and writes nothing", status == 400 and not os.path.exists(os.path.join(d, "author.pem")), body)
try:
    opener.open(base + f"/created?role=author&code=author-code&state={setup.state}", timeout=10)
    code = None
except urllib.error.HTTPError as e:
    code = e.code
check("the right state: the code is traded and the browser sent back to the main page", code == 302 and SEEN["converted"] == ["author"], code)
pem_path = os.path.join(d, "author.pem")
check("the key is written as the App sent it, not readable by others",
      open(pem_path).read() == PEM and stat.S_IMODE(os.stat(pem_path).st_mode) & 0o077 == 0, oct(os.stat(pem_path).st_mode))
cfg = mod.read_config(os.path.join(d, "config"))
check("config holds the App ID, slug and bot id — and no secret", cfg == {"AUTHOR_APP_ID": "1111", "AUTHOR_SLUG": "roloc-demo-author", "AUTHOR_BOT_ID": "3333"}, cfg)
status, page = get("/")
check("the page now offers the install for that App and still the Create for the other",
      "https://github.test/apps/roloc-demo-author/installations/new" in page and page.count("<form") == 1 and "never-stored" not in page, page[:400])
check("nothing the page says holds the key", "BEGIN" not in page)

status, body = get("/status?role=author")
check("before the owner installs: not installed, a signed JWT was presented",
      json.loads(body) == {"installed": False} and (SEEN["jwt"] or "").startswith("Bearer ey"), body)
INSTALLED["yes"] = True
status, body = get("/status?role=author")
check("after: installed, and the installation ID lands in config (login compared without case)",
      json.loads(body) == {"installed": True} and mod.read_config(os.path.join(d, "config")).get("AUTHOR_INSTALLATION_ID") == "2222", body)
check("the run is not finished until the reviewer is installed too", not setup.done.is_set())
try:
    opener.open(base + f"/created?role=reviewer&code=reviewer-code&state={setup.state}", timeout=10)
except urllib.error.HTTPError:
    pass
get("/status?role=reviewer")
check("both installed: the run is finished", setup.done.is_set() and setup.finished(), mod.read_config(os.path.join(d, "config")))
status, body = get("/created?role=wizard&code=x&state=" + setup.state)
check("an unknown role is a 404", status == 404)

print("3. the offline token script reads what this wrote")
env = dict(os.environ, GH_APP_DIR=d)
out = subprocess.run([os.path.join(HERE, "gh-app-token.sh"), "demo", "author", "--identity"], env=env, capture_output=True, text=True)
check("gh-app-token.sh --identity finds the config", out.stdout.strip() == "roloc-demo-author[bot] <3333+roloc-demo-author[bot]@users.noreply.github.com>", out.stdout + out.stderr)

stub.shutdown()
srv.shutdown()
print(f"{count} cases, {'all passed' if not fails else 'FAILURES ABOVE'}")
sys.exit(1 if fails else 0)
