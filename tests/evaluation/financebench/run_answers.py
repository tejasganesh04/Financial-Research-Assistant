"""Ask the running app each selected FinanceBench question, one at a time.

Each question gets a new Analyst conversation through the public JSON API, so
answers, timings and traces come from the real server. Only the question text
is sent; reference answers and evidence are attached to the saved row
afterwards for judging.

Provider limits (HTTP 429) are waited out and retried, and are never counted
as wrong answers. The run is resumable: rerunning with the same --run skips
questions that already have an answer.

    python tests/evaluation/financebench/run_answers.py --run 2026-11-14
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from select_questions import load_selected

RESULTS = Path(__file__).resolve().parents[1] / "results"
MAX_LIMIT_WAIT_SECONDS = 900


def latest_answers(path: Path) -> list[dict]:
    """The last saved row per question; earlier failed attempts are superseded."""
    rows: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        rows[row["case_id"]] = row
    return list(rows.values())


def post(base_url: str, path: str, body: dict, token: str | None = None, timeout: int = 900):
    """POST JSON and return (status, headers, parsed body)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base_url + path, json.dumps(body).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"detail": raw.decode(errors="replace")[:500]}
        return error.code, dict(error.headers), payload


def login(base_url: str, username: str, password: str) -> str:
    status, _, body = post(base_url, "/v1/auth/login", {"username": username, "password": password}, timeout=30)
    if status != 200:
        raise RuntimeError(f"Login failed with HTTP {status}: {body}")
    return body["access_token"]


def ask(base_url: str, token: str, question: str, max_attempts: int) -> dict:
    """Ask once, waiting out provider limits. Returns the outcome without judging it."""
    thread_id = str(uuid.uuid4())
    limit_waits = []
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        status, headers, body = post(
            base_url, "/v1/conversations/messages", {"message": question, "thread_id": thread_id}, token
        )
        elapsed = round(time.monotonic() - started, 2)
        headers = {k.lower(): v for k, v in headers.items()}
        if status == 429 and attempt < max_attempts:
            wait = min(int(headers.get("retry-after") or 60) + 5, MAX_LIMIT_WAIT_SECONDS)
            limit_waits.append({"code": headers.get("x-error-code"), "waited_s": wait})
            print(f"    provider limit ({headers.get('x-error-code')}); waiting {wait}s", flush=True)
            time.sleep(wait)
            # A failed attempt may leave a saved turn; retry in a fresh conversation.
            thread_id = str(uuid.uuid4())
            continue
        return {
            "http_status": status,
            "status": "answered" if status == 200 and not body.get("requires_approval") else "app_error",
            "system_answer": body.get("response", ""),
            "error_detail": None if status == 200 else body.get("detail"),
            "error_code": headers.get("x-error-code"),
            "request_id": body.get("request_id") or headers.get("x-request-id"),
            "thread_id": thread_id,
            "sources": body.get("sources", []),
            "confidence": body.get("confidence"),
            "tokens": body.get("tokens"),
            "elapsed_s": elapsed,
            "attempts": attempt,
            "limit_waits": limit_waits,
        }
    raise AssertionError("unreachable")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, help="Run name; results go to tests/evaluation/results/<run>/")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--user", default="analyst")
    parser.add_argument(
        "--password",
        default=os.environ.get("FRA_PASSWORD"),
        help="The account's password (default: the FRA_PASSWORD environment variable, else asked for)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Answer at most this many new questions")
    parser.add_argument("--pause", type=int, default=30, help="Seconds between questions, to respect free-tier limits")
    parser.add_argument("--max-attempts", type=int, default=4)
    args = parser.parse_args()

    if not args.password:
        args.password = getpass.getpass(f"Password for {args.user}: ")
    out_dir = RESULTS / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "answers.jsonl"
    done = set()
    if out_path.exists():
        done = {
            row["case_id"] for row in map(json.loads, out_path.read_text().splitlines()) if row["status"] == "answered"
        }

    questions = [q for q in load_selected() if q["financebench_id"] not in done][: args.limit]
    print(f"{len(done)} already answered; asking {len(questions)} now.", flush=True)
    token, token_time = login(args.base_url, args.user, args.password), time.monotonic()
    with out_path.open("a") as output:
        for index, question in enumerate(questions):
            if index:
                time.sleep(args.pause)
            if time.monotonic() - token_time > 45 * 60:  # access tokens last 60 minutes
                token, token_time = login(args.base_url, args.user, args.password), time.monotonic()
            print(f"[{index + 1}/{len(questions)}] {question['financebench_id']}", flush=True)
            outcome = ask(args.base_url, token, question["question"], args.max_attempts)
            row = {
                "case_id": question["financebench_id"],
                "question_type": question["question_type"],
                "question": question["question"],
                "reference_answer": question["answer"],
                "evidence": [
                    {"document": e["doc_name"], "page": e["evidence_page_num"], "text": e["evidence_text"]}
                    for e in question["evidence"]
                ],
                "asked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **outcome,
            }
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            print(f"    {row['status']} in {row['elapsed_s']}s (request {row['request_id']})", flush=True)


if __name__ == "__main__":
    main()
