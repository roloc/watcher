#!/usr/bin/env python3
"""Offline tests for bin/board.py's Needs-Steve comment reading (`needs_steve_ask` and
`needs_steve_images`, card #91 — the mockups a floor-page desk shows inline) and its board
cache (card #98, cases 13+). No network: `subprocess.run` is replaced with a stub that
returns canned `gh issue view --json comments` output, and `board.gh` (the GraphQL
wrapper, never touched by the comment-reading cases above) is replaced with a stub that
counts calls and returns canned `items()`/`project()` shapes.

    python3 bin/test-board.py      # exit 0 = all cases hold
"""
import json
import os
import sys
import tempfile
import time

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
    # board.py only ever calls `gh issue view <n> --json comments` here. An entry in
    # COMMENTS is either a plain body string (a synthetic `createdAt` is made up, for the
    # cases that only care about `needs_steve_ask`/`needs_steve_images`) or a
    # `{"when", "body"}` dict (the real-fixture round tests below, which sort on it).
    n = int(cmd[3])
    items = COMMENTS.get(n)
    if items is None:
        return FakeResult("", returncode=1)
    comments = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            comments.append({"body": item["body"], "createdAt": item["when"]})
        else:
            comments.append({"body": item, "createdAt": f"2026-01-01T00:00:{i:02d}Z"})
    return FakeResult(json.dumps({"comments": comments}))


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

# --- real fixtures: the exact comment bodies riffmap-site #3, #17, #18 and #19 carried as
# of October 9, 2026 (card #96's "Why nothing shows today"). Saved here rather than fetched,
# so this suite stays offline; `RIFFMAP_SITE_3_ROUNDS` is in the order `gh` returns comments
# (oldest first) — `needs_steve_rounds` does its own sort, so this only has to match reality.

RIFFMAP_SITE_FIXTURES = {
    17: {"when": '2026-10-09T07:55:40Z', "body": '🤖 **Needs Steve: pick a direction for the integrations page — and two smaller calls**\n\n- Card: the integrations page\'s first brief and mockup — keep your notetaker, add the\n  map, as one picture.\n- Done so far: `docs/design/integrations.md` and three full mockups merged to main; the\n  map crop is the existing brainstorm-offsite board, never a real meeting. 12 screenshots\n  (plus 28 unaffected ones), every one read.\n\n**Look:**\n- Direction A — the flow, with the stops as the encoding (recommended): https://riffmap-site.lonestarprod.com/preview/main/_design/mockups/integrations-a.html\n- Direction B — six shapes, one reader: https://riffmap-site.lonestarprod.com/preview/main/_design/mockups/integrations-b.html\n- Direction C — outside the call: https://riffmap-site.lonestarprod.com/preview/main/_design/mockups/integrations-c.html\n- The PR: https://github.com/roloc/riffmap-site/pull/25\n\n**Question: which direction should `builder` build?**\n- **A. The flow, with the stops as the encoding** (recommended) — doors grouped by how\n  the transcript travels, one line to one address, the map at the end. The site map\'s\n  "one picture", drawn as a picture.\n- **B. Six shapes, one reader** — the hero is the transcript itself, in every file shape\n  Riffmap reads, switchable. Answers "will it read my export?" directly; the flow picture\n  comes second.\n- **C. Outside the call** — the call as a ring with Riffmap outside it, one arrow out and\n  none back in. Leads with trust ("is this another bot on my calls?") over how-to.\n\nTwo smaller calls on the same card, if you want something different — otherwise I\'ll go\nwith the recommended answer on both:\n- **Google Meet\'s wording:** riffmap merged the Meet transcript reader the day after the\n  site map called it "not yet" (built, but only tested against a researched mock of the\n  real export, not a real one yet). Recommended: say so on the page ("download, then\n  send", with that caveat) rather than the now-stale "not yet".\n- **The account-page picture:** the site map optionally wanted a render of the real\n  product\'s provider-connection screen. Recommended: skip it — this repo can\'t render\n  that live screen, and the page\'s one required picture (the flow) already carries the\n  page.\n\nReply A, B or C (and a lean on the other two if you want to change them), then move it\nback to **In progress**.\n\n<details><summary>Details</summary>\n\n- Branch / PR: `card-17` (merged, squashed) → https://github.com/roloc/riffmap-site/pull/25\n- Files: `docs/design/integrations.md`, `docs/design/mockups/integrations-{a,b,c}.html`\n- Fixture: none needed (site map §2.3); the one product picture reuses the existing\n  `home-board-desktop-{light,dark}.png` crop of `demo-2026-09-16-offsite` at riffmap\n  `f8a57ce`.\n- Three riffmap findings recorded in the brief, none blocking this card: `connect-your-notetaker.md`\n  is a day behind `main` on the Meet reader; its Meet Zapier recipe is now reachable and\n  unverified; `docs/site-map.md` §1.3\'s "not yet" row is superseded by the same merge.\n- CI (`verify`) and `publish` both green on the merge commit; no `reviewer` pass\n  (docs-only).\n\n</details>\n\n'},
    18: {"when": '2026-10-09T08:18:30Z', "body": '🤖 **Needs Steve: pick a direction for invitation**\n\n- The brief and three mockups are merged ([PR #26](https://github.com/roloc/riffmap-site/pull/26)); the brief is [`docs/design/invitation.md`](https://github.com/roloc/riffmap-site/blob/main/docs/design/invitation.md). Every product picture is a real render of the portal\'s own `/request-access` page (riffmap `main` at `8f32b5c`) — the three-step track before and after a request. The route is a link to that page; nothing on the site is a form.\n- One thing the real page showed that the site may not: the second field\'s example text names a real person from a real call, so no picture on the site reaches that field (the brief\'s §8 names it as a riffmap fix).\n- Three small questions in the brief\'s §9: the direction, whether the page names you ("Steve reads it") or says "the instance owner", and whether the button says **Request access** (the portal\'s own words, recommended) or **Ask for an invitation**.\n\n**Look:**\n\n- **A (recommended) — The three steps are the page.** The real track as the hero, and the page shaped like it: your request · the owner\'s invite (with the real *after you send* picture beside who reads it) · you sign in.\n  [mockup](https://riffmap-site.lonestarprod.com/_design/mockups/invitation-a.html) · [phone light](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-a-phone-light.png) · [phone dark](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-a-phone-dark.png) · [desktop light](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-a-desktop-light.png) · [desktop dark](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-a-desktop-dark.png)\n- **B — The door, as it is.** The top of the portal\'s own request page is the hero and the link: the next page, shown before you go.\n  [mockup](https://riffmap-site.lonestarprod.com/_design/mockups/invitation-b.html) · [phone light](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-b-phone-light.png) · [phone dark](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-b-phone-dark.png) · [desktop light](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-b-desktop-light.png) · [desktop dark](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-b-desktop-dark.png)\n- **C — One card.** The site\'s page in the portal\'s shape: one card, three rows, one button. The control, and the cheapest.\n  [mockup](https://riffmap-site.lonestarprod.com/_design/mockups/invitation-c.html) · [phone light](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-c-phone-light.png) · [phone dark](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-c-phone-dark.png) · [desktop light](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-c-desktop-light.png) · [desktop dark](https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-c-desktop-dark.png)\n\nReply with a letter (or a lean) for Q1, and A/B for Q2 and Q3 if you have a view — the recommendation stands if you don\'t — then move the card back to **In progress**.\n\n'},
    19: {"when": '2026-10-09T08:49:37Z', "body": '🤖 **Needs Steve: pick a direction for the demo maps index**\n\n- Card: brief and mockup for the `/demo/` index, one row per demo map, linking the real rendered page.\n- Done so far: the brief and three mockup directions, merged (docs-only, PR #27).\n- Blocker: none — this is the pick, same as the other first-mockup cards.\n\n**Above the fold: the index turned out to be eleven maps, not two.** The card expected the\ntwo fixtures on file in September; riffmap has since landed ten more meetings at the same\ninvented company, so the page became six weeks of one team\'s calls instead of two\nunrelated demos.\n\n**Look:**\n- Direction A (recommended): https://riffmap-site.lonestarprod.com/preview/main/_design/mockups/demo-maps-a.html\n- Direction B: https://riffmap-site.lonestarprod.com/preview/main/_design/mockups/demo-maps-b.html\n- Direction C: https://riffmap-site.lonestarprod.com/preview/main/_design/mockups/demo-maps-c.html\n- Screenshots: https://riffmap-site.lonestarprod.com/preview/main/_shots/mockups/\n- The PR: https://github.com/roloc/riffmap-site/pull/27\n\n**Question 1: which direction?**\n- **A. Six weeks on a calendar** (recommended) — each call sits on the day it happened, the bar its own length, four topics you can tap to see which calls they ran through.\n- **B. Seven hours end to end** — all eleven calls as one bar, then a plain list below. Simplest, no moving parts.\n- **C. Grouped by kind of meeting** — brainstorms, sales calls, the interview, status meetings, each with a sample picture.\n\n**Question 2: the old Acme sales demo — on the page or not?**\n- **A. Leave it off** (recommended) — it\'s a different invented company, and its own name is "Riffmap selling Riffmap," which reads oddly next to the real set.\n- **B. Add it as a twelfth row**, kept separate from the Lumen Robotics set.\n\nReply with a letter for each question (or say which way you lean), then move it back to **In progress**.\n\n<details><summary>Details</summary>\n\n- Branch / PR: card-19, https://github.com/roloc/riffmap-site/pull/27 (merged, squashed, Refs #19)\n- Brief: docs/design/demo-maps.md on main\n- Files: docs/design/demo-maps.md, docs/design/mockups/demo-maps-{a,b,c}.html, docs/design/mockups/demo-maps-tl-*.png, demo-maps-clock-*.png\n- Shots: npm run shots -- --mockups, 64 shots, 0 problems\n- Riffmap findings not acted on here (filed for riffmap, not this card): a stale topic-river count on one fixture, the word "live" in one fixture\'s own meeting name, the disk-file render\'s old wordmark header, a small duration/axis mismatch on the panel interview, and a real company domain inside the Acme fixture\'s file (not rendered on any page). All in docs/design/demo-maps.md §8.\n\n</details>\n'},
}

RIFFMAP_SITE_3_ROUNDS = [
    {"when": '2026-10-08T20:57:37Z', "body": '🤖 **Needs Steve: approve the home page mockup**\n\n- Card: the home page\'s first brief and mockup — the hero is a real render, not a drawing.\n- Done so far: `docs/design/home.md` (the brief) and `docs/design/mockups/home.html` merged to main; four screenshots, both themes and both widths, all read clean.\n\n**Look:**\n- The mockup: https://riffmap-site.lonestarprod.com/_design/mockups/home.html\n- Shots — desktop light: https://riffmap-site.lonestarprod.com/_shots/mockups/home-desktop-light.png\n- Shots — desktop dark: https://riffmap-site.lonestarprod.com/_shots/mockups/home-desktop-dark.png\n- Shots — phone light: https://riffmap-site.lonestarprod.com/_shots/mockups/home-phone-light.png\n- Shots — phone dark: https://riffmap-site.lonestarprod.com/_shots/mockups/home-phone-dark.png\n- The PR: https://github.com/roloc/riffmap-site/pull/10\n\n**Question: does this mockup say and show the right thing?**\n- **A. Yes, build it** (recommended) — the hero is the real Elsa render (mode strip, board, verbatim stickies), the promise line and buyer copy trace to the positioning brief, the two CTAs are "See a real map" and "Ask for an invitation."\n- **B. Close, but change something** — say what, on this card.\n- **C. Wrong direction** — say why, on this card.\n\nTwo word-choice calls are in the brief for you to weigh in on too, if you want something different:\n- The word "notetaker" survives in two spots (the headline promise and the objection quote) even though positioning\'s own vocabulary rule (§5) bans it elsewhere on the site.\n- "Ask for an invitation" is a `mailto:` to your own address, already public as the repo\'s commit author.\n\nReply A, B or C on this card, then move it back to **In progress**.\n\n<details><summary>Details</summary>\n\n- Branch / PR: `card-3` (merged, squashed) → https://github.com/roloc/riffmap-site/pull/10\n- Files: `docs/design/home.md`, `docs/design/mockups/home.html`, `docs/design/mockups/home-hero-{desktop,phone}-{light,dark}.png`\n- The hero images are Playwright screenshots of riffmap\'s own renderer (`scripts/render.mjs`) running the committed `fixtures/elsa-2026-07-31.map.json`, clipped to the header, stat tiles, topic timeline and first board — not a mock of the UI.\n- CI (`verify`) and `publish` both green on the merge commit; no `reviewer` pass (docs-only).\n\n</details>\n'},
    {"when": '2026-10-08T23:41:55Z', "body": '🤖 **Needs Steve: riffmap#647 is closed, but the demo fixtures it was supposed to produce are not on riffmap\'s `main` yet — still blocked, no action needed from you**\n\n- Card: this card\'s own gate is "the home brief does not start until riffmap#647\'s brainstorm demo fixture is committed."\n- Checked: riffmap#647 is CLOSED, but its closing comment says "None of the fixtures exist on `main` yet" — it only decided the approach and spawned six build cards ([riffmap#650](https://github.com/roloc/riffmap/issues/650)–[655](https://github.com/roloc/riffmap/issues/655)).\n- Checked again just now: all six of those are still OPEN, and `fixtures/demo/` does not exist on riffmap\'s `main`.\n- This isn\'t a question — just recording that the gate is still shut so this doesn\'t get reopened on the strength of "#647 is done." Move this card to **In progress** again once those six cards (or at least the flagship brainstorm one, #650) land.\n\nNo reply needed — parking this back as blocked, not stuck on a question.\n'},
    {"when": '2026-10-09T07:16:54Z', "body": '🤖 **Needs Steve: pick a direction for the home page — and two smaller calls**\n\n- Card: the home page\'s first brief and mockup, rethought after your reply — no real\n  meetings, demo data at its best, a page that sells.\n- Done so far: `docs/design/home.md` (the brief) and three full mockups merged to main;\n  each one built from a real render of the new brainstorm demo (an invented offsite,\n  "Fleet Intelligence roadmap"), never Elsa. 28 screenshots, every one read.\n\n**Look:**\n- Direction A — the strip is the headline: https://riffmap-site.lonestarprod.com/_design/mockups/home-a.html\n- Direction B — the page is the meeting, scroll through it (recommended): https://riffmap-site.lonestarprod.com/_design/mockups/home-b.html\n- Direction C — promise, then the proof, the familiar shape: https://riffmap-site.lonestarprod.com/_design/mockups/home-c.html\n- The PR: https://github.com/roloc/riffmap-site/pull/22\n\n**Question: which direction should `builder` build?**\n- **B. Fifty-five minutes, top to bottom** (recommended) — the page scrolls through the\n  actual call in time order, with a playhead; the only one that *shows* Riffmap telling\n  the kinds of talk apart instead of just claiming it. Longest page.\n- **A. The strip is the headline** — the product\'s own timeline as the hero image, three\n  proof pictures below it. Shorter, scans fast; the pick if B feels too long on a phone.\n- **C. Promise, then the proof** — the conventional landing-page shape, same real renders.\n  The safe control.\n\nTwo smaller calls on the same card, if you want something different — otherwise I\'ll go\nwith the recommended answer on both:\n- The **Ask for an invitation** button: black/white (`--ink` on `--page`, recommended —\n  the brand rule keeps the brainstorm orange off text) or the brainstorm orange itself?\n- How loud should "this is a demo" be: once under the first picture and once in the\n  footer (recommended), a "demo" label on every picture, or footer only?\n\nReply A, B or C (and a lean on the other two if you want to change them), then move it\nback to **In progress**.\n\n<details><summary>Details</summary>\n\n- Branch / PR: `card-3` (merged, squashed) → https://github.com/roloc/riffmap-site/pull/22\n- Files: `docs/design/home.md`, `docs/design/mockups/home-{a,b,c}.html`, 56 PNG crops\n  under `docs/design/mockups/`\n- Hero fixture: `fixtures/demo/demo-2026-09-16-offsite.map.json` on riffmap `main` at\n  `f8a57ce` — checked against `docs/site-map.md` §5\'s bar for the home hero: passes on\n  six of seven axes (ideas, lanes, decisions, lineage depth, outcomes, no sales/interview\n  section), runs long and brainstorm-heavy on the other two (55 min vs. 35–50 asked for,\n  51% brainstorm share vs. 25–35%) — the brief says so and the copy says what the picture\n  shows rather than oversells it.\n- Sales thumbnail: `demo-2026-09-22-okapi-demo` (the only new fixture with reactions).\n  Interview thumbnail: `panel-interview-2026-10-05` (the new series has no interview of\n  its own).\n- Three riffmap findings recorded in the brief, none blocking this card: the owner\'s\n  quote-panel editor leaks into a visitor-facing render; the phone graph and the phone\n  Sankey both lose content at 390px.\n- CI (`verify`) and `publish` both green on the merge commit; no `reviewer` pass\n  (docs-only).\n\n</details>\n'},
]

print("8. riffmap-site#18 (real fixture): three directions, shot links on a wrapped\n"
      "   continuation line, lettered and ordered A, B, C, the recommended one flagged")
COMMENTS[18] = [RIFFMAP_SITE_FIXTURES[18]]
rounds18 = board.needs_steve_rounds(18)
check("exactly one round", len(rounds18) == 1, rounds18)
imgs18 = rounds18[0]["images"] if rounds18 else []
check("twelve images — four shots per direction, the .html mockup link excluded",
      len(imgs18) == 12, imgs18)
check("run A's, then B's, then C's",
      [d["letter"] for d in imgs18] == list("AAAABBBBCCCC"), [d["letter"] for d in imgs18])
check("only A is flagged recommended",
      [d["recommended"] for d in imgs18] == [True] * 4 + [False] * 8, imgs18)
check("each image's own label is the link text, not the whole bullet",
      [d["label"] for d in imgs18[:4]]
      == ["phone light", "phone dark", "desktop light", "desktop dark"], imgs18[:4])
check("direction A's phone-light shot is the right URL",
      imgs18[0]["url"] == "https://riffmap-site.lonestarprod.com/_shots/mockups/invitation-a-phone-light.png",
      imgs18[0])

print("9. riffmap-site#3 (real fixture): three rounds — a pick with pictures, a status\n"
      "   note with none, and a newer pick whose directions are .html links only")
COMMENTS[3] = RIFFMAP_SITE_3_ROUNDS
rounds3 = board.needs_steve_rounds(3)
check("three rounds, newest first", [r["when"] for r in rounds3]
      == ["2026-10-09T07:16:54Z", "2026-10-08T23:41:55Z", "2026-10-08T20:57:37Z"],
      [r["when"] for r in rounds3])
check("the newest round (pick a direction, home-{a,b,c}.html) has no pictures at all",
      rounds3[0]["images"] == [], rounds3[0])
check("the blocked status note has no pictures either",
      rounds3[1]["images"] == [], rounds3[1])
check("the October 8 mockup-approval round has its four shots, unlettered (one mockup, "
      "not a lettered set)", [d["url"] for d in rounds3[2]["images"]] == [
      "https://riffmap-site.lonestarprod.com/_shots/mockups/home-desktop-light.png",
      "https://riffmap-site.lonestarprod.com/_shots/mockups/home-desktop-dark.png",
      "https://riffmap-site.lonestarprod.com/_shots/mockups/home-phone-light.png",
      "https://riffmap-site.lonestarprod.com/_shots/mockups/home-phone-dark.png"],
      rounds3[2]["images"])
check("...and none of them carry a direction letter",
      all(d["letter"] is None for d in rounds3[2]["images"]), rounds3[2]["images"])

print("10. riffmap-site#17 and #19 (real fixtures): lettered directions whose own links are\n"
      "    .html mockups or a directory listing — no PNG anywhere, so no pictures, ever")
COMMENTS[17] = [RIFFMAP_SITE_FIXTURES[17]]
COMMENTS[19] = [RIFFMAP_SITE_FIXTURES[19]]
check("riffmap-site#17: zero images", board.needs_steve_images(17) == [])
check("riffmap-site#19: zero images", board.needs_steve_images(19) == [])

print("11. riffmap-site#19 (real fixture): the old regex anchored on the literal word\n"
      "    \"Question:\" never matched \"**Question 1:**\", so the Look section ran past it\n"
      "    clean into Question 2's options, the reply line and the <details> fold")
section19 = board._look_section(RIFFMAP_SITE_FIXTURES[19]["body"])
check("stops before the first Question heading", "Question" not in section19, section19)
check("never reaches the second one either", "Acme" not in section19, section19)
check("never reaches the <details> fold", "<details>" not in section19, section19)
check("the five real bullets are all still there",
      section19.count("- Direction") == 3 and "Screenshots:" in section19
      and "The PR:" in section19, section19)

print("12. a direction read off a mockup's own filename when the bullet's words don't say")
COMMENTS[20] = ["🤖 **Needs Steve: pick one**\n\n**Look:**\n"
                "- Screenshot: https://example.com/shots/idea-b-desktop.png\n\n"
                "**Question: which?**"]
imgs20 = board._parse_look_images(board._latest_needs_steve_comment(20))
check("letter B, read off \"-b-\" in the URL, not the bullet's own words",
      imgs20 == [{"url": "https://example.com/shots/idea-b-desktop.png", "label": "Screenshot",
                  "letter": "B", "recommended": False}], imgs20)

print("13. the board cache (card #98): a file per board, so two reads inside the TTL "
      "share one real gh() call")
# These cases stub `subprocess.run`, not `board.gh` itself, so the real `gh()` runs for
# real on every call — cache reads/writes, the rateLimit side effect (case 18) and the
# `-f`/`-F` argument shape all included — and only the process boundary is faked.

CACHE_DIR = tempfile.mkdtemp(prefix="watcher-cache-test-")
os.environ["WATCHER_CACHE_DIR"] = CACHE_DIR
os.environ["BOARD_CACHE_SEC"] = "45"
board._cache_dir_cached = None
board._cache_dir_warned = False

GRAPHQL_CALLS = []
PROJECT_STATUS_OPTIONS = [{"id": "O1", "name": "In progress"},
                           {"id": "O2", "name": "Stuck - Needs help"}]
PROJECT_STAGE_OPTIONS = [{"id": "O3", "name": "Claimed"}]


def _graphql_kv(cmd):
    """`cmd` is `["gh", "api", "graphql", "-f", "query=...", "-f", "owner=...", ...]` —
    board.py's own `gh()` building it. Reconstructs the keyword arguments it was called
    with, `query` included, the same way `gh`'s own caller passed them."""
    kv = {}
    i = 3
    while i < len(cmd):
        k, _, v = cmd[i + 1].partition("=")
        kv[k] = v
        i += 2
    return kv


def graphql_stub_run(cmd, **kwargs):
    if cmd[:3] != ["gh", "api", "graphql"]:
        return stub_run(cmd, **kwargs)
    kv = _graphql_kv(cmd)
    GRAPHQL_CALLS.append(kv["query"])
    if "fields(first:40)" in kv["query"]:
        payload = {"user": {"projectV2": {"id": "PVT_1", "title": "t", "fields": {"nodes": [
            {"name": "Status", "id": "F_STATUS", "options": PROJECT_STATUS_OPTIONS},
            {"name": "Stage", "id": "F_STAGE", "options": PROJECT_STAGE_OPTIONS},
        ]}}}}
    else:
        n = int(kv.get("number", "0"))
        payload = {"rateLimit": {"remaining": 4312, "resetAt": "2026-10-09T20:00:00Z", "limit": 5000, "cost": 1},
                   "user": {"projectV2": {"items": {
                       "pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [{"id": "ITEM_1",
                                  "fieldValues": {"nodes": [{"name": "In progress",
                                                              "field": {"name": "Status"}}]},
                                  "content": {"number": n, "title": "t", "url": "https://x",
                                              "state": "OPEN",
                                              "labels": {"nodes": [{"name": "in-flight"}]}}}]}}}}
    return FakeResult(json.dumps({"data": payload}))


board.subprocess.run = graphql_stub_run
board.OWNER, board.NUMBER = "roloc", 98

GRAPHQL_CALLS.clear()
first = board.items()
second = board.items()
check("two items() calls inside the TTL make exactly one real gh() call",
      len(GRAPHQL_CALLS) == 1, GRAPHQL_CALLS)
check("both calls return the same data", first == second)

GRAPHQL_CALLS.clear()
board.items(fresh=True)
check("items(fresh=True) always makes a live call, cache or not", len(GRAPHQL_CALLS) == 1, GRAPHQL_CALLS)

print("14. project(): cached the same way, and a field/option miss invalidates it and "
      "retries once live instead of waiting out the 10-minute TTL")
GRAPHQL_CALLS.clear()
board.project()
board.project()
check("two project() calls inside the TTL make exactly one real gh() call",
      len(GRAPHQL_CALLS) == 1, GRAPHQL_CALLS)

board.OWNER, board.NUMBER = "roloc", 99  # a fresh cache namespace for the miss-retry case
renamed_column_calls = []


def renamed_column_stub_run(cmd, **kwargs):
    if cmd[:3] != ["gh", "api", "graphql"]:
        return stub_run(cmd, **kwargs)
    kv = _graphql_kv(cmd)
    renamed_column_calls.append(1)
    # The first project() fetch has no Stuck column yet; the second (the live retry a miss
    # triggers) does — standing in for Steve renaming a column mid-TTL.
    options = [{"id": "O1", "name": "In progress"}]
    if len(renamed_column_calls) > 1:
        options.append({"id": "O2", "name": "Stuck - Needs help"})
    payload = {"user": {"projectV2": {"id": "PVT_99", "title": "t", "fields": {"nodes": [
        {"name": "Status", "id": "F_STATUS", "options": options},
        {"name": "Stage", "id": "F_STAGE", "options": PROJECT_STAGE_OPTIONS},
    ]}}}}
    return FakeResult(json.dumps({"data": payload}))


board.subprocess.run = renamed_column_stub_run
result = board.resolve_status("Stuck - Needs help")
check("resolve_status finds the option only visible after the live retry",
      result == "Stuck - Needs help", result)
check("exactly two project() fetches: the stale cache, then one live retry",
      len(renamed_column_calls) == 2, renamed_column_calls)
board.subprocess.run = graphql_stub_run

print("15. mutations invalidate the items() cache once their own mutation lands, never "
      "before (card #98, reviewer round 1): move, stage and unclaim all make the very "
      "next items() call live again, and set_option's own lookup during the mutation "
      "still uses the cache that was warm when the call started, never a forced refetch")
board.OWNER, board.NUMBER = "roloc", 100
COMMENTS[100] = []  # cmd_unclaim's `gh issue edit` (stub_run) needs a successful exit

board.items()  # warm the items cache
board.project()  # warm the project cache too, so cmd_stage's own project() lookup is also a
                 # hit — isolating what this case is actually about: set_option's ITEMS
                 # lookup, not a cold project() fetch that would happen either way
GRAPHQL_CALLS.clear()
board.cmd_stage("100", "Claimed")
check("cmd_stage: set_option's own items() AND project() lookups both reused the cache "
      "that was warm when the call started (invalidating AHEAD of the mutation would be "
      "the bug this guards — a concurrent reader could then re-warm the cache with a "
      "pre-mutation snapshot that outlives the TTL) — only the mutation call itself was "
      "real", len(GRAPHQL_CALLS) == 1, GRAPHQL_CALLS)
GRAPHQL_CALLS.clear()
board.items()
check("items() right after cmd_stage is a live call: the cache was invalidated only once "
      "the mutation had already landed", len(GRAPHQL_CALLS) == 1, GRAPHQL_CALLS)

board.items()  # warm it again before the next mutation
GRAPHQL_CALLS.clear()
board.cmd_unclaim("100")
board.items()
check("items() right after cmd_unclaim is a live call too, for the same reason",
      len(GRAPHQL_CALLS) == 1, GRAPHQL_CALLS)

print("15b. card #98, reviewer round 1 — the race this guards against, reproduced: a "
      "concurrent reader landing INSIDE the mutation's own network call re-warms the "
      "cache with a pre-mutation snapshot, and the fix must still leave the cache "
      "invalidated once the mutation returns, not stale for the rest of the TTL")
board.OWNER, board.NUMBER = "roloc", 106
COMMENTS[106] = []


def racing_stub_run(cmd, **kwargs):
    if cmd[:3] == ["gh", "issue", "edit"]:
        # Stands in for another lane's poll landing exactly inside this `gh issue edit`
        # call: it reads (and re-caches) the board before this mutation has had a chance
        # to invalidate anything.
        board.items()
    return graphql_stub_run(cmd, **kwargs)


board.items()  # warm it
board.subprocess.run = racing_stub_run
board.cmd_unclaim("106")
board.subprocess.run = graphql_stub_run
GRAPHQL_CALLS.clear()
board.items()
check("even with a reader racing inside the mutation's own call, the cache is live again "
      "right after — invalidating ONLY after the mutation (never before) means this "
      "function's own invalidate is always the LAST write, so the race's re-warm can "
      "never be the one left standing", len(GRAPHQL_CALLS) == 1, GRAPHQL_CALLS)

print("16. BOARD_CACHE_SEC=0 disables the cache entirely — reads and writes both — "
      "which is what the offline suites set")
board.OWNER, board.NUMBER = "roloc", 101
os.environ["BOARD_CACHE_SEC"] = "0"
GRAPHQL_CALLS.clear()
board.items()
board.items()
check("two calls with caching disabled make two real gh() calls",
      len(GRAPHQL_CALLS) == 2, GRAPHQL_CALLS)
check("and nothing was written to disk for this board",
      not os.path.exists(board._cache_path("items-roloc-101.json") or ""))
os.environ["BOARD_CACHE_SEC"] = "45"

print("17. items() TTL: a cache file older than BOARD_CACHE_SEC forces a live re-fetch")
board.OWNER, board.NUMBER = "roloc", 102
os.environ["BOARD_CACHE_SEC"] = "1"
board._cache_dir_cached = None
GRAPHQL_CALLS.clear()
board.items()
path = board._cache_path(board._items_cache_name())
old = time.time() - 5
os.utime(path, (old, old))
board.items()
check("a cache file older than the TTL is treated as a miss, not served stale",
      len(GRAPHQL_CALLS) == 2, GRAPHQL_CALLS)
os.environ["BOARD_CACHE_SEC"] = "45"

print("18. ratelimit: cmd_ratelimit reads the last cached value, never a call of its own")
board.OWNER, board.NUMBER = "roloc", 103
board._cache_dir_cached = None
GRAPHQL_CALLS.clear()
board.items()  # a real query that happens to carry rateLimit
cached_rl = board._cache_read_any_age(board.RATELIMIT_CACHE_NAME)
check("the rate limit from that call is on file",
      cached_rl == {"remaining": 4312, "resetAt": "2026-10-09T20:00:00Z", "limit": 5000, "cost": 1}, cached_rl)
calls_before = len(GRAPHQL_CALLS)
board._cache_read_any_age(board.RATELIMIT_CACHE_NAME)
check("reading it again makes no gh() call at all", len(GRAPHQL_CALLS) == calls_before)

print("19. with no writable cache directory, every call falls back to live and nothing "
      "written anywhere raises")
board.OWNER, board.NUMBER = "roloc", 104
# A plain file, not a missing directory: `os.makedirs` refuses to create a directory
# through a path component that is a regular file for ANY user, root included — unlike a
# merely-absent path under `/`, which a root-run test (some CI containers) could actually
# create, silently turning this into a no-op.
blocker = tempfile.NamedTemporaryFile(delete=False)
blocker.close()
os.environ["WATCHER_CACHE_DIR"] = os.path.join(blocker.name, "sub")
board._cache_dir_cached = None
board._cache_dir_warned = False
GRAPHQL_CALLS.clear()
a104 = board.items()
b104 = board.items()
check("no writable cache dir: every items() call is live",
      len(GRAPHQL_CALLS) == 2, GRAPHQL_CALLS)
check("...and the data is still correct", a104 == b104)
os.remove(blocker.name)

# Case 19 pointed WATCHER_CACHE_DIR at a now-deleted blocker path and never restored it —
# harmless while it was the last case, not harmless now that more follow.
os.environ["WATCHER_CACHE_DIR"] = CACHE_DIR
board._cache_dir_cached = None
board._cache_dir_warned = False

print("20. BOARD_CACHE_SEC's new default (card #114): the old 45s was SHORTER than the "
      "lanes' own 60-second poll, so a lane's own next poll was ALWAYS a miss on its own "
      "last read — only two different lanes landing within 45 seconds of each other ever "
      "actually shared a read. The new default comfortably outlives one poll interval, so "
      "two reads 60 seconds apart now share one real gh() call. Fails on main (45s).")
board.OWNER, board.NUMBER = "roloc", 110
os.environ.pop("BOARD_CACHE_SEC", None)  # the real default, not a test override
GRAPHQL_CALLS.clear()
board.items()
path110 = board._cache_path(board._items_cache_name())
sixty_ago = time.time() - 60
os.utime(path110, (sixty_ago, sixty_ago))
board.items()
check("two items() calls 60 seconds apart make exactly one real gh() call",
      len(GRAPHQL_CALLS) == 1, GRAPHQL_CALLS)
os.environ["BOARD_CACHE_SEC"] = "45"

print("21. a cache miss takes a lock around its own refill (card #114), so two processes "
      "racing the same miss make one real gh() call between them, not two — the October 9 "
      "measurement found exactly this: the same board read by two processes within a "
      "second of each other, twice in two minutes. Reproduced here with two threads "
      "racing a cold cache; fails on main (no lock: both call gh() independently).")
import threading  # noqa: E402  (only this one case needs it)

board.OWNER, board.NUMBER = "roloc", 111
refill_started = threading.Event()
release_refill = threading.Event()


def slow_graphql_stub_run(cmd, **kwargs):
    if cmd[:3] == ["gh", "api", "graphql"]:
        kv = _graphql_kv(cmd)
        if "items(first:100" in kv["query"] and kv.get("number") == "111":
            refill_started.set()
            release_refill.wait(2)
    return graphql_stub_run(cmd, **kwargs)


board.subprocess.run = slow_graphql_stub_run
GRAPHQL_CALLS.clear()
lock_results = []
t1 = threading.Thread(target=lambda: lock_results.append(board.items()))
t2 = threading.Thread(target=lambda: lock_results.append(board.items()))
t1.start()
refill_started.wait(2)   # t1 is inside its own live call now, holding the lock
t2.start()
time.sleep(0.2)          # best-effort: give t2 a real chance to reach the lock and block
release_refill.set()
t1.join(2)
t2.join(2)
board.subprocess.run = graphql_stub_run
check("two concurrent misses make exactly one real gh() call",
      len(GRAPHQL_CALLS) == 1, GRAPHQL_CALLS)
check("...and both threads got the same, correct data",
      len(lock_results) == 2 and lock_results[0] == lock_results[1], lock_results)

print("22. the ledger (card #114): a real gh() call appends one line with its caller, "
      "query kind, cost and remaining; `ledger` sums the last hour by caller, never a "
      "call of its own")
board.OWNER, board.NUMBER = "roloc", 112
os.environ["WATCHER_LEDGER_CALLER"] = "lane:test"
board.items(fresh=True)
ledger_path = board._cache_path(board.LEDGER_NAME)
with open(ledger_path) as f:
    last_line = json.loads(f.readlines()[-1])
check("the ledger's newest line names this call's caller",
      last_line["caller"] == "lane:test", last_line)
check("...and the kind of query", last_line["query"] == "items", last_line)
check("...and the cost GitHub returned with it", last_line["cost"] == 1, last_line)
summary = board._ledger_hour_summary()
check("the hour's summary attributes at least this call's cost to that caller",
      summary["by_caller"].get("lane:test", 0) >= 1, summary)
del os.environ["WATCHER_LEDGER_CALLER"]

print("23. a gh() failure that is a rate limit serves the stale items cache instead of "
      "failing the call outright, with no cache at all it still exits 1 the same as any "
      "other gh failure (card #114, the October 9 outage: 'API rate limit exceeded' "
      "turned into every lane sleeping five minutes with nothing cached to fall back on). "
      "items(fresh=True) — cmd_show's own call — is the one exception: its whole contract "
      "is to fail loud, never serve a stale answer, so it still exits even with cache on file")
board.OWNER, board.NUMBER = "roloc", 113
board.items()  # warm the cache
warm = board._cache_read_any_age(board._items_cache_name())
# Age the cache file past its TTL (not `_cache_invalidate`, which would delete the very
# data the fallback is supposed to serve) so the next items() call treats it as a miss.
_old_items_path = board._cache_path(board._items_cache_name())
_long_ago = time.time() - 600
os.utime(_old_items_path, (_long_ago, _long_ago))


class RateLimitedResult(FakeResult):
    def __init__(self):
        super().__init__("", returncode=1)
        self.stderr = "gh: API rate limit exceeded for user ID 1 (HTTP 403)"


def rate_limited_stub_run(cmd, **kwargs):
    if cmd[:3] == ["gh", "api", "graphql"]:
        kv = _graphql_kv(cmd)
        if "items(first:100" in kv["query"]:
            return RateLimitedResult()
    return graphql_stub_run(cmd, **kwargs)


board.subprocess.run = rate_limited_stub_run
served = board.items()
check("rate-limited with a stale cache on file: served that cache instead of exiting",
      served == warm, served)
try:
    # A direct call sees the raw RateLimitError; through the CLI dispatch (__main__,
    # below) it becomes a plain sys.exit(1) the same as any other gh failure — this is
    # testing that items(fresh=True) does NOT swallow it into a stale answer, not the
    # CLI's own conversion.
    board.items(fresh=True)
    died = False
except board.RateLimitError:
    died = True
check("items(fresh=True) still exits even with that same stale cache on file: show's "
      "contract is freshness or a loud failure, never a silent stale answer", died)
board.OWNER, board.NUMBER = "roloc", 114  # a cold board: nothing cached at all
try:
    board.items()
    died2 = False
except SystemExit:
    died2 = True
check("rate-limited with nothing cached at all: still exits, same as any other gh failure", died2)

board.OWNER, board.NUMBER = "roloc", 113  # back to the board with a stale-but-present cache
too_old = time.time() - (board.STALE_FALLBACK_MAX_SEC + 60)
os.utime(_old_items_path, (too_old, too_old))
try:
    board.items()
    died3 = False
except SystemExit:
    died3 = True
check("rate-limited with a cache older than STALE_FALLBACK_MAX_SEC: still exits, not served "
      "as current (reviewer: an unbounded stale serve is its own kind of wrong board state)", died3)
board.subprocess.run = graphql_stub_run

print("24. the closed-card Stage sweep is board-level, not per-lane (card #114): two "
      "sweeps inside STAGE_SWEEP_SEC do one real items() read between them, whichever "
      "lane calls second finding the stamp fresh and touching nothing")
board.OWNER, board.NUMBER = "roloc", 115
os.environ["STAGE_SWEEP_SEC"] = "300"
board._cache_invalidate(board._sweep_stamp_name())
GRAPHQL_CALLS.clear()
board.cmd_sweep_stages()
calls_after_first_sweep = len(GRAPHQL_CALLS)
board.cmd_sweep_stages()
check("the first sweep in the interval reads the board",
      calls_after_first_sweep >= 1, calls_after_first_sweep)
check("a second sweep inside the interval makes no additional call at all",
      len(GRAPHQL_CALLS) == calls_after_first_sweep, GRAPHQL_CALLS)
del os.environ["STAGE_SWEEP_SEC"]

board.subprocess.run = stub_run  # restore for anything appended after this suite
print(f"{count} cases, {'all passed' if not fails else 'FAILURES ABOVE'}")
sys.exit(fails)
