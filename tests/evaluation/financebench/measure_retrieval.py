"""Measure search alone on FinanceBench: does the answer page reach the top results?

Each question is filtered to the company and fiscal year of its known source
filing, then searched with the app's hybrid search and reranker. A question
counts as found when a chunk from an evidence page is in the top 50 search
results, and again after reranking to the top 8. No language-model calls are
made; only query embeddings and the local reranker run. Resumable per run.

    docker compose -f compose.minimal.yml -f compose.local.yml run --rm --no-deps \\
      -v ./tests/evaluation:/app/tests/evaluation api \\
      python tests/evaluation/financebench/measure_retrieval.py --run baseline
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
from src.services.company_registry import remove_company_mentions  # noqa: E402
from src.services.embeddings import embed_text  # noqa: E402
from src.services.reranker_service import get_reranker  # noqa: E402
from src.services.vector_store import build_retrieval_filter, get_qdrant_client, hybrid_search  # noqa: E402

RESULTS = Path(__file__).resolve().parents[1] / "results"
# Search-text variants compared in one run; reranking always reads the original question.
VARIANTS = {
    "as_asked": lambda question, company: question,
    "without_company": remove_company_mentions,
}


def filing_metadata(client, source_file: str) -> dict | None:
    points = client.scroll(
        settings.QDRANT_COLLECTION,
        scroll_filter={"must": [{"key": "source_file", "match": {"value": source_file}}]},
        limit=1,
        with_payload=["company", "fiscal_year"],
    )[0]
    return points[0].payload if points else None


def first_rank(chunks: list[dict], evidence: set[tuple[str, int]]) -> int | None:
    for rank, chunk in enumerate(chunks, 1):
        meta = chunk["metadata"]
        if (meta.get("source_file"), meta.get("page_number")) in evidence:
            return rank
    return None


def measure(question: dict, client, reranker) -> dict:
    # FinanceBench pages are zero-based; stored chunk pages are one-based.
    evidence = {(e["doc_name"] + ".pdf", e["evidence_page_num"] + 1) for e in question["evidence"]}
    filings = {source: filing_metadata(client, source) for source, _ in evidence}
    row = {"case_id": question["financebench_id"], "question_type": question["question_type"]}
    if any(meta is None for meta in filings.values()):
        return {**row, "status": "filing_not_indexed"}
    companies = sorted({meta["company"] for meta in filings.values()})
    years = sorted({meta["fiscal_year"] for meta in filings.values()})
    search_filter = build_retrieval_filter(["10k", "10q", "8k"], ["public"], companies, years)
    scores: dict[tuple, float] = {}
    for name, make_text in VARIANTS.items():
        text = make_text(question["question"], companies[0]) if len(companies) == 1 else question["question"]
        chunks = hybrid_search(
            client=client,
            query_text=text,
            query_dense_vector=embed_text(text),
            rbac_filter=search_filter,
            top_k=settings.RETRIEVAL_TOP_K,
        )
        keys = [(c["metadata"].get("source_file"), c["metadata"].get("chunk_index")) for c in chunks]
        new = [(key, c) for key, c in zip(keys, chunks) if key not in scores]
        if new:
            predicted = reranker.predict([(question["question"], c["content"]) for _, c in new])
            scores.update({key: float(score) for (key, _), score in zip(new, predicted)})
        reranked = [c for _, c in sorted(zip(keys, chunks), key=lambda pair: scores[pair[0]], reverse=True)]
        row[name] = {"search_rank": first_rank(chunks, evidence), "rerank_rank": first_rank(reranked, evidence)}
    return {**row, "status": "measured"}


def summarize(rows: list[dict], top_k: int) -> str:
    measured = [r for r in rows if r["status"] == "measured"]
    lines = [f"{len(measured)} questions measured; {len(rows) - len(measured)} skipped (filing not indexed).", ""]
    lines += ["| Search text | Answer page in top 50 | In top 8 after reranking |", "| --- | --- | --- |"]
    for name in VARIANTS:
        in_50 = sum(r[name]["search_rank"] is not None for r in measured)
        in_k = sum((r[name]["rerank_rank"] or 99) <= top_k for r in measured)
        lines.append(f"| {name} | {in_50}/{len(measured)} | {in_k}/{len(measured)} |")
    by_type = defaultdict(list)
    for r in measured:
        by_type[r["question_type"]].append(r)
    lines += ["", "| Question type | " + " | ".join(f"{n} top {top_k}" for n in VARIANTS) + " |"]
    lines.append("| --- |" + " --- |" * len(VARIANTS))
    for kind, group in sorted(by_type.items()):
        cells = [f"{sum((r[n]['rerank_rank'] or 99) <= top_k for r in group)}/{len(group)}" for n in VARIANTS]
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
            ranks = {n: row.get(n, {}).get("rerank_rank") for n in VARIANTS}
            print(f"[{index}/{len(questions)}] {row['case_id']} {row['status']} rerank ranks {ranks}", flush=True)
    rows = [json.loads(line) for line in out_path.read_text().splitlines()]
    report = summarize(rows, settings.RERANKER_TOP_K)
    (out_dir / "retrieval_summary.md").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
