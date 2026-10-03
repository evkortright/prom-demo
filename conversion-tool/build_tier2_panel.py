#!/usr/bin/env python3
"""
build_tier2_panel.py — build & import a Kibana panel from a Tier 2
AI-generated ES|QL query.

Deliberately minimal glue: Tier 2's job is only to produce a correct ES|QL
string (see tier2_engine.py). Panel-building (Kibana Lens JSON shape,
styling, dashboard envelope) is handled by convert_panel.py's existing
functions, which are tier-agnostic — they don't care whether the ES|QL came
from Tier 1's deterministic builder or Tier 2's AI engine.

This script hardcodes the specific query that was validated end-to-end
against live Elasticsearch on 2026-10-02 (qwen2.5-coder:7b, attempt 1, 0
validation issues, confirmed-correct categorization). Once this renders
correctly in Kibana, the next step is calling tier2_engine.translate_tier2()
live instead of hardcoding — this script is for validating the panel-building
side in isolation first.

Usage:
    python3 build_tier2_panel.py --print-only     # just show the dashboard JSON
    python3 build_tier2_panel.py                  # build and import to Kibana
                                                     (needs KIBANA_ENDPOINT, ES_API_KEY)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from convert_panel import (
    build_kibana_timeseries_panel,
    build_kibana_dashboard,
    import_to_kibana,
)

# Validated against live Elasticsearch 2026-10-02 — see session log.
# Six categories confirmed: Busy System, Busy User, Idle, Busy Other (all
# present with sane values on macOS); Busy Iowait, Busy IRQs (absent — no
# such CPU modes on macOS, expected, to be revalidated on Linux).
TIER2_ESQL = """TS metrics-prometheusreceiver.otel-default
| WHERE @timestamp >= NOW() - 1 hour
| WHERE service.name == "node-exporter"
| EVAL cpu_category = CASE(
    `system.cpu.state` == "system",  "Busy System",
    `system.cpu.state` == "user",    "Busy User",
    `system.cpu.state` == "iowait",  "Busy Iowait",
    `system.cpu.state` == "idle",    "Idle",
    `system.cpu.state` IN ("irq", "softirq"), "Busy IRQs",
    `system.cpu.state` != "idle" AND `system.cpu.state` != "user" AND `system.cpu.state` != "system" AND `system.cpu.state` != "iowait" AND `system.cpu.state` != "irq" AND `system.cpu.state` != "softirq", "Busy Other",
    null
  )
| WHERE cpu_category IS NOT NULL
| STATS cpu_rate = AVG(RATE(metrics.system.cpu.time))
    BY BUCKET(@timestamp, 30 seconds), cpu_category"""

PANEL_FILE = os.path.join(os.path.dirname(__file__), "panels", "cpu_basic.json")


def main() -> int:
    with open(PANEL_FILE) as f:
        grafana_panel = json.load(f)

    # Reuses Tier 1's panel builder — it only needs the Grafana panel for
    # styling metadata (title, gridPos, stacking/fill from fieldConfig), not
    # for re-deriving the query. The ES|QL, metric/bucket/dimension column
    # names come from Tier 2's output instead of Tier 1's own builder.
    kibana_panel = build_kibana_timeseries_panel(
        grafana_panel=grafana_panel,
        esql_query=TIER2_ESQL,
        metric_col="cpu_rate",
        bucket_col="BUCKET(@timestamp, 30 seconds)",
        dimension_field="cpu_category",
        series_map={},  # unused by build_kibana_timeseries_panel currently —
                        # Lens auto-colors/labels directly from the raw
                        # dimension column values, which are already the
                        # legend names we want (e.g. "Busy System").
    )

    dashboard = build_kibana_dashboard(
        panels=[kibana_panel],
        title="CPU Basic — Tier 2 AI-generated (test)",
    )

    if "--print-only" in sys.argv:
        print(json.dumps(dashboard, indent=2))
        return 0

    kibana_endpoint = os.environ.get("KIBANA_ENDPOINT", "")
    kibana_api_key = os.environ.get("KIBANA_API_KEY") or os.environ.get("ES_API_KEY", "")

    if not kibana_endpoint or not kibana_api_key:
        print("ERROR: KIBANA_ENDPOINT and ES_API_KEY must be set (source .env first).", file=sys.stderr)
        print("Run with --print-only to see the dashboard JSON without importing.", file=sys.stderr)
        return 2

    print("Importing to Kibana...")
    try:
        result = import_to_kibana(dashboard, kibana_endpoint, kibana_api_key)
    except Exception as e:
        print(f"✗ Import error: {e}", file=sys.stderr)
        return 1

    if result.get("success"):
        for obj in result.get("successResults", []):
            did = obj.get("destinationId", obj.get("id"))
            print(f"✓ Imported: {did}")
            print(f"  Open it at: {kibana_endpoint}/app/dashboards#/view/{did}")
        return 0
    else:
        for err in result.get("errors", []):
            print(f"✗ {err.get('error', {}).get('message', '')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
