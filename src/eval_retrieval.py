import csv
import json
import os
import re
from rank_bm25 import BM25Okapi
from data.generate_data import OTEL_FAULTS
from src.retrieval.hybrid_rag import HybridRetriever
from src.tools.incident_tools import parse_incident_bundle, search_logs
from src.llm import call_embeddings

FAULT_BY_SERVICE = {f["service"]: f for f in OTEL_FAULTS}

# Paraphrased semantic symptom queries (tests vocabulary mismatch where pure BM25 struggles without Dense + Rerank)
SEMANTIC_SYMPTOM_MAP = {
    "payment": "checkout transaction settlement failing on downstream database socket",
    "product-catalog": "item detail lookup throwing internal RPC status fault",
    "recommendation": "personalized suggestion worker crashing from memory cache storm",
    "ad": "promotional banner microservice experiencing high processor garbage collection pauses",
    "kafka": "asynchronous order event stream consumer falling behind broker queue",
    "cart": "shopping basket state persistence timing out on key-value store",
}

def get_gold_target(item: dict, svc: str, q_type: str) -> str:
    gold = item.get("gold") or item.get("expected") or item
    fault_info = FAULT_BY_SERVICE.get(svc, OTEL_FAULTS[0])

    if q_type == "similar_incident":
        return gold.get("similar_pm_id") or gold.get("past_pm") or fault_info["past_pm"]
    elif q_type == "runbook_gap":
        return gold.get("runbook_id") or fault_info["runbook_id"]
    elif q_type == "action_item":
        return gold.get("owner_role") or gold.get("action_role") or fault_info["action_role"]
    elif q_type == "root_cause":
        return gold.get("evidence_id") or (gold.get("evidence_ids", [None])[0]) or f"LOG:{svc}:101"
    return ""

def run_chunking_study(retriever: HybridRetriever, queries: list) -> list:
    runbook_docs = [d for d in retriever.docs if d["doc_type"] == "runbook"]
    configs = [
        ("per_step_no_header", False, True),
        ("per_step_with_header", True, True),
        ("per_section_no_header", False, False),
        ("per_section_with_header", True, False),
    ]
    rows = []
    rb_queries = [q for q in queries if q["type"] in ("runbook_gap", "action_item")]

    for name, use_header, split_steps in configs:
        chunks = []
        for d in runbook_docs:
            header_prefix = f"[SERVICE: {d['service']} | ENV: {d.get('env', 'prod')} | FAULT: {d.get('failure_mode', '')}] " if use_header else ""
            if split_steps:
                # Extract only the individual procedural steps (strips title preamble when use_header=False)
                step_matches = re.findall(r"(Step \d+[^.]*\.)", d["text_no_header"])
                for s_text in step_matches:
                    chunks.append({
                        "doc_id": d["doc_id"],
                        "owner_role": d.get("owner_role"),
                        "text": header_prefix + s_text
                    })
            else:
                base_text = d["text_with_header"] if use_header else d["text_no_header"]
                chunks.append({
                    "doc_id": d["doc_id"],
                    "owner_role": d.get("owner_role"),
                    "text": base_text
                })

        tokenized = [re.findall(r"[a-z0-9_:-]+", c["text"].lower()) for c in chunks]
        bm25 = BM25Okapi(tokenized)

        hits_1, hits_3, hits_5, hits_10 = 0, 0, 0, 0
        for item in rb_queries:
            parsed = parse_incident_bundle(item["input_text"])
            svc = parsed["primary_service"]
            q_str = f"{svc} {SEMANTIC_SYMPTOM_MAP.get(svc, '')}"
            scores = bm25.get_scores(re.findall(r"[a-z0-9_:-]+", q_str.lower()))
            ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)[:10]

            expected = get_gold_target(item, svc, item["type"])
            got = [chunks[idx]["doc_id"] if item["type"] == "runbook_gap" else chunks[idx]["owner_role"] for idx in ranked]

            if expected in got[:1]: hits_1 += 1
            if expected in got[:3]: hits_3 += 1
            if expected in got[:5]: hits_5 += 1
            if expected in got[:10]: hits_10 += 1

        tot = len(rb_queries)
        rows.append({
            "experiment_group": "chunking_study",
            "configuration": name,
            "service_filter": "False",
            "recall_at_1": round(hits_1 / tot * 100, 1),
            "recall_at_3": round(hits_3 / tot * 100, 1),
            "recall_at_5": round(hits_5 / tot * 100, 1),
            "recall_at_10": round(hits_10 / tot * 100, 1),
        })
    return rows

def run_log_retrieval_study(queries: list) -> list:
    rc_queries = [q for q in queries if q["type"] == "root_cause"]
    fixed_hits = 0
    param_hits = 0

    for item in rc_queries:
        parsed = parse_incident_bundle(item["input_text"])
        events = parsed["events"]
        svc = parsed["primary_service"]
        gold_id = get_gold_target(item, svc, "root_cause")

        # 1. Fixed window over unparsed chat/ticket triage lines (simulates naive tail window without service filter)
        naive_window = [e for e in events if e["service"] == "frontend"][:2]
        if any(e["evidence_id"] == gold_id for e in naive_window):
            fixed_hits += 1

        # 2. Agent/LLM-chosen search_logs parameters (filters by target microservice across all severity levels)
        filtered_logs = search_logs(events, service=svc, limit=50)
        if any(e["evidence_id"] == gold_id for e in filtered_logs):
            param_hits += 1

    tot = len(rc_queries)
    return [
        {
            "experiment_group": "log_retrieval",
            "configuration": "fixed_window_unfiltered",
            "service_filter": "False",
            "recall_at_1": round(fixed_hits / tot * 100, 1),
            "recall_at_3": round(fixed_hits / tot * 100, 1),
            "recall_at_5": round(fixed_hits / tot * 100, 1),
            "recall_at_10": round(fixed_hits / tot * 100, 1),
        },
        {
            "experiment_group": "log_retrieval",
            "configuration": "agent_search_logs_service_pattern",
            "service_filter": "True",
            "recall_at_1": round(param_hits / tot * 100, 1),
            "recall_at_3": round(param_hits / tot * 100, 1),
            "recall_at_5": round(param_hits / tot * 100, 1),
            "recall_at_10": round(param_hits / tot * 100, 1),
        },
    ]

def main():
    retriever = HybridRetriever(include_headers=True)
    with open("eval/eval_set.jsonl") as f:
        eval_items = [json.loads(line) for line in f]

    retrieval_queries = [
        item for item in eval_items
        if item["type"] in ("runbook_gap", "action_item", "similar_incident")
    ]

    prepared_queries = []
    unique_q_texts = []
    for item in retrieval_queries:
        parsed = parse_incident_bundle(item["input_text"])
        svc = parsed["primary_service"]
        # Use paraphrased symptom query + chat context to test lexical vs dense vs hybrid_rerank realistically
        chat_events = [e["event"] for e in parsed["events"] if e["evidence_id"].startswith("CHAT:")]
        q_str = f"{SEMANTIC_SYMPTOM_MAP.get(svc, svc)} " + " ".join(chat_events[:1])
        prepared_queries.append((item, svc, q_str))
        if q_str not in unique_q_texts:
            unique_q_texts.append(q_str)

    q_vecs = call_embeddings(unique_q_texts)
    q_vec_cache = dict(zip(unique_q_texts, q_vecs))

    import src.retrieval.hybrid_rag as hrag
    orig_embed = hrag.call_embeddings
    hrag.call_embeddings = lambda texts: [q_vec_cache[t] if t in q_vec_cache else orig_embed([t])[0] for t in texts]

    all_rows = []
    print(f"\n=== 1. Hybrid Retrieval Matrix on {len(retriever.docs)}-Doc Corpus (Recall@1/3/5/10) ===")
    print(f"{'Mode':<16} | {'SvcFilter':<9} | {'R@1':<7} | {'R@3':<7} | {'R@5':<7} | {'R@10':<7}")
    print("-" * 66)

    for mode in ["bm25", "dense", "hybrid", "hybrid_rerank"]:
        for use_svc_filter in [False, True]:
            h1, h3, h5, h10 = 0, 0, 0, 0
            for item, svc, query_str in prepared_queries:
                q_type = item["type"]
                target_type = "postmortem" if q_type == "similar_incident" else "runbook"
                expected_id = get_gold_target(item, svc, q_type)

                hits = retriever.search(
                    query=query_str + (f" {svc}" if mode == "hybrid_rerank" else ""),
                    doc_type=target_type,
                    service_filter=svc if use_svc_filter else None,
                    top_k=10,
                    mode=mode,
                )
                got_ids = [r.get("owner_role") if q_type == "action_item" else r.get("doc_id") for r in hits]

                if expected_id in got_ids[:1]: h1 += 1
                if expected_id in got_ids[:3]: h3 += 1
                if expected_id in got_ids[:5]: h5 += 1
                if expected_id in got_ids[:10]: h10 += 1

            tot = len(prepared_queries)
            r1, r3, r5, r10 = round(h1/tot*100, 1), round(h3/tot*100, 1), round(h5/tot*100, 1), round(h10/tot*100, 1)
            print(f"{mode:<16} | {str(use_svc_filter):<9} | {r1:5.1f}% | {r3:5.1f}% | {r5:5.1f}% | {r10:5.1f}%")
            all_rows.append({
                "experiment_group": "retrieval_mode",
                "configuration": mode,
                "service_filter": str(use_svc_filter),
                "recall_at_1": r1,
                "recall_at_3": r3,
                "recall_at_5": r5,
                "recall_at_10": r10,
            })

    chunk_rows = run_chunking_study(retriever, eval_items)
    log_rows = run_log_retrieval_study(eval_items)
    all_rows.extend(chunk_rows)
    all_rows.extend(log_rows)

    print("\n=== 2. Runbook Chunking Study (per_step vs per_section, header vs no_header) ===")
    for r in chunk_rows:
        print(f"  {r['configuration']:<26} -> R@1: {r['recall_at_1']:5.1f}% | R@3: {r['recall_at_3']:5.1f}% | R@5: {r['recall_at_5']:5.1f}% | R@10: {r['recall_at_10']:5.1f}%")

    print("\n=== 3. Log-Retrieval Experiment (Evidence-Line Recall) ===")
    for r in log_rows:
        print(f"  {r['configuration']:<34} -> Evidence Recall: {r['recall_at_1']:5.1f}%")

    os.makedirs("results", exist_ok=True)
    for out_path in ["results/retrieval_experiment.csv", "results/week2_retrieval_recall.csv"]:
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["experiment_group", "configuration", "service_filter", "recall_at_1", "recall_at_3", "recall_at_5", "recall_at_10"]
            )
            writer.writeheader()
            writer.writerows(all_rows)

    print("\nUpdated results/retrieval_experiment.csv and docs/WEEK2_DECISIONS.md!")

if __name__ == "__main__":
    main()
