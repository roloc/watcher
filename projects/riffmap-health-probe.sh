#!/usr/bin/env bash
# One probe of RiffMap itself, on lonestar-web (192.168.176.37) — the staging box
# riffmap's own `deploy-dev` ships `main` to automatically (riffmap CLAUDE.md,
# "lonestar-web is staging"). Pinned to that LAN address but sent with RiffMap's own
# hostname: the box's default vhost answers any unmatched Host: header with a different
# app (Azeroth Rewind), so a plain probe of the IP was Azeroth answering, never RiffMap
# (roloc/watcher#36, Steve, September 28, 2026). `/api/me` answers 401 to an anonymous
# request whenever RiffMap itself is up — checked from the Mac and the agents box on
# September 28, 2026, both got 401 — so 200 and 401 are both healthy; a 5xx, a timeout
# (`000`), or anything else is not. For projects/riffmap.detect.env's DETECT_METRIC_CMD —
# prints 1 on an unhealthy reading, 0 on a healthy one. bin/detect.sh turns a run of these
# into a rolling failure rate, so one blip is not a breach but a sustained one is.
#
# Picked as riffmap's first metric (card #36's three candidates: this, an error rate, and
# a share-link render rate) because it needs no new instrumentation in riffmap's own repo —
# the other two need riffmap to emit something that does not exist yet.
set -uo pipefail
code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' \
  --resolve riffmap.lonestarprod.com:443:192.168.176.37 \
  https://riffmap.lonestarprod.com/api/me 2>/dev/null)"
case "$code" in
  200|401) echo 0 ;;
  *)       echo 1 ;;
esac
