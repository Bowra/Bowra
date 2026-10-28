"""Verify narrative supersession from stream accumulation through done fields.

The path runs from the context's narrative text through composition and the
superseded marker to the terminal payload. Guard and renderer tests alone cannot
prove this wiring is active.
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.agents.lead_orchestrator import (
    _compose_turn_output,
    _narrative_superseded_fields,
)

# Synthetic conflicting narrative and tool values.
_NARRATIVE_FABRICATED = "三个核心结论已经精确算出：斜率 b = 50.629 箱/月，拟合优度 R² = 0.841，明年 1 月点预测约为 3,101 箱。"
_EXEC_SUMMARY_GROUNDED = "斜率 b = 62.937 箱/月，R² = 0.780，t = 6.165，预测 3165.758 箱。"


def _ctx(narrative: str) -> SimpleNamespace:
    return SimpleNamespace(
        _lead_narrative_text=narrative,
        canvas_store=None,  # Short-circuit composer; this test concerns only this guard section
        _last_nested_invocations=[],
        member_uid="",
        language_context=None,
        artifact_store=None,
        response_kind_pre_flight=None,
    )


def _result(observed_numbers: list[float]) -> SimpleNamespace:
    """一次 tool 调用，结果里含 peer 真实算出的那组数（build_observed_numbers 从中抽取）。"""
    return SimpleNamespace(
        tool_invocations=[
            SimpleNamespace(
                tool_name="subagent_spawn",
                result={"values": observed_numbers},
                nested=[],
                source=None,
                arguments={},
            )
        ],
        intent=None,
        final_answer=_EXEC_SUMMARY_GROUNDED,
        duration_ms=1,
        tokens_input=1,
        tokens_output=1,
    )


async def _run(narrative: str) -> SimpleNamespace:
    ctx = _ctx(narrative)
    runtime = SimpleNamespace(parent_ctx=ctx, parent_bus=SimpleNamespace(), router=None, session_store=None)
    sctx = SimpleNamespace(turn_id="t1", session_id="s1", workspace_id="w1", user_id="u1", prior_turn_status=None)
    await _compose_turn_output(
        runtime=runtime,  # type: ignore[arg-type]
        sctx=sctx,  # type: ignore[arg-type]
        result=_result([62.937, 1487.576, 0.78, 6.165, 3165.758]),
        aggregated_payload={"exec_summary": _EXEC_SUMMARY_GROUNDED},
        user_text="帮我拟合回归",
        peer_payloads=[],
    )
    return ctx


async def test_fabricated_narrative_sets_superseded_and_reaches_done_payload() -> None:
    """Unsupported narrative numbers set the marker and reach the done payload."""
    ctx = await _run(_NARRATIVE_FABRICATED)
    assert getattr(ctx, "_lead_narrative_superseded", False) is True
    assert _narrative_superseded_fields(ctx) == {"narrative_superseded": True}


async def test_grounded_narrative_leaves_done_payload_untouched() -> None:
    """narrative 数字有据 → 不标记，done 保持原 wire 形状（不带该键）。"""
    ctx = await _run("斜率 b = 62.937 箱/月，R² = 0.780，预测 3165.758 箱。")
    assert getattr(ctx, "_lead_narrative_superseded", False) is False
    assert _narrative_superseded_fields(ctx) == {}


async def test_empty_narrative_never_supersedes() -> None:
    """Lead 没写 narrative（纯工具轮）→ 不该凭空标记。"""
    ctx = await _run("")
    assert getattr(ctx, "_lead_narrative_superseded", False) is False
    assert _narrative_superseded_fields(ctx) == {}


def test_superseded_fields_omits_key_when_absent() -> None:
    """老 turn / 无该属性的 ctx → 空 dict（wire 形状不变）。"""
    assert _narrative_superseded_fields(SimpleNamespace()) == {}
    assert _narrative_superseded_fields(None) == {}


# Narrative and final-answer prose must use the same restatement criterion.
# Suppressing only the narrative would leave an incorrect final answer visible.

_DRIFTED_CHAT = "线性回归结果：斜率 62.622，截距 1475.394，R² = 0.831，明年1月点预测 3,149 箱。"
_GROUNDED_EXEC = "斜率 62.937 箱/月，R² = 0.770，预测明年1月 3165.758 箱。"


def _synthetic_payload() -> dict:
    return {
        "exec_summary": _GROUNDED_EXEC,
        "kpis": [
            {"label": "斜率", "value": "62.937 箱/月"},
            {"label": "R²", "value": "0.770"},
            {"label": "预测", "value": "3165.758 箱"},
        ],
    }


async def test_drifted_final_answer_is_swapped_to_grounded_exec_summary() -> None:
    """final_answer 复述失真 → 换成有据的 exec_summary（与 narrative 同一判据）。"""
    ctx = _ctx(_DRIFTED_CHAT)
    runtime = SimpleNamespace(parent_ctx=ctx, parent_bus=SimpleNamespace(), router=None, session_store=None)
    sctx = SimpleNamespace(turn_id="t2", session_id="s2", workspace_id="w1", user_id="u1", prior_turn_status=None)
    # ⚠ observed must be **rich enough that the old criterion does not fire**; otherwise this tests the old criterion, not repetition.
    # (Testing lesson: with sparse observed, is_chat_text_fabricated replaced the text itself;
    # removing the new criterion still left the test green = a false pass.) Each chat number here has a source within 2%.
    result = _result([62.937, 1487.576, 0.83, 3165.758, 13, 95, 1])
    result.final_answer = _DRIFTED_CHAT

    _kinds, text_for_chat = await _compose_turn_output(
        runtime=runtime,  # type: ignore[arg-type]
        sctx=sctx,  # type: ignore[arg-type]
        result=result,
        aggregated_payload=_synthetic_payload(),
        user_text="拟合回归",
        peer_payloads=[],
    )

    assert "62.622" not in text_for_chat, text_for_chat
    assert "0.831" not in text_for_chat
    assert "62.937" in text_for_chat


async def test_faithful_final_answer_is_left_alone() -> None:
    """忠实复述不得被换掉（换掉 = 用户少看到 Lead 的组织，误伤代价高）。"""
    faithful = "线性回归：斜率约 62.94 箱/月，R² = 0.770，预测明年1月约 3166 箱。"
    ctx = _ctx("")
    runtime = SimpleNamespace(parent_ctx=ctx, parent_bus=SimpleNamespace(), router=None, session_store=None)
    sctx = SimpleNamespace(turn_id="t3", session_id="s3", workspace_id="w1", user_id="u1", prior_turn_status=None)
    result = _result([62.937, 0.770, 3165.758])
    result.final_answer = faithful

    _kinds, text_for_chat = await _compose_turn_output(
        runtime=runtime,  # type: ignore[arg-type]
        sctx=sctx,  # type: ignore[arg-type]
        result=result,
        aggregated_payload=_synthetic_payload(),
        user_text="拟合回归",
        peer_payloads=[],
    )

    assert "62.94" in text_for_chat
