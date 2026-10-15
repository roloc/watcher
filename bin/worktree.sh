#!/usr/bin/env bash
#
# Parallel workstreams without stepping on each other.
#
# WHY THIS EXISTS (Azeroth Rewind, 2026-08-07): two Claude sessions worked the
# same checkout on the same day. One session's uncommitted fix got swept into
# the other's commit and shipped inside an unrelated PR; a `git checkout` in
# one session yanked code out from under a live dev server in the other.
# Neither is a git limitation — a working tree holds exactly one branch, and
# `git worktree` is the built-in answer. One worktree per workstream: its own
# directory, its own branch, its own build output, its own dev-server port.
#
# DELIBERATELY SHARED between worktrees:
#   .git           — same branches/commits/remotes, that's the whole point
#   env files      — symlinked (.env.local / .env), so secrets have ONE source
#                    of truth and a new required var can't go stale in a copy
#   databases      — if the project has them; revisit on a destructive migration
#   WORKTREE_COPY_DIRS — a project's own gitignored, in-tree private inputs (named in its
#                    `projects/<name>.env`) are COPIED, not linked. riffmap's is
#                    `docs/transcripts` (card #87, 2026-09-07): the real meetings are
#                    gitignored and live only in the main checkout, and Claude Code refuses
#                    to follow a symlink that leaves the project directory (it prompts,
#                    which headless is a refusal) — so a worktree session could never read
#                    them and no worktree could run a four-meeting eval. A copy is an
#                    ordinary in-tree file, still gitignored here by the same .gitignore.
#                    Transcripts are immutable inputs, so the staleness argument against
#                    copies does not apply to them — but a meeting ADDED to the main
#                    checkout later is not in older worktrees; `doctor` checks only that the
#                    copy exists and is non-empty, not that it is complete. AND: every
#                    worktree now holds real meeting transcripts, so an rsync of a riffmap
#                    worktree off this box must carry `--exclude docs/transcripts` next to
#                    `--exclude out` (the playbook recipe predates this copy and has no such
#                    line).
#
# NOT shared (each worktree gets its own):
#   node_modules   — package-lock.json differs between branches
#   build caches   — .next / build / .gradle are keyed to per-branch source
#
set -euo pipefail

# Worktrees live OUTSIDE the repo on purpose: nested ones get scanned by file
# watchers and are one stray `git add` away from being committed.
ROOT="${WORKTREE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && git rev-parse --show-toplevel)}"
# --show-toplevel inside a worktree returns THAT worktree, not the original
# checkout, so resolve the real one via the common git dir. The `cd "$ROOT"`
# first is load-bearing (Barf, Azeroth PR #26): from the MAIN checkout
# --git-common-dir returns a path RELATIVE to the repo (".git"), and without
# the cd it resolves against the caller's cwd.
MAIN="$(cd "$ROOT" && cd "$(git rev-parse --git-common-dir)/.." && pwd)"
REPO_NAME="$(basename "$MAIN")"
HOME_DIR="$(dirname "$MAIN")/${REPO_NAME}-worktrees"
ENV_FILES=(.env.local .env)
# The gitignored, in-tree inputs a worktree needs its own copy of (see the header for why
# these are copied and not linked). Card #157: no project's own private directory
# (`docs/transcripts` was riffmap's) is a default here — a project with such inputs sets
# WORKTREE_COPY_DIRS in its own `projects/<name>.env`, space-separated and relative to the
# checkout (`WORKTREE_COPY_DIRS="docs/fixtures data/seed"`); the spawner exports it. Unset
# or empty both mean "copy nothing," and every entry is guarded by a `-d` test regardless,
# so a project with no such directory is never affected. Expanded below as
# ${COPY_DIRS[@]+"${COPY_DIRS[@]}"}: under `set -u`, bash 3.2 (still what /bin/bash is on a
# Mac) treats a plain "${empty_array[@]}" as an unbound variable and aborts. The box runs
# bash 5, where it is fine; this script is also run by hand on a laptop, where it is not.
if [ -n "${WORKTREE_COPY_DIRS:-}" ]; then
  # Unquoted on purpose: a space-separated list from the env becomes the array's elements.
  # shellcheck disable=SC2206
  COPY_DIRS=(${WORKTREE_COPY_DIRS})
else
  COPY_DIRS=()
fi

usage() {
  cat <<USAGE
Usage: scripts/worktree.sh <command> [args]

  new <name> [base]   Create ../${REPO_NAME}-worktrees/<name> on a new branch
                      <name>, branched from [base] (default: main, freshly
                      fetched). Symlinks env files, copies the private inputs
                      (${COPY_DIRS[*]:-none}), installs dependencies.
  list                Show every worktree, its branch, and whether it is dirty.
  remove <name>       Delete the worktree and its branch. Refuses if it has
                      uncommitted work, or commits that neither reached main
                      nor shipped in a merged PR (this repo squash-merges,
                      so "not on main" alone does not mean unshipped).
  doctor              Check every worktree for the things that rot: a missing
                      env symlink and missing dependencies.

If .claude/launch.json sets "autoPort": true, each worktree's dev server picks
its own port, so several can run at once.
USAGE
}

# Per-stack dependency install. Add a branch when a new stack shows up.
install_deps() {
  local dir="$1"
  if [ -f "$dir/package-lock.json" ]; then
    echo "Installing dependencies (npm ci)…"
    (cd "$dir" && npm ci --silent)
  elif [ -f "$dir/gradlew" ]; then
    # Gradle's caches live in ~/.gradle and are shared across worktrees by
    # design; nothing to install per tree. Warm the wrapper so the first
    # build in this tree doesn't surprise anyone with a download.
    (cd "$dir" && ./gradlew --version --quiet >/dev/null 2>&1) || \
      echo "warning: ./gradlew failed — is a JDK installed on this machine?" >&2
  fi
}

# Repo-committed git hooks (scripts/hooks/pre-commit refuses a staged API key). The
# setting lives in the common git dir, so ONE `git config` covers the main checkout and
# every worktree; the path is relative, so each tree runs its own copy of the hook.
# Idempotent. Skipped for repos that ship no hooks.
HOOKS_DIR="scripts/hooks"
# Gated on the tree THIS script runs from ($ROOT), not $MAIN: the branch that adds the
# hooks has them before main does.
install_git_hooks() {
  [ -f "$ROOT/$HOOKS_DIR/pre-commit" ] || return 0
  git -C "$MAIN" config core.hooksPath "$HOOKS_DIR"
}
hooks_ok() {
  [ -f "$ROOT/$HOOKS_DIR/pre-commit" ] || return 0
  [ "$(git -C "$MAIN" config --get core.hooksPath 2>/dev/null)" = "$HOOKS_DIR" ]
}

deps_ok() {
  local dir="$1"
  if [ -f "$dir/package.json" ]; then [ -d "$dir/node_modules" ]; else true; fi
}

cmd_new() {
  local name="${1:-}" base="${2:-main}"
  [ -n "$name" ] || { echo "error: need a name" >&2; usage >&2; exit 1; }
  local dir="$HOME_DIR/$name"
  [ -e "$dir" ] && { echo "error: $dir already exists" >&2; exit 1; }

  # Branch from a FRESH base — the whole point is parallel work, so the base
  # has almost certainly moved since this shell last looked at it.
  git -C "$MAIN" fetch --quiet origin 2>/dev/null || \
    echo "warning: fetch failed; branching from the local '$base'" >&2

  local start="$base"
  if git -C "$MAIN" show-ref --verify --quiet "refs/remotes/origin/$base"; then
    start="origin/$base"
  fi

  mkdir -p "$HOME_DIR"
  # --no-track is load-bearing: branching from origin/main WITHOUT it sets
  # the new branch's upstream to origin/main, which points a later bare
  # `git push` at main itself. Set the real upstream on first push:
  #   git push -u origin <name>
  git -C "$MAIN" worktree add "$dir" -b "$name" --no-track "$start"

  # Symlink, never copy: a copied env file silently goes stale the day a new
  # required var lands. Check the target first — linking a missing file
  # yields a DANGLING symlink that `doctor` then reports as missing while its
  # suggested fix fails with "File exists" (Barf, Azeroth PR #26).
  local f linked=0
  for f in "${ENV_FILES[@]}"; do
    if [ -e "$MAIN/$f" ]; then ln -s "$MAIN/$f" "$dir/$f"; linked=1; fi
  done
  [ "$linked" -eq 1 ] || echo "note: no env file in $MAIN to share (none of: ${ENV_FILES[*]})." >&2

  # Copy, never link — see the header. The -d guard is what makes a missing source a
  # no-op instead of a failed `new`. -L dereferences: if the source ever becomes, or
  # contains, a symlink, the copy must be a real file, or it reproduces the exact
  # out-of-project link this copy exists to avoid.
  local d
  for d in ${COPY_DIRS[@]+"${COPY_DIRS[@]}"}; do
    if [ -d "$MAIN/$d" ]; then
      mkdir -p "$dir/$d"
      # A warning, not an abort: under `set -e` a broken link or an unreadable file in
      # the source would otherwise stop `new` after the worktree and branch exist but
      # before install_deps and the git hooks — a half-configured tree. The `/.` form
      # copies CONTENTS into an existing target, so the hint below is re-runnable after a
      # partial copy; `cp -RL src dst` onto an existing dst nests it as dst/transcripts.
      if cp -RL "$MAIN/$d/." "$dir/$d/"; then
        echo "Copied $d ($(find "$dir/$d" -type f | wc -l | tr -d ' ') files) from $MAIN."
      else
        echo "warning: copying $d failed (possibly partially); run: cp -RL $MAIN/$d/. $dir/$d/" >&2
      fi
    fi
  done

  install_deps "$dir"
  install_git_hooks

  cat <<EOF

Worktree ready:
  cd $dir

Branch '$name' from $start. Env files and databases are shared with $MAIN.
EOF
}

cmd_list() {
  git -C "$MAIN" worktree list --porcelain | awk '
    /^worktree /  { dir=$2 }
    /^branch /    { br=$2; sub("refs/heads/","",br); print dir "\t" br }
    /^detached/   { print dir "\tDETACHED" }
  ' | while IFS=$'\t' read -r dir br; do
    local_status="clean"
    if [ -n "$(git -C "$dir" status --porcelain 2>/dev/null)" ]; then
      local_status="DIRTY — uncommitted work"
    fi
    printf '%-60s %-32s %s\n' "$dir" "$br" "$local_status"
  done
}

cmd_remove() {
  local name="${1:-}"
  [ -n "$name" ] || { echo "error: need a name" >&2; exit 1; }
  local dir="$HOME_DIR/$name"
  [ -d "$dir" ] || { echo "error: no worktree at $dir" >&2; exit 1; }

  # Fetch before measuring against origin/main — otherwise a branch merged
  # upstream since the last fetch still looks unshipped and gets refused.
  # WARN, don't abort (Barf, Azeroth PR #27): a stale origin/main only makes
  # the comparison below MORE conservative.
  git -C "$MAIN" fetch --quiet origin 2>/dev/null || \
    echo "warning: fetch failed; comparing against a possibly-stale origin/main" >&2

  # Refuse rather than warn. The failure this whole script exists to prevent
  # was work disappearing.
  if [ -n "$(git -C "$dir" status --porcelain)" ]; then
    echo "error: $name has uncommitted changes. Commit or stash first." >&2
    git -C "$dir" status --short >&2
    exit 1
  fi
  # Read the branch OFF THE WORKTREE, never assume it matches the directory
  # name — a failed lookup must not read as a clean bill of health.
  local branch
  branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    echo "error: could not determine the branch checked out at $dir" >&2
    echo "Inspect it, or force with: git worktree remove --force $dir" >&2
    exit 1
  fi

  local unmerged
  if ! unmerged="$(git -C "$MAIN" log --oneline "origin/main..$branch" 2>/dev/null)"; then
    echo "error: could not compare '$branch' against origin/main." >&2
    echo "Refusing rather than guessing. Force with: git worktree remove --force $dir" >&2
    exit 1
  fi
  if [ -n "$unmerged" ]; then
    # `origin/main..$branch` is NOT a merged-check in a squash-merge repo:
    # the branch's own commits never land on main. Ask GitHub whether a PR
    # from this branch actually merged. THREE distinct states, three distinct
    # messages (Barf, Azeroth PR #27): no merged PR, gh missing, gh logged out.
    if ! command -v gh >/dev/null 2>&1; then
      echo "error: '$branch' has commits not on origin/main, and gh is not" >&2
      echo "       installed, so I cannot check whether a PR from it merged." >&2
      echo "       Verify manually, then: git worktree remove --force $dir" >&2
      exit 1
    fi
    local pr_line
    # From $MAIN: gh resolves the repo from the cwd, and the caller (the spawner) may be
    # anywhere — without this, every remove reported "gh not authenticated".
    if ! pr_line="$(cd "$MAIN" && gh pr list --head "$branch" --state merged --limit 1 \
                      --json number,headRefOid \
                      --jq '.[] | "\(.number) \(.headRefOid)"' 2>/dev/null)"; then
      echo "error: could not ask GitHub about '$branch' (gh not authenticated?)." >&2
      echo "       Refusing rather than guessing. Run: gh auth login" >&2
      exit 1
    fi

    local merged_pr merged_sha
    merged_pr="${pr_line%% *}"
    merged_sha="${pr_line##* }"

    if [ -z "$merged_pr" ]; then
      echo "error: '$branch' has commits not on origin/main and no merged PR:" >&2
      echo "$unmerged" >&2
      echo "Open a PR, or force with: git worktree remove --force $dir" >&2
      exit 1
    fi

    # --head matches by NAME, not identity: a recycled branch name would
    # match its old merged PR. EQUALITY of tips, not --is-ancestor (Barf,
    # Azeroth PR #27 re-review): after a squash merge with no further work
    # the branch tip IS the PR head; anything else means unshipped commits.
    local branch_tip
    branch_tip="$(git -C "$MAIN" rev-parse "$branch" 2>/dev/null || true)"
    if [ "$merged_sha" != "$branch_tip" ]; then
      echo "error: '$branch' matches merged PR #$merged_pr by NAME, but that PR's" >&2
      echo "       head ($merged_sha) is not this branch's tip ($branch_tip)." >&2
      echo "       Either the name was reused, or work was added after that PR" >&2
      echo "       merged — either way there are commits here it never shipped." >&2
      echo "$unmerged" >&2
      echo "Force with: git worktree remove --force $dir" >&2
      exit 1
    fi

    echo "Branch '$branch' shipped as PR #$merged_pr (squash-merged, so its"
    echo "commits aren't on main). Cleaning up."
  fi

  git -C "$MAIN" worktree remove "$dir"
  # -D, not -d: after a squash merge git still considers the branch unmerged.
  # Safe only because everything above established the work shipped.
  git -C "$MAIN" branch -D "$branch" 2>/dev/null || true
  git -C "$MAIN" worktree prune
  echo "Removed $name."
}

cmd_doctor() {
  local bad=0 dir f
  # Process substitution, NOT a pipe: a piped `while` runs in a subshell and
  # every `bad=1` inside it would be discarded.
  while read -r dir; do
    [ "$dir" = "$MAIN" ] && continue
    for f in "${ENV_FILES[@]}"; do
      if [ -e "$MAIN/$f" ] && [ ! -e "$dir/$f" ]; then
        echo "MISSING $f: $dir"
        # -f because the likeliest cause is a DANGLING symlink.
        echo "  fix: ln -sf $MAIN/$f $dir/$f"
        bad=1
      fi
    done
    for f in ${COPY_DIRS[@]+"${COPY_DIRS[@]}"}; do
      # Not for the harness's own sandbox worktrees under .claude/worktrees/: those come
      # and go on their own and no tick runs in one — no reason to multiply copies of
      # private meetings into them.
      case "$dir" in "$MAIN"/.claude/worktrees/*) continue ;; esac
      # Non-empty, not merely present: `new` creates the directory before copying, so a
      # failed copy leaves an empty one behind.
      if [ -d "$MAIN/$f" ] && { [ ! -d "$dir/$f" ] || [ -z "$(ls -A "$dir/$f" 2>/dev/null)" ]; }; then
        echo "MISSING $f: $dir"
        echo "  fix: mkdir -p $dir/$f && cp -RL $MAIN/$f/. $dir/$f/"
        bad=1
      fi
    done
    if ! deps_ok "$dir"; then
      echo "MISSING dependencies: $dir"
      echo "  fix: (cd $dir && npm ci)"
      bad=1
    fi
  done < <(git -C "$MAIN" worktree list --porcelain | awk '/^worktree /{print $2}')
  if ! hooks_ok; then
    echo "MISSING git hooks: core.hooksPath is not '$HOOKS_DIR' (the pre-commit key guard is off)"
    echo "  fix: git -C $MAIN config core.hooksPath $HOOKS_DIR"
    bad=1
  fi
  [ "$bad" -eq 0 ] && echo "All worktrees healthy."
  # Return the FLAG, not 0 (Barf, Azeroth PR #26): a checker that can only
  # ever pass is worse than no checker.
  return "$bad"
}

case "${1:-}" in
  new)    shift; cmd_new "$@" ;;
  list)   shift; cmd_list "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  doctor) shift; cmd_doctor "$@" ;;
  *)      usage; exit 1 ;;
esac
