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
import io
import json
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


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
    "cpu":        "system.cpu.logical_number",
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

    # Pattern: count(count(metric{filters}) by (label)) — cardinality count
    # Used to count distinct label values (e.g. CPU cores, network interfaces)
    nested_count_pattern = re.compile(
        r'^count\s*\(\s*count\s*\(\s*(\w+)\s*\{([^}]*)\}\s*\)\s*by\s*\(([^)]+)\)\s*\)$'
    )
    m = nested_count_pattern.match(expr)
    if m:
        return {
            "pattern": "nested_count",
            "metric": m.group(1),
            "filters": parse_filters(m.group(2)),
            "count_by": m.group(3).strip(),
        }

    # Pattern: agg(rate(metric{filters}[interval])) — range/time series mode
    # Same as agg_rate but explicitly for time series (range: true targets)
    range_rate_pattern = re.compile(
        r'^(\w+)\s*\(\s*rate\s*\(\s*(\w+)\s*\{([^}]*)\}\s*\[([^\]]+)\]\s*\)\s*\)$'
    )
    m = range_rate_pattern.match(expr)
    if m:
        return {
            "pattern": "agg_rate",
            "agg_fn": m.group(1),
            "metric": m.group(2),
            "filters": parse_filters(m.group(3)),
            "range": True,
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

    elif pattern == "nested_count":
        count_by = parsed["count_by"]
        # Map the Prometheus label to OTel field
        otel_field = LABEL_MAP.get(count_by, count_by)
        if "." in otel_field:
            otel_field_esql = f"`{otel_field}`"
        else:
            otel_field_esql = otel_field
        # Need the filter field (not metrics.) for COUNT_DISTINCT
        _, filter_field = METRIC_MAP[metric]
        result_col = f"{count_by}_count"
        lines.append(
            f"| WHERE {filter_field} IS NOT NULL"
        )
        lines.append(
            f"| STATS {result_col} = COUNT_DISTINCT({otel_field_esql})"
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
            "rangeMin": color_stops[0]["stop"],
            "rangeMax": stops[-1]["stop"],
            "progression": "fixed",
            "stops": stops,
            "colorStops": color_stops,
            "continuity": "above",
            "maxSteps": 5,
        }
    }


def build_esql_timeseries_query(
    targets: List[Dict],
    bucket_seconds: int = 30,
    panel_title: str = "Unknown Panel",
    allow_tier2: bool = True,
) -> tuple:
    """
    Build an ES|QL time series query from multiple PromQL targets.

    Merges compatible targets (same metric, same agg function, different
    mode filters) into a single query grouped by time bucket and dimension.

    If any target can't be parsed deterministically (regex filters, label
    exclusions, "sum without" aggregations) and allow_tier2 is True, the
    WHOLE panel is escalated to Tier 2 (AI-assisted translation) rather than
    silently dropping the unsupported targets — see tier2_engine.py and
    docs/conversion-tool/architecture.md for why a unified query is required
    rather than per-target translation.

    Returns (query_string, metric_col, bucket_col, dimension_col, series_map)
    where series_map maps dimension values to legend labels. For a Tier 2
    result, metric_col/dimension_col are always "metric_value"/"category"
    (standardized so this caller doesn't need to parse AI-chosen names) and
    series_map is empty (Lens derives the legend directly from the category
    column's values, which are already the correct legend names).
    """
    # Collect all parsed targets
    parsed_targets = []
    for t in targets:
        expr = t.get("expr", "").strip()
        legend = t.get("legendFormat", "")
        try:
            parsed = parse_promql(expr)
            parsed["legend"] = legend
            parsed_targets.append(parsed)
        except ValueError:
            # Flag unsupported targets for Tier 2
            parsed_targets.append({
                "pattern": "unsupported",
                "expr": expr,
                "legend": legend,
            })

    # Separate supported from unsupported
    supported = [t for t in parsed_targets if t["pattern"] != "unsupported"]
    unsupported = [t for t in parsed_targets if t["pattern"] == "unsupported"]

    if unsupported:
        for u in unsupported:
            print(
                f"  ⚠ Tier 2 required for: {u['expr'][:60]}... "
                f"(legend: {u['legend']})",
                file=sys.stderr
            )

        if allow_tier2:
            print(
                f"  → Escalating panel '{panel_title}' to Tier 2 "
                f"(AI-assisted translation)...",
                file=sys.stderr
            )
            # Deferred import: tier2_engine imports from this module at load
            # time, so importing it at module level here would be circular.
            # By call time this module is fully loaded, so it's safe.
            from tier2_engine import translate_tier2

            result = translate_tier2(
                panel_title=panel_title,
                targets=targets,  # ALL targets, not just the unsupported ones —
                                  # the unified query must cover every series
                bucket_seconds=bucket_seconds,
            )
            issues = result.get("validation_issues", [])
            if issues:
                raise ValueError(
                    f"Tier 2 translation for panel '{panel_title}' failed "
                    f"validation after {result.get('attempt', '?')} attempt(s) "
                    f"— needs Tier 3 (human) review:\n"
                    + "\n".join(f"  - {i}" for i in issues)
                    + f"\n\nLast attempted ES|QL:\n{result.get('esql', '(none)')}"
                )

            print(
                f"  ✓ Tier 2 succeeded (attempt {result.get('attempt')}, "
                f"confidence: {result.get('confidence')})",
                file=sys.stderr
            )
            bucket_col = f"BUCKET(@timestamp, {bucket_seconds} seconds)"
            return result["esql"], "metric_value", bucket_col, "category", {}

        # --tier1-only: keep the old partial behavior (unsupported targets
        # silently dropped) for debugging/comparison purposes only.
        print(
            f"  (--tier1-only set: proceeding with only the "
            f"{len(supported)} supported target(s), dropping the rest)",
            file=sys.stderr
        )

    if not supported:
        raise ValueError("No supported targets found — all require Tier 2 translation.")

    # For now handle agg_rate targets that share the same metric
    # Find the primary metric and dimension label
    metrics = set(t.get("metric") for t in supported if t.get("metric"))
    if len(metrics) > 1:
        raise ValueError(
            f"Multi-metric time series panels require Tier 2 translation. "
            f"Found metrics: {metrics}"
        )

    metric = metrics.pop()
    if metric not in METRIC_MAP:
        raise ValueError(f"Unknown metric '{metric}' — not in mapping table.")

    metrics_field, _ = METRIC_MAP[metric]

    # Find which label varies across targets (the dimension)
    # Compare filter sets to find the varying label
    dimension_label = None
    dimension_values = []
    series_map = {}  # dimension_value → legend label

    for t in supported:
        filters = t.get("filters", [])
        for f in filters:
            if not f["value"].startswith("$"):
                label = f["label"]
                value = f["value"]
                if label not in ("job", "instance"):
                    if dimension_label is None:
                        dimension_label = label
                    if value not in dimension_values:
                        dimension_values.append(value)
                    series_map[value] = t.get("legend", value)

    # Map dimension label to OTel field
    dimension_field = LABEL_MAP.get(dimension_label, dimension_label)
    if "." in dimension_field:
        dimension_field_esql = f"`{dimension_field}`"
    else:
        dimension_field_esql = dimension_field

    # Get service name from job filter
    service_name = None
    for t in supported:
        for f in t.get("filters", []):
            if f["label"] == "job" and not f["value"].startswith("$"):
                service_name = f["value"]
                break

    # Build the query
    bucket_col = f"BUCKET(@timestamp, {bucket_seconds} seconds)"
    metric_col = metric.replace("_total", "").replace("node_", "system_")
    # Simplify column name
    metric_col = "cpu_rate"

    lines = [f"TS {DATA_STREAM}"]
    lines.append(f"| WHERE @timestamp >= NOW() - 1 hour")
    if service_name:
        lines.append(f'| WHERE service.name == "{service_name}"')
    if dimension_values:
        if len(dimension_values) == 1:
            lines.append(
                f'| WHERE {dimension_field_esql} == "{dimension_values[0]}"'
            )
        else:
            values_str = ", ".join(f'"{v}"' for v in dimension_values)
            lines.append(
                f"| WHERE {dimension_field_esql} IN ({values_str})"
            )

    agg_fn = supported[0].get("agg_fn", "avg").upper()
    lines.append(
        f"| STATS {metric_col} = {agg_fn}(RATE(`{metrics_field}`)) "
        f"BY {bucket_col}, {dimension_field_esql}"
    )

    return "\n".join(lines), metric_col, bucket_col, dimension_field, series_map


def build_kibana_timeseries_panel(
    grafana_panel: Dict,
    esql_query: str,
    metric_col: str,
    bucket_col: str,
    dimension_field: str,
    series_map: Dict[str, str],
) -> Dict:
    """Build a Kibana lnsXY time series panel from a Grafana timeseries panel."""

    panel_id = str(uuid.uuid4())
    layer_id = str(uuid.uuid4())
    view_id = str(uuid.uuid4())
    metric_col_id = str(uuid.uuid4())
    bucket_col_id = f"BUCKET(@timestamp, 30 seconds)"
    dimension_col_id = str(uuid.uuid4())

    title = grafana_panel.get("title", "Converted Panel")
    grid = grafana_panel.get("gridPos", {"x": 0, "y": 0, "w": 24, "h": 15})

    # Determine series type from Grafana drawStyle
    custom = grafana_panel.get("fieldConfig", {}).get("defaults", {}).get("custom", {})
    draw_style = custom.get("drawStyle", "line")
    fill_opacity = custom.get("fillOpacity", 0)
    stacking_mode = custom.get("stacking", {}).get("mode", "none")

    if stacking_mode == "percent":
        series_type = "area_percentage_stacked"
    elif fill_opacity > 0:
        series_type = "area"
    else:
        series_type = "line"

    # Build columns
    columns = [
        {
            "columnId": metric_col_id,
            "fieldName": metric_col,
            "label": metric_col,
            "customLabel": False,
            "meta": {"type": "number", "esType": "double"},
            "inMetricDimension": True,
        },
        {
            "columnId": bucket_col_id,
            "fieldName": bucket_col_id,
            "label": bucket_col_id,
            "customLabel": False,
            "meta": {
                "type": "date",
                "esType": "date",
                "esMeta": {"bucket": {"unit": "second", "interval": 30}},
            },
        },
        {
            "columnId": dimension_col_id,
            "fieldName": dimension_field,
            "label": dimension_field,
            "customLabel": False,
            "meta": {"type": "string", "esType": "keyword"},
        },
    ]

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
                                    "columns": columns,
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
                        "legend": {
                            "isVisible": True,
                            "position": "bottom",
                        },
                        "valueLabels": "hide",
                        "fittingFunction": "Linear",
                        "axisTitlesVisibilitySettings": {
                            "x": True, "yLeft": True, "yRight": True
                        },
                        "tickLabelsVisibilitySettings": {
                            "x": True, "yLeft": True, "yRight": True
                        },
                        "gridlinesVisibilitySettings": {
                            "x": True, "yLeft": True, "yRight": True
                        },
                        "preferredSeriesType": series_type,
                        "layers": [
                            {
                                "layerId": layer_id,
                                "seriesType": series_type,
                                "xAccessor": bucket_col_id,
                                "accessors": [metric_col_id],
                                "splitAccessor": dimension_col_id,
                                "layerType": "data",
                                "colorMapping": {
                                    "assignments": [],
                                    "specialAssignments": [
                                        {
                                            "rules": [{"type": "other"}],
                                            "color": {"type": "loop"},
                                            "touched": False,
                                        }
                                    ],
                                    "paletteId": "elastic_line_optimized",
                                    "colorMode": {"type": "categorical"},
                                },
                            }
                        ],
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
                "visualizationType": "lnsXY",
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


def build_kibana_metric_panel(
    grafana_panel: Dict,
    esql_query: str,
    result_col: str,
) -> Dict:
    """Build a Kibana lnsMetric panel (stat — big number display)."""

    panel_id = str(uuid.uuid4())
    layer_id = str(uuid.uuid4())
    col_id = str(uuid.uuid4())
    view_id = str(uuid.uuid4())

    title = grafana_panel.get("title", "Converted Panel")
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
                                                "esType": "long",
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
                        "layerId": layer_id,
                        "layerType": "data",
                        "density": "default",
                        "metricAccessor": col_id,
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
                "visualizationType": "lnsMetric",
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

def convert_panel(grafana_panel: Dict, allow_tier2: bool = True) -> tuple:
    """
    Convert a single Grafana panel to a Kibana panel.
    Returns (kibana_panel, esql_query).
    Raises ValueError for unsupported patterns (or Tier 2 validation failures
    needing Tier 3 / human review, if allow_tier2 is True).
    """
    panel_type = grafana_panel.get("type", "unknown")
    title = grafana_panel.get("title", "Unknown")
    targets = grafana_panel.get("targets", [])

    if not targets:
        raise ValueError(f"Panel '{title}' has no query targets.")

    # --- Time series panel ---
    if panel_type == "timeseries":
        esql_query, metric_col, bucket_col, dimension_field, series_map = \
            build_esql_timeseries_query(
                targets, panel_title=title, allow_tier2=allow_tier2
            )
        kibana_panel = build_kibana_timeseries_panel(
            grafana_panel, esql_query, metric_col,
            bucket_col, dimension_field, series_map,
        )
        return kibana_panel, esql_query

    # --- Gauge panel ---
    if panel_type == "gauge":
        if len(targets) > 1:
            raise ValueError(
                f"Panel '{title}' has {len(targets)} targets — "
                f"multi-target gauge panels require Tier 2 translation."
            )
        expr = targets[0].get("expr", "")
        if not expr:
            raise ValueError(f"Panel '{title}' has an empty query expression.")
        parsed = parse_promql(expr)
        esql_query, result_col = build_esql_query(parsed)
        kibana_panel = build_kibana_panel(grafana_panel, esql_query, result_col)
        return kibana_panel, esql_query

    # --- Stat panel → lnsMetric (big number display) ---
    if panel_type == "stat":
        if len(targets) > 1:
            raise ValueError(
                f"Panel '{title}' has {len(targets)} targets — "
                f"multi-target stat panels require Tier 2 translation."
            )
        expr = targets[0].get("expr", "")
        if not expr:
            raise ValueError(f"Panel '{title}' has an empty query expression.")
        parsed = parse_promql(expr)
        esql_query, result_col = build_esql_query(parsed)
        kibana_panel = build_kibana_metric_panel(grafana_panel, esql_query, result_col)
        return kibana_panel, esql_query

    raise ValueError(
        f"Panel type '{panel_type}' is not yet supported. "
        f"Supported types: gauge, stat, timeseries."
    )


def import_to_kibana(
    dashboard: Dict,
    kibana_endpoint: str,
    kibana_api_key: str,
    object_id: Optional[str] = None,
    overwrite: bool = False,
) -> Dict:
    """
    Import a dashboard to Kibana via the saved objects import API.
    Returns the API response as a dict.

    By default (object_id=None, overwrite=False) this creates a brand-new
    dashboard object every call — fine for one-off conversions, but repeated
    runs pile up duplicates with different random IDs.

    Pass a fixed object_id + overwrite=True (e.g. for a reference demo
    dashboard that gets rebuilt repeatedly) to update the SAME saved object
    in place instead. This is the preferred approach over finding and
    deleting old copies: the Saved Objects _find API this project initially
    used for that is unavailable on Elastic Cloud Serverless, and more
    broadly the whole /api/saved_objects/* HTTP surface is deprecated and
    being phased out in favor of overwrite-by-id imports and internal
    plugin APIs.
    """
    if not HAS_REQUESTS:
        raise RuntimeError("'requests' is required for --import. Install with: pip install requests")

    # The import API expects ndjson — one JSON object per line
    ndjson_obj = {
        "id": object_id or str(uuid.uuid4()),
        "type": "dashboard",
        "attributes": dashboard["attributes"],
        "references": dashboard.get("references", []),
    }
    ndjson_content = json.dumps(ndjson_obj) + "\n"
    ndjson_file = io.BytesIO(ndjson_content.encode("utf-8"))

    mode_param = "overwrite=true" if overwrite else "createNewCopies=true"
    url = f"{kibana_endpoint}/api/saved_objects/_import?{mode_param}"
    headers = {
        "Authorization": f"ApiKey {kibana_api_key}",
        "kbn-xsrf": "true",
    }

    response = requests.post(
        url,
        headers=headers,
        files={"file": ("dashboard.ndjson", ndjson_file, "application/ndjson")},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def find_saved_objects_by_title(
    object_type: str,
    title: str,
    kibana_endpoint: str,
    kibana_api_key: str,
) -> List[Dict]:
    """
    Find saved objects of a given type with an EXACT title match.
    Kibana's _find API does fuzzy/tokenized search, so results are filtered
    client-side to exact matches — we never want to delete something just
    because it shares a word with the target title.
    Returns a list of {"id": ..., "type": ...} dicts.
    """
    if not HAS_REQUESTS:
        raise RuntimeError("'requests' is required. Install with: pip install requests")

    headers = {
        "Authorization": f"ApiKey {kibana_api_key}",
        "kbn-xsrf": "true",
    }
    # NOTE: deliberately not using the "search"/"search_fields" params here —
    # Kibana's _find uses a simple query syntax where characters like
    # parentheses (common in dashboard titles, e.g. "... (Converted)") are
    # treated as query syntax and cause a 400 Bad Request. Fetching all
    # objects of this type and filtering by exact title match client-side
    # sidesteps that entirely; we wanted an exact match anyway.
    matches = []
    page = 1
    per_page = 100
    while True:
        response = requests.get(
            f"{kibana_endpoint}/api/saved_objects/_find",
            headers=headers,
            params={"type": object_type, "per_page": per_page, "page": page},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"{response.status_code} error from _find: {response.text}"
            )
        data = response.json()
        saved_objects = data.get("saved_objects", [])

        matches.extend(
            {"id": obj["id"], "type": obj["type"]}
            for obj in saved_objects
            if obj.get("attributes", {}).get("title", "") == title
        )

        if len(saved_objects) < per_page:
            break
        page += 1

    return matches


def delete_saved_object(
    object_type: str,
    object_id: str,
    kibana_endpoint: str,
    kibana_api_key: str,
) -> None:
    """Delete a single saved object by type and id."""
    if not HAS_REQUESTS:
        raise RuntimeError("'requests' is required. Install with: pip install requests")

    headers = {
        "Authorization": f"ApiKey {kibana_api_key}",
        "kbn-xsrf": "true",
    }
    response = requests.delete(
        f"{kibana_endpoint}/api/saved_objects/{object_type}/{object_id}",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()


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
    parser.add_argument(
        "--import",
        action="store_true",
        dest="do_import",
        help="Import the converted dashboard directly into Kibana",
    )
    parser.add_argument(
        "--kibana-endpoint",
        default=os.environ.get("KIBANA_ENDPOINT", ""),
        help="Kibana endpoint URL (or set KIBANA_ENDPOINT env var)",
    )
    parser.add_argument(
        "--kibana-api-key",
        default=os.environ.get("KIBANA_API_KEY", ""),
        help="Kibana API key (or set KIBANA_API_KEY env var)",
    )
    parser.add_argument(
        "--tier1-only",
        action="store_true",
        help="Disable Tier 2 escalation — unsupported targets are dropped "
             "with a warning instead of being sent to the AI engine. "
             "Useful for debugging or comparing Tier 1 vs Tier 2 output.",
    )
    args = parser.parse_args()

    with open(args.panel) as f:
        grafana_panel = json.load(f)

    try:
        kibana_panel, esql_query = convert_panel(
            grafana_panel, allow_tier2=not args.tier1_only
        )
    except (ValueError, RuntimeError) as e:
        print(f"CONVERSION FAILED: {e}", file=sys.stderr)
        return 1

    if args.query_only:
        print(esql_query)
        return 0

    dashboard = build_kibana_dashboard(
        panels=[kibana_panel],
        title=f"Converted: {grafana_panel.get('title', 'Panel')}",
    )

    if args.do_import:
        if not args.kibana_endpoint:
            print("ERROR: KIBANA_ENDPOINT not set. Use --kibana-endpoint or export KIBANA_ENDPOINT=...", file=sys.stderr)
            return 2
        if not args.kibana_api_key:
            print("ERROR: KIBANA_API_KEY not set. Use --kibana-api-key or export KIBANA_API_KEY=...", file=sys.stderr)
            return 2
        try:
            result = import_to_kibana(dashboard, args.kibana_endpoint, args.kibana_api_key)
            if result.get("success"):
                imported = result.get("successResults", [])
                print(f"✓ Imported successfully ({len(imported)} object(s))")
                for obj in imported:
                    obj_id = obj.get("destinationId", obj.get("id"))
                    print(f"  {obj.get('type')}: {obj_id}")
                    if obj.get("type") == "dashboard":
                        print(f"  Open it at: {args.kibana_endpoint}/app/dashboards#/view/{obj_id}")
            else:
                errors = result.get("errors", [])
                print(f"✗ Import failed ({len(errors)} error(s))", file=sys.stderr)
                for err in errors:
                    print(f"  {err.get('type')}: {err.get('error', {}).get('message', '')}", file=sys.stderr)
                return 1
        except Exception as e:
            print(f"✗ Import error: {e}", file=sys.stderr)
            return 1
        return 0

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
