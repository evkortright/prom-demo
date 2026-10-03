#!/usr/bin/env python3
"""
build_demo_dashboard.py — Build the reference demo Kibana dashboard.

Converts the three reference panels (CPU Busy, CPU Cores, CPU Basic)
and imports them as a single dashboard titled
"Node Exporter — Kibana (Converted)".
"""

import json
import os
import sys

# Add conversion-tool to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "conversion-tool"))
from convert_panel import convert_panel, build_kibana_dashboard, import_to_kibana


PANELS_DIR = os.path.join(os.path.dirname(__file__), "conversion-tool", "panels")
DASHBOARD_TITLE = "Node Exporter — Kibana (Converted)"

# Fixed ID (same idea as the Grafana reference dashboard's "uid":
# "prom-demo-cpu-busy") so re-running this script updates the SAME Kibana
# object in place instead of creating a new one each time. We tried
# find-by-title + delete first, but that depends on the Saved Objects
# _find API, which is unavailable on Elastic Cloud Serverless (and more
# broadly, the whole /api/saved_objects/* HTTP surface is deprecated across
# Elastic Stack) — overwrite-by-id is the supported path going forward.
DASHBOARD_ID = "prom-demo-reference-dashboard"

# Panels in display order with desired grid positions
PANEL_FILES = [
    {
        "file": "cpu_busy.json",
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 10},
    },
    {
        "file": "cpu_cores.json",
        "gridPos": {"x": 12, "y": 0, "w": 12, "h": 10},
    },
    {
        "file": "cpu_basic.json",
        "gridPos": {"x": 0, "y": 10, "w": 24, "h": 18},
    },
]


def main():
    kibana_endpoint = os.environ.get("KIBANA_ENDPOINT", "")
    kibana_api_key = os.environ.get("KIBANA_API_KEY") or os.environ.get("ES_API_KEY", "")

    if not kibana_endpoint or not kibana_api_key:
        print("ERROR: KIBANA_ENDPOINT and ES_API_KEY must be set.", file=sys.stderr)
        return 1

    kibana_panels = []

    for entry in PANEL_FILES:
        path = os.path.join(PANELS_DIR, entry["file"])
        with open(path) as f:
            grafana_panel = json.load(f)

        try:
            kibana_panel, esql_query = convert_panel(grafana_panel)
        except (ValueError, RuntimeError) as e:
            print(f"  ✗ {entry['file']}: {e}", file=sys.stderr)
            continue

        # Override grid position for the demo layout
        kibana_panel["gridData"].update(entry["gridPos"])
        kibana_panel["gridData"]["i"] = kibana_panel["panelIndex"]

        kibana_panels.append(kibana_panel)
        title = grafana_panel.get("title", entry["file"])
        print(f"  ✓ {title}")

    if not kibana_panels:
        print("ERROR: No panels converted successfully.", file=sys.stderr)
        return 1

    dashboard = build_kibana_dashboard(
        panels=kibana_panels,
        title=DASHBOARD_TITLE,
    )

    print(f"\nImporting {len(kibana_panels)} panel(s) to Kibana "
          f"(updating existing dashboard '{DASHBOARD_ID}' in place)...")
    try:
        result = import_to_kibana(
            dashboard, kibana_endpoint, kibana_api_key,
            object_id=DASHBOARD_ID, overwrite=True,
        )
        if result.get("success"):
            imported = result.get("successResults", [])
            for obj in imported:
                did = obj.get("destinationId", obj.get("id"))
                print(f"✓ Dashboard imported: {did}")
                print(f"\nOpen it at:")
                print(f"  {kibana_endpoint}/app/dashboards#/view/{did}")
        else:
            errors = result.get("errors", [])
            for err in errors:
                print(f"✗ {err.get('error', {}).get('message', '')}", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"✗ Import error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
