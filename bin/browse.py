#!/usr/bin/env python3
"""A real browser, for looking at a page the way its reader does (card #63, card #71).

    python3 bin/browse.py check URL [--wait JS] [--settle SEC]   # exit 1 if the page's own code failed
    python3 bin/browse.py text  URL [--wait JS] [--settle SEC]   # the text a reader sees, after scripts ran
    python3 bin/browse.py shot  URL OUT.png [--width N] [--height N] [--wait JS] [--settle SEC]
    python3 bin/browse.py find                                   # which browser it would use; exit 1 if none

`curl` fetches what the server sent. A page that draws itself with its own script — the
floor page does — sends a shell and then fills it in, so `curl` returning 200 and a full
HTML document says the server is up and nothing about what a person saw. The floor page
shipped with a `<script>` block that did not parse (#70), every offline check green; the
tick sent to find out why "nothing loads" fetched it five times with `curl`, saw 200 every
time, and asked Steve what he had seen (#71). `check` on the same URL says it in one line:

    page error: Uncaught SyntaxError: Invalid or unexpected token

So: `check` loads the URL in headless Chrome, waits, and prints every uncaught exception,
`console.error` and failed request — exit 1 if there was one. `text` prints the rendered
text. `shot` writes a PNG you can `Read`. All three print the page's errors on stderr.

No dependencies beyond the standard library and a Chrome or Chromium binary: it speaks the
DevTools protocol over a pipe (`--remote-debugging-pipe`), so there is no node, no
Playwright package and no port. `find_browser` looks in, in order: `$BROWSE_BROWSER`, the
PATH, the usual macOS app paths, and Playwright's own download cache (which is where the
agents box and the ci box have one). None found is an error that says how to get one —
never a silent skip, which is how a check stops existing without anyone deciding it should.

Also a module: `bin/test-floor-browser.py` drives the floor page with `Browser` below.

This is the browser **every project on the box gets**: a tick in any worktree runs it as
`python3 "$WATCHER_HOME/bin/browse.py" …` (`commands/watch.md` says when), against a live
page, a server it started, or a `file://` path. A project does not install anything.
"""
import base64
import fcntl
import glob
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time

PATH_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
              "chrome-headless-shell", "chrome")
MAC_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
# Playwright's cache, newest revision first. The folder names inside a revision changed
# between releases (chrome-linux, chrome-linux64, chrome-headless-shell-linux64, ...), so
# these are patterns, not paths.
PLAYWRIGHT_CACHES = ("~/.cache/ms-playwright", "~/Library/Caches/ms-playwright")
PLAYWRIGHT_BINARIES = (
    "chromium_headless_shell-*/*/chrome-headless-shell",
    "chromium_headless_shell-*/*/headless_shell",
    "chromium-*/*/chrome",
    "chromium-*/*/Chromium.app/Contents/MacOS/Chromium",
    "chromium-*/*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
)

NO_BROWSER = """browse.py: no Chrome or Chromium found.
  Looked at $BROWSE_BROWSER, the PATH, /Applications, and Playwright's cache.
  Get one with:   apt install chromium
             or:   npx --yes playwright install chromium-headless-shell
  or point at one: BROWSE_BROWSER=/path/to/chrome"""


class BrowseError(Exception):
    pass


def _revision(path):
    """The Playwright revision in a cache path (chromium-1243 -> 1243), for newest-first."""
    for part in path.split(os.sep):
        head, _, rev = part.rpartition("-")
        if head.startswith("chromium") and rev.isdigit():
            return int(rev)
    return 0


def find_browser(env=None, which=shutil.which, exists=os.path.exists, globber=glob.glob):
    """The browser binary to run, or None. The seams are for `test-floor-browser.py`."""
    env = os.environ if env is None else env
    explicit = env.get("BROWSE_BROWSER")
    if explicit:
        return explicit if exists(explicit) else None
    for name in PATH_NAMES:
        hit = which(name)
        if hit:
            return hit
    for app in MAC_APPS:
        if exists(app):
            return app
    found = []
    for cache in PLAYWRIGHT_CACHES:
        for pattern in PLAYWRIGHT_BINARIES:
            found.extend(globber(os.path.join(os.path.expanduser(cache), pattern)))
    found = [p for p in found if os.access(p, os.X_OK)]
    return max(found, key=_revision) if found else None


class Browser:
    """One headless Chrome and one tab. Use as a context manager.

        with Browser() as b:
            b.goto("http://127.0.0.1:8787/")
            b.wait_for("document.querySelectorAll('.desk').length > 0")
            b.click(".peek-btn")
            print(b.errors)

    `errors` collects, for the life of the tab: uncaught exceptions (a script that does not
    parse is one), `console.error` calls, and error-level log entries (a failed request).
    `new_tabs` collects the URL of every other tab the page opened.
    """

    def __init__(self, width=1280, height=900, binary=None, timeout=20):
        self.binary = binary or find_browser()
        if not self.binary:
            raise BrowseError(NO_BROWSER)
        self.width, self.height, self.timeout = width, height, timeout
        self.errors = []
        self.new_tabs = []
        self._id = 0
        self._buf = b""
        self._loaded = False
        self._proc = None

    # --- lifecycle -------------------------------------------------------------------

    def __enter__(self):
        self._profile = tempfile.mkdtemp(prefix="watcher-browse-")
        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()
        # Chrome reads the protocol on fd 3 and writes it on fd 4. Move the two ends it
        # gets well clear of 3 and 4 first, so the shell's own redirections below can never
        # overwrite one of them with the other.
        child_in = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD, 20)
        child_out = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD, 20)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        args = [
            self.binary, "--headless=new", "--remote-debugging-pipe",
            f"--user-data-dir={self._profile}", f"--window-size={self.width},{self.height}",
            # The box and the ci runner are unprivileged containers, where Chrome's own
            # sandbox cannot start. This only ever loads pages the caller named.
            "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-background-networking", "--mute-audio",
            "--hide-scrollbars", "about:blank",
        ]
        # bash, not sh: on Linux sh is dash, which reads `3<&20` as fd 2 and a stray 0.
        self._proc = subprocess.Popen(
            ["bash", "-c", f'exec "$@" 3<&{child_in} 4>&{child_out}', "bash", *args],
            pass_fds=(child_in, child_out), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(child_in)
        os.close(child_out)
        self._w, self._r = to_chrome_w, from_chrome_r
        try:
            self._send("Target.setDiscoverTargets", {"discover": True}, session=None)
            target = self._send("Target.createTarget", {"url": "about:blank"}, session=None)
            self._target = target["targetId"]
            self._session = self._send(
                "Target.attachToTarget", {"targetId": self._target, "flatten": True},
                session=None)["sessionId"]
            for domain in ("Page", "Runtime", "Log"):
                self._send(f"{domain}.enable")
            self._send("Emulation.setDeviceMetricsOverride",
                       {"width": self.width, "height": self.height,
                        "deviceScaleFactor": 1, "mobile": False})
            self.pump(0.2)
            self.new_tabs = []  # the browser's own first tabs are not ones a page opened
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        if self._proc is not None:
            for fd in (self._w, self._r):
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
                self._proc.wait()
            self._proc = None
        shutil.rmtree(self._profile, ignore_errors=True)
        return False

    # --- the protocol ----------------------------------------------------------------

    def _read_message(self, deadline):
        while b"\0" not in self._buf:
            left = deadline - time.time()
            if left <= 0:
                raise BrowseError("browse.py: the browser stopped answering")
            ready, _, _ = select.select([self._r], [], [], left)
            if not ready:
                continue
            chunk = os.read(self._r, 1 << 16)
            if not chunk:
                raise BrowseError("browse.py: the browser exited "
                                  f"(is {self.binary} a working Chrome?)")
            self._buf += chunk
        raw, _, self._buf = self._buf.partition(b"\0")
        return json.loads(raw)

    def _on_event(self, msg):
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails") or {}
            text = (d.get("exception") or {}).get("description") or d.get("text") or "error"
            self.errors.append("page error: " + text.splitlines()[0])
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            parts = [str(a.get("value", a.get("description", ""))) for a in params.get("args") or []]
            self.errors.append("console.error: " + " ".join(parts)[:300])
        elif method == "Log.entryAdded":
            e = params.get("entry") or {}
            # The browser asks for /favicon.ico by itself; a 404 there is not the page's.
            if e.get("level") == "error" and not (e.get("url") or "").endswith("/favicon.ico"):
                self.errors.append(f"{e.get('source', 'log')} error: {e.get('text', '')} "
                                   f"{e.get('url', '')}".strip()[:300])
        elif method == "Page.loadEventFired":
            self._loaded = True
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") == "page" and info.get("targetId") != getattr(self, "_target", None):
                self.new_tabs.append(info.get("url", ""))

    def _send(self, method, params=None, session="tab"):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session == "tab":
            msg["sessionId"] = self._session
        try:
            os.write(self._w, json.dumps(msg).encode() + b"\0")
        except OSError:
            raise BrowseError(f"browse.py: the browser is gone (is {self.binary} a working Chrome?)")
        deadline = time.time() + self.timeout
        while True:
            got = self._read_message(deadline)
            if got.get("id") == self._id:
                if "error" in got:
                    raise BrowseError(f"browse.py: {method}: {got['error'].get('message')}")
                return got.get("result") or {}
            self._on_event(got)

    def pump(self, seconds):
        """Let the page run for `seconds`, collecting whatever it reports meanwhile."""
        end = time.time() + seconds
        while True:
            # Everything already read first: one read can bring several messages, and the
            # ones behind the first sit in the buffer, where select() cannot see them. Left
            # there, an exception the page threw is still unread when the caller looks at
            # `errors` — a broken page reported as clean.
            while b"\0" in self._buf:
                self._on_event(self._read_message(time.time() + self.timeout))
            left = end - time.time()
            if left <= 0:
                return
            ready, _, _ = select.select([self._r], [], [], left)
            if ready:
                self._on_event(self._read_message(time.time() + self.timeout))

    # --- what a caller does ----------------------------------------------------------

    def goto(self, url):
        self._loaded = False
        result = self._send("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise BrowseError(f"browse.py: could not open {url}: {result['errorText']}")
        deadline = time.time() + self.timeout
        while not self._loaded:
            if time.time() > deadline:
                raise BrowseError(f"browse.py: {url} never finished loading")
            self.pump(0.05)

    def eval(self, expression):
        """The value of a JS expression in the page (a promise is awaited)."""
        r = self._send("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                             "returnByValue": True})
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise BrowseError("browse.py: " + ((d.get("exception") or {}).get("description")
                                               or d.get("text") or "evaluate failed"))
        return (r.get("result") or {}).get("value")

    def wait_for(self, expression, timeout=8):
        """True once the JS expression is truthy; False if it never was within `timeout`."""
        deadline = time.time() + timeout
        while True:
            try:
                if self.eval(expression):
                    return True
            except BrowseError:
                pass  # the page's script may be the very thing that is broken
            if time.time() > deadline:
                return False
            self.pump(0.1)

    def click(self, selector):
        """A real click — a mouse press and release where the element is drawn, so whatever
        is on top of it, or wrapped around it, gets the click the way it would from a hand."""
        box = self.eval(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            el.scrollIntoView({{block: "center", inline: "center"}});
            const r = el.getBoundingClientRect();
            return {{x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height}};
        }})()""")
        if not box or not box["w"] or not box["h"]:
            raise BrowseError(f"browse.py: nothing to click at {selector!r} (missing, or not drawn)")
        self.click_at(box["x"], box["y"])

    def hover(self, selector):
        """The pointer moved onto the element — a real mouse move to where it is drawn, so
        whatever the page shows on hover shows the way it would under a hand."""
        box = self.eval(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            el.scrollIntoView({{block: "center", inline: "center"}});
            const r = el.getBoundingClientRect();
            return {{x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height}};
        }})()""")
        if not box or not box["w"] or not box["h"]:
            raise BrowseError(f"browse.py: nothing to hover at {selector!r} (missing, or not drawn)")
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": box["x"], "y": box["y"]})
        self.pump(0.05)

    def click_at(self, x, y):
        """A click at a point on the screen, whatever happens to be drawn there."""
        for kind in ("mousePressed", "mouseReleased"):
            self._send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                                     "button": "left", "clickCount": 1})
        self.pump(0.05)

    def key(self, name):
        for kind in ("keyDown", "keyUp"):
            self._send("Input.dispatchKeyEvent", {"type": kind, "key": name, "code": name})
        self.pump(0.05)

    def resize(self, width, height):
        self.width, self.height = width, height
        self._send("Emulation.setDeviceMetricsOverride",
                   {"width": width, "height": height, "deviceScaleFactor": 1,
                    "mobile": width < 600})

    def reduce_motion(self, reduce=True):
        """Emulate `prefers-reduced-motion: reduce` (or clear it) — the only way to check
        a page's own reduced-motion CSS from here: there is no DOM state to assert on
        otherwise, since the browser, not the page, decides the media query."""
        value = "reduce" if reduce else "no-preference"
        self._send("Emulation.setEmulatedMedia",
                   {"features": [{"name": "prefers-reduced-motion", "value": value}]})

    def text(self):
        return self.eval("document.body ? document.body.innerText : ''") or ""

    def screenshot(self, path, full_page=True):
        """Write the page as a PNG. full_page is the page's whole height, not just the first
        screen — but never wider than the window asked for. captureBeyondViewport on its own
        also widens the picture to the page's scroll width, so a phone-width shot of a table
        that overflows came out 495px wide on a 390px window: not what a phone shows (a
        390px frame with the table cut off at the right) and not the picture the usability
        pass was told it was getting, which cost every screen card a round arguing about the
        picture's size instead of the page on it (cards #170, #172, October 15, 2026).
        Clipping to the window's width keeps the overflow visible as what it is."""
        params = {"format": "png", "captureBeyondViewport": bool(full_page)}
        if full_page:
            h = self.eval("Math.max(document.documentElement ? document.documentElement.scrollHeight : 0,"
                          " document.body ? document.body.scrollHeight : 0)")
            try:
                h = int(h)
            except (TypeError, ValueError):
                h = 0
            params["clip"] = {"x": 0, "y": 0, "width": self.width,
                              "height": max(h, self.height), "scale": 1}
        r = self._send("Page.captureScreenshot", params)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        return path


def main(argv):
    import argparse
    if argv[:1] == ["find"]:
        found = find_browser()
        print(found or NO_BROWSER, file=sys.stdout if found else sys.stderr)
        return 0 if found else 1
    ap = argparse.ArgumentParser(
        prog="browse.py", description="Look at a page in a real browser.",
        epilog="Also: `browse.py find` prints the browser it would use (exit 1 if there is none).")
    ap.add_argument("command", choices=("check", "text", "shot"))
    ap.add_argument("url")
    ap.add_argument("out", nargs="?", help="shot: the PNG to write")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--wait", metavar="JS", help="a JS expression to wait for before looking")
    ap.add_argument("--settle", type=float, default=2.0,
                    help="seconds to let the page run after it loads (default 2)")
    args = ap.parse_args(argv)
    if args.command == "shot" and not args.out:
        ap.error("shot needs the PNG to write: browse.py shot URL OUT.png")

    try:
        with Browser(width=args.width, height=args.height) as b:
            b.goto(args.url)
            if args.wait and not b.wait_for(args.wait, timeout=max(args.settle, 8)):
                b.errors.append(f"never became true: {args.wait}")
            b.pump(args.settle)
            if args.command == "text":
                print(b.text())
            elif args.command == "shot":
                print(b.screenshot(args.out))
            for e in b.errors:
                print(e, file=sys.stderr)
            if args.command == "check":
                print(f"{args.url}: " + (f"{len(b.errors)} error(s) — the page's own code or "
                                         "requests failed" if b.errors else "loaded clean"))
                return 1 if b.errors else 0
            return 0
    except BrowseError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
