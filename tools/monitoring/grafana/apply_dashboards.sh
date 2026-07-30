#!/usr/bin/env bash
# apply_dashboards.sh — Idempotent Grafana dashboard upsert
#
# Tries the Grafana HTTP API first. If that fails with access denied,
# falls back to writing directly to the provisioning directory
# (/home/joshu/vllm-monitoring/grafana/dashboards/) — Grafana auto-reloads
# from there every 30s (configured in docker provisioning).
#
# Usage: ./apply_dashboards.sh [grafana_url] [api_key_or_password]
set -euo pipefail

GRAFANA_URL="${1:-http://localhost:3001}"
AUTH="${2:-admin:P8CvD1JQBOFP2UuPLnSvf7LhJyPV}"
DIR="$(cd "$(dirname "$0")" && pwd)"
PROVISIONING_DIR="/home/joshu/vllm-monitoring/grafana/dashboards"

apply_via_api() {
  local file="$1"
  local uid
  uid="$(python3 -c "import json; print(json.load(open('$file'))['dashboard']['uid'])")"
  echo "→ API: Applying $file (uid=$uid) ..."

  resp=$(curl -s -u "$AUTH" \
    -X POST "$GRAFANA_URL/api/dashboards/db" \
    -H 'Content-Type: application/json' \
    -d @- < "$file")

  status=$(echo "$resp" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    st = d.get('status', 'error')
    slug = d.get('slug', '')
    url = d.get('url', '')
    print(f'{st}  slug={slug}  url={url}')
except Exception as e:
    print(f'FAILED: {e}')
    print(repr(sys.stdin.read())[:200])
" 2>/dev/null)

  echo "  Result: $status"
  echo "$status" | grep -q '^success' || return 1
  return 0
}

apply_via_provisioning() {
  local file="$1"
  local uid
  uid="$(python3 -c "import json; print(json.load(open('$file'))['dashboard']['uid'])")"
  local target="$PROVISIONING_DIR/$uid.json"

  echo "→ Provisioning: Writing $file -> $target ..."

  # Extract just the dashboard object (provisioning format)
  python3 -c "
import json, sys
with open('$file') as f:
    d = json.load(f)
dashboard = d['dashboard']
# Remove id so Grafana assigns it
dashboard.pop('id', None)
with open('$target', 'w') as f:
    json.dump(dashboard, f, indent=2)
print('Written: uid=' + dashboard.get('uid','??') + ' version=' + str(dashboard.get('version','new')))
"

  echo "  → Wrote $target — Grafana auto-reloads within 30s"
}

echo "=== Grafana Dashboard Apply Script ==="
echo "Target: $GRAFANA_URL"
echo ""

for file in "$DIR/vllm-gemma.json" "$DIR/gpu-fleet.json"; do
  if [ ! -f "$file" ]; then
    echo "✗ File not found: $file"
    continue
  fi
  uid=$(python3 -c "import json; print(json.load(open('$file'))['dashboard']['uid'])")
  title=$(python3 -c "import json; print(json.load(open('$file'))['dashboard']['title'])")
  echo "[$uid] $title"

  if apply_via_api "$file"; then
    echo "  ✓ API apply succeeded"
  else
    echo "  ⚠ API failed — falling back to provisioning directory"
    apply_via_provisioning "$file"
  fi
  echo ""
done

echo "=== Done ==="
