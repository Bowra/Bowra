"""canvas-update-turn 批 B0 —— subagent_spawn 的 canvas_mode 参数（FEATURE_CANVAS_SECTION_REVISE）。

flag 开：schema 有 `canvas_mode` 且必填；handler 记 `bus._lead_canvas_mode`（多 spawn 取 revise>append>new_report），
revise/append 在 Sub-task context 追加一行操作说明。flag 关：schema、上下文逐字节不变。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.canvas_revise import (
    canvas_mode_context_line,
    lead_canvas_mode,
    normalize_canvas_mode,
    note_lead_canvas_mode,
)
from bowhead_core.subagent import SubagentResult, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

pytestmark = pytest.mark.unit


def _schema(monkeypatch: pytest.MonkeyPatch, flag: str | None):
    if flag is None:
        monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    else:
        monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", flag)
    registry = build_default_registry()
    register_subagent_tool(registry)
    return registry.get("subagent_spawn").schema.input_schema


def test_schema_has_required_canvas_mode_only_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _schema(monkeypatch, None)
    assert "canvas_mode" not in off["properties"] and "canvas_mode" not in off["required"]
    on = _schema(monkeypatch, "1")
    assert on["properties"]["canvas_mode"]["enum"] == ["new_report", "revise", "append"]
    assert "canvas_mode" in on["required"]


def _routed_agent_names(text: str) -> set[str]:
    """路由说明里点名的 Agent（系统 Agent 名形如 compute / consultant / xx-analyst / xx-writer）。"""
    import re

    return set(re.findall(r"\b(?:compute|consultant|[a-z]+-(?:analyst|writer))\b", text))


def test_canvas_mode_routing_names_only_registered_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """续轮档位说明把修订派给谁，谁就必须真能被派——点名一个不存在的 Agent，Lead 会照着去派。"""
    from bowhead_core.agents import AgentRegistry

    description = _schema(monkeypatch, "1")["properties"]["canvas_mode"]["description"]
    routed = _routed_agent_names(description)
    assert "compute" in routed
    assert routed <= set(AgentRegistry.bootstrap().list_all()), routed


def test_pure_helpers() -> None:
    assert normalize_canvas_mode(" Revise ") == "revise" and normalize_canvas_mode("x") is None
    bus = SimpleNamespace()
    note_lead_canvas_mode(bus, "new_report")
    note_lead_canvas_mode(bus, "append")
    note_lead_canvas_mode(bus, "new_report")  # Weaker modes cannot override stronger ones.
    assert lead_canvas_mode(bus) == "append"
    note_lead_canvas_mode(bus, "revise")
    assert lead_canvas_mode(bus) == "revise"
    assert canvas_mode_context_line("new_report", "T") == ""
    assert "revise" in canvas_mode_context_line("revise", "旧报告") and "《旧报告》" in canvas_mode_context_line("revise", "旧报告")
    assert "append" in canvas_mode_context_line("append", None)


class _CapturingRunner:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def run(self, **kwargs):
        self.kwargs = kwargs
        return SubagentResult(profile=str(kwargs.get("profile_name") or "compute"), subagent_id="sub_test", ok=True, final_answer="42")


class _FakeBus:
    async def emit(self, ev) -> None:  # noqa: ARG002
        return None


class _StoreWithReport:
    """画布上已有一份上一轮的报告（h1 在 t_prev），供 handler 取文档标题。"""

    async def get_canvas_by_session(self, _sid):
        return SimpleNamespace(id="cv_1")

    async def list_sections(self, _cid, include_hidden=False):  # noqa: ARG002
        return [SimpleNamespace(id="sec_h1", kind="h1", superseded=False, turn_id="t_prev", payload={"text": "旧报告"})]


async def _spawn(monkeypatch: pytest.MonkeyPatch, args: dict, *, flag: str | None = "1", bus=None, store=None):
    if flag is None:
        monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    else:
        monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", flag)
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _CapturingRunner()
    bus = bus if bus is not None else _FakeBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=None,
        subagent_runner=runner,
        parent_bus=bus,
        session_id="s1",
    )
    if store is not None:
        ctx.canvas_store = store
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "把报告改成上半年口径", **args}, ctx)
    assert inv.error is None, inv.error
    assert runner.kwargs, f"runner.run 未被调用: {inv.result}"
    return runner, bus


async def test_revise_mode_writes_bus_and_context_line_with_title(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, bus = await _spawn(monkeypatch, {"canvas_mode": "revise"}, store=_StoreWithReport())
    assert lead_canvas_mode(bus) == "revise"
    extra = str(runner.kwargs.get("extra_instructions") or "")
    assert "canvas_mode**: revise" in extra and "《旧报告》" in extra and "editable=true" in extra


async def test_append_mode_context_line(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, bus = await _spawn(monkeypatch, {"canvas_mode": "append"}, store=_StoreWithReport())
    assert lead_canvas_mode(bus) == "append"
    assert "canvas_mode**: append" in str(runner.kwargs.get("extra_instructions") or "")


async def test_revise_or_append_without_prior_report_coerced_to_new_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """review 修：画布没有上一轮可见报告时 revise/append 无对象——收敛成 new_report，
    否则收尾器按档位不发 h1/KPI，第一份报告成无头文档。"""
    for mode in ("revise", "append"):
        runner, bus = await _spawn(monkeypatch, {"canvas_mode": mode})  # No store = no report.
        assert lead_canvas_mode(bus) == "new_report"
        assert "canvas_mode**" not in str(runner.kwargs.get("extra_instructions") or "")


async def test_new_report_mode_adds_no_line(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, bus = await _spawn(monkeypatch, {"canvas_mode": "new_report"})
    assert lead_canvas_mode(bus) == "new_report"
    assert "canvas_mode**" not in str(runner.kwargs.get("extra_instructions") or "")


async def test_revise_mode_coerces_delivery_chat_to_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """116 dogfood R2：Lead 派 canvas-writer 改措辞时说 delivery=chat ⇒ 早判 chat 把画布事件整条压掉。
    就地改/加章必然动画布：档位赢，delivery 收敛成 report，早判不再是 chat。"""
    runner, bus = await _spawn(monkeypatch, {"canvas_mode": "revise", "delivery": "chat"}, store=_StoreWithReport())
    assert lead_canvas_mode(bus) == "revise"
    assert getattr(bus, "_lead_delivery_spawn", None) == "report"
    assert getattr(bus, "_early_chat_surface", False) is False
    # Positive control: new_report with delivery=chat retains old behavior.
    runner2, bus2 = await _spawn(monkeypatch, {"canvas_mode": "new_report", "delivery": "chat"}, store=_StoreWithReport())
    assert getattr(bus2, "_lead_delivery_spawn", None) == "chat"


class _BrokenStore:
    """读库失败（不是「没有报告」）。"""

    async def get_canvas_by_session(self, _sid):
        raise RuntimeError("db locked")


async def test_lookup_failure_keeps_lead_mode_instead_of_coercing(monkeypatch: pytest.MonkeyPatch) -> None:
    """fix-round review：取上一轮 h1 的读库失败不能被当成「没有报告」而把 Lead 的 revise 静默收敛成 new_report。"""
    runner, bus = await _spawn(monkeypatch, {"canvas_mode": "revise"}, store=_BrokenStore())
    assert lead_canvas_mode(bus) == "revise"
    assert "canvas_mode**: revise" in str(runner.kwargs.get("extra_instructions") or "")  # No title, but SOP still passes through.


async def test_multi_spawn_keeps_strongest_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = _FakeBus()
    await _spawn(monkeypatch, {"canvas_mode": "revise"}, bus=bus, store=_StoreWithReport())
    await _spawn(monkeypatch, {"canvas_mode": "new_report"}, bus=bus, store=_StoreWithReport())
    assert lead_canvas_mode(bus) == "revise"


async def test_flag_off_ignores_canvas_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, bus = await _spawn(monkeypatch, {}, flag=None)
    assert lead_canvas_mode(bus) is None
    assert "canvas_mode**" not in str(runner.kwargs.get("extra_instructions") or "")


async def test_flag_on_missing_canvas_mode_rejected_only_when_prior_report_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """schema 的 required 只是给模型看的；registry 不校验，必填得在 handler 落实（review P1-2）。
    画布已有上一轮报告 ⇒ 缺参/非法值拒；没有报告 ⇒ 默认 new_report（不为难调用方）。"""
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _CapturingRunner()
    ctx = ToolExecutionContext(
        workspace_id="1", member_uid="m", user_id="u", turn_id="parent_t", datarc=None, subagent_runner=runner, parent_bus=_FakeBus(), session_id="s1"
    )
    ctx.canvas_store = _StoreWithReport()
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "t"}, ctx)
    assert isinstance(inv.result, dict) and "canvas_mode is required" in str(inv.result.get("error")), inv.result
    assert inv.result.get("error_kind") == "invalid_args"  # Review fix: frontend renders as a self-correctable argument error, not a red failure.
    assert not runner.kwargs  # Peer was not dispatched.
    # Batch B2-3: second missing/invalid args in a turn use the default mode (guarded by test_canvas_revise_b2); test invalid values in a fresh turn/bus.
    ctx.parent_bus = _FakeBus()
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "t", "canvas_mode": "rewrite"}, ctx)
    assert "不合法" in str(inv.result.get("error")) and "rewrite" in str(inv.result.get("error")), inv.result
    assert not runner.kwargs
    # No report (no store) ⇒ default new_report, dispatch normally.
    runner2, bus2 = await _spawn(monkeypatch, {})
    assert lead_canvas_mode(bus2) == "new_report" and runner2.kwargs
