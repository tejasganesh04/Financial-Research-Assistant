"""Draw the LangGraph workflow diagrams as SVG: the current graph and one per measured version.

Every version uses the same layout, so diagrams can be compared side by side. Nodes
changed in a version are outlined in orange. Render PNGs from the SVGs with any
headless browser, for example:

    python scripts/build_diagrams.py
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \\
      --screenshot=docs/diagrams/langgraph-workflow.png --window-size=1900,<height> \\
      docs/diagrams/langgraph-workflow.svg
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "diagrams"
WIDTH = 1900
SANS = "Arial, Helvetica, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Consolas, monospace"
CHANGED = "#E8742A"

PALETTE = {
    "intake": ("#EAF1FC", "#9AB5D9", "#264E75"),
    "evidence": ("#E2F3EC", "#69AC94", "#165D48"),
    "store": ("#FFFFFF", "#69AC94", "#165D48"),
    "research": ("#FFF3D8", "#D7AE50", "#765419"),
    "answer": ("#E7F0FC", "#8AAED4", "#214F79"),
    "approval": ("#EEE5FA", "#A385C6", "#5F3F81"),
    "exit": ("#F0F3F6", "#B5C3CE", "#455E71"),
    "blocked": ("#FCECE7", "#CE9C8D", "#8C4B36"),
}
EDGE = {"main": "#607F95", "loop": "#398A72", "blocked": "#B87965", "approval": "#8760B2", "end": "#93A5B4"}

MAIN_X, MAIN_W = 760, 380
LEFT_X, LEFT_W = 242, 360
RIGHT_X, RIGHT_W = 1330, 430
CENTER = MAIN_X + MAIN_W // 2


@dataclass
class Version:
    slug: str
    number: int | None
    title: str
    subtitle: str
    labels: dict[str, list[str]]
    changed: set[str] = field(default_factory=set)
    metadata_first: bool = False
    expander: bool = True
    redaction: str = "names kept"
    metric: tuple[str, str] | None = None
    results: list[tuple[str, str]] = field(default_factory=list)


BASE = {
    "rbac_gate": ["Set allowed document types", "from role + explicit escalation"],
    "guardrails": ["Prompt injection checks", "Resolve follow-up context"],
    "router": ["Choose intent + complexity"],
    "metadata_extractor": ["Select company/year filters", "Small model + metadata catalog"],
    "knowledge_base": ["financial_docs: 68,059 chunks", "from 84 filings", "No context in the chunk text"],
    "retrieval": ["Meaning (voyage-finance-2) +", "keyword (BM25) search", "50 each, fused by rank → 50"],
    "reranker": ["Local BGE cross-encoder", "Keep top 8"],
    "grader": ["Small model keeps relevant chunks"],
    "query_rewriter": ["Rewrite the search query", "At most 2 retries"],
    "research_agent": [
        "Split into sub-questions",
        "Search → rerank → grade each",
        "Check gaps; bounded follow-ups",
        "Synthesize the findings",
    ],
    "generator": ["Draft from relevant evidence", "and recent conversation"],
    "hallucination_checker": ["Check support in the evidence", "Update generation retry count"],
    "hitl_gate": ["Expanded scope → interrupt()", "Hold draft for Admin review", "Resume after approve / reject"],
    "response_formatter": ["Final answer + source references", "Add warning if unverified"],
    "clarification_response": ["Ask the user to clarify"],
    "out_of_scope_response": ["Explain the scope limitation"],
    "no_info_response": ["No relevant information found", "within the search scope"],
    "blocked_response": ["Safety refusal OR review rejection", "Also used when approval", "storage is unavailable"],
}

QUESTION_HANDLING = {
    "router": ["Choose intent + complexity", "Company filings only"],
    "metadata_extractor": [
        "Company questions only",
        "Company required; no year →",
        "latest available year",
        "Record what is unavailable",
    ],
    "generator": ["Draft from relevant evidence", "Answer the available parts"],
    "response_formatter": ["Final answer + source references", "Warning if unverified", "Note on anything left out"],
    "no_info_response": ["Name the filings searched, or", "the companies this user can", "ask about"],
}

MORE_EVIDENCE = {
    "retrieval": [
        "Question + finance terms",
        "Meaning (voyage-finance-2) +",
        "keyword (BM25) search",
        "50 each, fused by rank → 50",
    ],
    "reranker": ["Local BGE, fused with the", "meaning-search rank", "Keep top 12"],
    "context_expander": ["Add the chunk before and after", "each kept chunk (size-capped)"],
    "generator": ["Draft from relevant evidence", "Answer the available parts", "Number rules: units, year, precision"],
}

CONTEXTUAL_KB = {
    "knowledge_base": [
        "financial_docs_v3: 64,952 chunks",
        "Each starts with [Company · Filing",
        "· Page · Section · Statement · Units]",
        "3,107 near-empty chunks removed",
    ],
}

FINAL = {
    "retrieval": [
        "Question + finance terms",
        "Meaning (voyage-finance-2) +",
        "keyword (BM25) search",
        "100 each, fused by rank → 100",
    ],
    "reranker": ["Voyage rerank-2.5", "Local BGE as fallback", "Keep top 12"],
}

# The current graph also shows what changed after version 4 was measured (the exact calculator,
# the access filter on neighbouring chunks, the live reranker) and spells out details of each node.
# Version diagrams keep the labels of the system as it was measured.
LIVE = {
    "guardrails": [
        "Prompt injection: local LLM Guard,",
        "then a model for borderline input",
        "Resolve follow-up context",
    ],
    "metadata_extractor": [
        "Pick company + years from what",
        "this user can access",
        "Company required; no year →",
        "latest available year",
        "Record what is unavailable",
    ],
    "retrieval": [
        "Question + finance terms",
        "Meaning (voyage-finance-2) +",
        "keyword (BM25) search",
        "Filter: access, company, year",
        "100 each, fused by rank → 100",
    ],
    "reranker": [
        "Voyage reranker · keep top 12",
        "Measured with rerank-2.5",
        "Live site: rerank-3",
        "Fallback: local BGE (off live)",
    ],
    "grader": ["Drop other companies/years", "Small model keeps relevant chunks"],
    "query_rewriter": ["Rewrite using the grader's reasons", "At most 2 retries"],
    "context_expander": ["Add the chunk before and after", "each kept chunk (size-capped)", "Same access filter"],
    "generator": [
        "Draft from evidence + recent chat",
        "Answer the available parts",
        "Number rules: units, year, precision",
        "Exact calculator for arithmetic",
        "Revision: last draft + feedback",
    ],
    "hallucination_checker": [
        "Check the draft against sources",
        "Calculations: check their inputs",
        "Failed → feedback; 1 revision max",
    ],
    "response_formatter": [
        "Final answer + source references",
        "Warning if unverified",
        "Notes: calculations, left-out parts",
        "Refusal → say what was searched",
    ],
    "no_info_response": [
        "Name the filings searched, or",
        "the companies this user can",
        "ask about",
        "Extra access: a plain reply that",
        "names nothing (not reviewed)",
    ],
}
LIVE_BEFORE_START = (
    "JWT + open session → question quota → conversation ownership → parse /escalate → "
    "redact personal data (names kept) → wait for a slot → state and trace"
)

VERSIONS = [
    Version(
        slug="v0-baseline",
        number=0,
        title="Baseline",
        subtitle="The system as first measured",
        labels={},
        metadata_first=True,
        expander=False,
        redaction="including names",
        metric=("101 / 150", "answer page reached the model"),
        results=[
            ("101 / 150", "Search check: answer page in the top 8"),
            ("15 / 25", "Answer benchmark (60%)"),
            ("73 s", "Median answer time"),
        ],
    ),
    Version(
        slug="v1-question-handling",
        number=1,
        title="Clear question handling",
        subtitle="Company required · router before metadata · answer what is available",
        labels=QUESTION_HANDLING,
        changed={"before_start", "router", "metadata_extractor", "generator", "response_formatter", "no_info_response"},
        expander=False,
        metric=("8 / 10", "edge-case questions as specified"),
        results=[
            ("8 / 10", "Edge cases exactly as specified, 2 acceptable"),
            ("101 / 150", "Search check (unchanged)"),
        ],
    ),
    Version(
        slug="v2-more-evidence",
        number=2,
        title="More evidence per answer",
        subtitle="Top 12 · neighbouring chunks · finance terms · rank fusion · number rules",
        labels={**QUESTION_HANDLING, **MORE_EVIDENCE},
        changed={"retrieval", "reranker", "context_expander", "generator"},
        metric=("119 / 150", "answer page reached the model"),
        results=[("111 / 150", "Search check, top 12 alone"), ("119 / 150", "With finance terms + fusion")],
    ),
    Version(
        slug="v3-contextual-knowledge-base",
        number=3,
        title="Contextual knowledge base",
        subtitle="Every chunk says which company, filing, statement and units it comes from",
        labels={**QUESTION_HANDLING, **MORE_EVIDENCE, **CONTEXTUAL_KB},
        changed={"knowledge_base"},
        metric=("129 / 150", "answer page reached the model"),
        results=[("128 / 150", "First header attempt (labelling bug)"), ("129 / 150", "Headers after the fix")],
    ),
    Version(
        slug="v4-final",
        number=4,
        title="Stronger reranker, wider search",
        subtitle="Voyage rerank-2.5 reads 100 candidates · the current system",
        labels={**QUESTION_HANDLING, **MORE_EVIDENCE, **CONTEXTUAL_KB, **FINAL},
        changed={"retrieval", "reranker"},
        metric=("145 / 150", "answer page reached the model"),
        results=[
            ("145 / 150", "Search check"),
            ("14 / 14", "New questions from random pages"),
            ("20 / 24", "Answer benchmark (83%)"),
            ("32 s", "Median answer time"),
        ],
    ),
]
CURRENT = VERSIONS[-1]


@dataclass
class Box:
    name: str
    x: int
    y: int
    w: int
    h: int
    kind: str
    lines: list[str]

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def mid(self) -> int:
        return self.y + self.h // 2


def height(lines: list[str]) -> int:
    return 87 + 29 * (len(lines) - 1)


def text(x, y, lines, size=21, weight=400, fill="#344F60", family=SANS, anchor="start", step=29) -> str:
    spans = "".join(
        f'<tspan x="{x}" dy="{0 if i == 0 else step}">{escape(line)}</tspan>' for i, line in enumerate(lines)
    )
    return (
        f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}">{spans}</text>'
    )


def rect(x, y, w, h, fill, stroke="none", rx=14, width=1.7, dash="") -> str:
    dashed = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{width}"{dashed}/>'
    )


def layout(v: Version, live: bool = False) -> dict[str, Box]:
    labels = {**BASE, **v.labels, **(LIVE if live else {})}
    boxes: dict[str, Box] = {}

    def main(name, y, kind):
        lines = labels[name]
        boxes[name] = Box(name, MAIN_X, y, MAIN_W, height(lines), kind, lines)
        return boxes[name].bottom

    def side(name, x, w, y, kind):
        lines = labels[name]
        boxes[name] = Box(name, x, y, w, height(lines), kind, lines)
        return boxes[name].bottom

    y = main("rbac_gate", 405, "intake") + 60
    y = main("guardrails", y, "intake") + 70
    first, second = ("metadata_extractor", "router") if v.metadata_first else ("router", "metadata_extractor")
    y = main(first, y, "intake") + 80
    y = main(second, y, "intake")
    router = boxes["router"]
    clar_bottom = side("clarification_response", LEFT_X, LEFT_W, router.y, "exit")
    side("out_of_scope_response", LEFT_X, LEFT_W, clar_bottom + 25, "exit")
    y = max(y, boxes["out_of_scope_response"].bottom) + 170

    retrieval_y = y
    y = main("retrieval", y, "evidence") + 60
    y = main("reranker", y, "evidence") + 70
    y = main("grader", y, "evidence")
    kb_bottom = side("knowledge_base", LEFT_X - 20, LEFT_W + 40, retrieval_y, "store")
    side("query_rewriter", LEFT_X, LEFT_W, kb_bottom + 70, "evidence")
    side("research_agent", RIGHT_X, RIGHT_W, retrieval_y, "research")
    if v.expander:
        y = main("context_expander", y + 90, "evidence")
    y = max(y, boxes["research_agent"].bottom, boxes["query_rewriter"].bottom) + 170

    y = main("generator", y, "answer") + 80
    side("no_info_response", RIGHT_X, RIGHT_W, boxes["generator"].y, "exit")
    y = main("hallucination_checker", y, "answer") + 90
    y = main("hitl_gate", y, "approval") + 70
    hitl = boxes["hitl_gate"]
    side("blocked_response", LEFT_X, LEFT_W, hitl.bottom + 10, "blocked")
    main("response_formatter", y, "answer")
    return boxes


def node_svg(b: Box, changed: bool) -> str:
    fill, stroke, title = PALETTE[b.kind]
    outline, accent, width = (CHANGED, CHANGED, 3.5) if changed else (stroke, stroke, 1.7)
    name = "Qdrant knowledge base" if b.name == "knowledge_base" else b.name
    parts = [
        f'<g class="graph-node" data-node="{b.name}"><title>{escape(name)}</title>',
        rect(b.x, b.y, b.w, b.h, fill, outline, 14, width, "8 6" if b.kind == "store" and not changed else ""),
        rect(b.x, b.y, 7, b.h, accent, rx=3),
        text(b.x + 22, b.y + 32, [name], weight=700, fill=title, family=MONO),
        text(b.x + 22, b.y + 65, b.lines),
    ]
    if changed:
        tag = "NEW" if b.name == "context_expander" else "CHANGED"
        tag_w = 64 if tag == "NEW" else 104
        parts += [
            rect(b.right - tag_w - 12, b.y + 12, tag_w, 26, CHANGED, rx=13),
            text(b.right - 12 - tag_w // 2, b.y + 31, [tag], 15, 700, "#FFFFFF", anchor="middle"),
        ]
    parts.append("</g>")
    return "\n".join(parts)


def edge_svg(path: str, color: str, label: list[str] | None = None, at: tuple[int, int] | None = None, dash="") -> str:
    dashed = f' stroke-dasharray="{dash}"' if dash else ""
    marker = "" if dash else ' marker-end="url(#arrow)"'
    out = [
        f'<path d="{path}" fill="none" stroke="#FFFFFF" stroke-width="7" stroke-linejoin="round"/>',
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.6" stroke-linejoin="round"{dashed}{marker}/>',
    ]
    if label and at:
        w = max(len(line) for line in label) * 10.8 + 16
        out.append(rect(at[0], at[1], round(w, 1), 31 + 25 * (len(label) - 1), "#FFFFFF", rx=5))
        out.append(text(at[0] + 7, at[1] + 21, label, 20, fill="#405D71", step=25))
    return "\n".join(out)


def edges(v: Version, b: dict[str, Box], end_y: int) -> list[str]:
    def down(a, c, label=None, color=EDGE["main"]):
        start, stop = b[a].bottom if a in b else a, b[c].y if c in b else c
        at = (CENTER + 18, (start + stop) // 2 - (31 + 25 * (len(label) - 1)) // 2) if label else None
        return edge_svg(f"M{CENTER} {start} V{stop}", color, label, at)

    guard, router, meta = b["guardrails"], b["router"], b["metadata_extractor"]
    retrieval, grader, research = b["retrieval"], b["grader"], b["research_agent"]
    rewriter, kb, gen, hall = b["query_rewriter"], b["knowledge_base"], b["generator"], b["hallucination_checker"]
    hitl, blocked, no_info = b["hitl_gate"], b["blocked_response"], b["no_info_response"]
    clar, oos = b["clarification_response"], b["out_of_scope_response"]
    after_grade = b["context_expander"] if v.expander else gen

    out = [down(345, "rbac_gate"), down("rbac_gate", "guardrails")]
    out.append(
        edge_svg(
            f"M{MAIN_X} {guard.y + 55} H152 V{blocked.y + 60} H{LEFT_X}",
            EDGE["blocked"],
            ["Safety check", "blocked"],
            (161, guard.y + 80),
        )
    )
    if v.metadata_first:
        out += [down("guardrails", "metadata_extractor", ["Clean"]), down("metadata_extractor", "router", ["Continue"])]
        search_from, unavailable = router, ["Requested metadata", "unavailable"]
    else:
        out += [
            down("guardrails", "router", ["Clean"]),
            down("router", "metadata_extractor", ["Question about", "a company"]),
        ]
        search_from, unavailable = meta, ["No usable company", "or year"]
    out.append(
        edge_svg(
            f"M{MAIN_X + MAIN_W} {meta.y + 45} H1810 V{no_info.y - 70} H1700 V{no_info.y}",
            EDGE["main"],
            unavailable,
            (1240, meta.y + 2),
        )
    )
    out.append(
        edge_svg(
            f"M{MAIN_X} {clar.y + 45} H{LEFT_X + LEFT_W}", EDGE["main"], ["Greeting /", "unclear"], (612, clar.y - 40)
        )
    )
    out.append(
        edge_svg(
            f"M{MAIN_X} {router.y + 75} H690 V{oos.y + 45} H{LEFT_X + LEFT_W}",
            EDGE["main"],
            ["Out of scope"],
            (612, oos.bottom + 4),
        )
    )
    out.append(
        edge_svg(
            f"M{MAIN_X + MAIN_W} {search_from.bottom - 30} H{RIGHT_X + 215} V{research.y}",
            EDGE["main"],
            ["Research required"],
            (1220, search_from.bottom - 68),
        )
    )
    out.append(down(search_from.name, "retrieval", ["Simple lookup"]))
    out.append(edge_svg(f"M{kb.right} {kb.y + 60} H{MAIN_X}", "#69AC94", dash="6 6"))
    out.append(down("retrieval", "reranker"))
    out.append(down("reranker", "grader"))
    out.append(
        edge_svg(
            f"M{MAIN_X} {grader.y + 55} H{rewriter.x + 195} V{rewriter.bottom}",
            EDGE["loop"],
            ["Too little evidence;", "retries remain"],
            (rewriter.x + 210, rewriter.bottom + 12),
        )
    )
    out.append(
        edge_svg(
            f"M{rewriter.right} {rewriter.y + 40} H690 V{retrieval.bottom - 30} H{MAIN_X}",
            EDGE["loop"],
            ["Search again"],
            (rewriter.x + 70, rewriter.y - 40),
        )
    )
    out.append(down("grader", after_grade.name, ["Enough relevant", "evidence"]))
    out.append(
        edge_svg(
            f"M{MAIN_X + MAIN_W} {grader.y + 60} H1265 V{no_info.y + 60} H{RIGHT_X}",
            EDGE["main"],
            ["Retry limit", "reached"],
            (1275, no_info.y - 95),
        )
    )
    out.append(
        edge_svg(
            f"M{RIGHT_X} {research.bottom - 40} H1202 V{after_grade.mid} H{MAIN_X + MAIN_W}",
            EDGE["main"],
            ["Evidence", "collected"],
            (1212, research.bottom - 10),
        )
    )
    out.append(
        edge_svg(
            f"M{RIGHT_X + 215} {research.bottom} V{no_info.y}",
            EDGE["main"],
            ["No relevant", "evidence"],
            (RIGHT_X + 230, research.bottom + 30),
        )
    )
    if v.expander:
        out.append(down("context_expander", "generator"))
    out.append(down("generator", "hallucination_checker"))
    out.append(
        edge_svg(
            f"M{MAIN_X} {hall.y + 55} H672 V{gen.y + 55} H{MAIN_X}",
            EDGE["loop"],
            ["Check not passed;", "retries remain"],
            (440, (gen.bottom + hall.y) // 2 - 28),
        )
    )
    out.append(down("hallucination_checker", "hitl_gate", ["Passed OR", "retries exhausted"]))
    out.append(down("hitl_gate", "response_formatter", ["No review needed", "OR approved"], EDGE["approval"]))
    out.append(
        edge_svg(
            f"M{MAIN_X} {hitl.y + 75} H672 V{blocked.y + 100} H{LEFT_X + LEFT_W}",
            EDGE["approval"],
            ["Rejected /", "review unavailable"],
            (420, hitl.y + 90),
        )
    )
    end_mid = end_y + 24
    out.append(down("response_formatter", end_y, color=EDGE["end"]))
    for exit_box in (clar, oos, blocked):
        out.append(edge_svg(f"M{LEFT_X} {exit_box.mid} H80 V{end_mid} H{CENTER - 76}", EDGE["end"]))
    out.append(edge_svg(f"M{no_info.right} {no_info.mid} H1850 V{end_mid} H{CENTER + 76}", EDGE["end"]))
    return out


def header(v: Version, main: bool) -> list[str]:
    eyebrow = "FINANCIAL RESEARCH ASSISTANT"
    if main:
        title, subtitle = (
            "The graph, from question to release",
            "Actual node names · every branch · both retry loops · Admin review",
        )
    else:
        eyebrow += f" · VERSION {v.number} OF {VERSIONS[-1].number}"
        title, subtitle = v.title, v.subtitle
    out = [
        rect(0, 0, WIDTH, 166, "#102B40", rx=0),
        text(66, 48, [eyebrow], 19, 700, "#9BD8CA"),
        text(66, 101, [title], 43, 700, "#FFFFFF"),
        text(66, 137, [subtitle], 22, 400, "#D0DFE8"),
    ]
    if main or not v.metric:
        out += [
            text(1830, 77, ["18 NODES"], 24, 700, "#FFFFFF", anchor="end"),
            text(1830, 112, ["+ START / END"], 18, 400, "#C4D6E2", anchor="end"),
        ]
    else:
        out += [
            text(1830, 88, [v.metric[0]], 40, 700, "#FFFFFF", anchor="end"),
            text(1830, 124, [v.metric[1]], 18, 400, "#C4D6E2", anchor="end"),
        ]
    return out


def bands(b: dict[str, Box], v: Version) -> list[str]:
    intake_bottom = max(b["metadata_extractor"].bottom, b["router"].bottom, b["out_of_scope_response"].bottom) + 40
    evidence_top = b["retrieval"].y - 75
    evidence_last = b["context_expander"] if v.expander else b["grader"]
    evidence_bottom = max(evidence_last.bottom, b["research_agent"].bottom, b["query_rewriter"].bottom) + 70
    answer_top = b["generator"].y - 70
    answer_bottom = max(b["response_formatter"].bottom, b["blocked_response"].bottom) + 40
    out = []
    for top, bottom, fill, number, label in (
        (352, intake_bottom, "#F7F9FD", "01", "UNDERSTAND THE QUESTION"),
        (evidence_top, evidence_bottom, "#F2F9F6", "02", "GATHER EVIDENCE"),
        (answer_top, answer_bottom, "#F7F8FC", "03", "DRAFT, VERIFY, RELEASE"),
    ):
        out += [
            rect(46, top, 1808, bottom - top, fill, rx=20),
            text(178, top + 32, [number], 20, 700, "#57738A"),
            text(224, top + 32, [label], 18, 700, "#57738A"),
        ]
    return out


def results(v: Version, top: int) -> tuple[list[str], int]:
    out = [
        rect(46, top, 1808, 190, "#F1F5F8", rx=20),
        text(78, top + 42, ["MEASURED IN THIS VERSION"], 18, 700, "#33546B"),
    ]
    width = (1808 - 64 - 24 * (len(v.results) - 1)) // len(v.results)
    for i, (value, caption) in enumerate(v.results):
        x = 78 + i * (width + 24)
        out += [
            rect(x, top + 62, width, 102, "#FFFFFF", "#D5E0E8", 12),
            text(x + 22, top + 108, [value], 34, 700, "#102B40"),
            text(x + 22, top + 142, [caption], 19, 400, "#4C6577"),
        ]
    return out, top + 190


def build(v: Version, main: bool = False) -> tuple[str, int]:
    boxes = layout(v, live=main)
    end_y = max(boxes["response_formatter"].bottom, boxes["blocked_response"].bottom) + 110
    total = end_y + 48 + 60
    result_parts: list[str] = []
    if not main and v.results:
        result_parts, bottom = results(v, end_y + 90)
        total = bottom + 50
    redaction = f"redact personal data ({v.redaction})"
    before_start = (
        LIVE_BEFORE_START
        if main
        else f"JWT + conversation ownership → parse /escalate → {redaction} → prepare state and trace"
    )
    before = [
        rect(
            62,
            194,
            1776,
            74,
            "#F1F5F8",
            CHANGED if "before_start" in v.changed and not main else "none",
            12,
            3.5 if "before_start" in v.changed and not main else 1.7,
        ),
        text(86, 224, ["BEFORE START · FASTAPI"], 17, 700, "#33546B"),
        text(
            86,
            251,
            [before_start],
            22 if not main else 20,
            400,
            "#344D60",
        ),
    ]
    changed = set() if main else v.changed
    name = "LangGraph workflow" if main else f"version {v.number}, {v.title}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {total}" '
        f'width="{WIDTH}" height="{total}" role="img">',
        f"<title>Financial Research Assistant: {escape(name)}</title>",
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
        'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="context-stroke"/></marker></defs>',
        rect(0, 0, WIDTH, total, "#FFFFFF", rx=0),
        *header(v, main),
        *before,
        *bands(boxes, v),
        *edges(v, boxes, end_y),
        '<g class="graph-node" data-node="__start__">',
        rect(CENTER - 76, 298, 152, 46, "#102B40", "#102B40", 22),
        text(CENTER, 328, ["START"], 21, 700, "#FFFFFF", anchor="middle"),
        "</g>",
        *(node_svg(box, name in changed) for name, box in boxes.items()),
        '<g class="graph-node" data-node="__end__">',
        rect(CENTER - 76, end_y, 152, 48, "#102B40", "#102B40", 22),
        text(CENTER, end_y + 31, ["END"], 21, 700, "#FFFFFF", anchor="middle"),
        "</g>",
        text(109, end_y + 82, ["Completed responses → END"], 19, 400, "#6E8495"),
        *result_parts,
    ]
    if changed:
        parts.append(
            text(1830, end_y + 82, ["Orange outline: changed in this version"], 19, 700, CHANGED, anchor="end")
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n", total


def mcp_access() -> tuple[str, int]:
    """Two ways into the knowledge base: the in-app assistant and an employee's own assistant over MCP."""
    w = 1600
    left, right, col = 90, 830, 680

    def box(x, y, bw, title, lines, kind, title_size=24):
        fill, stroke, ink = PALETTE[kind]
        bh = 74 + 31 * len(lines)
        return bh, [
            rect(x, y, bw, bh, fill, stroke, 14, 2),
            text(x + 26, y + 42, [title], title_size, 700, ink),
            text(x + 26, y + 80, lines, 20, 400, "#344F60", step=31),
        ]

    parts = [
        text(left, 62, ["Two ways to use the knowledge base"], 34, 700, "#102B40"),
        text(left, 100, ["Same search, same reranker, same role permissions"], 22, 400, "#5A7385"),
    ]
    _, a = box(left, 140, col, "Employee in the app", ["React interface · login token"], "intake")
    _, b = box(right, 140, col, "Employee's own assistant", ["Claude Code, Claude Desktop · MCP token"], "intake")
    h1, c = box(
        left,
        290,
        col,
        "In-app assistant (LangGraph)",
        [
            "Screens questions: private data, prompt injection",
            "Routes, sets company and year filters, researches",
            "Writes the answer, with an exact calculator",
            "Checks the answer against the sources",
            "Holds escalated access for Admin approval",
        ],
        "answer",
    )
    h2, d = box(
        right,
        290,
        col,
        "MCP server at /mcp",
        [
            "Checks the token: search-only, user's own role",
            "Rate limit per user · audit log of every call",
            "Tools: list_documents · search_documents · read_pages",
            "The assistant decides what to search and",
            "writes the answer itself",
        ],
        "approval",
    )
    core_y = 290 + max(h1, h2) + 80
    hc, e = box(
        left,
        core_y,
        right + col - left,
        "Shared search core",
        ["Role filter (document type, confidentiality) → hybrid search (meaning + keywords) → reranker → ranked pages"],
        "evidence",
    )
    store_y = core_y + hc + 70
    hs, f = box(
        left + 330,
        store_y,
        right + col - left - 660,
        "Qdrant knowledge base",
        ["84 public filings · 32 companies"],
        "store",
    )
    arrows = []
    for x, h in ((left + col // 2, h1), (right + col // 2, h2)):
        arrows.append(edge_svg(f"M{x} 245 L{x} 288", EDGE["main"]))
        arrows.append(edge_svg(f"M{x} {290 + h} L{x} {core_y - 2}", EDGE["main"]))
    mid = left + (right + col - left) // 2
    arrows.append(edge_svg(f"M{mid} {core_y + hc} L{mid} {store_y - 2}", EDGE["loop"]))
    total = store_y + hs + 60
    head = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {total}" width="{w}" height="{total}" role="img">',
        "<title>Financial Research Assistant: two ways to use the knowledge base</title>",
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
        'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="context-stroke"/></marker></defs>',
        rect(0, 0, w, total, "#FFFFFF", rx=0),
    ]
    return "\n".join(head + parts + a + b + c + d + e + f + arrows + ["</svg>"]) + "\n", total


def main() -> None:
    svg, total = mcp_access()
    (OUT / "mcp-access.svg").write_text(svg)
    print(f"mcp-access.svg 1600x{total}")
    (OUT / "versions").mkdir(parents=True, exist_ok=True)
    svg, total = build(CURRENT, main=True)
    (OUT / "langgraph-workflow.svg").write_text(svg)
    print(f"langgraph-workflow.svg {WIDTH}x{total}")
    for version in VERSIONS:
        svg, total = build(version)
        (OUT / "versions" / f"{version.slug}.svg").write_text(svg)
        print(f"versions/{version.slug}.svg {WIDTH}x{total}")


if __name__ == "__main__":
    main()
