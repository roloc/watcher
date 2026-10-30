// The owner-text check (card #210): a `gh issue comment` or `gh pr comment` whose body opens
// with a `Needs @<login>:` line is the Stuck ask that lands on the owner's phone, and
// docs/talking-to-the-owner.md says what shape it has. This reads that shape mechanically
// before the comment is posted and, when it breaks a rule, refuses with the rule's number and
// the offending line, so the tick's fix is one edit and a second post.
//
// Pure on purpose (`judgeOwnerText` takes a string), so the tests read every verdict off a
// sample. `FYI:` comments, and every comment that is not headed `Needs @`, are not looked at.
// What it cannot read it lets through — a body from stdin, a `$(...)` substitution, a file
// that is not there — because a nicety must never be the reason a tick cannot post.

// Does this body open (first non-empty line) with a `Needs @` ask?
export function isOwnerAsk(body: string): boolean {
  const first = body.split('\n').find((l) => l.trim() !== '')
  return first !== undefined && /Needs @/.test(first)
}

const FIRST = /^(?:🤖\s*)?\*\*Needs @[A-Za-z0-9][A-Za-z0-9-]*:\s+\S.*\*\*\s*$/u
const OPTION = /^\s*[-*]\s+\*\*([A-Z])\.\s/
const BULLET = /^\s*[-*]\s+/
const quote = (l: string) => `"${l.trim().slice(0, 100)}"`

// The first problem for each rule that fails, as "rule N: …" lines; empty when the body holds.
export function judgeOwnerText(body: string): string[] {
  const out: string[] = []
  const lines = body.replace(/\r/g, '').split('\n')
  const foldAt = lines.findIndex((l) => /<details/i.test(l))
  const above = foldAt < 0 ? lines : lines.slice(0, foldAt)
  const nonEmpty = above.filter((l) => l.trim() !== '')
  const first = nonEmpty[0] ?? ''

  const hasQuestion = above.some((l) => /^\s*\*\*Question\b/.test(l))
  const firstOk = FIRST.test(first)
  if (!firstOk) out.push(`rule 1: the first line must be the ask, bold, as **Needs @<login>: <one sentence>** (the real login, not a placeholder), got ${quote(first)}.`)

  // Rule 2: bullets are the facts above the options; the Look links and lettered options are counted by their own rules.
  const stop = above.findIndex((l) => /^\s*\*\*(Look|Question)\b/.test(l))
  const facts = (stop < 0 ? above : above.slice(0, stop)).filter((l) => BULLET.test(l) && !OPTION.test(l))
  if (facts.length > 8) out.push(`rule 2: ${facts.length} bullets above the fold, at most 8; move the rest under <details>.`)
  const prose = above.slice(1).find((l) => l.trim().length > 200 && !BULLET.test(l) && !/^\s*(Reply|\*\*)/.test(l))
  if (prose) out.push(`rule 2: bullets, not paragraphs — a long prose line above the fold: ${quote(prose)}.`)

  const qLines = above.filter((l) => /\?\s*(\*\*)?\s*$/.test(l.trim()) && !/https?:\/\/\S*\?\S*$/.test(l))
  if (hasQuestion) {
    if (qLines.length !== 1) out.push(`rule 3: exactly one question above the fold, found ${qLines.length}${qLines.length ? ': ' + qLines.map(quote).join(' and ') : ''}.`)
    const opts = above.filter((l) => OPTION.test(l))
    if (opts.length < 2) out.push(`rule 4: lettered options one line each, "- **A. …**" and at least a "- **B. …**"; found ${opts.length}.`)
    else {
      const rec = opts.filter((l) => /\(recommended\)/.test(l))
      if (rec.length !== 1) out.push(`rule 4: mark exactly one option (recommended), found ${rec.length}.`)
      else if (rec[0] !== opts[0]) out.push(`rule 4: put the recommended option first, not ${quote(rec[0])}.`)
    }
    if (!above.some((l) => /^\s*Reply\b.*\bon this card\b/.test(l))) out.push('rule 5: end above the fold with how to answer: "Reply A, B or C on this card, or say which way you lean, then move it back to **In progress**."')
  } else {
    // An access fact (rule 3) is a statement and one action: no question, no options.
    if (!firstOk) out.push('rule 3: no **Question: …?** line; a question needs one, with lettered options (an access fact is a statement and one action instead).')
    else if (qLines.length) out.push(`rule 3: an ask with no **Question:** block is an access fact, a statement with no question and no options, but this has a question: ${quote(qLines[0])}.`)
    const opt = above.find((l) => OPTION.test(l))
    if (opt) out.push(`rule 3: an access fact has no lettered options: ${quote(opt)}.`)
  }

  // Rule 8: plain words above the fold — no code, no paths. Links are not paths.
  for (const l of above) {
    const t = l.replace(/https?:\/\/\S+/g, '')
    if (t.includes('`')) { out.push(`rule 8: no code or commands above the fold (backticks); put it under <details>: ${quote(l)}.`); break }
    if (/(^|[\s(])(~|\.{1,2})?\/[\w.-]/.test(t) || /\b[A-Za-z][\w-]*\/[\w-]+\/[\w.-]+/.test(t) || /\b[A-Za-z][\w-]*\/[\w.-]*\.[a-z]{1,5}\b/.test(t)) {
      out.push(`rule 8: no file paths above the fold; say what it is in plain words and put the path under <details>: ${quote(l)}.`)
      break
    }
  }
  return out
}

// --- finding the body in a command ------------------------------------------------------

export type CommentBody = { inline: string } | { file: string }

// The body a `gh issue|pr comment` segment posts: its literal text, or the file it reads.
// undefined when this is not a comment, or the body is not something we can see.
export function commentBody(words: string[]): CommentBody | undefined {
  const pos: string[] = []
  for (let i = 1; i < words.length && pos.length < 2; i++) {
    if (words[i].startsWith('-')) { if (words[i] === '-R' || words[i] === '--repo') i++; continue }
    pos.push(words[i])
  }
  if ((pos[0] !== 'issue' && pos[0] !== 'pr') || pos[1] !== 'comment') return undefined
  for (let i = 2; i < words.length; i++) {
    const a = words[i]
    const next = words[i + 1]
    if ((a === '--body' || a === '-b') && next !== undefined) return /\$\(|`/.test(next) ? undefined : { inline: next }
    if (a.startsWith('--body=')) return { inline: a.slice(7) }
    if ((a === '--body-file' || a === '-F') && next !== undefined && next !== '-') return { file: next }
    if (a.startsWith('--body-file=')) return { file: a.slice(12) }
  }
  return undefined
}
