"""
Test case definitions for the prom-demo OTel normalization pipeline.

Each test case is a dict with:
  name        — short identifier, used in output and --filter
  description — what the test validates
  query       — ES|QL query string
  assertions  — list of assertion dicts, each with:
    type      — "row_count", "field_not_null", "field_equals",
                "field_greater_than", "field_distinct_count"
    ...       — type-specific parameters (see run_tests.py for details)
"""

DATA_STREAM = "metrics-prometheusreceiver.otel-default"

TEST_CASES = [

    # -------------------------------------------------------------------------
    # 1. Pipeline health
    # -------------------------------------------------------------------------
    {
        "name": "pipeline_health",
        "description": "Normalized documents exist for all three metric domains",
        "query": f"""
            FROM {DATA_STREAM}
            | STATS
                total_docs   = COUNT(*),
                http_docs    = COUNT(*) WHERE http.server.request.count IS NOT NULL,
                grpc_docs    = COUNT(*) WHERE rpc.server.request.count IS NOT NULL,
                db_docs      = COUNT(*) WHERE db.client.connection.count IS NOT NULL,
                svc_count    = COUNT_DISTINCT(service.name)
        """,
        "assertions": [
            {"type": "row_count", "expected": 1},
            {"type": "field_greater_than", "field": "total_docs", "threshold": 0},
            {"type": "field_greater_than", "field": "http_docs",  "threshold": 0},
            {"type": "field_greater_than", "field": "grpc_docs",  "threshold": 0},
            {"type": "field_greater_than", "field": "db_docs",    "threshold": 0},
            {"type": "field_equals",       "field": "svc_count",  "expected": 1},
        ],
    },

    # -------------------------------------------------------------------------
    # 2. Service identity — resource attributes
    # -------------------------------------------------------------------------
    {
        "name": "resource_service_name",
        "description": "job label promoted to service.name resource attribute",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE http.server.request.count IS NOT NULL
            | STATS svc = COUNT_DISTINCT(service.name)
        """,
        "assertions": [
            {"type": "field_equals", "field": "svc", "expected": 1},
        ],
    },
    {
        "name": "resource_service_instance",
        "description": "instance label promoted to service.instance.id",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE http.server.request.count IS NOT NULL
            | STATS n = COUNT_DISTINCT(service.instance.id)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "n", "threshold": 0},
        ],
    },

    # -------------------------------------------------------------------------
    # 3. Static label promotion to resource
    # -------------------------------------------------------------------------
    {
        "name": "resource_promotion_env",
        "description": "env label promoted to deployment.environment.name",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE http.server.request.count IS NOT NULL
            | STATS n = COUNT_DISTINCT(deployment.environment.name)
        """,
        "assertions": [
            {"type": "field_equals", "field": "n", "expected": 1},
        ],
    },
    {
        "name": "resource_promotion_region",
        "description": "region label promoted to cloud.region",
        "query": f"""
            FROM {DATA_STREAM}
            | STATS n = COUNT_DISTINCT(cloud.region)
        """,
        "assertions": [
            {"type": "field_equals", "field": "n", "expected": 1},
        ],
    },

    # -------------------------------------------------------------------------
    # 4. HTTP domain normalization
    # -------------------------------------------------------------------------
    {
        "name": "http_method_normalized",
        "description": "method label mapped to http.request.method",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE http.server.request.count IS NOT NULL
            | STATS n = COUNT_DISTINCT(`http.request.method`)
        """,
        "assertions": [
            # GET POST PUT DELETE = 4 methods
            {"type": "field_equals", "field": "n", "expected": 4},
        ],
    },
    {
        "name": "http_status_code_normalized",
        "description": "status_code label mapped to http.response.status_code as integer",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE http.server.request.count IS NOT NULL
            | STATS n = COUNT_DISTINCT(`http.response.status_code`)
        """,
        "assertions": [
            # 200 201 400 404 500 503 = 6 status codes
            {"type": "field_equals", "field": "n", "expected": 6},
        ],
    },
    {
        "name": "http_status_code_is_integer",
        "description": "http.response.status_code is integer, not string",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE http.server.request.count IS NOT NULL
            | WHERE `http.response.status_code` >= 200
            | STATS n = COUNT(*)
        """,
        "assertions": [
            # If status_code were a string, >= 200 comparison would fail
            {"type": "field_greater_than", "field": "n", "threshold": 0},
        ],
    },
    {
        "name": "http_url_path_normalized",
        "description": "path label mapped to url.path",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE http.server.request.count IS NOT NULL
            | STATS n = COUNT_DISTINCT(url.path)
        """,
        "assertions": [
            # /api/users /api/orders /api/products /api/checkout /health /metrics
            {"type": "field_greater_than", "field": "n", "threshold": 5},
        ],
    },
    {
        "name": "http_metric_name_normalized",
        "description": "http_requests_total renamed to http.server.request.count",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE http.server.request.count IS NOT NULL
            | STATS n = COUNT(*)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "n", "threshold": 0},
        ],
    },

    # -------------------------------------------------------------------------
    # 5. gRPC domain normalization
    # -------------------------------------------------------------------------
    {
        "name": "grpc_service_normalized",
        "description": "grpc_service label mapped to rpc.service",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE rpc.server.request.count IS NOT NULL
            | STATS n = COUNT_DISTINCT(rpc.service)
        """,
        "assertions": [
            # OrderService UserService ProductService PaymentService
            {"type": "field_equals", "field": "n", "expected": 4},
        ],
    },
    {
        "name": "grpc_method_normalized",
        "description": "grpc_method label mapped to rpc.method",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE rpc.server.request.count IS NOT NULL
            | STATS n = COUNT_DISTINCT(rpc.method)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "n", "threshold": 0},
        ],
    },
    {
        "name": "grpc_status_code_normalized",
        "description": "grpc_code label mapped to rpc.grpc.status_code",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE rpc.server.request.count IS NOT NULL
            | STATS n = COUNT_DISTINCT(rpc.grpc.status_code)
        """,
        "assertions": [
            # OK NOT_FOUND INVALID_ARGUMENT INTERNAL UNAVAILABLE
            {"type": "field_equals", "field": "n", "expected": 5},
        ],
    },
    {
        "name": "grpc_stream_type_normalized",
        "description": "grpc_type label mapped to rpc.grpc.stream_type",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE rpc.server.request.count IS NOT NULL
            | STATS n = COUNT_DISTINCT(rpc.grpc.stream_type)
        """,
        "assertions": [
            # unary server_stream
            {"type": "field_equals", "field": "n", "expected": 2},
        ],
    },
    {
        "name": "grpc_metric_name_normalized",
        "description": "grpc_server_handled_total renamed to rpc.server.request.count",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE rpc.server.request.count IS NOT NULL
            | STATS n = COUNT(*)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "n", "threshold": 0},
        ],
    },

    # -------------------------------------------------------------------------
    # 6. Database domain normalization
    # -------------------------------------------------------------------------
    {
        "name": "db_system_normalized",
        "description": "db_system label mapped to db.system.name",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE db.client.operation.errors IS NOT NULL
            | STATS n = COUNT_DISTINCT(`db.system.name`)
        """,
        "assertions": [
            # postgresql
            {"type": "field_equals", "field": "n", "expected": 1},
        ],
    },
    {
        "name": "db_namespace_normalized",
        "description": "db_name label mapped to db.namespace",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE db.client.operation.errors IS NOT NULL
            | STATS n = COUNT_DISTINCT(`db.namespace`)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "n", "threshold": 0},
        ],
    },
    {
        "name": "db_operation_normalized",
        "description": "db_operation label mapped to db.operation.name",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE db.client.operation.errors IS NOT NULL
            | STATS n = COUNT_DISTINCT(`db.operation.name`)
        """,
        "assertions": [
            # SELECT INSERT UPDATE DELETE
            {"type": "field_equals", "field": "n", "expected": 4},
        ],
    },
    {
        "name": "db_metric_name_normalized",
        "description": "db_errors_total renamed to db.client.operation.errors",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE db.client.operation.errors IS NOT NULL
            | STATS n = COUNT(*)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "n", "threshold": 0},
        ],
    },

    # -------------------------------------------------------------------------
    # 7. Go runtime metric name normalization
    # -------------------------------------------------------------------------
    {
        "name": "go_goroutine_count_normalized",
        "description": "go_goroutines renamed to go.goroutine.count",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE go.goroutine.count IS NOT NULL
            | STATS n = COUNT(*)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "n", "threshold": 0},
        ],
    },
    {
        "name": "go_memory_normalized",
        "description": "go_memstats_heap_* renamed to go.memory.heap.*",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE `go.memory.heap.used` IS NOT NULL
            | STATS n = COUNT(*)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "n", "threshold": 0},
        ],
    },

    # -------------------------------------------------------------------------
    # 8. Original labels preserved (backward compatibility)
    # -------------------------------------------------------------------------
    {
        "name": "original_labels_preserved_http",
        "description": "Original Prometheus labels preserved alongside OTel attributes",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE http.server.request.count IS NOT NULL
            | STATS
                has_method      = COUNT_DISTINCT(attributes.method),
                has_path        = COUNT_DISTINCT(attributes.path),
                has_status_code = COUNT_DISTINCT(attributes.status_code)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "has_method",      "threshold": 0},
            {"type": "field_greater_than", "field": "has_path",        "threshold": 0},
            {"type": "field_greater_than", "field": "has_status_code", "threshold": 0},
        ],
    },
    {
        "name": "original_labels_preserved_grpc",
        "description": "Original gRPC Prometheus labels preserved",
        "query": f"""
            FROM {DATA_STREAM}
            | WHERE rpc.server.request.count IS NOT NULL
            | STATS
                has_grpc_service = COUNT_DISTINCT(attributes.grpc_service),
                has_grpc_code    = COUNT_DISTINCT(attributes.grpc_code)
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "has_grpc_service", "threshold": 0},
            {"type": "field_greater_than", "field": "has_grpc_code",    "threshold": 0},
        ],
    },

    # -------------------------------------------------------------------------
    # 9. Cross-signal readiness
    # -------------------------------------------------------------------------
    {
        "name": "cross_signal_readiness",
        "description": "service.name, http.response.status_code (int), url.path all present — cross-signal correlation is possible",
        "query": f"""
            FROM {DATA_STREAM}
            | STATS
                services = COUNT_DISTINCT(service.name),
                paths    = COUNT_DISTINCT(url.path),
                int_codes = COUNT(*) WHERE `http.response.status_code` >= 200
        """,
        "assertions": [
            {"type": "field_greater_than", "field": "services",   "threshold": 0},
            {"type": "field_greater_than", "field": "paths",      "threshold": 0},
            {"type": "field_greater_than", "field": "int_codes",  "threshold": 0},
        ],
    },

]
