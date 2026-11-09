#!/usr/bin/env python3
"""deploy/app-setup.py <project> [--org ORG] [--repos owner/a,owner/b] [--roles author,reviewer]  (card #255)

Creates a project's GitHub Apps through GitHub's manifest flow and files their keys where
bin/gh-app-token.sh reads them. Standard library only, like bin/browse.py. Run on the box (as
root) or on a Mac, then open the printed address in a browser:

  1. The page has one prefilled form per App. Click Create (twice, once per App); GitHub sends
     the browser back here with a one-time code.
  2. This helper trades the code for the App's ID and private key and writes them straight into
     place — the key is never printed, never in a URL, a page or a log.
  3. The page then sends the browser to the App's install screen: pick the project's repos
     and click Install. The helper sees the installation by itself and writes its ID.

  box (root)   /etc/watcher/apps/<project>/{author,reviewer}.pem and config   (install.sh sets owners)
  Mac          ~/.config/roloc-apps/<project>/                                (author only: no process
               boundary on a Mac, so no reviewer App there)

App names are roloc-<project>-<role>; the author gets Contents, Pull requests, Workflows, Issues
and Projects (organization) write, the reviewer Pull requests write, Contents read, Actions write.
GH_APP_API and GH_APP_WEB point the helper somewhere else (bin/test-app-setup.py).
"""
import argparse
import html
import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GH_APP_API", "https://api.github.com")
WEB = os.environ.get("GH_APP_WEB", "https://github.com")

PERMISSIONS = {
    "author": {"contents": "write", "pull_requests": "write", "workflows": "write",
               "issues": "write", "organization_projects": "write"},
    "reviewer": {"pull_requests": "write", "contents": "read", "actions": "write"},
}


def app_name(project, role):
    return f"roloc-{project}-{role}"


def build_manifest(project, role, org, port):
    return {
        "name": app_name(project, role),
        "url": f"{WEB}/{org}",
        "hook_attributes": {"url": "https://example.invalid/unused", "active": False},
        "redirect_url": f"http://localhost:{port}/created?role={role}",
        "public": False,
        "default_permissions": PERMISSIONS[role],
        "default_events": [],
    }


def read_config(path):
    out = {}
    try:
        for line in open(path):
            k, sep, v = line.strip().partition("=")
            if sep:
                out[k] = v
    except FileNotFoundError:
        pass
    return out


def write_config(path, values):
    tmp = path + ".new"
    with open(tmp, "w") as f:
        for k in sorted(values):
            f.write(f"{k}={values[k]}\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def write_key(path, pem, group=None):
    """The key goes to a file that never has looser permissions than it ends with."""
    old = os.umask(0o077)
    try:
        tmp = path + ".new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        if os.geteuid() == 0 and group:
            try:
                shutil.chown(tmp, user="root", group=group)
                os.chmod(tmp, 0o640)
            except (LookupError, OSError):
                pass  # no such group yet: install.sh sets owners on the next deploy
        os.replace(tmp, path)
    finally:
        os.umask(old)


def http_json(url, method="GET", headers=None, body=b""):
    req = urllib.request.Request(url, data=body if method == "POST" else None, method=method,
                                 headers={"Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def jwt_for(app_id, key_path):
    def b64(b):
        import base64
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    now = int(time.time())
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"iat": now - 60, "exp": now + 480, "iss": str(app_id)}).encode())
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_path],
                         input=f"{head}.{body}".encode(), capture_output=True, check=True).stdout
    return f"{head}.{body}.{b64(sig)}"


class Setup:
    def __init__(self, project, org, repos, roles, directory, port):
        self.project, self.org, self.repos, self.roles = project, org, repos, roles
        self.dir, self.port = directory, port
        self.state = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.done = threading.Event()
        os.makedirs(self.dir, exist_ok=True)
        self.config_path = os.path.join(self.dir, "config")

    # -- state on disk
    def config(self):
        return read_config(self.config_path)

    def has(self, role, field):
        return bool(self.config().get(f"{role.upper()}_{field}"))

    def finished(self):
        return all(self.has(r, "INSTALLATION_ID") for r in self.roles)

    # -- the steps
    def convert(self, role, code):
        data = http_json(f"{API}/app-manifests/{urllib.parse.quote(code)}/conversions", "POST")
        pem = data.pop("pem")
        write_key(os.path.join(self.dir, f"{role}.pem"), pem, "watcher-reviewer" if role == "reviewer" else "watcher")
        values = self.config()
        pre = role.upper()
        values.update({f"{pre}_APP_ID": str(data["id"]), f"{pre}_SLUG": data["slug"]})
        try:
            bot = http_json(f"{API}/users/{urllib.parse.quote(data['slug'] + '[bot]')}")
            values[f"{pre}_BOT_ID"] = str(bot["id"])
        except (urllib.error.URLError, KeyError, ValueError):
            pass
        write_config(self.config_path, values)
        return data["slug"]

    def find_installation(self, role):
        """The installation on the organization, once the owner has clicked Install."""
        cfg = self.config()
        pre = role.upper()
        if cfg.get(f"{pre}_INSTALLATION_ID"):
            return True
        if not cfg.get(f"{pre}_APP_ID"):
            return False
        jwt = jwt_for(cfg[f"{pre}_APP_ID"], os.path.join(self.dir, f"{role}.pem"))
        found = http_json(f"{API}/app/installations", headers={"Authorization": f"Bearer {jwt}"})
        for inst in found:
            if (inst.get("account") or {}).get("login", "").lower() == self.org.lower():
                cfg[f"{pre}_INSTALLATION_ID"] = str(inst["id"])
                write_config(self.config_path, cfg)
                return True
        return False

    # -- the page
    def page(self):
        rows = []
        for role in self.roles:
            if self.has(role, "APP_ID"):
                slug = self.config().get(f"{role.upper()}_SLUG", app_name(self.project, role))
                where = html.escape(", ".join(self.repos) or "the project's repos")
                rows.append(f'<li><b>{html.escape(slug)}</b> created. '
                            f'<a href="{WEB}/apps/{html.escape(slug)}/installations/new" target="_blank">Install it</a> '
                            f'on {where} '
                            f'<span class="st" data-role="{role}">waiting for the install…</span></li>')
            else:
                manifest = html.escape(json.dumps(build_manifest(self.project, role, self.org, self.port)), quote=True)
                target = f"{WEB}/organizations/{urllib.parse.quote(self.org)}/settings/apps/new?state={self.state}"
                rows.append(f'<li><form action="{target}" method="post"><input type="hidden" name="manifest" value="{manifest}">'
                            f'<button>Create {html.escape(app_name(self.project, role))}</button></form></li>')
        script = ("<script>setInterval(()=>document.querySelectorAll('.st').forEach(e=>fetch('/status?role='+e.dataset.role)"
                  ".then(r=>r.json()).then(j=>{if(j.installed)e.textContent='installed ✓'})),2500)</script>")
        return (f"<!doctype html><meta charset=utf-8><title>Apps for {html.escape(self.project)}</title>"
                f"<h1>GitHub Apps for {html.escape(self.project)}</h1><ul>{''.join(rows)}</ul>{script}")


def handler(setup):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # the code and state are in these URLs; keep them out of any log
            pass

        def send(self, status, body, ctype="text/html; charset=utf-8", location=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            role = (q.get("role") or [""])[0]
            if u.path == "/":
                return self.send(200, setup.page())
            if role not in setup.roles:
                return self.send(404, "no such role")
            if u.path == "/created":
                if (q.get("state") or [""])[0] != setup.state or not q.get("code"):
                    return self.send(400, "the state did not match this run; start again from the main page")
                with setup.lock:
                    try:
                        setup.convert(role, q["code"][0])
                    except (urllib.error.URLError, KeyError, ValueError, OSError) as e:
                        return self.send(502, f"GitHub would not hand over the App: {html.escape(str(e))}")
                return self.send(302, "", location="/")
            if u.path == "/status":
                with setup.lock:
                    try:
                        ok = setup.find_installation(role)
                    except (urllib.error.URLError, ValueError, subprocess.CalledProcessError, OSError):
                        ok = False
                    if setup.finished():
                        setup.done.set()
                return self.send(200, json.dumps({"installed": ok}), "application/json")
            return self.send(404, "not found")
    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create a project's GitHub Apps and file their keys.")
    ap.add_argument("project")
    ap.add_argument("--org", default="lonestarprod")
    ap.add_argument("--repos", default="", help="comma-separated owner/name list, shown on the install step")
    ap.add_argument("--roles", help="default: author,reviewer as root on the box; author alone elsewhere")
    ap.add_argument("--dir", help="default: /etc/watcher/apps/<project> as root, else ~/.config/roloc-apps/<project>")
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args(argv)
    root = os.geteuid() == 0
    roles = (a.roles or ("author,reviewer" if root else "author")).split(",")
    if any(r not in PERMISSIONS for r in roles):
        sys.exit("app-setup: roles are author and reviewer")
    directory = a.dir or (f"/etc/watcher/apps/{a.project}" if root else os.path.expanduser(f"~/.config/roloc-apps/{a.project}"))
    setup = Setup(a.project, a.org, [r for r in a.repos.split(",") if r], roles, directory, a.port)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler(setup))
    print(f"app-setup: open http://localhost:{a.port}/ in your browser (writing to {directory})", flush=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        while not setup.done.wait(1):
            pass
    except KeyboardInterrupt:
        sys.exit("app-setup: stopped before every App was installed")
    server.shutdown()
    cfg = setup.config()
    for r in roles:
        print(f"app-setup: {r}: App {cfg.get(r.upper() + '_APP_ID')}, installation {cfg.get(r.upper() + '_INSTALLATION_ID')}")
    print(f"app-setup: done. Check with: GH_APP_DIR={directory} bin/gh-app-token.sh {a.project} author --identity")


if __name__ == "__main__":
    main()
