# prom-demo Training Guide

This is a conceptual and operational guide to the prom-demo migration tool —
why it's built the way it is, what each part actually does, and the specific
mistakes that got made and fixed along the way. The top-level `README.md` and
`conversion-tool/README.md` tell you *how* to run things. This document tells
you *why*, in more depth, and documents the non-obvious gotchas that cost
real debugging time to discover — so the next person (including a future you)
doesn't have to rediscover them.

## Table of contents

1. [Why this tool exists](#1-why-this-tool-exists)
2. [Architecture overview](#2-architecture-overview)
3. [Tier 1: deterministic conversion](#3-tier-1-deterministic-conversion)
4. [Tier 2: AI-assisted conversion](#4-tier-2-ai-assisted-conversion)
5. [ES|QL gotchas](#5-esql-gotchas)
6. [Running the stack locally](#6-running-the-stack-locally)
7. [The Streamlit UI](#7-the-streamlit-ui)
8. [Extending the tool](#8-extending-the-tool)
9. [Known limitations / roadmap](#9-known-limitations--roadmap)
10. [Appendix: command reference](#10-appendix-command-reference)

---

## 1. Why this tool exists

Organizations running Prometheus + Grafana who want to move to Elastic
Observability face two separate problems, and this tool addresses both:

**Problem 1 — field name mismatch.** Prometheus metrics land in Elasticsearch
with flat, underscore-style labels (`job`, `method`, `status_code`). OTel-native
traces and logs use dot-notation semantic convention field names
(`service.name`, `http.request.method`, `http.response.status_code`). Without
normalization, metrics can't be correlated with traces and logs from the same
system — the field names simply don't match up. The OTel Collector pipeline
in this project fixes this on ingestion.

**Problem 2 — every dashboard has to be rewritten by hand.** Even once the
data is normalized, every Grafana panel's PromQL query still needs to become
an ES|QL query against the new field names, and every Grafana panel
definition needs to become a Kibana Lens panel definition. For an
organization with dozens of dashboards and hundreds of panels, this is the
actual bulk of migration effort — and it's exactly the kind of repetitive,
pattern-matching work that's worth automating, with a fallback to AI for the
parts that don't fit a fixed pattern, and a fallback to a human for the parts
that don't fit anything at all.

This tool's three-tier conversion model (deterministic → AI-assisted → human)
is the answer to problem 2. Everything from here on is mostly about that.

## 2. Architecture overview

```
Node Exporter (:9100/metrics)
    │
    ▼
OTel Collector (otelcol-contrib)
    │  processors: memory_limiter, cumulative_to_delta (histograms only),
    │              resource_detection, transform/* (semantic convention
    │              mapping), batch
    ▼
Elasticsearch exporter (mapping mode: otel)
    ▼
metrics-prometheusreceiver.otel-default (Elastic Serverless, TSDS)
    ▲
    │  ES|QL queries (TS command, not FROM — see §5)
    │
Grafana panel JSON ──► convert_panel.py ──► Kibana panel JSON
                              │
                              ├─ Tier 1: deterministic (parse_promql, regex-based)
                              │
                              └─ Tier 2: tier2_engine.py
                                     │
                                     ▼
                                 Ollama (qwen2.5-coder:7b, local)
                                     │
                                     ▼
                                 mechanical validator ──► retry loop (max 2)
                                     │
                                     ▼
                                 validated ES|QL
```

Two independently useful halves:

- **The pipeline** (OTel Collector + mapping) solves the field-name problem.
  It's what's validated by `docs/esql-sanity-checks.md` and
  `prom-otel-pipeline/tests/`.
- **The conversion tool** (`conversion-tool/`) solves the dashboard-rewriting
  problem. It's independent of which metrics domain it's converting — the
  current field mappings happen to be for Node Exporter (system/CPU metrics),
  but the three-tier model generalizes to any Prometheus exporter.

## 3. Tier 1: deterministic conversion

Tier 1 lives in `convert_panel.py`'s `parse_promql()` and related functions.
It handles a fixed set of PromQL shapes via regex matching:

| Pattern | Example | Panel type |
|---|---|---|
| `metric{filters}` | current gauge value | stat |
| `agg(rate(metric{filters}[interval]))` | CPU rate | timeseries |
| `scalar * (1 - agg(rate(metric{filters}[interval])))` | CPU Busy % | gauge |
| `count(count(metric) by (label))` | CPU Cores count | stat |

For timeseries panels with multiple targets (e.g. one line per CPU mode),
Tier 1 merges them into a single ES|QL query: each target becomes one value
in a `WHERE field IN (...)` clause, and the same field becomes the `BY`
grouping column that splits the chart into separate series. This works
*only* when every target filters to exactly one raw label value — which is
precisely the assumption that breaks for the patterns Tier 2 exists to
handle (see §4).

**What makes Tier 1 trustworthy:** it's a deterministic function. Given the
same input, it always produces the same output, and that output can be read
and verified by inspection — there's no retry loop, no confidence score, no
"it probably means this." This is worth stating explicitly, because Tier 2
deliberately does *not* have this property, and the difference is the whole
reason Tier 2 needed an auditability feature (§4, §7) that Tier 1 never
needed.

**A known gap:** every sample panel filters on `job="$job"` — a Grafana
template variable, not a literal value. Tier 1 can't resolve `$job` to an
actual job name, so when it can't, it just **drops the `service.name` filter
from the query entirely** rather than erroring. This is invisible and
harmless in this demo environment (there's only one job in the data stream),
but it's a real correctness risk the moment a second job starts writing to
the same data stream — the query would then silently aggregate across both.
Tier 2 does not inherit this gap (see §4); Tier 1 still has it.

## 4. Tier 2: AI-assisted conversion

### Why it exists

Two of the sample panel's six targets don't fit Tier 1's "one target = one
filter value" model:

- **Busy IRQs:** `mode=~".*irq"` — a regex matching *two* raw values
  (`irq`, `softirq`) that need to collapse into *one* legend series.
- **Busy Other:** a six-value exclusion (`mode!='idle',mode!='user',...`) —
  everything that doesn't fit the named categories, again collapsed into one
  series.

### Why the whole panel gets rewritten, not just the hard targets

The natural-seeming approach — handle the four easy targets with Tier 1 and
the two hard ones with Tier 2 separately — doesn't work, for a structural
reason: **ES|QL can't union two separate time-series `STATS` queries into one
Kibana Lens visualization.** A Lens panel expects a single query producing
all of its series. So when any target in a panel needs Tier 2, the AI
rewrites the *entire* panel's query as one unified statement:

```esql
TS metrics-prometheusreceiver.otel-default
| WHERE @timestamp >= NOW() - 1 hour
| WHERE service.name == "node-exporter"
| EVAL category = CASE(
    `system.cpu.state` == "system",  "Busy System",
    `system.cpu.state` == "user",    "Busy User",
    `system.cpu.state` == "iowait",  "Busy Iowait",
    `system.cpu.state` == "idle",    "Idle",
    `system.cpu.state` IN ("irq", "softirq"), "Busy IRQs",
    `system.cpu.state` != "idle" AND `system.cpu.state` != "user" AND
      `system.cpu.state` != "system" AND `system.cpu.state` != "iowait" AND
      `system.cpu.state` != "irq" AND `system.cpu.state` != "softirq", "Busy Other",
    null
  )
| WHERE category IS NOT NULL
| STATS metric_value = AVG(RATE(`metrics.system.cpu.time`))
    BY BUCKET(@timestamp, 30 seconds), category
```

A single `EVAL` + `CASE` builds a synthetic category column covering *every*
target in the panel (not just the hard ones), and a single `STATS` produces
every named series via that one column. Lens then derives the legend
directly from the category column's distinct values — no separate mapping
step needed, since the category values *are* the legend names.

This was validated live, in stages: first the mechanism alone (grouping two
arbitrary raw values into one category, using modes that actually exist on
macOS as a stand-in for `irq`/`softirq`, which don't), then the actual
generated query end-to-end, then cross-checked against Grafana's native
rendering of the same underlying data — all landing within a couple of
percentage points of each other.

### Real failures hit during development, and how they were fixed

This is the part most worth reading carefully if you're going to touch
`tier2_engine.py`. All three of these were caught by actually running the
model and reading its output — not by reasoning about what the model
"should" do.

**Failure 1 — SQL-style `CASE WHEN`.** The first real run against
`qwen2.5-coder:7b` produced:
```
CASE WHEN system.cpu.state = 'system' THEN 'Busy System' WHEN ... ELSE ... END
```
This is valid *SQL* and valid *Elasticsearch SQL* (a separate, older Elastic
query language), but **invalid ES|QL**. ES|QL's `CASE` is a flat function:
`CASE(condition1, value1, condition2, value2, ..., default)`. The model
reached for the far more common SQL pattern from its training data. Fix: the
system prompt now explicitly states the correct syntax *and* shows a full
worked example — abstract prose rules weren't enough; the model needed to see
the actual shape.

**Failure 2 — chained `STATS`.** The same first run also produced six
separate `STATS` blocks, one per legend series, piped together. This is
structurally broken: each `STATS` consumes the columns from the stage before
it, so by the second `STATS`, the raw fields needed for the third onward no
longer exist. Fix: the prompt now explicitly says "exactly ONE STATS command
total," with the reasoning spelled out (why chaining can't work), not just
the rule.

**Failure 3 — single-quoted strings.** After fixing the above, a second run
produced syntactically-correct `CASE(...)` — but with `'system'` instead of
`"system"`. ES|QL requires double-quoted string literals; single quotes
throw a parsing error. This is almost certainly the model conflating ES|QL
with **Elasticsearch SQL**, which *does* use single quotes — two different
Elastic query languages with opposite quoting rules. Confirmed against
Elastic's own engineering discussion of the distinction before fixing. Fix:
an explicit constraint naming the likely confusion directly, plus a
validator check (see below) so this can never silently slip through again.

All three fixes are now baked into `tier2_engine.py`'s `SYSTEM_PROMPT`, and
all three failure modes have dedicated regression tests in `--selftest`
using the *actual* broken output that was produced, not a hypothetical.

### The validator

`validate_esql()` is a purely mechanical, no-network check run on every Tier 2
response before it's trusted. It checks for exactly the failure modes above,
plus: `TS` vs `FROM`, `RATE()` only on `metrics.*`-prefixed fields, balanced
parentheses, and (once a panel has multiple legends) that a `CASE(...)` call
and `BUCKET(@timestamp, ...)` grouping are both present, and that every
expected legend name actually appears in the query text.

If validation fails, `translate_tier2()` automatically retries (up to 2 more
times), appending the specific validation failures to the prompt so the model
can self-correct — this is why, after the prompt fixes above, the real model
now succeeds on the first attempt almost every time, but the retry mechanism
exists for whenever it doesn't.

### Output standardization

Early versions let the AI name its own output columns (it picked `cpu_rate`/
`cpu_category` for the CPU panel). This doesn't generalize — a memory or disk
panel would get different names, and downstream code (building the Kibana
panel) would have to parse arbitrary AI-chosen names to find them. The prompt
now mandates fixed names — `metric_value` and `category` — regardless of what
the panel measures, enforced by both the prompt and the validator.

### `$job` resolution

Unlike Tier 1 (§3), Tier 2 does *not* silently drop the `service.name`
filter when `$job` is unresolved. `tier2_engine.py` has an explicit
`DEFAULT_JOB_RESOLUTION` fallback (default: `"node-exporter"`, overridable
via the `PROM_JOB_NAME` env var) specifically so the AI-generated query
doesn't inherit Tier 1's correctness gap.

## 5. ES|QL gotchas

A dedicated list, since several of these took real debugging time and aren't
necessarily obvious if you know SQL or even Elasticsearch SQL.

- **`TS` vs `FROM`.** The data stream
  `metrics-prometheusreceiver.otel-default` is a TSDS (time-series data
  stream), applied automatically by the `metrics-otel@template`. Time-series
  queries must use `TS <data_stream>`, not `FROM`. `FROM` also doesn't
  support `COUNT(*)` against a TSDS — use `COUNT(<field>)` instead.

- **Double-quoted strings only.** `'value'` throws a parsing error
  (`token recognition error`). Use `"value"`. This is the opposite of
  Elasticsearch SQL, which uses single quotes — easy to conflate if you've
  used both.

- **`CASE` is a function, not `CASE WHEN ... THEN ... END`.** The correct
  form is `CASE(condition1, value1, condition2, value2, ..., default)` — a
  flat comma-separated list, evaluated left to right, returning the value
  paired with the first true condition.

- **`RATE()` needs `counter_double`, which means the `metrics.`-prefixed
  field.** The OTel exporter creates two versions of each metric: the
  unprefixed one (e.g. `system.cpu.time`, type `double`) for use in `WHERE`
  filters, and the `metrics.`-prefixed one (e.g. `metrics.system.cpu.time`,
  type `counter_double`) for use inside `RATE()`. Calling `RATE()` on the
  unprefixed field doesn't work.

- **Field names with a dot followed by a digit need backticks** — e.g.
  `` `system.cpu.load_average.1m` ``. `system.cpu.state` doesn't need them.

- **A single `STATS` can't be chained to produce multiple named outputs.**
  Each `STATS` consumes the columns from the stage before it. One query, one
  `STATS`, with a category column doing the splitting into multiple legend
  series (§4).

- **The Saved Objects `_find` API is unavailable on Elastic Cloud
  Serverless** (`400: exists but is not available with the current
  configuration`), and more broadly the whole `/api/saved_objects/*` HTTP
  surface is being deprecated across the Elastic Stack. Don't build
  find-then-delete logic against it. Instead, give a saved object a fixed
  ID and import with `overwrite=true` (not `createNewCopies=true`) — this is
  what `build_demo_dashboard.py` does, and it's the same pattern the Grafana
  provisioned dashboard already used via its fixed `uid`.

## 6. Running the stack locally

Covered step by step in the top-level `README.md`. Gotchas specific to
running this on macOS, discovered the hard way:

- **Don't run Node Exporter from a one-off `/tmp` binary.** `/tmp` doesn't
  reliably survive a reboot. Use `brew install node_exporter && brew
  services start node_exporter` instead — it also means you don't have to
  remember to manually relaunch it every session.

- **A stale Docker container can fail with a cryptic mount error**
  (`path / is mounted on / but it is not a shared or slave mount`) if it was
  created under a different Docker Desktop VM state and never actually
  started. If `docker ps -a` shows a container stuck at `Created` (never
  `Up`, never `Exited`) and it fails to start with this error, don't debug
  it — just `docker rm` it and recreate however the current compose file or
  install instructions specify.

- **`GF_SECURITY_ADMIN_PASSWORD` only applies the first time Grafana's
  storage volume is created.** If the volume persists across restarts (it
  does, by design, via the named `grafana-storage` volume) and the password
  ever changes, the env var won't reset it on subsequent `docker compose up`
  calls. Reset directly instead:
  ```bash
  docker exec -it prom-demo-grafana grafana cli admin reset-admin-password admin
  ```
  Note: newer Grafana images renamed `grafana-cli` to `grafana cli` (a
  subcommand of the main binary, no hyphen) — check `docker exec -it
  prom-demo-grafana which grafana` if this specific form doesn't work on
  whatever image version is current.

- **`python3` can silently start resolving to a different interpreter**
  after installing something via Homebrew (e.g. installing `node_exporter`
  can pull in `python@3.x` as a dependency, which then takes priority on
  `PATH`). If a package you installed yesterday suddenly reports
  `No module named X` today, run `which -a python3` and check whether the
  first result changed. On macOS, `/usr/bin/python3` (Apple's
  Command-Line-Tools Python, historically 3.9.x) is often where `pip install
  --user`-style installs actually land — calling it by full path sidesteps
  the `PATH`-ordering issue entirely.

- **`streamlit` as a bare command may not be on `PATH`** even when it's
  correctly installed, if pip installed it to a user-site `bin` directory
  that isn't on `PATH`. Use `python3 -m streamlit run app.py` instead of
  bare `streamlit run app.py` — works regardless of `PATH` configuration.

## 7. The Streamlit UI

`conversion-tool/app.py` is a thin wrapper around the same `convert_panel()`
function the CLI uses — upload or paste a panel, convert, see the ES|QL and
a tier badge, optionally import to Kibana.

The one feature that's more than a wrapper: **Tier 2 auditability.** This
was built deliberately, not as an afterthought, directly in response to §4's
history of real AI failures. The reasoning: Tier 1 is a deterministic
function you can trust by reading it; Tier 2 isn't, and anyone relying on an
AI-generated query in a production migration has a legitimate right to see
*what the AI actually did* before trusting it, not just a final "it worked."

Concretely, whenever Tier 2 fires, an expandable section shows:
- The AI's own explanation and any approximations it flagged
- Every attempt it made (not just the successful one), each with its own
  pass/fail status and viewable ES|QL — so "succeeded on attempt 1" and
  "succeeded after 2 retries, here's what failed the first time" are visibly
  different outcomes, not collapsed into the same "success" badge

This required a corresponding change in `tier2_engine.py`: `translate_tier2()`
used to overwrite its result on every retry attempt, discarding earlier
(failed) attempts once a later one succeeded. It now retains an
`attempt_history` list across the whole retry loop specifically so this UI
feature has data to show. This is covered by a self-test using a mocked
`call_ollama` (see `--selftest` case 6) so the retry/history logic is
verified without depending on a real model actually failing during a test
run.

## 8. Extending the tool

To support a different Prometheus exporter (not Node Exporter):

1. Add entries to `METRIC_MAP` and `LABEL_MAP` in `convert_panel.py` for the
   new exporter's metric and label names.
2. Confirm the OTel Collector's `otelcol-config.yaml` actually normalizes
   those metrics — the conversion tool assumes the normalized field already
   exists in Elasticsearch; it doesn't create the mapping itself.
3. Test Tier 1 first against a few real panels from that exporter. Anything
   that fails will show a `⚠ Tier 2 required for: ...` warning — that's your
   signal for what Tier 2 needs to handle, not a blocker.
4. For Tier 2 to work with the new domain, it mostly just needs the field
   mapping tables (step 1) — the prompt and validator are domain-agnostic.
   Worth re-running `tier2_engine.py --selftest` after any prompt changes,
   and testing the *real* model output against live data before trusting it,
   the same way CPU was validated (§4).

## 9. Known limitations / roadmap

- **Linux revalidation needed.** The `irq`/`softirq`/`iowait` CPU modes that
  motivated Tier 2 in the first place have never been tested against real
  data — macOS's Node Exporter doesn't report them. Only the general
  grouping/exclusion *mechanism* has been proven, using other real mode
  values (`nice`, `system`) as a stand-in. A Linux host (or VM) is needed to
  close this gap.
- **No conversion history.** Every conversion's result — including Tier 2's
  full attempt history — exists only for the duration of that run. Planned:
  a SQLite-backed store (always-on, no external dependency) recording every
  conversion, with Elasticsearch as an optional downstream sync target
  (SQLite as source of truth; ES as an eventually-consistent projection,
  synced whenever reachable).
- **No batch conversion.** One panel at a time. Planned: whole-dashboard
  batch conversion plus a migration scoping report (e.g. "312 panels across
  47 dashboards: 61% Tier 1, 24% Tier 2, 15% need a human — here's the
  list"), which the conversion-history store would also feed directly.
- **No Kubernetes domain mapping** (kube-state-metrics) yet.

## 10. Appendix: command reference

```bash
# Start everything
source .env
docker compose up -d
brew services start node_exporter   # if not already running
ollama serve &                      # if not already running

# Verify
curl -s http://localhost:9100/metrics | head -3
curl -s http://localhost:11434/api/tags

# Convert a single panel
cd conversion-tool
python3 convert_panel.py --panel panels/cpu_basic.json --query-only
python3 convert_panel.py --panel panels/cpu_basic.json --import \
  --kibana-endpoint $KIBANA_ENDPOINT --kibana-api-key $ES_API_KEY

# Run the Streamlit UI
python3 -m streamlit run app.py

# Rebuild the reference demo dashboard (Kibana)
cd ..
python3 build_demo_dashboard.py

# Run Tier 2's self-tests (no network required)
cd conversion-tool
python3 tier2_engine.py --selftest

# Grafana password reset (if admin/admin stops working)
docker exec -it prom-demo-grafana grafana cli admin reset-admin-password admin

# Full pipeline reset (stops containers, clears the ES data stream, restarts)
cd ..
bash prom-otel-pipeline/reset.sh
```
