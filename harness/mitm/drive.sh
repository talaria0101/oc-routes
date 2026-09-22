#!/usr/bin/env bash
# oc-routes CLI drive battery under the net tap.
# Safe by construction: every command gets `timeout -k 5 <secs>` (the CLI
# ignores SIGTERM while polling, so the KILL grace matters), stdin is
# /dev/null, and interactive-only flows are driven with short timeouts purely
# to capture their first network touch, never to complete them.
# Gentle by design: local reads plus a handful of live fetches, no auth,
# no retries. Quota instruments stop at the wall.
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

MANIFEST="$PWD/$OUT/manifest.json"
MANIFEST_TMP="$MANIFEST.tmp"
printf '{"cli": "%s", "runs": [' "$BIN" > "$MANIFEST_TMP"
FIRST_RUN=1
run() { # run <tag> <secs> <cmd...>
  local tag=$1 secs=$2; shift 2
  export OC_ROUTES_NETLOG="$PWD/$OUT/net-$tag.log"
  rm -f "$OC_ROUTES_NETLOG"
  echo "### $tag: $*"
  # shellcheck disable=SC2086
  local t0 t1 code ev
  t0=$(date +%s)
  code=0
  timeout -k 5 "$secs" "$@" </dev/null >"$OUT/out-$tag.txt" 2>"$OUT/err-$tag.txt" || code=$?
  t1=$(date +%s)
  ev=$(wc -l < "$OC_ROUTES_NETLOG" 2>/dev/null || echo 0)
  echo "exit=$code net=${ev}L"
  if [ "$FIRST_RUN" = 1 ]; then FIRST_RUN=0; else printf ',' >> "$MANIFEST_TMP"; fi
  printf '{"name": "%s", "rc": %s, "seconds": %s, "events": %s}' \
    "$tag" "$code" "$((t1 - t0))" "$ev" >> "$MANIFEST_TMP"
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

printf ']}\n' >> "$MANIFEST_TMP"
mv "$MANIFEST_TMP" "$MANIFEST"  # atomic: a killed drive never leaves half a manifest
echo "manifest: $MANIFEST"

# Scrub single-use device-flow codes from committed tails (expired, but
# never commit secrets-adjacent strings verbatim).
for f in "$OUT"/out-*.txt "$OUT"/err-*.txt "$MANIFEST"; do
  [ -f "$f" ] || continue
  sed -i -E -e 's/user_code=[A-Z0-9-]+/user_code=***/g' \
    -e 's/Enter code: [A-Z0-9-]+/Enter code: ***/g' \
    -e 's/device_code[\"'"'"']?\s*[:=]\s*[\"'"'"']?[0-9a-fA-F-]{8,}/device_code=***/g' "$f"
done
echo "scrubbed device codes in $OUT"

python3 harness/mitm/analyze.py --caps "$OUT"
echo "done. curated: $OUT/endpoints.json $OUT/tap-hosts.json $OUT/REPORT.md $OUT/REPORT.txt $OUT/NEW_ENDPOINTS.md"
