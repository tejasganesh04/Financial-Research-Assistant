"""Build language-model clients for each workflow task.

Groq-only mode uses GROQ_LIGHT_MODEL for smaller tasks and GROQ_ANSWER_MODEL
for synthesis, answers and grounding checks, without paid-provider fallback.
OpenAI-only mode uses OPENAI_FALLBACK_MODEL throughout. The optional mixed
profile uses OpenAI/Groq for smaller tasks and Anthropic for answer tasks.
Provider branches and environment settings determine the actual configuration."""

import logging
import os

from langchain_anthropic import ChatAnthropic
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from src.config.settings import settings
from src.services.cost_tracker import get_cost_handler
from src.services.request_context import current_user_id

logger = logging.getLogger(__name__)


def _attribution_kwargs() -> dict:
    """Add verified user attribution only when an optional LiteLLM gateway is configured. Direct providers receive no extra user field."""
    uid = current_user_id.get()
    if not uid or not settings.LITELLM_URL:
        return {}
    return {"model_kwargs": {"user": uid}}


_OPENAI_SEED = 42


def _openai(model: str, temperature: float = 0.0, max_tokens: int | None = None) -> ChatOpenAI:
    """Build an OpenAI client, using the optional gateway when configured."""
    kwargs = {
        "model": model,
        "temperature": temperature,
        "seed": _OPENAI_SEED,
        "callbacks": [get_cost_handler()],
    }
    if settings.LITELLM_URL:
        kwargs["base_url"] = f"{settings.LITELLM_URL.rstrip('/')}/v1"
        kwargs["api_key"] = "sk-litellm-proxy"
    else:
        kwargs["api_key"] = settings.OPENAI_API_KEY
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    kwargs.update(_attribution_kwargs())
    return ChatOpenAI(**kwargs)


_ANTHROPIC_NO_TEMPERATURE_MODELS: set[str] = {"claude-opus-4-7"}


def _anthropic(model: str, temperature: float = 0.0, max_tokens: int = 1024) -> ChatAnthropic:
    """Build an Anthropic client, omitting unsupported temperature parameters."""
    kwargs = {
        "model_name": model,
        "max_tokens": max_tokens,
        "callbacks": [get_cost_handler()],
    }
    if settings.LITELLM_URL:
        kwargs["base_url"] = f"{settings.LITELLM_URL.rstrip('/')}/anthropic"
        kwargs["api_key"] = "sk-litellm-proxy"
    else:
        kwargs["api_key"] = settings.ANTHROPIC_API_KEY
    if model not in _ANTHROPIC_NO_TEMPERATURE_MODELS:
        kwargs["temperature"] = temperature
    kwargs.update(_attribution_kwargs())
    return ChatAnthropic(**kwargs)


def _llm_for_task(model_name: str, temperature: float = 0.0, max_tokens: int = 2048):
    """Resolve a task model while respecting the Groq-only override."""
    if settings.FORCE_GROQ_ONLY:
        return _groq_demo(answer=model_name.startswith("claude-"), temperature=temperature, max_tokens=max_tokens)
    if model_name.startswith("claude-"):
        return _anthropic(model_name, temperature=temperature, max_tokens=max_tokens)
    if model_name.startswith("gpt-"):
        return _openai(model_name, temperature=temperature, max_tokens=max_tokens)
    raise ValueError(
        f"Unknown model provider for {model_name!r}. "
        f"Expected prefix 'claude-' or 'gpt-'."
    )


def _groq(model: str, temperature: float = 0.0, max_tokens: int | None = None) -> ChatGroq | ChatOpenAI:
    """Groq chat model. When `LITELLM_URL` is set, routes through LiteLLM's
    OpenAI-compatible endpoint (Groq is exposed as a regular model in
    LiteLLM's model_list, no separate /groq pass-through needed).

    Returns `ChatOpenAI` instead of `ChatGroq` when proxied — same `.invoke()`
    interface, langchain-groq isn't needed when the proxy is doing the
    provider routing.
    """
    if settings.LITELLM_URL and not settings.FORCE_GROQ_ONLY:
        return ChatOpenAI(
            model=model,
            temperature=temperature,
            seed=_OPENAI_SEED,
            base_url=f"{settings.LITELLM_URL.rstrip('/')}/v1",
            api_key="sk-litellm-proxy",
            callbacks=[get_cost_handler()],
            max_tokens=max_tokens,
            **_attribution_kwargs(),
        )
    kwargs = dict(
        model=model,
        temperature=temperature,
        api_key=settings.GROQ_API_KEY,
        callbacks=[get_cost_handler()],
    )
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if model.startswith("openai/gpt-oss-"):
        kwargs["reasoning_effort"] = "low"
    return ChatGroq(**kwargs)


def _groq_demo(*, answer: bool = False, temperature: float = 0.0, max_tokens: int = 1024):
    """Groq-only mode fails explicitly if its key is missing; never uses paid providers."""
    if not settings.GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY is required when FORCE_GROQ_ONLY=true")
    model = settings.GROQ_ANSWER_MODEL if answer else settings.GROQ_LIGHT_MODEL
    return _groq(model, temperature=temperature, max_tokens=max_tokens)


_warned_keys: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    """Emit each warning once per process — fast-path nodes are called per-question."""
    if key not in _warned_keys:
        _warned_keys.add(key)
        logger.warning(msg)


class LLMFactory:
    """Select the configured provider and model for each workflow task."""

    @staticmethod
    def get_router_llm():
        """Select the smaller model used for routing and related decisions."""
        if settings.FORCE_GROQ_ONLY:
            return _groq_demo()
        if settings.FORCE_OPENAI_ONLY or not settings.USE_GROQ_FAST_PATH:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0)
        if settings.GROQ_API_KEY:
            return _groq(settings.ROUTER_MODEL, 0.0)
        _warn_once("router_no_groq", "Groq API key not set for router, falling back to OpenAI")
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0)

    @staticmethod
    def get_grader_llm():
        """Select the model that judges each candidate chunk for relevance."""
        if settings.FORCE_GROQ_ONLY:
            return _groq_demo()
        if settings.USE_LLAMA_GRADER:
            provider = settings.LLAMA_GRADER_PROVIDER.lower()
            if provider == "openrouter":
                key = settings.OPENROUTER_API_KEY or os.environ.get("OPENROUTER_API_KEY")
                if not key:
                    raise RuntimeError(
                        "USE_LLAMA_GRADER=true + LLAMA_GRADER_PROVIDER=openrouter "
                        "requires OPENROUTER_API_KEY in env"
                    )
                _warn_once(
                    "grader_openrouter_llama",
                    f"Grader: OpenRouter {settings.LLAMA_GRADER_MODEL_OPENROUTER} "
                    f"(Sprint 7.17 follow-up #3 — full-eval test of +16pp gold-recall lift)",
                )
                return ChatOpenAI(
                    model=settings.LLAMA_GRADER_MODEL_OPENROUTER,
                    temperature=0.0,
                    max_tokens=1024,
                    base_url="https://openrouter.ai/api/v1",
                    api_key=key,
                    max_retries=3,
                    extra_body={
                        "provider": {
                            "order": ["fireworks", "together", "deepinfra"],
                            "allow_fallbacks": False,
                        }
                    },
                    callbacks=[get_cost_handler()],
                    **_attribution_kwargs(),
                )
            if provider == "fireworks":
                key = settings.FIREWORKS_API_KEY or os.environ.get("FIREWORKS_API_KEY")
                if not key:
                    raise RuntimeError(
                        "USE_LLAMA_GRADER=true + LLAMA_GRADER_PROVIDER=fireworks "
                        "requires FIREWORKS_API_KEY in env"
                    )
                _warn_once(
                    "grader_fireworks_llama",
                    f"Grader: Fireworks {settings.LLAMA_GRADER_MODEL_FIREWORKS} "
                    f"(Sprint 7.17 follow-up #3 — full-eval test of +16pp gold-recall lift)",
                )
                return ChatOpenAI(
                    model=settings.LLAMA_GRADER_MODEL_FIREWORKS,
                    temperature=0.0,
                    max_tokens=512,
                    base_url="https://api.fireworks.ai/inference/v1",
                    api_key=key,
                    max_retries=3,
                    callbacks=[get_cost_handler()],
                    **_attribution_kwargs(),
                )
            raise ValueError(
                f"Unknown LLAMA_GRADER_PROVIDER={settings.LLAMA_GRADER_PROVIDER!r} "
                f"(expected 'openrouter' or 'fireworks')"
            )
        if settings.FORCE_OPENAI_ONLY or not settings.USE_GROQ_FAST_PATH:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0)
        if settings.GROQ_API_KEY:
            return _groq(settings.GRADER_MODEL, 0.0)
        _warn_once("grader_no_groq", "Groq API key not set for grader, falling back to OpenAI")
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0)

    @staticmethod
    def get_generator_llm():
        """Select the answer model, respecting provider-mode overrides."""
        if settings.FORCE_GROQ_ONLY:
            return _groq_demo(answer=True, temperature=0.1, max_tokens=2048)
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1)
        if settings.ANTHROPIC_API_KEY:
            return _anthropic(settings.GENERATOR_MODEL, temperature=0.1, max_tokens=2048)
        logger.warning("Anthropic API key not set for generator, falling back to OpenAI")
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1)

    @staticmethod
    def get_hallucination_llm():
        """Select the model that checks support for the drafted answer."""
        if settings.FORCE_GROQ_ONLY:
            return _groq_demo(answer=True)
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=512)
        if settings.ANTHROPIC_API_KEY:
            return _anthropic(settings.HALLUCINATION_MODEL, temperature=0.0, max_tokens=2048)
        _warn_once(
            "hallucination_no_anthropic",
            "Anthropic API key not set for hallucination checker, falling back to OpenAI",
        )
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=512)

    @staticmethod
    def get_high_stakes_hallucination_llm():
        """Select the grounding checker for an explicitly escalated answer."""
        if settings.FORCE_GROQ_ONLY:
            return _groq_demo(answer=True)
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=512)
        if settings.ANTHROPIC_API_KEY:
            return _anthropic(settings.HIGH_STAKES_HALLUCINATION_MODEL, temperature=0.0, max_tokens=2048)
        _warn_once(
            "high_stakes_no_anthropic",
            "Anthropic API key not set for high-stakes check, falling back to OpenAI",
        )
        return LLMFactory.get_hallucination_llm()

    @staticmethod
    def get_research_decompose_llm():
        """Select the planning model that produces research subquestions."""
        if settings.FORCE_GROQ_ONLY:
            return _groq_demo(max_tokens=2048)
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=2048)
        if not settings.ANTHROPIC_API_KEY and settings.RESEARCH_AGENT_DECOMPOSE_MODEL.startswith("claude-"):
            _warn_once("decompose_no_anthropic", "Anthropic key missing, falling back to OpenAI for decompose")
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=2048)
        return _llm_for_task(settings.RESEARCH_AGENT_DECOMPOSE_MODEL, temperature=0.0, max_tokens=2048)

    @staticmethod
    def get_research_sufficiency_llm():
        """Select the model that checks for gaps in research evidence."""
        if settings.FORCE_GROQ_ONLY:
            return _groq_demo(max_tokens=2048)
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=2048)
        if not settings.ANTHROPIC_API_KEY and settings.RESEARCH_AGENT_SUFFICIENCY_MODEL.startswith("claude-"):
            _warn_once("sufficiency_no_anthropic", "Anthropic key missing, falling back to OpenAI for sufficiency")
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=2048)
        return _llm_for_task(settings.RESEARCH_AGENT_SUFFICIENCY_MODEL, temperature=0.0, max_tokens=2048)

    @staticmethod
    def get_research_synthesize_llm():
        """Select the model that combines research evidence into a synthesis."""
        if settings.FORCE_GROQ_ONLY:
            return _groq_demo(answer=True, temperature=0.1, max_tokens=2048)
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1, max_tokens=2048)
        if not settings.ANTHROPIC_API_KEY and settings.RESEARCH_AGENT_SYNTHESIZE_MODEL.startswith("claude-"):
            _warn_once("synthesize_no_anthropic", "Anthropic key missing, falling back to OpenAI for synthesize")
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1, max_tokens=2048)
        return _llm_for_task(settings.RESEARCH_AGENT_SYNTHESIZE_MODEL, temperature=0.1, max_tokens=2048)
