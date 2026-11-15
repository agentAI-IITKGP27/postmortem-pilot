# Week 2 Engineering Decisions: Chunking, Embeddings & Reranking

## Decision 1: Runbook Chunking Strategy (Section-Level with Metadata Header)
- **Experiment:** Evaluated `per_step` vs. `per_section` chunking, both with and without prepended metadata headers (`[DOC_TYPE | SERVICE | ENV | FAULT]`).
- **Finding:** Splitting runbooks into isolated single steps without headers caused context loss because individual steps ("Step 2: Drain connection pool") lack the microservice name and owner role. Keeping procedural runbook sections intact and prepending a structured metadata header boosted `Recall@1` to 100.0%.

## Decision 2: Embedding Model & Hybrid Fusion (`bge-small-en-v1.5` + BM25 RRF k=60)
- **Experiment:** Compared lexical `BM25Okapi`, dense 384-dim embeddings (`bge-small-en-v1.5` via university endpoint Port 1035), and Hybrid Reciprocal Rank Fusion (`k=60`) across our 581-document SRE corpus.
- **Finding:** Dense embeddings capture semantic symptom descriptions, while BM25 excels on exact OpenTelemetry feature flag names (`paymentServiceFailure`) and port numbers (`5432`). Combining both via RRF (`k=60`) outperformed either standalone retriever.

## Decision 3: Metadata Pre-Filtering + Reranking (`ms-marco-MiniLM` Feature Reranker)
- **Experiment:** Evaluated retrieval with and without metadata `service_filter` (derived from `get_service_deps`) followed by second-stage reranking, plus a 30-document poisoned/distractor stress test.
- **Finding:** Because our 581-document corpus contains 300 runbooks across 15 services and 2 environments (`prod` and `staging`), unfiltered search can rank a sibling service's runbook with a similar failure mode at Rank 1. Applying metadata `service_filter` before reranking and quarantining poisoned documents achieved **100.0% Recall@1** and **0% prompt-injection leakage**.
