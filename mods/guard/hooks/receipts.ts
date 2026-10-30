// The receipts mod (card #211), beside the guard in the same plugin: three things commands/watch.md
// §3 asks a tick to remember, done from the tool's own result instead. It only ever runs
// the steps §3 spells out; it approves, merges and refuses nothing, and the §3 prose still
// stands, so a tick that does them as well just posts an identical comment.
//
//   prompt.submit        `/watch 211` (or `/watcher:watch 211`): remember the card number.
//   Bash                 `gh pr create` that succeeded: its stdout holds the PR URL. Remember the
//                        PR, `board stage <card> "PR open"`, and post a usability line held from
//                        before the PR existed.
//   Agent                `reviewer` / `usability` returned: its first line, verbatim, as
//                        `🤖 reviewer: …` / `🤖 usability: …` on the remembered PR (the PR of this branch
//                        when none is remembered). The receipt goes up whatever the verdict. For the
//                        reviewer, then `gh run rerun <latest CI run on the branch> --failed`, the
//                        step that makes review-receipt look again.
//   Bash                 `gh pr merge` that succeeded: `board stage <card> Merging`.
//
// The hook never composes a verdict: a first line that is not `Reviewed <40 hex>: …` /
// `Usability <40 hex>: …` is not posted at all. Every action is best effort — a failure is one
// stderr line, and the tool's result goes back to the model exactly as it came.
//
// Tests: hooks/receipts.test.ts — `claude plugin test mods/guard`.
import type { EngineInterface, Register } from 'claude-code'

type Engine = EngineInterface

const NAME = 'guard'
const RUN_MS = 20_000

// The little this module remembers. A reload of the worker forgets it; each step then asks
// git or gh for the same fact instead.
let card: string | undefined
let pr: string | undefined
let heldUsability: string | undefined

const warn = (what: string, why: unknown) =>
  console.error(`${NAME}: receipts: ${what} failed: ${why instanceof Error ? why.message : String(why)}`)

async function run($: Engine, argv: string[]): Promise<{ ok: boolean; out: string }> {
  const r = await $.process.run(argv, { timeoutMs: RUN_MS })
  return { ok: r.exitCode === 0, out: r.stdout.trim() }
}

export const cardFromPrompt = (text: string): string | undefined =>
  /^\s*\/(?:watcher:)?watch\s+#?(\d+)\b/.exec(text)?.[1]

export const prFromCreate = (stdout: string): string | undefined => {
  const urls = [...stdout.matchAll(/\/pull\/(\d+)\b/g)]
  return urls.length ? urls[urls.length - 1][1] : undefined
}

// The agent's first line, if it is a receipt line: the SHA is what bin/receipt-check.sh reads.
export function receiptLine(text: string, kind: 'reviewer' | 'usability'): string | undefined {
  const first = text.trim().split('\n')[0].trim()
  const shape = kind === 'reviewer' ? /^Reviewed [0-9a-f]{40}: \S/ : /^Usability [0-9a-f]{40}: \S/
  return shape.test(first) ? first : undefined
}

const agentKind = (type: unknown): 'reviewer' | 'usability' | undefined => {
  const base = typeof type === 'string' ? type.split(':').pop() : undefined
  return base === 'reviewer' || base === 'usability' ? base : undefined
}

const resultText = (result: unknown): string => {
  const c = (result as { content?: Array<{ type?: string; text?: string }> } | undefined)?.content
  return Array.isArray(c) ? c.filter((b) => b?.type === 'text').map((b) => b.text ?? '').join('\n') : ''
}

async function cardNumber($: Engine): Promise<string | undefined> {
  if (card) return card
  const branch = await run($, ['git', 'rev-parse', '--abbrev-ref', 'HEAD'])
  return branch.ok ? /^card-(\d+)$/.exec(branch.out)?.[1] : undefined
}

async function stage($: Engine, name: string) {
  const n = await cardNumber($)
  if (!n) return
  const r = await run($, ['board', 'stage', n, name])
  if (!r.ok) throw new Error(`board stage ${n} ${name} exited non-zero`)
}

async function prNumber($: Engine): Promise<string | undefined> {
  if (pr) return pr
  const r = await run($, ['gh', 'pr', 'view', '--json', 'number', '--jq', '.number'])
  return r.ok && /^\d+$/.test(r.out) ? r.out : undefined
}

async function post($: Engine, number: string, kind: 'reviewer' | 'usability', line: string) {
  const r = await run($, ['gh', 'pr', 'comment', number, '--body', `🤖 ${kind}: ${line}`])
  if (!r.ok) throw new Error(`gh pr comment ${number} exited non-zero`)
}

// §3: the push started a CI run; review-receipt failed on it before this comment existed.
async function rerun($: Engine) {
  const branch = await run($, ['git', 'rev-parse', '--abbrev-ref', 'HEAD'])
  if (!branch.ok || !branch.out) throw new Error('no branch name')
  const latest = await run($, ['gh', 'run', 'list', '--branch', branch.out, '--workflow', 'CI', '--limit', '1', '--json', 'databaseId', '--jq', '.[0].databaseId'])
  if (!latest.ok || !/^\d+$/.test(latest.out)) throw new Error('no CI run for the branch')
  const r = await run($, ['gh', 'run', 'rerun', latest.out, '--failed'])
  if (!r.ok) throw new Error(`gh run rerun ${latest.out} exited non-zero`)
}

const step = async (what: string, fn: () => Promise<void>) => { try { await fn() } catch (e) { warn(what, e) } }

// register.ts calls this from its own session.start hook (a second one without a matcher is refused).
export const forgetSession = () => { card = pr = heldUsability = undefined }

export const register: Register = (on) => {
  on('prompt.submit', async ($, e, next) => {
    const n = cardFromPrompt(e.text)
    if (n) card = n
    return next(e)
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError === true) return ran
    const create = /\bgh\s+pr\s+create\b/.test(e.command)
    const merge = /\bgh\s+pr\s+merge\b/.test(e.command)
    if (!create && !merge) return ran
    if (create) {
      const stdout = (ran.result as { stdout?: string } | undefined)?.stdout ?? ''
      const made = prFromCreate(stdout)
      if (!made) return ran
      pr = made
      await step('stage PR open', () => stage($, 'PR open'))
      if (heldUsability) {
        const line = heldUsability
        heldUsability = undefined
        await step('usability receipt', () => post($, made, 'usability', line))
      }
    } else {
      await step('stage Merging', () => stage($, 'Merging'))
    }
    return ran
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError === true) return ran
    const kind = agentKind((e as { subagent_type?: unknown }).subagent_type)
    if (!kind) return ran
    const line = receiptLine(resultText(ran.result), kind)
    if (!line) return ran
    const number = await prNumber($).catch(() => undefined)
    if (!number) {
      // usability runs before the PR exists: hold its line until `gh pr create` returns.
      if (kind === 'usability') heldUsability = line
      return ran
    }
    await step(`${kind} receipt`, () => post($, number, kind, line))
    if (kind === 'reviewer') await step('rerun of the failed checks', () => rerun($))
    return ran
  }).catch(($, e, next) => next(e))
}
