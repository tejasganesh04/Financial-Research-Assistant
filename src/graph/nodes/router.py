import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from src.config.prompts import ROUTER_PROMPT, ROUTER_USER_TEMPLATE
from src.models.schemas import RouterDecision
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory
from src.services.provider_errors import raise_if_rate_limited

logger = logging.getLogger(__name__)


def router_node(state: RAGState) -> dict:
    """Classify question intent and decide whether retrieval needs the research path."""
    query = state.get("contextualized_query", "")
    if not query:
        return {"query_intent": "clarification", "query_complexity": None}

    llm = None
    try:
        llm = LLMFactory.get_router_llm()
        # GPT-OSS on Groq supports native JSON schemas. Its tool-call output
        # can otherwise fail parsing for greetings and out-of-scope questions.
        structured_options = {}
        if isinstance(llm, ChatGroq):
            structured_options["method"] = (
                "json_schema" if llm.model_name.startswith("openai/gpt-oss-") else "function_calling"
            )
        structured_llm = llm.with_structured_output(
            RouterDecision, **structured_options
        )
        result: RouterDecision = structured_llm.invoke([
            SystemMessage(content=ROUTER_PROMPT),
            HumanMessage(content=ROUTER_USER_TEMPLATE.format(query=query)),
        ])
        logger.info(
            f"Router decision: intent={result.intent}, complexity={result.complexity}, reason={result.reason}"
        )
        from src.services.event_log import emit
        emit("router", intent=result.intent, complexity=result.complexity, reason=result.reason)
        return {
            "query_intent": result.intent,
            "query_complexity": result.complexity,
        }
    except Exception as e:
        raise_if_rate_limited(e, "router", llm=llm)
        logger.error(f"Router failed, defaulting to retrieval/simple_lookup: {e}")
        from src.services.request_trace import record
        record("router_fallback", error_type=type(e).__name__, intent="retrieval", complexity="simple_lookup")
        return {"query_intent": "retrieval", "query_complexity": "simple_lookup"}
