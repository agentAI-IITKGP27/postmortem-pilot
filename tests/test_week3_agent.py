import json
import pytest
from src.agent.graph import build_postmortem_graph, invoke_with_trace
from src.tools.incident_tools import redact_secrets, parse_incident_bundle, get_service_deps

def load_sample_raw() -> str:
    with open("data/bundles/bundle_001.json") as f:
        b = json.load(f)
    return "\n".join(l["raw"] for l in b["lines"])

def test_tool_failure_handling():
    app = build_postmortem_graph()
    state = app.invoke({"raw_input": None, "user_prompt": "Analyze outage"}, config={"recursion_limit": 10})
    assert state["action"] == "ERROR"
    assert "ToolError" in state.get("tool_error", "")
    assert state["critic_passed"] is False

def test_recursion_and_loop_guard():
    app = build_postmortem_graph()
    raw_text = load_sample_raw()
    state = app.invoke({"raw_input": raw_text, "user_prompt": "Analyze", "step_count": 10, "max_steps": 10})
    assert state["action"] == "ABORT"
    assert state["critic_passed"] is False

def test_token_budget_cap_fallback():
    app = build_postmortem_graph()
    raw_text = load_sample_raw()
    state = app.invoke({"raw_input": raw_text, "user_prompt": "Analyze", "token_budget": 50})
    assert state["action"] == "PROCEED"
    assert state["critic_passed"] is True
    assert "summary" in state["report"]

def test_critic_rejects_and_heals_invalid_evidence_id():
    app = build_postmortem_graph()
    raw_text = load_sample_raw()
    state = app.invoke({
        "raw_input": raw_text,
        "user_prompt": "Analyze",
        "evidence_ids": ["LOG:hallucinated:99999"],
    })
    assert state["critic_passed"] is True
    assert state["revision_count"] == 2
    assert "LOG:hallucinated:99999" not in state["evidence_ids"]

def test_sqlite_multi_turn_followup():
    app = build_postmortem_graph(use_checkpointer=True)
    raw_text = load_sample_raw()
    import uuid
    cfg = {"configurable": {"thread_id": f"pytest-thread-{uuid.uuid4()}"}, "recursion_limit": 10}

    s1 = invoke_with_trace(app, {"bundle_id": "bundle_001", "raw_input": raw_text, "user_prompt": "Generate report"}, config=cfg)
    assert s1["action"] == "PROCEED"
    assert len(s1["hypotheses"]) >= 1

    s2 = invoke_with_trace(app, {"raw_input": "", "user_prompt": "Why not the DB upstream?"}, config=cfg)
    assert s2["action"] == "FOLLOWUP"
    assert s1["primary_service"] in s2["followup_answer"]

def test_redaction_and_dependency_tools():
    res = redact_secrets("Token Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 from 192.168.1.5")
    assert "[REDACTED_TOKEN]" in res["redacted_text"]
    assert "[REDACTED_IPV4]" in res["redacted_text"]
    deps = get_service_deps("checkout")
    assert "payment" in deps["depends_on"]
