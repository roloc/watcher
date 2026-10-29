#!/usr/bin/env bash
# The desk's one road to GitHub reads (card #205, review of roloc/watcher#218, round 4): a
# permission rule is a command prefix, and `gh issue view 1 -R <host>/<owner>/<repo>` sends
# a request to any host with chosen text in it. This script fixes the repository to one
# `owner/repo` on github.com, allows four read subcommands, and refuses every argument that
# could pick another host, open a browser or write.
#
#   bin/desk-gh.sh <owner/repo> issue view <n> [--comments] [--json … --jq …]
#   bin/desk-gh.sh <owner/repo> issue list [--state … --label … --limit … --json …]
#   bin/desk-gh.sh <owner/repo> pr view|list|checks|diff …
#   bin/desk-gh.sh <owner/repo> run view|list …
set -uo pipefail

usage() { echo "desk-gh.sh: usage: desk-gh.sh <owner/repo> issue view|list … | pr view|list|checks|diff … | run view|list …" >&2; exit 2; }
[ $# -ge 3 ] || usage
repo="$1"; noun="$2"; verb="$3"; shift 3
case "$repo" in
  *[!A-Za-z0-9_./-]*|*/*/*|*://*|/*|*/|"") echo "desk-gh.sh: the repository is owner/repo on github.com, got: $repo" >&2; exit 2 ;;
  */*) ;;
  *) echo "desk-gh.sh: the repository is owner/repo on github.com, got: $repo" >&2; exit 2 ;;
esac
case "$noun $verb" in
  "issue view"|"issue list"|"pr view"|"pr list"|"pr checks"|"pr diff"|"run view"|"run list") ;;
  *) echo "desk-gh.sh: $noun $verb is not one of the reads (issue view|list, pr view|list|checks|diff, run view|list)" >&2; exit 2 ;;
esac
for a in "$@"; do
  case "$a" in
    -R*|--repo*|--hostname*|-H*|-w|--web*|--browser*|--template*|--exec*|--run*|--rerun*|--watch*|--fail-fast*|--log*|-b*|--body*|--add*|--remove*|--edit*|--milestone*)
      echo "desk-gh.sh: refused argument $a (a host, a browser, a template, a watch or a write)" >&2; exit 2 ;;
    *://*|http*|*github.com*)
      echo "desk-gh.sh: refused argument $a (a URL picks a host; give the number)" >&2; exit 2 ;;
  esac
done
exec gh "$noun" "$verb" -R "$repo" "$@"
