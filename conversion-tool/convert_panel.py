#!/usr/bin/env python3
"""
convert_panel.py — Grafana panel → Kibana panel converter (Tier 1)

Converts a single Grafana panel JSON to a Kibana saved object JSON
using deterministic translation rules. Handles rate-based gauge/stat
panels backed by Node Exporter metrics.

Usage:
    python3 convert_panel.py --panel panel.json --output kibana_panel.json
    python3 convert_panel.py --panel panel.json  # prints to stdout

Supported panel types (Tier 1 — deterministic):
    - gauge  → lnsGauge (horizontalBullet)
    - stat   → lnsGauge (metric) [planned]

For panels requiring AI assistance (Tier 2) or human review (Tier 3),
the converter will flag them with a clear explanation.
"""

import argparse
import json
import re
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Field mapping table
# Prometheus metric name → (ES|QL metrics.* field, filter field)
# ---------------------------------------------------------------------------

METRIC_MAP: Dict[str, Tuple[str, str]] = {
    # Node Exporter — CPU
    "node_cpu_seconds_total":           ("metrics.system.cpu.time",                "system.cpu.time"),
    "node_load1":                       ("metrics.system.cpu.load_average.1m",     "`system.cpu.load_average.1m`"),
    "node_load5":                       ("metrics.system.cpu.load_average.5m",     "`system.cpu.load_average.5m`"),
    "node_load15":                      ("metrics.system.cpu.load_average.15m",    "`system.cpu.load_average.15m`"),

    # Node Exporter — Memory
    "node_memory_total_bytes":          ("metrics.system.memory.limit",            "system.memory.limit"),
    "node_memory_active_bytes":         ("metrics.system.memory.usage",            "system.memory.usage"),
    "node_memory_free_bytes":           ("metrics.system.memory.free",             "system.memory.free"),
    "node_memory_swap_total_bytes":     ("metrics.system.paging.limit",            "system.paging.limit"),
    "node_memory_swap_used_bytes":      ("metrics.system.paging.usage",            "system.paging.usage"),

    # Node Exporter — Filesystem
    "node_filesystem_size_bytes":       ("metrics.system.filesystem.capacity",     "system.filesystem.capacity"),
    "node_filesystem_avail_bytes":      ("metrics.system.filesystem.available",    "system.filesystem.available"),
    "node_filesystem_free_bytes":       ("metrics.system.filesystem.free",         "system.filesystem.free"),

    # Node Exporter — Disk
    "node_disk_read_bytes_total":       ("metrics.system.disk.io.read",            "system.disk.io.read"),
    "node_disk_written_bytes_total":    ("metrics.system.disk.io.write",           "system.disk.io.write"),
    "node_disk_reads_completed_total":  ("metrics.system.disk.operations.read",    "system.disk.operations.read"),
    "node_disk_writes_completed_total": ("metrics.system.disk.operations.write",   "system.disk.operations.write"),

    # Node Exporter — Network
    "node_network_receive_bytes_total":  ("metrics.system.network.io.receive",     "system.network.io.receive"),
    "node_network_transmit_bytes_total": ("metrics.system.network.io.transmit",    "system.network.io.transmit"),

    # prom-demo app — HTTP
    "http_requests_total":              ("metrics.http.server.request.count",      "http.server.request.count"),
    "http_request_duration_seconds":    ("metrics.http.server.request.duration",   "http.server.request.duration"),

    # prom-demo app — gRPC
    "grpc_server_handled_total":        ("metrics.rpc.server.request.count",       "rpc.server.request.count"),
    "grpc_server_handling_seconds":     ("metrics.rpc.server.duration",            "rpc.server.duration"),

    # prom-demo app — Database
    "db_connections_active":            ("metrics.db.client.connection.count",     "db.client.connection.count"),
    "db_query_duration_seconds":        ("metrics.db.client.operation.duration",   "db.client.operation.duration"),
    "db_errors_total":                  ("metrics.db.client.operation.errors",     "db.client.operation.errors"),
}

# Label name mappings
LABEL_MAP: Dict[str, str] = {
    "mode":       "system.cpu.state",
    "mountpoint": "system.filesystem.mountpoint",
    "fstype":     "system.filesystem.type",
    "device":     "system.device",
    "job":        "service.name",
    "instance":   "service.instance.id",
    "method":     "http.request.method",
    "status_code":"http.response.status_code",
    "grpc_service": "rpc.service",
    "grpc_method":  "rpc.method",
    "grpc_code":    "rpc.grpc.status_code",
}

# Grafana template variables → ES|QL WHERE clauses
# These are resolved to fixed values for the initial conversion.
# A full implementation would use Kibana controls/variables.
VARIABLE_MAP: Dict[str, str] = {
    "$job":  None,  # Resolved from metric context
    "$node": None,  # Omitted — produces global average
}

DATA_STREAM = "metrics-prometheusreceiver.otel-default"


# ---------------------------------------------------------------------------
# PromQL parser — minimal, handles the patterns we need
# ---------------------------------------------------------------------------

def parse_promql(expr: str) -> Dict[str, Any]:
    """
    Parse a PromQL expression into its components.
    Returns a dict describing the expression structure.
    Raises ValueError for unsupported patterns (Tier 2/3).
    """
    expr = expr.strip()

    # Pattern: scalar * (1 - avg(rate(metric{filters}[interval])))
    # Matches CPU Busy and similar inversion patterns
    inversion_pattern = re.compile(
        r'^(\d+)\s*\*\s*\(\s*1\s*-\s*(\w+)\s*\(\s*rate\s*\(\s*'
        r'(\w+)\s*\{([^}]*)\}\s*\[([^\]]+)\]\s*\)\s*\)\s*\)$'
    )
    m = inversion_pattern.match(expr)
    if m:
        scalar = int(m.group(1))
        agg_fn = m.group(2)
        metric = m.group(3)
        filters_str = m.group(4)
        return {
            "pattern": "scalar_times_one_minus_agg_rate",
            "scalar": scalar,
            "agg_fn": agg_fn,
            "metric": metric,
            "filters": parse_filters(filters_str),
        }

    # Pattern: avg(rate(metric{filters}[interval]))
    avg_rate_pattern = re.compile(
        r'^(\w+)\s*\(\s*rate\s*\(\s*(\w+)\s*\{([^}]*)\}\s*\[([^\]]+)\]\s*\)\s*\)$'
    )
    m = avg_rate_pattern.match(expr)
    if m:
        return {
            "pattern": "agg_rate",
            "agg_fn": m.group(1),
            "metric": m.group(2),
            "filters": parse_filters(m.group(3)),
        }

    # Pattern: simple metric with filters (no rate)
    simple_pattern = re.compile(
        r'^(\w+)\s*\{([^}]*)\}$'
    )
    m = simple_pattern.match(expr)
    if m:
        return {
            "pattern": "simple",
            "metric": m.group(1),
            "filters": parse_filters(m.group(2)),
        }

    raise ValueError(
        f"Unsupported PromQL pattern — requires Tier 2 (AI-assisted) translation:\n  {expr}"
    )


def parse_filters(filters_str: str) -> List[Dict[str, str]]:
    """Parse PromQL label filters into a list of dicts."""
    filters = []
    if not filters_str.strip():
        return filters
    for part in filters_str.split(","):
        part = part.strip()
        for op in ["!=", "=~", "!~", "="]:
            if op in part:
                key, value = part.split(op, 1)
                filters.append({
                    "label": key.strip(),
                    "op": op,
                    "value": value.strip().strip('"').strip("'"),
                })
                break
    return filters


# ---------------------------------------------------------------------------
# ES|QL query builder
# ---------------------------------------------------------------------------

def translate_filters_to_where(
    filters: List[Dict[str, str]],
    service_name: Optional[str] = None
) -> List[str]:
    """Convert PromQL label filters to ES|QL WHERE clauses."""
    clauses = []

    # Add service.name filter from job variable or metric context
    if service_name:
        clauses.append(f'service.name == "{service_name}"')

    for f in filters:
        label = f["label"]
        value = f["value"]
        op = f["op"]

        # Skip template variables
        if value.startswith("$"):
            continue

        # Map label name
        esql_field = LABEL_MAP.get(label, label)

        # Wrap field name in backticks if it contains dots or starts with number
        if "." in esql_field or esql_field[0].isdigit():
            esql_field = f"`{esql_field}`"

        if op == "=":
            clauses.append(f'{esql_field} == "{value}"')
        elif op == "!=":
            clauses.append(f'{esql_field} != "{value}"')

    return clauses


def build_esql_query(parsed: Dict[str, Any]) -> Tuple[str, str]:
    """
    Build an ES|QL query from a parsed PromQL expression.
    Returns (query_string, result_column_name).
    """
    metric = parsed["metric"]
    filters = parsed["filters"]
    pattern = parsed["pattern"]

    # Look up metric field
    if metric not in METRIC_MAP:
        raise ValueError(
            f"Unknown metric '{metric}' — not in mapping table. "
            f"Add it to METRIC_MAP or use Tier 2 (AI-assisted) translation."
        )

    metrics_field, _ = METRIC_MAP[metric]

    # Resolve service name from job filter
    service_name = None
    for f in filters:
        if f["label"] == "job" and not f["value"].startswith("$"):
            service_name = f["value"]

    where_clauses = translate_filters_to_where(filters, service_name)

    lines = [f"TS {DATA_STREAM}"]
    # Scope to recent data to match Grafana's $__rate_interval behavior.
    # Grafana defaults to ~5 minutes for instant queries on stat/gauge panels.
    lines.append("| WHERE @timestamp >= NOW() - 5 minutes")
    for clause in where_clauses:
        lines.append(f"| WHERE {clause}")

    if pattern == "scalar_times_one_minus_agg_rate":
        scalar = parsed["scalar"]
        agg_fn = parsed["agg_fn"].upper()
        result_col = "result_pct"
        lines.append(
            f"| STATS agg_rate = {agg_fn}(RATE(`{metrics_field}`))"
        )
        lines.append(
            f"| EVAL {result_col} = ROUND({scalar} * (1 - agg_rate), 1)"
        )

    elif pattern == "agg_rate":
        agg_fn = parsed["agg_fn"].upper()
        result_col = "result_value"
        lines.append(
            f"| STATS {result_col} = {agg_fn}(RATE(`{metrics_field}`))"
        )

    elif pattern == "simple":
        result_col = "result_value"
        lines.append(
            f"| STATS {result_col} = MAX(`{metrics_field}`)"
        )

    return "\n".join(lines), result_col


# ---------------------------------------------------------------------------
# Kibana panel builder
# ---------------------------------------------------------------------------

def translate_thresholds(grafana_thresholds: Dict) -> Dict:
    """
    Translate Grafana threshold steps to Kibana palette stops.
    Grafana: [{color, value}, ...] where first step has no value (= 0)
    Kibana:  colorStops [{color, stop}, ...] and stops [{color, stop}, ...]
    """
    steps = grafana_thresholds.get("steps", [])
    if len(steps) < 2:
        return None

    # Default colors matching Grafana's green/orange/red
    color_map = {
        "rgba(50, 172, 45, 0.97)":  "#24c292",
        "rgba(237, 129, 40, 0.89)": "#EAAE01",
        "rgba(245, 54, 54, 0.9)":   "#ea1519",
        "green":  "#24c292",
        "yellow": "#EAAE01",
        "orange": "#EAAE01",
        "red":    "#ea1519",
    }

    def map_color(c: str) -> str:
        return color_map.get(c, c)

    color_stops = []
    stops = []

    for i, step in enumerate(steps):
        color = map_color(step.get("color", "#24c292"))
        start = step.get("value", 0) or 0
        if i + 1 < len(steps):
            end = steps[i + 1].get("value", 100)
        else:
            end = 100

        color_stops.append({"color": color, "stop": start})
        stops.append({"color": color, "stop": end})

    return {
        "name": "custom",
        "type": "palette",
        "params": {
            "steps": len(steps) + 1,
            "name": "custom",
            "reverse": False,
            "rangeType": "number",
            "rangeMin": 0,
            "rangeMax": None,
            "progression": "fixed",
            "stops": stops,
            "colorStops": color_stops,
            "continuity": "above",
            "maxSteps": 5,
        }
    }


def build_kibana_panel(
    grafana_panel: Dict,
    esql_query: str,
    result_col: str,
) -> Dict:
    """Build a Kibana dashboard panel object from a Grafana panel."""

    panel_id = str(uuid.uuid4())
    layer_id = str(uuid.uuid4())
    col_id = str(uuid.uuid4())
    view_id = str(uuid.uuid4())

    title = grafana_panel.get("title", "Converted Panel")
    field_config = grafana_panel.get("fieldConfig", {}).get("defaults", {})
    thresholds = field_config.get("thresholds", {})
    panel_min = field_config.get("min", 0)
    panel_max = field_config.get("max", 100)

    palette = translate_thresholds(thresholds)

    # Grid position — preserve Grafana layout or use default
    grid = grafana_panel.get("gridPos", {"x": 0, "y": 0, "w": 24, "h": 15})

    panel = {
        "type": "vis",
        "embeddableConfig": {
            "attributes": {
                "title": title,
                "references": [],
                "state": {
                    "datasourceStates": {
                        "textBased": {
                            "layers": {
                                layer_id: {
                                    "index": view_id,
                                    "query": {"esql": esql_query},
                                    "columns": [
                                        {
                                            "columnId": col_id,
                                            "fieldName": result_col,
                                            "label": title,
                                            "customLabel": True,
                                            "meta": {
                                                "type": "number",
                                                "esType": "double",
                                            },
                                            "inMetricDimension": True,
                                        }
                                    ],
                                    "timeField": "@timestamp",
                                }
                            },
                            "indexPatternRefs": [
                                {
                                    "id": view_id,
                                    "title": DATA_STREAM,
                                    "timeField": "@timestamp",
                                }
                            ],
                        }
                    },
                    "filters": [],
                    "visualization": {
                        "shape": "horizontalBullet",
                        "layerId": layer_id,
                        "layerType": "data",
                        "ticksPosition": "bands",
                        "labelMajorMode": "auto",
                        "colorMode": "palette",
                        "palette": palette,
                        "metricAccessor": col_id,
                        "minAccessor": str(panel_min),
                        "maxAccessor": str(panel_max),
                    },
                    "adHocDataViews": {
                        view_id: {
                            "id": view_id,
                            "title": DATA_STREAM,
                            "timeFieldName": "@timestamp",
                            "sourceFilters": [],
                            "type": "esql",
                            "fieldFormats": {},
                            "runtimeFieldMap": {},
                            "allowNoIndex": False,
                            "name": DATA_STREAM,
                            "allowHidden": False,
                            "managed": False,
                        }
                    },
                    "query": {"esql": esql_query},
                },
                "visualizationType": "lnsGauge",
                "version": 2,
            },
            "drilldowns": [],
        },
        "panelIndex": panel_id,
        "gridData": {
            "x": grid.get("x", 0),
            "y": grid.get("y", 0),
            "w": grid.get("w", 24),
            "h": grid.get("h", 15),
            "i": panel_id,
        },
    }

    return panel


def build_kibana_dashboard(
    panels: List[Dict],
    title: str = "Converted Dashboard",
) -> Dict:
    """Wrap panels in a Kibana saved object envelope."""
    return {
        "attributes": {
            "title": title,
            "description": "Converted from Grafana/Prometheus dashboard",
            "panelsJSON": json.dumps(panels),
            "optionsJSON": json.dumps({
                "autoApplyFilters": True,
                "hidePanelTitles": False,
                "hidePanelBorders": False,
                "useMargins": True,
                "syncColors": False,
                "syncTooltips": False,
                "syncCursor": True,
            }),
            "timeFrom": "now-1h",
            "timeTo": "now",
            "timeRestore": True,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({
                    "query": {"query": "", "language": "kuery"}
                })
            },
        },
        "type": "dashboard",
        "references": [],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def convert_panel(grafana_panel: Dict) -> Dict:
    """
    Convert a single Grafana panel to a Kibana panel.
    Returns the Kibana panel object.
    Raises ValueError for unsupported patterns.
    """
    panel_type = grafana_panel.get("type", "unknown")
    title = grafana_panel.get("title", "Unknown")
    targets = grafana_panel.get("targets", [])

    if not targets:
        raise ValueError(f"Panel '{title}' has no query targets.")

    if len(targets) > 1:
        raise ValueError(
            f"Panel '{title}' has {len(targets)} targets — "
            f"multi-target panels require Tier 2 (AI-assisted) translation."
        )

    expr = targets[0].get("expr", "")
    if not expr:
        raise ValueError(f"Panel '{title}' has an empty query expression.")

    # Parse PromQL
    parsed = parse_promql(expr)

    # Build ES|QL
    esql_query, result_col = build_esql_query(parsed)

    # Build Kibana panel
    kibana_panel = build_kibana_panel(grafana_panel, esql_query, result_col)

    return kibana_panel, esql_query


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Grafana panel JSON to a Kibana panel."
    )
    parser.add_argument(
        "--panel",
        required=True,
        help="Path to Grafana panel JSON file",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path for Kibana saved object JSON (default: stdout)",
    )
    parser.add_argument(
        "--query-only",
        action="store_true",
        help="Print only the ES|QL query, not the full Kibana panel",
    )
    args = parser.parse_args()

    with open(args.panel) as f:
        grafana_panel = json.load(f)

    try:
        kibana_panel, esql_query = convert_panel(grafana_panel)
    except ValueError as e:
        print(f"CONVERSION FAILED: {e}", file=sys.stderr)
        return 1

    if args.query_only:
        print(esql_query)
        return 0

    dashboard = build_kibana_dashboard(
        panels=[kibana_panel],
        title=f"Converted: {grafana_panel.get('title', 'Panel')}",
    )

    output = json.dumps(dashboard, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Written to {args.output}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
