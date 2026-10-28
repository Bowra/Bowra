"""bail 的人话必须跟着 kind 一起走到计划卡上（116 dogfood 2026-08-14）。

**现场**：BI 后端 `download_data: poll timeout after 60.0s` 连挂 5 次 → 两个计划步
failed → 用户在计划卡上看到的字面是 **「Failed: transient: transient」**，
真因在 UI 上完全不存在。

**机制**：`PlanStepError.__init__` 是 `super().__init__(message or self.error_kind)`，
不给 message 时异常串就等于 kind；`TaskFailureError.__str__` 又是
`f"{reason}: {original}"` —— 于是 `kind: kind`。**任何不带 message 的 PlanStepError
子类都会长成这样**，不是个例。而 bail 信封的契约本来就强制带 1-2 句人话
（`analysis_payload`: `error.message missing/empty (need 1-2 sentence reason)`）。
"""

from __future__ import annotations

from bowhead_core.agents.agent_runner_adapter import _bail_message
from bowhead_core.agents.task_spec import PlanStepBailError, TaskFailureError


def test_bail_without_message_used_to_double_the_kind() -> None:
    """回归钉：不给 message 时仍是旧形状（kind: kind）—— 这正是要被 message 取代的。"""
    e = PlanStepBailError("transient")
    assert str(TaskFailureError(e.error_kind, e)) == "transient: transient"


def test_bail_with_message_reaches_the_step_error_text() -> None:
    """★正身：带上人话后，落库的 error 串就是用户能读懂的那句。

    突变：把 `message=_bail_message(...)` 摘掉 → 本条红。
    """
    e = PlanStepBailError("transient", message="BI 查询下载超时 60s，数据源暂时不可用")
    assert e.error_kind == "transient", "kind 仍要是干净的枚举值（下游按它分支）"
    assert str(TaskFailureError(e.error_kind, e)) == "transient: BI 查询下载超时 60s，数据源暂时不可用"


def test_bail_message_picks_first_non_empty_known_key() -> None:
    assert _bail_message({"kind": "transient", "message": "数据源超时"}) == "数据源超时"
    # Two bail paths use different envelopes: reason/detail is another shape.
    assert _bail_message({"kind": "tool_failure", "reason": "bi_query 连续失败"}) == "bi_query 连续失败"
    # message outranks reason (message is contract field).
    assert _bail_message({"message": "A", "reason": "B"}) == "A"


def test_bail_message_degrades_quietly() -> None:
    """拿不到人话时不许瞎编 —— 返空，调用方回落成旧行为(kind)。"""
    assert _bail_message(None) == ""
    assert _bail_message({}) == ""
    assert _bail_message({"kind": "transient"}) == ""
    assert _bail_message({"message": "   "}) == ""


def test_bail_message_is_capped() -> None:
    """整段 traceback 不该灌进计划卡。"""
    assert len(_bail_message({"message": "x" * 5000})) == 300
