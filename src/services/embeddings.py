"""Dense embeddings with Voyage (`voyage-finance-2`, the model behind the knowledge base).

`embed_text` embeds a search query.
Voyage's SDK does not retry transient errors, so calls are retried with
exponential backoff on server errors, timeouts and dropped connections.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from src.config.settings import settings
from src.services.provider_errors import raise_if_rate_limited

logger = logging.getLogger(__name__)

_voyage_client = None  # voyageai.Client, imported on first use

VOYAGE_MAX_RETRIES = 5
VOYAGE_INITIAL_BACKOFF_S = 2.0


@contextmanager
def _measure_embedding(provider: str):
    """Observe an SDK call without changing its inputs, result or retry policy."""
    from src.services.cost_tracker import MODEL_PRICING, compute_cost
    from src.services.request_trace import record

    start = time.monotonic()
    measurement = {}
    error_type = None
    try:
        yield measurement
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        response = measurement.get("response")
        usage = getattr(response, "usage", None)
        tokens = getattr(response, "total_tokens", None)
        if tokens is None:
            tokens = getattr(usage, "total_tokens", None)
        available = isinstance(tokens, int)
        model = settings.EMBEDDING_MODEL
        record(
            "embedding_call",
            provider=provider,
            model=model,
            duration_s=round(time.monotonic() - start, 4),
            error_type=error_type,
            usage_available=available,
            input_tokens=tokens if available else None,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=compute_cost(model, input_tokens=tokens) if available and model in MODEL_PRICING else None,
        )


def _get_voyage_client():
    global _voyage_client
    if _voyage_client is None:
        import voyageai

        _voyage_client = voyageai.Client(api_key=settings.VOYAGE_API_KEY)
    return _voyage_client


def _voyage_embed(texts: list[str], input_type: str) -> list[list[float]]:
    """Call Voyage embed with exponential backoff on transient errors.

    Retryable: 5xx, timeout, connection drop. Rate limits surface immediately.
    Non-retryable (re-raised): auth (401), invalid request (400).
    """
    import voyageai.error as ve

    backoff = VOYAGE_INITIAL_BACKOFF_S
    last_err: Exception | None = None
    for attempt in range(1, VOYAGE_MAX_RETRIES + 1):
        try:
            with _measure_embedding("voyage") as measurement:
                response = _get_voyage_client().embed(
                    texts=texts,
                    model=settings.EMBEDDING_MODEL,
                    input_type=input_type,
                )
                measurement["response"] = response
            return response.embeddings
        except (
            ve.RateLimitError,
            ve.ServerError,
            ve.ServiceUnavailableError,
            ve.Timeout,
            ve.TryAgain,
            ve.APIConnectionError,
        ) as err:
            raise_if_rate_limited(err, "retrieval", provider="Voyage", model=settings.EMBEDDING_MODEL)
            last_err = err
            if attempt == VOYAGE_MAX_RETRIES:
                break
            logger.warning(
                "Voyage transient error (attempt %d/%d): %s — retrying in %.1fs",
                attempt,
                VOYAGE_MAX_RETRIES,
                type(err).__name__,
                backoff,
            )
            time.sleep(backoff)
            backoff *= 2
    raise RuntimeError(f"Voyage embed failed after {VOYAGE_MAX_RETRIES} attempts: {last_err}")


def embed_text(text: str, input_type: str = "query") -> list[float]:
    """Embed one text; the knowledge base already holds document vectors, so this embeds queries."""
    try:
        return _voyage_embed([text], input_type=input_type)[0]
    except Exception as error:
        raise_if_rate_limited(
            error,
            "retrieval" if input_type == "query" else "embedding",
            provider="Voyage",
            model=settings.EMBEDDING_MODEL,
        )
        raise
