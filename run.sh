#!/usr/bin/env bash
# oc-routes one-command refresh. Stdlib only, needs network unless --no-network.
set -euo pipefail
cd "$(dirname "$0")"

SRC=""
PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --src=*) SRC="${1#--src=}"; shift;;
    --src) SRC="${2:-}"; shift 2;;
    *) PASS+=("$1"); shift;;
  esac
done
if [ -z "$SRC" ]; then
  for c in ../opencode-src ./opencode-src /workspace/opencode-src; do
    if [ -d "$c" ]; then SRC="$c"; break; fi
  done
fi

if [ -n "$SRC" ]; then
  python3 harness/discover.py --src "$SRC" "${PASS[@]}"
else
  python3 harness/discover.py "${PASS[@]}"
fi
python3 harness/build_spec.py "${PASS[@]}"
