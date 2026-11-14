"""Week 2 Task #4: benchmark BM25, dense, hybrid RRF and reranked retrieval.

Runs every strategy over the same corpus and query set, then writes
``results/retrieval_experiment.csv`` with recall@1, recall@5, MRR@10,
nDCG@10 and latency per configuration.
"""

import csv
import math
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

from src.retrieval.hybrid_rag import (
    EMBED_MODEL_NAME,
    RERANK_MODEL_NAME,
    RETRIEVAL_STRATEGIES,
    HybridRetriever,
    Query,
    chunk_documents,
    load_corpus,
    load_queries,
)

TOP_K = 10
OUTPUT_PATH = "results/retrieval_experiment.csv"

CONFIGS: List[Dict[str, object]] = [
    {"chunking": "section", "prepend_metadata": True, "metadata_filter": True},
    {"chunking": "step", "prepend_metadata": True, "metadata_filter": True},
    {"chunking": "section", "prepend_metadata": False, "metadata_filter": True},
    {"chunking": "section", "prepend_metadata": True, "metadata_filter": False},
]

FIELDNAMES = [
    "experiment",
    "strategy",
    "dense_model",
    "rerank_model",
    "chunking",
    "metadata_header",
    "metadata_filter",
    "num_docs",
    "num_chunks",
    "num_queries",
    "recall_at_1",
    "recall_at_5",
    "mrr_at_10",
    "ndcg_at_10",
    "avg_latency_ms",
]


def _first_rank(ranked_doc_ids: Sequence[str], gold_doc_id: str) -> Optional[int]:
    for rank, doc_id in enumerate(ranked_doc_ids, start=1):
        if doc_id == gold_doc_id:
            return rank
    return None


def _dedupe_doc_ids(retriever: HybridRetriever, hits: Sequence[Tuple[int, float]]) -> List[str]:
    seen: List[str] = []
    for chunk_id, _ in hits:
        doc_id = retriever.chunks[chunk_id].doc_id
        if doc_id not in seen:
            seen.append(doc_id)
    return seen


def evaluate(
    retriever: HybridRetriever,
    queries: Sequence[Query],
    strategy: str,
    metadata_filter: bool,
) -> Dict[str, float]:
    hits_at_1 = hits_at_5 = 0
    reciprocal_rank = 0.0
    ndcg = 0.0
    timings: List[float] = []

    # Warm up lazy components so latency reflects steady-state query time.
    if strategy in ("dense", "hybrid", "hybrid_rerank"):
        _ = retriever.client
        retriever.encoder.encode([queries[0].text], show_progress_bar=False)
    if strategy == "hybrid_rerank":
        _ = retriever.reranker

    for query in queries:
        started = time.perf_counter()
        hits = retriever.search(
            query.text,
            strategy=strategy,
            top_k=TOP_K,
            service=query.service,
            failure_mode=query.failure_mode,
            metadata_filter=metadata_filter,
        )
        timings.append((time.perf_counter() - started) * 1000)

        rank = _first_rank(_dedupe_doc_ids(retriever, hits), query.gold_doc_id)
        if rank == 1:
            hits_at_1 += 1
        if rank is not None and rank <= 5:
            hits_at_5 += 1
        if rank is not None and rank <= TOP_K:
            reciprocal_rank += 1.0 / rank
            ndcg += 1.0 / math.log2(rank + 1)

    total = len(queries)
    return {
        "num_queries": total,
        "recall_at_1": round(hits_at_1 / total, 4),
        "recall_at_5": round(hits_at_5 / total, 4),
        "mrr_at_10": round(reciprocal_rank / total, 4),
        "ndcg_at_10": round(ndcg / total, 4),
        "avg_latency_ms": round(sum(timings) / total, 2),
    }


def run_benchmark(
    corpus_dir: str = "data/corpus",
    bundles_dir: str = "data/bundles",
    output_path: str = OUTPUT_PATH,
) -> List[Dict[str, object]]:
    documents = load_corpus(corpus_dir=corpus_dir, bundles_dir=bundles_dir)
    queries = load_queries(documents, corpus_dir=corpus_dir)
    print(f"Loaded {len(documents)} documents and {len(queries)} queries.")

    rows: List[Dict[str, object]] = []
    for config in CONFIGS:
        chunks = chunk_documents(
            documents,
            mode=str(config["chunking"]),
            prepend_metadata=bool(config["prepend_metadata"]),
        )
        print(
            f"\nConfig chunking={config['chunking']} "
            f"header={config['prepend_metadata']} "
            f"filter={config['metadata_filter']} -> {len(chunks)} chunks"
        )
        retriever = HybridRetriever(chunks)
        for strategy in RETRIEVAL_STRATEGIES:
            metrics = evaluate(
                retriever,
                queries,
                strategy=strategy,
                metadata_filter=bool(config["metadata_filter"]),
            )
            row = {
                "experiment": "retrieval_sweep",
                "strategy": strategy,
                "dense_model": EMBED_MODEL_NAME,
                "rerank_model": RERANK_MODEL_NAME if strategy == "hybrid_rerank" else "",
                "chunking": config["chunking"],
                "metadata_header": config["prepend_metadata"],
                "metadata_filter": config["metadata_filter"],
                "num_docs": len(documents),
                "num_chunks": len(chunks),
                **metrics,
            }
            rows.append(row)
            print(
                f"  {strategy:<14} recall@1={row['recall_at_1']:.3f} "
                f"recall@5={row['recall_at_5']:.3f} "
                f"mrr@10={row['mrr_at_10']:.3f} "
                f"ndcg@10={row['ndcg_at_10']:.3f} "
                f"latency={row['avg_latency_ms']:.1f}ms"
            )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved {len(rows)} rows to {output_path}")
    return rows


if __name__ == "__main__":
    run_benchmark()
