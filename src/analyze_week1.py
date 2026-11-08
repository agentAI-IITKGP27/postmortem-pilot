import csv
import json
from collections import defaultdict

def main():
    cat_total = defaultdict(int)
    zs_passed = defaultdict(int)
    sp_passed = defaultdict(int)
    failure_reasons = defaultdict(int)
    has_two_strategies = False

    with open("results/week1_strategies.csv") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_two_strategies = "schema_cot_passed" in fieldnames

        for row in reader:
            q_type = row["type"]
            cat_total[q_type] += 1

            if has_two_strategies:
                z_ok = row["zero_shot_passed"] == "True"
                s_ok = row["schema_cot_passed"] == "True"
                if z_ok:
                    zs_passed[q_type] += 1
                if s_ok:
                    sp_passed[q_type] += 1
                else:
                    failure_reasons[row["schema_cot_reason"]] += 1
            else:
                z_ok = row["passed"] == "True"
                if z_ok:
                    zs_passed[q_type] += 1
                else:
                    failure_reasons[row["reason"]] += 1

    print("=== Week 1 Baseline Breakdown by Category ===")
    if has_two_strategies:
        print(f"{'Category':<18} | {'Zero-Shot':<14} | {'Schema-CoT':<14}")
        print("-" * 52)
        for q_type, tot in cat_total.items():
            z = zs_passed[q_type]
            s = sp_passed[q_type]
            print(f"{q_type:<18} | {z}/{tot} ({z/tot*100:5.1f}%) | {s}/{tot} ({s/tot*100:5.1f}%)")
    else:
        print(f"{'Category':<18} | {'Zero-Shot':<14}")
        print("-" * 36)
        for q_type, tot in cat_total.items():
            z = zs_passed[q_type]
            print(f"{q_type:<18} | {z}/{tot} ({z/tot*100:5.1f}%)")

    print("\n=== Remaining Failure Reasons ===")
    for reason, count in sorted(failure_reasons.items(), key=lambda x: -x[1]):
        print(f"  - {reason:<28}: {count}")

    def fmt_metric(types):
        tot = sum(cat_total[t] for t in types)
        if not tot:
            return "0.0%"
        z = sum(zs_passed[t] for t in types)
        if has_two_strategies:
            s = sum(sp_passed[t] for t in types)
            return f"Zero-Shot: {z/tot*100:.1f}% ({z}/{tot}) / Schema-CoT: {s/tot*100:.1f}% ({s}/{tot})"
        return f"Zero-Shot: {z/tot*100:.1f}% ({z}/{tot})"

    c1_val = fmt_metric(["runbook_gap", "similar_incident", "injection_doc"])
    c2_val = fmt_metric(["timeline"])
    c3_val = fmt_metric(["root_cause", "action_item"])
    c4_val = fmt_metric(["template", "clarify"])
    c5_val = fmt_metric(["blameless", "injection_user"])

    comp_md = f"""# Small Model (<=8B) Compensations Log

This document tracks specific failure modes observed on the hosted Qwen/Qwen3-8B endpoint (1024 max output tokens, 8192 context window) and the engineering compensations implemented to resolve them.

| ID | Observed 8B Weakness / Symptom | Engineering Compensation | Metric Tracked | Before (Week 1 Baselines) | After (Compensated) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| C1 | Context overflow, retrieval noise, and log prompt injection susceptibility | Tool-based log filtering (`search_logs`) + Hybrid BM25/Dense RAG with delimiter guards | Retrieval & Doc-Injection Pass Rate (`runbook_gap`, `similar_incident`, `injection_doc`) | {c1_val} | TBD (Week 3) |
| C2 | Incorrect chronological sorting and mixed timezone (`IST`/`PST` to `UTC`) arithmetic | Parse and normalize all timestamps to UTC in Python (`dateutil`); LLM only summarizes event text | Timeline Order & UTC Accuracy (`timeline`) | {c2_val} | TBD (Week 3) |
| C3 | Unverified root causes and schema key drift on remediation tickets | Require explicit `evidence_ids` per hypothesis; Critic deterministically verifies ID existence | Root Cause & Action Item Accuracy (`root_cause`, `action_item`) | {c3_val} | TBD (Week 3) |
| C4 | Template section omission and JSON truncation under the 1024 output token limit | Section-by-section Pydantic v2 validation via `instructor` with error-feedback retries | Template & Triage Schema Validity (`template`, `clarify`) | {c4_val} | TBD (Week 3) |
| C5 | Blame attribution in chaotic chat threads and compliance with blame-forcing prompts | Deterministic blame-lexicon scanner paired with refusal routing and automated rewrite pass | Blameless & Refusal Pass Rate (`blameless`, `injection_user`) | {c5_val} | TBD (Week 3) |
"""

    with open("docs/COMPENSATIONS.md", "w") as f:
        f.write(comp_md)

    print("\nSuccessfully updated docs/COMPENSATIONS.md.")

if __name__ == "__main__":
    main()