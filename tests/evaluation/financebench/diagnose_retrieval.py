"""Explain each search miss from a measure_retrieval.py run.

For every question whose answer page missed the reranked top 8, record where
it was lost and why: whether the evidence numbers appear on the stored page,
the answer chunk's rank in dense and keyword search separately, its reranker
score against the cut-off, what the reranker preferred instead, and simple
chunk features. No language-model calls are made.

    docker compose -f compose.minimal.yml -f compose.local.yml run --rm --no-deps \\
      -v ./tests/evaluation:/app/tests/evaluation api \\
      python tests/evaluation/financebench/diagnose_retrieval.py --run search-baseline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from measure_retrieval import filing_metadata  # noqa: E402
from select_questions import load_source  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.services.embeddings import embed_text  # noqa: E402
from src.services.reranker_service import get_reranker  # noqa: E402
from src.services.vector_store import (  # noqa: E402
    build_retrieval_filter,
    compute_query_sparse_vector,
    get_qdrant_client,
    hybrid_search,
)

RESULTS = Path(__file__).resolve().parents[1] / "results"
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+")


def snippet(text: str, width: int = 280) -> str:
    return re.sub(r"\s+", " ", text)[:width]


def digit_share(text: str) -> float:
    return round(sum(ch.isdigit() for ch in text) / max(len(text), 1), 3)


def scope_points(client, search_filter) -> list:
    points, offset = [], None
    while True:
        batch, offset = client.scroll(
            settings.QDRANT_COLLECTION,
            scroll_filter=search_filter,
            limit=1000,
            offset=offset,
            with_payload=["content", "source_file", "page_number", "chunk_index"],
        )
        points += batch
        if offset is None:
            return points


def arm_rank(client, search_filter, query, using: str, evidence_ids: set, total: int) -> int | None:
    hits = client.query_points(
        settings.QDRANT_COLLECTION, query=query, using=using, query_filter=search_filter, limit=total
    ).points
    return next((rank for rank, point in enumerate(hits, 1) if point.id in evidence_ids), None)


def diagnose(question: dict, baseline: dict, client, reranker) -> dict:
    evidence_pages = {(e["doc_name"] + ".pdf", e["evidence_page_num"] + 1) for e in question["evidence"]}
    filings = [filing_metadata(client, source) for source, _ in evidence_pages]
    companies = sorted({f["company"] for f in filings})
    years = sorted({f["fiscal_year"] for f in filings})
    search_filter = build_retrieval_filter(["10k", "10q", "8k"], ["public"], companies, years)
    points = scope_points(client, search_filter)
    by_page: dict[tuple, list] = {}
    for point in points:
        by_page.setdefault((point.payload["source_file"], point.payload["page_number"]), []).append(point)
    evidence_chunks = [p for page in evidence_pages for p in by_page.get(page, [])]

    # Do the numbers in FinanceBench's evidence appear on the stored page, or on a neighbouring one?
    numbers = {n for e in question["evidence"] for n in NUMBER.findall(e["evidence_text"])}

    def number_hits(chunks):
        text = " ".join(c.payload["content"] for c in chunks)
        return sum(n in text for n in numbers)

    neighbours = [p for (src, page) in evidence_pages for d in (-1, 1) for p in by_page.get((src, page + d), [])]
    row = {
        "case_id": question["financebench_id"],
        "question_type": question["question_type"],
        "question": question["question"],
        "lost_at": "search" if baseline["as_asked"]["search_rank"] is None else "rerank",
        "baseline_rerank_rank": baseline["as_asked"]["rerank_rank"],
        "scope_chunks": len(points),
        "evidence_chunks": len(evidence_chunks),
        "evidence_numbers": len(numbers),
        "numbers_on_page": number_hits(evidence_chunks),
        "numbers_on_neighbour_pages": number_hits(neighbours),
    }
    if not evidence_chunks:
        return {**row, "diagnosis_note": "evidence page has no stored chunks"}

    evidence_ids = {p.id for p in evidence_chunks}
    text = question["question"]
    row["dense_rank"] = arm_rank(client, search_filter, embed_text(text), "dense", evidence_ids, len(points))
    row["sparse_rank"] = arm_rank(
        client, search_filter, compute_query_sparse_vector(text), "sparse", evidence_ids, len(points)
    )
    candidates = hybrid_search(client, text, embed_text(text), search_filter, top_k=settings.RETRIEVAL_TOP_K)
    scored = sorted(
        zip(reranker.predict([(text, c["content"]) for c in candidates]), candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    evidence_scores = reranker.predict([(text, p.payload["content"]) for p in evidence_chunks])
    best = max(range(len(evidence_chunks)), key=lambda i: evidence_scores[i])
    best_chunk = evidence_chunks[best].payload["content"]
    top_score, top_chunk = scored[0]
    return {
        **row,
        "evidence_best_rerank_score": round(float(evidence_scores[best]), 3),
        "rerank_cutoff_score": round(float(scored[min(7, len(scored) - 1)][0]), 3),
        "evidence_chunk_chars": len(best_chunk),
        "evidence_chunk_digit_share": digit_share(best_chunk),
        "evidence_chunk": snippet(best_chunk),
        "preferred_chunk_page": top_chunk["metadata"].get("page_number"),
        "preferred_chunk_digit_share": digit_share(top_chunk["content"]),
        "preferred_chunk": snippet(top_chunk["content"]),
        "preferred_score": round(float(top_score), 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    run_dir = RESULTS / args.run
    baseline = {r["case_id"]: r for r in map(json.loads, (run_dir / "retrieval.jsonl").read_text().splitlines())}
    misses = [
        q
        for q in load_source()
        if baseline[q["financebench_id"]]["status"] == "measured"
        and (baseline[q["financebench_id"]]["as_asked"]["rerank_rank"] or 99) > settings.RERANKER_TOP_K
    ]
    print(f"Diagnosing {len(misses)} misses.", flush=True)
    client, reranker = get_qdrant_client(), get_reranker()
    with (run_dir / "diagnosis.jsonl").open("w") as output:
        for index, question in enumerate(misses, 1):
            row = diagnose(question, baseline[question["financebench_id"]], client, reranker)
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            print(f"[{index}/{len(misses)}] {row['case_id']} lost at {row['lost_at']}", flush=True)


if __name__ == "__main__":
    main()
