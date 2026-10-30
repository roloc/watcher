// Card #213: the card-context mod — `claude plugin test mods/card-context`, offline. `process.run`
// is answered from memory (a fixture `gh`), and every command it was asked is kept.
import { expect, mock, test } from 'claude-code/testing'
import { cardFromPrompt, cardSection, HEADING } from './register'

type On = Parameters<Parameters<typeof test>[1] extends (...a: infer A) => unknown ? (...a: A) => unknown : never>[1]

// The body says to do one thing; the owner's newer comment says not to. The list is oldest first,
// the way gh prints it.
const ISSUE = {
  title: 'Send a test mail',
  body: 'Only the owner can open the inbox.',
  comments: [
    { author: { login: 'bot' }, createdAt: '2026-10-17T10:00:00Z', body: 'Parked: only you can read the inbox.' },
    { author: { login: 'owner' }, createdAt: '2026-10-17T12:00:00Z', body: 'Please just send the test mail yourself.' },
  ],
}

const INPUT = { model: 'm', promptModel: 'm', surfaces: [], tools: [], outputStyle: null, traits: [] }

function engine(on: On, run: (cmd: string) => { exitCode: number; stdout: string }, branch = 'card-213', env: Record<string, string> = {}) {
  mock.env(on, env)
  const ran: string[] = []
  on('process.run', (_, e) => {
    const cmd = e.argv.join(' ')
    if (cmd.startsWith('git rev-parse')) return { value: { exitCode: 0, stdout: branch, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    ran.push(cmd)
    return { value: { ...run(cmd), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // The engine's own sections, standing under the mod.
  on('prompt.compose', () => ({ sections: [{ id: 'core', text: 'You are a tick.', scope: 'shared' as const }] }))
  return ran
}

test('the helpers read the card number and order the comments newest first', () => {
  expect(cardFromPrompt('/watch 213')).toBe('213')
  expect(cardFromPrompt('/watcher:triage #7')).toBe('7')
  expect(cardFromPrompt('please /watch 213')).toBeUndefined()
  const text = cardSection('213', ISSUE)
  expect(text.startsWith(`# ${HEADING}`)).toBe(true)
  expect(text.indexOf('Please just send the test mail')).toBeLessThan(text.indexOf('Parked: only you'))
  expect(text.indexOf('Parked: only you')).toBeLessThan(text.indexOf('Only the owner can open the inbox'))
})

test('a body and a newer comment that disagree: one session section, last, comment first', async ($, on) => {
  const ran = engine(on, () => ({ exitCode: 0, stdout: JSON.stringify(ISSUE) }))
  await $.prompt.submit({ text: '/watch 213' }).catch(() => undefined)
  const { sections } = await $.prompt.compose(INPUT)
  expect(ran).toEqual(['gh issue view 213 --json title,body,comments'])
  expect(sections.map((s) => s.id)).toEqual(['core', 'card-context:card'])
  const last = sections[sections.length - 1]
  expect(last.scope).toBe('session')
  expect(last.text).toContain(HEADING)
  expect(last.text.indexOf('Please just send the test mail')).toBeLessThan(last.text.indexOf('Only the owner can open'))
})

test('a failing gh leaves the sections as they were', async ($, on) => {
  engine(on, () => ({ exitCode: 1, stdout: '' }))
  await $.prompt.submit({ text: '/triage 213' }).catch(() => undefined)
  const { sections } = await $.prompt.compose(INPUT)
  expect(sections.map((s) => s.id)).toEqual(['core'])
})

test('gh output that is not JSON leaves the sections as they were', async ($, on) => {
  engine(on, () => ({ exitCode: 0, stdout: 'not json' }))
  await $.prompt.submit({ text: '/watch 213' }).catch(() => undefined)
  const { sections } = await $.prompt.compose(INPUT)
  expect(sections.map((s) => s.id)).toEqual(['core'])
})

test('no prompt yet (claude -p composes first): the worktree branch names the card', async ($, on) => {
  const ran = engine(on, () => ({ exitCode: 0, stdout: JSON.stringify(ISSUE) }))
  const { sections } = await $.prompt.compose(INPUT)
  expect(ran).toEqual(['gh issue view 213 --json title,body,comments'])
  expect(sections.map((s) => s.id)).toEqual(['core', 'card-context:card'])
})

test('no prompt and no card branch: nothing is added and gh is not asked', async ($, on) => {
  const ran = engine(on, () => ({ exitCode: 0, stdout: '{}' }), 'main')
  const { sections } = await $.prompt.compose(INPUT)
  expect(ran).toEqual([])
  expect(sections.map((s) => s.id)).toEqual(['core'])
})

test('a triage tick (project root, branch main) gets its card from WATCHER_CARD', async ($, on) => {
  const ran = engine(on, () => ({ exitCode: 0, stdout: JSON.stringify(ISSUE) }), 'main', { WATCHER_CARD: '778' })
  const { sections } = await $.prompt.compose(INPUT)
  expect(ran).toEqual(['gh issue view 778 --json title,body,comments'])
  expect(sections.map((s) => s.id)).toEqual(['core', 'card-context:card'])
})
