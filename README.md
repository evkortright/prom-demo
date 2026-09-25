# prom-demo

A proof-of-concept pipeline that normalizes raw Prometheus metrics to
[OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
before indexing into Elasticsearch.

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
semantic convention field names:

```json
{
  "service.name": "api",
  "http.request.method": "GET",
  "http.response.status_code": 200
}
```

The mismatch silently breaks cross-signal correlation queries in Elastic Observability.

## The solution

An OTel Collector pipeline that scrapes Prometheus metrics, applies a semantic
convention mapping table, and exports OTel-compliant data to Elasticsearch.
The result lands in `metrics-*.otel-*` data streams with proper field names,
integer types, and resource attributes — compatible with traces and logs from day one.

```
Prometheus app (/metrics)
    │
    │  OTel Collector scrape (15s)
    ▼
OTel Collector
    │  transform/resource_promotion   env → deployment.environment.name
    │  transform/universal            job → service.name
    │  transform/http                 method → http.request.method
    │  transform/grpc                 grpc_service → rpc.service
    │  transform/database             db_system → db.system.name
    │  transform/metric_names         http_requests_total → http.server.request.count
    ▼
Elasticsearch (metrics-*.otel-* data streams)
```

## Repository structure

```
prom-demo/
├── app/                          # Go demo service (pure Prometheus instrumentation)
│   ├── main.go                   # HTTP, gRPC, and DB metrics simulation
│   ├── go.mod
│   └── Dockerfile                # Multi-stage build, no local Go toolchain needed
├── otelcol/
│   └── otelcol-config.yaml       # OTel Collector normalization pipeline
├── prometheus/
│   └── prometheus.yaml           # Prometheus scrape config (no credentials)
├── prom-otel-pipeline/
│   ├── reset.sh                  # Full reset: stop → delete index → start
│   ├── pipeline.json             # ES ingest pipeline (reference only — see note)
│   ├── install.sh                # Install the ES ingest pipeline
│   ├── test-simulate.json        # Pipeline simulate test for Kibana Dev Tools
│   └── tests/
│       ├── run_tests.py          # Automated test runner (24 tests)
│       ├── test_cases.py         # ES|QL test case definitions
│       └── README.md             # Test suite documentation
├── docs/
│   ├── esql-sanity-checks.md     # ES|QL validation queries
│   └── handoffs/                 # Session handoff documents
├── docker-compose.yaml           # App + Prometheus + OTel Collector
└── .gitignore
```

## Quick start

### Prerequisites

- Docker Desktop (≥12 GB RAM allocated)
- An [Elastic Cloud Serverless](https://cloud.elastic.co) Observability project
- Python 3.9+ and `pip install requests` (for the test suite)

### 1. Credentials

```bash
cp .env.example .env
# Edit .env — add ES_ENDPOINT and ES_API_KEY
```

Get your credentials from Elastic Cloud:
- **ES_ENDPOINT** — project overview page → Endpoints → Elasticsearch
- **ES_API_KEY** — Kibana → Stack Management → API Keys → Create API key (copy the Encoded value)

### 2. Start the stack

```bash
source .env
bash prom-otel-pipeline/reset.sh
```

This stops any running containers, clears the Elasticsearch data stream, and
starts the full stack. After ~20 seconds, normalized metrics will be flowing.

### 3. Verify

In Kibana Dev Tools:

```
GET metrics-prometheusreceiver.otel-default/_search
{ "size": 1 }
```

You should see documents with OTel-normalized fields: `service.name`,
`http.request.method`, `http.response.status_code` (integer), `rpc.service`, etc.

### 4. Run the test suite

```bash
cd prom-otel-pipeline/tests
python3 run_tests.py
# Expected: 24/24 passed
```

## Mapping table

### Universal (every metric)

| Prometheus label | OTel attribute | Notes |
|---|---|---|
| `job` | `service.name` | Resource attribute |
| `instance` | `service.instance.id` | Resource attribute |
| `host_name` | `host.name.scrape` | Resource attribute |
| `env` | `deployment.environment.name` | Promoted from datapoint to resource |
| `region` | `cloud.region` | Promoted from datapoint to resource |

### HTTP domain

| Prometheus label | OTel attribute | Type |
|---|---|---|
| `method` | `http.request.method` | string |
| `path` | `url.path` | string |
| `status_code` | `http.response.status_code` | integer |

Metric names: `http_requests_total` → `http.server.request.count`,
`http_request_duration_seconds` → `http.server.request.duration`

### gRPC domain

| Prometheus label | OTel attribute |
|---|---|
| `grpc_service` | `rpc.service` |
| `grpc_method` | `rpc.method` |
| `grpc_code` | `rpc.grpc.status_code` |
| `grpc_type` | `rpc.grpc.stream_type` |

Metric names: `grpc_server_handled_total` → `rpc.server.request.count`,
`grpc_server_handling_seconds` → `rpc.server.duration`

### Database domain

| Prometheus label | OTel attribute |
|---|---|
| `db_system` | `db.system.name` |
| `db_name` | `db.namespace` |
| `db_operation` | `db.operation.name` |

Metric names: `db_query_duration_seconds` → `db.client.operation.duration`,
`db_connections_active` → `db.client.connection.count`,
`db_errors_total` → `db.client.operation.errors`

## Design decisions

**Why the OTel Collector instead of an Elasticsearch ingest pipeline?**
The Elastic Serverless PRW endpoint bypasses the `@custom` ingest pipeline hook
entirely — it writes directly to the TSDB layer, skipping all ingest processing.
The OTel Collector is the correct upstream insertion point.

**Why upstream `otelcol-contrib` instead of EDOT?**
EDOT is now built into Elastic Agent, making it a heavier runtime than needed
for this use case. The upstream contrib Collector's `elasticsearchexporter` is
developed by Elastic and works identically on Serverless. Our tool targets
Prometheus users who may have no Elastic Agent deployed.

**Why copy labels rather than rename them?**
Original Prometheus labels are preserved alongside OTel attributes so existing
PromQL queries and dashboards continue to work during migration.

**Why two data streams?**
Raw PRW data lands in `metrics-generic.prometheus-default` (unchanged).
Normalized data lands in `metrics-prometheusreceiver.otel-default` (OTel template).
Both can be queried simultaneously for comparison and validation.

## ES|QL notes

Metrics exported as counters have type `counter_double` in Elasticsearch.
ES|QL requires `TO_DOUBLE()` before using counter fields with `MAX`/`MIN`/`SUM`:

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE http.server.request.count IS NOT NULL
| EVAL c = TO_DOUBLE(http.server.request.count)
| STATS total = MAX(c) BY `http.request.method`
```

## What's next

- Extend mapping table: Node Exporter (system metrics domain)
- Extend mapping table: kube-state-metrics (Kubernetes domain)
- Dashboard migration layer: Grafana panel JSON → Kibana panel using normalized fields
- Publish mapping table as a machine-readable artifact for contribution to OTel Collector contrib
