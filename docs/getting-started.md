# Getting started: fork to first merged card in an afternoon

Fork this repo, set up one box, and watch it merge its first card. Read
`docs/threat-model.md` first if you want to know exactly what the box can touch before you
give it write access to anything — short version: two GitHub Apps' keys and a Claude
subscription token, nothing else, on a box you can rebuild if it ever misbehaves.

Each step below says what to run and what tells you it worked. Steps marked **by hand**
are not scripted on purpose (an account, a grant, a judgment call); everything else is one
command.

## 1. Fork the repo

**By hand:** fork this repo on GitHub, into your own account or org.

```
git clone <your fork's clone URL>
cd watcher
```

Your fork's `main` is what your box runs — `deploy/autodeploy.sh` ships whatever lands
there by itself, so nothing after step 4 is a manual deploy.

**Check:** `git remote -v` shows your fork, not this one.

## 2. An organization and two GitHub Apps

GitHub's terms allow one machine account per person, and a lane, a desk and a few sessions
each wanting their own login is how an owner ends up suspended (the watcher's own owner was,
November 8, 2026). The sanctioned shape is a **GitHub App**: free, unlimited, and with its
own 5,000-requests-per-hour budget per installation. So the watcher acts as Apps, not as a
user. A user-owned Projects board is out of an App's reach, which is why the repos and boards
live in an **organization**.

**By hand:** create a free organization at `https://github.com/organizations/plan` and move
(or create) the repo there. Then, per project, on the box as root:

```
python3 /opt/watcher/deploy/app-setup.py myproject --org myorg --repos myorg/myproject
```

and open the address it prints. The page has two buttons, **Create roloc-myproject-author**
and **Create roloc-myproject-reviewer**; click each, then click **Install** on the repos it
names. The helper receives each App's ID and private key and writes them under
`/etc/watcher/apps/myproject/` itself. You never download or paste a key. About thirty seconds.

- The **author** App pushes, opens PRs, comments and moves cards.
- The **reviewer** App files the review verdict as a real GitHub review. Its key is read by a
  separate system user (`watcher-reviewer`), so a tick cannot approve its own PR.

On a Mac the same command (as yourself, `--roles author`) writes under
`~/.config/roloc-apps/myproject/`. There is no reviewer App there: no process boundary.

**Fallback, one machine account.** Not everyone wants an organization. A single bot account
(`https://github.com/join`, never your own, one per person) still works: skip this step's
helper and do `su - watcher; gh auth login; gh auth setup-git` in step 4. The box notices
that `/etc/watcher/apps/myproject/` is absent and leaves `gh` alone. The reviewer's verdict is
then a PR comment instead of a review.

**Check:** `bin/gh-app-token.sh myproject author --identity`, run on the box, prints
`roloc-myproject-author[bot]` and an address.

`gh api user` is not a check here: an installation token has no user, so GitHub answers 403
("Resource not accessible by integration"). That 403 is the App working, not broken.

## 3. The Claude login

Your own Claude **subscription**, never a shared one.

**By hand**, on a machine with a browser:

```
claude setup-token
```

It prints one token value. Put it into `/etc/watcher/env` on the box (step 4 creates that
file) as:

```
CLAUDE_CODE_OAUTH_TOKEN=<the printed value>
```

Never set `ANTHROPIC_API_KEY` in that file — it would outrank the login and bill your API
account instead of spending the subscription's own quota
(`docs/how-it-works.md`, "The dollar figure is quota, not a bill"). Each tick can use up
to `TICK_MAX_BUDGET_USD` of **imputed** quota (`bin/spawn.sh`, default $15) — a usage
meter, not an invoice; nobody is billed per tick.

**Check:** the command printed a token and no error. Step 4's check confirms it logged in
for real.

## 4. The box

A Debian 12 VM or LXC with systemd, 4 cores, 8 GB, nothing else running on it. As root:

```
apt install git
git clone <your fork's clone URL> /opt/watcher
/opt/watcher/deploy/bootstrap.sh
/opt/watcher/deploy/install.sh
```

`deploy/bootstrap.sh` installs everything else the box needs (python3, curl, `gh`, a
browser, Node 22, the Claude CLI) — idempotent, so re-running it after an interrupted or
partial run only installs what is still missing. `deploy/install.sh` makes the `watcher`
user, the systemd units, `/etc/watcher/env` and the floor page.

**Check:** `install.sh`'s own last lines. It prints `Next, by hand:` with a list, or
`Nothing left by hand` once everything is set. Fill in what it lists — the token from
step 3 goes in `/etc/watcher/env`; with the fallback single bot account, its GitHub login goes
in with `su - watcher; gh auth login; gh auth setup-git` (Apps need no login) — then run `/opt/watcher/deploy/install.sh`
again. It is safe to re-run, and this time it should print `Nothing left by hand`.

**Any Linux host with systemd will do.** Proxmox is what the original box runs on, and it
is incidental: the repo never talks to it except for optional telemetry on the floor page.
A cloud VM, a spare Mac mini or laptop running Debian, WSL2 on a desktop that stays on, or
a guest on whatever hypervisor you already have all work exactly as written above. What the
watcher does need is a machine that stays on, keeps its disk between restarts, and runs
systemd: each lane is a systemd unit, the floor page is a service, the deployer and the
detector are timers, and a deploy swaps a release folder under a running lane without
killing the tick it is in the middle of (one can run for two hours). That is why there is
no Docker image or Kubernetes chart yet. Containers are not a safety problem, since a
container is more throwaway than a VM, but replacing a container kills the tick inside it,
so a container build needs its own answer to "wait for the lane to be idle" first. If that
is the shape you need, start with a VM so your first afternoon is about seeing a card
merge, and file the port as a card once you have.

## 5. First project

From **your own machine**, never the box, authenticated as **yourself**:

```
BOT_LOGIN=<the bot's login from step 2> deploy/add-project.sh <name> <owner/repo>
```

It builds the board, then stops once and prints the one thing GitHub gives no API for —
granting the bot access to the new board — and asks you to confirm you did it. (The
board's Auto-add workflow is optional now: every card the watcher files puts itself on the
board.) Do it, answer yes, and it goes on to the labels,
`main`'s branch protection and `projects/<name>.env`. Answering no, or piping it
non-interactively, stops the script there; re-run the same command once it is done —
everything it already built is left alone the second time.

```
git add projects/<name>.env
git commit -m "add <name>"
git push
```

On the box:

```
/opt/watcher/deploy/install.sh <name>
systemctl enable --now watcher@<name>
```

**Check:** `journalctl -u watcher@<name> -n 20` shows it polling the board, and
`install.sh`'s own output (it runs `bin/doctor.sh` for you) ends with `doctor: ok`.

## 6. The project's own CLAUDE.md

Paste `templates/project-CLAUDE.md`'s section into the project's own `CLAUDE.md` and fill
in every `<bracket>` — the required CI check's name most of all, in the exact phrase
`required CI check is \`<name>\`` (backticks included): `bin/doctor.sh` greps for those
exact words and warns when it can't find them.

**Check:** re-run step 5's `install.sh <name>`; `bin/doctor.sh`'s output carries no
warning about the required check.

While you are in the project: give it a `docs/board.md` from `templates/board.md` (step
5's `add-project.sh` printed the `sed` line that fills in the repo, the board's owner and
its number). It is the page a session opens when you say "file a card" or "take a look at
#n" — the commands, the labels, the body shape — so nobody rediscovers them per session.

## 7. First card

**By hand:** file an issue on the project's repo (through the board, or
`gh issue create`). Label nothing. Move it to **In progress** — that column is the
hand-off to the lane (`Ready` only gets pulled from by itself once `READY_PULL=1` is set
in `projects/<name>.env`, which `deploy/add-project.sh` does not set for you).

Open the floor page: `http://<the box's address>:8787/`.

**Check, in the first ten minutes:** the card's Stage field reaches **Claimed** within a
couple of minutes of the move (the lane polls once a minute), then ticks through
Building, Reviewing, Merging; the floor page shows
the project's lane working, not idle; and the card gets a `🤖 **FYI:** plan for this card`
comment before the Stage leaves Claimed.

## 8. When a card lands in Stuck

The bot needs a decision it can't make itself. Read the **first line** of its newest
comment — that is the ask — then the lettered options under it.

**By hand:** reply with a letter (or say which way you lean) on the card, then move it
back to **In progress**.

**Check:** the card leaves **Stuck - Needs help**; the lane picks it up again within a
minute (`journalctl -u watcher@<name> -f`, or just watch the floor page).

---

From here, `docs/how-it-works.md` is the map for anything you change in this repo, and
`docs/owner-cheat-sheet.md` is the one page for whoever answers Stuck cards day to day.
