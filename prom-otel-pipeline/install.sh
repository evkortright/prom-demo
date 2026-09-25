#!/usr/bin/env bash
# install.sh — Installs the Prometheus → OTel normalization ingest pipeline
#
# Usage:
#   source .env && bash install.sh
#
# What it does:
#   1. Strips comments from pipeline.json (ES doesn't accept JSON with // comments)
#   2. PUTs the pipeline to _ingest/pipeline/metrics-generic.prometheus@custom
#   3. Verifies the pipeline was installed correctly
#
# The @custom pipeline is automatically called by Elasticsearch for every
# document written to metrics-generic.prometheus-* data streams.
# No other configuration is required.

set -euo pipefail

PIPELINE_NAME="metrics-generic.prometheus@custom"
PIPELINE_FILE="$(dirname "$0")/pipeline.json"

if [[ -z "${ES_ENDPOINT:-}" ]] || [[ -z "${ES_API_KEY:-}" ]]; then
  echo "ERROR: ES_ENDPOINT and ES_API_KEY must be set."
  echo "Run: source .env && bash install.sh"
  exit 1
fi

echo "Installing pipeline: ${PIPELINE_NAME}"
echo "Target: ${ES_ENDPOINT}"
echo ""

# Strip // comments from JSON before sending to Elasticsearch.
# ES's JSON parser does not support comments.
CLEAN_JSON=$(grep -v '^\s*//' "${PIPELINE_FILE}" | grep -v '^\s*$')

# Install the pipeline.
HTTP_STATUS=$(curl -s -o /tmp/es_response.json -w "%{http_code}" \
  -X PUT "${ES_ENDPOINT}/_ingest/pipeline/${PIPELINE_NAME}" \
  -H "Authorization: ApiKey ${ES_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "${CLEAN_JSON}")

if [[ "${HTTP_STATUS}" == "200" ]]; then
  echo "✓ Pipeline installed successfully (HTTP ${HTTP_STATUS})"
else
  echo "✗ Pipeline installation failed (HTTP ${HTTP_STATUS})"
  echo "Response:"
  cat /tmp/es_response.json
  exit 1
fi

echo ""
echo "Verifying installation..."
curl -s \
  -H "Authorization: ApiKey ${ES_API_KEY}" \
  "${ES_ENDPOINT}/_ingest/pipeline/${PIPELINE_NAME}" \
  | python3 -m json.tool | head -10

echo ""
echo "Done. New documents written to metrics-generic.prometheus-* will be"
echo "automatically normalized to OTel semantic conventions on ingest."
echo ""
echo "To test against an existing document, use the pipeline simulate API:"
echo "  See test.json for a ready-to-run simulate request."
