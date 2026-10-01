# Dashboard Conversion Tool — Architecture & Design

## Overview

This document captures the architecture, design decisions, and validated
patterns for converting Grafana/Prometheus dashboards to Kibana/Elasticsearch
equivalents using the prom-demo OTel normalization pipeline.

## Context

The prom-demo pipeline normalizes raw Prometheus metrics to OTel semantic
conventions before indexing into Elasticsearch. This creates a stable,
well-defined field namespace that makes dashboard conversion tractable.

Without normalization, dashboard conversion would require mapping both metric
names AND query semantics simultaneously. With normalization, metric names are
already translated — the conversion tool only needs to handle query semantics.

## Storage Architecture (validated 2026-10-01)

**Data stream:** `metrics-prometheusreceiver.otel-default`
**Index mode:** `time_series` (TSDS) — applied automatically by Elastic
Serverless via the `metrics-otel@template` built-in template.

This means:
- The `TS` source command is available for all metric queries
- `RATE()`, `DERIVATIVE()` and other time-series aggregate functions work
- Counter fields land as `counter_double` (required for `RATE()`)
- Dimension detection is automatic — no manual `time_series_dimension` config

**Critical field naming:** The OTel exporter creates two representations:
- `system.cpu.time` — the normalized attribute (type: `double`, for filtering)
- `metrics.system.cpu.time` — the metric value (type: `counter_double`, for RATE())

Dashboard queries must use `metrics.*` prefixed fields with `RATE()`.
Filter/dimension fields use the unprefixed names.

## Three-Tier Conversion Architecture

### Tier 1 — Deterministic Translation
Rule-based, no AI needed. Fast, reliable, auditable.

Covers:
- Field name translation (Prometheus → OTel via mapping table)
- Simple aggregation mapping (`sum()` → `SUM()`, `avg()` → `AVG()`)
- Label filter translation (`{label="value"}` → `WHERE label == "value"`)
- `rate()` / `irate()` → `RATE()` on `metrics.*` prefixed counter fields
- `FROM` → `TS` source command substitution
- Template variable substitution (`$node` → `WHERE service.instance.id == ?`)

### Tier 2 — AI-Assisted Translation
LLM proposes translation with confidence score and explanation.
Human reviews before accepting.

Covers:
- Complex multi-metric expressions
- `histogram_quantile()` → `PERCENTILE()` approximations
- `predict_linear()` → trend approximations
- `deriv()` → manual delta calculations
- Grafana template variables with complex logic
- Any expression where the AI detects semantic ambiguity

**Key principle:** AI never silently approximates. Every Tier 2 translation
includes an explanation of what changed and what the tradeoff is.

### Tier 3 — Human-Only
AI flags and explains. No automated translation attempted.

Covers:
- PromQL subqueries (no ES|QL equivalent)
- Recording rules (different workflow — use ES transforms)
- Cross-metric joins with no single-stream equivalent
- Highly custom expressions with no reasonable approximation

## Validated Conversion Patterns

### Pattern 1: Rate-based stat panel (validated)

**Source (Grafana/PromQL):**
```
Panel type: gauge
Query: 100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval])))
Instant: true, Reduction: lastNotNull
```

**Target (Kibana/ES|QL):**
```esql
TS metrics-prometheusreceiver.otel-default
| WHERE service.name == "node-exporter"
| WHERE `system.cpu.state` == "idle"
| STATS avg_idle_rate = AVG(RATE(`metrics.system.cpu.time`))
| EVAL cpu_busy_pct = ROUND(100 * (1 - avg_idle_rate), 1)
```

**Result:** 62.1% (validated against live data)

**Translation rules applied:**
- `FROM` → `TS`
- `node_cpu_seconds_total` → `metrics.system.cpu.time` (via mapping table)
- `{mode="idle"}` → `WHERE system.cpu.state == "idle"` (label normalization)
- `rate(...)` → `RATE(...)` (time-series function)
- `avg(...)` → `AVG(...)` (aggregation)
- `job="$job"` → `WHERE service.name == "node-exporter"` (variable resolution)
- `instance="$node"` → omitted for global average (intent-preserving)

**Tier:** 1 (fully deterministic)

## Field Mapping Reference

For query translation, use these field name mappings:

### Node Exporter
| Prometheus metric | ES|QL metric field | Filter field |
|---|---|---|
| `node_cpu_seconds_total` | `metrics.system.cpu.time` | `system.cpu.time` |
| `node_memory_total_bytes` | `metrics.system.memory.limit` | `system.memory.limit` |
| `node_filesystem_size_bytes` | `metrics.system.filesystem.capacity` | `system.filesystem.capacity` |
| `node_disk_read_bytes_total` | `metrics.system.disk.io.read` | `system.disk.io.read` |
| `node_network_receive_bytes_total` | `metrics.system.network.io.receive` | `system.network.io.receive` |
| `node_load1` | `metrics.system.cpu.load_average.1m` | `` `system.cpu.load_average.1m` `` |

### Label mappings
| Prometheus label | ES|QL field |
|---|---|
| `mode` | `system.cpu.state` |
| `mountpoint` | `system.filesystem.mountpoint` |
| `fstype` | `system.filesystem.type` |
| `device` | `system.device` |
| `job` | `service.name` |
| `instance` | `service.instance.id` |

## Kibana Panel Type Mapping

| Grafana type | Kibana equivalent |
|---|---|
| `gauge` | Metric vis / Gauge |
| `stat` | Metric vis |
| `timeseries` | Lens time series |
| `table` | Lens table |
| `bargauge` | Lens bar gauge |
| `heatmap` | Lens heatmap |

## Open Questions / Future Work

- Template variable handling: Grafana's `$node`, `$job` variables need
  a Kibana controls equivalent. Design TBD.
- `histogram_quantile()` → `PERCENTILE()` mapping validation needed.
- Node Exporter Full has ~200 panels — classify all by tier before building
  the full conversion tool.
- AI assist layer: define the prompt structure, confidence scoring, and
  review workflow.
- Consider adding a machine-readable mapping manifest (YAML/JSON) that
  the conversion tool reads, rather than hardcoding field mappings.

## Tier 1 Coverage — Node Exporter Full (as of 2026-10-01)

### Panel type coverage

| Grafana type | Kibana type | Status | Notes |
|---|---|---|---|
| `gauge` | `lnsGauge` | ✓ Complete | 5 panels in dashboard |
| `stat` | `lnsMetric` | ✓ Complete | 5 panels in dashboard |
| `timeseries` | `lnsXY` | ✓ Complete | 4 panels in dashboard |
| `row` | n/a | ✓ Skip | Section dividers, no query |
| `bargauge` | `lnsGauge` (horizontalBullet) | ⏳ Deferred | 1 panel — PSI metrics |

### bargauge / PSI metrics — deferred (Linux only)

The one `bargauge` panel in Node Exporter Full uses PSI (Pressure Stall
Information) metrics: `node_pressure_cpu_waiting_seconds_total`,
`node_pressure_memory_waiting_seconds_total`, etc.

PSI is Linux kernel 4.20+ only — not available on macOS Node Exporter.
Validation requires a real Linux system.

Plan: validate on Linux Mint USB boot. The `bargauge` Kibana equivalent
is `lnsGauge` with `shape: "horizontalBullet"` — already implemented for
the gauge panel type. The panel type support itself is quick to add.

Metrics to add to METRIC_MAP when Linux data is available:
- `node_pressure_cpu_waiting_seconds_total` → `system.cpu.pressure`
- `node_pressure_memory_waiting_seconds_total` → `system.memory.pressure`
- `node_pressure_io_waiting_seconds_total` → `system.disk.pressure`
- `node_pressure_irq_stalled_seconds_total` → `system.irq.pressure`

### PromQL pattern coverage

| Pattern | Example | Tier | Status |
|---|---|---|---|
| `scalar * (1 - agg(rate(...)))` | CPU Busy % | 1 | ✓ |
| `agg(rate(metric{filters}[interval]))` | CPU rate | 1 | ✓ |
| `count(count(metric) by (label))` | CPU Cores count | 1 | ✓ |
| `simple metric{filters}` | Current value | 1 | ✓ |
| `rate(metric{filters}[interval])` (no agg) | PSI pressure | 1 | ⏳ Deferred |
| `agg(sum without(label)(rate(...)))` | Busy IRQs | 2 | Planned |
| Complex exclusion `mode!='x',mode!='y'...` | Busy Other | 2 | Planned |
| Subqueries | n/a | 3 | Out of scope |
