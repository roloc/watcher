// Card #214: the owner-pane mod's pure half — what /api/state.json becomes, and the text of each
// command. No engine in here, so the tests read it without one.
import type { Draft, LaneRow, Snapshot, StuckRow } from '../types'

export const EMPTY: Snapshot = { at: '', error: '', ship: '', stuck: [], lanes: [] }

const PILL: Record<string, string> = {
  working: 'working',
  idle: 'idle',
  needs_steve: 'needs you',
}
export const pillText = (p: string) => PILL[p] ?? p.replace(/_/g, ' ')

type Json = Record<string, any>

// The floor page's own JSON, cut down to the rows the pane draws.
export function fromState(state: Json, at: string): Snapshot {
  const stuck: StuckRow[] = []
  const lanes: LaneRow[] = []
  let ship = ''
  for (const p of (state.projects ?? []) as Json[]) {
    for (const s of (p.stuck ?? []) as Json[]) {
      stuck.push({ project: p.name, number: s.card?.number, url: s.card?.url ?? '', ask: oneLine(s.ask ?? '') })
    }
    for (const l of (p.lanes ?? []) as Json[]) {
      lanes.push({ project: p.name, lane: l.lane, pill: l.pill ?? '', card: l.card?.number, reason: l.reason ?? '' })
    }
    const d = p.ship?.deploy
    if (!ship && d?.live) ship = `/opt/watcher runs ${d.live}${d.state && d.state !== 'live' ? `; deploy ${d.state}${d.target ? ` at ${d.target}` : ''}` : ''}`
  }
  return { at, error: '', ship, stuck, lanes }
}

// `**Needs @roloc: the ask?**` -> `the ask?`
export function oneLine(ask: string): string {
  return ask.replace(/\*\*/g, '').replace(/^\s*Needs\s+@?\w[\w-]*:\s*/i, '').replace(/\s+/g, ' ').trim()
}

export const laneLine = (l: LaneRow) =>
  `${l.lane}: ${pillText(l.pill)}${l.card ? ` #${l.card}` : ''}`

export function stuckText(s: Snapshot): string {
  if (!s.stuck.length) return s.at ? 'Nothing is waiting on you.' : 'No reading of the floor page yet.'
  const rows = s.stuck.map((c) => `#${c.number} (${c.project}): ${c.ask || '(no question posted yet)'}`)
  return [...rows, '', 'Answer with `/stuck <n> <your reply>` — a letter is enough. It posts on the card and moves it back to In progress.'].join('\n')
}

// owner/repo from a card's URL.
export const repoOf = (url: string) => /github\.com\/([^/]+\/[^/]+)\/issues\//.exec(url)?.[1]

// The env file a card's board is read with: projects/<project, lowercased>.env.
export const envFile = (checkout: string, project: string) => `${checkout}/projects/${project.toLowerCase()}.env`

type Comment = { author?: { login?: string }; createdAt?: string; body?: string }

// `/card`: title, column, the newest comment first, its PR.
export function cardText(n: string, issue: Json, prs: Json[]): string {
  const column = (issue.projectItems ?? []).map((i: Json) => i.status?.name).filter(Boolean).join(', ') || '(not on a board)'
  const comments = [...((issue.comments ?? []) as Comment[])].sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''))
  const out = [`#${n} ${issue.title ?? ''}`, `Column: ${column} (${String(issue.state ?? '').toLowerCase()})`]
  const labels = (issue.labels ?? []).map((l: Json) => l.name).join(', ')
  if (labels) out.push(`Labels: ${labels}`)
  const c = comments[0]
  out.push('', c ? `Newest comment, ${c.author?.login ?? 'unknown'}, ${c.createdAt ?? ''}:\n${(c.body ?? '').trim()}` : 'No comments.')
  out.push('', prs.length ? `PR: ${prs.map((p) => `#${p.number} ${p.state} ${p.url}`).join('; ')}` : 'PR: none found.')
  return out.join('\n')
}

export const DRAFT_PROMPT = (sentence: string) => `Draft a GitHub card for a project board from the owner's one sentence. Use exactly this shape, American English, no preamble:

TITLE: <what ships, in one line>
---
**Ask:** <one line, bold label, what ships>

## Why now
<the reading, with today's date if it was given>

## What the owner gets
<what they see or can do, and where they open it>

## How
<files or approach; say what is unchanged>

## Rules that still hold
<only when the gate is touched; else "None.">

## Done when
<what a reviewer can check>

Replaces: nothing

<details><summary>Details</summary>

- <history: the owner's words, dated>

</details>

The owner's sentence: ${sentence}`

export function parseDraft(text: string, repo: string): Draft | undefined {
  const m = /^\s*TITLE:\s*(.+?)\s*\n-{3,}\s*\n([\s\S]+)$/.exec(text.trim())
  return m ? { repo, title: m[1], body: m[2].trim() + '\n' } : undefined
}
