// The contract for the owner-pane mod's `$.state` (card #214).
export type StuckRow = { project: string; number: number; url: string; ask: string }
export type LaneRow = { project: string; lane: string; pill: string; card?: number; reason: string }
export type Snapshot = {
  // ISO time of the last good read; empty before the first.
  at: string
  // Why the last read failed, or empty.
  error: string
  // `/opt/watcher` line: which commit runs and the deploy's state, or empty.
  ship: string
  stuck: StuckRow[]
  lanes: LaneRow[]
}
export type Draft = { repo: string; title: string; body: string }

declare module 'claude-code' {
  interface PluginState {
    'owner-pane': { snapshot: Shaped<Snapshot>; draft: Shaped<Draft | null> }
  }
}
