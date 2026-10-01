<!--
This is the section a project's own CLAUDE.md needs for the watcher to run against it —
built from what commands/watch.md, commands/triage.md and agents/reviewer.md actually read
out of a project's CLAUDE.md (grep for `project's \`CLAUDE.md\`` across the three if this
drifts). Paste it into the project's CLAUDE.md and fill in every <bracket>; delete a
subsection only when it truly does not apply (most do — see the note on each one). This
repo's own "When the watcher works its own board" section (CLAUDE.md) is the first filled-
in example; read it alongside this file.

bin/doctor.sh greps for the exact words "required CI check is `<name>`" — keep that phrase
verbatim, backticks included, or doctor.sh reports the project as not naming one even
though a human reading the file would say it obviously does.
-->

## Running under the watcher

- **The required CI check is `<name>`.** <Run its test suite(s) locally before pushing —
  name the command(s).> A red check you could have caught in a few seconds locally is a
  wasted round trip.

- **Reading the project's own data.** <How is a tick allowed to look at what the card is
  about — a live page, a meeting, a record, a screenshot? Name the exact mechanism (a
  read-only share-link form, an API a bot token can call, a fixtures directory) and, if
  there is one, the URL shape it is found at (riffmap: a meeting is read through its
  `/s/<token>` share link; the owner's `/maps/<key>/…` URL always needs a human's Google
  session and is never the tick's way in).> If a card names something with no such link on
  it, `watch.md` §2 already covers what a tick does next (ask for exactly that, build
  nothing meanwhile) — this section only has to say what the legitimate way in IS.

- **Where its people are.** <Where does the person who files cards actually open this
  project's results — an app, a page, the board, a channel — and on what device? Say what
  they never use (a shell on the box, a log file). `watch.md` §2 makes the first line of
  every plan `You get:` and builds for the place the asker can open; without this bullet a
  tick assumes a phone and the board.>

- **Looking at a screen.** <If the project has anything a person looks at that runs code —
  a web app, a page with its own script — name the command that renders it and writes a
  picture (riffmap: `cd web && npm run shots`; the watcher: `python3
  bin/test-floor-browser.py --shots <dir>`), and the one that loads a live URL the way a
  browser does (the watcher: `python3 bin/browse.py check <url>`). `watch.md` and
  `reviewer.md` treat a change to such a screen that nothing rendered as not shippable, and
  a page fetched with `curl` as not looked at. Delete this bullet if the project has no
  screen.>

- **The merge gate** is <list the files/paths: the CI workflow, a pre-commit or pre-push
  hook, `agents/reviewer.md` if the project keeps its own, any replay/eval suite>. A change
  to any of it always gets the reviewer pass, even when it reads as prose or config, and
  anything that weakens it (drops a check, loosens a condition, makes a test easier to
  pass) is a blocking finding, never a nit.

- **Contracts a builder must not touch.** <Name anything a tick is never allowed to decide
  for itself — a data contract between services, a pricing rule, a migration, a public API
  shape — so `watch.md` §4 sends a card that would touch it straight to Stuck instead of
  guessing. Delete this bullet if the project has none.>

- **Quota vs. real spend.** <If the project calls a model itself, through its own key or
  backend (not the watcher's own subscription token), say so and name the env var that
  pins it (riffmap: `RIFFMAP_BACKEND=claude`). If it pins a subscription backend with no
  metered path, say that instead — `watch.md`'s "$2 of real spend goes to Stuck first" rule
  only bites when there is a real bill to worry about. Delete this bullet if the project
  never calls a model on its own.>

- **Standing traps for the reviewer.** <Anything `agents/reviewer.md` should check on every
  pass because it has broken before and would not be obvious from the diff alone — a
  footgun in the framework, a convention the linter does not catch, a file that looks
  editable but is generated. Delete this bullet if there are none yet; add to it the first
  time one bites.>

- **Access, for a card `/triage` cannot route.** <If a symptom card can arrive with no
  reachable link at all (commands/triage.md's "stuck on access" case), name the one action
  that unblocks it, in the exact words a Stuck comment should use — `- Steve: <that
  action> — no tick can open the link above.`>
