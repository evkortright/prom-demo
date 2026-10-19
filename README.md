# prom-demo

A migration tool for teams moving from Prometheus + Grafana to Elastic
Observability. It does two things:

1. **Normalizes** raw Prometheus metrics to
   [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
   before indexing into Elasticsearch, so cross-signal correlation with
   OTel-native traces and logs works out of the box.
2. **Converts** Grafana dashboard panels into Kibana equivalents automatically
   — a deterministic rule-based converter (Tier 1) backed by a local AI model
   (Tier 2) for PromQL patterns the rules can't handle.

## The problem

When Prometheus `remote_write` sends metrics to Elasticsearch, data lands with
flat, underscore-style Prometheus labels:

```json
{
  "labels": { "job": "api", "method": "GET", "status_code": "200" },
  "metrics": { "http_requests_total": 4821 }
}
```

This is incompatible with OTel-native traces and logs, which use dot-notation
semantic convention field names, and it means every Grafana dashboard has to
be rewritten by hand to work against the new field names.

## The solution

An OTel Collector pipeline normalizes the metrics on the way in. A separate
conversion tool then rewrites each Grafana panel's PromQL into ES|QL against
the normalized fields, and builds the equivalent Kibana panel automatically.

```
Prometheus app / Node Exporter
    │
    │  OTel Collector scrape
    ▼
OTel Collector  (semantic convention mapping, metric renaming)
    ▼
Elasticsearch (metrics-*.otel-* data streams, TSDS)
    ▲
    │  ES|QL (Tier 1 deterministic, or Tier 2 AI-assisted)
    │
Grafana panel JSON  →  conversion-tool  →  Kibana panel
```

## Repository structure

```
prom-demo/
├── app/                           # Go demo service (HTTP/gRPC/DB metrics)
├── otelcol/
│   └── otelcol-config.yaml        # OTel Collector normalization pipeline
├── prometheus/
│   └── prometheus.yaml            # Prometheus scrape config
├── grafana/
│   └── provisioning/              # Auto-loaded Grafana dashboards
│       └── dashboard-files/cpu-busy.json   # Reference demo (3 panels)
├── conversion-tool/
│   ├── convert_panel.py           # CLI: Grafana panel JSON → Kibana panel
│   ├── tier2_engine.py            # AI-assisted translation (Ollama)
│   ├── app.py                     # Streamlit UI wrapper
│   ├── build_tier2_panel.py       # One-off Tier 2 panel-building helper
│   ├── panels/                    # Sample Grafana panel JSON fixtures
│   └── README.md                  # Conversion tool details
├── build_demo_dashboard.py        # Builds the Kibana reference dashboard
│                                   # (all 3 panels, updates in place)
├── docs/
│   ├── conversion-tool/architecture.md   # Tier 1/2/3 design rationale
│   ├── esql-sanity-checks.md      # ES|QL validation queries (app metrics)
│   ├── training-guide.md          # Full operational + conceptual guide
│   └── handoffs/                  # Dated session handoff documents
├── prom-otel-pipeline/
│   ├── reset.sh                   # Full reset: stop → delete index → start
│   └── tests/                     # Automated pipeline test suite
├── docker-compose.yaml            # App + Prometheus + OTel Collector + Grafana
└── .gitignore
```

## Quick start

### Prerequisites

- Docker Desktop
- An [Elastic Cloud Serverless](https://cloud.elastic.co) Observability project
- Node Exporter, installed via Homebrew (**not** run from a `/tmp` binary —
  that doesn't survive a reboot):
  ```bash
  brew install node_exporter
  brew services start node_exporter
  ```
- [Ollama](https://ollama.com), for Tier 2 AI-assisted conversion:
  ```bash
  brew install ollama
  ollama pull qwen2.5-coder:7b
  ```
- Python 3.9+ and `pip install requests streamlit`

### 1. Credentials

```bash
cp .env.example .env
# Edit .env — add ES_ENDPOINT, KIBANA_ENDPOINT, and ES_API_KEY
```

### 2. Start the stack

```bash
source .env
docker compose up -d
```

Grafana is on **http://localhost:3099** (admin/admin) — not the default 3000,
remapped to avoid a common local port conflict. If login fails with
"invalid username or password" after the container's been running a while,
the `GF_SECURITY_ADMIN_PASSWORD` env var only applies on first volume
creation — reset it directly instead:

```bash
docker exec -it prom-demo-grafana grafana cli admin reset-admin-password admin
```

(Newer Grafana images use `grafana cli`, not the older `grafana-cli`.)

### 3. Verify data is flowing

```bash
curl -s http://localhost:9100/metrics | head -3        # Node Exporter
curl -s http://localhost:11434/api/tags                # Ollama
```

### 4. See both dashboards side by side

- **Grafana:** http://localhost:3099 → "prom-demo: Node Exporter CPU"
- **Kibana:** rebuild/open the reference dashboard any time with:
  ```bash
  python3 build_demo_dashboard.py
  ```
  (Uses a fixed dashboard ID and `overwrite=true`, so re-running always
  updates the same dashboard instead of creating duplicates.)

### 5. Try the conversion tool

```bash
cd conversion-tool
python3 convert_panel.py --panel panels/cpu_basic.json --query-only
```

Or use the Streamlit UI:

```bash
python3 -m streamlit run app.py
```

See `docs/training-guide.md` for the full conceptual walkthrough — why the
tool is built the way it is, what each tier actually handles, and the
ES|QL/environment gotchas that cost real debugging time to find.

## Mapping table

See `conversion-tool/README.md` for the full Node Exporter field mapping
table used by the conversion tool, and `docs/esql-sanity-checks.md` for the
original app-level (HTTP/gRPC/DB) OTel normalization mapping.

## Design decisions

**Why the OTel Collector instead of an Elasticsearch ingest pipeline?**
The Elastic Serverless PRW endpoint bypasses the `@custom` ingest pipeline
hook entirely — it writes directly to the TSDB layer. The OTel Collector is
the correct upstream insertion point.

**Why a three-tier conversion model (deterministic → AI → human)?**
Most Grafana panels follow a handful of common PromQL shapes — those convert
reliably without AI. A smaller set need a regex filter, label exclusion, or
`sum without(...)` — patterns a deterministic parser can't generalize to, but
a code-tuned local model can translate correctly once given a worked example
and validated against a mechanical checker. A residual few patterns
(subqueries, recording rules) still need a human. See
`docs/conversion-tool/architecture.md` for the full rationale, including why
Tier 2 rewrites the *whole* panel's query rather than translating one target
at a time.

**Why validate AI output mechanically instead of trusting it?**
Early Tier 2 runs produced syntactically-plausible-but-wrong ES|QL — SQL-style
`CASE WHEN` instead of ES|QL's `CASE(...)` function, single-quoted strings
(invalid in ES|QL), multiple chained `STATS` commands. A validator checks for
these specific failure modes before a human ever sees the output, and the
engine automatically retries with the validator's feedback appended to the
prompt. See `docs/training-guide.md` for the real failures hit during
development and how each was fixed.

## Known limitations / what's next

- Tier 2's `irq`/`softirq`/`iowait` CPU modes are untested on real data —
  macOS doesn't report them. Needs revalidation on a Linux host.
- Conversions aren't persisted anywhere — each run's result (including the
  full Tier 2 attempt history) only exists for that run. A SQLite-backed
  history store, with optional sync to Elasticsearch, is planned next.
- Batch/whole-dashboard conversion (convert every panel in a Grafana
  dashboard export in one pass, with a migration scoping report) is planned
  after that.
- Kubernetes domain (kube-state-metrics) mapping not yet started.
