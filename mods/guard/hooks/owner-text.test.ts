// Card #210: the owner-text check — `claude plugin test mods/guard`, offline. The samples are
// the ones docs/talking-to-the-owner.md carries: its template, its "Before" and its "After".
import { expect, mock, test } from 'claude-code/testing'
import { commentBody, judgeOwnerText } from './owner-text'
import { segments } from './register'

const TEMPLATE = `🤖 **Needs @alex: pick where speaker names live in the meeting summary.**

- Card: add speaker names to the meeting summary.
- Done so far: the code change is ready either way.
- Blocker: one option changes a format other tools rely on. That is your call, not mine.

**Look:** (only when there is something to open)
- The mockup: https://example.com/mockups/a.html?v=2
- The PR: https://github.com/roloc/watcher/pull/1

**Question: where should speaker names live?**
- **A. Inside the summary format** (recommended) — one source of truth; changes a shared format.
- **B. Worked out at display time** — no format change; other tools never see the names.
- **C. In a separate file** — no format change; two files to keep in sync.

Reply A, B or C on this card, or say which way you lean, then move it back to **In progress**.

<details><summary>Details</summary>

- Branch \`card-87\`, no PR yet.
- A touches the \`MeetingMap\` JSON contract and 4 fixtures under \`test/fixtures/\`.

</details>`

const BEFORE = `Needs input. I looked into the transcript ingestion path and there are a couple of ways
this could go: we could either extend the existing MeetingMap schema with an optional
speakers array, which would require touching the JSON contract and the four fixtures
under test/fixtures, or we could keep the schema as-is and derive speakers at render time,
which is cheaper but loses the information for downstream consumers. There's also the
option of a separate sidecar file. Let me know how you'd like to proceed.`

const ACCESS = `🤖 **Needs @alex: paste the share link for the meeting.**

- Card: write up the call from last Friday.
- Blocker: the page asks for a Google login the box does not have.

Press Share at the top of the meeting page and paste the link here, then move this card back to **In progress**.

<details><summary>Details</summary>

- Exact error: 401 from the page.

</details>`

test('the template in docs/talking-to-the-owner.md passes', () => {
  expect(judgeOwnerText(TEMPLATE)).toEqual([])
})

test('the "Before" example is refused with rule 1 and rule 3', () => {
  const out = judgeOwnerText(BEFORE).join('\n')
  expect(out).toMatch(/rule 1: .*"Needs input\. I looked/)
  expect(out).toMatch(/rule 3:/)
})

test('an access-fact ask with no question and no options passes; a stray option in it does not', () => {
  expect(judgeOwnerText(ACCESS)).toEqual([])
  expect(judgeOwnerText(ACCESS.replace('Press Share', '- **A. paste a summary yourself**\nPress Share')).join('\n')).toMatch(/rule 3: an access fact has no lettered options/)
})

test('each numbered rule names itself and quotes the line', () => {
  const bullets = Array.from({ length: 9 }, (_, i) => `- fact ${i}`).join('\n')
  expect(judgeOwnerText(TEMPLATE.replace('- Card: add', `${bullets}\n- Card: add`)).join('\n')).toMatch(/rule 2: 12 bullets/)
  expect(judgeOwnerText(TEMPLATE.replace('**Question: where should speaker names live?**', '**Question: where? and when?**\n- Is this fine?')).join('\n')).toMatch(/rule 3: exactly one question above the fold, found 2/)
  expect(judgeOwnerText(TEMPLATE.replace(' (recommended)', '')).join('\n')).toMatch(/rule 4: mark exactly one/)
  expect(judgeOwnerText(TEMPLATE.replace('- **A. Inside the summary format** (recommended)', '- **A. Inside the summary format**').replace('- **B. Worked out at display time**', '- **B. Worked out at display time** (recommended)')).join('\n')).toMatch(/rule 4: put the recommended option first/)
  expect(judgeOwnerText(TEMPLATE.replace(/^Reply.*\n/m, '')).join('\n')).toMatch(/rule 5/)
  expect(judgeOwnerText(TEMPLATE.replace('4 fixtures', '4 fixtures').replace('- Blocker: one', '- Blocker: `verify` fails in one')).join('\n')).toMatch(/rule 8: no code .*"- Blocker: `verify`/)
  expect(judgeOwnerText(TEMPLATE.replace('- Card: add', '- Card: edit bin/spawn.sh to add')).join('\n')).toMatch(/rule 8: no file paths/)
  expect(judgeOwnerText(TEMPLATE.replace('🤖 **Needs @alex', '🤖 **Needs @<login>')).join('\n')).toMatch(/rule 1/)
})

test('commentBody finds the literal body or the file, and ignores what it cannot see', () => {
  const body = (c: string) => commentBody(segments(c)[0])
  expect(body('gh issue comment 5 --body "hello there"')).toEqual({ inline: 'hello there' })
  expect(body('gh pr comment 5 -R roloc/watcher --body-file "$scratch/stuck.md"')).toEqual({ file: '$scratch/stuck.md' })
  expect(body('gh issue comment 5 -F out.md')).toEqual({ file: 'out.md' })
  expect(body('gh issue comment 5 --body-file -')).toBeUndefined()
  expect(body('gh issue comment 5 --body "$(cat x.md)"')).toBeUndefined()
  expect(body('gh issue edit 5 --body "Needs @a"')).toBeUndefined()
})

const WORKTREE = '/home/watcher/projects/watcher-worktrees/card-9'
function engine(on: Parameters<typeof mock.env>[0], files: Record<string, string>) {
  mock.env(on, { HOME: '/home/watcher', TMPDIR: '/tmp' })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: WORKTREE }))
  on('fs.stat', (_, e) => {
    if (e.path in files || e.path === '/tmp' || e.path === WORKTREE) return { value: { kind: 'file' as const, size: 0, mtimeMs: e.path.includes('new') ? 2 : 1, isLink: false, realPath: e.path } }
    throw new Error(`ENOENT: ${e.path}`)
  })
  on('fs.list', () => ({ value: [{ name: 'tmp.old', kind: 'directory' as const, size: 0, mtimeMs: 0, isLink: false }, { name: 'tmp.new', kind: 'directory' as const, size: 0, mtimeMs: 0, isLink: false }] }))
  on('fs.read', (_, e) => {
    if (e.path in files) return { value: files[e.path] }
    throw new Error(`ENOENT: ${e.path}`)
  })
  on('tool.call', () => ({ result: {} }))
}

test('a Needs-@ comment that breaks the rules is refused before gh runs; the fixed one, FYIs and other comments pass', async ($, on) => {
  engine(on, { '/tmp/tmp.old/stuck.md': BEFORE, '/tmp/tmp.new/stuck.md': TEMPLATE, '/tmp/tmp.bad/body.md': 'Needs @alex: what?' })
  await $.session.start({ cwd: WORKTREE, surface: null, isInteractive: false })
  const run = (command: string) => $.tool.call({ tool: 'Bash', command })
  // the newest scratch folder's stuck.md is the one read for an unexpanded $scratch
  expect((await run('gh issue comment 9 --body-file "$scratch/stuck.md"')).deny).toBeUndefined()
  const bad = await run('gh issue comment 9 --body-file /tmp/tmp.bad/body.md')
  expect(bad.deny).toMatch(/guard: this comment breaks docs\/talking-to-the-owner\.md.*rule 1/)
  expect((await run('gh pr comment 9 --body "Needs @alex: just say go"')).deny).toMatch(/rule 1/)
  expect((await run('gh issue comment 9 --body "🤖 **FYI:** plan for this card."')).deny).toBeUndefined()
  expect((await run('gh issue comment 9 --body "Needs input"')).deny).toBeUndefined()
  expect((await run('gh issue comment 9 --body-file /tmp/tmp.bad/missing.md')).deny).toBeUndefined()
  expect((await run('gh issue comment 9 --body-file -')).deny).toBeUndefined()
})
