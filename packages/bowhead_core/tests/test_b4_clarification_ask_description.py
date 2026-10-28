"""bi-hitl-cleanup P4 / B4 tests — clarification_ask description rewrite.

Per tech-design §P4 + PRD r2.1 §4 B4 + 之前讨论:
- Description self-contained ~50 lines (was 9 lines, externalized to lead.py)
- Source-agnostic (NOT BI-specific wording — primitive serves any source)
- Anthropic ask_user_input_v0 style: WHEN TO USE / CRITICAL — WHEN NOT TO USE /
  OUTPUT FORMAT / SCHEMA / EXAMPLE
- General instruction + 2 BI anchor (NOT堆 4+ examples per past PRD lesson)
- CRITICAL硬措辞 + "DO NOT fabricate" anti-hallucination guardrail
- No reference to lead.py _TOOL_USE_GUIDANCE (description must be self-contained)

P4 is description-only; schema (input_schema) upgrade is P5 — kept unchanged here.
"""

from __future__ import annotations

from bowhead_core.agents.clarification_tool import _clarification_ask_schema


def _desc() -> str:
    return _clarification_ask_schema().description


# ── Self-contained (no external reference) ─────────────────────────────


def test_b4_description_no_external_reference() -> None:
    """Description must NOT reference `lead.py _TOOL_USE_GUIDANCE` —
    self-containment per Anthropic skill-creator pattern.
    """
    d = _desc()
    assert "_TOOL_USE_GUIDANCE" not in d
    assert "lead.py" not in d


def test_b4_description_substantial_size() -> None:
    """Description must be substantial (was 9 lines pre-rewrite + externalized
    to lead.py), but not bloated. Tech-design target ~50 lines / ~50% dense
    content — relax to ≥30 lines + ≥1500 chars since dense self-contained
    content beats line-count padding.

    上限 4000 → 4500（2026-09-30 用户拍板）：`064720cf3` 新增澄清类型
    `free_text`（缺数值时让用户自由输入），描述里补了该类型的用法、
    `options=[]` 约定与 schema 取值，合计约 4100 字符。上限只防膨胀，
    按新类型所需的篇幅放宽，不是放任继续变长。"""
    d = _desc()
    line_count = d.count("\n")
    assert line_count >= 30, f"Description has {line_count} lines, need ≥30"
    assert len(d) >= 1500, f"Description is {len(d)} chars, too short"
    assert len(d) <= 4500, f"Description is {len(d)} chars, too bloated"


# ── Anthropic ask_user_input_v0 structure ──────────────────────────────


def test_b4_has_when_to_use_section() -> None:
    d = _desc()
    assert "WHEN TO USE" in d


def test_b4_has_critical_when_not_to_use_section() -> None:
    """The CRITICAL anti-section per ask_user_input_v0 (强措辞)."""
    d = _desc()
    assert "CRITICAL" in d
    assert "WHEN NOT TO USE" in d


def test_b4_has_output_format_section() -> None:
    d = _desc()
    assert "OUTPUT FORMAT" in d


def test_b4_has_schema_and_example() -> None:
    d = _desc()
    assert "SCHEMA" in d
    assert "EXAMPLE" in d
    # Example actually demonstrates a call
    assert "clarification_ask(" in d


# ── Source-agnostic (cross-source primitive) ──────────────────────────


def test_b4_no_bi_specific_framing() -> None:
    """Description must NOT lock the primitive into BI-only wording (per
    discovery §4.4 L0 generic). It can use BI anchor examples but the framing
    must be source-agnostic ('clarifying question', 'ambiguous', 'workspace data'
    — not 'BI 概念歧义' as the primary descriptor)."""
    d = _desc()
    # NO hardcoded source name in the OPENING framing (lines before WHEN TO USE)
    intro = d.split("WHEN TO USE")[0]
    # Tag-line should say "ambiguous", "request", "the user picks" — not "BI"
    # 5dffe7387 rewrote the opening as "required value is missing ... consequential ambiguity",
    # retaining source-independent ambiguity/missing-value framing.
    assert any(term in intro.lower() for term in ("ambiguous", "ambiguity", "clarifying"))
    # Source-agnostic terms in intro
    assert "BI" not in intro, "Tool description opening framing must be source-agnostic — no 'BI' in intro before WHEN TO USE block"


def test_b4_check_conversation_first_rule() -> None:
    """The CRITICAL anti-pattern: don't re-ask what user already said."""
    d = _desc()
    # Firm Anthropic-style wording.
    assert "CHECK CONVERSATION FIRST" in d or "already gave" in d


# ── 2 BI anchor only (not 4+ enumerated cases) ─────────────────────────


def test_b4_two_bi_anchor_examples_present() -> None:
    """Per PRD r2.1 §4 B4 + 上 session 教训: 2 BI anchor examples (not 4+
    enumerated case list). Anchor examples are concrete + workspace-grounded."""
    d = _desc()
    # 2 anchor terms (typical BI ambiguity patterns)
    assert "客单价" in d  # derived metric anchor
    # Either '重点' (broad workspace term anchor) or another BI ambiguity
    # General instruction + 2 anchor IS the goal — exact count ≤3 anchors total
    anchor_terms = ["客单价", "重点", "万达店", "国美店", "华东", "广州"]
    anchor_hits = sum(1 for t in anchor_terms if t in d)
    assert 1 <= anchor_hits <= 3, (
        f"Description has {anchor_hits} anchor terms — should be 1-3 (general instruction + small anchor set, NOT enumerated cases)"
    )


def test_b4_anti_hallucination_guardrail() -> None:
    """DO NOT fabricate specific values rule (preserved from old description,
    important anti-hallucination guard)."""
    d = _desc()
    # The rule against fabricating specific dates / Top-N / brand names / %
    assert "DO NOT" in d or "Don't" in d or "不要" in d or "严禁" in d
    # Specific anti-patterns called out (sample 2)
    fabrication_examples = ["Top", "%", "同比", "2026"]
    assert any(t in d for t in fabrication_examples), (
        "Description should call out specific anti-hallucination examples (specific dates / percentages / Top-N)"
    )


# ── Schema unchanged (P4 is description-only; P5 will upgrade schema) ──


def test_b4_input_schema_candidates_field_present() -> None:
    """P4 doesn't remove the candidates field. (P5 later upgrades to add
    `questions` field AND removes top-level `required` to accept either shape;
    candidates remains as backward-compat input.)"""
    schema = _clarification_ask_schema()
    assert schema.input_schema["properties"].get("candidates") is not None


# ── Pattern over case lists (lesson against piling up examples) ──────────────────


def test_b4_general_instruction_over_case_enumeration() -> None:
    """Past lesson (P2.1 + PRD r1.1): general-instruction-with-1-2-anchor
    beats堆 4+ enumerated cases. WHEN TO USE block should describe a PATTERN
    (when ambiguity needs user input), not a case list."""
    d = _desc()
    # Find WHEN TO USE block boundary
    wtu_start = d.find("WHEN TO USE")
    wtu_end = d.find("CRITICAL")
    assert wtu_start >= 0 and wtu_end > wtu_start
    wtu_block = d[wtu_start:wtu_end]
    # WHEN TO USE should be pattern-form: "Trigger when ALL of:" style + bullets
    # NOT 4+ enumerated cases like "店名 / 区域 / metric / 城市"
    # 5dffe7387 rewrote WHEN TO USE as two general conditions (missing required value / multiple defensible interpretations).
    pattern_markers = ("Trigger when", "Genuinely ambiguous", "where the right answer", "multiple defensible interpretations")
    has_pattern_language = any(marker in wtu_block for marker in pattern_markers)
    assert has_pattern_language, "WHEN TO USE should be pattern-form (general instruction), not enumerated cases"
