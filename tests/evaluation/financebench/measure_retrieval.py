"""Measure search alone on FinanceBench: does the answer page reach the reranked results?

For each question, two filters are tried: the company and fiscal year of the known
source filing, and the company alone. Under each, the app's dense and keyword
searches run once; the reranker scores every candidate once; then several
candidate pools are compared:

- fused50: dense and keyword lists merged with RRF and cut to 50 (the app's original pool)
- union30: top 30 from each search, merged without a cut
- union50: top 50 from each search, merged without a cut

A question counts as found when a chunk from an evidence page is among the top
8 or top 12 reranked chunks. No language-model calls are made; only query
embeddings and the local reranker run. Resumable per run.

    docker compose -f compose.minimal.yml -f compose.local.yml run --rm --no-deps \\
      -v ./tests/evaluation:/app/tests/evaluation api \\
      python tests/evaluation/financebench/measure_retrieval.py --run search-configs
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

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
FILTERS = ("filing_year", "company_only")
POOLS = ("fused50", "union30", "union50")
CUTOFFS = (8, 12)


def filing_metadata(client, source_file: str) -> dict | None:
    points = client.scroll(
        settings.QDRANT_COLLECTION,
        scroll_filter={"must": [{"key": "source_file", "match": {"value": source_file}}]},
        limit=1,
        with_payload=["company", "fiscal_year"],
    )[0]
    return points[0].payload if points else None


def key(meta: dict) -> tuple:
    return meta.get("source_file"), meta.get("chunk_index")


def arm(client, search_filter, query, using: str, limit: int = 50) -> list[dict]:
    points = client.query_points(
        settings.QDRANT_COLLECTION, query=query, using=using, query_filter=search_filter, limit=limit, with_payload=True
    ).points
    return [{"content": p.payload.get("content", ""), "metadata": p.payload} for p in points]


def first_rank(chunks: list[dict], evidence: set) -> int | None:
    for rank, chunk in enumerate(chunks, 1):
        if (chunk["metadata"].get("source_file"), chunk["metadata"].get("page_number")) in evidence:
            return rank
    return None


def measure(question: dict, client, reranker) -> dict:
    # FinanceBench pages are zero-based; stored chunk pages are one-based.
    evidence = {(e["doc_name"] + ".pdf", e["evidence_page_num"] + 1) for e in question["evidence"]}
    filings = [filing_metadata(client, source) for source, _ in evidence]
    row = {"case_id": question["financebench_id"], "question_type": question["question_type"]}
    if any(meta is None for meta in filings):
        return {**row, "status": "filing_not_indexed"}
    companies = sorted({meta["company"] for meta in filings})
    years = sorted({meta["fiscal_year"] for meta in filings})
    text = question["question"]
    dense_query, sparse_query = embed_text(text), compute_query_sparse_vector(text)
    scores: dict[tuple, float] = {}
    for filter_name in FILTERS:
        search_filter = build_retrieval_filter(
            ["10k", "10q", "8k"], ["public"], companies, years if filter_name == "filing_year" else []
        )
        dense, sparse = (
            arm(client, search_filter, dense_query, "dense"),
            arm(client, search_filter, sparse_query, "sparse"),
        )
        pools = {"fused50": hybrid_search(client, text, dense_query, search_filter, top_k=50)}
        for size in (30, 50):
            merged = {}
            for chunk in dense[:size] + sparse[:size]:
                merged.setdefault(key(chunk["metadata"]), chunk)
            pools[f"union{size}"] = list(merged.values())
        unscored = {key(c["metadata"]): c for pool in pools.values() for c in pool if key(c["metadata"]) not in scores}
        if unscored:
            predicted = reranker.predict([(text, c["content"]) for c in unscored.values()])
            scores.update(dict(zip(unscored, map(float, predicted))))
        for pool_name, pool in pools.items():
            ranked = sorted(pool, key=lambda c: scores[key(c["metadata"])], reverse=True)
            row[f"{filter_name}/{pool_name}"] = {
                "pool_size": len(pool),
                "in_pool": first_rank(pool, evidence) is not None,
                "rerank_rank": first_rank(ranked, evidence),
            }
    return {**row, "status": "measured"}


def summarize(rows: list[dict]) -> str:
    measured = [r for r in rows if r["status"] == "measured"]
    lines = [f"{len(measured)} questions measured.", ""]
    lines += ["| Filter | Pool | Avg pool size | In pool | Top 8 | Top 12 |", "| --- | --- | --- | --- | --- | --- |"]
    for f in FILTERS:
        for p in POOLS:
            cells = [r[f"{f}/{p}"] for r in measured]
            size = sum(c["pool_size"] for c in cells) / len(cells)
            in_pool = sum(c["in_pool"] for c in cells)
            tops = [sum((c["rerank_rank"] or 999) <= k for c in cells) for k in CUTOFFS]
            lines.append(f"| {f} | {p} | {size:.0f} | {in_pool} | {tops[0]} | {tops[1]} |")
    by_type = defaultdict(list)
    for r in measured:
        by_type[r["question_type"]].append(r)
    lines += [
        "",
        "Top 12 by question type:",
        "",
        "| Question type | " + " | ".join(f"{f}/{p}" for f in FILTERS for p in POOLS) + " |",
    ]
    lines.append("| --- |" + " --- |" * (len(FILTERS) * len(POOLS)))
    for kind, group in sorted(by_type.items()):
        cells = [
            f"{sum((r[f'{f}/{p}']['rerank_rank'] or 999) <= 12 for r in group)}/{len(group)}"
            for f in FILTERS
            for p in POOLS
        ]
        lines.append(f"| {kind} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    out_dir = RESULTS / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "retrieval.jsonl"
    done = {json.loads(line)["case_id"] for line in out_path.read_text().splitlines()} if out_path.exists() else set()
    questions = [q for q in load_source() if q["financebench_id"] not in done][: args.limit]
    print(f"{len(done)} already measured; measuring {len(questions)} now.", flush=True)
    client, reranker = get_qdrant_client(), get_reranker()
    with out_path.open("a") as output:
        for index, question in enumerate(questions, 1):
            row = measure(question, client, reranker)
            output.write(json.dumps(row) + "\n")
            output.flush()
            print(f"[{index}/{len(questions)}] {row['case_id']} {row['status']}", flush=True)
    rows = [json.loads(line) for line in out_path.read_text().splitlines()]
    report = summarize(rows)
    (out_dir / "retrieval_summary.md").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
