#!/usr/bin/env bash
# One probe of lonestar-web (192.168.176.37), the staging box riffmap's own `deploy-dev`
# ships `main` to automatically (riffmap CLAUDE.md, "lonestar-web is staging"). For
# projects/riffmap.detect.env's DETECT_METRIC_CMD — prints 1 on anything but a 2xx/3xx
# response or a timeout, 0 otherwise. bin/detect.sh turns a run of these into a rolling
# failure rate, so one blip is not a breach but a sustained one is.
#
# Picked as riffmap's first metric (card #36's three candidates: this, an error rate, and
# a share-link render rate) because it needs no new instrumentation in riffmap's own repo —
# the other two need riffmap to emit something that does not exist yet.
set -uo pipefail
code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' http://192.168.176.37/ 2>/dev/null)"
case "$code" in
  2*|3*) echo 0 ;;
  *)     echo 1 ;;
esac
