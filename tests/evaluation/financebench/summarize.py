"""Summarize a FinanceBench run: accuracy, failures, latency and token use.

Combines answers, judge verdicts and the app's local request traces into
tests/evaluation/results/<run>/summary.md.

    python tests/evaluation/financebench/summarize.py --run 2026-11-14
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from run_answers import latest_answers
from select_questions import load_selected

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
TRACES = Path("logs/traces")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def trace_facts(request_id: str | None) -> dict:
    """Path taken and provider-reported tokens per model for one request."""
    path = TRACES / str(request_id) / "events.jsonl"
    facts = {"research": False, "tokens": Counter()}
    if not request_id or not path.exists():
        return facts
    for event in map(json.loads, path.read_text().splitlines()):
        if event.get("stage") == "step_finished" and event.get("step") == "research_agent":
            facts["research"] = True
        if event.get("stage") == "model_call" and event.get("model"):
            facts["tokens"][event["model"]] += (event.get("input_tokens") or 0) + (event.get("output_tokens") or 0)
    return facts


def rate(passed: int, failed: int) -> str:
    return f"{passed}/{passed + failed} ({100 * passed / (passed + failed):.0f}%)" if passed + failed else "n/a"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()

    run_dir = RESULTS / args.run
    answers = {row["case_id"]: row for row in latest_answers(run_dir / "answers.jsonl")}
    verdicts = {}
    verdict_path = run_dir / "verdicts.jsonl"
    if verdict_path.exists():
        for v in map(json.loads, verdict_path.read_text().splitlines()):
            if v["status"] == "judged" or v["case_id"] not in verdicts:
                verdicts[v["case_id"]] = v
    selected = load_selected()

    outcome = Counter()
    by_type = defaultdict(Counter)
    by_path = defaultdict(Counter)
    latencies, tokens, failures, errors = [], defaultdict(list), [], []
    for question in selected:
        case_id = question["financebench_id"]
        row = answers.get(case_id)
        if row is None:
            outcome["not run"] += 1
            continue
        if row["status"] != "answered":
            outcome["app error"] += 1
            errors.append(row)
            continue
        latencies.append(row["elapsed_s"])
        facts = trace_facts(row["request_id"])
        for model, count in facts["tokens"].items():
            tokens[model].append(count)
        verdict = verdicts.get(case_id, {})
        label = verdict.get("verdict") or ("judge error" if verdict else "not judged")
        outcome[label] += 1
        by_type[row["question_type"]][label] += 1
        by_path["research" if facts["research"] else "simple lookup"][label] += 1
        if label in {"fail", "review"}:
            failures.append((row, verdict))

    lines = [
        f"# FinanceBench sample: {args.run}",
        "",
        f"{len(selected)} questions selected; Analyst role; one new conversation per question.",
        "",
        "## Accuracy",
        "",
        f"- **Pass rate (judged pass or fail): {rate(outcome['pass'], outcome['fail'])}**",
        f"- Outcomes: {dict(outcome)}",
        "- `review` means the judge found the reference ambiguous; it is excluded from the pass rate.",
        "",
        "| Group | Pass rate | Review |",
        "| --- | --- | --- |",
    ]
    for group in (by_type, by_path):
        for name, counts in sorted(group.items()):
            lines.append(f"| {name} | {rate(counts['pass'], counts['fail'])} | {counts['review']} |")
    lines += [
        "",
        "## Speed and tokens (answered questions)",
        "",
        f"- Response time p50 {percentile(latencies, 0.5)}s, p95 {percentile(latencies, 0.95)}s",
    ]
    for model, counts in sorted(tokens.items()):
        lines.append(f"- {model}: median {statistics.median(counts):,.0f} tokens per question, max {max(counts):,}")
    lines += ["", "## Failures and reviews", ""]
    for row, verdict in failures:
        lines.append(f"- **{row['case_id']}** ({verdict.get('verdict')}): {row['question'][:160]}")
        lines.append(f"  - Reference: {str(row['reference_answer'])[:200]}")
        lines.append(f"  - Judge: {verdict.get('reason', '')[:400]}")
        lines.append(f"  - Trace: `logs/traces/{row['request_id']}/summary.md`")
    if errors:
        lines += ["", "## App errors (not counted as wrong answers)", ""]
        for row in errors:
            lines.append(
                f"- {row['case_id']}: HTTP {row['http_status']} {row.get('error_code') or ''} {row.get('error_detail')}"
            )
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:16]))


if __name__ == "__main__":
    main()
