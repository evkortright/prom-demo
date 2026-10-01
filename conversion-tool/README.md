# Dashboard Conversion Tool

Converts Grafana/Prometheus dashboard panels to Kibana/ES|QL equivalents
using the prom-demo OTel normalization pipeline as the translation layer.

## How it works

The converter operates in three tiers:

**Tier 1 — Deterministic** (this tool): Rule-based translation using the
OTel field mapping table. Fast, reliable, no AI needed. Covers rate-based
gauge and stat panels with standard PromQL patterns.

**Tier 2 — AI-assisted** (planned): LLM proposes translations for complex
PromQL expressions with an explanation of any approximations made.

**Tier 3 — Human-only** (flagged): Patterns with no reasonable automated
equivalent (subqueries, recording rules). The tool flags these clearly.

## Requirements

```bash
pip install requests  # only needed for future ES validation feature
```

Python 3.9+. No other dependencies.

## Usage

```bash
# Convert a panel and print the Kibana saved object JSON
python3 convert_panel.py --panel panels/cpu_busy.json

# Print only the ES|QL query (useful for validation in Kibana Dev Tools)
python3 convert_panel.py --panel panels/cpu_busy.json --query-only

# Write output to a file
python3 convert_panel.py --panel panels/cpu_busy.json --output out.json
```

## Adding a panel to convert

1. Export the panel JSON from Grafana (panel menu → Inspect → Panel JSON)
2. Save it to `panels/<panel_name>.json`
3. Run the converter and validate the ES|QL query in Kibana Dev Tools
4. If it works, the output JSON can be imported into Kibana via:
   `POST kbn:/api/saved_objects/dashboard`

## Supported PromQL patterns (Tier 1)

| Pattern | Example |
|---|---|
| `scalar * (1 - agg(rate(metric{filters}[interval])))` | CPU Busy |
| `agg(rate(metric{filters}[interval]))` | Network throughput |
| `metric{filters}` | Current gauge value |

## Field mapping

The converter uses the OTel normalization mapping table defined in
`otelcol/otelcol-config.yaml`. Key mappings for Node Exporter:

| Prometheus metric | ES|QL field |
|---|---|
| `node_cpu_seconds_total` | `metrics.system.cpu.time` |
| `node_memory_total_bytes` | `metrics.system.memory.limit` |
| `node_filesystem_size_bytes` | `metrics.system.filesystem.capacity` |
| `node_disk_read_bytes_total` | `metrics.system.disk.io.read` |
| `node_network_receive_bytes_total` | `metrics.system.network.io.receive` |
| `node_load1` | `` `metrics.system.cpu.load_average.1m` `` |

Label mappings:

| Prometheus label | ES|QL field |
|---|---|
| `mode` | `system.cpu.state` |
| `device` | `system.device` |
| `mountpoint` | `system.filesystem.mountpoint` |
| `fstype` | `system.filesystem.type` |
| `job` | `service.name` |
| `instance` | `service.instance.id` |

## Validated panels

| Panel | Source | Pattern | Status |
|---|---|---|---|
| CPU Busy | Node Exporter Full | `scalar * (1 - avg(rate(...)))` | ✓ Tier 1 |

## Architecture

See `docs/conversion-tool/architecture.md` for the full design rationale,
storage architecture notes (TSDS, TS command, counter_double types), and
the three-tier conversion framework.

## Known limitations

- Template variables (`$node`, `$job`) are currently resolved to global
  averages. Kibana controls integration is planned.
- Multi-target panels (multiple queries per panel) require Tier 2.
- Kibana panel import requires manual copy/paste or API call for now.
  A `--import` flag using the Kibana saved objects API is planned.
