// Card #214: the owner pane — a pane in the owner's own desktop Claude sessions, and three
// commands. A reader of the floor page's JSON and the Mac's own `gh` login; it never touches a
// lane, a tick, /opt/watcher or the board except on a command the owner typed.
//
//   session.start   registers /stuck, /card and /file-card, opens the pane, reads the floor page
//                   now and then every minute (`$.clock.every`).
//   ui.render       the pane: the deploy line, one row per Stuck card, one row per lane.
//   command.run     /stuck                lists the open questions
//                   /stuck <n> <reply>    posts <reply> on card n, then moves it to In progress
//                                         (only a card the floor page lists as Stuck)
//                   /card [repo#]<n>      title, column, newest comment, its PR
//                   /file-card <text>     drafts the house body (docs/board.md) and shows it
//                   /file-card yes|no     files the draft with `gh issue create`, or drops it
//
// Deviation from the card: the deploy line comes from /api/state.json (each project's `ship`),
// not /api/ship.json, which needs a project argument and says no more.
// Tests: hooks/register.test.ts — `claude plugin test mods/owner-pane`.
import { atom, read, update } from 'claude-code'
import type { EngineInterface as Dollar, Register } from 'claude-code'

import type { Draft, Snapshot } from '../types'
import { DRAFT_PROMPT, EMPTY, cardText, envFile, fromState, laneLine, parseDraft, repoOf, stuckText } from './model'

const PANE = 'owner-pane'
const RUN_MS = 30_000
const EVERY_MS = 60_000

// Change a tag when the shape changes: it declines what an earlier version stored.
const snapshot = atom({ plugin: 'owner-pane', key: 'snapshot' } as const, EMPTY as Snapshot, { shape: 'snapshot-1' })
const draft = atom({ plugin: 'owner-pane', key: 'draft' } as const, null as Draft | null, { shape: 'draft-1' })

type Options = { floorUrl?: string; checkout?: string; repo?: string }

async function checkout($: Dollar, configured: string | undefined): Promise<string> {
  return (configured ?? '~/projects/watcher').replace(/^~(?=\/|$)/, (await $.env.get('HOME')) ?? '')
}

async function sh($: Dollar, argv: string[], init: { stdin?: string } = {}) {
  const r = await $.process.run(argv, { timeoutMs: RUN_MS, ...init })
  return { ok: r.exitCode === 0, out: r.stdout.trim(), err: r.stderr.trim() }
}

async function refresh($: Dollar, floor: string): Promise<Snapshot> {
  try {
    const r = await $.http.fetch(`${floor}/api/state.json`)
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    const snap = fromState(JSON.parse(r.text), new Date().toISOString())
    await update($, snapshot, () => snap)
    return snap
  } catch (err) {
    const why = err instanceof Error ? err.message : String(err)
    return update($, snapshot, (s: Snapshot) => ({ ...s, error: why }))
  }
}

export const register: Register = (on, options) => {
  const o = options as Options
  const floor = (o.floorUrl ?? 'http://agents:8787').replace(/\/+$/, '')
  const defaultRepo = o.repo ?? 'roloc/watcher'

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'stuck', description: 'The open questions waiting on you; /stuck <n> <reply> answers one' })
    await $.command.register({ name: 'card', description: 'Read a card: /card <n>' })
    await $.command.register({ name: 'file-card', description: 'Draft a card from one sentence; /file-card yes files it' })
    void $.ui.open({ id: PANE, title: 'Watcher' })
    void refresh($, floor)
    $.clock.every(EVERY_MS, () => void refresh($, floor))
    return next(e)
  })

  on('command.run', { command: 'stuck' }, async ($, e) => {
    const snap = await refresh($, floor)
    const m = /^\s*#?(\d+)\s+([\s\S]*\S)\s*$/.exec(e.args)
    if (!m) return { text: snap.error && !snap.at ? `Could not read the floor page: ${snap.error}` : stuckText(snap) }
    const card = snap.stuck.find((c) => String(c.number) === m[1])
    if (!card) return { text: `#${m[1]} is not one of the cards the floor page lists as Stuck, so nothing was posted.` }
    const repo = repoOf(card.url)
    if (!repo) return { text: `Could not tell which repository #${card.number} is in from ${card.url}; nothing was posted.` }
    const post = await sh($, ['gh', 'issue', 'comment', String(card.number), '-R', repo, '--body', m[2]])
    if (!post.ok) return { text: `The reply did not post (${post.err}); the card was not moved.` }
    const dir = await checkout($, o.checkout)
    const move = await sh($, ['bash', '-c', 'set -a; . "$1"; set +a; exec python3 "$2" move "$3" "In progress"', '_', envFile(dir, card.project), `${dir}/bin/board.py`, String(card.number)])
    await refresh($, floor)
    return { text: move.ok ? `Posted on #${card.number} and moved it to In progress.` : `Posted on #${card.number}, but the move failed (${move.err}). Move it to In progress by hand.` }
  })

  on('command.run', { command: 'card' }, async ($, e) => {
    const m = /^\s*(?:(\S+\/\S+?)#)?#?(\d+)\s*$/.exec(e.args)
    if (!m) return { text: 'Usage: /card <n>   or   /card owner/repo#<n>' }
    const repo = m[1] ?? defaultRepo
    const view = await sh($, ['gh', 'issue', 'view', m[2], '-R', repo, '--json', 'title,state,labels,comments,projectItems'])
    if (!view.ok) return { text: `Could not read #${m[2]} in ${repo}: ${view.err}` }
    const prs = await sh($, ['gh', 'pr', 'list', '-R', repo, '--search', `${m[2]} in:body`, '--state', 'all', '--json', 'number,state,url'])
    return { text: cardText(m[2], JSON.parse(view.out), prs.ok && prs.out ? JSON.parse(prs.out) : []) }
  })

  on('command.run', { command: 'file-card' }, async ($, e) => {
    const arg = e.args.trim()
    const pending = await read($, draft)
    const yes = /^(yes|y)$/i.test(arg)
    const no = /^(no|n)$/i.test(arg)
    if (yes || no) {
      if (!pending) return { text: 'There is no draft waiting. Start one with /file-card <one sentence>.' }
      await update($, draft, () => null)
      if (no) return { text: 'Dropped the draft. Nothing was filed.' }
      const made = await sh($, ['gh', 'issue', 'create', '-R', pending.repo, '--title', pending.title, '--body-file', '-'], { stdin: pending.body })
      const n = /\/issues\/(\d+)/.exec(made.out)?.[1]
      if (!made.ok || !n) return { text: `The card was not filed (${made.err || made.out}). Run /file-card again to redraft.` }
      const col = await sh($, ['gh', 'issue', 'view', n, '-R', pending.repo, '--json', 'projectItems', '--jq', '.projectItems[].status.name'])
      const where = col.ok && col.out ? `on the board in ${col.out}` : 'NOT on the board yet — add it with `gh project item-add` (docs/board.md, "Filing a card")'
      return { text: `Filed ${made.out}: ${where}.` }
    }
    if (!arg) return { text: 'Usage: /file-card <one sentence about what you want>' }
    const r = await $.model.complete({ model: 'sonnet', prompt: DRAFT_PROMPT(arg) })
    const parsed = r.isAnswered ? parseDraft(r.text, defaultRepo) : undefined
    if (!parsed) return { text: 'The draft did not come back in the house shape; nothing was filed. Try the sentence again.' }
    await update($, draft, () => parsed)
    return { text: `Draft for ${parsed.repo} (nothing filed yet):\n\n${parsed.title}\n\n${parsed.body}\nReply \`/file-card yes\` to file it in Backlog, or \`/file-card no\` to drop it.` }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const s = await read($, snapshot)
    return (
      <Box flexDirection="column">
        <Text dimColor>{s.error && !s.at ? `Floor page unreachable: ${s.error}` : s.ship || 'Reading the floor page…'}</Text>
        <Text bold>Needs you ({s.stuck.length})</Text>
        {s.stuck.length === 0 && <Text dimColor>Nothing is waiting on you.</Text>}
        {s.stuck.map((c) => (
          <Text>#{c.number} {c.ask || '(no question posted yet)'}</Text>
        ))}
        <Text bold>Lanes</Text>
        {s.lanes.map((l) => (
          <Text dimColor={l.pill === 'idle'}>{laneLine(l)}</Text>
        ))}
        {s.error && s.at && <Text dimColor>Last read failed: {s.error}</Text>}
      </Box>
    )
  })
}
