#!/usr/bin/env python3
"""
tier2_engine.py — AI-assisted ES|QL translation (Tier 2)

Handles PromQL patterns that convert_panel.py's deterministic Tier 1 parser
cannot handle: regex label filters, multi-value exclusions, and anything
requiring `sum without(...)`.

Key architectural decision (2026-10-02, see docs/conversion-tool/architecture.md):
When a panel contains ANY target that needs Tier 2, this engine does not
translate that target in isolation. It rewrites the WHOLE panel's query as
a single ES|QL statement using EVAL + CASE to build a synthetic category
column, so Tier 1 series (simple equality filters) and Tier 2 series (regex/
exclusion-derived) are produced by one STATS ... BY bucket, category clause.
This is required because ES|QL / Kibana Lens has no clean way to union two
separate time-series STATS blocks into one visualization.

Usage:
    python3 tier2_engine.py --panel panels/cpu_basic.json
    python3 tier2_engine.py --selftest      # validator unit checks, no LLM call
    python3 tier2_engine.py --panel panels/cpu_basic.json --dry-run   # print prompt only

Requires Ollama running locally (OpenAI-compatible endpoint):
    brew install ollama && ollama pull qwen2.5-coder:7b
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

# Reuse Tier 1's mapping tables so the two tiers never drift apart.
try:
    from convert_panel import METRIC_MAP, LABEL_MAP, DATA_STREAM, parse_filters
except ImportError:
    # Allow running from a different working directory / standalone testing
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from convert_panel import METRIC_MAP, LABEL_MAP, DATA_STREAM, parse_filters


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1/chat/completions")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "60"))

# KNOWN GAP (carried from Tier 1, see convert_panel.py VARIABLE_MAP comment
# "$job: None  # Resolved from metric context" — never actually implemented):
# every validated panel uses job="$job", which is a Grafana template variable,
# not a literal. Tier 1 silently omits the service.name filter whenever it
# can't resolve $job to a literal, which only looks correct in this demo
# because there's currently only one job (node-exporter) in the data stream.
# Tier 2 resolves it explicitly instead of silently dropping the filter, so
# the AI-generated query stays scoped correctly even as more jobs get added.
# Override with PROM_JOB_NAME env var or --job CLI flag if this changes.
DEFAULT_JOB_RESOLUTION = os.environ.get("PROM_JOB_NAME", "node-exporter")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert ES|QL query translator for a Prometheus-to-\
Elasticsearch migration tool. You translate a whole Grafana timeseries panel's \
set of PromQL targets into ONE valid Elastic ES|QL query.

HARD CONSTRAINTS — violating any of these makes the output unusable:

1. Always use `TS <data_stream>` as the source command. Never use `FROM`.
2. `RATE(...)` must only ever wrap a field prefixed with `metrics.` \
(a counter_double field). Never call RATE() on an unprefixed field.
3. Unprefixed fields (e.g. `system.cpu.state`) are used for WHERE, EVAL, and \
CASE conditions — never inside RATE().
4. A field name containing a dot immediately followed by a digit needs \
backticks, e.g. `` `system.cpu.load_average.1m` ``.
5. If the panel's targets cannot each be produced by filtering to exactly \
one raw label value (i.e. any target needs a regex match, a multi-value \
exclusion, or an aggregation "without" a label), do NOT emit one STATS block \
per target. Instead emit EXACTLY ONE query with EXACTLY ONE STATS command \
total: use EVAL with a CASE expression to build a single synthetic category \
column that maps every raw label value to its correct legend name, covering \
every target in the panel (not just the hard ones). End the CASE with a NULL \
fallback and filter it out afterward with `WHERE <category_column> IS NOT NULL`. \
Then do a single \
`STATS <value> = <AGG>(RATE(...)) BY BUCKET(@timestamp, <n> seconds), <category_column>`. \
NEVER chain multiple STATS commands in one query — each STATS consumes the \
rows and columns from the stage before it, so a second STATS cannot see the \
original raw fields anymore. All series come from the ONE STATS, split by the \
category column — not from separate STATS blocks per series.
6. ES|QL's CASE is a FUNCTION, not SQL's `CASE WHEN ... THEN ... END` syntax. \
The correct and ONLY valid form is: \
`CASE(condition1, value1, condition2, value2, ..., default_value)` — a flat \
comma-separated argument list. Do not write `WHEN`, `THEN`, or `END` anywhere \
— those are invalid ES|QL and will fail to parse.
7. CASE evaluates its conditions left to right and returns the value paired \
with the first true condition — order matters. Put specific/narrow conditions \
before broad fallback conditions.
8. Never invent field names. Only use fields given to you in the mapping table.
9. Only use the fields and metric given in the task — do not hallucinate \
additional metrics.
10. ALL string literals MUST use double quotes: "system", "node-exporter", \
"irq". Single-quoted strings ('system') are INVALID in ES|QL and will cause a \
parsing error — this is a different language from Elasticsearch SQL (which \
does use single quotes); do not confuse the two. Every string value anywhere \
in the query — in WHERE, in CASE conditions, in IN (...) lists — must be \
double-quoted, with no exceptions.

WORKED EXAMPLE — this exact query was run and validated against live \
Elasticsearch data, and is the pattern to follow for any panel needing a \
unified category column:

Targets: mode="system" -> "Busy System", mode="user" -> "Busy User", \
mode IN ("irq","softirq") -> "Busy IRQs", mode="idle" -> "Idle"

Correct ES|QL:
TS metrics-prometheusreceiver.otel-default
| WHERE @timestamp >= NOW() - 1 hour
| WHERE service.name == "node-exporter"
| EVAL cpu_category = CASE(
    `system.cpu.state` == "system",  "Busy System",
    `system.cpu.state` == "user",    "Busy User",
    `system.cpu.state` IN ("irq", "softirq"), "Busy IRQs",
    `system.cpu.state` == "idle",    "Idle",
    null
  )
| WHERE cpu_category IS NOT NULL
| STATS cpu_rate = AVG(RATE(`metrics.system.cpu.time`))
    BY BUCKET(@timestamp, 30 seconds), cpu_category

Notice: ONE EVAL with ONE CASE(...) function call (flat comma list, no WHEN/\
THEN/END), ONE WHERE to drop nulls, ONE STATS at the end producing ONE value \
column grouped by the bucket and the category — this single STATS produces \
ALL the named series simultaneously via the category column. Follow this \
exact shape for the panel you are given, adapting the CASE conditions and \
legend names to match its targets.

You will be given: the panel's PromQL targets (expression + legend name), \
the field mapping table, the ES|QL data stream name, and the aggregation \
window / bucket size to use.

Respond with ONLY a single JSON object, no markdown fences, no prose outside \
the JSON:
{
  "esql": "<the full ES|QL query as a single string with \\n between pipe stages>",
  "explanation": "<2-4 sentences: what the query does, and any approximation made>",
  "confidence": "high" | "medium" | "low",
  "tier": 2,
  "approximations": ["<short bullet of any semantic approximation>", "..."]
}
"""


def build_user_prompt(
    panel_title: str,
    targets: List[Dict[str, Any]],
    service_name: Optional[str],
    bucket_seconds: int = 30,
    window: str = "1 hour",
) -> str:
    """
    Build the user-turn prompt: panel targets + field mapping context +
    the exact ES|QL constraints the output must satisfy.
    """
    target_lines = []
    for t in targets:
        target_lines.append(
            f'  - expr: {t.get("expr", "")}\n'
            f'    legend: "{t.get("legendFormat", "")}"'
        )

    metric_map_lines = [
        f'  {promql} -> metrics_field: {m[0]} | filter_field: {m[1]}'
        for promql, m in METRIC_MAP.items()
    ]
    label_map_lines = [
        f'  {prom_label} -> {esql_field}'
        for prom_label, esql_field in LABEL_MAP.items()
    ]

    prompt = f"""Panel title: {panel_title}
Data stream: {DATA_STREAM}
Service name filter (from Grafana $job): {service_name or "(none resolved — omit service.name filter)"}
Aggregation window: last {window}
Time bucket size: {bucket_seconds} seconds

PromQL targets in this panel (ALL of them — your single query must produce
every one of these as a named series):
{chr(10).join(target_lines)}

Metric name mapping (Prometheus metric -> ES|QL fields):
{chr(10).join(metric_map_lines)}

Label mapping (Prometheus label -> ES|QL field):
{chr(10).join(label_map_lines)}

Produce the single unified ES|QL query per the system instructions, covering
every target above with its exact legend name as the category value.
"""
    return prompt


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

def call_ollama(system_prompt: str, user_prompt: str) -> str:
    """Call the local Ollama OpenAI-compatible chat endpoint. Returns raw text."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "stream": False,
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_URL} — is it running? "
            f"(brew services start ollama / ollama serve). Original error: {e}"
        )
    return data["choices"][0]["message"]["content"]


def parse_ai_response(raw: str) -> Dict[str, Any]:
    """Parse the model's JSON response, tolerating stray markdown fences."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"AI response was not valid JSON: {e}\nRaw response:\n{raw}")

    required_keys = {"esql", "explanation", "confidence", "tier"}
    missing = required_keys - parsed.keys()
    if missing:
        raise ValueError(f"AI response missing required keys: {missing}")
    return parsed


# ---------------------------------------------------------------------------
# Mechanical validator — checks the hard constraints before a human sees it
# ---------------------------------------------------------------------------

def validate_esql(esql: str, expected_legends: Optional[List[str]] = None) -> List[str]:
    """
    Check an AI-produced ES|QL query against the non-negotiable constraints.
    Returns a list of violation strings (empty list = passed all checks).
    This does NOT execute the query — it's a static, mechanical check only.
    """
    issues: List[str] = []
    stripped = esql.strip()

    # 1. Must start with TS, not FROM
    if not re.match(r'^\s*TS\s+\S', stripped):
        issues.append("Query must start with `TS <data_stream>`, not FROM or another source command.")

    if re.search(r'^\s*FROM\s', stripped, re.MULTILINE):
        issues.append("Query contains a `FROM` source command — only `TS` is allowed for this data stream.")

    # 2. RATE() must only wrap metrics.*-prefixed fields
    for m in re.finditer(r'RATE\(\s*`?([a-zA-Z0-9_.]+)`?\s*\)', stripped):
        field = m.group(1)
        if not field.startswith("metrics."):
            issues.append(
                f"RATE() called on non-metrics field `{field}` — RATE() requires "
                f"a `metrics.`-prefixed counter_double field."
            )

    # 3. metrics.* fields should only appear inside RATE(...), not bare in WHERE/CASE
    bare_metrics_refs = re.findall(r'(?<!RATE\()`?(metrics\.[a-zA-Z0-9_.]+)`?', stripped)
    rate_wrapped = set(re.findall(r'RATE\(\s*`?(metrics\.[a-zA-Z0-9_.]+)`?\s*\)', stripped))
    for ref in bare_metrics_refs:
        if ref not in rate_wrapped and f'RATE(`{ref}`)' not in stripped and f'RATE({ref})' not in stripped:
            # Heuristic — flag for human review rather than hard-fail, since
            # regex overlap can produce false positives here.
            issues.append(
                f"Possible bare reference to `{ref}` outside RATE() — verify this is intentional."
            )

    # 4. Balanced parentheses
    if stripped.count("(") != stripped.count(")"):
        issues.append("Unbalanced parentheses in ES|QL query.")

    # 4a. Single-quoted string literals are invalid ES|QL (forbidden, parse
    # error) — the model sometimes confuses this with Elasticsearch SQL,
    # which does use single quotes. Must be double-quoted instead.
    single_quoted = re.findall(r"'[^']*'", stripped)
    if single_quoted:
        examples = ", ".join(single_quoted[:3])
        issues.append(
            f"Query uses single-quoted string literal(s) ({examples}) — ES|QL "
            f"FORBIDS single quotes for strings and will throw a parsing "
            f"error. Every string literal must use double quotes instead, "
            f"e.g. \"system\" not 'system'."
        )

    # 4b. SQL-style CASE WHEN...END is invalid ES|QL — CASE is a flat function
    if re.search(r'\bCASE\s+WHEN\b', stripped, re.IGNORECASE):
        issues.append(
            "Query uses SQL-style `CASE WHEN ... THEN ... END` syntax, which is "
            "INVALID in ES|QL. ES|QL's CASE is a function: "
            "CASE(condition1, value1, condition2, value2, ..., default) — a flat "
            "comma-separated argument list with no WHEN/THEN/END keywords."
        )

    # 4c. Multiple chained STATS — structurally broken, each STATS consumes
    # the prior stage's columns so a second STATS can't see raw fields anymore
    stats_count = len(re.findall(r'\|\s*STATS\b', stripped, re.IGNORECASE))
    if stats_count > 1:
        issues.append(
            f"Query has {stats_count} STATS commands chained — ES|QL cannot "
            f"chain multiple STATS to produce multiple named series, since each "
            f"STATS consumes the columns from the stage before it. Use exactly "
            f"ONE STATS ... BY bucket, category_column instead, with the "
            f"category column (from EVAL + CASE) producing all series at once."
        )

    # 5. If multiple legends expected, there should be a CASE + BUCKET + category GROUP BY
    if expected_legends and len(expected_legends) > 1:
        if "CASE(" not in stripped.upper() and "CASE (" not in stripped.upper():
            issues.append(
                f"Panel has {len(expected_legends)} legend series but query has no "
                f"valid CASE(...) function call — multi-series panels with "
                f"non-trivial filters need a unified category column (see "
                f"architecture.md). Remember: CASE is a function, not CASE WHEN."
            )
        if "BUCKET(@timestamp" not in stripped:
            issues.append("Timeseries query is missing a BUCKET(@timestamp, ...) grouping.")

        for legend in expected_legends:
            if legend not in stripped:
                issues.append(f"Expected legend \"{legend}\" does not appear anywhere in the query.")

    # 6. CASE must have a fallback path and the NULL should be filtered
    if ("CASE(" in stripped.upper() or "CASE (" in stripped.upper()):
        if "NULL" not in stripped.upper():
            issues.append("CASE expression has no NULL fallback for unmatched values.")
        if "IS NOT NULL" not in stripped.upper():
            issues.append("Query does not filter out the CASE's NULL fallback with `WHERE ... IS NOT NULL`.")

    return issues


# ---------------------------------------------------------------------------
# Top-level translation entry point
# ---------------------------------------------------------------------------

def translate_tier2(
    panel_title: str,
    targets: List[Dict[str, Any]],
    bucket_seconds: int = 30,
    window: str = "1 hour",
    max_retries: int = 2,
) -> Dict[str, Any]:
    """
    Translate a full panel's targets via the AI engine. Retries once with
    validator feedback appended to the prompt if the first response fails
    mechanical validation.
    """
    service_name = _resolve_service_name(targets)

    expected_legends = [t.get("legendFormat", "") for t in targets if t.get("legendFormat")]
    user_prompt = build_user_prompt(panel_title, targets, service_name, bucket_seconds, window)

    last_result = None
    last_issues: List[str] = []

    for attempt in range(max_retries + 1):
        prompt = user_prompt
        if last_issues:
            prompt += (
                "\n\nYour previous attempt failed these checks — fix them and "
                "return a corrected query:\n"
                + "\n".join(f"  - {i}" for i in last_issues)
            )

        raw = call_ollama(SYSTEM_PROMPT, prompt)
        result = parse_ai_response(raw)
        issues = validate_esql(result["esql"], expected_legends)

        result["validation_issues"] = issues
        result["attempt"] = attempt + 1
        last_result = result
        last_issues = issues

        if not issues:
            break

    return last_result


def _extract_filters_str(expr: str) -> str:
    """Pull the {...} filter block out of a PromQL expression, if present."""
    m = re.search(r'\{([^}]*)\}', expr)
    return m.group(1) if m else ""


def _resolve_service_name(targets: List[Dict[str, Any]]) -> Optional[str]:
    """
    Resolve the job/service name for a set of targets. Prefers a literal
    job="..." value if any target has one; falls back to DEFAULT_JOB_RESOLUTION
    when every target uses the unresolved $job template variable (see the
    KNOWN GAP note above DEFAULT_JOB_RESOLUTION).
    """
    for t in targets:
        for f in parse_filters(_extract_filters_str(t.get("expr", ""))):
            if f["label"] == "job" and not f["value"].startswith("$"):
                return f["value"]

    uses_job_var = any(
        f["label"] == "job" and f["value"].startswith("$")
        for t in targets
        for f in parse_filters(_extract_filters_str(t.get("expr", "")))
    )
    return DEFAULT_JOB_RESOLUTION if uses_job_var else None


# ---------------------------------------------------------------------------
# Self-test — validator unit checks, no LLM / network required
# ---------------------------------------------------------------------------

def _selftest() -> int:
    print("Running tier2_engine self-tests (validator only, no LLM call)...\n")
    failures = 0

    # Case 1: the exact unified query from the architecture discussion — should PASS
    good_query = """TS metrics-prometheusreceiver.otel-default
| WHERE @timestamp >= NOW() - 1 hour
| WHERE service.name == "node-exporter"
| EVAL cpu_category = CASE(
    `system.cpu.state` == "system",  "Busy System",
    `system.cpu.state` == "user",    "Busy User",
    `system.cpu.state` == "iowait",  "Busy Iowait",
    `system.cpu.state` IN ("irq", "softirq"), "Busy IRQs",
    `system.cpu.state` == "idle",    "Idle",
    `system.cpu.state` NOT IN ("idle","user","system","iowait","irq","softirq"), "Busy Other",
    null
  )
| WHERE cpu_category IS NOT NULL
| STATS cpu_rate = AVG(RATE(`metrics.system.cpu.time`))
    BY BUCKET(@timestamp, 30 seconds), cpu_category"""
    legends = ["Busy System", "Busy User", "Busy Iowait", "Busy IRQs", "Idle", "Busy Other"]
    issues = validate_esql(good_query, legends)
    print(f"[Case 1: valid unified query] issues found: {len(issues)}")
    for i in issues:
        print(f"    ! {i}")
    if issues:
        failures += 1
    print()

    # Case 2: RATE() called on an unprefixed field — should FAIL
    bad_query = """TS metrics-prometheusreceiver.otel-default
| WHERE service.name == "node-exporter"
| STATS cpu_rate = AVG(RATE(`system.cpu.time`))"""
    issues = validate_esql(bad_query)
    print(f"[Case 2: RATE() on unprefixed field] issues found: {len(issues)} (expect >= 1)")
    for i in issues:
        print(f"    ! {i}")
    if not issues:
        print("    FAILED — validator should have caught this")
        failures += 1
    print()

    # Case 3: FROM instead of TS — should FAIL
    bad_query_2 = """FROM metrics-prometheusreceiver.otel-default
| STATS cpu_rate = AVG(RATE(`metrics.system.cpu.time`))"""
    issues = validate_esql(bad_query_2)
    print(f"[Case 3: FROM instead of TS] issues found: {len(issues)} (expect >= 1)")
    for i in issues:
        print(f"    ! {i}")
    if not issues:
        print("    FAILED — validator should have caught this")
        failures += 1
    print()

    # Case 4: multi-series panel with no CASE — should FAIL
    bad_query_3 = """TS metrics-prometheusreceiver.otel-default
| WHERE service.name == "node-exporter"
| STATS cpu_rate = AVG(RATE(`metrics.system.cpu.time`)) BY BUCKET(@timestamp, 30 seconds)"""
    issues = validate_esql(bad_query_3, legends)
    print(f"[Case 4: multi-legend panel missing CASE] issues found: {len(issues)} (expect >= 1)")
    for i in issues:
        print(f"    ! {i}")
    if not issues:
        print("    FAILED — validator should have caught this")
        failures += 1
    print()

    # Case 4b: REGRESSION — the actual broken output from the first real
    # Ollama run (qwen2.5-coder:7b, 2026-10-02): SQL-style CASE WHEN...END
    # plus 6 chained STATS blocks. Both bugs must be caught.
    real_bad_output = (
        "TS metrics-prometheusreceiver.otel-default | EVAL category = CASE WHEN "
        "system.cpu.state = 'system' THEN 'Busy System' WHEN system.cpu.state = "
        "'user' THEN 'Busy User' WHEN system.cpu.state = 'iowait' THEN 'Busy "
        "Iowait' WHEN system.cpu.state = 'idle' THEN 'Idle' WHEN "
        "system.cpu.state LIKE '%irq' THEN 'Busy IRQs' ELSE 'Busy Other' END "
        "| WHERE category IS NOT NULL "
        "| STATS `Busy System` = AVG(RATE(metrics.system.cpu.time)) BY "
        "BUCKET(@timestamp, 30s), category "
        "| STATS `Busy User` = AVG(RATE(metrics.system.cpu.time)) BY "
        "BUCKET(@timestamp, 30s), category "
        "| STATS `Idle` = AVG(RATE(metrics.system.cpu.time)) BY "
        "BUCKET(@timestamp, 30s), category"
    )
    issues = validate_esql(real_bad_output, ["Busy System", "Busy User", "Idle"])
    has_case_when_issue = any("CASE WHEN" in i for i in issues)
    has_multi_stats_issue = any("STATS commands chained" in i for i in issues)
    print(f"[Case 4b: REGRESSION — real Ollama failure mode] issues found: {len(issues)}")
    for i in issues:
        print(f"    ! {i}")
    print(f"    {'OK' if has_case_when_issue else 'FAILED'} — caught SQL-style CASE WHEN")
    print(f"    {'OK' if has_multi_stats_issue else 'FAILED'} — caught chained STATS")
    if not (has_case_when_issue and has_multi_stats_issue):
        failures += 1
    print()

    # Case 4c: REGRESSION — single-quote query that was confirmed to throw
    # a real parsing_exception against the live Elasticsearch cluster
    # (2026-10-02): "line 4:25: token recognition error at: '''"
    single_quote_query = """TS metrics-prometheusreceiver.otel-default
| WHERE @timestamp >= NOW() - 1 hour
| WHERE service.name == 'node-exporter'
| EVAL cpu_category = CASE(
    `system.cpu.state` == 'system', 'Busy System',
    `system.cpu.state` == 'idle', 'Idle',
    null
  )
| WHERE cpu_category IS NOT NULL
| STATS cpu_rate = AVG(RATE(`metrics.system.cpu.time`))
    BY BUCKET(@timestamp, 30 seconds), cpu_category"""
    issues = validate_esql(single_quote_query, ["Busy System", "Idle"])
    has_quote_issue = any("single-quoted" in i for i in issues)
    print(f"[Case 4c: REGRESSION — single-quote parse error] issues found: {len(issues)}")
    for i in issues:
        print(f"    ! {i}")
    print(f"    {'OK' if has_quote_issue else 'FAILED'} — caught single-quoted string literals")
    if not has_quote_issue:
        failures += 1
    print()

    # Case 5: prompt construction smoke test (D and E targets from the handoff)
    targets = [
        {
            "expr": 'avg(sum without(mode)(rate(node_cpu_seconds_total{instance="$node",job="$job", mode=~".*irq"}[$__rate_interval])))',
            "legendFormat": "Busy IRQs",
        },
        {
            "expr": "avg(sum without(mode)(rate(node_cpu_seconds_total{instance=\"$node\",job=\"$job\", mode!='idle',mode!='user',mode!='system',mode!='iowait',mode!='irq',mode!='softirq'}[$__rate_interval])))",
            "legendFormat": "Busy Other",
        },
    ]
    prompt = build_user_prompt("CPU Basic", targets, service_name="node-exporter")
    checks = [
        ("Busy IRQs" in prompt, "legend 'Busy IRQs' present in prompt"),
        ("Busy Other" in prompt, "legend 'Busy Other' present in prompt"),
        ("node_cpu_seconds_total" in prompt, "metric name present in prompt"),
        ("metrics.system.cpu.time" in prompt, "mapped metrics field present in prompt"),
        ("system.cpu.state" in prompt, "mapped label field present in prompt"),
    ]
    print("[Case 5: prompt construction]")
    for ok, desc in checks:
        print(f"    {'OK' if ok else 'FAILED'} — {desc}")
        if not ok:
            failures += 1
    print()

    print(f"{'ALL PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Tier 2 AI-assisted ES|QL translation")
    parser.add_argument("--panel", help="Path to Grafana panel JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Print the prompt only, don't call the LLM")
    parser.add_argument("--selftest", action="store_true", help="Run validator unit checks, no LLM/network needed")
    parser.add_argument("--bucket-seconds", type=int, default=30)
    parser.add_argument("--window", default="1 hour")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()

    if not args.panel:
        parser.error("--panel is required unless --selftest is given")

    with open(args.panel) as f:
        panel = json.load(f)

    targets = panel.get("targets", [])
    title = panel.get("title", "Unknown Panel")
    service_name = _resolve_service_name(targets)

    prompt = build_user_prompt(title, targets, service_name, args.bucket_seconds, args.window)

    if args.dry_run:
        print("=== SYSTEM PROMPT ===")
        print(SYSTEM_PROMPT)
        print("\n=== USER PROMPT ===")
        print(prompt)
        return 0

    result = translate_tier2(title, targets, args.bucket_seconds, args.window)
    print(json.dumps(result, indent=2))
    return 0 if not result.get("validation_issues") else 1


if __name__ == "__main__":
    sys.exit(main())
