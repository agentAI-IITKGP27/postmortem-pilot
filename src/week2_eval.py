import csv
import json
import os
from collections import defaultdict
from src.agent.graph import build_postmortem_graph
from eval.score import score_prediction

def extract_task_prediction(q_type: str, state: dict) -> dict:
    if state.get("action") in ("REFUSE", "CLARIFY"):
        return {"action": state["action"], "message": state.get("message", "")}

    if q_type == "timeline":
        return {"timeline": state.get("timeline", [])}
    elif q_type in ("root_cause", "injection_doc"):
        return {
            "root_cause": state.get("root_cause", ""),
            "evidence_ids": state.get("evidence_ids", []),
        }
    elif q_type == "runbook_gap":
        return state.get("runbook_gap", {})
    elif q_type == "action_item":
        return state.get("action_item", {})
    elif q_type == "similar_incident":
        return {"similar_pm_id": state.get("similar_pm_id", "")}
    elif q_type == "template":
        return state.get("report", {})
    elif q_type == "blameless":
        rep = state.get("report", {})
        return {
            "root_cause": rep.get("root_cause", ""),
            "contributing_factors": rep.get("contributing_factors", []),
        }
    return state.get("report", {})

def run_week2_evaluation():
    with open("eval/eval_set.jsonl") as f:
        eval_items = [json.loads(line) for line in f]

    app = build_postmortem_graph()
    os.makedirs("results", exist_ok=True)

    rows = []
    cat_total = defaultdict(int)
    cat_passed = defaultdict(int)
    pass_count = 0

    print(f"Running Week 2 Compensated LangGraph Agent on {len(eval_items)} queries...")

    for idx, item in enumerate(eval_items):
        q_id = item["query_id"]
        q_type = item["type"]
        cat_total[q_type] += 1

        state = app.invoke({
            "raw_input": item["input_text"],
            "user_prompt": item["prompt"],
        })
        pred = extract_task_prediction(q_type, state)
        score = score_prediction(item, pred)
        passed = score["passed"]

        if passed:
            pass_count += 1
            cat_passed[q_type] += 1

        rows.append({
            "query_id": q_id,
            "type": q_type,
            "compensated_passed": passed,
            "reason": score["reason"],
            "critic_passed": state.get("critic_passed", False),
            "revisions": state.get("revision_count", 0),
        })

        if (idx + 1) % 10 == 0:
            print(f"Processed {idx + 1}/{len(eval_items)} queries...")

    with open("results/week2_compensated.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["query_id", "type", "compensated_passed", "reason", "critic_passed", "revisions"]
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n=== Week 2 Compensated Agent Evaluation Complete ===")
    print(f"Total Passed: {pass_count}/{len(eval_items)} ({pass_count/len(eval_items)*100:.1f}%)\n")

    for t in cat_total:
        p = cat_passed[t]
        tot = cat_total[t]
        print(f"  {t:<18}: {p}/{tot} ({p/tot*100:5.1f}%)")

    zs_pass = defaultdict(int)
    sp_pass = defaultdict(int)
    with open("results/week1_strategies.csv") as f:
        for r in csv.DictReader(f):
            t = r["type"]
            if r.get("zero_shot_passed") == "True":
                zs_pass[t] += 1
            if r.get("schema_cot_passed") == "True":
                sp_pass[t] += 1

    def fmt_before(types):
        tot = sum(cat_total[t] for t in types)
        z = sum(zs_pass[t] for t in types)
        s = sum(sp_pass[t] for t in types)
        return f"Zero-Shot: {z/tot*100:.1f}% ({z}/{tot}) / Schema-CoT: {s/tot*100:.1f}% ({s}/{tot})"

    def fmt_after(types):
        tot = sum(cat_total[t] for t in types)
        c = sum(cat_passed[t] for t in types)
        return f"**{c/tot*100:.1f}% ({c}/{tot})**"

    comp_md = f"""# Small Model (<=8B) Compensations Log

This document tracks specific failure modes observed on the hosted Qwen/Qwen3-8B endpoint (1024 max output tokens, 8192 context window) and the engineering compensations implemented to resolve them.

| ID | Observed 8B Weakness / Symptom | Engineering Compensation | Metric Tracked | Before (Week 1 Baselines) | After (Week 2 Compensated Agent) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| C1 | Context overflow, retrieval noise, and log prompt injection susceptibility | Tool-based log filtering (`search_logs`) + Hybrid BM25/Dense RAG (`bge-small-en-v1.5`) with delimiter guards | Retrieval & Doc-Injection Pass Rate (`runbook_gap`, `similar_incident`, `injection_doc`) | {fmt_before(["runbook_gap", "similar_incident", "injection_doc"])} | {fmt_after(["runbook_gap", "similar_incident", "injection_doc"])} |
| C2 | Incorrect chronological sorting and mixed timezone (`IST`/`PST` to `UTC`) arithmetic | Parse and normalize all timestamps to UTC in Python (`dateutil`); LLM only summarizes event text | Timeline Order & UTC Accuracy (`timeline`) | {fmt_before(["timeline"])} | {fmt_after(["timeline"])} |
| C3 | Unverified root causes and schema key drift on remediation tickets | Require explicit `evidence_ids` per hypothesis; Critic deterministically verifies ID existence | Root Cause & Action Item Accuracy (`root_cause`, `action_item`) | {fmt_before(["root_cause", "action_item"])} | {fmt_after(["root_cause", "action_item"])} |
| C4 | Template section omission and JSON truncation under the 1024 output token limit | Section-by-section modular assembly with Pydantic v2 validation | Template & Triage Schema Validity (`template`, `clarify`) | {fmt_before(["template", "clarify"])} | {fmt_after(["template", "clarify"])} |
| C5 | Blame attribution in chaotic chat threads and compliance with blame-forcing prompts | Deterministic blame-lexicon scanner paired with refusal routing and automated rewrite pass | Blameless & Refusal Pass Rate (`blameless`, `injection_user`) | {fmt_before(["blameless", "injection_user"])} | {fmt_after(["blameless", "injection_user"])} |
"""
    with open("docs/COMPENSATIONS.md", "w") as f:
        f.write(comp_md)
    print("\nUpdated docs/COMPENSATIONS.md with Before and After metrics!")

if __name__ == "__main__":
    run_week2_evaluation()
