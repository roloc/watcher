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
# Long flags only, from a fixed list; a positional is a number. The repository is added
# LAST (gh takes the last -R) and gh's own environment knobs are unset before the exec.
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
# Each segment of owner/repo is a name, never `.` or `..`.
case "$repo" in */.|*/..|./*|../*) echo "desk-gh.sh: the repository is owner/repo on github.com, got: $repo" >&2; exit 2 ;; esac

# The arguments: an exact allow list of LONG flags and numbers, nothing else. A prefix
# check is not enough (review of #218, round 5: `-cRhost/o/r` is a cluster that ends in
# -R, `-cw` opens the browser), so no short flag passes at all, and a flag's value may not
# look like a flag or carry a URL.
BARE=" --comments --required --name-only --patch --draft "
VALUED=" --json --jq --state --label --limit --search --author --assignee --base --head --workflow --branch --status --event --sort --order --app --commit --user "
args=()
value_ok() { case "$1" in -*|*://*|*github.com*|"") return 1 ;; esac; return 0; }
while [ $# -gt 0 ]; do
  a="$1"; shift
  case "$a" in
    --*=*) flag="${a%%=*}"; val="${a#*=}"
      case "$VALUED" in *" $flag "*) value_ok "$val" || { echo "desk-gh.sh: refused value for $flag: $val" >&2; exit 2; }; args+=("$flag" "$val") ;;
        *) echo "desk-gh.sh: refused argument $a (not one of the read flags)" >&2; exit 2 ;;
      esac ;;
    --*)
      case "$VALUED" in *" $a "*)
        [ $# -gt 0 ] && value_ok "$1" || { echo "desk-gh.sh: $a needs a plain value" >&2; exit 2; }
        args+=("$a" "$1"); shift; continue ;;
      esac
      case "$BARE" in *" $a "*) args+=("$a") ;;
        *) echo "desk-gh.sh: refused argument $a (not one of the read flags)" >&2; exit 2 ;;
      esac ;;
    -*) echo "desk-gh.sh: refused argument $a (no short flags here: write the long form)" >&2; exit 2 ;;
    *) case "$a" in *[!0-9]*|"") echo "desk-gh.sh: refused argument $a (a positional is a number: an issue, PR or run)" >&2; exit 2 ;; esac
       args+=("$a") ;;
  esac
done
# gh honors the LAST -R and a handful of environment variables; the repository goes last
# and the variables go away, so neither can redirect the request.
unset GH_HOST GH_REPO GH_ENTERPRISE_TOKEN GH_BROWSER BROWSER GH_CONFIG_DIR GH_PAGER PAGER GH_EDITOR EDITOR VISUAL GH_FORCE_TTY
exec gh "$noun" "$verb" ${args[@]+"${args[@]}"} -R "$repo"
