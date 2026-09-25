#!/usr/bin/env bash
# reset.sh — Full reset cycle for prom-otel-pipeline development
#
# Usage (from prom-demo/ root):
#   source .env && bash prom-otel-pipeline/reset.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="${SCRIPT_DIR}/.."

# Load credentials
ENV_FILE="${COMPOSE_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  source "${ENV_FILE}"
fi

if [[ -z "${ES_ENDPOINT:-}" ]] || [[ -z "${ES_API_KEY:-}" ]]; then
  echo "ERROR: ES_ENDPOINT and ES_API_KEY must be set."
  echo "Run: source .env && bash prom-otel-pipeline/reset.sh"
  exit 1
fi

# Step 1 — Stop
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Step 1 — Stopping Docker Compose stack"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
cd "${COMPOSE_DIR}"
docker compose down
echo "✓ Stack stopped"
echo ""

# Step 2 — Delete data streams
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Step 2 — Deleting metrics data streams"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
HTTP_STATUS=$(curl -s -o /tmp/es_delete_response.json -w "%{http_code}" \
  -X DELETE "${ES_ENDPOINT}/_data_stream/metrics-*" \
  -H "Authorization: ApiKey ${ES_API_KEY}")

if [[ "${HTTP_STATUS}" == "200" ]]; then
  echo "✓ Data streams deleted (HTTP ${HTTP_STATUS})"
elif [[ "${HTTP_STATUS}" == "404" ]]; then
  echo "✓ No data streams found — nothing to delete"
else
  echo "⚠ Unexpected response (HTTP ${HTTP_STATUS}) — continuing anyway"
fi
echo ""

# Step 3 — Regenerate prometheus-resolved.yaml
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Step 3 — Regenerating prometheus-resolved.yaml"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
sed "s|\${ES_ENDPOINT}|${ES_ENDPOINT}|g; s|\${ES_API_KEY}|${ES_API_KEY}|g" \
  "${COMPOSE_DIR}/prometheus/prometheus.yaml" \
  > "${COMPOSE_DIR}/prometheus/prometheus-resolved.yaml"
echo "✓ prometheus-resolved.yaml written"
echo ""

# Step 4 — Start
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Step 4 — Starting Docker Compose stack"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
docker compose up --detach
echo ""

echo "Waiting for first scrape cycle (~20 seconds)..."
sleep 20

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Verification — checking for OTel data streams"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
STREAMS=$(curl -s \
  -H "Authorization: ApiKey ${ES_API_KEY}" \
  "${ES_ENDPOINT}/_data_stream/metrics-*" \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
streams = [s['name'] for s in data.get('data_streams', [])]
print('\n'.join(streams) if streams else 'none yet')
" 2>/dev/null || echo "none yet")

echo "Data streams found:"
echo "${STREAMS}"
echo ""

if [[ "${STREAMS}" == "none yet" ]]; then
  echo "⚠ No data streams yet — Collector may still be starting."
  echo "  Check logs: docker compose logs otelcol"
else
  echo "✓ Data is flowing!"
  echo "  Inspect in Kibana Dev Tools: GET metrics-*.otel-*/_search"
fi

echo ""
echo "Live Collector logs: docker compose logs -f otelcol"
