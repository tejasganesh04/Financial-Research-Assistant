"""Grade saved app answers against FinanceBench references with Claude Sonnet.

The judge sees the question, reference answer, reference evidence and the
app's final answer, and returns pass, fail or review with a reason. Judge
errors are recorded separately and never counted as wrong app answers.
Resumable: answers that already have a verdict are skipped.

Needs the `anthropic` package; run it inside the project's Docker image:

    python tests/evaluation/financebench/judge_answers.py --run 2026-11-14

Check the judge first on hand-labelled answers with known verdicts:

    python tests/evaluation/financebench/judge_answers.py --controls path/to/controls.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from run_answers import latest_answers

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
PROMPT = (HERE / "judge_prompt.txt").read_text()
MODEL = "claude-sonnet-5-5"
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "review"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}
# On a safety decline, the API reruns the request on a fallback model it chooses.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def api_key() -> str:
    """Read ANTHROPIC_API_KEY from the environment, or from .env as a fallback."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = Path(".env")
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("Set ANTHROPIC_API_KEY")


def judge(client: anthropic.Anthropic, row: dict, model: str) -> dict:
    data = json.dumps(
        {
            "question": row["question"],
            "reference_answer": row["reference_answer"],
            "evidence": [e["text"] if isinstance(e, dict) else e for e in row["evidence"]],
            "system_answer": row["system_answer"],
        },
        ensure_ascii=False,
    )
    result = {
        "case_id": row["case_id"],
        "judge_model": model,
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=PROMPT,
            messages=[{"role": "user", "content": data}],
            output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            betas=[FALLBACK_BETA],
            extra_body={"fallbacks": "default"},
        )
    except anthropic.APIStatusError as error:
        return {**result, "status": "judge_error", "http_status": error.status_code, "error": str(error.message)[:500]}
    except anthropic.APIConnectionError as error:
        return {**result, "status": "judge_error", "error": str(error)[:500]}
    if response.stop_reason != "end_turn":
        return {**result, "status": "judge_error", "error": f"Judge stopped with {response.stop_reason}"}
    verdict = json.loads(next(block.text for block in response.content if block.type == "text"))
    return {
        **result,
        "status": "judged",
        "served_by": response.model,
        "verdict": verdict["verdict"],
        "reason": verdict["reason"],
        "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
    }


def run_controls(client: anthropic.Anthropic, path: Path, model: str, pause: float):
    """Judge hand-labelled answers and report agreement with their expected verdicts."""
    controls = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    agreed = 0
    for index, control in enumerate(controls):
        if index:
            time.sleep(pause)
        result = judge(client, control, model)
        agreed += result.get("verdict") == control["expected_verdict"]
        outcome = result.get("verdict") or f"{result['status']}: {result.get('error')}"
        print(f"{control['case_id']}: expected {control['expected_verdict']}, judge {outcome}", flush=True)
        if result.get("reason"):
            print(f"    {result['reason'][:300]}", flush=True)
    print(f"Agreement: {agreed}/{len(controls)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run", help="Judge tests/evaluation/results/<run>/answers.jsonl")
    target.add_argument("--controls", type=Path, help="Judge hand-labelled answers and report agreement")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--pause", type=float, default=2.0, help="Seconds between judge calls")
    args = parser.parse_args()
    client = anthropic.Anthropic(api_key=api_key(), max_retries=4)
    if args.controls:
        run_controls(client, args.controls, args.model, args.pause)
        return

    run_dir = RESULTS / args.run
    verdicts_path = run_dir / "verdicts.jsonl"
    judged = set()
    if verdicts_path.exists():
        judged = {
            v["case_id"] for v in map(json.loads, verdicts_path.read_text().splitlines()) if v["status"] == "judged"
        }
    pending = [
        row
        for row in latest_answers(run_dir / "answers.jsonl")
        if row["status"] == "answered" and row["case_id"] not in judged
    ]
    print(f"{len(judged)} already judged; judging {len(pending)} now.", flush=True)
    with verdicts_path.open("a") as output:
        for index, row in enumerate(pending):
            if index:
                time.sleep(args.pause)
            result = judge(client, row, args.model)
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            print(f"{row['case_id']}: {result.get('verdict', result['status'])}", flush=True)


if __name__ == "__main__":
    main()
