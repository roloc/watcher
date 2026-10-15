#!/usr/bin/env python3
"""
Decodes and follows one Claude Code session transcript — the part of `bin/tail.sh` that
needs real JSON parsing. Invoked by `tail.sh`, never by hand: see that file's header for
the interface a human actually types. `bin/floor.py` imports this module directly for the
floor page's "Peek" panel, reusing `replay_lines`/`read_new_lines` (and the path-resolving
helpers below) rather than re-implementing the decode.

`--path <n>` is the other caller: `bin/spawn.sh` runs `python3 tail.py --path <n>
--workdir-mode <mode> --worktree-base <base>` to resolve a dead tick's own transcript
path before copying it beside the empty result log (card #143) — prints the path and
exits, no `--state` or polling involved.

A transcript line is one of several `type`s (`assistant`, `user`, `attachment`,
`queue-operation`, `last-prompt`, ...); only `assistant` (tool calls, and what the model
said) and the first `user` turn (the slash command that started the tick) say anything
about what a tick is DOING, so everything else is dropped rather than printed raw.
"""
import argparse
import json
import os
import sys
import time

REPLAY_LINES = 20

# Tried in order for a short label on a tool call this script has no special case for —
# whatever a future tool's input shape turns out to be, one of these usually names the
# thing it's acting on.
GENERIC_KEYS = ("description", "command", "file_path", "path", "prompt", "query", "skill", "url", "pattern")


def encode_dir(path):
    # The Claude CLI keys a session's directory on the literal cwd string it was launched
    # with, every "/" turned into "-" (observed on this box, not a documented contract —
    # see tail.sh's header, "Risk"). This is the one place to fix if that ever changes.
    return path.replace("/", "-")


def card_dir(n, workdir_mode, worktree_base):
    if workdir_mode == "root":
        return worktree_base
    return os.path.join(worktree_base, "card-%s" % n)


# The canary lane's own Claude config dir (card #46): bin/spawn.sh exports
# CLAUDE_CONFIG_DIR to this (or to WATCHER_CANARY_CLAUDE_CONFIG_DIR) before it re-execs
# into /opt/watcher-canary, so that lane's transcripts land under <this>/projects/, never
# under ~/.claude/projects. Must stay the same literal spawn.sh uses.
DEFAULT_CANARY_CONFIG_DIR = "/home/watcher/.claude-canary"


def projects_dirs(env=None):
    """Every directory a lane's session transcript can be under, the default one first:
    `~/.claude/projects` (`CLAUDE_PROJECTS_DIR` overrides it), then the canary config
    dir's `projects/` (card #164) — derived the way spawn.sh derives the config dir,
    honoring `WATCHER_CANARY_CLAUDE_CONFIG_DIR`; `CLAUDE_CANARY_PROJECTS_DIR` names it
    outright, for a test. Looked in regardless of which release the lane says it is on:
    a transcript is wherever the CLI wrote it, and the newest one for the card wins."""
    env = os.environ if env is None else env
    main = env.get("CLAUDE_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")
    canary = env.get("CLAUDE_CANARY_PROJECTS_DIR") or os.path.join(
        env.get("WATCHER_CANARY_CLAUDE_CONFIG_DIR") or DEFAULT_CANARY_CONFIG_DIR, "projects")
    return [main] if canary == main else [main, canary]


def session_dirs(projects_dirs, n, workdir_mode, worktree_base):
    """The card's session directory under each projects dir, in the same order."""
    enc = encode_dir(card_dir(n, workdir_mode, worktree_base))
    return [os.path.join(p, enc) for p in projects_dirs]


def _as_dirs(session_dir):
    # Every reader here takes one directory (a string) or several (a list) — the floor
    # page's own tests pass one, the box passes both (`projects_dirs`).
    return [session_dir] if isinstance(session_dir, str) else list(session_dir)


def newest_jsonl(session_dir):
    """The newest `.jsonl` under `session_dir` — one directory, or a list of them, in which
    case the newest across all of them (a lane that moved from the stable release to the
    canary has an older session for the same card under the other dir)."""
    paths = []
    for d in _as_dirs(session_dir):
        try:
            paths += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
        except OSError:
            continue
    if not paths:
        return None
    # A file can vanish between listdir and stat (the CLI's own cleanupPeriodDays) — drop
    # it from contention rather than crashing on a live lane.
    mtimes = {}
    for p in paths:
        try:
            mtimes[p] = os.path.getmtime(p)
        except OSError:
            continue
    return max(mtimes, key=mtimes.get) if mtimes else None


def read_current(state_dir):
    try:
        with open(os.path.join(state_dir, "current")) as f:
            n = f.read().strip()
    except OSError:
        return None
    return n or None


def describe(entry):
    """One short line for a transcript entry, or None to drop it."""
    t = entry.get("type")

    if t == "assistant":
        for c in entry.get("message", {}).get("content") or []:
            ctype = c.get("type")
            if ctype == "tool_use":
                return describe_tool_use(c.get("name") or "?", c.get("input") or {})
            if ctype == "text":
                text = (c.get("text") or "").strip()
                if text:
                    return text.splitlines()[0][:200]
        return None

    if t == "user":
        content = entry.get("message", {}).get("content")
        if isinstance(content, str) and content.strip():
            return "> " + content.strip().splitlines()[0][:200]
        return None

    return None


def describe_tool_use(name, inp):
    if name == "Bash":
        label = inp.get("description") or inp.get("command") or ""
        return ("$ " + label.splitlines()[0])[:160] if label else "$"
    for k in GENERIC_KEYS:
        v = inp.get(k)
        if v:
            return "%s: %s" % (name, str(v)[:160])
    if inp:
        return "%s: %s" % (name, json.dumps(inp)[:160])
    return name


def decode_line(raw):
    raw = raw.strip()
    if not raw:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return describe(entry)


def replay_lines(path, n=REPLAY_LINES):
    """The last `n` decoded lines of `path`, and the byte position (end of file) a
    follow-up `read_new_lines` call should start from. Pure — no printing — so a caller
    other than this CLI (`bin/floor.py`'s Peek) can reuse the same decode exactly."""
    lines = []
    with open(path) as fh:
        for raw in fh:
            d = decode_line(raw)
            if d:
                lines.append(d)
    return lines[-n:], os.path.getsize(path)


def read_new_lines(path, pos):
    """Every complete line's decode appended since `pos`, and the new position. A
    trailing line with no newline yet is left for the next call — the tick is still
    writing it."""
    lines = []
    new_pos = pos
    with open(path) as fh:
        fh.seek(pos)
        while True:
            line = fh.readline()
            if not line or not line.endswith("\n"):
                break
            new_pos = fh.tell()
            d = decode_line(line)
            if d:
                lines.append(d)
    return lines, new_pos


def replay(path, n=REPLAY_LINES):
    lines, pos = replay_lines(path, n)
    for d in lines:
        print(d, flush=True)
    return pos


def drain(path, pos):
    lines, new_pos = read_new_lines(path, pos)
    for d in lines:
        print(d, flush=True)
    return new_pos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state")
    ap.add_argument("--path", metavar="N",
                     help="print the newest session transcript path for card N and exit "
                          "(used by spawn.sh to find a dead tick's own transcript)")
    ap.add_argument("--workdir-mode", required=True, choices=("card", "root"))
    ap.add_argument("--worktree-base", required=True)
    ap.add_argument("--no-follow", action="store_true")
    ap.add_argument("--poll-sec", type=float, default=2.0)
    args = ap.parse_args()
    if args.path is None and not args.state:
        ap.error("--state is required unless --path is given")

    dirs = projects_dirs()

    if args.path is not None:
        session_dir = session_dirs(dirs, args.path, args.workdir_mode, args.worktree_base)
        f = newest_jsonl(session_dir)
        if f is None:
            print("no session transcript found under %s" % " or ".join(session_dir), file=sys.stderr)
            return 1
        print(f)
        return 0

    last_card = None
    last_file = None
    pos = 0
    # Dedupes "lane idle" / "waiting for a session" across polls in follow mode — without
    # it, either prints once per poll_sec forever for as long as the lane sits idle.
    last_status = None

    while True:
        n = read_current(args.state)

        if n is None:
            if last_status != "idle":
                if last_card is not None:
                    print("-- #%s idle --" % last_card, flush=True)
                else:
                    print("lane idle", flush=True)
                last_status = "idle"
            last_card, last_file, pos = None, None, 0
            if args.no_follow:
                return 0
            time.sleep(args.poll_sec)
            continue

        if n != last_card:
            print("-- #%s --" % n, flush=True)
            last_card, last_file, pos = n, None, 0
        last_status = None

        session_dir = session_dirs(dirs, n, args.workdir_mode, args.worktree_base)
        f = newest_jsonl(session_dir)

        if f is None:
            if last_status != "waiting":
                print("waiting for a session under %s" % " or ".join(session_dir), flush=True)
                last_status = "waiting"
            if args.no_follow:
                return 0
            time.sleep(args.poll_sec)
            continue
        last_status = None

        if f != last_file:
            last_file = f
            pos = replay(f)
        else:
            pos = drain(f, pos)

        if args.no_follow:
            return 0
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    sys.exit(main())
