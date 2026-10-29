// The guard mod (card #204): a second wall behind the prose rules every tick is asked to
// obey — docs/how-it-works.md "Hard rules for every agent" and CLAUDE.md "When the watcher
// works its own board". bin/spawn.sh loads it into every tick with
// `--plugin-dir "$WATCHER_HOME/mods/guard"`, and bin/eval.sh into every replay case, so a
// tick that is about to run one of the commands below is refused before the command runs:
// the model reads the one-line reason as the tool's error and goes on. Nothing here ever
// approves, merges or moves a card — a hook only answers `{ deny }` or hands the call on.
//
// Every guard is registered with `.catch(... next.called ? next(e) : { deny })`, so a guard
// that throws or overruns its budget still refuses instead of falling open. There is no
// `$.ui` call anywhere: a tick is `claude -p`, which has no surface.
//
// What it refuses, and how it decides (best effort, by the command's spelling — the robust
// half is the Write/Edit fence, which judges where a path really lands):
//   Bash  git push with --force / -f / --force-with-lease / --force-if-includes / --mirror / --all,
//         or a + refspec
//         git push whose refspec lands on main (origin main, HEAD:main, +main, refs/heads/main, :main)
//         gh pr merge --admin; gh api with a write method at …/pulls/<n>/merge, …/git/refs/heads/main
//         or …/branches/main/protection
//         systemctl (any form, sudo included)
//         deploy/rollout.sh, deploy/autodeploy.sh when RUN (as the program, or the script given to
//         sh/bash/source) — naming them (cat, git add, bash -n, git diff) is fine
//         the string handed to `bash -c`, `sh -c` or `eval`, judged as a command of its own
//         a write by spelling under a protected root: a redirect (> >> &>) or tee into it;
//         rm/mv/cp/mkdir/touch/ln/chmod/chown/truncate/install/rsync/sed -i/dd of= on it;
//         git -C <root> (or after `cd <root> &&`) with a subcommand that writes.
//         Protected roots: /opt/watcher, /opt/watcher-releases, /opt/watcher-canary,
//         /etc/watcher, and the lane state root — WATCHER_STATE_ROOT, else ~/.local/state/watcher,
//         spelled with ~, $HOME, ${HOME} or in full.
//   Write, Edit, NotebookEdit  a file_path whose RESOLVED path (every symlink followed, `..`
//         folded) is not under the worktree (the session's cwd, resolved the same way), the
//         system temp directory ($TMPDIR, else /tmp — watch.md §1's `mktemp -d` scratch), or
//         the engine's own ~/.claude/projects and ~/.claude/plans (its transcripts, memory and
//         plans — never its settings.json or plugins/). A path that cannot be placed at all is
//         refused too: a spelling the fence cannot resolve, the tool might open.
//   Known to pass, by design of "by spelling": a path hidden in a variable (`"$STATE/PAUSE"`),
//         a program that writes a path it computes (python, perl), a heredoc into a script. The
//         Write/Edit fence is the robust half; the Bash list is the second wall's second wall.
//
// Tests: hooks/guard.test.ts — `claude plugin test mods/guard` (offline; the engine's own
// kit, `fs.stat` answered from memory). `claude plugin validate mods/guard` reads this file
// the way the engine will and lists every gating hook with its .catch.
import type { EngineInterface, Register } from 'claude-code'

type Engine = EngineInterface

const NAME = 'guard'
const FIXED_ROOTS = ['/opt/watcher', '/opt/watcher-releases', '/opt/watcher-canary', '/etc/watcher']
const SEP = '/'

// The one state this module keeps: where the worktree, the temp directory, the state root
// and the engine's folder really are. Filled at session.start (and again on a reload, which
// runs session.start again); a tool call that somehow arrives first fills it itself.
type Roots = { worktree: string; temp: string; home?: string; claudeDir?: string; stateRoot: string }
let roots: Promise<Roots> | undefined

const stripSlash = (p: string) => (p.length > 1 ? p.replace(/\/+$/, '') : p)
const under = (real: string, root: string) => real === root || real.startsWith(stripSlash(root) + SEP)

// Where a path lands, with every link followed; the spelling itself when it leads nowhere
// (a directory not made yet) — never undefined, since these are roots, not candidates.
async function resolved($: Engine, path: string): Promise<string> {
  const st = await $.fs.stat(path, { resolve: true }).catch(() => undefined)
  return stripSlash(st?.realPath ?? path)
}

function loadRoots($: Engine, cwd?: string): Promise<Roots> {
  if (!roots) {
    roots = (async () => {
      const worktree = await resolved($, cwd ?? (await $.session.cwd()))
      const home = await $.env.get('HOME')
      const temp = await resolved($, (await $.env.get('TMPDIR')) || '/tmp')
      const stateRoot = (await $.env.get('WATCHER_STATE_ROOT')) || (home ? `${home}/.local/state/watcher` : '')
      const claudeDir = home ? await resolved($, `${home}/.claude`) : undefined
      return { worktree, temp, home, claudeDir, stateRoot: stripSlash(stateRoot) }
    })()
    // A failure here is not held against the next call: it tries again.
    roots.catch(() => { roots = undefined })
  }
  return roots
}

// Where a path lands, or undefined: the file itself when it exists, else its folder plus
// the name (a Write may name a file not there yet). Unplaceable spellings first, with no
// file system call. The shape the API's own example gives.
async function placed($: Engine, path: string): Promise<string | undefined> {
  const cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  const name = path.slice(cut + 1)
  const isPlaceable = !/^[A-Za-z]:(?![\\/])/.test(path) && !/^[\\/][\\/]/.test(path) && !/^[A-Za-z]:/.test(name)
    && name !== '' && name !== '.' && name !== '..'
  if (!isPlaceable) return undefined
  const own = await $.fs.stat(path, { resolve: true }).catch(() => undefined)
  if (own) return own.realPath
  const folder = cut < 0 ? '.' : path.slice(0, cut + 1)
  const dir = await $.fs.stat(folder, { resolve: true }).catch(() => undefined)
  return dir?.realPath === undefined ? undefined : `${dir.realPath.replace(/[\\/]$/, '')}${SEP}${name}`
}

// The Write/Edit/NotebookEdit fence: the reason to refuse, or undefined to let it through.
async function fence($: Engine, path: string): Promise<string | undefined> {
  const r = await loadRoots($)
  const real = await placed($, path)
  if (real === undefined) return `cannot tell where ${path} lands, so the write was refused (a spelling the fence cannot resolve, the tool might open).`
  // Of the engine's own folder only what a session writes as it runs — its transcripts and
  // memory under projects/, a plan under plans/ — never its settings.json or plugins/,
  // which a tick could otherwise use to rewrite its own rules.
  const allowed = [r.worktree, r.temp, ...(r.claudeDir ? [`${r.claudeDir}/projects`, `${r.claudeDir}/plans`] : [])]
  if (allowed.some((root) => under(real, root))) return undefined
  return `${path} lands at ${real}, outside the worktree ${r.worktree} (and ${r.temp}). CLAUDE.md: never write outside your worktree.`
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    roots = undefined
    await loadRoots($, e.cwd).catch(() => undefined)
    return next(e)
  })

  // --- Bash: the deny list, by spelling -------------------------------------------------

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const r = await loadRoots($)
    const why = judgeBash(e.command, r)
    return why ? { deny: `${NAME}: ${why}` } : next(e)
  }).catch(($, e, next) => (next.called ? next(e) : { deny: `${NAME}: its Bash guard failed, so the command was refused.` }))

  // --- Write / Edit / NotebookEdit: the worktree fence, by where the path lands ------------

  on('tool.call', { tool: 'Write' }, async ($, e, next) => {
    const why = await fence($, e.file_path)
    return why ? { deny: `${NAME}: ${why}` } : next(e)
  }).catch(($, e, next) => (next.called ? next(e) : { deny: `${NAME}: its write fence failed, so the write was refused.` }))

  on('tool.call', { tool: 'Edit' }, async ($, e, next) => {
    const why = await fence($, e.file_path)
    return why ? { deny: `${NAME}: ${why}` } : next(e)
  }).catch(($, e, next) => (next.called ? next(e) : { deny: `${NAME}: its write fence failed, so the edit was refused.` }))

  on('tool.call', { tool: 'NotebookEdit' }, async ($, e, next) => {
    const why = await fence($, e.notebook_path)
    return why ? { deny: `${NAME}: ${why}` } : next(e)
  }).catch(($, e, next) => (next.called ? next(e) : { deny: `${NAME}: its write fence failed, so the edit was refused.` }))
}

// --- the Bash judge: pure, so the test can read every verdict off a string ---------------

// The command split into simple commands on the shell's own separators, each a list of
// words with surrounding quotes dropped. Crude on purpose: it reads spellings, it does not
// run them. `cd <dir>` in an earlier segment is remembered for the git rule.
export function segments(command: string): string[][] {
  const out: string[][] = []
  let words: string[] = []
  let word = ''
  let quote: '"' | "'" | undefined
  let had = false
  const endWord = () => { if (had) { words.push(word); word = ''; had = false } }
  const endSeg = () => { endWord(); if (words.length) out.push(words); words = [] }
  for (let i = 0; i < command.length; i++) {
    const c = command[i]
    if (quote) {
      if (c === quote) quote = undefined
      else if (c === '\\' && quote === '"' && i + 1 < command.length) { word += command[++i]; had = true }
      else { word += c; had = true }
      continue
    }
    if (c === '"' || c === "'") { quote = c; had = true; continue }
    if (c === '\\' && i + 1 < command.length) { word += command[++i]; had = true; continue }
    if (/\s/.test(c)) { endWord(); continue }
    if (c === ';' || c === '\n' || c === '(' || c === ')' || c === '`') { endSeg(); continue }
    if ((c === '&' || c === '|') && command[i + 1] === c) { endSeg(); i++; continue }
    if (c === '|') { endSeg(); continue }
    if (c === '&' && command[i + 1] !== '>') { endSeg(); continue }
    if (c === '$' && command[i + 1] === '(') { endSeg(); i++; continue }
    // A redirection operator is its own word, so the target after it is a word of its own.
    if (c === '>' || (c === '&' && command[i + 1] === '>')) {
      endWord()
      let op = c
      while (command[i + 1] === '>' || command[i + 1] === '|' || command[i + 1] === '&') op += command[++i]
      words.push(op)
      continue
    }
    word += c; had = true
  }
  endSeg()
  return out
}

const WRAPPERS = new Set(['sudo', 'command', 'exec', 'nohup', 'time', 'env', 'nice', 'ionice', 'timeout', 'flock', 'xargs', 'doas', 'setsid', 'stdbuf', 'chronic', 'caffeinate'])
const WRAPPER_FLAGS_WITH_VALUE: Record<string, Set<string>> = {
  sudo: new Set(['-u', '-g', '-C', '-D', '-h', '-p', '-r', '-t', '-U', '-T']),
  timeout: new Set(['-s', '-k', '--signal', '--kill-after']),
  flock: new Set(['-w', '-E', '--timeout', '--conflict-exit-code']),
  nice: new Set(['-n']),
}
// Drops leading env assignments and wrappers (`sudo -u watcher systemctl …` is systemctl).
function program(words: string[]): string[] {
  let w = words.slice()
  for (;;) {
    while (w.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(w[0])) w.shift()
    if (!w.length) return w
    const base = w[0].split('/').pop() ?? w[0]
    if (!WRAPPERS.has(base)) return w
    const valued = WRAPPER_FLAGS_WITH_VALUE[base]
    w.shift()
    if (base === 'timeout' || base === 'nice') { /* positional duration/adjustment below */ }
    while (w.length && w[0].startsWith('-')) {
      const f = w.shift() as string
      if (valued && valued.has(f) && w.length) w.shift()
    }
    if (base === 'timeout' && w.length && /^[0-9.]+[smhd]?$/.test(w[0])) w.shift()
    if (base === 'flock' && w.length && !/^[A-Za-z_]/.test(w[0].split('/').pop() ?? '')) w.shift()
  }
}

const GIT_WRITES = new Set(['pull', 'fetch', 'checkout', 'switch', 'reset', 'clean', 'merge', 'rebase', 'commit', 'add', 'rm', 'mv', 'stash', 'worktree', 'apply', 'cherry-pick', 'revert', 'am', 'config', 'remote', 'tag', 'branch', 'gc', 'prune', 'init', 'clone', 'submodule', 'restore', 'update-ref', 'symbolic-ref', 'filter-branch', 'reflog'])
const GIT_GLOBAL_WITH_VALUE = new Set(['-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path'])
const PUSH_FLAGS_WITH_VALUE = new Set(['-o', '--push-option', '--receive-pack', '--exec', '--repo', '--recurse-submodules', '--signed', '--force-with-lease'])
const MAIN = /^\+?(refs\/heads\/)?main$/
const WRITERS_ANY = new Set(['rm', 'mkdir', 'touch', 'chmod', 'chown', 'chgrp', 'truncate', 'rmdir', 'unlink', 'shred'])
const WRITERS_LAST = new Set(['mv', 'cp', 'ln', 'install', 'rsync', 'scp'])

export function judgeBash(command: string, r: Pick<Roots, 'home' | 'stateRoot'>): string | undefined {
  const segs = segments(command)
  const roots = [...FIXED_ROOTS, r.stateRoot].filter(Boolean)
  const expand = (w: string) => {
    let s = w
    if (r.home) s = s.replace(/^~(?=\/|$)/, r.home).replace(/^\$\{HOME\}|^\$HOME(?![A-Za-z0-9_])/, r.home)
    return s
  }
  const protectedPath = (w: string) => {
    const s = expand(w)
    return roots.some((root) => under(stripSlash(s), root))
  }
  let cwdHint = ''
  for (const words of segs) {
    const w = program(words)
    if (!w.length) continue
    const base = w[0].split('/').pop() ?? w[0]

    if (base === 'cd') { cwdHint = w[1] ? expand(w[1]) : ''; continue }

    // deploy scripts: refused when RUN — as the program, or as the script handed to a shell
    // (`bash deploy/rollout.sh`, `sh -x …`, `source …`, `. …`) — never when merely named:
    // a tick on a deploy card has to `cat`, `bash -n`, `git add` and `git diff` them.
    const DEPLOY = /(^|\/)deploy\/(rollout|autodeploy)\.sh$/
    if (DEPLOY.test(w[0])) return `${w[0]} is the deployer's, never a tick's (CLAUDE.md: never deploy/rollout.sh or deploy/autodeploy.sh).`
    if (base === 'sh' || base === 'bash' || base === 'zsh' || base === 'dash' || base === 'source' || base === '.') {
      const k = w.slice(1).findIndex((x) => !x.startsWith('-'))
      const script = k < 0 ? undefined : w[k + 1]
      // `bash -n <script>` only parses it (a syntax check, CI's own first step): not a run.
      const noexec = w.slice(1, k < 0 ? w.length : k + 1).some((x) => /^-[a-zA-Z]*n[a-zA-Z]*$/.test(x))
      if (script && !noexec && DEPLOY.test(script)) return `${script} is the deployer's, never a tick's (CLAUDE.md: never deploy/rollout.sh or deploy/autodeploy.sh).`
    }

    // a command string handed to a shell or eval is judged as a command of its own
    if (base === 'sh' || base === 'bash' || base === 'zsh' || base === 'dash') {
      const c = w.indexOf('-c')
      if (c > 0 && w[c + 1]) { const inner = judgeBash(w[c + 1], r); if (inner) return inner }
    }
    if (base === 'eval') { const inner = judgeBash(w.slice(1).join(' '), r); if (inner) return inner }

    if (base === 'systemctl') return `never systemctl from a tick (CLAUDE.md: deploying and the services are the deployer's).`

    if (base === 'gh') {
      const pos = w.slice(1).filter((x) => !x.startsWith('-'))
      if (pos[0] === 'pr' && pos[1] === 'merge' && w.includes('--admin')) return `never gh pr merge --admin (docs/how-it-works.md, hard rules: the gate is the gate).`
      if (pos[0] === 'api') {
        const writes = w.some((x, k) => (x === '-X' || x === '--method') && /^(PUT|POST|PATCH|DELETE)$/i.test(w[k + 1] ?? ''))
          || w.some((x) => /^(--method|-X)=(PUT|POST|PATCH|DELETE)$/i.test(x))
          || w.some((x) => /^-[fF]$|^--(raw-)?field$/.test(x))
        const target = pos.find((x) => /\/pulls\/\d+\/merge$|\/git\/refs\/heads\/main$|\/branches\/main\/protection/.test(x))
        if (writes && target) return `never merge or move main through the API (${target}) — docs/how-it-works.md, hard rules.`
      }
    }

    if (base === 'git') {
      // global options, then the subcommand
      let i = 1
      let dir = cwdHint
      while (i < w.length && w[i].startsWith('-')) {
        const f = w[i]
        if (GIT_GLOBAL_WITH_VALUE.has(f)) { if (f === '-C' && w[i + 1]) dir = expand(w[i + 1]); i += 2 }
        else if (f.startsWith('-C') && f.length > 2) { dir = expand(f.slice(2)); i++ }
        else i++
      }
      const sub = w[i]
      if (sub === 'push') {
        const rest = w.slice(i + 1)
        const positional: string[] = []
        for (let j = 0; j < rest.length; j++) {
          const a = rest[j]
          if (a === '--force' || a === '--force-if-includes' || a === '--mirror' || a === '--all' || a.startsWith('--force-with-lease')) return `never force-push (${a}) — docs/how-it-works.md, hard rules.`
          if (/^-[A-Za-z]+$/.test(a) && a.includes('f')) return `never force-push (${a}) — docs/how-it-works.md, hard rules.`
          if (a === '--') { positional.push(...rest.slice(j + 1)); break }
          if (a.startsWith('-')) { if (PUSH_FLAGS_WITH_VALUE.has(a) && !a.includes('=')) j++; continue }
          positional.push(a)
        }
        for (const ref of positional.slice(1)) {
          if (ref.startsWith('+')) return `never force-push (${ref}: a + refspec is a force) — docs/how-it-works.md, hard rules.`
          const dst = ref.includes(':') ? ref.slice(ref.indexOf(':') + 1) : ref
          if (MAIN.test(dst) || (ref.includes(':') && /^(refs\/heads\/)?main$/.test(dst))) return `never push to main (${ref}) — docs/how-it-works.md, hard rules: main moves only by a merged PR.`
        }
      }
      if (sub && GIT_WRITES.has(sub) && dir && protectedPath(dir)) return `git ${sub} in ${dir} writes to a protected checkout (CLAUDE.md: never write under /opt/watcher, /etc/watcher or a lane's state directory).`
    }

    // writes by spelling under a protected root
    for (let j = 0; j < w.length; j++) {
      const a = w[j]
      if (/^(&?>>?\|?|&>)$/.test(a) && w[j + 1] && protectedPath(w[j + 1])) return `a redirect into ${w[j + 1]} writes under a protected root (CLAUDE.md: never write under /opt/watcher, /etc/watcher or a lane's state directory).`
      const m = /^(>>?|&>)(\/.+|~.+|\$.+)$/.exec(a)
      if (m && protectedPath(m[1 + 1])) return `a redirect into ${m[2]} writes under a protected root (CLAUDE.md: never write under /opt/watcher, /etc/watcher or a lane's state directory).`
      const of = /^of=(.+)$/.exec(a)
      if (base === 'dd' && of && protectedPath(of[1])) return `dd ${a} writes under a protected root.`
    }
    const args = w.slice(1).filter((x) => !x.startsWith('-') && !/^(&?>>?\|?|&>)/.test(x))
    if (base === 'tee') {
      const hit = args.find(protectedPath)
      if (hit) return `tee into ${hit} writes under a protected root (CLAUDE.md: never write under /opt/watcher, /etc/watcher or a lane's state directory).`
    }
    if (WRITERS_ANY.has(base)) {
      const hit = args.find(protectedPath)
      if (hit) return `${base} ${hit} changes a protected root (CLAUDE.md: never write under /opt/watcher, /etc/watcher or a lane's state directory).`
    }
    if (WRITERS_LAST.has(base) && args.length) {
      const last = args[args.length - 1]
      if (protectedPath(last)) return `${base} into ${last} writes under a protected root (CLAUDE.md: never write under /opt/watcher, /etc/watcher or a lane's state directory).`
    }
    if (base === 'sed' && w.some((x) => x === '-i' || /^-i\S*$/.test(x) || x === '--in-place' || x.startsWith('--in-place='))) {
      const hit = args.find(protectedPath)
      if (hit) return `sed -i on ${hit} edits a protected root (CLAUDE.md: never write under /opt/watcher, /etc/watcher or a lane's state directory).`
    }
  }
  return undefined
}
