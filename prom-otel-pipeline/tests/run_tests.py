#!/usr/bin/env python3
"""
run_tests.py — Automated test runner for the prom-demo OTel normalization pipeline.

Usage:
    # Run all tests
    python3 run_tests.py

    # Run with JSON output (for CI)
    python3 run_tests.py --json

    # Run only tests matching a name fragment
    python3 run_tests.py --filter http

    # Run against a different data stream
    python3 run_tests.py --stream metrics-prometheusreceiver.otel-default

    # Credentials via environment (recommended)
    export ES_ENDPOINT=https://...
    export ES_API_KEY=...
    python3 run_tests.py

    # Or pass credentials directly
    python3 run_tests.py --endpoint https://... --api-key ...

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
    2 — configuration error (missing credentials, unreachable endpoint)
"""

import argparse
import json
import os
import sys
import time
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is required. Install with: pip install requests")
    sys.exit(2)

from test_cases import TEST_CASES


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class AssertionResult:
    passed: bool
    message: str


@dataclass
class TestResult:
    name: str
    description: str
    passed: bool
    duration_ms: float
    assertion_results: List[AssertionResult] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# ES|QL query execution
# ---------------------------------------------------------------------------

def run_esql(endpoint: str, api_key: str, query: str) -> Dict[str, Any]:
    """Execute an ES|QL query and return the response as a dict."""
    url = f"{endpoint}/_query"
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "Content-Type": "application/json",
    }
    # Normalize whitespace in query
    clean_query = textwrap.dedent(query).strip()

    response = requests.post(
        url,
        headers=headers,
        json={"query": clean_query},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def parse_rows(response: dict) -> list[Dict[str, Any]]:
    """Convert ES|QL columnar response into a list of row dicts."""
    columns = [col["name"] for col in response.get("columns", [])]
    rows = response.get("values", [])
    return [dict(zip(columns, row)) for row in rows]


# ---------------------------------------------------------------------------
# Assertion evaluation
# ---------------------------------------------------------------------------

def evaluate_assertion(assertion: dict, rows: List[Dict]) -> AssertionResult:
    """Evaluate a single assertion against the query result rows."""

    assertion_type = assertion["type"]

    if assertion_type == "row_count":
        expected = assertion["expected"]
        actual = len(rows)
        passed = actual == expected
        return AssertionResult(
            passed=passed,
            message=f"row_count: expected {expected}, got {actual}",
        )

    if assertion_type == "field_not_null":
        field_name = assertion["field"]
        row_index = assertion.get("row", 0)
        if row_index >= len(rows):
            return AssertionResult(
                passed=False,
                message=f"field_not_null({field_name}): no row at index {row_index}",
            )
        value = rows[row_index].get(field_name)
        passed = value is not None
        return AssertionResult(
            passed=passed,
            message=f"field_not_null({field_name}): value={value!r}",
        )

    if assertion_type == "field_equals":
        field_name = assertion["field"]
        expected = assertion["expected"]
        row_index = assertion.get("row", 0)
        if row_index >= len(rows):
            return AssertionResult(
                passed=False,
                message=f"field_equals({field_name}): no row at index {row_index}",
            )
        actual = rows[row_index].get(field_name)
        # Coerce numeric types for comparison
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            passed = float(actual) == float(expected)
        else:
            passed = actual == expected
        return AssertionResult(
            passed=passed,
            message=f"field_equals({field_name}): expected {expected!r}, got {actual!r}",
        )

    if assertion_type == "field_greater_than":
        field_name = assertion["field"]
        threshold = assertion["threshold"]
        row_index = assertion.get("row", 0)
        if row_index >= len(rows):
            return AssertionResult(
                passed=False,
                message=f"field_greater_than({field_name}): no row at index {row_index}",
            )
        actual = rows[row_index].get(field_name)
        try:
            passed = float(actual) > float(threshold)
        except (TypeError, ValueError):
            passed = False
        return AssertionResult(
            passed=passed,
            message=f"field_greater_than({field_name}): {actual!r} > {threshold}",
        )

    if assertion_type == "field_distinct_count":
        field_name = assertion["field"]
        expected = assertion["expected"]
        row_index = assertion.get("row", 0)
        if row_index >= len(rows):
            return AssertionResult(
                passed=False,
                message=f"field_distinct_count({field_name}): no row at index {row_index}",
            )
        actual = rows[row_index].get(field_name)
        try:
            passed = int(actual) == int(expected)
        except (TypeError, ValueError):
            passed = False
        return AssertionResult(
            passed=passed,
            message=f"field_distinct_count({field_name}): expected {expected}, got {actual}",
        )

    return AssertionResult(
        passed=False,
        message=f"Unknown assertion type: {assertion_type!r}",
    )


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

def run_test(test_case: dict, endpoint: str, api_key: str) -> TestResult:
    """Execute a single test case and return its result."""
    start = time.monotonic()
    name = test_case["name"]
    description = test_case["description"]

    try:
        response = run_esql(endpoint, api_key, test_case["query"])
        rows = parse_rows(response)
    except requests.HTTPError as e:
        duration_ms = (time.monotonic() - start) * 1000
        try:
            error_body = e.response.json()
            reason = error_body.get("error", {}).get("reason", str(e))
        except Exception:
            reason = str(e)
        return TestResult(
            name=name,
            description=description,
            passed=False,
            duration_ms=duration_ms,
            error=f"HTTP {e.response.status_code}: {reason}",
        )
    except Exception as e:
        duration_ms = (time.monotonic() - start) * 1000
        return TestResult(
            name=name,
            description=description,
            passed=False,
            duration_ms=duration_ms,
            error=str(e),
        )

    assertion_results = []
    for assertion in test_case.get("assertions", []):
        result = evaluate_assertion(assertion, rows)
        assertion_results.append(result)

    all_passed = all(r.passed for r in assertion_results)
    duration_ms = (time.monotonic() - start) * 1000

    return TestResult(
        name=name,
        description=description,
        passed=all_passed,
        duration_ms=duration_ms,
        assertion_results=assertion_results,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
SKIP = "\033[33m-\033[0m"


def print_human(results: List[TestResult], total_duration_ms: float) -> None:
    """Print results in human-readable format."""
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    print()
    print("━" * 60)
    print("  prom-demo OTel Pipeline — Test Results")
    print("━" * 60)

    for result in results:
        icon = PASS if result.passed else FAIL
        print(f"  {icon}  {result.name:<40} {result.duration_ms:6.0f}ms")
        if not result.passed:
            if result.error:
                print(f"        ERROR: {result.error}")
            for ar in result.assertion_results:
                icon2 = "  " if ar.passed else "  ✗"
                if not ar.passed:
                    print(f"     {icon2} {ar.message}")

    print()
    print("━" * 60)
    color = "\033[32m" if failed == 0 else "\033[31m"
    reset = "\033[0m"
    print(f"  {color}{passed}/{total} passed{reset}  "
          f"{'(' + str(failed) + ' failed)  ' if failed else ''}"
          f"{total_duration_ms:.0f}ms total")
    print("━" * 60)
    print()


def print_json_output(results: List[TestResult], total_duration_ms: float) -> None:
    """Print results as JSON."""
    output = {
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
            "total_duration_ms": round(total_duration_ms, 1),
        },
        "tests": [
            {
                "name": r.name,
                "description": r.description,
                "passed": r.passed,
                "duration_ms": round(r.duration_ms, 1),
                "error": r.error,
                "assertions": [
                    {"passed": ar.passed, "message": ar.message}
                    for ar in r.assertion_results
                ],
            }
            for r in results
        ],
    }
    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run automated tests against the OTel normalization pipeline."
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("ES_ENDPOINT", ""),
        help="Elasticsearch endpoint (or set ES_ENDPOINT env var)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ES_API_KEY", ""),
        help="Elasticsearch API key (or set ES_API_KEY env var)",
    )
    parser.add_argument(
        "--filter",
        default="",
        help="Only run tests whose name contains this string",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON (for CI integration)",
    )
    parser.add_argument(
        "--stream",
        default="",
        help="Override data stream name in queries",
    )
    args = parser.parse_args()

    # Validate credentials
    if not args.endpoint:
        print("ERROR: ES_ENDPOINT not set. Use --endpoint or export ES_ENDPOINT=...")
        return 2
    if not args.api_key:
        print("ERROR: ES_API_KEY not set. Use --api-key or export ES_API_KEY=...")
        return 2

    # Select and optionally filter test cases
    cases = TEST_CASES
    if args.filter:
        cases = [t for t in cases if args.filter.lower() in t["name"].lower()]
        if not cases:
            print(f"No tests matched filter: {args.filter!r}")
            return 2

    # Override data stream if requested
    if args.stream:
        cases = [
            {**t, "query": t["query"].replace(
                "metrics-prometheusreceiver.otel-default", args.stream
            )}
            for t in cases
        ]

    if not args.json:
        print(f"\nRunning {len(cases)} test(s) against {args.endpoint}")
        print(f"Filter: {args.filter!r}" if args.filter else "")

    # Run tests
    start = time.monotonic()
    results = []
    for case in cases:
        if not args.json:
            print(f"  Running {case['name']}...", end="", flush=True)
        result = run_test(case, args.endpoint, args.api_key)
        results.append(result)
        if not args.json:
            icon = "✓" if result.passed else "✗"
            print(f"\r  {icon}  {case['name']}")

    total_duration_ms = (time.monotonic() - start) * 1000

    # Output results
    if args.json:
        print_json_output(results, total_duration_ms)
    else:
        print_human(results, total_duration_ms)

    # Exit code
    failed = sum(1 for r in results if not r.passed)
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
