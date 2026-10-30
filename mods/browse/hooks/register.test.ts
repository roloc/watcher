// Card #215: the browse mod — `claude plugin test mods/browse`, offline. `process.run` is
// answered from memory (a stub browse.py) and every argv it was asked is kept.
import { expect, mock, test } from 'claude-code/testing'
import { argvFor, render } from './register'

type On = Parameters<Parameters<typeof test>[1] extends (...a: infer A) => unknown ? (...a: A) => unknown : never>[1]

const HOME = '/opt/watcher'
const BROWSE = `${HOME}/bin/browse.py`
const TOOL = 'mcp__browse__browse'

const registered: { name: string; description: string; inputSchema?: any; isDeferred?: boolean }[] = []

function engine(on: On, run: (argv: string[]) => { exitCode: number; stdout: string; stderr: string }, env: Record<string, string> = { WATCHER_HOME: HOME }) {
  mock.env(on, env)
  const ran: string[][] = []
  on('process.run', (_, e) => {
    ran.push([...e.argv])
    return { value: { ...run(e.argv), isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('tool.register', (_, e) => {
    registered.push(e)
    return { value: { tool: `mcp__browse__${e.name}` } }
  })
  return ran
}

test('each command maps to the right argv', () => {
  expect(argvFor(HOME, { command: 'check', url: 'http://localhost:8787/' })).toEqual(['python3', BROWSE, 'check', 'http://localhost:8787/'])
  expect(argvFor(HOME, { command: 'text', url: 'file:///x.html', wait: 'window.ready' })).toEqual(['python3', BROWSE, 'text', 'file:///x.html', '--wait', 'window.ready'])
  expect(argvFor(HOME, { command: 'shot', url: 'http://h/', out: '/tmp/a/p.png' })).toEqual(['python3', BROWSE, 'shot', 'http://h/', '/tmp/a/p.png'])
})

test('a call that cannot be a browse.py line says why instead', () => {
  expect(argvFor(HOME, { command: 'curl', url: 'http://h/' })).toMatch(/command must be/)
  expect(argvFor(HOME, { command: 'check' })).toMatch(/url/)
  expect(argvFor(HOME, { command: 'check', url: '--wait' })).toMatch(/not a flag/)
  expect(argvFor(HOME, { command: 'shot', url: 'http://h/' })).toMatch(/needs out/)
})

test('render: output on success, exit code and error text on failure', () => {
  expect(render({ command: 'text' }, { exitCode: 0, stdout: 'hello\n', stderr: '' })).toBe('hello')
  expect(render({ command: 'check' }, { exitCode: 0, stdout: '', stderr: '' })).toBe('ok: no errors')
  expect(render({ command: 'shot', out: '/tmp/p.png' }, { exitCode: 0, stdout: '', stderr: '' })).toBe('/tmp/p.png')
  expect(render({ command: 'check' }, { exitCode: 1, stdout: '', stderr: 'page error: SyntaxError' })).toBe('browse check failed (exit 1):\npage error: SyntaxError')
})

test('session.start registers the tool', async ($, on) => {
  engine(on, () => ({ exitCode: 0, stdout: '', stderr: '' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  expect(registered.map((t) => t.name)).toEqual(['browse'])
  expect(registered[0].inputSchema.properties.command.enum).toEqual(['check', 'text', 'shot'])
  expect(registered[0].inputSchema.required).toEqual(['command', 'url'])
  expect(registered[0].isDeferred).toBe(false)
})

test('a tool call runs browse.py and returns its output', async ($, on) => {
  const ran = engine(on, () => ({ exitCode: 0, stdout: 'Hello reader', stderr: '' }))
  const r = await $.tool.call({ tool: TOOL, tool_use_id: 't1', command: 'text', url: 'http://h/' })
  expect(ran).toEqual([['python3', BROWSE, 'text', 'http://h/']])
  expect(r.result).toBe('Hello reader')
})

test('a failing browse.py comes back as text, not a crash', async ($, on) => {
  engine(on, () => ({ exitCode: 1, stdout: '', stderr: 'page error: Uncaught SyntaxError' }))
  const r = await $.tool.call({ tool: TOOL, tool_use_id: 't2', command: 'check', url: 'http://h/' })
  expect(r.result).toBe('browse check failed (exit 1):\npage error: Uncaught SyntaxError')
})

test('no WATCHER_HOME: the tool says so and runs nothing', async ($, on) => {
  const ran = engine(on, () => ({ exitCode: 0, stdout: '', stderr: '' }), {})
  const r = await $.tool.call({ tool: TOOL, tool_use_id: 't3', command: 'check', url: 'http://h/' })
  expect(ran).toEqual([])
  expect(String(r.result)).toMatch(/WATCHER_HOME/)
})
