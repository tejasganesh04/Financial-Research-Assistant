"""RBAC-checked PDF document serving for the frontend citation clickthrough.

Frontend renders source chips on each answer (`{source_file, page}`); clicking
a chip opens the PDF in an in-browser viewer. That viewer needs to fetch the
PDF over HTTP — which means we expose the file but must NOT bypass RBAC.

Design:
  1. Resolve the requested filename to a Qdrant chunk and read its
     `doc_type` + `confidentiality` payload.
  2. Compare against the caller's role permissions; reject if not allowed.
  3. Validate the filename against path traversal (no ``../``, no absolute
     paths, no resolving outside DOCUMENTS_ROOT).
  4. Stream the file with `Content-Disposition: inline` so browsers render
     in-window rather than triggering a download.

If a filename has been ingested but its underlying file is missing on disk
we surface a 404 — Qdrant being the source of truth for "what's indexed"
means RBAC still applies even when the file is gone, but we don't fabricate
a fake response.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from qdrant_client.models import FieldCondition, Filter, MatchValue

from src.api.dependencies import get_current_user
from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.auth import User
from src.services.vector_store import get_qdrant_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

# Document types as people see them, in the order they are listed.
DOC_TYPE_LABELS = {
    "10k": "10-K",
    "10q": "10-Q",
    "8k": "8-K",
    "invoice": "Invoices",
    "expense_policy": "Expense policies",
    "board_report": "Board reports",
}
FILINGS = ("10k", "10q", "8k")
LEVEL_ORDER = ("public", "internal", "confidential")


def _visible(perms: dict, doc_type: str, level: str) -> bool:
    return _is_allowed(perms, {"doc_type": doc_type, "confidentiality": level})


def _requestable(perms: dict, hidden: list) -> list[dict]:
    """Groups of documents this role cannot search, which a person may ask an administrator to open
    for one answer. Each names the access to request; the three SEC filing types go together."""
    groups: dict[str, list] = {}
    for doc in hidden:
        groups.setdefault("filings" if doc.doc_type in FILINGS else doc.doc_type, []).append(doc)
    allowed_types = perms.get("allowed_doc_types") or []
    allowed_levels = perms.get("allowed_confidentiality") or []
    options = []
    for key, docs in groups.items():
        if key not in ("filings", *DOC_TYPE_LABELS):
            continue
        kinds = sorted({d.doc_type for d in docs})
        needs_type = any(k not in allowed_types for k in kinds)
        levels = sorted(
            {d.confidentiality for d in docs if d.confidentiality not in allowed_levels},
            key=lambda v: LEVEL_ORDER.index(v) if v in LEVEL_ORDER else 9,
        )
        if any(v not in LEVEL_ORDER for v in levels):
            continue
        label = "SEC filings" if key == "filings" else DOC_TYPE_LABELS[key]
        detail = "10-K, 10-Q and 8-K reports" if key == "filings" else ""
        if levels:
            detail = ", ".join(filter(None, [detail, " and ".join(levels) + " documents"]))
        options.append(
            {
                "key": key,
                "label": label,
                "detail": detail,
                "documents": len({d.source_file for d in docs}),
                "doc_type": [key] if needs_type else [],
                "confidentiality": levels,
            }
        )
    return sorted(options, key=lambda o: (o["key"] != "filings", o["label"]))


@router.get("")
async def library(user: User = Depends(get_current_user)) -> dict:
    """What the signed-in person's role can search, and what they may ask to have opened for one answer."""
    from src.mcp_server.knowledge import load_documents

    try:
        documents = await run_in_threadpool(load_documents)
    except Exception as error:
        logger.warning("Document list unavailable: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The document list is unavailable right now."
        ) from error
    perms = get_permissions(user.role)
    visible = [d for d in documents if _visible(perms, d.doc_type, d.confidentiality)]
    hidden = [d for d in documents if not _visible(perms, d.doc_type, d.confidentiality)]
    counts = Counter(d.doc_type for d in visible)
    return {
        "documents": len({d.source_file for d in visible}),
        "companies": len({d.company for d in visible}),
        "doc_types": [
            {"type": kind, "label": DOC_TYPE_LABELS[kind], "documents": counts[kind]}
            for kind in DOC_TYPE_LABELS
            if counts[kind]
        ],
        "requestable": [] if user.role == "admin" else _requestable(perms, hidden),
    }


def _lookup_document_meta(filename: str) -> dict | None:
    """Fetch a single chunk for this filename to read doc_type + confidentiality.

    Qdrant `scroll` with a filter is the cheapest way — we only need one
    chunk to read the doc-level metadata (all chunks of one document share
    the same `doc_type` and `confidentiality`).
    """
    client = get_qdrant_client()
    try:
        points, _ = client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            scroll_filter=Filter(must=[FieldCondition(key="source_file", match=MatchValue(value=filename))]),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        logger.warning("Qdrant scroll for %s failed: %s", filename, e)
        return None
    if not points:
        return None
    payload = points[0].payload or {}
    return {
        "doc_type": payload.get("doc_type"),
        "confidentiality": payload.get("confidentiality"),
    }


# Apply the caller's ordinary role permissions to the PDF. Approval releases one answer, not new permanent PDF access.
def _is_allowed(perms: dict, meta: dict) -> bool:
    """Apply the standard RBAC check: doc_type + confidentiality must be in
    the caller's allowed sets. Admin's `["*"]` wildcard short-circuits.
    """
    doc_type = meta.get("doc_type")
    conf = meta.get("confidentiality")
    allowed_types = perms.get("allowed_doc_types") or []
    allowed_conf = perms.get("allowed_confidentiality") or []
    type_ok = "*" in allowed_types or doc_type in allowed_types
    conf_ok = "*" in allowed_conf or conf in allowed_conf
    return type_ok and conf_ok


@router.get("/{filename}")
async def get_document(filename: str, user: User = Depends(get_current_user)):
    # Path traversal guard — the filename must be a plain basename living
    # under DOCUMENTS_ROOT. We reject anything with separators, parent refs,
    # or non-PDF extensions before touching the filesystem.
    if "/" in filename or "\\" in filename or ".." in filename or filename.startswith("."):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This document couldn't be found.")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This document couldn't be found.")

    root = Path(settings.DOCUMENTS_ROOT).resolve()
    target = (root / filename).resolve()
    # Defense-in-depth: even if the filename slipped past the basename check,
    # require the resolved path is inside DOCUMENTS_ROOT.
    if not str(target).startswith(str(root) + "/") and target != root:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This document couldn't be found.")

    # RBAC: read doc metadata from Qdrant + compare to caller's role perms.
    # We do this BEFORE checking the file exists so we don't leak "this
    # filename is indexed" to unauthorized callers via a 404 vs 403.
    meta = _lookup_document_meta(filename)
    if meta is None:
        # Not in the index → file isn't a known document, treat as not found
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This document couldn't be found.")

    perms = get_permissions(user.role)
    if not _is_allowed(perms, meta):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Your role doesn't have access to open this document."
        )

    if not target.exists() or not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="The original document isn't available to open right now."
        )

    return FileResponse(
        path=str(target),
        media_type="application/pdf",
        filename=filename,
        # `inline` so PDF viewer embeds in the page; not as an attachment
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
