// A trimmed copy of the floor page's /api/state.json (card #214's test fixture).
export const STATE = JSON.stringify({"header": {"owner_login": "roloc"},
 "projects": [
  {"name": "watcher", "board_url": "https://github.com/users/roloc/projects/2",
   "ship": {"deploy": {"live": "b6a9eb8", "target": "debdfad", "state": "held"}},
   "lanes": [{"lane": "watcher", "kind": "Build", "pill": "needs_steve", "card": {"number": 213, "url": "https://github.com/roloc/watcher/issues/213"}, "reason": "Waiting on the owner."}],
   "stuck": [{"card": {"number": 213, "url": "https://github.com/roloc/watcher/issues/213"}, "ask": "**Needs @roloc: is the note at tick start enough?**", "rounds": []}]},
  {"name": "riffmap", "ship": null,
   "lanes": [{"lane": "riffmap", "kind": "Build", "pill": "idle", "reason": "Nothing to pick up right now."}],
   "stuck": []}
 ]}
)
