"""A local recorder: workflow events in JSONL, plus a readable Markdown summary.

It never executes the workflow or records full prompts, chunks or draft answers.
All writes are best-effort so a tracing failure cannot break an answer.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)
# Request-local pointer to the active recorder; helpers access it without sharing one mutable global trace between users.
active_trace: ContextVar[RequestTrace | None] = ContextVar("request_trace", default=None)
active_step: ContextVar[str | None] = ContextVar("trace_step", default=None)

PROMPTS = {
    "guardrails": "QUERY_CONTEXTUALIZER_PROMPT / QUERY_CONTEXTUALIZER_USER_TEMPLATE",
    "metadata_extractor": "METADATA_EXTRACTOR_PROMPT / METADATA_EXTRACTOR_USER_TEMPLATE",
    "router": "ROUTER_PROMPT / ROUTER_USER_TEMPLATE",
    "grader": "GRADER_PROMPT / GRADER_USER_TEMPLATE",
    "query_rewriter": "QUERY_REWRITER_PROMPT / QUERY_REWRITER_USER_TEMPLATE",
    "generator": "GENERATOR_SYSTEM_PROMPT / GENERATOR_USER_TEMPLATE / GENERATOR_REVISION_TEMPLATE",
    "hallucination_checker": "HALLUCINATION_CHECK_SYSTEM_PROMPT / HALLUCINATION_CHECK_USER_TEMPLATE",
    "research_agent": "DECOMPOSE_SYSTEM_PROMPT / SUFFICIENCY_SYSTEM_PROMPT / AGENT_SYNTHESIZER_SYSTEM_PROMPT",
    "research.decompose": "DECOMPOSE_SYSTEM_PROMPT / DECOMPOSE_USER_TEMPLATE",
    "research.sufficiency": "SUFFICIENCY_SYSTEM_PROMPT / SUFFICIENCY_USER_TEMPLATE",
    "research.synthesize": "AGENT_SYNTHESIZER_SYSTEM_PROMPT / AGENT_SYNTHESIZER_USER_TEMPLATE",
}
STEPS = set(PROMPTS) | {
    "rbac_gate", "retrieval", "reranker", "hitl_gate", "response_formatter",
    "no_info_response", "blocked_response", "out_of_scope_response", "clarification_response",
}
DETAIL_FIELDS = {
    "access_scope",
    "allowed_doc_types", "query_intent", "query_complexity", "target_companies", "target_fiscal_years",
    "metadata_filter_reason", "metadata_unavailable",
    "guardrail_status", "retrieval_retry_count", "generation_retry_count",
    "hallucination_status", "hallucination_score", "requires_human_approval", "human_decision",
    "agent_sub_questions", "agent_turns_used",
    "contextualized_query", "retrieval_query",
}


# Local filesystem destination, configured by REQUEST_TRACE_DIR; the default is logs/traces, not PostgreSQL.
def trace_root() -> Path:
    return Path(os.environ.get("REQUEST_TRACE_DIR", "logs/traces"))


def record(stage: str, **details) -> None:
    """Record an explicitly selected decision or measurement in the current request."""
    trace = active_trace.get()
    if trace is not None:
        trace.record(stage, **details)


def observe_step(name, function, *args):
    """Time research's ordinary Python calls, which are not separate graph nodes."""
    trace = active_trace.get()
    if trace is None:
        return function(*args)
    run_id = uuid4()
    token = active_step.set(name)
    try:
        try:
            trace.on_chain_start(None, None, run_id=run_id, name=name)
        except Exception:
            logger.warning("Could not record research step start", exc_info=True)
        result = function(*args)
        try:
            trace.on_chain_end(result, run_id=run_id)
        except Exception:
            logger.warning("Could not record research step result", exc_info=True)
        return result
    except BaseException as error:
        try:
            trace.on_chain_error(error, run_id=run_id)
        except Exception:
            logger.warning("Could not record research step error", exc_info=True)
        raise
    finally:
        active_step.reset(token)


def selected_details(output) -> dict:
    if not isinstance(output, dict):
        return {}
    details = {k: v for k, v in output.items() if k in DETAIL_FIELDS}
    for field in ("retrieved_chunks", "reranked_chunks", "relevant_chunks"):
        if field in output:
            chunks = output[field] or []
            details[field + "_count"] = len(chunks)
            details[field + "_sources"] = [
                {k: (c.get("metadata") or {}).get(k) for k in
                 ("source_file", "page_number", "company", "fiscal_year", "doc_type", "confidentiality")}
                for c in chunks
            ]
    # Verdicts are structured decisions, not text chunks or generated answers.
    if "grading_results" in output:
        details["grading_results"] = output["grading_results"]
    return details


# Our observer class, attached as a LangChain callback. It records the run but never executes graph/model work.
class RequestTrace(BaseCallbackHandler):
    """One question, one ID, one event file. LangChain supplies step callbacks."""
    raise_error = False
    run_inline = True

    # TRACE START: Create one request UUID and output folder for this question; retain the conversation ID separately.
    def __init__(self, conversation_id: str, question: str, role: str, permissions: dict, started=None):
        # This backend UUID becomes the frontend Request details ID. It is different from thread_id and checkpoint_id.
        self.request_id = str(uuid4())
        self.conversation_id = conversation_id
        self.directory = trace_root() / self.request_id
        self.started = started if started is not None else time.monotonic()
        self._lock = threading.RLock()
        self.events: list[dict] = []
        self.steps: dict[str, tuple[str, float, int]] = {}
        self.attempts: dict[str, int] = {}
        self.outcome = None
        self.last_step = None
        # Immediately write the cleaned question, role and ordinary permissions as the first local trace event.
        self.record("request_started", question=question, role=role, permissions=permissions)
        self.record("request_preparation", duration_s=round(time.monotonic() - self.started, 4),
                    includes="ownership checks, graph preparation and initial input redaction")

    @contextmanager
    # For this with-block, let helper functions find this recorder through ContextVar; restore the previous value afterward.
    def activate(self):
        token = active_trace.set(self)
        try:
            yield self
        finally:
            active_trace.reset(token)

    # Append one timestamped JSON line to events.jsonl. A file-write failure is logged without breaking the answer.
    def record(self, stage: str, **details):
        try:
            with self._lock:
                event = {"sequence": len(self.events) + 1,
                         "timestamp": datetime.now(timezone.utc).isoformat(),
                         "request_id": self.request_id, "conversation_id": self.conversation_id,
                         "elapsed_s": round(time.monotonic() - self.started, 4),
                         "stage": stage, **details}
                self.events.append(event)
                self.directory.mkdir(parents=True, exist_ok=True)
                with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, default=str) + "\n")
        except Exception:
            logger.warning("Request trace write unavailable", exc_info=True)

    # STEP CALLBACK: Remember node/run ID and start time when LangGraph starts an observed step.
    def on_chain_start(self, serialized, inputs, *, run_id, name=None, metadata=None, **kwargs):
        name = name or (serialized or {}).get("name")
        if name not in STEPS:
            return
        with self._lock:
            attempt = self.attempts.get(name, 0) + 1
            self.attempts[name] = attempt
            self.steps[str(run_id)] = (name, time.monotonic(), attempt)
        self.record("step_started", step=name, attempt=attempt, step_id=str(run_id),
                    prompt=PROMPTS.get(name))

    # STEP CALLBACK: Calculate duration and record selected decisions/counts/sources when the step finishes.
    def on_chain_end(self, outputs, *, run_id, **kwargs):
        with self._lock:
            step = self.steps.pop(str(run_id), None)
        if step:
            name, start, attempt = step
            self.last_step = name
            self.record("step_finished", step=name, attempt=attempt, step_id=str(run_id),
                        duration_s=round(time.monotonic() - start, 4), **selected_details(outputs))

    # STEP CALLBACK: Record a failure or interrupt. An approval pause is distinguished from an ordinary step error.
    def on_chain_error(self, error, *, run_id, **kwargs):
        with self._lock:
            step = self.steps.pop(str(run_id), None)
        if step:
            name, start, attempt = step
            self.record("step_interrupted" if type(error).__name__ == "GraphInterrupt" else "step_failed",
                        step=name, attempt=attempt, step_id=str(run_id),
                        duration_s=round(time.monotonic() - start, 4), error_type=type(error).__name__)

    # TRACE END: Set an outcome once, append total time and generate summary.md. Repeated cleanup calls are harmless.
    def finish(self, outcome: str | None = None):
        try:
            with self._lock:
                if self.outcome is not None:
                    return
                self.outcome = outcome or {
                    "no_info_response": "no_evidence", "blocked_response": "blocked",
                    "clarification_response": "clarification", "out_of_scope_response": "out_of_scope",
                }.get(self.last_step, "answered")
                self.record("request_finished", outcome=self.outcome,
                            duration_s=round(time.monotonic() - self.started, 4))
                self._write_summary()
        except Exception:
            logger.warning("Request trace summary unavailable", exc_info=True)

    # Turn recorded events into the readable report opened through the browser's Request details path.
    def _write_summary(self):
        first, last = self.events[0], self.events[-1]
        lines = ["# Request report", "", f"Request: `{self.request_id}`",
                 f"Conversation: `{self.conversation_id}`", f"Role: **{first['role']}**",
                 f"Question (sanitized): {first['question']}", f"Outcome: **{self.outcome}**",
                 f"Elapsed: **{last['duration_s']} seconds**", "",
                 "Permissions: `" + json.dumps(first["permissions"]) + "`", "",
                 "## Path and decisions", "", "| Step | Attempt | Seconds | Prompt definition (if used) |",
                 "| --- | --- | --- | --- |"]
        for e in self.events:
            if e["stage"] in ("step_finished", "step_failed", "step_interrupted"):
                prompt = PROMPTS.get(e["step"])
                reference = f"[{prompt}](../../../src/config/prompts.py)" if prompt else "No prompt"
                lines.append(f"| {e['step']} | {e['attempt']} | {e['duration_s']} | {reference} |")
        lines += ["", "Step times overlap during parallel work; their sum is not elapsed time.", "",
                  "Prompt definitions: [prompts.py](../../../src/config/prompts.py).", ""]
        for e in self.events:
            if e["stage"] not in ("request_started", "request_finished", "step_started", "model_call", "embedding_call"):
                details = {k: v for k, v in e.items() if k not in
                           {"sequence", "timestamp", "request_id", "conversation_id", "elapsed_s", "stage"}}
                label = e["stage"] + (f" · {e['step']}" if e.get("step") else "")
                lines += [f"### {label}", ""]
                for key, value in details.items():
                    if key in {"step", "step_id", "attempt"} or (key == "duration_s" and e.get("step")):
                        continue  # Already shown in the timing table.
                    if key.endswith("_sources"):
                        refs = list(dict.fromkeys(f"{v.get('source_file')} p.{v.get('page_number')}" for v in value))
                        value = ", ".join(refs[:8]) + (" (more in events.jsonl)" if len(refs) > 8 else "")
                    elif key == "grading_results":
                        if value and all(v.get("agent_curated") for v in value):
                            value = f"{len(value)} evidence items curated by research"
                        else:
                            value = f"{sum(bool(v.get('relevant')) for v in value)} relevant verdicts; details in events.jsonl"
                    text = json.dumps(value, default=str) if not isinstance(value, str) else value
                    lines.append(f"- **{key}:** {text[:600]}" + ("… (see events.jsonl)" if len(text) > 600 else ""))
                lines.append("")
        lines += ["## Provider usage", "", "| Step | Model | Input | Output | Cache read | Cache write | Estimated USD |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        total = 0.0
        incomplete = self.outcome in {"interrupted", "cancelled", "failed"}
        by_model = {}
        for e in self.events:
            if e["stage"] not in ("model_call", "embedding_call"):
                continue
            cost = e.get("cost_usd")
            incomplete |= cost is None or not e.get("usage_available", False)
            total += cost or 0.0
            row = by_model.setdefault(e.get("model", "unknown"), {"input": 0, "output": 0, "calls": 0})
            row["calls"] += 1
            row["input"] += e.get("input_tokens") or 0
            row["output"] += e.get("output_tokens") or 0
            count = lambda k: str(e.get(k, "unknown")) if e.get("usage_available") else "unknown"
            lines.append(f"| {e.get('node') or e['stage']} | {e.get('model', 'unknown')} | {count('input_tokens')} | "
                         f"{count('output_tokens')} | {count('cache_read_tokens')} | {count('cache_write_tokens')} | "
                         f"{cost if cost is not None else 'unknown'} |")
        lines += ["", "### Known tokens by model", "", "| Model | Calls | Input | Output |",
                  "| --- | --- | --- | --- |"]
        for model, row in by_model.items():
            lines.append(f"| {model} | {row['calls']} | {row['input']} | {row['output']} |")
        lines += ["", f"Known estimated cost: **${total:.6f}**" + (" (incomplete)" if incomplete else ""),
                  "", "Provider-reported usage is measured; dollars use configured rates, not invoices.",
                  "Failed calls may have unknown billed usage. Local cache hits make no new provider call;",
                  "provider prompt-cache tokens are still usage. No full prompts, document text or drafts are saved.", ""]
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "summary.md").write_text("\n".join(lines), encoding="utf-8")


# Append the later reviewer decision to a local audit file keyed by conversation ID; this does not generate an answer.
def record_approval(conversation_id: str, **decision):
    """Later decisions are separate events; old reports remain unchanged."""
    try:
        root = trace_root()
        root.mkdir(parents=True, exist_ok=True)
        with (root / "approval-decisions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"conversation_id": conversation_id, **decision}, default=str) + "\n")
    except Exception:
        logger.warning("Approval trace unavailable", exc_info=True)
