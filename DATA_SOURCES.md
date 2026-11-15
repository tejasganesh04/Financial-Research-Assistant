# Knowledge-base provenance

The `financial_docs` collection was seeded from the public snapshot:

- Dataset: https://huggingface.co/datasets/cmpunkmannu/financebench-voyage-finance-2-embeddings
- Pinned revision: `c31f0efb6eea1f33310eb82fed4d3b940367c21b`
- Snapshot files: `chunks.parquet` and `manifest.json`.
- Manifest counts: 68,059 chunks representing 84 source documents.
- Dense embeddings: Voyage Finance 2, 1024 dimensions, cosine distance.
- Sparse embeddings: Qdrant FastEmbed BM25.
- Source corpus: [FinanceBench](https://github.com/patronus-ai/financebench).
- FinanceBench paper: https://arxiv.org/abs/2311.11944.
- Dataset/snapshot license stated in the manifest: CC BY-NC 4.0.

The code's MIT license does not replace the separate dataset terms. The
published snapshot supplies extracted text, metadata, and precomputed vectors;
it does not supply every source PDF as a locally available download.

The local source manifest and dataset card are retained under
`data/raw/financebench-snapshot/` when present; that downloaded directory is
excluded from Git. This document preserves the essential provenance in the
active repository.

## Contextual collection

The app searches `financial_docs_v3`, built from `financial_docs` by
`scripts/build_contextual_collection.py`. It keeps the snapshot's text and
metadata, adds a header to each chunk (company, filing, page, section,
statement title and units, detected from the document itself), drops near-empty
chunks such as "Table of Contents", and re-embeds the chunks with Voyage
Finance 2 and BM25. The source collection is never modified.

