// Card #214: the owner-pane mod — `claude plugin test mods/owner-pane`, offline. The floor page is
// a fixture state.json, `gh` and `board.py` are answered from memory, and every command is kept.
import { expect, test } from 'claude-code/testing'
import { STATE } from './fixtures/state'
import { fromState, oneLine, parseDraft } from './model'

const PANE = {
  plugin: 'owner-pane',
  component: 'Pane',
  requestId: 'owner-pane',
  props: { title: 'Watcher', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const
const R = { stderr: '', isStdoutTruncated: false, isStderrTruncated: false }

const DRAFT = 'TITLE: Show lane cost on the floor page\n---\n**Ask:** show it.\n\n## Why now\nBecause.\n\nReplaces: nothing\n'

function engine(on: any, floorOk = true, ghFails = false) {
  const ran: { cmd: string; stdin?: string }[] = []
  on('command.register', () => ({ value: { command: 'x' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('clock.every', () => ({ value: () => undefined }))
  on('session.start', (_: unknown, e: any) => ({ cwd: e.cwd }))
  on('env.get', () => ({ value: '/home/me' }))
  on('http.fetch', () => ({ value: floorOk ? { status: 200, ok: true, headers: {}, text: STATE } : { status: 503, ok: false, headers: {}, text: '' } }))
  on('model.complete', () => ({ value: { isAnswered: true, text: DRAFT, usage: { input_tokens: 1, output_tokens: 1 } } }))
  on('process.run', (_: unknown, e: any) => {
    const cmd = e.argv.join(' ')
    ran.push({ cmd, stdin: e.init?.stdin })
    if (ghFails) return { value: { exitCode: 1, stdout: '', stderr: 'boom', ...R } }
    if (cmd.startsWith('gh issue create')) return { value: { exitCode: 0, stdout: 'https://github.com/roloc/watcher/issues/300\n', ...R } }
    if (cmd.includes('--json projectItems')) return { value: { exitCode: 0, stdout: 'Backlog\n', ...R } }
    if (cmd.startsWith('gh issue view')) return { value: { exitCode: 0, stdout: JSON.stringify({ title: 'A card', state: 'OPEN', labels: [{ name: 'in-flight' }], projectItems: [{ status: { name: 'Stuck - Needs help' } }], comments: [{ author: { login: 'bot' }, createdAt: '2026-10-30T10:00:00Z', body: 'old' }, { author: { login: 'roloc' }, createdAt: '2026-10-30T12:00:00Z', body: 'newest one' }] }), ...R } }
    if (cmd.startsWith('gh pr list')) return { value: { exitCode: 0, stdout: '[{"number":9,"state":"MERGED","url":"https://github.com/roloc/watcher/pull/9"}]', ...R } }
    return { value: { exitCode: 0, stdout: '', ...R } }
  })
  return ran
}

test('the helpers cut the floor page down to rows', () => {
  const s = fromState(JSON.parse(STATE), 'now')
  expect(s.stuck).toEqual([{ project: 'watcher', number: 213, url: 'https://github.com/roloc/watcher/issues/213', ask: 'is the note at tick start enough?' }])
  expect(s.lanes.map((l) => l.pill)).toEqual(['needs_steve', 'idle'])
  expect(s.ship).toBe('/opt/watcher runs b6a9eb8; deploy held at debdfad')
  expect(oneLine('**Needs @roloc: a b?**')).toBe('a b?')
  expect(parseDraft('no title', 'r/x')).toBeUndefined()
})

test('the pane lists the Stuck card, the lanes and the deploy line, on every surface', async ($, on) => {
  engine(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /#213 is the note/ }))?.text).toContain('#213')
    expect(await ui.find({ type: 'Text', text: /watcher: needs you #213/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /riffmap: idle/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /deploy held at debdfad/ })).toBeTruthy()
    await ui.unmount()
  }
})

test('an unreachable floor page says so in the pane', async ($, on) => {
  engine(on, false)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /Floor page unreachable: HTTP 503/ })).toBeTruthy()
  await ui.unmount()
})

test('/stuck lists the question; /stuck <n> <letter> posts the comment and then the move', async ($, on) => {
  const ran = engine(on)
  const list = await $.command.run({ command: 'stuck', args: '' })
  expect(list.text).toContain('#213 (watcher): is the note at tick start enough?')
  expect(ran).toEqual([])
  const done = await $.command.run({ command: 'stuck', args: '213 A' })
  expect(done.text).toBe('Posted on #213 and moved it to In progress.')
  expect(ran[0].cmd).toBe('gh issue comment 213 -R roloc/watcher --body A')
  expect(ran[1].cmd).toContain('/home/me/projects/watcher/projects/watcher.env')
  expect(ran[1].cmd).toContain('/home/me/projects/watcher/bin/board.py 213')
  expect(ran.length).toBe(2)
})

test('/stuck refuses a card that is not Stuck', async ($, on) => {
  const ran = engine(on)
  const no = await $.command.run({ command: 'stuck', args: '999 A' })
  expect(no.text).toContain('not one of the cards')
  expect(ran).toEqual([])
})

test('/card prints the title, column, newest comment first and the PR', async ($, on) => {
  engine(on)
  const r = await $.command.run({ command: 'card', args: '214' })
  expect(r.text).toContain('#214 A card')
  expect(r.text).toContain('Column: Stuck - Needs help')
  expect(r.text).toContain('newest one')
  expect(r.text).not.toContain('old')
  expect(r.text).toContain('PR: #9 MERGED')
})

test('/file-card drafts first and files only after a yes', async ($, on) => {
  const ran = engine(on)
  const d = await $.command.run({ command: 'file-card', args: 'show lane cost on the floor page' })
  expect(d.text).toContain('nothing filed yet')
  expect(ran).toEqual([])
  const f = await $.command.run({ command: 'file-card', args: 'yes' })
  expect(f.text).toBe('Filed https://github.com/roloc/watcher/issues/300: on the board in Backlog.')
  expect(ran[0].cmd).toContain('gh issue create -R roloc/watcher --title Show lane cost on the floor page')
  expect(ran[0].stdin).toContain('**Ask:** show it.')
  expect((await $.command.run({ command: 'file-card', args: 'yes' })).text).toContain('no draft waiting')
})

test('/file-card no files nothing', async ($, on) => {
  const ran = engine(on)
  await $.command.run({ command: 'file-card', args: 'something' })
  expect((await $.command.run({ command: 'file-card', args: 'no' })).text).toContain('Nothing was filed')
  expect(ran).toEqual([])
})

test('a comment that fails to post leaves the card where it is', async ($, on) => {
  const ran = engine(on, true, true)
  const bad = await $.command.run({ command: 'stuck', args: '213 A' })
  expect(bad.text).toContain('the card was not moved')
  expect(ran.length).toBe(1)
})
