// The guard mod's own tests — `claude plugin test mods/guard`, offline. The kit loads the
// mod from this folder; the hooks registered here sit BENEATH it and stand for the engine:
// `tool.call` answers `{ result: {} }` (so a call the guard lets through resolves with no
// `deny`), `fs.stat` answers from the small file system below, `env.get` from mock.env.
import { expect, mock, test } from 'claude-code/testing'
import { judgeBash, segments } from './register'

const HOME = '/home/watcher'
const WORKTREE = '/home/watcher/projects/riffmap-worktrees/card-9'
const RELEASE = '/opt/watcher-releases/4a12ba7d8c90'
const STATE = `${HOME}/.local/state/watcher`

// path -> where it really lands. Anything else: ENOENT. /tmp resolves elsewhere on purpose
// (macOS: /tmp -> /private/tmp), and /opt/watcher is the deployer's pointer onto a release.
const FS: Record<string, string> = {
  [WORKTREE]: WORKTREE,
  [`${WORKTREE}/notes.md`]: `${WORKTREE}/notes.md`,
  [`${WORKTREE}/docs`]: `${WORKTREE}/docs`,
  [`${WORKTREE}/link.md`]: `${RELEASE}/commands/watch.md`,
  // The engine folds `..` before it asks: a Write to <worktree>/../x stats the folded parent.
  '/home/watcher/projects/riffmap-worktrees': '/home/watcher/projects/riffmap-worktrees',
  '/tmp': '/private/tmp',
  '/tmp/tmp.abc123': '/private/tmp/tmp.abc123',
  '/opt/watcher': RELEASE,
  '/opt/watcher/commands': `${RELEASE}/commands`,
  [`${HOME}/.claude`]: `${HOME}/.claude`,
  [`${HOME}/.claude/projects`]: `${HOME}/.claude/projects`,
  [`${HOME}/.claude/settings.json`]: `${HOME}/.claude/settings.json`,
  '/': '/',
}

type On = Parameters<Parameters<typeof test>[1] extends (...a: infer A) => unknown ? (...a: A) => unknown : never>[1]

function engine(on: On) {
  mock.env(on, { HOME, TMPDIR: '/tmp' })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: WORKTREE }))
  on('fs.stat', (_, e) => {
    // The engine asks for a folder with its trailing slash kept; the table is keyed without.
    const key = e.path.length > 1 ? e.path.replace(/\/+$/, '') : e.path
    const real = FS[key]
    if (real === undefined) throw new Error(`ENOENT: ${e.path}`)
    return { value: { kind: 'file' as const, size: 0, mtimeMs: 0, isLink: real !== e.path, realPath: real } }
  })
  const ran: string[] = []
  on('tool.call', (_, e) => { ran.push(e.tool); return { result: {} } })
  return ran
}

const ENV = { home: HOME, stateRoot: STATE }

// --- the Bash judge, by spelling: every verdict read off a string -------------------------

const REFUSED: Array<[string, RegExp]> = [
  ['git push --force origin card-9', /force-push/],
  ['git push -f origin card-9', /force-push/],
  ['git push -fu origin card-9', /force-push/],
  ['git push --force-with-lease origin card-9', /force-push/],
  ['git push --force-with-lease=card-9 origin card-9', /force-push/],
  ['git push --force-if-includes origin card-9', /force-push/],
  ['git push --mirror origin', /force-push/],
  ['git push --all origin', /force-push/],
  ['git push origin main', /push to main/],
  ['git push -u origin main', /push to main/],
  ['git push origin HEAD:main', /push to main/],
  ['git push origin +main', /force-push|push to main/],
  ['git push origin card-9:main', /push to main/],
  ['git push origin refs/heads/main', /push to main/],
  ['git push origin :main', /push to main/],
  ['git push --set-upstream origin main', /push to main/],
  ['git -C /home/w/proj push origin main', /push to main/],
  ['cd "$dir" && git push origin main', /push to main/],
  ['git fetch origin && git push origin main', /push to main/],
  ['gh pr merge 12 --admin --squash', /--admin/],
  ['gh pr merge --admin 12', /--admin/],
  ['systemctl restart watcher@riffmap', /systemctl/],
  ['sudo systemctl stop watcher@riffmap', /systemctl/],
  ['sudo -u watcher systemctl --user status', /systemctl/],
  ['/bin/systemctl daemon-reload', /systemctl/],
  ['bash deploy/rollout.sh', /deployer/],
  ['/opt/watcher/deploy/autodeploy.sh', /deployer/],
  ['sh ../watcher/deploy/rollout.sh abc123', /deployer/],
  ['source deploy/rollout.sh', /deployer/],
  ['. ./deploy/autodeploy.sh', /deployer/],
  ['git push origin +card-1', /force-push/],
  ['bash -c "git push origin main"', /push to main/],
  ["sh -c 'git push -f origin card-9'", /force-push/],
  ['eval "git push --force origin card-9"', /force-push/],
  ['gh api -X PUT repos/roloc/watcher/pulls/12/merge', /through the API/],
  ['gh api --method PUT repos/roloc/watcher/pulls/12/merge -f merge_method=squash', /through the API/],
  ['gh api -X PATCH repos/roloc/watcher/git/refs/heads/main -f sha=abc', /through the API/],
  ['gh api -X DELETE repos/roloc/watcher/branches/main/protection', /through the API/],
  ['echo x > /opt/watcher/commands/watch.md', /protected root/],
  ['echo x >> /opt/watcher-releases/4a12ba7d8c90/CLAUDE.md', /protected root/],
  ['echo x >/opt/watcher-canary/x', /protected root/],
  ['printf hi | tee /etc/watcher/env', /protected root/],
  ['printf hi | tee -a /etc/watcher/env', /protected root/],
  ['touch ~/.local/state/watcher/riffmap/PAUSE', /protected root/],
  ['touch $HOME/.local/state/watcher/riffmap/PAUSE', /protected root/],
  ['touch ${HOME}/.local/state/watcher/riffmap/PAUSE', /protected root/],
  ['touch /home/watcher/.local/state/watcher/riffmap/PAUSE', /protected root/],
  ['rm -rf /opt/watcher-canary', /protected root/],
  ['rm /home/watcher/.local/state/watcher/riffmap/logs/card-1.json', /protected root/],
  ['cp fixed.md /opt/watcher/commands/watch.md', /protected root/],
  ['mv watch.md /opt/watcher/commands/', /protected root/],
  ['ln -sfn /tmp/x /etc/watcher/env', /protected root/],
  ['chmod 600 /etc/watcher/env', /protected root/],
  ['sed -i s/a/b/ /opt/watcher/commands/watch.md', /protected root/],
  ['sed -i.bak "s/a/b/" /opt/watcher/bin/spawn.sh', /protected root/],
  ['dd if=/dev/zero of=/etc/watcher/env', /protected root/],
  ['mkdir -p ~/.local/state/watcher/new-lane', /protected root/],
  ['git -C /opt/watcher pull', /protected checkout/],
  ['git -C /opt/watcher checkout main', /protected checkout/],
  ['cd /opt/watcher && git pull --ff-only', /protected checkout/],
  ['cd /opt/watcher; git reset --hard', /protected checkout/],
  ['true && (cd /opt/watcher-canary && git fetch)', /protected checkout/],
]

const ALLOWED = [
  'git push -u origin card-204-guard',
  'git push origin card-9:card-9',
  'git push origin HEAD',
  'git push --force-with-lease origin card-9 --dry-run'.replace(' --force-with-lease', ''),
  'git push origin maintenance',
  'git push origin feature/main-page',
  'git fetch origin main && git rebase origin/main',
  'git log origin/main..HEAD',
  'gh pr merge 12 --squash --delete-branch',
  'gh pr view 12 --json state',
  'gh pr create --title t --body-file /tmp/tmp.abc123/body.md',
  'cat /opt/watcher/commands/watch.md',
  'ls -t ~/.local/state/watcher/riffmap/logs | head',
  'cat $HOME/.local/state/watcher/riffmap/logs/card-1.json',
  "journalctl -u 'watcher@*' -n 50",
  'git -C /opt/watcher log -1 --oneline',
  'git -C /opt/watcher rev-parse HEAD',
  'cd /opt/watcher && git status',
  'cp /opt/watcher/templates/board.md docs/board.md',
  'diff /etc/watcher/env.example ./env',
  'echo x > notes.txt',
  'echo x > /tmp/tmp.abc123/pr-body.md',
  'printf hi | tee out.log',
  'sed -n 1,20p /opt/watcher/bin/spawn.sh',
  'grep -rn systemctl docs/',
  'echo "never run systemctl here"',
  'bash bin/test-spawn.sh',
  'python3 /opt/watcher/bin/browse.py check http://localhost:8787/',
  'board move 87 "In review"',
  'rm -rf node_modules',
  'cat deploy/autodeploy.sh',
  'grep -n flip deploy/autodeploy.sh',
  'bash -n deploy/rollout.sh',
  'bash -n deploy/autodeploy.sh && echo ok',
  'git add deploy/autodeploy.sh bin/test-deploy.sh',
  'git diff origin/main...HEAD -- deploy/autodeploy.sh',
  'git commit -m "fix: x" -- deploy/autodeploy.sh',
  'sed -n 1,40p deploy/rollout.sh',
  'bash bin/test-deploy.sh',
  'bash -c "git push -u origin card-9"',
  'gh api repos/roloc/watcher/pulls/12/merge',
  'gh api -X DELETE repos/roloc/watcher/git/refs/heads/card-9-old',
  'gh api repos/roloc/watcher/branches/main/protection',
  'gh api -X POST repos/roloc/watcher/issues/12/comments -f body=hi',
]

test('the Bash judge refuses each hard-rule spelling with a reason that names the rule', () => {
  for (const [command, why] of REFUSED) {
    const verdict = judgeBash(command, ENV)
    expect(verdict, command).toBeDefined()
    expect(verdict, command).toMatch(why)
  }
})

test('the Bash judge lets ordinary work through', () => {
  for (const command of ALLOWED) expect(judgeBash(command, ENV), command).toBeUndefined()
})

test('WATCHER_STATE_ROOT, when set, is the state root it protects', () => {
  const r = { home: HOME, stateRoot: '/var/lib/watcher-state' }
  expect(judgeBash('touch /var/lib/watcher-state/riffmap/PAUSE', r)).toMatch(/protected root/)
  expect(judgeBash('touch ~/.local/state/watcher/riffmap/PAUSE', r)).toBeUndefined()
})

test('segments: quotes hold a word together, separators split commands, a redirect is its own word', () => {
  expect(segments(`echo "a && b" > out; git push`)).toEqual([['echo', 'a && b', '>', 'out'], ['git', 'push']])
  expect(segments(`x | tee f || y && z`)).toEqual([['x'], ['tee', 'f'], ['y'], ['z']])
  expect(segments(`echo hi >>/opt/x`)).toEqual([['echo', 'hi', '>>', '/opt/x']])
  expect(segments(`a $(b c) \`d\``)).toEqual([['a'], ['b', 'c'], ['d']])
})

// --- the hooks themselves, through the engine's own tool.call --------------------------------

test('a force-push is refused before it runs; a plain push to the branch goes through', async ($, on) => {
  const ran = engine(on)
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })

  const refused = await $.tool.call({ tool: 'Bash', command: 'git push --force origin card-9' })
  expect(refused.deny).toMatch(/^guard: never force-push/)
  expect(ran).toEqual([])

  const passed = await $.tool.call({ tool: 'Bash', command: 'git push -u origin card-9' })
  expect(passed.deny).toBeUndefined()
  expect(ran).toEqual(['Bash'])
})

test('a push to main, an admin merge, systemctl and the deploy scripts are refused', async ($, on) => {
  const ran = engine(on)
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  for (const command of ['git push origin HEAD:main', 'gh pr merge 12 --admin', 'systemctl restart watcher@riffmap', 'bash /opt/watcher/deploy/rollout.sh']) {
    const r = await $.tool.call({ tool: 'Bash', command })
    expect(r.deny, command).toMatch(/^guard: /)
  }
  expect(ran).toEqual([])
})

test('a redirect into /opt/watcher or a touch of a PAUSE file is refused; reading them is not', async ($, on) => {
  const ran = engine(on)
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  expect((await $.tool.call({ tool: 'Bash', command: 'echo x > /opt/watcher/commands/watch.md' })).deny).toMatch(/protected root/)
  expect((await $.tool.call({ tool: 'Bash', command: 'touch ~/.local/state/watcher/riffmap/PAUSE' })).deny).toMatch(/protected root/)
  expect((await $.tool.call({ tool: 'Bash', command: 'cat /opt/watcher/commands/watch.md' })).deny).toBeUndefined()
  expect(ran).toEqual(['Bash'])
})

test('Write under the worktree, under $TMPDIR and under ~/.claude goes through, even when the spelling needs resolving', async ($, on) => {
  const ran = engine(on)
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  for (const file_path of [`${WORKTREE}/notes.md`, `${WORKTREE}/docs/new.md`, '/tmp/tmp.abc123/pr-body.md', `${HOME}/.claude/projects/x.md`]) {
    const r = await $.tool.call({ tool: 'Write', file_path, content: 'x' })
    expect(r.deny, file_path).toBeUndefined()
  }
  expect(ran).toEqual(['Write', 'Write', 'Write', 'Write'])
})

test('Write and Edit outside the worktree are refused — a symlink that resolves under /opt/watcher included', async ($, on) => {
  const ran = engine(on)
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  const outside = await $.tool.call({ tool: 'Write', file_path: '/opt/watcher/commands/watch.md', content: 'x' })
  expect(outside.deny).toMatch(/outside the worktree/)
  const viaLink = await $.tool.call({ tool: 'Edit', file_path: `${WORKTREE}/link.md`, old_string: 'a', new_string: 'b' })
  expect(viaLink.deny).toMatch(new RegExp(`lands at ${RELEASE}/commands/watch.md`))
  const dotdot = await $.tool.call({ tool: 'Write', file_path: `${WORKTREE}/../other.md`, content: 'x' })
  expect(dotdot.deny).toMatch(/outside the worktree/)
  const settings = await $.tool.call({ tool: 'Write', file_path: `${HOME}/.claude/settings.json`, content: '{}' })
  expect(settings.deny).toMatch(/outside the worktree/)
  const nb = await $.tool.call({ tool: 'NotebookEdit', notebook_path: '/opt/watcher/x.ipynb', new_source: '' })
  expect(nb.deny).toMatch(/outside the worktree|cannot tell/)
  expect(ran).toEqual([])
})

test('a path the fence cannot place is refused, not let through', async ($, on) => {
  const ran = engine(on)
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  const r = await $.tool.call({ tool: 'Write', file_path: '/no/such/dir/file.md', content: 'x' })
  expect(r.deny).toMatch(/cannot tell where/)
  expect(ran).toEqual([])
})

test('a guard that throws refuses instead of falling open', async ($, on) => {
  on('env.get', () => { throw new Error('the environment is gone') })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => { throw new Error('no cwd either') })
  on('fs.stat', () => { throw new Error('ENOENT') })
  const ran: string[] = []
  on('tool.call', (_, e) => { ran.push(e.tool); return { result: {} } })
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  const r = await $.tool.call({ tool: 'Bash', command: 'ls' })
  expect(r.deny).toMatch(/guard failed, so the command was refused/)
  expect(ran).toEqual([])
})
