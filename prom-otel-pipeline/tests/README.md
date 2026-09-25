# OTel Pipeline Test Suite

Automated tests for the `prom-demo` Prometheus → OTel normalization pipeline.
Each test runs an ES|QL query against Elasticsearch and validates the results.

## Setup

```bash
pip install requests
```

## Running

```bash
cd prom-otel-pipeline/tests

# Run all tests (credentials from environment)
source ../../.env
python3 run_tests.py

# Run with JSON output (for CI)
python3 run_tests.py --json

# Run only HTTP-related tests
python3 run_tests.py --filter http

# Run only gRPC tests
python3 run_tests.py --filter grpc

# Run only resource attribute tests
python3 run_tests.py --filter resource
```

## Test coverage

| Group | Tests | What it validates |
|---|---|---|
| pipeline_health | 1 | All three domains have normalized documents |
| resource_* | 4 | job/instance/env/region promoted to resource attributes |
| http_* | 5 | method/status_code/url.path normalized, status_code is integer |
| grpc_* | 5 | service/method/status_code/stream_type normalized |
| db_* | 4 | system/namespace/operation normalized |
| go_* | 2 | goroutine count, memory metrics renamed |
| original_labels_* | 2 | Original Prometheus labels preserved |
| cross_signal_* | 1 | OTel fields compatible with trace/log correlation |

## Adding tests

Add a new entry to `test_cases.py`. Each test needs:

```python
{
    "name": "my_test",           # unique snake_case identifier
    "description": "What it validates",
    "query": """
        FROM metrics-prometheusreceiver.otel-default
        | STATS n = COUNT_DISTINCT(some.field)
    """,
    "assertions": [
        {"type": "field_equals", "field": "n", "expected": 4},
    ],
}
```

### Assertion types

| Type | Parameters | Description |
|---|---|---|
| `row_count` | `expected` | Number of rows returned equals expected |
| `field_equals` | `field`, `expected`, `row=0` | Field value equals expected |
| `field_greater_than` | `field`, `threshold`, `row=0` | Field value > threshold |
| `field_not_null` | `field`, `row=0` | Field is not null |
| `field_distinct_count` | `field`, `expected`, `row=0` | COUNT_DISTINCT result equals expected |

### ES|QL counter fields

Metrics mapped as counters have type `counter_double`. Use `TO_DOUBLE()` before
aggregating them, and use `COUNT_DISTINCT()` instead of `COUNT()`:

```python
# Wrong — counter_double can't be used with MAX directly
"query": "FROM ... | STATS n = MAX(http.server.request.count)"

# Right — cast first
"query": """
    FROM ...
    | EVAL c = TO_DOUBLE(http.server.request.count)
    | STATS n = MAX(c)
"""
```

## CI integration

```bash
python3 run_tests.py --json | tee results.json
# Exit code 0 = all passed, 1 = failures, 2 = config error
```
