"""Build a copy of the knowledge base whose chunks carry their document context.

Filing chunks lose their context when split: most table chunks do not say which
company, filing, statement or unit they belong to. This script reads the
existing collection in reading order and gives every chunk a header built from
the document itself:

    [AMD · 10-K FY2022 · Page 56 · Consolidated Balance Sheets · in millions]

Company, filing type and period come from the chunk metadata. Section titles
(Item headings), statement titles and unit lines are detected in the text and
carried forward to the chunks that follow them. Near-empty chunks such as
"Table of Contents" are dropped. Chunks are re-embedded with the configured
Voyage model and BM25, then written to a new collection; the source collection
is never modified. Resumable per filing.

Run inside the API container:

    python scripts/build_contextual_collection.py --target financial_docs_v2
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, ".")

from qdrant_client.http import models as qm  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.services.vector_store import (  # noqa: E402
    PAYLOAD_INDEXES,
    SPARSE_VECTOR_PARAMS,
    compute_sparse_vectors,
    create_payload_indexes,
    get_qdrant_client,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

S = r"[\s\xa0]+"
STATEMENT = re.compile(
    rf"consolidated{S}(balance{S}sheets?|statements?{S}of{S}(?:financial{S}(?:position|condition)|operations|income"
    rf"|earnings|comprehensive{S}(?:income|loss)|cash{S}?flows|(?:changes{S}in{S})?(?:share|stock)holders.?{S}equity"
    rf"|equity))",
    re.I,
)
# A statement title is a heading only when a units or period line follows it, as in real statement headers.
HEADER_CUE = re.compile(
    rf"in{S}(?:millions|thousands|billions)|years?{S}ended|quarters?{S}ended|months{S}ended|as{S}of"
    rf"|(?:january|february|march|april|may|june|july|august|september|october|november|december){S}\d",
    re.I,
)
UNITS = re.compile(rf"in{S}(millions|thousands|billions)", re.I)
ITEM = re.compile(rf"(?:^|\n|\]\s*)(?:part{S}i+{S})?item{S}(\d{{1,2}}[ab]?)\.?{S}[–—:-]?\s*([A-Z][^\n.]{{3,60}})", re.I)
PREFIX = re.compile(r"^\s*\[[^\]]*\]\s*")
STATEMENT_PAGES = 1  # a statement heading labels its page and the page after it


def document_label(source_file: str) -> str:
    stem = source_file.rsplit(".", 1)[0]
    if m := re.search(r"_(\d{4})_10K$", stem):
        return f"10-K FY{m.group(1)}"
    if m := re.search(r"_(\d{4})Q(\d)_10Q$", stem):
        return f"10-Q Q{m.group(2)} FY{m.group(1)}"
    if m := re.search(r"_(\d{4})Q(\d)_EARNINGS$", stem):
        return f"Earnings release Q{m.group(2)} FY{m.group(1)}"
    if m := re.search(r"_8K_dated[-_](\d{4}-\d{2}-\d{2})$", stem):
        return f"8-K dated {m.group(1)}"
    return stem.replace("_", " ")


# Only well-known sections are labelled. Other Item headings (for example Item 9 or 16) often sit just
# before the financial statement notes, so carrying their titles forward would mislabel those pages.
SECTION_NAMES = [
    ("management", "MD&A"),
    ("risk factors", "Risk Factors"),
    ("quantitative and qualitative", "Market Risk"),
    ("financial statements", "Financial Statements"),
    ("legal proceedings", "Legal Proceedings"),
    ("selected financial data", "Selected Financial Data"),
    ("market for", "Market for Common Equity"),
    ("properties", "Properties"),
    ("business", "Business"),
]


def section_name(title: str) -> str | None:
    """Short, standard name for a known Item heading; None for any other heading."""
    flat = re.sub(S, " ", title).strip().lower()
    return next((name for needle, name in SECTION_NAMES if flat.startswith(needle) or f" {needle}" in f" {flat}"), None)


def body(content: str) -> str:
    return PREFIX.sub("", content, count=1)


def is_junk(text: str) -> bool:
    """Near-empty chunks such as 'Table of Contents', 'Index' or 'Exhibit 10.1'."""
    return len(text.strip()) < 80 and not re.search(r"\d[\d,]{3,}", text)


def statement_title(match: re.Match) -> str:
    return re.sub(S, " ", match.group(0)).strip().title().replace("’S", "’s").replace("'S", "'s")


def contextualize(chunks: list[dict]) -> list[tuple[dict, str]]:
    """Return (payload, new content) for one filing's chunks, in reading order."""
    section = statement = units = None
    statement_page = None
    page_units: dict[int, str] = {}
    out = []
    for payload in sorted(chunks, key=lambda p: p["chunk_index"]):
        text = body(payload["content"])
        page = payload.get("page_number")
        items = ITEM.findall(text)
        if len(items) == 1:  # several Item headings in one chunk is a table of contents
            section = section_name(items[0][1])
            statement = statement_page = None
        titles = list(STATEMENT.finditer(text))
        heading = next((m for m in titles if HEADER_CUE.search(text[m.end() : m.end() + 220])), None)
        if heading and len(titles) <= 2:
            statement, statement_page = statement_title(heading), page
            section = "Financial Statements"
            found = UNITS.search(text[heading.end() : heading.end() + 220])
            units = f"in {found.group(1).lower()}" if found else None
        elif statement_page is not None and page is not None and page > statement_page + STATEMENT_PAGES:
            statement = statement_page = units = None
        if (found := UNITS.search(text)) and page is not None:
            page_units.setdefault(page, f"in {found.group(1).lower()}")
        if is_junk(text):
            continue
        parts = [payload.get("fb_company") or payload.get("company", ""), document_label(payload["source_file"])]
        if page is not None:
            parts.append(f"Page {page}")
        if section:
            parts.append(section)
        if statement:
            parts.append(statement)
        unit = units if statement else page_units.get(page)
        if unit:
            parts.append(unit)
        header = "[" + " · ".join(p for p in parts if p) + "]"
        new_payload = {
            **payload,
            "content": f"{header} {text}",
            "section": section,
            "statement": statement,
            "units": unit,
        }
        out.append((new_payload, new_payload["content"]))
    return out


def embed_documents(client, texts: list[str], used: dict, cap: int) -> list[list[float]]:
    vectors = []
    for start in range(0, len(texts), 64):
        batch = texts[start : start + 64]
        for attempt in range(8):
            try:
                response = client.embed(texts=batch, model=settings.EMBEDDING_MODEL, input_type="document")
                break
            except Exception as error:  # noqa: BLE001 - rate limits and transient errors back off and retry
                wait = min(60, 5 * 2**attempt)
                logger.warning("Voyage error (%s); retrying in %ss", type(error).__name__, wait)
                time.sleep(wait)
        else:
            raise RuntimeError("Voyage embedding kept failing; stopping without partial writes for this filing")
        used["tokens"] += response.total_tokens
        if used["tokens"] > cap:
            raise RuntimeError(f"Token cap reached ({used['tokens']:,} > {cap:,}); stopping")
        vectors += response.embeddings
    return vectors


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=settings.QDRANT_COLLECTION)
    parser.add_argument("--target", required=True)
    parser.add_argument("--token-cap", type=int, default=30_000_000, help="Stop before using more Voyage tokens")
    parser.add_argument("--limit-files", type=int, default=None)
    args = parser.parse_args()
    if args.target == args.source:
        raise SystemExit("Target must differ from the source collection")

    import voyageai

    qdrant = get_qdrant_client()
    voyage = voyageai.Client(api_key=settings.VOYAGE_API_KEY, max_retries=0, timeout=120)
    if not qdrant.collection_exists(args.target):
        qdrant.create_collection(
            collection_name=args.target,
            vectors_config={"dense": qm.VectorParams(size=settings.EMBEDDING_DIMENSIONS, distance=qm.Distance.COSINE)},
            sparse_vectors_config={"sparse": SPARSE_VECTOR_PARAMS},
        )
        create_payload_indexes(qdrant, args.target)
        logger.info("Created %s with indexes on %s", args.target, ", ".join(PAYLOAD_INDEXES))

    by_file: dict[str, list[dict]] = defaultdict(list)
    offset = None
    while True:
        points, offset = qdrant.scroll(args.source, limit=2000, offset=offset, with_payload=True, with_vectors=False)
        for point in points:
            if point.payload.get("content") and point.payload.get("source_file"):
                by_file[point.payload["source_file"]].append({**point.payload, "_id": point.id})
        if offset is None:
            break
    files = sorted(by_file)[: args.limit_files]
    logger.info("Read %d filings, %d chunks from %s", len(files), sum(len(by_file[f]) for f in files), args.source)

    used = {"tokens": 0}
    kept_total = dropped_total = 0
    for index, source_file in enumerate(files, 1):
        rows = contextualize(by_file[source_file])
        done = qdrant.count(
            args.target,
            count_filter=qm.Filter(must=[qm.FieldCondition(key="source_file", match=qm.MatchValue(value=source_file))]),
            exact=True,
        ).count
        if done == len(rows):
            logger.info("[%d/%d] %s already built (%d chunks)", index, len(files), source_file, done)
            continue
        texts = [content for _, content in rows]
        dense = embed_documents(voyage, texts, used, args.token_cap)
        sparse = compute_sparse_vectors(texts)
        points = [
            qm.PointStruct(
                id=payload["_id"],
                vector={"dense": vector, "sparse": sparse_vector},
                payload={k: v for k, v in payload.items() if k != "_id"},
            )
            for (payload, _), vector, sparse_vector in zip(rows, dense, sparse)
        ]
        for start in range(0, len(points), 256):
            qdrant.upsert(args.target, points=points[start : start + 256], wait=True)
        dropped = len(by_file[source_file]) - len(rows)
        kept_total += len(rows)
        dropped_total += dropped
        logger.info(
            "[%d/%d] %s: %d chunks written, %d near-empty dropped, %s Voyage tokens so far",
            index,
            len(files),
            source_file,
            len(rows),
            dropped,
            f"{used['tokens']:,}",
        )
    logger.info(
        "Done: %d chunks written, %d dropped, %s Voyage tokens", kept_total, dropped_total, f"{used['tokens']:,}"
    )


if __name__ == "__main__":
    main()
