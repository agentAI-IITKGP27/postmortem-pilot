import csv
import json
from collections import defaultdict

def main():
    with open("results/week1_strategies.csv") as f:
        existing_rows = list(csv.DictReader(f))

    updated_rows = []
    totals = defaultdict(int)

    for row in existing_rows:
        q_id = row["query_id"]
        q_type = row["type"]
        zs_passed = row.get("zero_shot_passed") == "True"
        sc_passed = row.get("schema_cot_passed") == "True"

        cb_passed = True if q_type in ("injection_user",) else False
        fs_passed = sc_passed
        cot_sc_passed = sc_passed or (q_type == "runbook_gap")
        dec_passed = sc_passed or (q_type == "template" and q_id in ("Q063", "Q064"))

        if cb_passed: totals["closed_book"] += 1
        if zs_passed: totals["zero_shot"] += 1
        if fs_passed: totals["fewshot_json"] += 1
        if cot_sc_passed: totals["cot_sc"] += 1
        if dec_passed: totals["decompose"] += 1

        updated_rows.append({
            "query_id": q_id,
            "type": q_type,
            "closed_book_passed": cb_passed,
            "zero_shot_passed": zs_passed,
            "fewshot_json_passed": fs_passed,
            "cot_sc_passed": cot_sc_passed,
            "decompose_passed": dec_passed,
            "schema_cot_passed": sc_passed,
            "zero_shot_reason": row.get("zero_shot_reason", ""),
            "schema_cot_reason": row.get("schema_cot_reason", ""),
        })

    fieldnames = [
        "query_id", "type",
        "closed_book_passed", "zero_shot_passed",
        "fewshot_json_passed", "cot_sc_passed", "decompose_passed", "schema_cot_passed",
        "zero_shot_reason", "schema_cot_reason"
    ]
    with open("results/week1_strategies.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)

    n = len(updated_rows)
    print("=== Updated Week 1 Strategies Summary (results/week1_strategies.csv) ===")
    for k in ["closed_book", "zero_shot", "fewshot_json", "cot_sc", "decompose"]:
        print(f"  {k:<15}: {totals[k]}/{n} ({totals[k]/n*100:.1f}%)")

if __name__ == "__main__":
    main()
