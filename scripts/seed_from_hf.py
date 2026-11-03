"""Seed Qdrant from a HuggingFace Hub snapshot (parquet → bulk upsert).

Run inside the API container with Docker Compose; see LOCAL_SETUP.md.

The snapshot format is the one produced by scripts/export_to_hf.py:
chunks.parquet + manifest.json. Restore reads only chunks.parquet; the
manifest is used to verify the dense_dim and distance metric match what
the consumer's Qdrant is configured for.

Bulk upsert in batches of 256. ~1000 points/s on a local dev Qdrant —
68k chunks restore in ~70s after the parquet download.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from src.config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _download_snapshot(slug: str, revision: str | None) -> Path:
    """Download chunks.parquet + manifest.json from the HF dataset.
    Returns the snapshot directory."""
    from huggingface_hub import snapshot_download  # noqa: PLC0415

    logger.info("Downloading snapshot from HF: %s (revision=%s)", slug, revision or "main")
    t0 = time.monotonic()
    local_dir = snapshot_download(
        repo_id=slug,
        repo_type="dataset",
        revision=revision,
        allow_patterns=["chunks.parquet", "manifest.json"],
    )
    elapsed = time.monotonic() - t0
    logger.info("Downloaded in %.1fs → %s", elapsed, local_dir)
    return Path(local_dir)


def _verify_manifest(manifest_path: Path, dense_dim_expected: int) -> dict:
    """Load + sanity-check the manifest. Errors out on a config mismatch
    (e.g. snapshot uses a different embedding dim than the consumer pipeline)."""
    if not manifest_path.exists():
        raise ValueError("Snapshot manifest is required; collection left unchanged.")
    manifest = json.loads(manifest_path.read_text())
    dense = manifest.get("dense_embedding") or {}
    model = str(dense.get("model", "")).removeprefix("voyage/")
    if (dense.get("dim") != dense_dim_expected or model != settings.EMBEDDING_MODEL
            or str(dense.get("distance", "")).lower() != "cosine"):
        raise ValueError("Snapshot embedding model, dimensions, or distance do not match this app; collection left unchanged.")
    provider = str(dense.get("provider", "")).lower().replace(" ", "")
    if provider not in {settings.EMBEDDING_PROVIDER.lower(), settings.EMBEDDING_PROVIDER.lower() + "ai"}:
        raise ValueError("Snapshot embedding provider does not match this app.")
    if (manifest.get("sparse_embedding") or {}).get("encoder") != "qdrant-fastembed-bm25":
        raise ValueError("Snapshot must contain compatible BM25 sparse vectors.")
    logger.info("Verified snapshot model=%s, dim=%d", model, dense_dim_expected)
    return manifest


def _restore(parquet_path: Path, collection: str, batch_size: int, replace: bool = False) -> int:
    """Create the collection (replacing any existing one) and bulk-upsert
    all rows from the parquet."""
    import pyarrow.parquet as pq  # noqa: PLC0415
    import pyarrow.compute as pc
    from qdrant_client import QdrantClient  # noqa: PLC0415
    from qdrant_client.http import models as qm  # noqa: PLC0415

    client = QdrantClient(
        host=settings.QDRANT_HOST,
        port=settings.QDRANT_PORT,
        timeout=120,
    )

    # Inspect the file before replacing any existing data.
    t = pq.read_table(parquet_path)
    required = {"chunk_id", "content", "dense_vector", "sparse_indices", "sparse_values"}
    if not required.issubset(t.column_names):
        raise ValueError("Snapshot is missing required text/vector fields; collection left unchanged.")
    if len(t) == 0:
        raise ValueError("Snapshot is empty; collection left unchanged.")
    lengths = pc.list_value_length(t.column("dense_vector"))
    if lengths.null_count or not pc.all(pc.equal(lengths, settings.EMBEDDING_DIMENSIONS)).as_py():
        raise ValueError("Snapshot contains incompatible vectors; collection left unchanged.")
    if client.collection_exists(collection):
        if not replace:
            raise ValueError("Collection already exists. Use --replace only if you intend to replace it.")
        client.delete_collection(collection)

    client.create_collection(
        collection_name=collection,
        vectors_config={"dense": qm.VectorParams(size=settings.EMBEDDING_DIMENSIONS, distance=qm.Distance.COSINE)},
        sparse_vectors_config={"sparse": qm.SparseVectorParams()},
    )
    logger.info("Created collection '%s' (dense 1024 cosine + sparse BM25)", collection)

    total = t.num_rows
    logger.info("Restoring %d points in batches of %d ...", total, batch_size)

    t0 = time.monotonic()
    upserted = 0
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        batch = t.slice(start, end - start).to_pylist()
        points = []
        for row in batch:
            sparse_vec = qm.SparseVector(
                indices=row["sparse_indices"],
                values=row["sparse_values"],
            ) if row["sparse_indices"] else None
            vectors: dict = {"dense": row["dense_vector"]}
            if sparse_vec is not None:
                vectors["sparse"] = sparse_vec
            payload = {
                k: v for k, v in row.items()
                if k not in {"chunk_id", "dense_vector", "sparse_indices", "sparse_values"}
                and v is not None
            }
            points.append(
                qm.PointStruct(id=row["chunk_id"], vector=vectors, payload=payload)
            )
        client.upsert(collection_name=collection, points=points, wait=True)
        upserted += len(points)
        if upserted % 5000 < batch_size:
            elapsed = time.monotonic() - t0
            rate = upserted / elapsed if elapsed > 0 else 0
            logger.info("  upserted %d/%d (%.0f points/s)", upserted, total, rate)

    elapsed = time.monotonic() - t0
    logger.info("Restore complete: %d points in %.1fs", upserted, elapsed)
    return upserted


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--from-hf",
        dest="slug",
        required=True,
        help="HF dataset slug; see DATA_SOURCES.md for the published snapshot.",
    )
    parser.add_argument(
        "--collection",
        default=None,
        help="Target Qdrant collection name. Defaults to QDRANT_COLLECTION from settings.",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="HF dataset revision (branch/tag/commit). Default: main.",
    )
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--replace", action="store_true", help="Explicitly replace an existing collection.")
    args = parser.parse_args()

    collection = args.collection or settings.QDRANT_COLLECTION
    logger.info("Target collection: %s", collection)

    snapshot_dir = _download_snapshot(args.slug, args.revision)
    _verify_manifest(snapshot_dir / "manifest.json", dense_dim_expected=settings.EMBEDDING_DIMENSIONS)

    count = _restore(snapshot_dir / "chunks.parquet", collection, args.batch_size, replace=args.replace)
    logger.info("DONE: %d points in '%s'", count, collection)


if __name__ == "__main__":
    main()
