# Dashboard Conversion Tool

Converts Grafana/Prometheus dashboard panels to Kibana/ES|QL equivalents
using the prom-demo OTel normalization pipeline as the translation layer.

## How it works

The converter operates in three tiers:

**Tier 1 — Deterministic.** Rule-based translation using the OTel field
mapping table below. Fast, reliable, no AI needed. Covers gauge, stat, and
timeseries panels with standard PromQL patterns (simple metric lookups,
`rate()`, `agg(rate(...))`, label-equality filters).

**Tier 2 — AI-assisted.** When a panel contains any target Tier 1 can't parse
(a regex label filter, a multi-value exclusion, `sum without(...)`), the
*whole panel* is escalated to a local model (`qwen2.5-coder:7b` via Ollama),
which rewrites it as a single unified ES|QL query using `EVAL` + `CASE` to
build a synthetic category column — see `docs/conversion-tool/architecture.md`
for why this has to be a whole-panel rewrite rather than per-target
translation. Output is checked by a mechanical validator before being
trusted; failures trigger an automatic retry with the specific validation
errors appended to the prompt.

**Tier 3 — Human-only.** Patterns with no reasonable automated equivalent
(subqueries, recording rules) still require manual conversion. Not yet
auto-flagged as a distinct category — currently these either succeed via
Tier 2 or raise an error requiring human review.

## Requirements

```bash
pip install requests streamlit
```

Python 3.9+. [Ollama](https://ollama.com) running locally for Tier 2:
```bash
brew install ollama
ollama pull qwen2.5-coder:7b
ollama serve   # if not already running
```

## Usage

### Command line

```bash
# Convert a panel and print the Kibana saved object JSON
python3 convert_panel.py --panel panels/cpu_basic.json

# Print only the ES|QL query (useful for validation in Kibana Dev Tools)
python3 convert_panel.py --panel panels/cpu_basic.json --query-only

# Convert and import directly into Kibana
python3 convert_panel.py --panel panels/cpu_basic.json --import \
  --kibana-endpoint $KIBANA_ENDPOINT --kibana-api-key $ES_API_KEY

# Disable Tier 2 — unsupported targets get dropped with a warning instead
# of being sent to the AI engine (useful for debugging/comparison)
python3 convert_panel.py --panel panels/cpu_basic.json --query-only --tier1-only
```

### Streamlit UI

```bash
python3 -m streamlit run app.py
```

Upload or paste a panel, convert it, see the ES|QL and which tier handled it.
Whenever Tier 2 fires, an expandable "What the AI did" section shows its
explanation, any approximations, and the full attempt history — every
attempt it made, not just the one that succeeded, each with its own
pass/fail status and viewable ES|QL. This exists because early Tier 2 runs
needed real debugging (see `docs/training-guide.md`), and that same
transparency is valuable for anyone trusting AI-generated output in a real
migration, not just for us during development.

## Adding a panel to convert

1. Export the panel JSON from Grafana (panel menu → Inspect → Panel JSON)
2. Save it to `panels/<panel_name>.json`
3. Run the converter — if it needs Tier 2, Ollama must be running
4. Validate the resulting ES|QL in Kibana Dev Tools before trusting it in
   production, especially for a brand-new PromQL pattern Tier 2 hasn't seen

## Field mapping

The converter uses `METRIC_MAP` and `LABEL_MAP` in `convert_panel.py`.
Key mappings for Node Exporter:

| Prometheus metric | ES|QL metrics field | ES|QL filter field |
|---|---|---|
| `node_cpu_seconds_total` | `metrics.system.cpu.time` | `system.cpu.time` |
| `node_memory_total_bytes` | `metrics.system.memory.limit` | `system.memory.limit` |
| `node_filesystem_size_bytes` | `metrics.system.filesystem.capacity` | `system.filesystem.capacity` |
| `node_disk_read_bytes_total` | `metrics.system.disk.io.read` | `system.disk.io.read` |
| `node_network_receive_bytes_total` | `metrics.system.network.io.receive` | `system.network.io.receive` |
| `node_load1` | `` `metrics.system.cpu.load_average.1m` `` | `` `system.cpu.load_average.1m` `` |

Label mappings:

| Prometheus label | ES|QL field |
|---|---|
| `mode` | `system.cpu.state` |
| `cpu` | `system.cpu.logical_number` |
| `device` | `system.device` |
| `mountpoint` | `system.filesystem.mountpoint` |
| `fstype` | `system.filesystem.type` |
| `job` | `service.name` |
| `instance` | `service.instance.id` |

Note: every sample panel uses Grafana's `$job` template variable rather than
a literal job name. Tier 1 silently drops the `service.name` filter when it
can't resolve this (harmless with one job in the data stream, a real risk
once there's more than one). Tier 2 resolves it explicitly via
`DEFAULT_JOB_RESOLUTION` in `tier2_engine.py` — override with the
`PROM_JOB_NAME` env var if your job name differs from `node-exporter`.

## Validated panels

| Panel | Type | Pattern | Tier |
|---|---|---|---|
| CPU Busy | gauge | `scalar * (1 - avg(rate(...)))` | 1 |
| CPU Cores | stat | `count(count(metric) by (label))` | 1 |
| CPU Basic | timeseries | System/User/Iowait/Idle (equality filters) | 1 |
| CPU Basic | timeseries | Busy IRQs (`mode =~ ".*irq"`, grouped) | 2 |
| CPU Basic | timeseries | Busy Other (6-value exclusion) | 2 |

All validated live against a real Elastic Cloud Serverless project and
cross-checked against Grafana's native rendering of the same underlying
data. The IRQ/softirq-specific values are *not* yet validated against real
data — macOS's Node Exporter doesn't report those CPU modes — only the
general grouping/exclusion mechanism has been proven, using other mode
values as stand-ins. See `docs/training-guide.md` for details.

## Architecture

See `docs/conversion-tool/architecture.md` for the full design rationale,
storage architecture notes (TSDS, TS command, counter_double types), and the
three-tier conversion framework. See `docs/training-guide.md` for the
narrative version — what broke during development, why, and how it was fixed.

## Known limitations

- Template variables resolve via a hardcoded default (Tier 2) or get
  silently dropped (Tier 1) rather than through real Kibana controls
  integration.
- No persistence: every conversion's result — including Tier 2's full
  attempt history — exists only for that run. A SQLite-backed history store
  is planned.
- No batch/whole-dashboard conversion yet; one panel at a time.
- Grafana's host port is 3099 in this project's `docker-compose.yaml` (see
  top-level README) — not relevant to this tool directly, but worth knowing
  if cross-referencing the reference dashboard.
