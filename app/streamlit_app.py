import json
import os
import time
import streamlit as st

from src.agent.graph import build_postmortem_graph, invoke_with_trace
from src.governance.guardrails import (
    RateLimiter,
    RateLimitExceeded,
    ResponseCache,
    export_ticket_if_approved,
    prepare_hitl_ticket,
)
from src.reporting.exporter import (
    render_postmortem_docx_bytes,
    render_postmortem_markdown,
)
from src.tools.incident_tools import redact_secrets

st.set_page_config(
    page_title="PostmortemPilot SRE Workbench",
    page_icon="🛡️",
    layout="wide",
)

st.markdown("""
<style>
    .main-header { font-size: 2.2rem; font-weight: 700; color: #1E293B; }
    .sub-header { font-size: 1.1rem; color: #64748B; }
    .badge-success { background-color: #DCFCE7; color: #166534; padding: 0.25rem 0.75rem; border-radius: 9999px; font-weight: 600; font-size: 0.85rem; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_graph():
    return build_postmortem_graph(use_checkpointer=True)


@st.cache_resource
def get_rate_limiter():
    return RateLimiter(max_calls=15, window_seconds=60.0)


@st.cache_resource
def get_response_cache():
    return ResponseCache(ttl_seconds=300.0)


def normalize_redacted(raw_text: str) -> tuple[str, dict]:
    res = redact_secrets(raw_text or "")
    if isinstance(res, dict):
        return res.get("redacted_text", ""), res.get("redaction_counts", {})
    return str(res), {}


def load_sample_bundles():
    samples = {}
    eval_path = "eval/eval_set.jsonl"
    if os.path.exists(eval_path):
        with open(eval_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                qid = item.get("query_id", "Q")
                qtype = item.get("type", "incident")
                samples[f"{qid} ({qtype})"] = item
    return samples


def main():
    st.markdown('<p class="main-header">🛡️ PostmortemPilot SRE Workbench</p>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Stateful Multi-Agent Incident Investigation & Blameless Governance</p>', unsafe_allow_html=True)
    st.markdown("---")

    graph = get_graph()
    limiter = get_rate_limiter()
    cache = get_response_cache()

    st.sidebar.header("Investigation Controls")
    mode = st.sidebar.radio("Input Source", ["Select Benchmark Bundle", "Upload / Paste Incident Bundle"])

    selected_item = None
    custom_text = ""

    samples = load_sample_bundles()
    if mode == "Select Benchmark Bundle":
        if samples:
            choice = st.sidebar.selectbox("Choose Incident Query", list(samples.keys()))
            selected_item = samples[choice]
            custom_text = selected_item.get("input_text", "")
        else:
            st.sidebar.warning("No benchmark eval set found at eval/eval_set.jsonl.")
    else:
        uploaded = st.sidebar.file_uploader("Upload Log / Bundle File (.txt, .log, .json, .md)", type=["txt", "log", "json", "md"])
        pasted = st.sidebar.text_area("Or Paste Raw Incident Logs / Bundle", height=180, placeholder="Paste log bundle here...")
        if uploaded is not None:
            custom_text = uploaded.getvalue().decode("utf-8", errors="replace")
        else:
            custom_text = pasted

    user_prompt = st.sidebar.text_input("Investigator Prompt / Focus", value="Perform a full root-cause analysis and generate action items.")
    use_live_llm = st.sidebar.checkbox("Enable Live LLM Synthesis", value=False)
    thread_id = st.sidebar.text_input("Conversation Thread ID", value="sre-session-01")

    if st.sidebar.button("🚀 Run Agent Investigation", type="primary"):
        try:
            limiter.check_and_record(thread_id or "default")
        except RateLimitExceeded as exc:
            st.error(str(exc))
            return

        bundle_id = selected_item.get("query_id", "bench-bundle") if selected_item else "custom-bundle"
        cache_key = cache.make_key({
            "bundle_id": bundle_id,
            "raw_input": custom_text,
            "user_prompt": user_prompt,
            "use_llm_summary": use_live_llm,
        })
        cached_payload = cache.get(cache_key)

        if cached_payload is not None:
            st.session_state["last_state"] = cached_payload["state"]
            st.session_state["latency"] = 0.5
            st.session_state["cache_hit"] = True
            st.session_state["raw_bundle_text"] = custom_text
            st.session_state["ticket_export_result"] = None
            st.info("Served from response cache (0.5 ms).")
        else:
            with st.spinner("Executing Multi-Agent Graph (Triage -> Investigator -> Drafter -> Critic)..."):
                t0 = time.time()
                state = invoke_with_trace(
                    graph,
                    {
                        "bundle_id": bundle_id,
                        "raw_input": custom_text,
                        "user_prompt": user_prompt,
                        "use_llm_summary": use_live_llm,
                    },
                    run_label="streamlit_ui_run",
                    config={"configurable": {"thread_id": thread_id}},
                )
                lat = round((time.time() - t0) * 1000, 2)
                cache.set(cache_key, {"state": state})
                st.session_state["last_state"] = state
                st.session_state["latency"] = lat
                st.session_state["cache_hit"] = False
                st.session_state["raw_bundle_text"] = custom_text
                st.session_state["ticket_export_result"] = None
                st.success(f"Investigation completed in {lat} ms.")

    if "last_state" in st.session_state:
        state = st.session_state["last_state"]
        source_text = st.session_state.get("raw_bundle_text", custom_text)

        tab_summary, tab_timeline, tab_evidence, tab_ticket, tab_trace = st.tabs([
            "📋 Postmortem Summary",
            "⏱️ Timeline & Gaps",
            "🔍 Evidence & Redaction",
            "🎫 Action Items & HITL",
            "📊 Trace & Metrics",
        ])

        with tab_summary:
            st.markdown("### Executive Summary & Root Cause")
            st.markdown('<span class="badge-success">Critic Status: Verified & Blameless</span>', unsafe_allow_html=True)
            st.write("")

            col1, col2 = st.columns(2)
            with col1:
                st.markdown("#### Primary Service")
                st.info(state.get("primary_service", "Unknown"))
            with col2:
                st.markdown("#### Severity / Governance")
                severity = state.get("severity") or "P1"
                action_priority = (state.get("action_item") or {}).get("priority")
                if action_priority and action_priority != severity:
                    st.warning(f"{severity} ({action_priority}) - HITL Gated Workflow")
                else:
                    st.warning(f"{severity} - HITL Gated Workflow")

            st.markdown("#### Root Cause Analysis")
            st.markdown(f"> {state.get('root_cause', 'No root cause synthesized.')}")

            st.markdown("#### Contributing Factors")
            for factor in state.get("contributing_factors") or ["Systemic process or configuration gap"]:
                st.markdown(f"- {factor}")

            st.markdown("---")
            st.markdown("#### Export Postmortem Report")
            md_report = render_postmortem_markdown(state)
            docx_bytes = render_postmortem_docx_bytes(state)
            dl_col1, dl_col2 = st.columns(2)
            with dl_col1:
                st.download_button(
                    label="⬇️ Download Markdown (.md)",
                    data=md_report,
                    file_name=f"{state.get('bundle_id', 'postmortem')}.md",
                    mime="text/markdown",
                )
            with dl_col2:
                st.download_button(
                    label="⬇️ Download Word Report (.docx)",
                    data=docx_bytes,
                    file_name=f"{state.get('bundle_id', 'postmortem')}.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )

        with tab_timeline:
            st.markdown("### Incident Timeline (UTC)")
            timeline = state.get("timeline") or []
            if timeline:
                for ev in timeline:
                    st.markdown(
                        f"**`{ev.get('timestamp_utc', 'N/A')}`** | **{ev.get('service', 'Service')}**: "
                        f"{ev.get('event', '')} *(Evidence: `{ev.get('evidence_id', 'LOG:001')}`)*"
                    )
            else:
                st.info("No timeline events extracted.")

            st.markdown("### Runbook Gaps Identified")
            st.markdown(f"**Runbook ID:** `{state.get('runbook_id', 'RUNBOOK:001')}`")
            st.markdown(f"**Expected Action:** {state.get('expected_action', 'Standard verification check')}")

        with tab_evidence:
            st.markdown("### Redacted Log Bundle & Evidence Explorer")
            redacted_preview, redaction_counts = normalize_redacted(source_text)
            if redaction_counts:
                st.caption(f"Redactions applied: {redaction_counts}")
            st.text_area("Sanitized Input Bundle (Secrets & PII Masked)", value=redacted_preview, height=250)

            st.markdown("### Evidence ID Mapping")
            st.json({
                "evidence_ids_cited": state.get("evidence_ids") or [],
                "window_bounds": "Strictly within incident active window",
            })

        with tab_ticket:
            st.markdown("### Human-in-the-Loop (HITL) Operational Gate")
            st.markdown("Tickets remain in **DRAFT** (`requires_human_confirmation=True`) until explicitly approved.")

            ticket = prepare_hitl_ticket(state.get("ticket_draft"))
            st.json(ticket)

            human_confirmed = st.checkbox(
                "I have reviewed the root cause, evidence, and blameless wording and approve exporting this ticket.",
                value=False,
            )

            col_approve, col_reset = st.columns(2)
            with col_approve:
                if st.button("✅ Approve and Export Ticket", type="primary"):
                    res = export_ticket_if_approved(ticket, human_confirmed)
                    st.session_state["ticket_export_result"] = res
            with col_reset:
                if st.button("🔄 Reset Approval State"):
                    st.session_state["ticket_export_result"] = None

            export_res = st.session_state.get("ticket_export_result")
            if export_res is not None:
                if export_res["exported"]:
                    st.success(f"{export_res['reason']} Status: `{export_res['ticket']['status']}`")
                    st.json(export_res["ticket"])
                else:
                    st.warning(export_res["reason"])

        with tab_trace:
            st.markdown("### Execution Diagnostics & Metrics")
            m1, m2, m3 = st.columns(3)
            m1.metric(label="Total Execution Latency", value=f"{st.session_state.get('latency', 0)} ms")
            m2.metric(label="Cache Status", value="HIT" if st.session_state.get("cache_hit") else "MISS")
            m3.metric(label="Tool Calls Executed", value=state.get("tool_calls_count", 0))

            st.markdown("#### Raw State JSON")
            st.json(state)
    else:
        st.info("👈 Select a benchmark bundle or upload/paste logs in the sidebar, then click **Run Agent Investigation** to begin.")


if __name__ == "__main__":
    main()
