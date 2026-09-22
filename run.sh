#!/usr/bin/env bash
# oc-routes one-command refresh. Stdlib only, needs network unless --offline.
# Chain: discover (inventory + twins) -> build_spec (spec + twins) ->
# analyze (captures curation, offline) -> snapshot prune (latest per kind).
# Guards in each step abort before writing rather than publishing bad data.
set -euo pipefail
cd "$(dirname "$0")"

MODE="full"
SRC=""
PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --src=*) SRC="${1#--src=}"; shift;;
    --src) SRC="${2:-}"; shift 2;;
    --report-only) MODE="report"; shift;;
    --offline) MODE="offline"; shift;;
    --force) PASS+=("--force"); shift;;
    *) PASS+=("$1"); shift;;
  esac
done
if [ -z "$SRC" ]; then
  for c in ../opencode-src ./opencode-src /workspace/opencode-src; do
    if [ -d "$c" ]; then SRC="$c"; break; fi
  done
fi

case "$MODE" in
  report)
    python3 harness/discover.py --report-only || exit $?
    python3 harness/build_spec.py --report-only || exit $?
    python3 harness/mitm/analyze.py --report-only --caps captures || exit $?
    echo "reports re-rendered offline (no network)"
    exit 0
    ;;
  offline)
    # rebuild specs from snapshots only; re-curate captures offline
    if [ -n "$SRC" ]; then
      python3 harness/discover.py --src "$SRC" --no-network "${PASS[@]}" || exit $?
    else
      python3 harness/discover.py --no-network "${PASS[@]}" || exit $?
    fi
    python3 harness/build_spec.py --no-network "${PASS[@]}" || exit $?
    python3 harness/mitm/analyze.py --caps captures --routes spec/routes.json \
      --spec spec/opencode-models.json || exit $?
    exit 0
    ;;
  full)
    if [ -n "$SRC" ]; then
      python3 harness/discover.py --src "$SRC" "${PASS[@]}" || exit $?
    else
      python3 harness/discover.py "${PASS[@]}" || exit $?
    fi
    python3 harness/build_spec.py "${PASS[@]}" || exit $?
    python3 harness/mitm/analyze.py --caps captures --routes spec/routes.json \
      --spec spec/opencode-models.json "${PASS[@]}"
    rc=$?
    if [ $rc -eq 1 ]; then
      echo "analyze verdict: unknown hosts (see captures/NEW_ENDPOINTS.md)" >&2
    fi
    [ $rc -eq 0 ] || [ $rc -eq 1 ] || exit $rc  # 2 = validation refusal: fail

    # prune snapshots: latest file per kind only (large catalog/api refetch live)
    for pat in "openapi-*.json" "probes-*.json" "spec-probes-*.json" \
               "zen-models-*.json" "zen-go-models-*.json" "docs-zen-*.html" \
               "models-api-*.json" "catalog-*.json"; do
      # shellcheck disable=SC2086
      ls -1 snapshots/$pat 2>/dev/null | sort | head -n -1 | xargs -r rm -v
    done
    echo "refresh done: spec/*.json + REPORT.md/TXT twins + captures curation"
    exit $rc
    ;;
esac
