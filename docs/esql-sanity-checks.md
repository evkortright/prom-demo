# ES|QL Sanity Check Suite
## prom-demo OTel Normalization Pipeline — v1.0

Run these in Kibana Discover (ES|QL mode) or Dev Tools.
Data stream: `metrics-prometheusreceiver.otel-default`

**Important:** Metrics mapped as counters have type `counter_double` in Elasticsearch.
ES|QL requires `TO_DOUBLE()` before using counter fields with MAX/MIN/SUM/STATS.
Use `RATE()` for true per-second rates over time windows.

---

## 1. Pipeline health — confirm normalized documents exist

```esql
FROM metrics-prometheusreceiver.otel-default
| STATS
    total_docs = COUNT(*),
    http_docs = COUNT(*) WHERE http.server.request.count IS NOT NULL,
    grpc_docs = COUNT(*) WHERE rpc.server.request.count IS NOT NULL,
    db_docs = COUNT(*) WHERE db.client.connection.count IS NOT NULL,
    normalized_services = COUNT_DISTINCT(service.name)
```

**Expected:** all counts > 0, `normalized_services` = 1 ("prom-demo")

---

## 2. HTTP domain — confirm all three attributes normalized

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE http.server.request.count IS NOT NULL
| KEEP
    `http.request.method`,
    `http.response.status_code`,
    `url.path`,
    `http.server.request.count`,
    service.name
| LIMIT 10
```

**Expected:**
- `http.request.method` = GET/POST/PUT/DELETE
- `http.response.status_code` = integer (200, 404, 500, etc.)
- `url.path` = /api/users, /api/orders, etc.
- `service.name` = "prom-demo"

---

## 3. HTTP — request count by method and status

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE http.server.request.count IS NOT NULL
| EVAL request_count = TO_DOUBLE(http.server.request.count)
| STATS
    total_requests = MAX(request_count)
    BY `http.request.method`, `http.response.status_code`
| SORT total_requests DESC
```

**Expected:** rows for GET/POST/PUT/DELETE × 200/201/400/404/500/503
**Note:** `TO_DOUBLE()` is required because `http.server.request.count` is `counter_double`.

---

## 4. gRPC domain — confirm all four attributes normalized

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE rpc.server.request.count IS NOT NULL
| KEEP
    rpc.service,
    rpc.method,
    rpc.grpc.status_code,
    rpc.grpc.stream_type,
    rpc.server.request.count,
    service.name
| LIMIT 10
```

**Expected:**
- `rpc.service` = OrderService/UserService/ProductService/PaymentService
- `rpc.method` = GetUser/CreateOrder/ListProducts/etc.
- `rpc.grpc.status_code` = OK/NOT_FOUND/INTERNAL/UNAVAILABLE/INVALID_ARGUMENT
- `rpc.grpc.stream_type` = unary/server_stream

---

## 5. gRPC — error rate by service

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE rpc.server.request.count IS NOT NULL
| EVAL
    count_d = TO_DOUBLE(rpc.server.request.count),
    is_error = CASE(rpc.grpc.status_code != "OK", TO_DOUBLE(rpc.server.request.count), 0.0)
| STATS
    total  = MAX(count_d),
    errors = MAX(is_error)
    BY rpc.service
| EVAL error_rate = ROUND(errors / total * 100, 2)
| SORT error_rate DESC
```

**Expected:** 4 services with non-zero error rates

---

## 6. Database domain — confirm all three attributes normalized

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE db.client.operation.errors IS NOT NULL
| KEEP
    `db.system.name`,
    `db.namespace`,
    `db.operation.name`,
    `db.client.operation.errors`,
    service.name
| LIMIT 10
```

**Expected:**
- `db.system.name` = postgresql
- `db.namespace` = orders
- `db.operation.name` = SELECT/INSERT/UPDATE/DELETE

---

## 7. Database — errors by system, namespace, and operation

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE db.client.operation.errors IS NOT NULL
| EVAL error_count = TO_DOUBLE(db.client.operation.errors)
| STATS
    total_errors = MAX(error_count)
    BY `db.system.name`, `db.namespace`, `db.operation.name`
| SORT total_errors DESC
```

---

## 8. Resource attributes — confirm promotion worked

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE http.server.request.count IS NOT NULL
| KEEP
    service.name,
    service.instance.id,
    deployment.environment.name,
    cloud.region,
    host.name
| LIMIT 5
```

**Expected:**
- `service.name` = "prom-demo" (from job label)
- `service.instance.id` = "app:8080" (from instance label)
- `deployment.environment.name` = "production" (promoted from env)
- `cloud.region` = "us-central1" (promoted from region)
- `host.name` = container ID (from resourcedetection)

---

## 9. Go runtime — confirm metric name normalization

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE go.goroutine.count IS NOT NULL
| KEEP
    go.goroutine.count,
    `go.memory.heap.used`,
    `go.memory.heap.idle`,
    `go.memory.stack.used`,
    service.name
| LIMIT 5
```

**Expected:** non-null values for all normalized Go runtime metrics

---

## 10. Cross-signal readiness — error rate by path

```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE http.server.request.count IS NOT NULL
| EVAL
    count_d = TO_DOUBLE(http.server.request.count),
    error_d = CASE(http.response.status_code >= 500, TO_DOUBLE(http.server.request.count), 0.0)
| STATS
    total_requests = MAX(count_d),
    error_requests = MAX(error_d)
    BY service.name, url.path
| EVAL error_rate = ROUND(error_requests / total_requests * 100, 2)
| SORT error_rate DESC
| LIMIT 10
```

**This query only works because:**
- `service.name` matches OTel traces field names exactly
- `http.response.status_code` is an integer (not a string)
- `url.path` follows OTel semconv

These three conditions are what make cross-signal correlation possible.

---

## 11. Normalization coverage check

```esql
FROM metrics-prometheusreceiver.otel-default
| STATS
    has_service_name = COUNT_DISTINCT(service.name),
    has_http_method  = COUNT_DISTINCT(`http.request.method`),
    has_rpc_service  = COUNT_DISTINCT(rpc.service),
    has_db_system    = COUNT_DISTINCT(`db.system.name`),
    has_env          = COUNT_DISTINCT(deployment.environment.name),
    has_cloud_region = COUNT_DISTINCT(cloud.region)
```

**Expected:** all counts ≥ 1

---

## 12. Dual data stream comparison — raw vs normalized

Run these side by side to see the before/after:

**Raw (PRW path):**
```esql
FROM metrics-generic.prometheus-default
| WHERE labels.job == "prom-demo"
| KEEP @timestamp, labels.method, labels.status_code, labels.job
| LIMIT 3
```

**Normalized (OTel Collector path):**
```esql
FROM metrics-prometheusreceiver.otel-default
| WHERE http.server.request.count IS NOT NULL
| KEEP @timestamp, `http.request.method`, `http.response.status_code`, service.name
| LIMIT 3
```

**What this shows:** Same underlying data. One path is raw Prometheus — flat labels,
string status codes, no OTel structure. The other is normalized — proper semconv
field names, integer status codes, resource attributes separated from metric
dimensions. The difference is our pipeline.
