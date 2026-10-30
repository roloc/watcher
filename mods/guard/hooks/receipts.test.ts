// Card #211: the receipts mod — `claude plugin test mods/guard`, offline. `process.run` is
// answered from memory and every command it was asked is kept, so each test reads off what
// the hook ran: the Stage set, the comment posted, the rerun.
import { expect, mock, test } from 'claude-code/testing'
import { cardFromPrompt, prFromCreate, receiptLine } from './receipts'

const HOME = '/home/watcher'
const WORKTREE = '/home/watcher/projects/watcher-worktrees/card-211'
const SHA = '0123456789abcdef0123456789abcdef01234567'
const CLEAN = `Reviewed ${SHA}: no blocking findings`
const BLOCKING = `Reviewed ${SHA}: 1 blocking finding`
const USABILITY = `Usability ${SHA}: no blocking findings`

type On = Parameters<Parameters<typeof test>[1] extends (...a: infer A) => unknown ? (...a: A) => unknown : never>[1]

// What each tool gives back, and how `process.run` answers: by the first words of the command.
function engine(on: On, opts: { failRun?: boolean } = {}) {
  mock.env(on, { HOME, TMPDIR: '/tmp' })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: WORKTREE }))
  on('fs.stat', (_, e) => ({ value: { kind: 'file' as const, size: 0, mtimeMs: 0, isLink: false, realPath: e.path } }))
  const ran: string[] = []
  on('process.run', (_, e) => {
    const cmd = e.argv.join(' ')
    ran.push(cmd)
    if (opts.failRun) throw new Error('cannot start')
    const out = cmd.startsWith('git rev-parse') ? 'card-211'
      : cmd.startsWith('gh run list') ? '777'
      : cmd.startsWith('gh pr view') ? '55'
      : ''
    return { value: { exitCode: 0, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return ran
}

const withResult = (on: On, result: unknown) => on('tool.call', () => ({ result }))
const agentResult = (text: string) => ({ content: [{ type: 'text', text }] })
const reviewer = (prompt = 'review it') => ({ tool: 'Agent' as const, description: 'review', prompt, subagent_type: 'watcher:reviewer' })

test('the helpers read the card, the PR and the receipt line', () => {
  expect(cardFromPrompt('/watch 211')).toBe('211')
  expect(cardFromPrompt('/watcher:watch #12')).toBe('12')
  expect(cardFromPrompt('please /watch 211')).toBeUndefined()
  expect(prFromCreate('https://github.com/roloc/watcher/pull/230\n')).toBe('230')
  expect(prFromCreate('nothing')).toBeUndefined()
  expect(receiptLine(`${CLEAN}\n\nmore`, 'reviewer')).toBe(CLEAN)
  expect(receiptLine(`Reviewed abc: no blocking findings`, 'reviewer')).toBeUndefined()
  expect(receiptLine(CLEAN, 'usability')).toBeUndefined()
  expect(receiptLine(USABILITY, 'usability')).toBe(USABILITY)
})

test('a gh pr create that succeeded sets the Stage PR open', async ($, on) => {
  const ran = engine(on)
  withResult(on, { stdout: 'https://github.com/roloc/watcher/pull/230\n', stderr: '' })
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  await $.prompt.submit({ text: '/watch 211' }).catch(() => undefined)
  const r = await $.tool.call({ tool: 'Bash', command: 'gh pr create --title t --body-file b.md' })
  expect(r.deny).toBeUndefined()
  expect(ran.filter((c) => c.startsWith('board'))).toEqual(['board stage 211 PR open'])
})

test('a gh pr create that printed no PR URL sets nothing', async ($, on) => {
  const ran = engine(on)
  withResult(on, { stdout: 'warning: nothing\n', stderr: '' })
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  await $.tool.call({ tool: 'Bash', command: 'gh pr create --fill' })
  expect(ran.filter((c) => c.startsWith('board'))).toEqual([])
})

test('a clean reviewer line is posted verbatim, then the failed checks are rerun', async ($, on) => {
  const ran = engine(on)
  withResult(on, agentResult(`${CLEAN}\n\nNothing else.`))
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  const r = await $.tool.call(reviewer())
  expect(r.deny).toBeUndefined()
  expect(ran).toContain(`gh pr comment 55 --body 🤖 reviewer: ${CLEAN}`)
  expect(ran).toContain('gh run list --branch card-211 --workflow CI --limit 1 --json databaseId --jq .[0].databaseId')
  expect(ran).toContain('gh run rerun 777 --failed')
})

test('a blocking reviewer line is posted too, as watch.md says', async ($, on) => {
  const ran = engine(on)
  withResult(on, agentResult(`${BLOCKING}\n\n- x`))
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  await $.tool.call(reviewer())
  expect(ran).toContain(`gh pr comment 55 --body 🤖 reviewer: ${BLOCKING}`)
  expect(ran).toContain('gh run rerun 777 --failed')
})

test('text that is not a receipt line is never posted', async ($, on) => {
  const ran = engine(on)
  withResult(on, agentResult('Looks fine to me.'))
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  await $.tool.call(reviewer())
  expect(ran.filter((c) => c.startsWith('gh pr comment') || c.startsWith('gh run'))).toEqual([])
})

test('a usability line from before the PR exists is held and posted when the PR opens', async ($, on) => {
  const ran: string[] = []
  mock.env(on, { HOME, TMPDIR: '/tmp' })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: WORKTREE }))
  on('fs.stat', (_, e) => ({ value: { kind: 'file' as const, size: 0, mtimeMs: 0, isLink: false, realPath: e.path } }))
  on('process.run', (_, e) => {
    const cmd = e.argv.join(' ')
    ran.push(cmd)
    // no PR yet: `gh pr view` fails
    const bad = cmd.startsWith('gh pr view')
    return { value: { exitCode: bad ? 1 : 0, stdout: cmd.startsWith('git rev-parse') ? 'card-211' : '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('tool.call', (_, e) => ({ result: e.tool === 'Agent' ? agentResult(USABILITY) : { stdout: 'https://github.com/roloc/watcher/pull/230\n', stderr: '' } }))
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  await $.tool.call({ tool: 'Agent', description: 'u', prompt: 'p', subagent_type: 'watcher:usability' })
  expect(ran.filter((c) => c.startsWith('gh pr comment'))).toEqual([])
  await $.tool.call({ tool: 'Bash', command: 'gh pr create --fill' })
  expect(ran).toContain(`gh pr comment 230 --body 🤖 usability: ${USABILITY}`)
})

test('a gh pr merge that succeeded sets the Stage Merging', async ($, on) => {
  const ran = engine(on)
  withResult(on, { stdout: '', stderr: '' })
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  await $.tool.call({ tool: 'Bash', command: 'gh pr merge --squash --delete-branch' })
  expect(ran.filter((c) => c.startsWith('board'))).toEqual(['board stage 211 Merging'])
})

test('a hook that cannot run its commands leaves the tool result untouched', async ($, on) => {
  const ran = engine(on, { failRun: true })
  withResult(on, agentResult(CLEAN))
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  const r = await $.tool.call(reviewer())
  expect(r.deny).toBeUndefined()
  expect(r.result).toEqual(agentResult(CLEAN))
  const b = await $.tool.call({ tool: 'Bash', command: 'gh pr merge --squash' })
  expect(b.deny).toBeUndefined()
  expect(ran.length).toBeGreaterThan(0)
})

test('a failed or other command is left alone', async ($, on) => {
  const ran = engine(on)
  on('tool.call', () => ({ result: { stdout: '', stderr: 'boom' }, isError: true }))
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  await $.tool.call({ tool: 'Bash', command: 'gh pr create --fill' })
  await $.tool.call({ tool: 'Agent', description: 'x', prompt: 'p', subagent_type: 'Explore' })
  expect(ran).toEqual([])
})
