"""Chunk headers: document labels, statement headings versus mentions, and dropped near-empty chunks."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_contextual_collection.py"
spec = importlib.util.spec_from_file_location("build_contextual_collection", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def chunk(index, text, page):
    return {
        "content": f"[FY2021 10K | Page {page}] {text}",
        "chunk_index": index,
        "page_number": page,
        "source_file": "COCACOLA_2021_10K.pdf",
        "fb_company": "Coca-Cola",
    }


def headers(chunks):
    return [content.split("]")[0] + "]" for _, content in builder.contextualize(chunks)]


def test_document_labels():
    assert builder.document_label("AMD_2022_10K.pdf") == "10-K FY2022"
    assert builder.document_label("BESTBUY_2024Q2_10Q.pdf") == "10-Q Q2 FY2024"
    assert builder.document_label("PEPSICO_2023_8K_dated-2023-05-30.pdf") == "8-K dated 2023-05-30"


def test_statement_heading_labels_its_page_and_units():
    statement = chunk(
        1,
        "THE COCA-COLA COMPANY AND SUBSIDIARIES CONSOLIDATED STATEMENTS OF INCOME (In millions except per share data) "
        "Year Ended December 31, 2021 2020 Net Operating Revenues $ 38,655 $ 33,014",
        62,
    )
    assert headers([statement]) == [
        "[Coca-Cola · 10-K FY2021 · Page 62 · Financial Statements · Consolidated Statements Of Income · in millions]"
    ]


def test_a_mention_inside_a_sentence_is_not_a_heading():
    note = chunk(
        2,
        "These amounts were recorded in the line item other operating charges in our consolidated statements of "
        "income. Year ended December 31, 2021, charges were $1,234 million.",
        68,
    )
    assert "Statements Of Income" not in headers([note])[0]


def test_near_empty_chunks_are_dropped():
    assert headers([chunk(3, "Table of Contents", 10)]) == []
