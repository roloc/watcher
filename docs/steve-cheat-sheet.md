# Steve's cheat sheet

**You move cards. The bots do the rest.** One page, no code above the fold.

## The columns

- **Ready** — yours. The bots never touch it.
- **In progress** — move a card here to hand it to the bots.
- **In review** — the bot's PR is finished and green but the merge did not go through. Nothing for you to do: after two hours the bots try once more, and if that fails the card comes to you in **Stuck** with the question. A card you drop here by hand, with no PR, is moved to **Stuck** within about ten minutes asking where you meant to put it.
- **Done** — shipped.
- **Stuck - Needs help** — the bots need you. Read the last comment on the card.

## Three lanes

- Design cards and build cards run **side by side**, one of each at a time.
- The label decides the lane: `agent: architect` or `agent: designer` is the design lane.
  Every other label is the build lane. Two builds never run at once.
- **No label needed.** A card with no `agent:` label is read by the triage lane within a
  minute of you moving it to In progress, whatever the other two are doing. It adds the
  label, says why in a comment, and the right lane takes it from there. Change the label
  if you disagree; the card moves lanes with it.
- A card in one lane never waits on the other. It never touches the other's branch either.

## Cards that wait on other cards

- Move several cards over at once; the bots work the prerequisite first.
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

## What the bot comments mean

| Comment starts with | Meaning | You need to do |
|---|---|---|
| 🤖 Watcher picked this up | a tick started on the card | nothing |
| 🤖 **Needs Steve:** | a decision or an answer is needed | pick A, B or C, move the card |
| 🤖 **FYI:** I labeled this for the … lane | the triage bot picked the lane | nothing, unless you disagree — then change the label |
| 🤖 **FYI:** this card waits on … | it is blocked by an open card; it starts when that closes | nothing, unless that card is not on its way |
| 🤖 **FYI:** | information only | nothing |
| 🤖 Watcher: the run ended without finishing | the tick died or ran out of budget; the card will be retried once | nothing, unless it happens twice |
| 🤖 Watcher: a run died while holding this card | cleanup after a crash | nothing |

## What the Discord messages mean

Discord repeats the board; the card is always the record. Every message links to the card.

| Message starts with | Meaning | You need to do |
|---|---|---|
| 🚨 **Needs Steve:** | a card is in Stuck; the ask is in the message | open the card, pick A, B or C, move it back to In progress |
| 📋 #n → **In review** | the PR is green but did not merge | nothing; the bots retry in two hours, then ask you in Stuck |
| 📋 #n → *(any other column)* | the card changed column | nothing |
| 🔧 #n → **Building** (or any stage) | the bot crossed into that step | nothing |
| ✅ #n shipped | merged and closed; the PR is linked | nothing |
| 🗂️ #n was closed without a merge | the card was closed by hand | nothing |
| ⚠️ #n: the run ended without finishing | the tick died; it will be retried once | nothing, unless it comes back as 🚨 |

To turn it off, remove `DISCORD_WEBHOOK_URL` from `/etc/watcher/env` on the box and
restart the unit. To get pinged on 🚨 only, set `DISCORD_MENTION` there to your Discord
user mention (`<@your-id>`) or `@here`.

## Deploy a new watcher version

- One command, as root on the box: `/opt/watcher/deploy/rollout.sh riffmap`
- It covers every lane. Each lane waits for its own running tick to finish, then swaps.
  It never kills one.
- Add a branch name at the end to deploy a branch before it merges.

## Pause, resume, logs

<details><summary>Commands (on the box)</summary>

```
# pause / resume one lane (design is riffmap-design, triage is riffmap-triage; pause all three to pause riffmap)
touch ~watcher/.local/state/watcher/riffmap/PAUSE
rm    ~watcher/.local/state/watcher/riffmap/PAUSE

# what is a lane doing right now
journalctl -u watcher@riffmap -f
journalctl -u watcher@riffmap-design -f
journalctl -u watcher@riffmap-triage -f

# what did the last tick say and cost
ls -t ~watcher/.local/state/watcher/riffmap/logs/ | head
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("subtype"), d.get("total_cost_usd")); print(d.get("result"))' \
  ~watcher/.local/state/watcher/riffmap/logs/<newest>.json

# the board from the command line
board list "In progress"
board show 87
board move 87 "In progress"
board unclaim 87        # only if a card is stuck with in-flight and nothing is running
```

</details>
