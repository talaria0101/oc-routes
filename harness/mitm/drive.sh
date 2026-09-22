#!/usr/bin/env bash
# oc-routes CLI drive battery under the net tap.
# Safe by construction: every command gets `timeout -k 5 <secs>` (the CLI
# ignores SIGTERM while polling, so the KILL grace matters), stdin is
# /dev/null, and interactive-only flows are driven with short timeouts purely
# to capture their first network touch, never to complete them.
#
#   ./harness/mitm/drive.sh [outdir]   # default: captures/
#
# Requires: prebuilt CLI at /tmp/oc-bin (bun add opencode-ai) or OPENCODE_BIN,
# and harness/mitm/netlog.so built (gcc -shared -fPIC -O2 -o netlog.so preload.c -ldl).
set -euo pipefail
cd "$(dirname "$0")/../.."

OUT="${1:-captures}"
BIN="${OPENCODE_BIN:-/tmp/oc-bin/node_modules/.bin/opencode}"
TAP="$PWD/harness/mitm/netlog.so"
mkdir -p "$OUT"

if [ ! -x "$BIN" ]; then
  echo "no CLI binary at $BIN (set OPENCODE_BIN= or install: bun add opencode-ai in /tmp/oc-bin)" >&2
  exit 2
fi
if [ ! -f "$TAP" ]; then
  echo "missing $TAP, build it: gcc -shared -fPIC -O2 -o netlog.so preload.c -ldl" >&2
  exit 2
fi

export LD_PRELOAD="$TAP"
export OPENCODE_DISABLE_AUTOUPDATE=1

run() { # run <tag> <secs> <cmd...>
  local tag=$1 secs=$2; shift 2
  export OC_ROUTES_NETLOG="$PWD/$OUT/net-$tag.log"
  rm -f "$OC_ROUTES_NETLOG"
  echo "### $tag: $*"
  # shellcheck disable=SC2086
  code=0
  timeout -k 5 "$secs" "$@" </dev/null >"$OUT/out-$tag.txt" 2>"$OUT/err-$tag.txt" || code=$?
  echo "exit=$code net=$(wc -l < "$OC_ROUTES_NETLOG" 2>/dev/null || echo 0)L"
}

run models-cached 60 "$BIN" models
run models-refresh 120 "$BIN" models --refresh
run models-opencode 60 "$BIN" models opencode
run models-verbose 60 "$BIN" models opencode --verbose
run providers-list 60 "$BIN" providers list
run stats 60 "$BIN" stats
run console-login 30 "$BIN" console login
run auth-login 25 "$BIN" auth login --provider opencode
run serve 15 "$BIN" serve --port 18789

echo "done. now: python3 harness/mitm/analyze.py --caps $OUT"
