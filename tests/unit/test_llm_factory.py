"""Provider isolation for the temporary Groq-only mode; no API calls required."""

import unittest
from unittest.mock import patch, sentinel

from langchain_groq import ChatGroq

from src.services import llm_factory


class GroqOnlyFactoryTests(unittest.TestCase):
    light_methods = (
        "get_router_llm", "get_grader_llm", "get_research_decompose_llm",
        "get_research_sufficiency_llm",
    )
    answer_methods = (
        "get_generator_llm", "get_hallucination_llm",
        "get_high_stakes_hallucination_llm", "get_research_synthesize_llm",
    )

    def test_every_stage_uses_groq_even_with_other_provider_flags_enabled(self):
        with (
            patch.multiple(
                llm_factory.settings,
                FORCE_GROQ_ONLY=True,
                FORCE_OPENAI_ONLY=True,
                USE_LLAMA_GRADER=True,
                USE_GROQ_FAST_PATH=False,
                GROQ_API_KEY="gsk-test-placeholder",
                LITELLM_URL="http://unused-paid-proxy.invalid",
            ),
            patch.object(llm_factory, "ChatOpenAI", side_effect=AssertionError("Paid OpenAI path selected")),
            patch.object(llm_factory, "ChatAnthropic", side_effect=AssertionError("Paid Claude path selected")),
        ):
            for method in self.light_methods + self.answer_methods:
                with self.subTest(method=method):
                    model = getattr(llm_factory.LLMFactory, method)()
                    self.assertIsInstance(model, ChatGroq)
                    expected = (
                        llm_factory.settings.GROQ_LIGHT_MODEL if method in self.light_methods
                        else llm_factory.settings.GROQ_ANSWER_MODEL
                    )
                    self.assertEqual(model.model_name, expected)
                    self.assertIsNotNone(model.max_tokens)
            # Optional callers such as Multi-HyDE also use the task dispatcher.
            self.assertEqual(
                llm_factory._llm_for_task("gpt-4o-mini").model_name,
                llm_factory.settings.GROQ_LIGHT_MODEL,
            )
            self.assertEqual(
                llm_factory._llm_for_task("claude-sonnet-4-6").model_name,
                llm_factory.settings.GROQ_ANSWER_MODEL,
            )

    def test_missing_groq_key_fails_without_a_paid_fallback(self):
        with (
            patch.multiple(llm_factory.settings, FORCE_GROQ_ONLY=True, GROQ_API_KEY=""),
            patch.object(llm_factory, "ChatOpenAI", side_effect=AssertionError("Paid fallback selected")),
            patch.object(llm_factory, "ChatAnthropic", side_effect=AssertionError("Paid fallback selected")),
        ):
            for method in self.light_methods + self.answer_methods:
                with self.subTest(method=method), self.assertRaisesRegex(ValueError, "GROQ_API_KEY is required"):
                    getattr(llm_factory.LLMFactory, method)()

    def test_switching_mode_off_restores_original_provider_selection(self):
        with (
            patch.multiple(
                llm_factory.settings, FORCE_GROQ_ONLY=False, FORCE_OPENAI_ONLY=False,
                USE_LLAMA_GRADER=False, USE_GROQ_FAST_PATH=False,
                ANTHROPIC_API_KEY="test-placeholder", LITELLM_URL="",
            ),
            patch.object(llm_factory, "ChatOpenAI", return_value=sentinel.openai),
            patch.object(llm_factory, "ChatAnthropic", return_value=sentinel.claude),
        ):
            self.assertIs(llm_factory.LLMFactory.get_router_llm(), sentinel.openai)
            self.assertIs(llm_factory.LLMFactory.get_grader_llm(), sentinel.openai)
            self.assertIs(llm_factory.LLMFactory.get_generator_llm(), sentinel.claude)
            self.assertIs(llm_factory.LLMFactory.get_hallucination_llm(), sentinel.claude)


if __name__ == "__main__":
    unittest.main()
