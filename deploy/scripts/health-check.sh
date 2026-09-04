#!/usr/bin/env bash
set -euo pipefail

URL="${LICENSE_HEALTH_URL:-http://127.0.0.1:8787/health}"
BODY="$(curl --fail --silent --show-error --max-time 5 "$URL")"
grep -q '"status":"ok"' <<<"$BODY"
echo "$BODY"

