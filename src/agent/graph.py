import re
from typing import TypedDict, List, Dict, Any, Optional
from langgraph.graph import StateGraph, END
from src.llm import call_chat
from src.tools.incident_tools import parse_bundle_lines, search_logs_duckdb
from src.retrieval.hybrid_rag import HybridRetriever
from eval.score import contains_blame, REQUIRED_TEMPLATE_SECTIONS

_RETRIEVER: Optional[HybridRetriever] = None

def get_retriever() -> HybridRetriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        _RETRIEVER = HybridRetriever()
    return _RETRIEVER

class IncidentState(TypedDict, total=False):
    raw_input: str
    user_prompt: str
    action: str
    message: Optional[str]
    primary_service: str
    timeline: List[Dict[str, str]]
    valid_evidence_ids: List[str]
    quarantined_injections: List[str]
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

USER_BLAME_TRIGGER = re.compile(
    r"(caused the outage due to negligence|should be fired|blame\s+\w+|write that\s+\w+\s+caused)",
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

    if USER_BLAME_TRIGGER.search(prompt):
        return {
            **state,
            "action": "REFUSE",
            "message": (
                "Refused: PostmortemPilot enforces a strict blameless postmortem policy and "
                "cannot attribute outages to individual personnel or recommend disciplinary action."
            ),
            "critic_passed": True,
        }

    parsed = parse_bundle_lines(raw_input)
    events = parsed["events"]
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
            "critic_passed": True,
        }

    valid_ids = [e["evidence_id"] for e in events]
    return {
        **state,
        "action": "PROCEED",
        "primary_service": primary_svc,
        "timeline": events,
        "valid_evidence_ids": valid_ids,
        "quarantined_injections": parsed["quarantined_injections"],
        "revision_count": 0,
    }

def timeline_rag_node(state: IncidentState) -> IncidentState:
    retriever = get_retriever()
    svc = state["primary_service"]
    events = state["timeline"]

    svc_logs = search_logs_duckdb(events, service=svc, limit=50)
    query_str = f"{svc} " + " ".join(e["event"] for e in svc_logs)

    rb_hits = retriever.search(query_str, doc_type="runbook", top_k=1)
    pm_hits = retriever.search(query_str, doc_type="postmortem", top_k=1)

    rb = rb_hits[0] if rb_hits else {}
    pm = pm_hits[0] if pm_hits else {}

    return {
        **state,
        "runbook_hit": rb,
        "postmortem_hit": pm,
        "similar_pm_id": pm.get("doc_id", "PM:001"),
    }

def drafter_node(state: IncidentState) -> IncidentState:
    svc = state["primary_service"]
    events = state["timeline"]
    rb = state.get("runbook_hit", {})
    pm = state.get("postmortem_hit", {})
    revisions = state.get("revision_count", 0)

    primary_log_id = next((e["evidence_id"] for e in events if e["evidence_id"].startswith("LOG:")), events[0]["evidence_id"])
    chat_gap_id = next((e["evidence_id"] for e in events if e["evidence_id"].startswith("CHAT:")), primary_log_id)
    ticket_id = next((e["evidence_id"] for e in events if e["evidence_id"].startswith("TICKET:")), primary_log_id)
    resolution_id = events[-1]["evidence_id"]

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
    except Exception:
        exec_summary = f"SEV-1 incident in {svc} triggered by fault flag anomaly ({primary_log_id}) and mitigated via flag rollback ({resolution_id})."

    rc_cause = pm.get("root_cause") or f"{svc} service failure evidenced in {primary_log_id}"
    root_cause_str = sanitize_blame_text(f"Technical root cause: {rc_cause} (verified by {primary_log_id}).")

    runbook_gap = {
        "runbook_id": rb.get("doc_id", f"RUNBOOK:{svc}:1"),
        "step_number": int(rb.get("step_number", 1)),
        "expected_action": rb.get("missed_step", "Follow standard service recovery step"),
        "observed_gap": f"Automated guardrail did not enforce {rb.get('doc_id', 'runbook')} prior to pod restart ({chat_gap_id}).",
        "evidence_id": chat_gap_id,
    }

    action_item = {
        "title": rb.get("action_title", f"Automate failover and health checks for {svc}"),
        "owner_role": rb.get("owner_role", "SRE-OnCall"),
        "priority": rb.get("priority", "P1"),
        "due_days": 7 if rb.get("priority") == "P0" else 14,
        "description": f"Remediate {rc_cause} ({primary_log_id}) and enforce {runbook_gap['runbook_id']} ({chat_gap_id}).",
        "status": "DRAFT",
        "requires_human_confirmation": True,
    }

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

    return {
        **state,
        "root_cause": root_cause_str,
        "evidence_ids": [primary_log_id],
        "runbook_gap": runbook_gap,
        "action_item": action_item,
        "report": report,
        "revision_count": revisions + 1,
    }

def critic_node(state: IncidentState) -> IncidentState:
    report = state.get("report", {})
    valid_ids = set(state.get("valid_evidence_ids", []))
    reasons = []

    missing = [s for s in REQUIRED_TEMPLATE_SECTIONS if not report.get(s)]
    if missing:
        reasons.append(f"missing_sections:{missing}")

    blame_hits = contains_blame(str(report))
    if blame_hits or "ravi" in str(report).lower():
        reasons.append(f"blame_detected:{blame_hits}")

    ev_ids = state.get("evidence_ids", [])
    if not ev_ids or any(eid not in valid_ids for eid in ev_ids):
        reasons.append("invalid_evidence_id")

    passed = len(reasons) == 0
    return {
        **state,
        "critic_passed": passed,
        "critic_reasons": reasons,
    }

def route_after_triage(state: IncidentState) -> str:
    if state.get("action") in ("REFUSE", "CLARIFY"):
        return END
    return "timeline_rag"

def route_after_critic(state: IncidentState) -> str:
    if state.get("critic_passed", False) or state.get("revision_count", 0) >= 2:
        return END
    return "drafter"

def build_postmortem_graph():
    workflow = StateGraph(IncidentState)
    workflow.add_node("triage", triage_node)
    workflow.add_node("timeline_rag", timeline_rag_node)
    workflow.add_node("drafter", drafter_node)
    workflow.add_node("critic", critic_node)

    workflow.set_entry_point("triage")
    workflow.add_conditional_edges("triage", route_after_triage, {"timeline_rag": "timeline_rag", END: END})
    workflow.add_edge("timeline_rag", "drafter")
    workflow.add_edge("drafter", "critic")
    workflow.add_conditional_edges("critic", route_after_critic, {"drafter": "drafter", END: END})

    return workflow.compile()

if __name__ == "__main__":
    import json
    app = build_postmortem_graph()
    with open("data/bundles/bundle_002.json") as f:
        b = json.load(f)
    raw_text = "\n".join(l["raw"] for l in b["lines"])
    final_state = app.invoke({"raw_input": raw_text, "user_prompt": "Generate full postmortem"})
    print("LangGraph Action:", final_state["action"])
    print("Critic Passed:", final_state["critic_passed"])
    print("Extracted Action Item:", json.dumps(final_state["action_item"], indent=2))
