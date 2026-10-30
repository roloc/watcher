// The card-context mod (card #213): a comment the owner leaves on a card while a tick holds it
// is read by that tick on its next turn, not the next tick. riffmap #778 (October 17, 2026):
// five triage ticks answered a card's body and repeated a park the owner had rejected in a
// comment. The prose rule "the owner's newest comment outranks the body" (watcher #185) is
// kept; this makes it structural.
//
//   prompt.submit    `/watch 213` or `/triage 213` (also `/watcher:watch`): remember the card number.
//   prompt.compose   every time the engine renders the system prompt (card number: the one
//                    remembered, else the worktree's branch `card-<n>` — a `claude -p` tick renders
//                    its prompt before its first prompt is submitted): `gh issue view <n> --json
//                    title,body,comments` (REST, one call per render, nothing against the GraphQL
//                    budget of card #114), appended as ONE `session` section, last: the title, the
//                    body, then the comments newest first.
//
// MEASURED on claude 2.1.312 under `claude -p` (October 30, 2026): the engine renders the system
// prompt ONCE, at startup, and reuses it for every request of the tick, however many tool turns
// follow. So the section is the card as it stood when the tick started; a comment added mid-tick
// is not seen until the next tick. The hook itself is written to be re-read on every render, so a
// build that renders more often (compaction, a resumed session) needs no change here.
//
// A failed read (gh not found, non-zero, unparseable) leaves the sections as the engine composed
// them and prints one stderr line, which lands in the tick's .stderr and the floor page's Peek.
// commands/watch.md and commands/triage.md are not edited: their own "read the card" steps stay,
// and this is a second copy that cannot go stale. It reads; it never writes the board.
//
// Tests: hooks/register.test.ts — `claude plugin test mods/card-context`.
import type { EngineInterface, Register } from 'claude-code'

type Engine = EngineInterface

const NAME = 'card-context'
const RUN_MS = 20_000

export const HEADING = "The card as it stands now; the owner's newest comment is the ask."

// The little this module remembers. A reload of the worker forgets it; the tick's next turn
// then composes without a section until the next prompt names the card again.
let card: string | undefined

export const cardFromPrompt = (text: string): string | undefined =>
  /^\s*\/(?:watcher:)?(?:watch|triage)\s+#?(\d+)\b/.exec(text)?.[1]

type Comment = { author?: { login?: string } | null; createdAt?: string; body?: string }
type Issue = { title?: string; body?: string; comments?: Comment[] }

// The section's text. Comments newest first (by createdAt; the list's own order breaks ties).
export function cardSection(n: string, issue: Issue): string {
  const comments = (issue.comments ?? [])
    .map((c, i) => ({ c, i }))
    .sort((a, b) => (b.c.createdAt ?? '').localeCompare(a.c.createdAt ?? '') || b.i - a.i)
    .map(({ c }) => c)
  const out = [`# ${HEADING}`, '', `Card #${n}: ${issue.title ?? ''}`, '']
  if (comments.length) {
    out.push('## Comments, newest first', '')
    for (const c of comments) {
      out.push(`### ${c.author?.login ?? 'unknown'}, ${c.createdAt ?? ''}`, '', (c.body ?? '').trim(), '')
    }
  } else {
    out.push('## Comments', '', '(none)', '')
  }
  out.push('## Body', '', (issue.body ?? '').trim(), '')
  return out.join('\n')
}

const warn = (why: unknown) =>
  console.error(`${NAME}: could not read card ${card}: ${why instanceof Error ? why.message : String(why)}`)

async function cardNumber($: Engine): Promise<string | undefined> {
  if (card) return card
  const r = await $.process.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], { timeoutMs: RUN_MS })
  return r.exitCode === 0 ? /^card-(\d+)$/.exec(r.stdout.trim())?.[1] : undefined
}

async function read($: Engine, n: string): Promise<Issue> {
  const r = await $.process.run(['gh', 'issue', 'view', n, '--json', 'title,body,comments'], { timeoutMs: RUN_MS })
  if (r.exitCode !== 0) throw new Error(`gh issue view exited ${r.exitCode}`)
  return JSON.parse(r.stdout) as Issue
}


export const register: Register = (on) => {
  on('prompt.submit', async ($, e, next) => {
    const n = cardFromPrompt(e.text)
    if (n) card = n
    return next(e)
  }).catch(($, e, next) => next(e))

  on('prompt.compose', async ($, e, next) => {
    const base = await next(e)
    try {
      const n = await cardNumber($)
      if (!n) return base
      card = n
      const text = cardSection(n, await read($, n))
      return { ...base, sections: [...base.sections, { id: `${NAME}:card`, text, scope: 'session' as const }] }
    } catch (err) {
      warn(err)
      return base
    }
  }).catch(($, e, next) => next(e))
}
