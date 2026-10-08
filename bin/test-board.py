#!/usr/bin/env python3
"""Offline tests for bin/board.py's Needs-Steve comment reading: `needs_steve_ask` and
`needs_steve_images` (card #91 — the mockups a floor-page desk shows inline). No network:
`subprocess.run` is replaced with a stub that returns canned `gh issue view --json
comments` output.

    python3 bin/test-board.py      # exit 0 = all cases hold
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import board  # noqa: E402

fails = 0
count = 0


def check(name, cond, detail=""):
    global fails, count
    count += 1
    print(f"  {name:<78} {'ok' if cond else 'FAIL ' + str(detail)}")
    if not cond:
        fails = 1


class FakeResult:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


COMMENTS = {}  # issue number -> list of comment bodies, newest last


def stub_run(cmd, **kwargs):
    # board.py only ever calls `gh issue view <n> --json comments` here.
    n = int(cmd[3])
    bodies = COMMENTS.get(n)
    if bodies is None:
        return FakeResult("", returncode=1)
    return FakeResult(json.dumps({"comments": [{"body": b} for b in bodies]}))


board.subprocess.run = stub_run

STUCK_TEMPLATE = """🤖 **Needs Steve: pick a direction**

- Card: approve the mockup.
- Done so far: built both options.
- Blocker: needs your taste.

**Look:**
- The trimmed call view mockup: https://private-user-images.githubusercontent.com/1/a.png?jwt=x
- A live page, not a picture: https://example.com/preview
- The PR: https://github.com/roloc/riffmap/pull/99

**Question: which one?**
- **A. Option one** (recommended) — why
- **B. Option two** — why not

Reply A or B on this card, then move it back to In progress.

<details><summary>Details</summary>
- An image buried in the fold, never counted: https://example.com/hidden.png
</details>
"""

print("1. needs_steve_images: only the direct image link under Look:, nothing else")
COMMENTS[1] = [STUCK_TEMPLATE]
images = board.needs_steve_images(1)
check("exactly the one mockup link, not the live page, the PR, or the folded image",
      images == ["https://private-user-images.githubusercontent.com/1/a.png?jwt=x"], images)
check("needs_steve_ask still reads the same comment's first line",
      board.needs_steve_ask(1) == "**Needs Steve: pick a direction**", board.needs_steve_ask(1))

print("2. no Needs Steve comment at all: both empty, never a crash")
COMMENTS[2] = ["just a regular comment", "🤖 FYI: routine update, nothing needed"]
check("needs_steve_images: []", board.needs_steve_images(2) == [])
check("needs_steve_ask: ''", board.needs_steve_ask(2) == "")

print("3. no comments / gh failure: both empty")
COMMENTS[3] = []
check("needs_steve_images: [] on an empty comment list", board.needs_steve_images(3) == [])
check("needs_steve_images: [] when gh itself fails (issue not in COMMENTS)",
      board.needs_steve_images(999) == [])

print("4. the NEWEST Needs Steve comment wins, not an earlier one Steve already answered")
COMMENTS[4] = [
    "🤖 **Needs Steve: first ask**\n\n**Look:**\n- old mockup: https://example.com/old.png\n\n**Question: a?**",
    "A, go with option A.",
    "🤖 **Needs Steve: second ask**\n\n**Look:**\n- new mockup: https://example.com/new.png\n\n**Question: b?**",
]
check("picks the later ask's image, not the earlier one",
      board.needs_steve_images(4) == ["https://example.com/new.png"], board.needs_steve_images(4))
check("picks the later ask's first line",
      board.needs_steve_ask(4) == "**Needs Steve: second ask**", board.needs_steve_ask(4))

print("5. a comment with no Look: section at all: images empty, ask still reads")
COMMENTS[5] = ["🤖 **Needs Steve: a plain judgment call, nothing to look at**\n\n"
               "**Question: a or b?**\n- **A. a**\n- **B. b**"]
check("needs_steve_images: []", board.needs_steve_images(5) == [])
check("needs_steve_ask still works", "plain judgment call" in board.needs_steve_ask(5))

print("6. several mockups: kept in order, capped at MAX_LOOK_IMAGES")
many = "\n".join(f"- shot {i}: https://example.com/s{i}.png" for i in range(board.MAX_LOOK_IMAGES + 2))
COMMENTS[6] = [f"🤖 **Needs Steve: pick one**\n\n**Look:**\n{many}\n\n**Question: which?**"]
images6 = board.needs_steve_images(6)
check(f"capped at MAX_LOOK_IMAGES ({board.MAX_LOOK_IMAGES})", len(images6) == board.MAX_LOOK_IMAGES, images6)
check("kept in the order they were written",
      images6 == [f"https://example.com/s{i}.png" for i in range(board.MAX_LOOK_IMAGES)], images6)

print("7. the same mockup linked twice is never shown twice")
COMMENTS[7] = ["🤖 **Needs Steve: pick one**\n\n**Look:**\n"
               "- the mockup: https://example.com/same.png\n"
               "- the same mockup again: https://example.com/same.png\n\n**Question: ok?**"]
check("one entry, not two", board.needs_steve_images(7) == ["https://example.com/same.png"],
      board.needs_steve_images(7))

print(f"{count} cases, {'all passed' if not fails else 'FAILURES ABOVE'}")
sys.exit(fails)
