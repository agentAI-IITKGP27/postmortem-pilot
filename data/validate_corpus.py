#!/usr/bin/env python3
"""Validate data/corpus against the spec and the Week 1 gold.

    python -m data.validate_corpus [--dir data/corpus]

Checks:
  1. Counts per set (300 runbooks, 80 SOPs, 50 dependency notes, 30 poisoned, postmortems fill the rest).
  2. Unique doc IDs, no unfilled placeholders, every doc has a label and vice versa.
  3. Gold alignment with data/generate_data.py OTEL_FAULTS:
       - RB-<svc>-flag_fault-{prod,staging} step <gold step> == the gold missed step text
       - PM:00x exists, mentions the gold flag, and is the only *clean* postmortem that does
  4. Leakage: corpus.jsonl contains no label fields and no poison/distractor wording.
  5. Hygiene of non-poisoned docs: no blame lexicon, no emails/IPs/tokens, no real names used for blame.
  6. Determinism: regenerating with seed 42 reproduces the manifest sha256 exactly.
Exits non-zero on any failure.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

from data.generate_corpus import (N_DEPS, N_POISON, N_RUNBOOKS, N_SOPS, PLACEHOLDER, build_corpus, sha256,
                                  to_jsonl, gold_step_text)
from data.generate_data import OTEL_FAULTS
from eval.score import BLAME_LEXICON

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
TOKEN = re.compile(r"(?i)(\b(api[_-]?key|token|password|secret)\s*[=:]\s*\S+|bearer\s+[A-Za-z0-9_\-.]{16,})")
BLAME_NAMES = ["ravi", "priya", "daniel", "mei"]
LABEL_FIELDS = {"set", "poison_type", "gold_for", "expected_behavior", "shadows", "injection_key", "blamed_name"}
LEAK_WORDS = re.compile(r"(?i)\b(poison(ed)?|distractor|gold[_ ]for|hard negative)\b")


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/corpus")
    ap.add_argument("--skip-determinism", action="store_true")
    args = ap.parse_args()

    corpus_path = os.path.join(args.dir, "corpus.jsonl")
    labels_path = os.path.join(args.dir, "labels", "doc_labels.jsonl")
    docs, labels = load_jsonl(corpus_path), load_jsonl(labels_path)
    with open(os.path.join(args.dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    by_id = {d["doc_id"]: d for d in docs}
    lab = {l["doc_id"]: l for l in labels}
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # 1. counts
    sets = Counter(l["set"] for l in labels)
    n_pm = manifest["args"]["n_postmortems"]
    expected = {"runbook": N_RUNBOOKS, "sop": N_SOPS, "dependency_note": N_DEPS, "poisoned": N_POISON, "postmortem": n_pm}
    for k, v in expected.items():
        check(sets.get(k) == v, f"count[{k}] = {sets.get(k)} expected {v}")
    check(len(docs) == sum(expected.values()), f"total {len(docs)} != {sum(expected.values())}")

    # 2. ids / placeholders / label coverage
    check(len(by_id) == len(docs), "duplicate doc_id values")
    check(set(by_id) == set(lab), "corpus and labels disagree on doc_ids")
    check(not any(PLACEHOLDER in d["text"] for d in docs), "unfilled doc-id placeholder in text")
    check(all(d["doc_id"] in d["text"] or d["doc_type"] == "runbook" for d in docs), "doc text missing its own ID")

    # 3. gold alignment
    clean = [d for d in docs if lab[d["doc_id"]]["set"] != "poisoned"]
    for f in OTEL_FAULTS:
        svc, k, want = f["service"], f["runbook_step"], gold_step_text(f)
        for env in ("prod", "staging"):
            rb = by_id.get(f"RB-{svc}-flag_fault-{env}")
            check(rb is not None, f"missing gold runbook RB-{svc}-flag_fault-{env}")
            if rb:
                got = rb["steps"][k - 1]
                check(got["text"] == want and got["cite_as"] == f["runbook_id"],
                      f"{rb['doc_id']} step {k} != gold ({got['text'][:60]!r})")
                check(f"{k}. {want}" in rb["text"], f"{rb['doc_id']} text missing numbered gold step")
        pm = by_id.get(f["past_pm"])
        check(pm is not None and f["fault_flag"] in pm["text"], f"gold postmortem {f['past_pm']} missing or wrong")
        pms_with_flag = [d["doc_id"] for d in clean if d["doc_type"] == "postmortem" and f["fault_flag"] in d["text"]]
        check(pms_with_flag == [f["past_pm"]], f"{f['fault_flag']} appears in clean postmortems {pms_with_flag}")
        allowed = {f"RB-{svc}-flag_fault-prod", f"RB-{svc}-flag_fault-staging", f["past_pm"]}
        stray = [d["doc_id"] for d in clean if f["fault_flag"] in d["text"] and d["doc_id"] not in allowed]
        check(not stray, f"{f['fault_flag']} leaks into clean docs {stray[:5]}")
        rb_ids = [d["doc_id"] for d in clean if d["doc_type"] == "runbook" and want in d["text"]]
        check(sorted(rb_ids) == sorted(allowed - {f["past_pm"]}), f"gold step text appears in {rb_ids}")

    # 4. leakage
    for d in docs:
        check(not (LABEL_FIELDS & set(d)), f"{d['doc_id']} carries label fields")
        check(not LEAK_WORDS.search(d["text"] + d["title"]), f"{d['doc_id']} text reveals its label")
    for l in labels:
        if l["set"] == "poisoned":
            check(bool(l.get("poison_type")) and bool(l.get("expected_behavior")), f"{l['doc_id']} poison label incomplete")

    # 5. hygiene of clean docs; poisoned docs must actually carry their payload
    for d in clean:
        t = d["text"].lower()
        hits = [w for w in BLAME_LEXICON if w in t] + [n for n in BLAME_NAMES if re.search(rf"\b{n}\b", t)]
        check(not hits, f"{d['doc_id']} has blame terms {hits}")
        check(not EMAIL.search(d["text"]), f"{d['doc_id']} contains an email address")
        check(not IPV4.search(d["text"]), f"{d['doc_id']} contains an IPv4 address")
        check(not TOKEN.search(d["text"]), f"{d['doc_id']} contains a credential-like string")
    for l in labels:
        if l["set"] != "poisoned":
            continue
        t = by_id[l["doc_id"]]["text"]
        p = l["poison_type"]
        if p == "blame_laden_postmortem":
            check(any(w in t.lower() for w in BLAME_LEXICON), f"{l['doc_id']} has no blame payload")
        elif p == "pii_secret_leak":
            check(EMAIL.search(t) and IPV4.search(t) and TOKEN.search(t), f"{l['doc_id']} missing PII payload")
        elif p == "stale_runbook":
            f = next(x for x in OTEL_FAULTS if x["service"] == l["target_service"])
            check(gold_step_text(f) not in t, f"{l['doc_id']} still contains the gold step")
        elif p == "conflicting_step_number":
            f = next(x for x in OTEL_FAULTS if x["service"] == l["target_service"])
            st = by_id[l["doc_id"]]["steps"]
            check(st[f["runbook_step"]]["text"] == gold_step_text(f), f"{l['doc_id']} gold step not shifted to k+1")

    # 6. determinism
    with open(corpus_path, encoding="utf-8") as f:
        on_disk = f.read()
    check(sha256(on_disk) == manifest["sha256"]["corpus.jsonl"], "corpus.jsonl does not match manifest sha256")
    if not args.skip_determinism:
        d2, l2 = build_corpus(manifest["args"]["total"], n_pm)
        check(sha256(to_jsonl(d2)) == manifest["sha256"]["corpus.jsonl"], "regeneration changed corpus.jsonl")
        check(sha256(to_jsonl(l2)) == manifest["sha256"]["labels/doc_labels.jsonl"], "regeneration changed labels")

    print(f"Checked {len(docs)} docs: " + ", ".join(f"{k}={v}" for k, v in sorted(sets.items())))
    if failures:
        print(f"FAILED ({len(failures)}):")
        for m in failures[:50]:
            print("  -", m)
        sys.exit(1)
    print("All corpus checks passed.")


if __name__ == "__main__":
    main()
