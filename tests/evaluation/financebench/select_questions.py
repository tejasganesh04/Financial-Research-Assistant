"""Select a reproducible, type-balanced FinanceBench sample.

Reads the pinned public FinanceBench file, checks its SHA-256, and writes the
selected question IDs. Questions, references and evidence stay in the
downloaded file; only IDs are kept in the repository.

    python tests/evaluation/financebench/select_questions.py --count 25
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_URL = "https://raw.githubusercontent.com/patronus-ai/financebench/main/data/financebench_open_source.jsonl"
SOURCE_SHA256 = "a5a2aa673e573e55675fc3c0f9aa38c1cf59d2abc91edb077534f71f10a71877"
SOURCE_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")
IDS_PATH = HERE / "question_ids.json"


def load_source() -> list[dict]:
    """Download the pinned file if missing and verify it before use."""
    if not SOURCE_PATH.exists():
        SOURCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(SOURCE_URL, SOURCE_PATH)
    digest = hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()
    if digest != SOURCE_SHA256:
        raise ValueError(f"{SOURCE_PATH} does not match the pinned FinanceBench release")
    return [json.loads(line) for line in SOURCE_PATH.read_text().splitlines() if line.strip()]


def load_selected() -> list[dict]:
    """Return the selected questions in selection order."""
    by_id = {row["financebench_id"]: row for row in load_source()}
    return [by_id[case_id] for case_id in json.loads(IDS_PATH.read_text())["ids"]]


def select(rows: list[dict], count: int, seed: int) -> list[str]:
    """Sample each question type in proportion to its share of the dataset."""
    by_type: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda r: r["financebench_id"]):
        by_type.setdefault(row["question_type"], []).append(row)
    types = sorted(by_type)
    exact = {t: count * len(by_type[t]) / len(rows) for t in types}
    quota = {t: int(exact[t]) for t in types}
    # Largest remainder; ties go to the alphabetically first type.
    for t in sorted(types, key=lambda t: (-(exact[t] - quota[t]), t))[: count - sum(quota.values())]:
        quota[t] += 1
    rng = random.Random(seed)
    chosen = [row for t in types for row in rng.sample(by_type[t], quota[t])]
    rng.shuffle(chosen)
    return [row["financebench_id"] for row in chosen]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=25)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    rows = load_source()
    ids = select(rows, args.count, args.seed)
    IDS_PATH.write_text(
        json.dumps(
            {"source_url": SOURCE_URL, "source_sha256": SOURCE_SHA256, "seed": args.seed, "ids": ids},
            indent=2,
        )
        + "\n"
    )
    by_type: dict[str, int] = {}
    for row in rows:
        if row["financebench_id"] in ids:
            by_type[row["question_type"]] = by_type.get(row["question_type"], 0) + 1
    print(f"Selected {len(ids)} of {len(rows)} questions: {by_type}")


if __name__ == "__main__":
    main()
