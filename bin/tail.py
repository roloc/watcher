#!/usr/bin/env python3
"""
Decodes and follows one Claude Code session transcript — the part of `bin/tail.sh` that
needs real JSON parsing. Invoked by `tail.sh`, never by hand: see that file's header for
the interface a human actually types. `bin/floor.py` imports this module directly for the
floor page's "Peek" panel, reusing `replay_lines`/`read_new_lines` (and the path-resolving
helpers below) rather than re-implementing the decode.

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


def newest_jsonl(session_dir):
    try:
        names = [f for f in os.listdir(session_dir) if f.endswith(".jsonl")]
    except OSError:
        return None
    if not names:
        return None
    paths = [os.path.join(session_dir, f) for f in names]
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
    ap.add_argument("--state", required=True)
    ap.add_argument("--workdir-mode", required=True, choices=("card", "root"))
    ap.add_argument("--worktree-base", required=True)
    ap.add_argument("--no-follow", action="store_true")
    ap.add_argument("--poll-sec", type=float, default=2.0)
    args = ap.parse_args()

    projects_dir = os.environ.get("CLAUDE_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")

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

        session_dir = os.path.join(projects_dir, encode_dir(card_dir(n, args.workdir_mode, args.worktree_base)))
        f = newest_jsonl(session_dir)

        if f is None:
            if last_status != "waiting":
                print("waiting for a session under %s" % session_dir, flush=True)
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
