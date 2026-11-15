"""Compare search, reranking and selection variants on FinanceBench evidence pages.

Each question is filtered to its source filing's company and fiscal year, and the
app's hybrid search (an RRF pool of 50 by default; --pool changes it) runs with two search texts: the question as
asked, and the question plus statement wording for named financial concepts.
The reranker scores each pool against the question as asked and against the
expanded text. Final selections of 12 chunks are then compared:

- rerank: reranker order alone (the app's selection)
- fusion: reciprocal rank fusion of the reranker rank and the dense-search rank
- protect: top 9 by reranker plus the dense-search top 3

A question counts as found when an evidence-page chunk is among the selected 12
(and among the first 8 of the ordering, for reference). No language-model calls
are made. Resumable per run; ``--collection`` compares knowledge-base versions and
``--reranker voyage`` scores with Voyage rerank-2.5 instead of the local model.

    docker compose -f compose.minimal.yml -f compose.local.yml run --rm --no-deps \\
      -v ./tests/evaluation:/app/tests/evaluation api \\
      python tests/evaluation/financebench/measure_variants.py --run variants
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from measure_retrieval import key  # noqa: E402
from select_questions import load_source  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.services.embeddings import embed_text  # noqa: E402
from src.services.finance_terms import with_statement_terms  # noqa: E402
from src.services.reranker_service import get_reranker  # noqa: E402
from src.services.vector_store import build_retrieval_filter, get_qdrant_client, hybrid_search  # noqa: E402

RESULTS = Path(__file__).resolve().parents[1] / "results"
REWRITES: dict[str, dict] = {}  # case_id -> {"search", "passage"} from generate_rewrites.py, when supplied
SELECTIONS = ("rerank", "fusion", "protect")
FUSION_K = 10
POOL = {"size": 50}  # search pool size; --pool changes it
VOYAGE_RERANKER = "rerank-2.5"  # first 200M tokens free per account
INSTRUCTION = (
    "Instruction: find the passage or financial statement table that contains the figures or statements "
    "needed to answer this question about the named company and period.\nQuery: "
)


class VoyageReranker:
    """Same interface as the local CrossEncoder: predict([(query, document), ...]) -> scores."""

    def __init__(self):
        import voyageai

        self.client = voyageai.Client(api_key=settings.VOYAGE_API_KEY, max_retries=6, timeout=120)

    def predict(self, pairs):
        query, documents = pairs[0][0], [document for _, document in pairs]
        result = self.client.rerank(query, documents, model=VOYAGE_RERANKER, truncation=True)
        scores = [0.0] * len(documents)
        for item in result.results:
            scores[item.index] = item.relevance_score
        return scores


def filing(client, collection: str, source_file: str) -> dict | None:
    points = client.scroll(
        collection,
        scroll_filter={"must": [{"key": "source_file", "match": {"value": source_file}}]},
        limit=1,
        with_payload=["company", "fiscal_year"],
    )[0]
    return points[0].payload if points else None


def select(pool: list[dict], rerank_scores: dict, dense_rank: dict, how: str, size: int = 12) -> list[dict]:
    by_rerank = sorted(pool, key=lambda c: rerank_scores[key(c["metadata"])], reverse=True)
    if how == "rerank":
        return by_rerank[:size]
    if how == "fusion":
        position = {key(c["metadata"]): i for i, c in enumerate(by_rerank, 1)}

        def fused(c):
            k = key(c["metadata"])
            return 1 / (FUSION_K + position[k]) + 1 / (FUSION_K + dense_rank.get(k, 999))

        return sorted(pool, key=fused, reverse=True)[:size]
    protected = sorted(
        (c for c in pool if dense_rank.get(key(c["metadata"]), 999) <= 3), key=lambda c: dense_rank[key(c["metadata"])]
    )
    chosen = {key(c["metadata"]) for c in protected}
    rest = [c for c in by_rerank if key(c["metadata"]) not in chosen][: size - len(protected)]
    return protected + rest


def rank_of(chunks: list[dict], evidence: set) -> int | None:
    return next(
        (
            i
            for i, c in enumerate(chunks, 1)
            if (c["metadata"].get("source_file"), c["metadata"].get("page_number")) in evidence
        ),
        None,
    )


def measure(question: dict, client, reranker, collection: str) -> dict:
    evidence = {(e["doc_name"] + ".pdf", e["evidence_page_num"] + 1) for e in question["evidence"]}
    filings = [filing(client, collection, source) for source, _ in evidence]
    row = {"case_id": question["financebench_id"], "question_type": question["question_type"]}
    if any(meta is None for meta in filings):
        return {**row, "status": "filing_not_indexed"}
    search_filter = build_retrieval_filter(
        ["10k", "10q", "8k"],
        ["public"],
        sorted({m["company"] for m in filings}),
        sorted({m["fiscal_year"] for m in filings}),
    )
    asked = question["question"]
    texts = {"asked": asked, "terms": with_statement_terms(asked)}
    row["has_terms"] = texts["terms"] != asked
    rewrite = REWRITES.get(question["financebench_id"])
    if rewrite:
        texts["rewrite"] = f"{asked}\n{rewrite['search']}"
        texts["hyde"] = rewrite["passage"]
    rerank_texts = [name for name in ("asked", "terms", "rewrite") if name in texts]
    if isinstance(reranker, VoyageReranker):
        texts["instructed"] = INSTRUCTION + texts["terms"]
        rerank_texts.append("instructed")
    pools, dense_ranks = {}, {}
    for name, text in texts.items():
        if name == "instructed":
            continue
        if name == "terms" and not row["has_terms"]:
            pools[name], dense_ranks[name] = pools["asked"], dense_ranks["asked"]
            continue
        vector = embed_text(text)
        pools[name] = hybrid_search(
            client,
            text,
            vector,
            search_filter,
            top_k=POOL["size"],
            fusion_candidates_per_arm=POOL["size"],
            collection=collection,
        )
        dense = client.query_points(
            collection,
            query=vector,
            using="dense",
            query_filter=search_filter,
            limit=POOL["size"],
            with_payload=["source_file", "chunk_index"],
        ).points
        dense_ranks[name] = {
            (p.payload.get("source_file"), p.payload.get("chunk_index")): i for i, p in enumerate(dense, 1)
        }
    if rewrite:
        # Union of the question's pool and the rewrite's pool; a chunk keeps its best dense rank.
        merged = {key(c["metadata"]): c for name in ("asked", "rewrite") for c in pools[name]}
        pools["asked+rewrite"] = list(merged.values())
        dense_ranks["asked+rewrite"] = {
            k: min(dense_ranks["asked"].get(k, 999), dense_ranks["rewrite"].get(k, 999)) for k in merged
        }
    candidates = {key(c["metadata"]): c for pool in pools.values() for c in pool}
    scores = {}
    for name in rerank_texts:
        if name == "terms" and not row["has_terms"]:
            scores[name] = scores["asked"]
            continue
        predicted = reranker.predict([(texts[name], c["content"]) for c in candidates.values()])
        scores[name] = dict(zip(candidates, map(float, predicted)))
    for search_name in pools:
        pool = pools[search_name]
        row[f"pool/{search_name}"] = rank_of(pool, evidence) is not None
        for rerank_name in rerank_texts:
            for how in SELECTIONS:
                chosen = select(pool, scores[rerank_name], dense_ranks[search_name], how)
                row[f"{search_name}/{rerank_name}/{how}"] = rank_of(chosen, evidence)
    return {**row, "status": "measured"}


def summarize(rows: list[dict]) -> str:
    measured = [r for r in rows if r["status"] == "measured"]
    variants = sorted({k for r in measured for k in r if k.count("/") == 2 and not k.startswith("pool/")})
    measured = [r for r in measured if all(v in r for v in variants)]
    groups = {"all": measured, "with concept": [r for r in measured if r["has_terms"]]}
    for r in measured:
        groups.setdefault(r["question_type"], []).append(r)
    lines = [f"{len(measured)} questions measured. Columns: found in the selected 12 (first 8 in brackets).", ""]
    lines.append("| search / rerank / selection | " + " | ".join(f"{g} (n={len(v)})" for g, v in groups.items()) + " |")
    lines.append("| --- |" + " --- |" * len(groups))
    for variant in variants:
        cells = []
        for group in groups.values():
            found12 = sum(r[variant] is not None for r in group)
            found8 = sum((r[variant] or 99) <= 8 for r in group)
            cells.append(f"{found12} ({found8})")
        lines.append(f"| {variant} | " + " | ".join(cells) + " |")
    pools = defaultdict(int)
    for r in measured:
        for name in [k.split("/", 1)[1] for k in r if k.startswith("pool/")]:
            pools[name] += r[f"pool/{name}"]
    lines += ["", f"Evidence in the search pool of {POOL['size']}: " + ", ".join(f"{k} {v}" for k, v in pools.items())]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True)
    parser.add_argument("--collection", default=settings.QDRANT_COLLECTION)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--rewrites", type=Path, default=None, help="rewrites.jsonl from generate_rewrites.py")
    parser.add_argument("--reranker", choices=["local", "voyage"], default="local")
    parser.add_argument("--pool", type=int, default=50, help="Candidates taken from hybrid search")
    args = parser.parse_args()
    POOL["size"] = args.pool
    if args.rewrites:
        REWRITES.update({r["case_id"]: r for r in map(json.loads, args.rewrites.read_text().splitlines())})
    out_dir = RESULTS / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "variants.jsonl"
    done = {json.loads(line)["case_id"] for line in out_path.read_text().splitlines()} if out_path.exists() else set()
    questions = [q for q in load_source() if q["financebench_id"] not in done][: args.limit]
    print(f"{len(done)} already measured; measuring {len(questions)} now on {args.collection}.", flush=True)
    client = get_qdrant_client()
    reranker = VoyageReranker() if args.reranker == "voyage" else get_reranker()
    with out_path.open("a") as output:
        for index, question in enumerate(questions, 1):
            row = measure(question, client, reranker, args.collection)
            output.write(json.dumps(row) + "\n")
            output.flush()
            print(f"[{index}/{len(questions)}] {row['case_id']} {row['status']}", flush=True)
    rows = [json.loads(line) for line in out_path.read_text().splitlines()]
    report = summarize(rows)
    (out_dir / "variants_summary.md").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
