"""workflow-solidify S2 — the workflow_creator conversational TRIGGER.

The Lead calls it to OPEN the FE 固化 card (emit cca.workflow_solidify_open); it never
distills/creates. Flag-gated by BOWHEAD_CCA_WORKFLOW_CREATOR (the SAME single switch that
mounts the distill route), Lead-only, payload carries only {turn_id (distill target; None on
conversational entry), hint (≤200)} — never structured state.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from bowhead_core.tool_registry import ToolRegistry
from bowhead_core.workflow_creator_trigger import (
    TOOL_NAME,
    register_workflow_creator_trigger,
    workflow_creator_enabled,
)


def _run(coro):
    return asyncio.run(coro)


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


def _handler():
    # the registered handler isn't exported; pull it off a flag-on registry.
    import os

    os.environ["BOWHEAD_CCA_WORKFLOW_CREATOR"] = "1"
    os.environ["BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY"] = "1"  # Narrow entry flag defaults off; explicitly enable to get the handler.
    reg = ToolRegistry()
    register_workflow_creator_trigger(reg)
    return reg.get(TOOL_NAME).handler


def test_enabled_flag_parsing(monkeypatch) -> None:
    for v in ("1", "true", "YES", "On"):
        monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", v)
        assert workflow_creator_enabled() is True
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", off)
        assert workflow_creator_enabled() is False
    # Default ON after skill-team-governance completion: unset/empty → True.
    monkeypatch.delenv("BOWHEAD_CCA_WORKFLOW_CREATOR", raising=False)
    assert workflow_creator_enabled() is True


def test_register_gated_by_flag(monkeypatch) -> None:
    """flag OFF → 不注册（advertise = to_schemas ∩ whitelist 自然不含它，字节安全）；ON → 注册 Lead-only."""
    monkeypatch.setenv("BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY", "1")  # Test only the family flag; entry gate is open.
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "0")  # Explicitly off (default changed to ON).
    reg = ToolRegistry()
    assert register_workflow_creator_trigger(reg) is False
    assert not reg.has(TOOL_NAME)

    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "1")
    reg2 = ToolRegistry()
    assert register_workflow_creator_trigger(reg2) is True
    assert reg2.has(TOOL_NAME)
    spec = reg2.get(TOOL_NAME)
    assert spec.allowed_for_roles == frozenset({"lead"})  # Lead-only


def test_entry_flag_defaults_off_and_hides_the_trigger(monkeypatch) -> None:
    """★ 2026-09-01 用户要求先隐藏「固化此流程」：**入口窄开关默认关**。

    族开关 `BOWHEAD_CCA_WORKFLOW_CREATOR` 照旧 ON（它还管团队 skill 治理、
    `/settings/workflows` 管理页——已固化产物的去处，不能一起关），
    但 `workflow_creator` 工具不再注册 ⇒ Lead 在对话里打不开固化卡。
    """
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "1")
    monkeypatch.delenv("BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY", raising=False)  # Default = off.
    reg = ToolRegistry()
    assert register_workflow_creator_trigger(reg) is False
    assert not reg.has(TOOL_NAME)

    # ★ Positive control: explicitly reenabling must restore it; otherwise this is removal, not hiding.
    monkeypatch.setenv("BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY", "1")
    reg2 = ToolRegistry()
    assert register_workflow_creator_trigger(reg2) is True
    assert reg2.get(TOOL_NAME).allowed_for_roles == frozenset({"lead"})


def test_entry_flag_parsing(monkeypatch) -> None:
    """默认关 ⇒ 只有显式 on/1/true/yes 才开（与族开关的「默认开、显式关」相反，别写反）。"""
    from bowhead_core.workflow_creator_trigger import workflow_solidify_entry_enabled

    for on in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY", on)
        assert workflow_solidify_entry_enabled() is True
    for off in ("0", "false", "no", "off", "", "  ", "garbage"):
        monkeypatch.setenv("BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY", off)
        assert workflow_solidify_entry_enabled() is False
    monkeypatch.delenv("BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY", raising=False)
    assert workflow_solidify_entry_enabled() is False, "默认必须是关"


def test_handler_emits_open_event_turn_id_none_with_hint() -> None:
    bus = _Bus()
    ctx = SimpleNamespace(parent_bus=bus, turn_id="t1")
    out = _run(_handler()({"purpose": "  以后核渠道毛利都这么做  "}, ctx))
    assert out["ok"] is True and out["opened"] is True
    assert len(bus.events) == 1
    ev = bus.events[0]
    assert ev.event_type == "cca.workflow_solidify_open"
    assert ev.turn_id == "t1"  # SSE routes on the current turn.
    assert ev.payload["turn_id"] is None  # Target turn: frontend resolves the conversation entry.
    assert ev.payload["hint"] == "以后核渠道毛利都这么做"  # trimmed.


def test_handler_no_hint_is_none() -> None:
    bus = _Bus()
    out = _run(_handler()({}, SimpleNamespace(parent_bus=bus, turn_id="t")))
    assert out["ok"] is True
    assert bus.events[0].payload["hint"] is None


def test_handler_truncates_long_hint() -> None:
    bus = _Bus()
    out = _run(_handler()({"purpose": "x" * 500}, SimpleNamespace(parent_bus=bus, turn_id="t")))
    assert out["ok"] is True
    assert len(bus.events[0].payload["hint"]) == 200  # _MAX_HINT.


def test_handler_no_bus_graceful() -> None:
    out = _run(_handler()({"purpose": "x"}, SimpleNamespace(parent_bus=None, turn_id="t")))
    assert out["ok"] is False and out["opened"] is False


def test_lead_manifest_whitelists_the_trigger() -> None:
    """Lead advertise = (registry.to_schemas ∩ lead whitelist) + role-filter，所以
    workflow_creator MUST 在 lead.yaml tools.allow，否则被 drop、Lead 永远开不了固化卡片
    （回退 skill-creation）。注册仍是 flag-gate；whitelist 在 flag-off 时 inert."""
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.registry import AgentRegistry

    wl = manifest_to_profile(AgentRegistry.bootstrap().get("lead")).tool_whitelist
    assert TOOL_NAME in wl  # workflow_creator.
    assert "subagent_creator" in wl  # Sibling trigger with the same advertising gate.
    assert "tool_creator" in wl
