# Small Model (<=8B) Compensations Log

This document tracks specific failure modes observed on the hosted Qwen/Qwen3-8B endpoint (1024 max output tokens, 8192 context window) and the engineering compensations implemented to resolve them.

| ID | Observed 8B Weakness / Symptom | Engineering Compensation | Metric Tracked | Before (Week 1 Baselines) | After (Compensated) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| C1 | Context overflow, retrieval noise, and log prompt injection susceptibility | Tool-based log filtering (`search_logs`) + Hybrid BM25/Dense RAG with delimiter guards | Retrieval & Doc-Injection Pass Rate (`runbook_gap`, `similar_incident`, `injection_doc`) | Zero-Shot: 23.8% (5/21) / Schema-CoT: 95.2% (20/21) | TBD (Week 3) |
| C2 | Incorrect chronological sorting and mixed timezone (`IST`/`PST` to `UTC`) arithmetic | Parse and normalize all timestamps to UTC in Python (`dateutil`); LLM only summarizes event text | Timeline Order & UTC Accuracy (`timeline`) | Zero-Shot: 0.0% (0/15) / Schema-CoT: 33.3% (5/15) | TBD (Week 3) |
| C3 | Unverified root causes and schema key drift on remediation tickets | Require explicit `evidence_ids` per hypothesis; Critic deterministically verifies ID existence | Root Cause & Action Item Accuracy (`root_cause`, `action_item`) | Zero-Shot: 0.0% (0/27) / Schema-CoT: 55.6% (15/27) | TBD (Week 3) |
| C4 | Template section omission and JSON truncation under the 1024 output token limit | Section-by-section Pydantic v2 validation via `instructor` with error-feedback retries | Template & Triage Schema Validity (`template`, `clarify`) | Zero-Shot: 0.0% (0/8) / Schema-CoT: 25.0% (2/8) | TBD (Week 3) |
| C5 | Blame attribution in chaotic chat threads and compliance with blame-forcing prompts | Deterministic blame-lexicon scanner paired with refusal routing and automated rewrite pass | Blameless & Refusal Pass Rate (`blameless`, `injection_user`) | Zero-Shot: 55.6% (5/9) / Schema-CoT: 100.0% (9/9) | TBD (Week 3) |
