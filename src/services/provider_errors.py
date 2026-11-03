"""Recognize provider limits and expose a safe explanation instead of a fallback answer."""

import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def _error_chain(error):
    """SDK errors may be wrapped by our search/generation helpers."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def is_rate_limited(error) -> bool:
    return any(
        isinstance(item, ProviderRateLimitError)
        or getattr(item, "status_code", None) == 429
        or getattr(getattr(item, "response", None), "status_code", None) == 429
        or type(item).__name__ == "RateLimitError"
        for item in _error_chain(error)
    )


def _retry_after(error) -> int | None:
    headers = getattr(getattr(error, "response", None), "headers", None) or getattr(error, "headers", {})
    headers = {str(key).lower(): str(value) for key, value in headers.items()}
    try:
        if "retry-after-ms" in headers:
            seconds = float(headers["retry-after-ms"]) / 1000
        elif "retry-after" in headers:
            try:
                seconds = float(headers["retry-after"])
            except ValueError:
                seconds = (parsedate_to_datetime(headers["retry-after"]) - datetime.now(timezone.utc)).total_seconds()
        else:
            return None
        return max(1, math.ceil(seconds)) if math.isfinite(seconds) else None
    except (TypeError, ValueError, OverflowError):
        return None


class ProviderRateLimitError(RuntimeError):
    """Only safe provider/model/stage fields reach the browser; never raw exception text."""

    def __init__(self, *, stage, provider, model=None, retry_after_seconds=None, quota_exceeded=False):
        self.stage = stage
        self.provider = provider
        self.model = model
        self.retry_after_seconds = retry_after_seconds
        self.quota_exceeded = quota_exceeded
        subject = f"{provider} model {model}" if model else f"{provider} API"
        if quota_exceeded:
            message = (
                f"{subject} has reached its usage quota at node {stage}. "
                "Check the provider's billing and usage limits before trying again."
            )
        else:
            wait = (
                f"Wait at least {retry_after_seconds} seconds before trying again."
                if retry_after_seconds is not None
                else "The provider did not supply a retry time. Check its limits dashboard before trying again."
            )
            message = f"{subject} was rate limited at node {stage}. {wait}"
        super().__init__(message + " No answer was released for this request.")

    def payload(self):
        return {
            "type": "error",
            "code": "provider_quota_exceeded" if self.quota_exceeded else "provider_rate_limited",
            "status": 429,
            "message": str(self),
            "provider": self.provider,
            "model": self.model,
            "node": self.stage,
            "retry_after_seconds": self.retry_after_seconds,
        }


def raise_if_rate_limited(error, stage, *, llm=None, provider=None, model=None):
    """Call before an ordinary fallback. Rate limits stop the request; other errors keep existing behaviour."""
    if isinstance(error, ProviderRateLimitError):
        raise error
    if not is_rate_limited(error):
        return
    chain = list(_error_chain(error))
    if model is None and llm is not None:
        for field in ("model_name", "model"):
            value = getattr(llm, field, None)
            if isinstance(value, str) and value:
                model = value
                break
    if provider is None:
        identity = (type(llm).__name__ + " " + " ".join(type(item).__module__ for item in chain)).lower()
        provider = next(
            (
                name
                for token, name in (
                    ("groq", "Groq"),
                    ("anthropic", "Anthropic"),
                    ("voyage", "Voyage"),
                    ("openai", "OpenAI-compatible"),
                )
                if token in identity
            ),
            "External provider",
        )
    seconds = None
    quota = False
    for item in chain:
        if seconds is None:
            seconds = _retry_after(item)
        body = getattr(item, "body", None)
        if isinstance(body, dict):
            detail = body.get("error", body)
            if isinstance(detail, dict):
                quota |= detail.get("code") in {"insufficient_quota", "billing_hard_limit_reached"}
    raise ProviderRateLimitError(
        stage=stage, provider=provider, model=model, retry_after_seconds=seconds, quota_exceeded=quota
    ) from error


def rate_limit_payload(error):
    """API boundary helper for normalized errors and unexpected wrapped HTTP 429s."""
    for item in _error_chain(error):
        if isinstance(item, ProviderRateLimitError):
            return item.payload()
    try:
        raise_if_rate_limited(error, "workflow")
    except ProviderRateLimitError as limit:
        return limit.payload()
    return None
