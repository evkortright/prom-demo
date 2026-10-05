#!/usr/bin/env python3
"""
app.py — Streamlit UI for the Grafana → Kibana conversion tool.

Stage 1: a wrapper around convert_panel.py (upload/paste a Grafana panel,
convert it, see the ES|QL, import to Kibana) — PLUS Tier 2 auditability
built in from the start: whenever the AI engine is used, show its full
attempt history (every attempt, not just the one that succeeded), its
confidence, and its own explanation of what it did and why.

Run with:
    streamlit run app.py
"""

import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from convert_panel import convert_panel, build_kibana_dashboard, import_to_kibana


st.set_page_config(
    page_title="Grafana → Kibana Converter",
    page_icon="🔄",
    layout="wide",
)

st.title("🔄 Grafana → Kibana Panel Converter")
st.caption(
    "Converts Prometheus/Grafana panels to Elastic Observability (ES|QL + "
    "Kibana Lens). Tier 1 handles common patterns deterministically; "
    "Tier 2 uses a local AI model for anything Tier 1 can't parse."
)

# ---------------------------------------------------------------------------
# Input: upload or paste a panel
# ---------------------------------------------------------------------------

col_input, col_options = st.columns([3, 1])

with col_input:
    uploaded_file = st.file_uploader(
        "Upload a Grafana panel JSON file", type=["json"]
    )
    pasted_json = st.text_area(
        "...or paste panel JSON directly",
        height=200,
        placeholder='{"type": "timeseries", "title": "...", "targets": [...]}',
    )

with col_options:
    st.markdown("**Options**")
    allow_tier2 = st.checkbox(
        "Enable Tier 2 (AI)",
        value=True,
        help="If off, targets Tier 1 can't parse are dropped with a "
             "warning instead of being sent to the AI engine.",
    )
    st.markdown("---")
    st.markdown("**Kibana import (optional)**")
    kibana_endpoint = st.text_input(
        "Kibana endpoint", value=os.environ.get("KIBANA_ENDPOINT", "")
    )
    kibana_api_key = st.text_input(
        "API key", value=os.environ.get("ES_API_KEY", ""), type="password"
    )

panel_json_str = None
if uploaded_file is not None:
    panel_json_str = uploaded_file.read().decode("utf-8")
elif pasted_json.strip():
    panel_json_str = pasted_json

convert_clicked = st.button("Convert", type="primary", disabled=panel_json_str is None)

# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

if convert_clicked and panel_json_str:
    try:
        grafana_panel = json.loads(panel_json_str)
    except json.JSONDecodeError as e:
        st.error(f"Invalid JSON: {e}")
        st.stop()

    with st.spinner("Converting..."):
        try:
            kibana_panel, esql_query, tier2_info = convert_panel(
                grafana_panel, allow_tier2=allow_tier2
            )
        except (ValueError, RuntimeError) as e:
            st.error(f"Conversion failed: {e}")
            st.stop()

    # Persist across reruns (e.g. when the import button is clicked below)
    st.session_state["kibana_panel"] = kibana_panel
    st.session_state["esql_query"] = esql_query
    st.session_state["tier2_info"] = tier2_info
    st.session_state["panel_title"] = grafana_panel.get("title", "Untitled")

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

if "esql_query" in st.session_state:
    esql_query = st.session_state["esql_query"]
    tier2_info = st.session_state["tier2_info"]
    panel_title = st.session_state["panel_title"]

    st.markdown("---")
    st.subheader(f"Result: {panel_title}")

    # Tier badge
    if tier2_info is None:
        st.success("✅ **Tier 1** — converted deterministically, no AI involved.")
    else:
        confidence = tier2_info.get("confidence", "unknown")
        n_attempts = len(tier2_info.get("attempt_history", []))
        badge = "🤖 **Tier 2 (AI-assisted)**"
        if n_attempts > 1:
            st.warning(
                f"{badge} — succeeded after {n_attempts} attempts "
                f"(confidence: {confidence}). See attempt history below."
            )
        else:
            st.info(f"{badge} — succeeded on the first attempt (confidence: {confidence}).")

    st.code(esql_query, language="sql")

    # --- Tier 2 auditability: what the AI actually did ---
    if tier2_info is not None:
        with st.expander("🔍 What the AI did (Tier 2 audit trail)", expanded=(n_attempts > 1)):
            st.markdown(f"**Explanation:** {tier2_info.get('explanation', '(none provided)')}")

            approximations = tier2_info.get("approximations", [])
            if approximations:
                st.markdown("**Approximations made:**")
                for a in approximations:
                    st.markdown(f"- {a}")

            st.markdown(f"**Attempts:** {n_attempts}")
            for attempt in tier2_info.get("attempt_history", []):
                status_icon = "✅" if attempt["passed"] else "❌"
                with st.container():
                    st.markdown(
                        f"{status_icon} **Attempt {attempt['attempt']}** "
                        f"— confidence: {attempt.get('confidence', 'unknown')}"
                    )
                    if not attempt["passed"]:
                        st.markdown("Validation failures:")
                        for issue in attempt["validation_issues"]:
                            st.markdown(f"  - ⚠️ {issue}")
                    with st.expander(f"View ES|QL from attempt {attempt['attempt']}"):
                        st.code(attempt["esql"], language="sql")
                st.markdown("")

    # --- Import to Kibana ---
    st.markdown("---")
    if st.button("Import to Kibana"):
        if not kibana_endpoint or not kibana_api_key:
            st.error("Kibana endpoint and API key are required to import.")
        else:
            kibana_panel = st.session_state["kibana_panel"]
            dashboard = build_kibana_dashboard(
                panels=[kibana_panel], title=f"Converted: {panel_title}"
            )
            with st.spinner("Importing..."):
                try:
                    result = import_to_kibana(dashboard, kibana_endpoint, kibana_api_key)
                    if result.get("success"):
                        for obj in result.get("successResults", []):
                            did = obj.get("destinationId", obj.get("id"))
                            st.success(f"✅ Imported: {did}")
                            st.markdown(
                                f"[Open in Kibana]({kibana_endpoint}/app/dashboards#/view/{did})"
                            )
                    else:
                        for err in result.get("errors", []):
                            st.error(err.get("error", {}).get("message", "Unknown error"))
                except Exception as e:
                    st.error(f"Import failed: {e}")
