#!/usr/bin/env bash
set -euo pipefail
BASE="${FT_API_URL:-http://127.0.0.1:${FT_PORT:-8094}}"
CURL_ARGS=(--fail-with-body --silent --show-error)
if [[ -n "${FT_API_TOKEN:-}" ]]; then CURL_ARGS+=(-H "Authorization: Bearer $FT_API_TOKEN"); fi
job_body() { python3 -c 'import json,sys; print(json.dumps({"job":sys.argv[1]}))' "${1:?Job ID required}"; }
case "${1:-status}" in
  status) curl "${CURL_ARGS[@]}" "$BASE/status" ;;
  jobs) curl "${CURL_ARGS[@]}" "$BASE/jobs" ;;
  report) curl "${CURL_ARGS[@]}" "$BASE/report/${2:?Job ID required}" ;;
  queue) curl "${CURL_ARGS[@]}" "$BASE/jobs/queue" -H 'Content-Type: application/json' --data-binary "${2:?JSON required}" ;;
  activate|cancel) curl "${CURL_ARGS[@]}" "$BASE/jobs/$1" -H 'Content-Type: application/json' --data-binary "$(job_body "${2:?Job ID required}")" ;;
  import) curl "${CURL_ARGS[@]}" "$BASE/datasets/import" -H 'Content-Type: application/json' --data-binary "${2:?JSON or @file required}" ;;
  *) echo 'usage: ftctl.sh [status|jobs|report ID|queue JSON|activate ID|cancel ID|import JSON_OR_@FILE]'; exit 1 ;;
esac
