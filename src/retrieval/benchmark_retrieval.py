"""Compatibility entry point for the Week 2 retrieval benchmark.

The retrieval sweep that this module originally implemented now lives in
``src/eval_retrieval.py``, which uses the current ``HybridRetriever`` API and
writes ``results/retrieval_experiment.csv`` / ``results/week2_retrieval_recall.csv``.
"""

from src.eval_retrieval import main


def run_benchmark() -> None:
    main()


if __name__ == "__main__":
    run_benchmark()
