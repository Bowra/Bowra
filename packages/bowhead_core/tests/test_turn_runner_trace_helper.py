"""chat-hydrate-overhaul Round 5: _invocations_to_trace_rows 展平 + subagent_id 测."""

from __future__ import annotations

from bowhead_core.turn_runner import _invocations_to_trace_rows
from bowhead_core.types import IntentType, ToolInvocation


def _inv(
    name: str, call_id: str, *, error: str | None = None, result: str = "ok", nested: list[ToolInvocation] | None = None, duration_ms: int = 100
) -> ToolInvocation:
    return ToolInvocation(
        tool_name=name,
        arguments={},
        call_id=call_id,
        result=result if not error else None,
        error=error,
        duration_ms=duration_ms,
        nested=nested or [],
    )


def test_no_invocations_only_classify():
    rows = _invocations_to_trace_rows(
        turn_id="t1",
        intent=IntentType.LOOKUP,
        invocations=[],
    )
    assert len(rows) == 1
    assert rows[0].step_id == "classify"
    assert rows[0].kind == "success"
    assert rows[0].subagent_id is None


def test_top_level_only():
    rows = _invocations_to_trace_rows(
        turn_id="t1",
        intent=IntentType.LOOKUP,
        invocations=[
            _inv("bi.fetch", "tl_a"),
            _inv("bi_attribute", "tl_b"),
        ],
    )
    # classify + 2 tool
    assert len(rows) == 3
    assert rows[0].step_id == "classify"
    assert rows[1].step_id == "tool_tl_a"
    assert rows[2].step_id == "tool_tl_b"
    # All top-level → subagent_id=None.
    assert all(r.subagent_id is None for r in rows)


def test_nested_subagent_spawn_flattened_with_subagent_id():
    """spawn parent + 5 内部 tool → 6 行 (含 spawn), nested 5 行带 subagent_id."""
    nested = [_inv("bi.fetch", f"tl_n{i}", duration_ms=100) for i in range(5)]
    spawn = _inv("subagent_spawn", "tl_spawn1", nested=nested, duration_ms=8000)
    rows = _invocations_to_trace_rows(
        turn_id="t1",
        intent=IntentType.CAUSAL,
        invocations=[spawn],
    )
    # classify (1) + spawn (1) + 5 nested = 7
    assert len(rows) == 7
    assert rows[0].step_id == "classify"
    assert rows[1].step_id == "tool_tl_spawn1"
    assert rows[1].tool_name == "subagent_spawn"
    assert rows[1].subagent_id is None
    # Nested 5 rows carry spawn call_id as subagent_id.
    for i in range(5):
        assert rows[2 + i].step_id == f"tool_tl_n{i}"
        assert rows[2 + i].subagent_id == "tl_spawn1"


def test_deep_nested_recursion():
    """spawn → spawn → tool: 三层. 内层 subagent_id 跟到最近 spawn parent."""
    deep_inner = [_inv("bi.fetch", "tl_deep_a")]
    inner_spawn = _inv("subagent_spawn", "tl_spawn_inner", nested=deep_inner)
    outer_spawn = _inv("subagent_spawn", "tl_spawn_outer", nested=[inner_spawn])
    rows = _invocations_to_trace_rows(
        turn_id="t1",
        intent=None,
        invocations=[outer_spawn],
    )
    # 0: outer_spawn (top), 1: inner_spawn (subagent_id=outer), 2: deep_a (subagent_id=inner)
    assert len(rows) == 3
    assert rows[0].step_id == "tool_tl_spawn_outer"
    assert rows[0].subagent_id is None
    assert rows[1].step_id == "tool_tl_spawn_inner"
    assert rows[1].subagent_id == "tl_spawn_outer"
    assert rows[2].step_id == "tool_tl_deep_a"
    assert rows[2].subagent_id == "tl_spawn_inner"


def test_error_kind_set_for_failed_invocation():
    rows = _invocations_to_trace_rows(
        turn_id="t1",
        intent=None,
        invocations=[
            _inv("bi.fetch", "tl_x", error="datarc 500"),
        ],
    )
    assert rows[0].kind == "error"
    assert "datarc 500" in (rows[0].summary or "")


def test_expected_outcome_kinds_persist_as_success():
    """Expected pauses succeed; rejected attempts remain failures after replay."""

    def _result_inv(call_id: str, result: dict) -> ToolInvocation:
        return ToolInvocation(
            tool_name="bi_schema",
            arguments={},
            call_id=call_id,
            result=result,
            error=None,
            duration_ms=10,
            nested=[],
        )

    rows = _invocations_to_trace_rows(
        turn_id="t1",
        intent=None,
        invocations=[
            _result_inv(
                "tl_clar",
                {
                    "ok": False,
                    "error_kind": "needs_clarification",
                    "clarification": {
                        "kind": "tool_context_select",
                        "prompt": "选工作区",
                        "options": [{"value": "1", "label": "商超零售场景"}],
                    },
                },
            ),
            _result_inv("tl_miss", {"ok": False, "error_kind": "not_accessible", "did_you_mean": []}),
            _result_inv("tl_ia", {"ok": False, "error_kind": "invalid_args", "error": "missing name"}),
            _result_inv("tl_hard", {"ok": False, "error": "boom"}),  # Control: real failure remains error.
            # Control (review P2): claims clarification kind, lacks clarification structure →
            # failure incapable of showing card; never reclassify success.
            _result_inv("tl_badclar", {"ok": False, "error_kind": "needs_clarification"}),
            # feedback-0713 F4: persist mirror for same-family two kinds (B3 missed cases).
            _result_inv(
                "tl_pending",
                {
                    "ok": False,
                    "error": "pending_user_confirmation",
                    "error_kind": "pending_user_confirmation",
                    "accessible": ["商超零售场景"],
                },
            ),
            _result_inv(
                "tl_refused",
                {
                    "ok": False,
                    "clarification_refused_on_resume": True,
                    "error_kind": "clarification_refused_on_resume",
                    "note": "同一轮不要再问第二次澄清",
                },
            ),
        ],
    )
    kinds = {r.step_id: r.kind for r in rows}
    assert kinds["tool_tl_clar"] == "success"
    assert kinds["tool_tl_miss"] == "success"
    assert kinds["tool_tl_ia"] == "error"
    assert kinds["tool_tl_hard"] == "error"
    assert kinds["tool_tl_badclar"] == "error"
    assert kinds["tool_tl_pending"] == "success"
    assert kinds["tool_tl_refused"] == "success"


def test_intent_none_skips_classify_step():
    """cancel 路径 intent=None → 不 emit classify step."""
    rows = _invocations_to_trace_rows(
        turn_id="t1",
        intent=None,
        invocations=[_inv("bi.fetch", "tl_a")],
    )
    assert len(rows) == 1
    assert rows[0].step_id == "tool_tl_a"


def test_expected_outcome_family_requires_structural_evidence():
    """feedback-0713 F4 review 安全: switch/resume 两护栏 kind 光带 error_kind
    字符串、无对应结构证据 → 不得洗成 success（防第三方 MCP 的 untrusted result
    伪造 error_kind 掩盖真失败）。与 needs_clarification 的 options 校验同思路。"""

    def _r(call_id: str, result: dict) -> ToolInvocation:
        return ToolInvocation(
            tool_name="mcp_tool",
            arguments={},
            call_id=call_id,
            result=result,
            error=None,
            duration_ms=5,
            nested=[],
        )

    rows = _invocations_to_trace_rows(
        turn_id="t1",
        intent=None,
        invocations=[
            # pending without accessible list → still error.
            _r("tl_fake_pending", {"ok": False, "error_kind": "pending_user_confirmation"}),
            # refused without clarification_refused_on_resume=True → still error.
            _r("tl_fake_refused", {"ok": False, "error_kind": "clarification_refused_on_resume"}),
            # Negative control: real structurally evidenced guard result still success (no false rejection).
            _r("tl_real_pending", {"ok": False, "error_kind": "pending_user_confirmation", "accessible": ["A"]}),
        ],
    )
    kinds = {r.step_id: r.kind for r in rows}
    assert kinds["tool_tl_fake_pending"] == "error"
    assert kinds["tool_tl_fake_refused"] == "error"
    assert kinds["tool_tl_real_pending"] == "success"
