#!/usr/bin/env bash
# oc-routes one-command refresh. Stdlib only, needs network unless --no-network.
# Chain: discover (inventory + twins) -> build_spec (spec + twins) ->
# analyze (captures curation, offline) -> snapshot prune (latest per kind).
# Guards in each step abort before writing rather than publishing bad data.
set -euo pipefail
cd "$(dirname "$0")"

SRC=""
PASS=()
REPORT_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --src=*) SRC="${1#--src=}"; shift;;
    --src) SRC="${2:-}"; shift 2;;
    --report-only) REPORT_ONLY=1; shift;;
    *) PASS+=("$1"); shift;;
  esac
done
if [ -z "$SRC" ]; then
  for c in ../opencode-src ./opencode-src /workspace/opencode-src; do
    if [ -d "$c" ]; then SRC="$c"; break; fi
  done
fi

if [ "$REPORT_ONLY" = 1 ]; then
  python3 harness/discover.py --report-only
  python3 harness/build_spec.py --report-only
  python3 harness/mitm/analyze.py --caps captures --routes spec/routes.json \
    --spec spec/opencode-models.json
  echo "reports re-rendered offline (no network)"
  exit 0
fi

if [ -n "$SRC" ]; then
  python3 harness/discover.py --src "$SRC" "${PASS[@]}"
else
  python3 harness/discover.py "${PASS[@]}"
fi
python3 harness/build_spec.py "${PASS[@]}"
python3 harness/mitm/analyze.py --caps captures --routes spec/routes.json \
  --spec spec/opencode-models.json || true  # FAIL=unknown hosts; don't block

# prune snapshots: latest file per kind only (large catalog/api refetch live)
for pat in "openapi-*.json" "probes-*.json" "spec-probes-*.json" \
           "zen-models-*.json" "zen-go-models-*.json" "docs-zen-*.html" \
           "models-api-*.json" "catalog-*.json"; do
  # shellcheck disable=SC2086
  ls -1 snapshots/$pat 2>/dev/null | sort | head -n -1 | xargs -r rm -v
done
echo "refresh done: spec/*.json + REPORT.md/TXT twins + captures curation"
