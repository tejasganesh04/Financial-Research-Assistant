"""Measure the MCP search tool on FinanceBench: does the answer page reach the results?

For each question the tool is called the way an assistant would call it: the question as the
query, the company's name, and the fiscal year of its filing, under the analyst role. One search
per question; both result styles are scored from the same ranking:

- chunks: the top N reranked chunks (several may come from one page)
- pages: the top N pages, each grouping its matching chunks (what search_documents returns)

A question counts as found when an evidence page is among the top N. Uses query embeddings and
the configured reranker; no language model is called. Resumable per run.

    docker compose -f compose.minimal.yml -f compose.local.yml exec api \\
      python tests/evaluation/financebench/measure_mcp_search.py --run mcp-search
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from select_questions import load_source  # noqa: E402

from src.mcp_server.knowledge import Caller, RequestError, load_documents, page_passages, rank_candidates  # noqa: E402

RESULTS = Path(__file__).resolve().parents[1] / "results"
CUTOFFS = (5, 8, 12)
CALLER = Caller("evaluation", "FinanceBench evaluation", "analyst")


def evidence_pages(question: dict) -> set[tuple[str, int]]:
    # FinanceBench pages are zero-based; stored pages are one-based.
    return {(e["doc_name"] + ".pdf", int(e["evidence_page_num"]) + 1) for e in question["evidence"]}


def first_chunk_rank(chunks: list[dict], evidence: set) -> int | None:
    for rank, chunk in enumerate(chunks, 1):
        meta = chunk.get("metadata") or {}
        if (meta.get("source_file"), meta.get("page_number")) in evidence:
            return rank
    return None


def first_page_rank(chunks: list[dict], evidence: set, limit: int = 20) -> int | None:
    for rank, group in enumerate(page_passages(chunks, limit), 1):
        meta = group[0].get("metadata") or {}
        if (meta.get("source_file"), meta.get("page_number")) in evidence:
            return rank
    return None


def measure(question: dict, years_by_doc: dict[str, int]) -> dict:
    row = {"case_id": question["financebench_id"], "question_type": question["question_type"]}
    year = years_by_doc.get(question["doc_name"])
    if year is None:
        return {**row, "status": "document_not_indexed"}
    started = time.monotonic()
    try:
        found = rank_candidates(CALLER, question["question"], [question["company"]], [year])
    except RequestError as error:
        return {**row, "status": "rejected", "reason": str(error)}
    evidence = evidence_pages(question)
    return {
        **row,
        "status": "measured",
        "seconds": round(time.monotonic() - started, 2),
        "candidates": len(found.chunks),
        "chunk_rank": first_chunk_rank(found.chunks, evidence),
        "page_rank": first_page_rank(found.chunks, evidence),
    }


def summarize(rows: list[dict]) -> str:
    measured = [r for r in rows if r["status"] == "measured"]
    others = [r for r in rows if r["status"] != "measured"]
    lines = [f"{len(measured)} questions measured; {len(others)} not measured.", ""]
    lines += ["| Results | " + " | ".join(f"Top {k}" for k in CUTOFFS) + " |", "| --- |" + " --- |" * len(CUTOFFS)]
    for style, key in (("chunks", "chunk_rank"), ("pages (returned)", "page_rank")):
        lines.append(
            f"| {style} | " + " | ".join(str(sum((r[key] or 999) <= k for r in measured)) for k in CUTOFFS) + " |"
        )
    by_type = defaultdict(list)
    for r in measured:
        by_type[r["question_type"]].append(r)
    lines += ["", "| Question type | Pages top 8 | Chunks top 8 |", "| --- | --- | --- |"]
    for kind, group in sorted(by_type.items()):
        pages = sum((r["page_rank"] or 999) <= 8 for r in group)
        chunks = sum((r["chunk_rank"] or 999) <= 8 for r in group)
        lines.append(f"| {kind} | {pages}/{len(group)} | {chunks}/{len(group)} |")
    seconds = sorted(r["seconds"] for r in measured)
    if seconds:
        lines += ["", f"Median search time: {seconds[len(seconds) // 2]:.1f} s"]
    missed = [r["case_id"] for r in measured if (r["page_rank"] or 999) > 8]
    lines += ["", "Answer page not in the top 8 pages: " + (", ".join(missed) or "none")]
    for r in others:
        lines.append(f"- {r['case_id']}: {r['status']} {r.get('reason', '')}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    out_dir = RESULTS / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "mcp_search.jsonl"
    done = {json.loads(line)["case_id"] for line in out_path.read_text().splitlines()} if out_path.exists() else set()
    questions = [q for q in load_source() if q["financebench_id"] not in done][: args.limit]
    years_by_doc = {d.name: d.fiscal_year for d in load_documents()}
    print(f"{len(done)} already measured; measuring {len(questions)} now.", flush=True)
    with out_path.open("a") as output:
        for index, question in enumerate(questions, 1):
            row = measure(question, years_by_doc)
            output.write(json.dumps(row) + "\n")
            output.flush()
            print(
                f"[{index}/{len(questions)}] {row['case_id']} {row['status']} page_rank={row.get('page_rank')}",
                flush=True,
            )
    rows = [json.loads(line) for line in out_path.read_text().splitlines()]
    report = summarize(rows)
    (out_dir / "mcp_search_summary.md").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
