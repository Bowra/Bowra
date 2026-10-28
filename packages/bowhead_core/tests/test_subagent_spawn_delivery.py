"""delivery-surface C4 —— subagent_spawn 的 delivery 参数。

Lead 拿主意（chat=聊天短答 / report=画布报告），经 spawn 参数结构化下传，
注入 peer 的 Sub-task context——这是 peer 第一次能看到用户的交付诉求
（task= 是 Lead 转述，用户原话不下传）。
"""

from __future__ import annotations

from bowhead_core.subagent import SubagentResult, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry


def test_spawn_schema_advertises_delivery() -> None:
    registry = build_default_registry()
    register_subagent_tool(registry)
    props = registry.get("subagent_spawn").schema.input_schema["properties"]
    assert props["delivery"]["enum"] == ["chat", "report"]
    assert "delivery" not in registry.get("subagent_spawn").schema.input_schema["required"]


class _CapturingRunner:
    """假 runner：只捕获 extra_instructions，返回最小成功结果。"""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def run(self, **kwargs):
        self.kwargs = kwargs
        return SubagentResult(
            profile=str(kwargs.get("profile_name") or "compute"),
            subagent_id="sub_test",
            ok=True,
            final_answer="42",
        )


class _FakeBus:
    async def emit(self, ev) -> None:  # noqa: ARG002
        return None


async def _spawn_with(args: dict, bus: _FakeBus | None = None) -> _CapturingRunner:
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _CapturingRunner()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=None,
        subagent_runner=runner,
        parent_bus=bus if bus is not None else _FakeBus(),
    )
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "查今年利润前三的品牌", **args}, ctx)
    assert inv.error is None, inv.error
    assert runner.kwargs, f"runner.run 未被调用: {inv.result}"
    return runner


async def test_delivery_chat_injected_into_peer_context() -> None:
    runner = await _spawn_with({"delivery": "chat"})
    extra = str(runner.kwargs.get("extra_instructions") or "")
    assert "delivery" in extra
    assert "delivery_surface='chat'" in extra
    # Delivery-surface autonomy (batch 5ⓐ): chat may include inline charts; canvas_add_* is no longer forbidden.
    assert "不要写画布报告" in extra


async def test_delivery_report_injected_into_peer_context() -> None:
    runner = await _spawn_with({"delivery": "report"})
    extra = str(runner.kwargs.get("extra_instructions") or "")
    assert "report" in extra
    assert "AnalysisPayload" in extra


async def test_delivery_omitted_leaves_context_untouched() -> None:
    """省略 delivery（证据驱动默认）：不注入任何 delivery 行——行为与 C4 之前逐字节一致。"""
    runner = await _spawn_with({})
    extra = str(runner.kwargs.get("extra_instructions") or "")
    assert "delivery" not in extra


async def test_delivery_invalid_value_treated_as_omitted() -> None:
    """LLM 可写面防御：非法值按省略处理，不进 peer 上下文。"""
    runner = await _spawn_with({"delivery": "canvas"})
    extra = str(runner.kwargs.get("extra_instructions") or "")
    assert "delivery" not in extra


async def test_spawn_stamps_delivery_on_bus_report_wins() -> None:
    """review #13b：spawn 决定钉到 turn 级 bus（ψ-E eager 位点读它），
    冲突口径与 `_lead_spawn_delivery` 同 = report 赢，且不受 spawn 顺序影响。"""
    bus = _FakeBus()
    await _spawn_with({"delivery": "chat"}, bus=bus)
    assert bus._lead_delivery_spawn == "chat"
    await _spawn_with({"delivery": "report"}, bus=bus)
    assert bus._lead_delivery_spawn == "report"
    # Once report is chosen, later chat spawns cannot change it back.
    await _spawn_with({"delivery": "chat"}, bus=bus)
    assert bus._lead_delivery_spawn == "report"


async def test_spawn_without_delivery_leaves_bus_unstamped() -> None:
    bus = _FakeBus()
    await _spawn_with({}, bus=bus)
    assert not hasattr(bus, "_lead_delivery_spawn")
