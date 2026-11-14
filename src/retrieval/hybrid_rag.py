"""Hybrid retrieval: BM25 + BGE dense vectors (Qdrant) with RRF fusion.

Week 2 Task #4 compares lexical, dense, hybrid and cross-encoder reranked
retrieval over the incident corpus. Dense vectors use the small
``BAAI/bge-small-en-v1.5`` encoder (384 dimensions) stored in a local Qdrant
collection; fusion is Reciprocal Rank Fusion (RRF, k=60).
"""

import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from rank_bm25 import BM25Okapi

EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
RERANK_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RRF_K = 60

TOKEN_RE = re.compile(r"[a-z0-9]+")
FLAG_RE = re.compile(r"flagd:([A-Za-z0-9_]+)")

RETRIEVAL_STRATEGIES = ("bm25", "dense", "hybrid", "hybrid_rerank")


@dataclass
class Document:
    doc_id: str
    text: str
    doc_type: str
    service: str
    failure_mode: str = "unknown"
    environment: str = "prod"
    incident_id: str = ""
    start_utc: str = ""


@dataclass
class Chunk:
    chunk_id: int
    doc_id: str
    text: str
    body: str
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class Query:
    query_id: str
    text: str
    gold_doc_id: str
    service: Optional[str] = None
    failure_mode: Optional[str] = None


def tokenize(text: str) -> List[str]:
    return TOKEN_RE.findall(text.lower())


def _bundle_failure_mode(bundle: Dict) -> str:
    for line in bundle.get("lines", []):
        match = FLAG_RE.search(line.get("raw", ""))
        if match:
            return match.group(1)
    return "unknown"


def documents_from_bundles(bundles_dir: str = "data/bundles") -> List[Document]:
    """Fallback corpus: turn the frozen Week 1 incident bundles into documents."""
    documents: List[Document] = []
    for path in sorted(glob.glob(os.path.join(bundles_dir, "bundle_*.json"))):
        with open(path) as handle:
            bundle = json.load(handle)
        service = bundle["affected_services"][0]
        failure_mode = _bundle_failure_mode(bundle)
        incident_id = bundle["bundle_id"]
        start_utc = min(
            (line["timestamp_utc"] for line in bundle.get("lines", []) if "timestamp_utc" in line),
            default="",
        )

        def add(doc_type: str, text: str) -> None:
            if not text:
                return
            documents.append(
                Document(
                    doc_id=f"{incident_id}:{doc_type}",
                    text=text,
                    doc_type=doc_type,
                    service=service,
                    failure_mode=failure_mode,
                    environment="prod",
                    incident_id=incident_id,
                    start_utc=start_utc,
                )
            )

        add("log", "\n".join(l["raw"] for l in bundle["lines"] if l["id"].startswith("LOG:")))
        add("chat", "\n".join(l["raw"] for l in bundle["lines"] if l["id"].startswith("CHAT:")))
        add("runbook", bundle.get("runbook_snippet", ""))
        add("postmortem", bundle.get("past_postmortem_snippet", ""))
    return documents


def documents_from_jsonl(corpus_dir: str = "data/corpus") -> List[Document]:
    documents: List[Document] = []
    for path in sorted(glob.glob(os.path.join(corpus_dir, "**", "*.jsonl"), recursive=True)):
        if os.path.basename(path) == "queries.jsonl":
            continue
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                documents.append(
                    Document(
                        doc_id=record["doc_id"],
                        text=record["text"],
                        doc_type=record.get("doc_type", "runbook"),
                        service=record.get("service", "unknown"),
                        failure_mode=record.get("failure_mode", "unknown"),
                        environment=record.get("environment", "prod"),
                        incident_id=record.get("incident_id", ""),
                        start_utc=record.get("start_utc", ""),
                    )
                )
    return documents


def load_corpus(corpus_dir: str = "data/corpus", bundles_dir: str = "data/bundles") -> List[Document]:
    """Prefer the Week 2 corpus from Tasks #1-#3, else use the incident bundles."""
    corpus_docs = documents_from_jsonl(corpus_dir)
    if corpus_docs:
        return corpus_docs
    return documents_from_bundles(bundles_dir)


def build_queries_from_bundles(documents: Sequence[Document]) -> List[Query]:
    """One query per document type and incident, with the source doc as gold."""
    by_incident: Dict[str, Dict[str, Document]] = {}
    for doc in documents:
        by_incident.setdefault(doc.incident_id, {})[doc.doc_type] = doc

    queries: List[Query] = []
    templates = {
        "log": "At {start}, which log line records the {service} failure with {failure_mode}?",
        "chat": "At {start}, what did the oncall chat thread say during the {service} {failure_mode} incident?",
        "runbook": "At {start}, which runbook step covers {failure_mode} recovery for {service}?",
        "postmortem": "At {start}, find a similar past postmortem (PM) for the {service} {failure_mode} incident.",
    }
    for incident_id in sorted(by_incident):
        for doc_type, doc in sorted(by_incident[incident_id].items()):
            template = templates.get(doc_type)
            if not template:
                continue
            queries.append(
                Query(
                    query_id=f"{incident_id}:{doc_type}",
                    text=template.format(
                        service=doc.service,
                        failure_mode=doc.failure_mode,
                        start=doc.start_utc or "the incident",
                    ),
                    gold_doc_id=doc.doc_id,
                    service=doc.service,
                    failure_mode=doc.failure_mode,
                )
            )
    return queries


def load_queries(
    documents: Sequence[Document],
    corpus_dir: str = "data/corpus",
) -> List[Query]:
    query_path = os.path.join(corpus_dir, "queries.jsonl")
    if os.path.exists(query_path):
        queries = []
        with open(query_path) as handle:
            for line in handle:
                if line.strip():
                    record = json.loads(line)
                    queries.append(
                        Query(
                            query_id=record["query_id"],
                            text=record["text"],
                            gold_doc_id=record["gold_doc_id"],
                            service=record.get("service"),
                            failure_mode=record.get("failure_mode"),
                        )
                    )
        return queries
    if documents and documents[0].incident_id:
        return build_queries_from_bundles(documents)
    raise ValueError(
        "No queries found. Add data/corpus/queries.jsonl or use the bundle fallback."
    )


def _metadata_header(meta: Dict[str, str]) -> str:
    header = (
        f"[service={meta.get('service', 'unknown')} | "
        f"type={meta.get('doc_type', 'unknown')} | "
        f"failure_mode={meta.get('failure_mode', 'unknown')} | "
        f"env={meta.get('environment', 'prod')}"
    )
    if meta.get("start_utc"):
        header += f" | start={meta['start_utc']}"
    return header + "] "


def chunk_documents(
    documents: Sequence[Document],
    mode: str = "section",
    prepend_metadata: bool = True,
) -> List[Chunk]:
    """Chunk at section or step level and optionally prepend metadata headers."""
    chunks: List[Chunk] = []
    for doc in documents:
        meta = {
            "service": doc.service,
            "doc_type": doc.doc_type,
            "failure_mode": doc.failure_mode,
            "environment": doc.environment,
            "incident_id": doc.incident_id,
            "start_utc": doc.start_utc,
        }
        if mode == "step":
            bodies = [line.strip() for line in doc.text.splitlines() if line.strip()]
        else:
            bodies = [doc.text.strip()]
        for body in bodies:
            if not body:
                continue
            text = _metadata_header(meta) + body if prepend_metadata else body
            chunks.append(
                Chunk(
                    chunk_id=len(chunks),
                    doc_id=doc.doc_id,
                    text=text,
                    body=body,
                    metadata=meta,
                )
            )
    return chunks


class HybridRetriever:
    """BM25 + Qdrant dense retrieval with RRF and optional cross-encoder rerank."""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        embed_model: str = EMBED_MODEL_NAME,
        rerank_model: str = RERANK_MODEL_NAME,
    ):
        if not chunks:
            raise ValueError("HybridRetriever requires at least one chunk")
        self.chunks = list(chunks)
        self.embed_model_name = embed_model
        self.rerank_model_name = rerank_model
        self._encoder = None
        self._reranker = None
        self._client = None
        self._collection = "incident_chunks"
        self._tokenized = [tokenize(chunk.text) for chunk in self.chunks]
        self._bm25 = BM25Okapi(self._tokenized)

    @property
    def encoder(self):
        if self._encoder is None:
            from sentence_transformers import SentenceTransformer

            self._encoder = SentenceTransformer(self.embed_model_name, device="cpu")
        return self._encoder

    @property
    def reranker(self):
        if self._reranker is None:
            from sentence_transformers import CrossEncoder

            self._reranker = CrossEncoder(self.rerank_model_name, device="cpu")
        return self._reranker

    @property
    def client(self):
        if self._client is None:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, PointStruct, VectorParams

            self._client = QdrantClient(":memory:")
            dimension = (
                self.encoder.get_embedding_dimension()
                if hasattr(self.encoder, "get_embedding_dimension")
                else self.encoder.get_sentence_embedding_dimension()
            )
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
            )
            vectors = self.encoder.encode(
                [chunk.text for chunk in self.chunks],
                normalize_embeddings=True,
                batch_size=32,
                show_progress_bar=False,
            )
            points = [
                PointStruct(
                    id=chunk.chunk_id,
                    vector=vectors[index].tolist(),
                    payload={
                        "doc_id": chunk.doc_id,
                        "service": chunk.metadata.get("service", ""),
                        "doc_type": chunk.metadata.get("doc_type", ""),
                        "failure_mode": chunk.metadata.get("failure_mode", ""),
                    },
                )
                for index, chunk in enumerate(self.chunks)
            ]
            self._client.upsert(collection_name=self._collection, points=points)
        return self._client

    def _filter_chunks(
        self,
        indices: Iterable[int],
        service: Optional[str],
        failure_mode: Optional[str],
    ) -> List[int]:
        result = list(indices)
        if service:
            result = [i for i in result if self.chunks[i].metadata.get("service") == service]
        if failure_mode:
            result = [
                i for i in result if self.chunks[i].metadata.get("failure_mode") == failure_mode
            ]
        return result

    def bm25_search(
        self,
        query: str,
        top_n: int = 50,
        service: Optional[str] = None,
        failure_mode: Optional[str] = None,
    ) -> List[Tuple[int, float]]:
        scores = self._bm25.get_scores(tokenize(query))
        order = list(np.argsort(-np.asarray(scores), kind="stable"))
        order = self._filter_chunks(order, service, failure_mode)
        positive = [(int(i), float(scores[i])) for i in order if scores[i] > 0]
        if positive:
            return positive[:top_n]
        return [(int(i), float(scores[i])) for i in order[:top_n]]

    def dense_search(
        self,
        query: str,
        top_n: int = 50,
        service: Optional[str] = None,
        failure_mode: Optional[str] = None,
    ) -> List[Tuple[int, float]]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        vector = self.encoder.encode(
            [query], normalize_embeddings=True, show_progress_bar=False
        )[0].tolist()
        conditions = []
        if service:
            conditions.append(
                FieldCondition(key="service", match=MatchValue(value=service))
            )
        if failure_mode:
            conditions.append(
                FieldCondition(key="failure_mode", match=MatchValue(value=failure_mode))
            )
        query_filter = Filter(must=conditions) if conditions else None
        response = self.client.query_points(
            collection_name=self._collection,
            query=vector,
            query_filter=query_filter,
            limit=top_n,
            with_payload=True,
        )
        return [(int(hit.id), float(hit.score)) for hit in response.points]

    def hybrid_search(
        self,
        query: str,
        top_n: int = 50,
        service: Optional[str] = None,
        failure_mode: Optional[str] = None,
    ) -> List[Tuple[int, float]]:
        fused: Dict[int, float] = {}
        candidates = max(top_n * 2, 20)
        for ranked in (
            self.bm25_search(
                query, top_n=candidates, service=service, failure_mode=failure_mode
            ),
            self.dense_search(
                query, top_n=candidates, service=service, failure_mode=failure_mode
            ),
        ):
            for rank, (chunk_id, _) in enumerate(ranked, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        return sorted(fused.items(), key=lambda item: item[1], reverse=True)[:top_n]

    def rerank(
        self, query: str, candidates: Sequence[Tuple[int, float]], top_n: int = 10
    ) -> List[Tuple[int, float]]:
        if not candidates:
            return []
        pairs = [(query, self.chunks[chunk_id].text) for chunk_id, _ in candidates]
        scores = self.reranker.predict(pairs)
        ranked = sorted(
            zip((chunk_id for chunk_id, _ in candidates), scores),
            key=lambda item: item[1],
            reverse=True,
        )
        return [(int(chunk_id), float(score)) for chunk_id, score in ranked[:top_n]]

    def search(
        self,
        query: str,
        strategy: str = "hybrid",
        top_k: int = 10,
        service: Optional[str] = None,
        failure_mode: Optional[str] = None,
        metadata_filter: bool = True,
    ) -> List[Tuple[int, float]]:
        filter_service = service if metadata_filter else None
        filter_failure = failure_mode if metadata_filter else None
        if strategy == "bm25":
            return self.bm25_search(
                query, top_n=top_k, service=filter_service, failure_mode=filter_failure
            )
        if strategy == "dense":
            return self.dense_search(
                query, top_n=top_k, service=filter_service, failure_mode=filter_failure
            )
        if strategy == "hybrid":
            return self.hybrid_search(
                query, top_n=top_k, service=filter_service, failure_mode=filter_failure
            )
        if strategy == "hybrid_rerank":
            candidates = self.hybrid_search(
                query, top_n=20, service=filter_service, failure_mode=filter_failure
            )
            return self.rerank(query, candidates, top_n=top_k)
        raise ValueError(f"Unknown strategy: {strategy}")


def _demo() -> None:
    documents = load_corpus()
    queries = load_queries(documents)
    chunks = chunk_documents(documents, mode="section", prepend_metadata=True)
    print(f"Corpus: {len(documents)} documents -> {len(chunks)} chunks; queries: {len(queries)}")

    retriever = HybridRetriever(chunks)
    sample = queries[0]
    print(f"\nQuery: {sample.text}\nGold:  {sample.gold_doc_id}")
    for strategy in RETRIEVAL_STRATEGIES:
        hits = retriever.search(
            sample.text,
            strategy=strategy,
            top_k=3,
            service=sample.service,
            failure_mode=sample.failure_mode,
        )
        labels = [f"{retriever.chunks[i].doc_id} ({score:.3f})" for i, score in hits]
        print(f"  {strategy:<14} {labels}")


if __name__ == "__main__":
    _demo()
