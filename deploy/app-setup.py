#!/usr/bin/env python3
"""deploy/app-setup.py <project> [<project>…] [--org ORG] [--repos owner/a] [--all-repos] [--roles author,reviewer]  (cards #255, #261, #288)

Creates each project's GitHub Apps through GitHub's manifest flow and files their keys where
bin/gh-app-token.sh reads them. Standard library only, like bin/browse.py. Run on the box (as
root) or on a Mac, then open the printed address in a browser. The address is localhost, and
GitHub sends the browser back to it, so on the box first open a tunnel from the Mac:

  ssh -L 8799:127.0.0.1:8799 root@<box>

(the port is --port, 8799 by default), and open http://localhost:8799/ on the Mac:

  1. The page has one section per project and one prefilled form per App. Click Create (once
     per App); GitHub sends the browser back here with a one-time code.
  2. This helper trades the code for the App's ID and private key and writes them straight into
     place — the key is never printed, never in a URL, a page or a log.
  3. The page then links the App's install screen, pointed at the organization: choose "Only
     select repositories", pick the project's repo, click Install. The helper sees the
     installation by itself and writes its ID. It exits once every App of every project is in.

Each <project> is a projects/<name>.env name (riffmap, nulltrader); the helper reads PROJECT_REPO
from that file, names the Apps and the key folder after the repo's own name (the folder
bin/spawn.sh looks under: `${PROJECT_REPO##*/}`, so NullTrader, not nulltrader), and shows that
repo on the install step. A name with no env file is used as it is, with --repos for the repo
(one project only). A project whose Apps already exist is listed as done and its Apps checked:
an App installed with fewer permissions than a tick needs (the shape before card #261) is called
out with the two links that fix it, because App permissions cannot be changed through the API.

  box (root)   /etc/watcher/apps/<repo name>/{author,reviewer}.pem and config   (install.sh sets owners)
  Mac          ~/.config/roloc-apps/<repo name>/                                (author only: no process
               boundary on a Mac, so no reviewer App there)

The owner's own Mac sessions get one App of their own, not a project's (card #288): `app-setup.py
desk --all-repos`, author only, under ~/.config/roloc-apps/desk/. It writes to every project's
repo and board, so --all-repos makes the install step say "All repositories" and calls out an
installation that covers selected repos only (GitHub lets the owner change that under the
organization's installations, not this helper). deploy/mac-setup.sh then points the Mac's Claude
sessions at it, so a comment from one of them reads roloc-desk-author[bot], the lane's reads
roloc-<repo name>-author[bot], and roloc is the owner's own hand alone.

App names are <prefix>-<repo name>-<role>, the prefix `roloc` unless --prefix says otherwise (a fork
names its own; bin/receipt-check.sh's RECEIPT_REVIEWER_APPS and bin/gh-app-token.sh's GH_APP_PREFIX match it). The author gets Contents, Pull requests, Workflows,
Issues, Actions and Projects (organization) write, Checks and Commit statuses read — what a tick
needs to push, open and merge a PR, read its checks and rerun a failed job. The reviewer gets
Pull requests write and Contents read: it reads the PR's head and files one review, nothing
else. GH_APP_API and GH_APP_WEB point the helper somewhere else (bin/test-app-setup.py).
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
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("WATCHER_ROOT") or os.path.dirname(HERE)

PERMISSIONS = {
    "author": {"contents": "write", "pull_requests": "write", "workflows": "write", "issues": "write",
               "organization_projects": "write", "actions": "write", "checks": "read", "statuses": "read"},
    "reviewer": {"pull_requests": "write", "contents": "read"},
}


# What GitHub's App settings page calls each permission and level, for anything a person reads.
UI_NAMES = {"actions": "Actions", "checks": "Checks", "statuses": "Commit statuses", "contents": "Contents",
            "pull_requests": "Pull requests", "issues": "Issues", "workflows": "Workflows",
            "organization_projects": "Projects (organization)", "metadata": "Metadata"}
UI_LEVELS = {"read": "Read-only", "write": "Read and write", "admin": "Admin"}


def ui_permission(key, level):
    return f"{UI_NAMES.get(key, key)}: {UI_LEVELS.get(level, level)}"


PREFIX = "roloc"


def app_name(key, role):
    return f"{PREFIX}-{key}-{role}"


def build_manifest(key, role, org, port):
    return {
        "name": app_name(key, role),
        "url": f"{WEB}/{org}",
        "hook_attributes": {"url": "https://example.invalid/unused", "active": False},
        "redirect_url": f"http://localhost:{port}/created?project={urllib.parse.quote(key)}&role={role}",
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


def project_repo(name, root=ROOT):
    """PROJECT_REPO from projects/<name>.env, or None when there is no such file or line."""
    path = os.path.join(root, "projects", f"{name}.env")
    try:
        for line in open(path):
            k, sep, v = line.strip().partition("=")
            if sep and k.strip() == "PROJECT_REPO":
                return v.strip().strip('"').strip("'") or None
    except FileNotFoundError:
        return None
    return None


class Project:
    def __init__(self, name, repos, apps_dir, all_repos=False):
        self.name = name
        self.repos = repos
        self.all_repos = all_repos      # the desk App (card #288): installed on every repo of the organization
        # The folder bin/spawn.sh looks under is the repo's own name (`${PROJECT_REPO##*/}`).
        self.key = repos[0].rsplit("/", 1)[-1] if repos else name
        self.dir = os.path.join(apps_dir, self.key)
        self.config_path = os.path.join(self.dir, "config")

    def config(self):
        return read_config(self.config_path)

    def has(self, role, field):
        return bool(self.config().get(f"{role.upper()}_{field}"))


class Setup:
    def __init__(self, org, projects, roles, port):
        self.org, self.projects, self.roles, self.port = org, projects, roles, port
        self.state = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.done = threading.Event()
        self._org_id = None
        for p in projects:
            os.makedirs(p.dir, exist_ok=True)

    def project(self, key):
        return next((p for p in self.projects if p.key == key), None)

    def finished(self):
        return all(p.has(r, "INSTALLATION_ID") for p in self.projects for r in self.roles)

    def org_id(self):
        """The organization's numeric id, so the install link lands on it instead of asking where."""
        if self._org_id is None:
            try:
                self._org_id = http_json(f"{API}/orgs/{urllib.parse.quote(self.org)}").get("id") or 0
            except (urllib.error.URLError, ValueError, KeyError):
                self._org_id = 0
        return self._org_id

    def install_url(self, slug):
        base = f"{WEB}/apps/{slug}/installations/new"
        oid = self.org_id()
        return f"{base}/permissions?target_id={oid}" if oid else base

    # -- the steps
    def convert(self, p, role, code):
        data = http_json(f"{API}/app-manifests/{urllib.parse.quote(code)}/conversions", "POST")
        pem = data.pop("pem")
        write_key(os.path.join(p.dir, f"{role}.pem"), pem, "watcher-reviewer" if role == "reviewer" else "watcher")
        values = p.config()
        pre = role.upper()
        values.update({f"{pre}_APP_ID": str(data["id"]), f"{pre}_SLUG": data["slug"]})
        try:
            bot = http_json(f"{API}/users/{urllib.parse.quote(data['slug'] + '[bot]')}")
            values[f"{pre}_BOT_ID"] = str(bot["id"])
        except (urllib.error.URLError, KeyError, ValueError):
            pass
        write_config(p.config_path, values)
        return data["slug"]

    def find_installation(self, p, role):
        """The installation on the organization, once the owner has clicked Install."""
        cfg = p.config()
        pre = role.upper()
        if cfg.get(f"{pre}_INSTALLATION_ID"):
            return True
        if not cfg.get(f"{pre}_APP_ID"):
            return False
        jwt = jwt_for(cfg[f"{pre}_APP_ID"], os.path.join(p.dir, f"{role}.pem"))
        found = http_json(f"{API}/app/installations", headers={"Authorization": f"Bearer {jwt}"})
        for inst in found:
            if (inst.get("account") or {}).get("login", "").lower() == self.org.lower():
                cfg[f"{pre}_INSTALLATION_ID"] = str(inst["id"])
                write_config(p.config_path, cfg)
                return True
        return False

    def installation(self, p, role):
        """What GitHub reports for this App's installation (its permissions, which repos it
        reaches). None when the App is not installed or GitHub did not answer."""
        cfg = p.config()
        pre = role.upper()
        if not cfg.get(f"{pre}_INSTALLATION_ID") or not cfg.get(f"{pre}_APP_ID"):
            return None
        try:
            jwt = jwt_for(cfg[f"{pre}_APP_ID"], os.path.join(p.dir, f"{role}.pem"))
            return http_json(f"{API}/app/installations/{cfg[f'{pre}_INSTALLATION_ID']}",
                             headers={"Authorization": f"Bearer {jwt}"})
        except (urllib.error.URLError, ValueError, subprocess.CalledProcessError, OSError):
            return None

    def missing_permissions(self, p, role, inst=None):
        """Permissions a tick needs that this installed App does not have (an App made before
        card #261, or one trimmed by hand). None when the App is not installed or GitHub did
        not answer; [] when it has every one. `inst` is the installation when the caller
        already fetched it."""
        if inst is None:
            inst = self.installation(p, role)
        if inst is None:
            return None
        have = inst.get("permissions") or {}
        rank = {"read": 1, "write": 2, "admin": 3}
        return [k for k, v in PERMISSIONS[role].items() if rank.get(have.get(k), 0) < rank[v]]

    def wrong_scope(self, p, role, inst=None):
        """True when this App must reach every repo of the organization (--all-repos, the desk
        App, card #288) and its installation covers selected ones only. False otherwise, and
        when the App is not installed or GitHub did not answer."""
        if not p.all_repos:
            return False
        if inst is None:
            inst = self.installation(p, role)
        return bool(inst) and inst.get("repository_selection") != "all"

    # -- the page
    def row(self, p, role):
        if not p.has(role, "APP_ID"):
            manifest = html.escape(json.dumps(build_manifest(p.key, role, self.org, self.port)), quote=True)
            target = f"{WEB}/organizations/{urllib.parse.quote(self.org)}/settings/apps/new?state={self.state}"
            return (f'<li><form action="{target}" method="post"><input type="hidden" name="manifest" value="{manifest}">'
                    f'<button>Create {html.escape(app_name(p.key, role))}</button></form></li>')
        slug = p.config().get(f"{role.upper()}_SLUG", app_name(p.key, role))
        if not p.has(role, "INSTALLATION_ID"):
            if p.all_repos:
                pick = 'choose <i>All repositories</i>, click Install'
            else:
                where = html.escape(", ".join(p.repos) or "the project's repos")
                pick = f'choose <i>Only select repositories</i>, pick {where}, click Install'
            return (f'<li><b>{html.escape(slug)}</b> created. '
                    f'<a href="{html.escape(self.install_url(slug))}" target="_blank">Install it</a>: {pick}. '
                    f'<span class="st" data-project="{html.escape(p.key, quote=True)}" data-role="{role}">waiting for the install…</span></li>')
        inst = self.installation(p, role)
        if self.wrong_scope(p, role, inst):
            fix = f"{WEB}/organizations/{urllib.parse.quote(self.org)}/settings/installations"
            return (f'<li><b>{html.escape(slug)}</b> installed on <b>selected repositories only</b>; it must reach every repo. '
                    f'<a href="{fix}" target="_blank">Configure it</a>: Repository access → All repositories, Save.</li>')
        missing = self.missing_permissions(p, role, inst)
        if missing:
            fix = f"{WEB}/organizations/{urllib.parse.quote(self.org)}/settings/apps/{html.escape(slug)}/permissions"
            accept = f"{WEB}/organizations/{urllib.parse.quote(self.org)}/settings/installations"
            want = "; ".join(ui_permission(k, PERMISSIONS[role][k]) for k in missing)
            return (f'<li><b>{html.escape(slug)}</b> installed, <b>missing permissions</b>: {html.escape(want)}. '
                    f'<a href="{fix}" target="_blank">Add them</a> under Repository permissions and save, then '
                    f'<a href="{accept}" target="_blank">accept them</a> on the installation (Configure).</li>')
        return f'<li><b>{html.escape(slug)}</b> installed ✓</li>'

    def page(self):
        sections = []
        for p in self.projects:
            rows = "".join(self.row(p, role) for role in self.roles)
            repos = html.escape(", ".join(p.repos)) if p.repos else "repo not known here (no projects/%s.env)" % html.escape(p.name)
            sections.append(f"<h2>{html.escape(p.key)}</h2><p>{repos}</p><ul>{rows}</ul>")
        script = ("<script>setInterval(()=>document.querySelectorAll('.st:not([data-done])').forEach(e=>"
                  "fetch('/status?project='+encodeURIComponent(e.dataset.project)+'&role='+e.dataset.role)"
                  ".then(r=>r.json()).then(j=>{if(j.installed){e.textContent='installed ✓';e.dataset.done='1';"
                  "if(j.scope)e.textContent+=' — reload this page: install it on all repositories';"
                  "else if(j.missing&&j.missing.length)e.textContent+=' — reload this page: permissions to add'}})),2500)</script>")
        title = ", ".join(p.key for p in self.projects)
        return (f"<!doctype html><meta charset=utf-8><title>Apps for {html.escape(title)}</title>"
                f"<h1>GitHub Apps for {html.escape(self.org)}</h1>{''.join(sections)}{script}")


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
            p = setup.project((q.get("project") or [""])[0])
            if p is None or role not in setup.roles:
                return self.send(404, "no such project or role")
            if u.path == "/created":
                if (q.get("state") or [""])[0] != setup.state or not q.get("code"):
                    return self.send(400, "the state did not match this run; start again from the main page")
                with setup.lock:
                    try:
                        setup.convert(p, role, q["code"][0])
                    except (urllib.error.URLError, KeyError, ValueError, OSError) as e:
                        return self.send(502, f"GitHub would not hand over the App: {html.escape(str(e))}")
                return self.send(302, "", location="/")
            if u.path == "/status":
                with setup.lock:
                    try:
                        ok = setup.find_installation(p, role)
                    except (urllib.error.URLError, ValueError, subprocess.CalledProcessError, OSError):
                        ok = False
                    inst = setup.installation(p, role) if ok else None
                    missing = setup.missing_permissions(p, role, inst) if ok else None
                    scope = setup.wrong_scope(p, role, inst) if ok else False
                    if setup.finished():
                        setup.done.set()
                return self.send(200, json.dumps({"installed": ok, "missing": missing, "scope": scope}), "application/json")
            return self.send(404, "not found")
    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create each project's GitHub Apps and file their keys.")
    ap.add_argument("projects", nargs="+", metavar="project", help="projects/<name>.env names; all on one page")
    ap.add_argument("--org", default="lonestarprod")
    ap.add_argument("--repos", default="", help="comma-separated owner/name list for a project with no env file (one project only)")
    ap.add_argument("--roles", help="default: author,reviewer as root on the box; author alone elsewhere")
    ap.add_argument("--apps-dir", help="default: /etc/watcher/apps as root, else ~/.config/roloc-apps; each project under it by repo name")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--prefix", default="roloc", help="the Apps are named <prefix>-<repo name>-<role>")
    ap.add_argument("--all-repos", action="store_true",
                    help="install on every repository of the organization (the Mac's desk App, card #288): the install step "
                         "says All repositories, and an installation on selected repos is called out")
    a = ap.parse_args(argv)
    global PREFIX
    PREFIX = a.prefix
    root = os.geteuid() == 0
    roles = (a.roles or ("author,reviewer" if root else "author")).split(",")
    if any(r not in PERMISSIONS for r in roles):
        sys.exit("app-setup: roles are author and reviewer")
    repos_flag = [r for r in a.repos.split(",") if r]
    if repos_flag and len(a.projects) > 1:
        sys.exit("app-setup: --repos names one project's repo; with several projects each comes from its projects/<name>.env")
    apps_dir = a.apps_dir or ("/etc/watcher/apps" if root else os.path.expanduser("~/.config/roloc-apps"))
    projects = []
    for name in a.projects:
        repo = project_repo(name)
        env_path = os.path.join(ROOT, "projects", f"{name}.env")
        if repo is None and os.path.exists(env_path) and not repos_flag:
            sys.exit(f"app-setup: projects/{name}.env has no PROJECT_REPO, so it is a lane file (it sources its project's); "
                     f"name the project itself — its Apps serve every lane of that project")
        repos = repos_flag or ([repo] if repo else [])
        if not repos and not a.all_repos:
            print(f"app-setup: no projects/{name}.env with PROJECT_REPO under {ROOT}; the install step will not name the repo"
                  + ("" if len(a.projects) > 1 else " (pass --repos owner/name)"), file=sys.stderr)
        projects.append(Project(name, repos, apps_dir, all_repos=a.all_repos))
    setup = Setup(a.org, projects, roles, a.port)
    already = [p.key for p in projects if all(p.has(r, "INSTALLATION_ID") for r in roles)]
    if already:
        print(f"app-setup: already set up, listed for a permissions check only: {', '.join(already)}", flush=True)
    if setup.finished():
        report(setup)
        print("app-setup: nothing to create", flush=True)
        return
    server = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler(setup))
    print(f"app-setup: open http://localhost:{a.port}/ in your browser (writing under {apps_dir})", flush=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        while not setup.done.wait(1):
            pass
    except KeyboardInterrupt:
        sys.exit("app-setup: stopped before every App was installed")
    server.shutdown()
    report(setup)
    print(f"app-setup: done. Check with: GH_APP_DIR={projects[0].dir} bin/gh-app-token.sh {projects[0].key} author --identity")


def report(setup):
    for p in setup.projects:
        cfg = p.config()
        for r in setup.roles:
            R = r.upper()
            line = f"app-setup: {p.key} {r}: App {cfg.get(R + '_APP_ID')}, installation {cfg.get(R + '_INSTALLATION_ID')}"
            inst = setup.installation(p, r)
            if setup.wrong_scope(p, r, inst):
                line += f" — installed on SELECTED repositories only; it must reach every repo (Configure at {WEB}/organizations/{setup.org}/settings/installations: All repositories)"
            missing = setup.missing_permissions(p, r, inst)
            if missing:
                line += " — MISSING permissions: " + "; ".join(ui_permission(k, PERMISSIONS[r][k]) for k in missing) \
                        + f" (add at {WEB}/organizations/{setup.org}/settings/apps/{cfg.get(R + '_SLUG')}/permissions, then accept at {WEB}/organizations/{setup.org}/settings/installations)"
            print(line)


if __name__ == "__main__":
    main()
