# The owner's cheat sheet

**You stock Ready. The bots do the rest.** One page, no code above the fold.

**The floor page** shows what every bot is doing right now, without asking anyone:
`http://<your box>:8787/`. Each board reads left to right in the
order work flows: the **dispatcher** (triage, sorting new cards to the right bot), then
**design**, then **build**, then **ship** — the build check on `main`, and for the
watcher's own board whether the newest version is live, being tested, waiting for the bots
to be idle, or held (a red flag, with its card). Working, idle, needing you, paused, or
asleep after a failure — one look, phone-friendly, light and dark. Read-only: it never
changes a card.

**A board whose cards are waiting on you gets its own strip, pictures and all.** Any card
in Stuck shows there — not only the one a lane's desk happens to be sitting on — with the
ask above it and every picture its Needs-the-owner comment pointed at: flip through with the
arrows (or a swipe), lettered A/B/C when there were directions to choose from, the
recommended one marked. Asked more than once? It opens on the newest round; older ones are
still there, past the last picture, each dated. Nothing to approve yet — the card linked a
page instead of a picture — and you'll see its ask with no carousel under it, just a link.

**Peek** at any desk to see what that bot is actually doing, not just which card it holds.
Every desk has a **Peek** button, busy or not: on a working desk it opens that bot's
session live, one short line at a time, updating as it works; on a quiet desk it shows the
bot's last session, and says so. The **Ship** desk has one too: every step the build check
ran on the newest change, with a ✓ or a ✗ on each, the changes before it, and — on the
watcher's own board — the last things the deployer did. While a build is still running —
the normal wait before a merge — this updates by itself, step by step, with nothing to
refresh. Close it with the ×, or tap outside
it — nothing there can change a card either. If the page ever shows a red line saying its own code hit an error,
that is the page telling you it is broken: file a card.

**Studio**, top of the floor page, is the office — made for a computer's browser; a phone
pans it. Across the top: the **break room** and **your office**, a hallway between them. The
main hallway runs across the middle. Along the bottom: a room per board, hallways between,
and the **server room** at the end. Every board's room has a desk per bot (its screen lit
and scanning while it works), the counter by the door for triage, the ship station on a
shelf, and the board's counts on the whiteboard. Hands up at a desk is paused. An idle bot
walks out of its door, down the hallway and into the break room, and wanders about there with
a mug until a card comes; its empty desk says whose it is. The ship station does the same once
there is nothing for it to watch — green build, nothing being shipped — and comes back the
moment there is. A bot that fell asleep after a
failure naps on the couch. A bot that needs you walks to your office and waves in front of
your desk, holding up the picture its card is waiting on, and the light over your door turns
red the moment anyone does (amber when something is paused, asleep or blocked; green when all
is well). Every card waiting on you is in the **in-tray** on your desk, with the same picture
carousel as the floor's strip. Two boards hang on your wall, and both open when clicked: the
**bulletin board** lists every card being worked right now and every card recently worked,
and **Peek** on any of them shows the card itself — its text, labels and last comments — with
a link to open it on GitHub; the **wins board** lists everything the teams shipped this week,
by board, with the count on the board itself. Hover the **server room** for the box's load,
memory and disk, the tokens the bots moved in the last day, and (once `/etc/watcher/floor.env` on the box has a
Proxmox token — ask for it) every box in the lab with its CPU and memory. Tap a bot for what it just
said; tap that for the full Peek. **Floor** takes you back. Still read-only, still just a
look.

## The columns

- **Backlog** — yours. The bots never start anything from here. They will **put a card
  back** here if its own text says not to work it yet (see below).
- **Ready** — **the bots' queue, since September 19, 2026.** When they have room they take
  the top card here by themselves and start it. Put a card in Ready when you want it done;
  you no longer have to hand it over one at a time.
- **In progress** — work in flight. **Two cards at a time**, whoever put them there. Move a
  card here yourself to jump the line: a card you move by hand is always started before one
  the bots would have chosen.
- **In review** — the bot's PR is finished and green but the merge did not go through. Nothing for you to do: after two hours the bots try once more, and if that fails the card comes to you in **Stuck** with the question. A card you drop here by hand, with no PR, is moved to **Stuck** within about ten minutes asking where you meant to put it.
- **Done** — shipped.
- **Stuck - Needs help** — the bots need you. Read the last comment on the card.

## How much runs at once

- **Two cards in flight**, counted across all three lanes.
- When both slots are full, Ready waits — nothing is dropped, and nothing piles up.
- A card that is parked in Stuck is not in flight, so it does not hold a slot.
- **To stop the bots starting anything new**, empty Ready or pause a lane (last section).

## Cards that say "not yet"

- A card whose body says **"do not work this until …"** is obeyed. The bot comments why
  and moves it **back to Backlog**.
- **This is not a failure and not a Stuck** — you are not asked anything, and nothing
  pings you.
- **Being handed the card is not the same as lifting the gate.** Moving a batch to Ready
  is not a considered answer to a line written weeks ago, so the bots do not read it as
  one.
- To actually lift it: **edit that line out of the card**, then move it to Ready.

## Three lanes

- Design cards and build cards run **side by side**, one of each at a time.
- The label decides the lane: `agent: architect` or `agent: designer` is the design lane.
  Every other label is the build lane. Two builds never run at once.
- **No label needed.** A card with no `agent:` label is read by the triage lane within a
  minute of it reaching In progress — whether you moved it or the bots took it from Ready
  — whatever the other two are doing. It adds the
  label, says why in a comment, and the right lane takes it from there. Change the label
  if you disagree; the card moves lanes with it.
- A card in one lane never waits on the other. It never touches the other's branch either.

## Every screen change gets a second look

- Before a bot ships anything with a picture, a second check looks only at that picture —
  the way you would on your phone — and blocks if the thing the card asked for can't
  actually be done from it.
- You see it the same way as any other block: a Stuck card, the ask on the first line.

## Cards that wait on other cards

- Stock Ready with several at once; the bots work the prerequisite first.
- Say it in the card, in words: "after #291", "once the radar redesign lands", "depends
  on". The triage bot records it as the issue's **Relationships** (blocked by). You can
  also set that field yourself on the issue.
- A card with an open "blocked by" waits, whatever its priority, and gets one comment
  saying what it waits on. The minute the last one closes, it starts.
- A card that already has an agent label is not read by the triage bot, so its
  dependencies are yours to set on the issue.

## When a card lands in Stuck

1. Read the **first line** of the bot's comment. That is the ask.
2. Pick **A, B or C**. The bot's recommendation is always first.
3. Reply on the card with the letter (add a sentence if you want).
4. Move the card back to **In progress**. That resets its attempt count and the bots retry.

If the comment does not follow that shape (no first-line ask, no lettered options), that is
a bug in the bots. Say so on the card and the next tick will fix the instructions.

## When a card comes back to Stuck a second time

- The bots tried twice and stopped. They will not try a third time on their own.
- Your options are the same: **A** retry as is, **B** edit the card then retry, **C** close it.
- Smaller cards get done. A card that stalls twice is usually two cards.

## When the fix landed and it still looks the same

- If you reported **something you saw** — a page that looks wrong, a number that is off, a
  screen showing what it should not — the bot looks at that same thing again before it
  closes your card.
- **The card only closes if what you saw actually changed.** If the code shipped and the
  page still looks the way you reported it, the card **stays open** and comes to you in
  **Stuck**, with a comment saying what was built and what the page still shows.
- So **a card that is still open with a merged PR on it is not a mistake.** It means the fix
  was for the wrong cause, and the bot told you instead of quietly marking it done.
- Answer it the usual way: **A, B or C**, then move the card back to In progress.
- A card with nothing to look at — a cleanup, a document, a decision — is not affected. This
  only applies to cards where you reported seeing something.

## What the bot comments mean

| Comment starts with | Meaning | You need to do |
|---|---|---|
| 🤖 Watcher picked this up | a tick started on the card | nothing |
| 🤖 `Needs @<login>:` | a decision or an answer is needed | pick A, B or C, move the card |
| 🤖 `Needs @<login>:` the fix landed and it still looks the same | the code shipped and the thing you reported did not change, so the card stayed open | pick A, B or C, move the card |
| 🤖 **FYI:** I labeled this for the … lane | the triage bot picked the lane | nothing, unless you disagree — then change the label |
| 🤖 **FYI:** not doing this yet — the card gates itself | the card's own text says to wait; the bot obeyed and put it back in Backlog | nothing, unless the wait is over — then edit that line out and move it to Ready |
| 🤖 **FYI:** this card waits on … | it is blocked by an open card; it starts when that closes | nothing, unless that card is not on its way |
| 🤖 **FYI:** plan for this card | what the bot is about to build, in three lines, before it starts. **The first, `You get:`, is what you will end up with and where you open it** | nothing, unless `You get` is not what you asked for — then say so on the card |
| 🤖 **FYI:** | information only | nothing |
| 🤖 Watcher: the run ended without finishing | the tick died or ran out of budget; the card will be retried once | nothing, unless it happens twice |
| 🤖 Watcher: a run died while holding this card | cleanup after a crash | nothing |

## What the Discord messages mean

Discord only speaks when a card is **stuck**, something **broke**, or it's **Monday
morning**. Every message but the Monday one links to a card. Everything else — a card
starting, each step, a card shipping — is on the floor page, not in the channel.

| Message starts with | Meaning | You need to do |
|---|---|---|
| 🚨 `Needs @<login>:` | a card is in Stuck; the ask is in the message | open the card, pick A, B or C, move it back to In progress |
| ⚠️ #n: the run ended without finishing | the tick died; it will be retried once | nothing, unless it comes back as 🚨 |
| 📉 **Something broke:** | a product check failed; a card with the numbers is in Backlog | open the link in the message and look; move the card to In progress to have it fixed |
| 📊 Weekly digest | Monday morning's numbers, per lane — ticks, first-pass rate, parks, turns, deaths, this week next to last | nothing |

To turn it off, remove `DISCORD_WEBHOOK_URL` from `/etc/watcher/env` on the box and
restart the unit. To get pinged on 🚨 only, set `DISCORD_MENTION` there to your Discord
user mention (`<@your-id>`) or `@here`.

## The watcher's own board

- **The watcher works its own cards too,** on the
  [Watcher board](https://github.com/users/roloc/projects/2). Same columns, same rules.
- **One card at a time** there, separate from riffmap's two.
- **What it merges goes live by itself** once the checks pass — within about five minutes,
  whatever any bot is doing at the time (next section).
- **You never merge anything,** here or on RiffMap. The bots merge their own work once the
  build check passes and the reviewer finds nothing blocking. Changes to their own safety
  checks get the strictest review, and nothing goes live until its tests pass again.

## New watcher versions deploy themselves

- **You never deploy.** The box checks for a new version every five minutes.
- It ships one only when **the build check passed** and **the bots' tests passed**. It
  never pauses a bot or waits for one to finish a card — a version lands in its own folder,
  and each bot picks it up on its own, between cards, so nothing it is doing is ever
  interrupted.
- **If a version fails its checks,** a card lands in **Stuck** saying which one. Move it to
  **In progress** to have the watcher fix it, or add the **deploy-anyway** label to ship it.
- **The floor page's Ship desk says which version each bot is running** — so if one still
  shows an older one, it just has not finished its current card yet.
- **To turn deploys off,** on the box: `systemctl stop watcher-deploy.timer` (`start` turns
  them back on).

## How well the bots are doing

A report adds up what every past run already said about itself.

- **First-pass merge rate** — of the cards that made it all the way to Done, how many did
  it in one run, no do-overs.
- **Parked** — split two ways: parked by a bot with a real question for you, and parked
  because it tried the same card twice in a row and gave up either time.
- **Died** — a run that crashed or ran out of room before it could finish.
- **Turns and quota per shipped card** — the typical and the worst it took, in bot turns
  and quota, to get one card all the way to Done.
- **Time from start to Done** — the typical and the worst wall-clock time, including any
  stretch a card spent sitting in Stuck waiting on you.

**Every Monday, the short version lands in Discord on its own** — one line per lane, this
week's ticks, first-pass rate, parks, turns and deaths, last week's number next to each
(card #139). The full report above is still something you ask for; nothing pings you for
that one.

<details><summary>Command (on the box)</summary>

```
python3 /opt/watcher/bin/metrics.py ~watcher/.local/state/watcher/riffmap
python3 /opt/watcher/bin/metrics.py ~watcher/.local/state/watcher/riffmap ~watcher/.local/state/watcher/riffmap-design --weeks 4
python3 /opt/watcher/bin/metrics.py --digest          # the Monday shape, printed instead of posted
```

One state directory per lane (riffmap, riffmap-design, riffmap-triage, watcher); give it
several to see them side by side. `--weeks N` sets how many weeks the weekly table covers
(default 8). `--digest` with no state directory reads every lane on the box; add `--post`
to send it to Discord by hand instead of waiting for Monday.

</details>

## Pause, resume, logs

<details><summary>Commands (on the box)</summary>

```
# pause / resume one lane (design is riffmap-design, triage is riffmap-triage; pause all three to pause riffmap)
touch ~watcher/.local/state/watcher/riffmap/PAUSE
rm    ~watcher/.local/state/watcher/riffmap/PAUSE

# what a lane's tick is doing right now, one line per tool call — the card it holds,
# then a live stream (Ctrl-C to stop); --no-follow prints what's there now and exits
/opt/watcher/bin/tail.sh riffmap
/opt/watcher/bin/tail.sh riffmap-design
/opt/watcher/bin/tail.sh riffmap-triage
/opt/watcher/bin/tail.sh watcher           # the watcher's own lane

# coarser: when a lane polled, launched or exited a tick — not what the tick itself did
journalctl -u watcher@riffmap -f
journalctl -u watcher@riffmap-design -f
journalctl -u watcher@riffmap-triage -f
journalctl -u watcher@watcher -f          # the watcher's own lane
journalctl -u watcher-deploy -n 30        # what the deployer last did

# what did the last tick say, and how much quota it used (not a bill — the box is on your subscription)
ls -t ~watcher/.local/state/watcher/riffmap/logs/ | head
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("subtype"), "quota~$%s (not billed)" % d.get("total_cost_usd")); print(d.get("result"))' \
  ~watcher/.local/state/watcher/riffmap/logs/<newest>.json

# the board from the command line
board list "In progress"
board show 87
board move 87 "In progress"
board unclaim 87        # only if a card is stuck with in-flight and nothing is running
```

</details>
