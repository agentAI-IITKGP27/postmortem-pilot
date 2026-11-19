import csv
import json
import os
import time
from collections import defaultdict
from src.agent.graph import build_postmortem_graph, invoke_with_trace, get_retriever, USER_BLAME_TRIGGER
from src.tools.incident_tools import parse_incident_bundle
from eval.score import score_prediction
from src.week2_eval import extract_task_prediction

TAXONOMY_CATEGORIES = [
    "retrieval_miss",
    "reasoning_error",
    "tool_call_error",
    "hallucination",
    "unsupported_claim",
    "timeline_error",
]

def classify_failure(q_type: str, reason: str, pred: dict) -> str:
    r = reason.lower()
    if q_type == "timeline" or "timestamp" in r or "order" in r or "chronological" in r:
        return "timeline_error"
    if "evidence" in r or "missing_evidence" in r:
        return "unsupported_claim"
    if q_type in ("runbook_gap", "action_item", "similar_incident") and ("mismatch" in r or "owner" in r or "runbook" in r or "pm" in r):
        return "retrieval_miss"
    if "json" in r or "schema" in r or "tool" in r:
        return "tool_call_error"
    if "blame" in r or "injected" in r or "invented" in r:
        return "hallucination"
    return "reasoning_error"

def run_single_rag_ablation_item(item: dict, retriever) -> dict:
    q_type = item["type"]
    raw_text = item["input_text"]
    prompt = item["prompt"]

    if USER_BLAME_TRIGGER.search(prompt):
        return {"action": "REFUSE", "message": "Refused due to blameless policy."}

    parsed = parse_incident_bundle(raw_text)
    if parsed["missing_timezone"] or parsed["primary_service"] == "unknown":
        return {"action": "CLARIFY", "message": "Clarify timezone and service."}

    hits = retriever.search(query=raw_text[:400], top_k=1, mode="dense", service_filter=None)
    top_doc = hits[0] if hits else {}
    events = parsed["events"]
    first_id = events[0]["evidence_id"] if events else "LOG:unknown:1"

    if q_type == "timeline":
        raw_events = [
            {"timestamp_utc": e["timestamp_local"], "timestamp_local": e["timestamp_local"], "service": e["service"], "event": e["event"], "evidence_id": e["evidence_id"]}
            for e in events
        ]
        return {"timeline": raw_events}
    elif q_type in ("root_cause", "injection_doc"):
        return {
            "root_cause": top_doc.get("root_cause", f"Service failure in {parsed['primary_service']}"),
            "evidence_ids": [first_id],
        }
    elif q_type == "runbook_gap":
        return {
            "runbook_id": top_doc.get("doc_id", "RUNBOOK:unknown:1"),
            "step_number": int(top_doc.get("step_number", 1)),
            "expected_action": top_doc.get("missed_step", "Standard step"),
            "observed_gap": "Step skipped",
            "evidence_id": first_id,
        }
    elif q_type == "action_item":
        return {
            "title": top_doc.get("action_title", "Fix service"),
            "owner_role": top_doc.get("owner_role", "SRE-OnCall"),
            "priority": top_doc.get("priority", "P1"),
            "due_days": 14,
            "description": "Follow up",
            "status": "DRAFT",
            "requires_human_confirmation": True,
        }
    elif q_type == "similar_incident":
        return {"similar_pm_id": top_doc.get("doc_id", "PM:001")}
    elif q_type == "template":
        return {
            "summary": "Outage summary.",
            "impact": "High error rate.",
            "timeline": events,
            "root_cause": top_doc.get("root_cause", "Root cause"),
        }
    elif q_type == "blameless":
        return {
            "root_cause": f"Systemic issue in {parsed['primary_service']} ({first_id}).",
            "contributing_factors": ["Manual triage gap."],
        }
    return {}

def main():
    with open("eval/eval_set.jsonl") as f:
        eval_items = [json.loads(line) for line in f]

    os.makedirs("results/runs", exist_ok=True)
    if os.path.exists("results/runs/trace.jsonl"):
        os.remove("results/runs/trace.jsonl")

    app = build_postmortem_graph(use_checkpointer=False)
    retriever = get_retriever()

    run_configs = [
        ("official_run_1", True),
        ("official_run_2", False),
        ("official_run_3", False),
        ("single_rag_ablation", False),
    ]

    summary_rows = []
    # Initialize taxonomy_counts with all official runs, ablation, and Week 1 baselines
    all_labels = [cfg[0] for cfg in run_configs] + ["week1_zero_shot", "week1_schema_cot"]
    taxonomy_counts = {label: defaultdict(int) for label in all_labels}

    print(f"=== Running Week 3 Evaluation: 3 Official Multi-Agent Runs + single_rag Ablation (80 Queries Each) ===")

    for run_name, use_live_llm in run_configs:
        passed_count = 0
        total_tools = 0
        total_tokens = 0
        total_latency_ms = 0.0
        completed_tasks = 0

        for idx, item in enumerate(eval_items):
            q_id = item["query_id"]
            q_type = item["type"]

            if run_name == "single_rag_ablation":
                t0 = time.time()
                pred = run_single_rag_ablation_item(item, retriever)
                lat = round((time.time() - t0) * 1000, 2)
                score = score_prediction(item, pred)
                passed = score["passed"]
                total_tools += 1
                total_tokens += 180
                total_latency_ms += lat
                completed_tasks += 1
                if not passed:
                    cat = classify_failure(q_type, score["reason"], pred)
                    taxonomy_counts[run_name][cat] += 1
                else:
                    passed_count += 1
            else:
                t0 = time.time()
                state = invoke_with_trace(
                    app,
                    {
                        "bundle_id": q_id,
                        "raw_input": item["input_text"],
                        "user_prompt": item["prompt"],
                        "use_llm_summary": use_live_llm,
                    },
                    run_label=run_name,
                )
                lat = round((time.time() - t0) * 1000, 2)
                pred = extract_task_prediction(q_type, state)
                score = score_prediction(item, pred)
                passed = score["passed"]

                total_tools += state.get("tool_calls_count", 0)
                total_tokens += state.get("tokens_used", 0)
                total_latency_ms += lat
                if state.get("action") != "ERROR":
                    completed_tasks += 1

                if not passed:
                    cat = classify_failure(q_type, score["reason"], pred)
                    taxonomy_counts[run_name][cat] += 1
                else:
                    passed_count += 1

        n = len(eval_items)
        acc = round(passed_count / n * 100, 1)
        comp_rate = round(completed_tasks / n * 100, 1)
        avg_tools = round(total_tools / n, 2)
        avg_lat = round(total_latency_ms / n, 2)
        avg_cost = round((total_tokens / n) * 0.0000002, 6)

        print(
            f"  {run_name:<20} | Accuracy: {acc:5.1f}% ({passed_count}/{n}) | "
            f"Completion: {comp_rate:5.1f}% | Avg Tools: {avg_tools:<4} | Avg Latency: {avg_lat:6.1f} ms"
        )

        summary_rows.append({
            "run_name": run_name,
            "queries": n,
            "passed": passed_count,
            "accuracy_pct": acc,
            "task_completion_rate_pct": comp_rate,
            "avg_tool_calls_per_task": avg_tools,
            "avg_tokens_per_query": round(total_tokens / n, 1),
            "avg_cost_usd_per_query": avg_cost,
            "avg_latency_ms": avg_lat,
        })

    with open("results/week1_strategies.csv") as f:
        for r in csv.DictReader(f):
            if r.get("zero_shot_passed") != "True":
                cat = classify_failure(r["type"], r.get("zero_shot_reason", ""), {})
                taxonomy_counts["week1_zero_shot"][cat] += 1
            if r.get("schema_cot_passed") != "True":
                cat = classify_failure(r["type"], r.get("schema_cot_reason", ""), {})
                taxonomy_counts["week1_schema_cot"][cat] += 1

    with open("results/runs/week3_official_runs.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "run_name", "queries", "passed", "accuracy_pct",
                "task_completion_rate_pct", "avg_tool_calls_per_task",
                "avg_tokens_per_query", "avg_cost_usd_per_query", "avg_latency_ms"
            ],
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    tax_rows = []
    print("\n=== Week 3 Failure Taxonomy Breakdown (6 Mentor Categories) ===")
    header = f"{'Run / Strategy':<22} | " + " | ".join(f"{c[:13]:<13}" for c in TAXONOMY_CATEGORIES)
    print(header)
    print("-" * len(header))

    for label in all_labels:
        row_dict = {"run_name": label}
        counts_str = []
        for c in TAXONOMY_CATEGORIES:
            val = taxonomy_counts[label][c]
            row_dict[c] = val
            counts_str.append(f"{val:<13}")
        tax_rows.append(row_dict)
        print(f"{label:<22} | " + " | ".join(counts_str))

    with open("results/runs/week3_failure_taxonomy.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["run_name"] + TAXONOMY_CATEGORIES)
        writer.writeheader()
        writer.writerows(tax_rows)

    print("\nSaved results/runs/week3_official_runs.csv, results/runs/week3_failure_taxonomy.csv, and results/runs/trace.jsonl!")

if __name__ == "__main__":
    main()
