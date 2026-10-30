// The browse mod (card #215): `bin/browse.py`'s three commands as one registered tool, so a tick
// or a desktop session looks at a page by calling a tool with a schema instead of remembering a
// shell line. watcher #71 (October 1, 2026): `curl` where `browse.py check` was the rule. The tool
// is a shim: it runs `python3 "$WATCHER_HOME/bin/browse.py" <command> <url> [out]` and hands back
// what that printed. browse.py is unchanged and its command line stays, so a project's own tick
// keeps working without this mod.
//
//   session.start   registers `browse` (listed to the model as `mcp__browse__browse`).
//   tool.call       runs browse.py; a non-zero exit comes back as text — the exit code, then
//                   what browse.py printed — never a crash. `shot` returns the PNG's path to `Read`.
//
// $WATCHER_HOME is the box's own variable (/opt/watcher); unset, the tool says so rather than guess.
// Tests: hooks/register.test.ts — `claude plugin test mods/browse`.
import type { EngineInterface, Register } from 'claude-code'

type Engine = EngineInterface

const NAME = 'browse'
const RUN_MS = 120_000
const COMMANDS = ['check', 'text', 'shot'] as const
type Command = (typeof COMMANDS)[number]

export const DESCRIPTION =
  "Look at a web page in a real headless browser, the way its reader does — use this, never curl, for any page that draws itself with a script. " +
  "command=check: loads the url and reports every error the page's own code or its requests raised (exit status says pass or fail); use it first, and before saying a page 'loads fine'. " +
  "command=text: the text a reader sees after the scripts ran. " +
  "command=shot: writes a PNG to `out` (an absolute path in your scratch directory) and returns that path; Read it to see the page. " +
  "url is anything this box can reach: a live page, a local server you started in the same Bash call, or file:///… . " +
  "wait is an optional JS expression to wait for before looking."

export const INPUT_SCHEMA = {
  type: 'object',
  properties: {
    command: { type: 'string', enum: [...COMMANDS], description: 'check, text or shot' },
    url: { type: 'string', description: 'http(s):// or file:// URL' },
    out: { type: 'string', description: 'shot only: where to write the PNG (absolute path)' },
    wait: { type: 'string', description: 'optional JS expression to wait for before looking' },
  },
  required: ['command', 'url'],
}

type Args = { command?: unknown; url?: unknown; out?: unknown; wait?: unknown }

// The argv for one call, or the sentence saying why there is none.
export function argvFor(home: string, a: Args): string[] | string {
  if (!COMMANDS.includes(a.command as Command)) return `browse: command must be one of ${COMMANDS.join(', ')}`
  if (typeof a.url !== 'string' || !a.url || a.url.startsWith('-')) return 'browse: url must be a URL, not a flag'
  const argv = ['python3', `${home}/bin/browse.py`, a.command as string, a.url]
  if (a.command === 'shot') {
    if (typeof a.out !== 'string' || !a.out || a.out.startsWith('-')) return 'browse: shot needs out, the path of the PNG to write'
    argv.push(a.out)
  }
  if (typeof a.wait === 'string' && a.wait) argv.push('--wait', a.wait)
  return argv
}

// What the model reads back.
export function render(a: Args, r: { exitCode: number; stdout: string; stderr: string }): string {
  const printed = [r.stdout.trim(), r.stderr.trim()].filter(Boolean).join('\n')
  if (r.exitCode !== 0) return `browse ${a.command} failed (exit ${r.exitCode}):\n${printed}`
  if (a.command === 'shot') return `${a.out}\n${printed}`.trim()
  return printed || 'ok: no errors'
}

export const register: Register = (on) => {
  on('session.start', async ($: Engine, e, next) => {
    await $.tool.register({ name: NAME, description: DESCRIPTION, inputSchema: INPUT_SCHEMA, isDeferred: false })
    return next(e)
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: `mcp__${NAME}__${NAME}` }, async ($: Engine, e) => {
    const home = (await $.env.get('WATCHER_HOME'))?.trim()
    if (!home) return { result: 'browse: WATCHER_HOME is not set, so bin/browse.py cannot be found' }
    const argv = argvFor(home, e as Args)
    if (typeof argv === 'string') return { result: argv }
    try {
      return { result: render(e as Args, await $.process.run(argv, { timeoutMs: RUN_MS })) }
    } catch (err) {
      return { result: `browse ${e.command} failed: ${err instanceof Error ? err.message : String(err)}` }
    }
  }).catch(($, e, next) => ({ result: 'browse: the tool failed before it could run' }))
}
