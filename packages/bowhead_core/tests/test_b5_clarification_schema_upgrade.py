"""bi-hitl-cleanup P5 / B5 tests — clarification_ask schema upgrade.

Per tech-design §P5 + PRD r2.1 §4 B5:
- Schema accepts NEW `questions: list[Question]` shape (preferred, B5)
- ALSO accepts legacy `candidates: list[str]` (backward-compat)
- New `Question.type` enum: single_select / multi_select / free_text（`rank_priorities`
  2026-09-07 摘除，见 test_rank_priorities_removed_from_schema）
- `make_clarification_sentinel` adds `type: str` param (default 'single_select')
- Tool handler: questions normalizes to flat candidates + preserves all
  questions on sentinel; store.put
  `multi_select` toggles by type; sentinel carries `type` field
- FE V4ClarificationCard NOT updated (per P0-SRE-1 close: zero FE diff)
  ⚠ 原文这里写「rank_priorities downgraded to single_select at FE」——**与代码不符**：
  前端的 `single` 判据是 `type === "single_select"`，所以 rank_priorities 落的是
  **复选组**分支（序被丢掉）。2026-09-07 已从 schema 摘除并在后端显式映射为
  multi_select，渲染与此前一致。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from bowhead_core.agents.clarification_tool import (
    _clarification_ask,
    _clarification_ask_schema,
    extract_clarification_event_payload,
    is_clarification_sentinel,
    make_clarification_sentinel,
)

# ── Schema shape ────────────────────────────────────────────────────────


def test_b5_schema_has_questions_field() -> None:
    """New B5 input field present."""
    schema = _clarification_ask_schema()
    props = schema.input_schema["properties"]
    assert "questions" in props
    questions_def = props["questions"]
    assert questions_def["type"] == "array"
    assert questions_def["minItems"] == 1
    assert questions_def["maxItems"] == 3


def test_b5_schema_question_item_has_3_fields() -> None:
    """Each Question = {question, options, type}."""
    schema = _clarification_ask_schema()
    qdef = schema.input_schema["properties"]["questions"]
    item = qdef["items"]
    item_props = item["properties"]
    assert set(item_props.keys()) == {"question", "options", "type"}
    # question + options required (type defaults to single_select)
    assert "question" in item["required"]
    assert "options" in item["required"]


def test_b5_schema_type_enum_3_values() -> None:
    """Choice cards and a question with a typed answer use distinct contracts."""
    schema = _clarification_ask_schema()
    type_def = schema.input_schema["properties"]["questions"]["items"]["properties"]["type"]
    assert set(type_def["enum"]) == {"single_select", "multi_select", "free_text"}
    assert schema.input_schema["properties"]["questions"]["items"]["properties"]["options"]["minItems"] == 0
    assert type_def["default"] == "single_select"


def test_rank_priorities_removed_from_schema_and_description() -> None:
    """模型不该再看到一个前端渲不出来的格式（宣称支持 → 静默降级 → 丢序）。"""
    schema = _clarification_ask_schema()
    assert "rank_priorities" not in json.dumps(schema.input_schema)
    assert "rank_priorities" not in schema.description


def test_b5_schema_legacy_candidates_field_preserved() -> None:
    """Backward-compat: `candidates` still accepted."""
    schema = _clarification_ask_schema()
    props = schema.input_schema["properties"]
    assert "candidates" in props
    cand_def = props["candidates"]
    assert cand_def["type"] == "array"


def test_b5_schema_description_documents_both_shapes() -> None:
    """SCHEMA section in description shows both new + legacy shapes."""
    desc = _clarification_ask_schema().description
    assert "SCHEMA (preferred, B5" in desc
    assert "LEGACY SCHEMA (backward-compat" in desc
    # New shape pseudocode
    assert "questions: list[Question]" in desc
    assert "single_select" in desc and "multi_select" in desc


def test_b5_schema_anyof_gate_present() -> None:
    """P9 Eng-P1 fix: schema must require at least one of {questions,
    candidates, fallback_text} so LLM emitting `clarification_ask({})`
    fails validation instead of producing an empty card."""
    schema = _clarification_ask_schema()
    any_of = schema.input_schema.get("anyOf")
    assert isinstance(any_of, list) and len(any_of) >= 3
    required_sets = {tuple(sorted(c.get("required", []))) for c in any_of}
    assert ("questions",) in required_sets
    assert ("candidates",) in required_sets
    assert ("fallback_text",) in required_sets


@pytest.mark.asyncio
async def test_b5_handler_rejects_empty_args() -> None:
    """P11 final review fix: handler-level reject mirrors schema anyOf gate
    (registry._validate_jsonschema doesn't run anyOf at runtime — only `type` +
    `additionalProperties`). Empty args returns schema_invalid sentinel, NOT a
    boilerplate-text card."""
    ctx = SimpleNamespace(turn_id="t_b5_empty")
    result = await _clarification_ask({}, ctx)
    # Not a clarification sentinel — handler refused
    assert not is_clarification_sentinel(result)
    assert result.get("error_kind") == "schema_invalid"
    assert "at least one of" in result.get("error_message", "")


@pytest.mark.asyncio
async def test_b5_handler_accepts_fallback_text_only() -> None:
    """fallback_text alone is sufficient (per schema anyOf) — no questions/
    candidates needed when LLM has a free-form prompt to user."""
    ctx = SimpleNamespace(turn_id="t_b5_fb")
    result = await _clarification_ask(
        {"fallback_text": "请换种问法?"},
        ctx,
    )
    # Still produces a sentinel (degraded shape — 0 candidates, just text)
    assert is_clarification_sentinel(result)
    assert result["fallback_text"] == "请换种问法?"


# ── make_clarification_sentinel signature ─────────────────────────────


def test_b5_sentinel_maker_accepts_type_param() -> None:
    """B5: make_clarification_sentinel adds `type` param (backward-compat default)."""
    # Backward-compat call (no type) — defaults to single_select
    s1 = make_clarification_sentinel(
        candidates=["a", "b"],
        fallback_text=None,
        aborted_turn_id="t_x",
    )
    assert s1["type"] == "single_select"

    # Explicit type
    s2 = make_clarification_sentinel(
        candidates=["a", "b", "c"],
        fallback_text=None,
        aborted_turn_id="t_x",
        type="multi_select",
    )
    assert s2["type"] == "multi_select"

    # rank_priorities is an **existing value** (old prompt caches / database rows); normalize it to multi_select,
    # never single_select: the frontend renders non-single types as checkbox groups, so mapping to multi
    # preserves rendering byte for byte; single would change "check several, then confirm" into "click once to submit".
    s3 = make_clarification_sentinel(
        candidates=["a", "b"],
        fallback_text=None,
        aborted_turn_id="t_x",
        type="rank_priorities",
    )
    assert s3["type"] == "rank_priorities"  # This function only passes it through; handler/normalize performs normalization.


def test_b5_sentinel_still_passes_is_clarification_sentinel_check() -> None:
    """B3 detector still recognizes B5 sentinel."""
    s = make_clarification_sentinel(
        candidates=["a", "b"],
        fallback_text=None,
        aborted_turn_id="t_x",
        type="multi_select",
    )
    assert is_clarification_sentinel(s)


# ── Handler: new shape parsing ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_b5_handler_accepts_questions_shape() -> None:
    """New shape: question options → candidates, .type → sentinel type."""
    ctx = SimpleNamespace(turn_id="t_b5_1")
    args = {
        "questions": [
            {
                "question": "客单价用哪个公式?",
                "options": ["GMV/订单数", "净销售额/订单数"],
                "type": "single_select",
            },
        ],
    }
    result = await _clarification_ask(args, ctx)
    assert is_clarification_sentinel(result)
    assert result["candidates"] == ["GMV/订单数", "净销售额/订单数"]
    assert result["type"] == "single_select"


@pytest.mark.asyncio
async def test_missing_numeric_value_uses_free_text_question_without_fake_choices() -> None:
    ctx = SimpleNamespace(turn_id="t_missing_equity")
    question = {"question": "请补充平均所有者权益是多少？", "options": [], "type": "free_text"}
    result = await _clarification_ask({"questions": [question]}, ctx)
    assert is_clarification_sentinel(result)
    assert result["questions"] == [question]
    assert result["candidates"] == []
    assert result["type"] == "free_text"


@pytest.mark.asyncio
async def test_b5_handler_preserves_all_questions_shape() -> None:
    """B5 multi-question payloads must not collapse to questions[0]."""
    ctx = SimpleNamespace(turn_id="t_b5_multi")
    args = {
        "questions": [
            {
                "question": "主要看哪个区域?",
                "options": ["全国", "华东"],
                "type": "single_select",
            },
            {
                "question": "重点看哪个指标?",
                "options": ["销售额", "利润率"],
                "type": "single_select",
            },
        ],
    }
    result = await _clarification_ask(args, ctx)
    assert is_clarification_sentinel(result)
    assert result["candidates"] == ["全国", "华东", "销售额", "利润率"]
    assert result["questions"] == args["questions"]
    assert result["fallback_text"] == "请选择一个最关键的方向继续"


@pytest.mark.asyncio
async def test_b5_handler_carries_multi_select_type() -> None:
    """multi_select type carried through to sentinel."""
    ctx = SimpleNamespace(turn_id="t_b5_2")
    args = {
        "questions": [
            {
                "question": "选 ≥2 个想看的维度",
                "options": ["销售", "成本", "库存", "客流"],
                "type": "multi_select",
            },
        ],
    }
    result = await _clarification_ask(args, ctx)
    assert result["type"] == "multi_select"
    assert len(result["candidates"]) == 4


@pytest.mark.asyncio
async def test_b5_handler_maps_legacy_rank_priorities_to_multi_select() -> None:
    """存量 rank_priorities → multi_select（**不是** single_select）。

    这是本次摘除的行为陷阱：只从白名单删掉它会掉进默认分支变 single_select，
    多选卡就变成「点一下就提交」——行为回归。
    """
    ctx = SimpleNamespace(turn_id="t_b5_3")
    args = {
        "questions": [
            {
                "question": "按重要性排序",
                "options": ["销售额", "毛利", "客流"],
                "type": "rank_priorities",
            },
        ],
    }
    result = await _clarification_ask(args, ctx)
    assert result["type"] == "multi_select"
    assert result["questions"][0]["type"] == "multi_select"


@pytest.mark.asyncio
async def test_b5_handler_invalid_type_falls_back_to_single_select() -> None:
    """Defensive: unknown type values normalize to single_select."""
    ctx = SimpleNamespace(turn_id="t_b5_4")
    args = {
        "questions": [
            {
                "question": "test",
                "options": ["a", "b"],
                "type": "made_up_type",
            },
        ],
    }
    result = await _clarification_ask(args, ctx)
    assert result["type"] == "single_select"


@pytest.mark.asyncio
async def test_b5_handler_uses_question_text_as_fallback_when_no_explicit_fallback() -> None:
    """If LLM gave `question` text but no `fallback_text`, use question as fallback."""
    ctx = SimpleNamespace(turn_id="t_b5_5")
    args = {
        "questions": [
            {
                "question": "客单价的定义?",
                "options": ["GMV/订单", "净销/订单"],
                "type": "single_select",
            },
        ],
    }
    result = await _clarification_ask(args, ctx)
    assert result["fallback_text"] == "客单价的定义?"


# ── Handler: legacy backward-compat ────────────────────────────────────


@pytest.mark.asyncio
async def test_b5_legacy_candidates_shape_still_works() -> None:
    """Pre-B5 callers use `candidates: list[str]` — must still work."""
    ctx = SimpleNamespace(turn_id="t_b5_legacy_1")
    args = {
        "candidates": ["GMV同比", "净销售同比", "毛利同比"],
    }
    result = await _clarification_ask(args, ctx)
    assert is_clarification_sentinel(result)
    assert result["candidates"] == ["GMV同比", "净销售同比", "毛利同比"]
    # Default type for legacy callers
    assert result["type"] == "single_select"


@pytest.mark.asyncio
async def test_b5_questions_takes_precedence_over_candidates() -> None:
    """If both supplied, `questions` wins (new shape preferred)."""
    ctx = SimpleNamespace(turn_id="t_b5_both_1")
    args = {
        "questions": [
            {
                "question": "选公式",
                "options": ["new_a", "new_b"],
                "type": "single_select",
            },
        ],
        "candidates": ["legacy_a", "legacy_b", "legacy_c"],
    }
    result = await _clarification_ask(args, ctx)
    # New shape candidates win
    assert result["candidates"] == ["new_a", "new_b"]


# ── extract_clarification_event_payload unchanged for B5 ───────────────


def test_b5_sentinel_extract_still_works() -> None:
    """extract_clarification_event_payload reads sentinel — unchanged path."""
    s = make_clarification_sentinel(
        candidates=["a", "b"],
        fallback_text="question?",
        aborted_turn_id="t_x",
        clarification_id="cid_1",
        type="multi_select",
        questions=[
            {
                "question": "q1?",
                "options": ["a", "b"],
                "type": "multi_select",
            }
        ],
    )
    payload = extract_clarification_event_payload(s)
    assert payload["candidates"] == ["a", "b"]
    assert payload["fallback_text"] == "question?"
    assert payload["clarification_id"] == "cid_1"
    assert payload["questions"] == [
        {
            "question": "q1?",
            "options": ["a", "b"],
            "type": "multi_select",
        }
    ]
