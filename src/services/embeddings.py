"""Dense-embedding service. Dispatches to the configured provider.

Provider is selected via `settings.EMBEDDING_PROVIDER`:
  - "openai" (default): text-embedding-3-{small,large}
  - "voyage": voyage-finance-2 (uses voyageai SDK, input_type for query/doc)
  - "abaci":  AbaciNLP-hosted Fin-E5 (OpenAI-compatible API at
              https://abacinlp.com/v1, queries get an "Instruct: ...\\nQuery:"
              prefix, documents are embedded as-is)

The defaults match the call-site convention: `embed_text` is used for query
embedding at retrieval time, `embed_texts` is used for corpus indexing.

Voyage's SDK does not auto-retry transient errors, so we wrap calls with
exponential backoff for rate limits, 5xx, timeouts, and network drops.
AbaciNLP uses OpenAI's SDK which has built-in retries.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from openai import OpenAI

from src.config.settings import settings
from src.services.provider_errors import raise_if_rate_limited

logger = logging.getLogger(__name__)

_openai_client: OpenAI | None = None
_voyage_client = None  # voyageai.Client, lazy-imported
_abaci_client: OpenAI | None = None

VOYAGE_MAX_RETRIES = 5
VOYAGE_INITIAL_BACKOFF_S = 2.0

ABACI_BASE_URL = "https://abacinlp.com/v1"
ABACI_QUERY_INSTRUCTION = (
    "Given a financial question, retrieve relevant passages that answer the query."
)


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
        record("embedding_call", provider=provider, model=model,
               duration_s=round(time.monotonic() - start, 4), error_type=error_type,
               usage_available=available, input_tokens=tokens if available else None,
               output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
               cost_usd=compute_cost(model, input_tokens=tokens)
               if available and model in MODEL_PRICING else None)


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai_client


def _get_voyage_client():
    global _voyage_client
    if _voyage_client is None:
        import voyageai

        _voyage_client = voyageai.Client(api_key=settings.VOYAGE_API_KEY)
    return _voyage_client


def _get_abaci_client() -> OpenAI:
    global _abaci_client
    if _abaci_client is None:
        _abaci_client = OpenAI(
            api_key=settings.ABACI_NLP_API_KEY,
            base_url=ABACI_BASE_URL,
        )
    return _abaci_client


def _abaci_format_query(text: str) -> str:
    """AbaciNLP retrieval pattern: queries get the Instruct prefix; docs don't."""
    return f"Instruct: {ABACI_QUERY_INSTRUCTION}\nQuery: {text}"


def _abaci_embed(texts: list[str], input_type: str) -> list[list[float]]:
    """Call AbaciNLP via the OpenAI-compatible client.

    For input_type="query" each text is wrapped with the Instruct prefix.
    For input_type="document" texts are passed as-is.
    """
    if input_type == "query":
        formatted = [_abaci_format_query(t) for t in texts]
    else:
        formatted = texts
    with _measure_embedding("abaci") as measurement:
        response = _get_abaci_client().embeddings.create(
            input=formatted, model=settings.EMBEDDING_MODEL
        )
        measurement["response"] = response
    return [item.embedding for item in response.data]


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
                attempt, VOYAGE_MAX_RETRIES, type(err).__name__, backoff,
            )
            time.sleep(backoff)
            backoff *= 2
    raise RuntimeError(
        f"Voyage embed failed after {VOYAGE_MAX_RETRIES} attempts: {last_err}"
    )


def embed_text(text: str, input_type: str = "query") -> list[float]:
    """Embed a search query, reusing its cached vector when available.
    
    Keys include provider, model, input type and exact text. Document embeddings
    are computed directly; the published snapshot already supplies corpus vectors."""
    if input_type == "query":
        from src.services.result_cache import get_or_compute

        return get_or_compute(
            "query-emb",
            (
                settings.EMBEDDING_PROVIDER,
                settings.EMBEDDING_MODEL,
                input_type,
                text,
            ),
            lambda: _embed_one_uncached(text, input_type),
        )
    return _embed_one_uncached(text, input_type)


def _embed_one_uncached(text: str, input_type: str) -> list[float]:
    try:
        if settings.EMBEDDING_PROVIDER == "voyage":
            return _voyage_embed([text], input_type=input_type)[0]
        if settings.EMBEDDING_PROVIDER == "abaci":
            return _abaci_embed([text], input_type=input_type)[0]
        with _measure_embedding("openai") as measurement:
            response = _get_openai_client().embeddings.create(
                input=text, model=settings.EMBEDDING_MODEL
            )
            measurement["response"] = response
        return response.data[0].embedding
    except Exception as error:
        raise_if_rate_limited(error, "retrieval" if input_type == "query" else "embedding",
                             provider=settings.EMBEDDING_PROVIDER.title(), model=settings.EMBEDDING_MODEL)
        raise


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """Embed multiple texts in a single API call."""
    if settings.EMBEDDING_PROVIDER == "voyage":
        return _voyage_embed(texts, input_type=input_type)
    if settings.EMBEDDING_PROVIDER == "abaci":
        return _abaci_embed(texts, input_type=input_type)
    with _measure_embedding("openai") as measurement:
        response = _get_openai_client().embeddings.create(
            input=texts, model=settings.EMBEDDING_MODEL
        )
        measurement["response"] = response
    return [item.embedding for item in response.data]
