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

print(f"{count} cases, {'all passed' if not fails else 'FAILURES ABOVE'}")
sys.exit(fails)
