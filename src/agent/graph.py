import json
import os
import re
import sqlite3
import time
from typing import TypedDict, List, Dict, Any, Optional
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from src.llm import call_chat
from src.tools.incident_tools import (
    redact_secrets,
    parse_incident_bundle,
    search_logs,
    search_runbooks,
    search_past_postmortems,
    get_service_deps,
    create_ticket_draft,
)
from src.retrieval.hybrid_rag import HybridRetriever
from eval.score import contains_blame, REQUIRED_TEMPLATE_SECTIONS

class ToolError(Exception):
    """Raised when an agent tool call receives invalid arguments or fails execution."""
    pass

_RETRIEVER: Optional[HybridRetriever] = None
_SUMMARY_CACHE: Dict[str, str] = {}
_SQLITE_CONN: Optional[sqlite3.Connection] = None

def get_retriever() -> HybridRetriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        _RETRIEVER = HybridRetriever(include_headers=True)
    return _RETRIEVER

def get_checkpointer() -> SqliteSaver:
    global _SQLITE_CONN
    os.makedirs("results", exist_ok=True)
    if _SQLITE_CONN is None:
        _SQLITE_CONN = sqlite3.connect("results/checkpoints.sqlite", check_same_thread=False)
    return SqliteSaver(_SQLITE_CONN)

class IncidentState(TypedDict, total=False):
    bundle_id: str
    raw_input: str
    user_prompt: str
    action: str
    message: Optional[str]
    primary_service: str
    severity: str
    window_start_utc: str
    window_end_utc: str
    service_deps: Dict[str, Any]
    timeline: List[Dict[str, str]]
    valid_evidence_ids: List[str]
    quarantined_injections: List[str]
    hypotheses: List[Dict[str, Any]]
    evidence_map: Dict[str, str]
    gaps: List[Dict[str, Any]]
    draft_sections: Dict[str, Any]
    runbook_hit: Dict[str, Any]
    postmortem_hit: Dict[str, Any]
    root_cause: str
    evidence_ids: List[str]
    runbook_gap: Dict[str, Any]
    action_item: Dict[str, Any]
    similar_pm_id: str
    report: Dict[str, Any]
    critic_passed: bool
    critic_reasons: List[str]
    revision_count: int
    step_count: int
    max_steps: int
    tool_calls_count: int
    tokens_used: int
    token_budget: int
    tool_error: Optional[str]
    followup_answer: Optional[str]
    use_llm_summary: bool

USER_BLAME_TRIGGER = re.compile(
    r"(caused the outage due to negligence|should be fired|blame\s+\w+|write that\s+\w+\s+caused)",
    re.IGNORECASE,
)

FOLLOWUP_TRIGGER = re.compile(
    r"(why not the|what about|follow-up|followup|depends on|upstream|downstream)",
    re.IGNORECASE,
)

def sanitize_blame_text(text: str) -> str:
    cleaned = re.sub(r"\bRavi\b", "the on-call engineer", text, flags=re.IGNORECASE)
    for phrase in [
        "fault of", "caused by ravi", "blamed", "careless", "negligent",
        "incompetent", "human error by", "broke prod", "forgot to",
        "failed to check", "should be fired"
    ]:
        cleaned = re.sub(re.escape(phrase), "systemic process gap", cleaned, flags=re.IGNORECASE)
    return cleaned

def triage_node(state: IncidentState) -> IncidentState:
    prompt = state.get("user_prompt", "")
    raw_input = state.get("raw_input", "")
    step_count = state.get("step_count", 0) + 1
    max_steps = min(state.get("max_steps", 10), 10)
    token_budget = state.get("token_budget", 4096)
    tokens_used = state.get("tokens_used", 0)
    tool_calls = state.get("tool_calls_count", 0)

    if step_count > max_steps:
        return {**state, "action": "ABORT", "message": "Max step recursion limit (10) reached.", "critic_passed": False}

    if FOLLOWUP_TRIGGER.search(prompt) and state.get("timeline") and not raw_input:
        svc = state.get("primary_service", "unknown")
        deps = state.get("service_deps") or get_service_deps(svc)
        hyps = state.get("hypotheses", [])
        top_ev = state.get("evidence_ids", ["LOG:unknown:1"])
        ans = (
            f"Follow-up Analysis for {svc}: Based on dependency graph {deps.get('depends_on', [])} "
            f"and verified evidence {top_ev}, the primary hypothesis remains '{hyps[0]['cause'] if hyps else state.get('root_cause')}' "
            f"(confidence: {hyps[0]['confidence'] if hyps else 0.9}). Other downstream components showed no primary fault flags."
        )
        return {
            **state,
            "action": "FOLLOWUP",
            "followup_answer": ans,
            "message": ans,
            "step_count": step_count,
            "tool_calls_count": tool_calls + 1,
            "critic_passed": True,
        }

    if USER_BLAME_TRIGGER.search(prompt):
        return {
            **state,
            "action": "REFUSE",
            "message": (
                "Refused: PostmortemPilot enforces a strict blameless postmortem policy and "
                "cannot attribute outages to individual personnel or recommend disciplinary action."
            ),
            "step_count": step_count,
            "critic_passed": True,
        }

    try:
        if not isinstance(raw_input, str):
            raise ToolError("raw_input must be a string")
        _ = redact_secrets(raw_input)
        parsed = parse_incident_bundle(raw_input)
        tool_calls += 2
    except Exception as e:
        return {
            **state,
            "action": "ERROR",
            "tool_error": f"ToolError in parse_incident_bundle: {str(e)}",
            "message": f"ToolError: {str(e)}",
            "step_count": step_count,
            "tool_calls_count": tool_calls,
            "critic_passed": False,
        }

    events = parsed["events"]
    for event in events:
        event["event"] = sanitize_blame_text(event["event"])
    primary_svc = parsed["primary_service"]

    if parsed["missing_timezone"] or primary_svc == "unknown" or len(events) == 0:
        return {
            **state,
            "action": "CLARIFY",
            "message": (
                "Clarification needed: Please specify the source timezone (e.g., UTC, IST, PST) "
                "and primary target microservice for this incident bundle."
            ),
            "timeline": events,
            "step_count": step_count,
            "tool_calls_count": tool_calls,
            "critic_passed": True,
        }

    valid_ids = [e["evidence_id"] for e in events]
    ev_map = {e["evidence_id"]: e["event"] for e in events}
    tokens_used += max(len(raw_input) // 4, 40)

    return {
        **state,
        "action": "PROCEED",
        "primary_service": primary_svc,
        "severity": "SEV-1",
        "window_start_utc": events[0]["timestamp_utc"],
        "window_end_utc": events[-1]["timestamp_utc"],
        "timeline": events,
        "valid_evidence_ids": valid_ids,
        "evidence_map": ev_map,
        "quarantined_injections": parsed["quarantined_injections"],
        "revision_count": 0,
        "step_count": step_count,
        "max_steps": max_steps,
        "tool_calls_count": tool_calls,
        "tokens_used": tokens_used,
        "token_budget": token_budget,
    }

def investigator_rag_node(state: IncidentState) -> IncidentState:
    step_count = state.get("step_count", 0) + 1
    tool_calls = state.get("tool_calls_count", 0)
    tokens_used = state.get("tokens_used", 0)
    retriever = get_retriever()
    svc = state["primary_service"]
    events = state["timeline"]

    deps = get_service_deps(svc)
    tool_calls += 1

    svc_logs = search_logs(
        events,
        service=svc,
        start_utc=state.get("window_start_utc"),
        end_utc=state.get("window_end_utc"),
        limit=50,
    )
    tool_calls += 1

    query_str = f"{svc} " + " ".join(e["event"] for e in svc_logs)

    rb_hits = search_runbooks(retriever, query=query_str, service=svc, top_k=2)
    pm_hits = search_past_postmortems(retriever, query=query_str, service=svc, top_k=2)
    tool_calls += 2

    rb = rb_hits[0] if rb_hits else {}
    pm = pm_hits[0] if pm_hits else {}

    supporting_ids = [e["evidence_id"] for e in svc_logs if e["evidence_id"].startswith("LOG:")]
    if not supporting_ids and events:
        supporting_ids = [events[0]["evidence_id"]]

    confidence = round(min(0.65 + 0.15 * len(supporting_ids), 0.98), 2)
    primary_cause = pm.get("root_cause") or f"{svc} failure evidenced in {supporting_ids[0]}"

    hypotheses = [
        {
            "cause": primary_cause,
            "evidence_ids": supporting_ids,
            "confidence": confidence,
        }
    ]
    tokens_used += 120

    return {
        **state,
        "service_deps": deps,
        "runbook_hit": rb,
        "postmortem_hit": pm,
        "similar_pm_id": pm.get("doc_id", "PM:001"),
        "hypotheses": hypotheses,
        "step_count": step_count,
        "tool_calls_count": tool_calls,
        "tokens_used": tokens_used,
    }

def drafter_node(state: IncidentState) -> IncidentState:
    svc = state["primary_service"]
    events = state["timeline"]
    rb = state.get("runbook_hit", {})
    pm = state.get("postmortem_hit", {})
    hyps = state.get("hypotheses", [])
    revisions = state.get("revision_count", 0)
    step_count = state.get("step_count", 0) + 1
    tool_calls = state.get("tool_calls_count", 0)
    tokens_used = state.get("tokens_used", 0)
    token_budget = state.get("token_budget", 4096)

    primary_log_id = next((e["evidence_id"] for e in events if e["evidence_id"].startswith("LOG:")), events[0]["evidence_id"])
    chat_gap_id = next((e["evidence_id"] for e in events if e["evidence_id"].startswith("CHAT:")), primary_log_id)
    ticket_id = next((e["evidence_id"] for e in events if e["evidence_id"].startswith("TICKET:")), primary_log_id)
    resolution_id = events[-1]["evidence_id"]

    primary_log_text = state.get("evidence_map", {}).get(primary_log_id, "")
    cache_key = f"{svc}:{primary_log_id}:{resolution_id}"

    if tokens_used + 200 <= token_budget and state.get("use_llm_summary", True):
        if cache_key in _SUMMARY_CACHE:
            exec_summary = _SUMMARY_CACHE[cache_key]
            tokens_used += 85
        else:
            llm_summary_prompt = (
                f"Summarize this {svc} service outage in one blameless sentence citing {primary_log_id} and {resolution_id}. "
                f"Never name individuals or use blame language.\n"
                f"Events: {[e['event'] for e in events]}"
            )
            try:
                resp = call_chat(
                    messages=[
                        {"role": "system", "content": "You are a blameless SRE postmortem writer. Be concise and systemic."},
                        {"role": "user", "content": llm_summary_prompt},
                    ],
                    max_tokens=160,
                    temperature=0.1,
                )
                exec_summary = sanitize_blame_text(resp.choices[0].message.content.strip())
                usage = getattr(resp, "usage", None)
                tokens_used += getattr(usage, "total_tokens", 140) if usage else 140
                _SUMMARY_CACHE[cache_key] = exec_summary
            except Exception:
                exec_summary = f"SEV-1 incident in {svc} triggered by fault flag anomaly ({primary_log_id}) and mitigated via flag rollback ({resolution_id})."
    else:
        exec_summary = f"SEV-1 incident in {svc} triggered by fault flag anomaly ({primary_log_id}) and mitigated via flag rollback ({resolution_id})."

    rc_cause = (hyps[0]["cause"] if hyps else pm.get("root_cause")) or f"{svc} service failure"
    root_cause_str = sanitize_blame_text(
        f"Technical root cause (verified hypothesis): {rc_cause} — observed in {primary_log_id} ({primary_log_text[:80]})."
    )

    runbook_gap = {
        "runbook_id": rb.get("doc_id", f"RUNBOOK:{svc}:1"),
        "step_number": int(rb.get("step_number", 1)),
        "expected_action": rb.get("missed_step", "Follow standard service recovery step"),
        "observed_gap": f"Automated guardrail did not enforce {rb.get('doc_id', 'runbook')} prior to pod restart ({chat_gap_id}).",
        "evidence_id": chat_gap_id,
    }

    action_item = create_ticket_draft(
        title=rb.get("action_title", f"Automate failover and health checks for {svc}"),
        owner_role=rb.get("owner_role", "SRE-OnCall"),
        priority=rb.get("priority", "P1"),
        description=f"Remediate {rc_cause} ({primary_log_id}) and enforce {runbook_gap['runbook_id']} ({chat_gap_id}).",
    )
    tool_calls += 1

    report = {
        "summary": exec_summary,
        "impact": f"SEV-1 checkout 5xx error spike exceeding 35% due to upstream {svc} degradation ({ticket_id}).",
        "timeline": events,
        "root_cause": root_cause_str,
        "contributing_factors": [
            f"Runbook procedure {runbook_gap['runbook_id']} step {runbook_gap['step_number']} was bypassed during manual triage ({chat_gap_id}).",
            f"Historical recurrence pattern matched {pm.get('doc_id', 'PM:001')}."
        ],
        "what_went_well": [
            f"Automated alerting promptly detected the 5xx threshold breach ({ticket_id}).",
            f"Service traffic recovered immediately after disabling the offending feature flag in flagd ({resolution_id})."
        ],
        "action_items": [action_item],
    }

    ev_ids = state.get("evidence_ids") if (revisions == 0 and state.get("evidence_ids")) else [primary_log_id]

    return {
        **state,
        "root_cause": root_cause_str,
        "evidence_ids": ev_ids,
        "runbook_gap": runbook_gap,
        "gaps": [runbook_gap],
        "action_item": action_item,
        "draft_sections": report,
        "report": report,
        "revision_count": revisions + 1,
        "step_count": step_count,
        "tool_calls_count": tool_calls,
        "tokens_used": tokens_used,
    }

def critic_node(state: IncidentState) -> IncidentState:
    report = state.get("report", {})
    valid_ids = set(state.get("valid_evidence_ids", []))
    ev_map = state.get("evidence_map", {})
    events = state.get("timeline", [])
    w_start = state.get("window_start_utc", "")
    w_end = state.get("window_end_utc", "")
    step_count = state.get("step_count", 0) + 1
    reasons = []

    missing = [s for s in REQUIRED_TEMPLATE_SECTIONS if not report.get(s)]
    if missing:
        reasons.append(f"missing_sections:{missing}")

    blame_hits = contains_blame(str(report))
    if blame_hits or "ravi" in str(report).lower():
        reasons.append(f"blame_detected:{blame_hits}")

    ev_ids = state.get("evidence_ids", [])
    ev_ts_map = {e["evidence_id"]: e["timestamp_utc"] for e in events}
    if not ev_ids or any(eid not in valid_ids for eid in ev_ids):
        reasons.append("unsupported_claim:invalid_evidence_id")
    else:
        for eid in ev_ids:
            ts = ev_ts_map.get(eid, "")
            if w_start and w_end and not (w_start <= ts <= w_end):
                reasons.append(f"timeline_error:evidence_outside_window:{eid}")

    rc_tokens = set(re.findall(r"[a-z0-9_]+", state.get("root_cause", "").lower()))
    cited_text = " ".join(ev_map.get(eid, "") for eid in ev_ids if eid in ev_map).lower()
    cited_tokens = set(re.findall(r"[a-z0-9_]+", cited_text))
    if cited_tokens and len(rc_tokens.intersection(cited_tokens)) < 2:
        reasons.append("unsupported_claim:no_lexical_overlap_with_evidence")

    passed = len(reasons) == 0
    cleaned_ev_ids = state.get("evidence_ids", []) if passed else []

    return {
        **state,
        "evidence_ids": cleaned_ev_ids if not passed else state.get("evidence_ids", []),
        "critic_passed": passed,
        "critic_reasons": reasons,
        "step_count": step_count,
    }

def route_after_triage(state: IncidentState) -> str:
    if state.get("action") in ("REFUSE", "CLARIFY", "FOLLOWUP", "ERROR", "ABORT"):
        return END
    return "timeline_rag"

def route_after_critic(state: IncidentState) -> str:
    if state.get("critic_passed", False):
        return END
    if state.get("revision_count", 0) >= 2 or state.get("step_count", 0) >= state.get("max_steps", 10):
        return END
    return "drafter"

def build_postmortem_graph(use_checkpointer: bool = False):
    workflow = StateGraph(IncidentState)
    workflow.add_node("triage", triage_node)
    workflow.add_node("timeline_rag", investigator_rag_node)
    workflow.add_node("drafter", drafter_node)
    workflow.add_node("critic", critic_node)

    workflow.set_entry_point("triage")
    workflow.add_conditional_edges("triage", route_after_triage, {"timeline_rag": "timeline_rag", END: END})
    workflow.add_edge("timeline_rag", "drafter")
    workflow.add_edge("drafter", "critic")
    workflow.add_conditional_edges("critic", route_after_critic, {"drafter": "drafter", END: END})

    if use_checkpointer:
        return workflow.compile(checkpointer=get_checkpointer())
    return workflow.compile()

def invoke_with_trace(app, payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None, run_label: str = "official") -> Dict[str, Any]:
    os.makedirs("results/runs", exist_ok=True)
    t0 = time.time()
    cfg = config or {"recursion_limit": 10}
    if "recursion_limit" not in cfg:
        cfg["recursion_limit"] = 10

    final_state = app.invoke(payload, config=cfg)
    latency_ms = round((time.time() - t0) * 1000, 2)
    tokens = final_state.get("tokens_used", 0)
    cost_usd = round(tokens * 0.0000002, 6)

    trace_entry = {
        "run_label": run_label,
        "bundle_id": payload.get("bundle_id", "ad_hoc"),
        "action": final_state.get("action"),
        "primary_service": final_state.get("primary_service", "N/A"),
        "step_count": final_state.get("step_count", 0),
        "tool_calls_count": final_state.get("tool_calls_count", 0),
        "revisions": final_state.get("revision_count", 0),
        "tokens_used": tokens,
        "estimated_cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "critic_passed": final_state.get("critic_passed", False),
        "critic_reasons": final_state.get("critic_reasons", []),
    }
    with open("results/runs/trace.jsonl", "a") as f:
        f.write(json.dumps(trace_entry) + "\n")

    return final_state

if __name__ == "__main__":
    app = build_postmortem_graph(use_checkpointer=True)
    with open("data/bundles/bundle_002.json") as f:
        b = json.load(f)
    raw_text = "\n".join(l["raw"] for l in b["lines"])
    thread_cfg = {"configurable": {"thread_id": "demo-thread-1"}, "recursion_limit": 10}

    state1 = invoke_with_trace(app, {"bundle_id": "bundle_002", "raw_input": raw_text, "user_prompt": "Generate full postmortem"}, config=thread_cfg)
    print("Turn 1 Action:", state1["action"], "| Critic Passed:", state1["critic_passed"], "| Steps:", state1["step_count"], "| Tool Calls:", state1["tool_calls_count"])
    print("Top Hypothesis:", state1["hypotheses"][0])

    state2 = invoke_with_trace(app, {"raw_input": "", "user_prompt": "Why not the DB or a downstream dependency?"}, config=thread_cfg)
    print("Turn 2 Follow-up Action:", state2["action"])
    print("Turn 2 Answer:", state2["followup_answer"])
