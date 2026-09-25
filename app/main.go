// prom-demo: A pure Prometheus instrumented service.
//
// This app exists for one purpose: to generate realistic Prometheus metrics
// with the flat, underscore-label conventions used by real Prometheus exporters
// — no OpenTelemetry, no OTLP, no semantic conventions. Just raw Prometheus.
//
// It simulates three common metric domains:
//   - HTTP server (method, path, status_code labels)
//   - gRPC server (grpc_service, grpc_method, grpc_code labels)
//   - Database client (db_system, db_name, db_operation labels)
//
// These are the exact label names our ingest pipeline will normalize to OTel
// semantic conventions on the way into Elasticsearch.

package main

import (
	"log"
	"math/rand"
	"net/http"
	"time"

	// The official Prometheus Go client library.
	// prometheus: core types (Counter, Gauge, Histogram, etc.)
	// promauto:   auto-registers metrics on creation — less boilerplate
	// promhttp:   the /metrics HTTP handler
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// ---------------------------------------------------------------------------
// Metric definitions
//
// In Go, you declare metrics as package-level variables. Each metric specifies
// its name, help text, and the label names it will carry. Label *values* are
// set later when you record observations.
//
// These label names are deliberately Prometheus-conventional (underscores, flat)
// — NOT OTel semantic conventions. This is what real Prometheus exporters emit.
// ---------------------------------------------------------------------------

// HTTP server metrics.
// CounterVec: a counter with labels. Counts requests by method/path/status.
var httpRequestsTotal = promauto.NewCounterVec(
	prometheus.CounterOpts{
		Name: "http_requests_total",
		Help: "Total number of HTTP requests received.",
	},
	[]string{"method", "path", "status_code"},
)

// HistogramVec: records the distribution of request durations.
// Buckets define the upper bounds of each timing bucket (in seconds).
var httpRequestDuration = promauto.NewHistogramVec(
	prometheus.HistogramOpts{
		Name:    "http_request_duration_seconds",
		Help:    "HTTP request latency in seconds.",
		Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5},
	},
	[]string{"method", "path"},
)

// gRPC server metrics.
// Real gRPC deployments use grpc_server_handled_total from the go-grpc-prometheus
// library. We reproduce its label names exactly so our mapping table is accurate.
var grpcRequestsTotal = promauto.NewCounterVec(
	prometheus.CounterOpts{
		Name: "grpc_server_handled_total",
		Help: "Total number of RPCs completed on the server.",
	},
	[]string{"grpc_type", "grpc_service", "grpc_method", "grpc_code"},
)

var grpcRequestDuration = promauto.NewHistogramVec(
	prometheus.HistogramOpts{
		Name:    "grpc_server_handling_seconds",
		Help:    "Histogram of response latency (seconds) of gRPC that had been handled by the server.",
		Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0},
	},
	[]string{"grpc_type", "grpc_service", "grpc_method"},
)

// Database client metrics.
// Modelled after the conventions used by sqlx, pgx, and other DB client
// instrumentation libraries. db_system matches the OTel db.system convention
// already — useful to show in the mapping table as a "no-op" case.
var dbQueryDuration = promauto.NewHistogramVec(
	prometheus.HistogramOpts{
		Name:    "db_query_duration_seconds",
		Help:    "Database query latency in seconds.",
		Buckets: []float64{0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5, 1.0},
	},
	[]string{"db_system", "db_name", "db_operation"},
)

var dbConnectionsActive = promauto.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "db_connections_active",
		Help: "Number of active database connections.",
	},
	[]string{"db_system", "db_name"},
)

var dbErrorsTotal = promauto.NewCounterVec(
	prometheus.CounterOpts{
		Name: "db_errors_total",
		Help: "Total number of database errors.",
	},
	[]string{"db_system", "db_name", "db_operation"},
)

// ---------------------------------------------------------------------------
// Simulation helpers
//
// These functions generate realistic-looking traffic patterns so the metrics
// have meaningful distributions rather than uniform noise.
// ---------------------------------------------------------------------------

// weighted randomly picks an item from a slice with associated weights.
// Example: weighted([]string{"GET","POST"}, []int{8,2}) picks GET 80% of the time.
func weighted(items []string, weights []int) string {
	total := 0
	for _, w := range weights {
		total += w
	}
	r := rand.Intn(total)
	for i, w := range weights {
		r -= w
		if r < 0 {
			return items[i]
		}
	}
	return items[len(items)-1]
}

// simulateHTTP generates a stream of HTTP request observations.
// It runs forever in its own goroutine (a lightweight Go thread).
func simulateHTTP() {
	paths := []string{
		"/api/users", "/api/orders", "/api/products",
		"/api/checkout", "/health", "/metrics",
	}
	pathWeights := []int{30, 20, 25, 10, 10, 5}

	methods := []string{"GET", "POST", "PUT", "DELETE"}
	methodWeights := []int{60, 20, 15, 5}

	// Status codes with realistic distribution: mostly 200, some errors.
	statuses := []string{"200", "201", "400", "404", "500", "503"}
	statusWeights := []int{70, 10, 8, 6, 4, 2}

	for {
		method := weighted(methods, methodWeights)
		path := weighted(paths, pathWeights)
		status := weighted(statuses, statusWeights)

		// Simulate latency: faster for GET, slower for POST/PUT, errors are fast.
		var latencyMs float64
		switch method {
		case "GET":
			latencyMs = 10 + rand.Float64()*90   // 10–100ms
		case "POST", "PUT":
			latencyMs = 50 + rand.Float64()*450  // 50–500ms
		default:
			latencyMs = 5 + rand.Float64()*20    // 5–25ms
		}
		// Errors tend to be faster (fail fast) or slower (timeouts).
		if status == "500" || status == "503" {
			latencyMs = latencyMs * (0.5 + rand.Float64()*3)
		}

		// Record the observations.
		// With{} sets the label values — order must match the label names declared above.
		httpRequestsTotal.With(prometheus.Labels{
			"method":      method,
			"path":        path,
			"status_code": status,
		}).Inc()

		httpRequestDuration.With(prometheus.Labels{
			"method": method,
			"path":   path,
		}).Observe(latencyMs / 1000.0) // Prometheus convention: seconds

		// Simulate ~10 requests per second with some jitter.
		time.Sleep(time.Duration(80+rand.Intn(40)) * time.Millisecond)
	}
}

// simulateGRPC generates gRPC request observations.
func simulateGRPC() {
	services := []string{"OrderService", "UserService", "ProductService", "PaymentService"}
	serviceWeights := []int{35, 25, 25, 15}

	// Methods per service (simplified — real gRPC would have service-specific methods).
	methods := []string{"GetUser", "CreateOrder", "ListProducts", "ProcessPayment", "GetOrder"}
	methodWeights := []int{30, 20, 25, 15, 10}

	// gRPC status codes — these are the canonical gRPC codes, not HTTP.
	// This is an important distinction for the mapping table.
	codes := []string{"OK", "NOT_FOUND", "INVALID_ARGUMENT", "INTERNAL", "UNAVAILABLE"}
	codeWeights := []int{75, 10, 8, 4, 3}

	types := []string{"unary", "server_stream"}
	typeWeights := []int{85, 15}

	for {
		service := weighted(services, serviceWeights)
		method := weighted(methods, methodWeights)
		code := weighted(codes, codeWeights)
		rpcType := weighted(types, typeWeights)

		latencyMs := 5 + rand.Float64()*195 // 5–200ms
		if code == "UNAVAILABLE" || code == "INTERNAL" {
			latencyMs = latencyMs * (0.2 + rand.Float64()*4)
		}

		grpcRequestsTotal.With(prometheus.Labels{
			"grpc_type":    rpcType,
			"grpc_service": service,
			"grpc_method":  method,
			"grpc_code":    code,
		}).Inc()

		grpcRequestDuration.With(prometheus.Labels{
			"grpc_type":    rpcType,
			"grpc_service": service,
			"grpc_method":  method,
		}).Observe(latencyMs / 1000.0)

		time.Sleep(time.Duration(120+rand.Intn(80)) * time.Millisecond)
	}
}

// simulateDB generates database client observations.
func simulateDB() {
	operations := []string{"SELECT", "INSERT", "UPDATE", "DELETE"}
	opWeights := []int{60, 20, 15, 5}

	// Simulate connection pool fluctuation as a gauge.
	go func() {
		for {
			// Active connections: fluctuate between 2 and 10.
			active := float64(2 + rand.Intn(9))
			dbConnectionsActive.With(prometheus.Labels{
				"db_system": "postgresql",
				"db_name":   "orders",
			}).Set(active)
			dbConnectionsActive.With(prometheus.Labels{
				"db_system": "redis",
				"db_name":   "cache",
			}).Set(float64(1 + rand.Intn(4)))
			time.Sleep(5 * time.Second)
		}
	}()

	for {
		op := weighted(operations, opWeights)

		// PostgreSQL queries.
		latencyMs := 1 + rand.Float64()*49 // 1–50ms typical
		if op == "SELECT" && rand.Float64() < 0.05 {
			latencyMs = 200 + rand.Float64()*800 // occasional slow query
		}
		dbQueryDuration.With(prometheus.Labels{
			"db_system":    "postgresql",
			"db_name":      "orders",
			"db_operation": op,
		}).Observe(latencyMs / 1000.0)

		// Occasional DB errors.
		if rand.Float64() < 0.02 {
			dbErrorsTotal.With(prometheus.Labels{
				"db_system":    "postgresql",
				"db_name":      "orders",
				"db_operation": op,
			}).Inc()
		}

		// Redis cache operations (much faster).
		cacheOp := weighted([]string{"GET", "SET", "DEL"}, []int{70, 25, 5})
		cacheLatency := 0.1 + rand.Float64()*2.9 // 0.1–3ms
		dbQueryDuration.With(prometheus.Labels{
			"db_system":    "redis",
			"db_name":      "cache",
			"db_operation": cacheOp,
		}).Observe(cacheLatency / 1000.0)

		time.Sleep(time.Duration(50+rand.Intn(100)) * time.Millisecond)
	}
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	// Seed the random number generator (Go 1.20+ does this automatically,
	// but explicit seeding is clear and safe for older versions too).
	rand.New(rand.NewSource(time.Now().UnixNano()))

	// Start the three simulation goroutines.
	// The `go` keyword launches a goroutine — think of it as a lightweight
	// concurrent thread managed by the Go runtime, not the OS.
	go simulateHTTP()
	go simulateGRPC()
	go simulateDB()

	// Register the /metrics endpoint using Prometheus's built-in HTTP handler.
	// This is what Prometheus scrapes — it formats all registered metrics
	// in the Prometheus text exposition format.
	http.Handle("/metrics", promhttp.Handler())

	// Simple health check endpoint so Prometheus can verify the app is up.
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})

	log.Println("prom-demo listening on :8080")
	log.Println("  /metrics — Prometheus scrape endpoint")
	log.Println("  /health  — health check")

	// ListenAndServe blocks forever, serving HTTP requests.
	// log.Fatal exits if the server fails to start (e.g. port already in use).
	log.Fatal(http.ListenAndServe(":8080", nil))
}
