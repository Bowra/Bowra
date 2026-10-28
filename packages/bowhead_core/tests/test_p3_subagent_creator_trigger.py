"""CCA P3 · S5 — the subagent_creator conversational TRIGGER.

The Lead calls it to OPEN the FE create-subagent card (emit cca.subagent_creator_open); it
never creates. It is Lead-only and carries only a bounded purpose hint, never structured state.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bowhead_core.subagent_creator_trigger import (
    TOOL_NAME,
    register_subagent_creator_trigger,
)
from bowhead_core.tool_registry import ToolRegistry


def _run(coro):
    return asyncio.run(coro)


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


class _RejectFirstBus(_Bus):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def emit(self, ev: Any) -> bool:
        self.calls += 1
        if self.calls == 1:
            return False
        self.events.append(ev)
        return True


class _PurposeContextStore:
    def __init__(self) -> None:
        self.insufficient_calls = 0

    async def get(self, **_kwargs: Any) -> None:
        return None

    async def record_insufficient_prompt(self, **_kwargs: Any) -> Any:
        self.insufficient_calls += 1
        if self.insufficient_calls == 1:
            return SimpleNamespace(insufficient_reprompt_count=1)
        return None


class _BoundEditor:
    def __init__(self) -> None:
        self.edits: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    async def inspect(self) -> dict[str, Any]:
        return {"ok": True, "outcome": "draft_inspected", "draft_id": "sad_bound"}

    async def edit(self, edits: dict[str, Any], text_operations: list[dict[str, Any]]) -> dict[str, Any]:
        self.edits.append((edits, text_operations))
        return {
            "ok": True,
            "applied": True,
            "outcome": "draft_updated",
            "draft_id": "sad_bound",
            "draft_status": "ready",
            "draft_title": "更新后的智能体",
            "version": 4,
        }


def test_bound_draft_edit_emits_only_durable_pointer() -> None:
    editor = _BoundEditor()
    bus = _Bus()
    ctx = SimpleNamespace(parent_bus=bus, turn_id="t_edit", team_id="team", subagent_draft_editor=editor)
    inspected = _run(_handler()({"action": "inspect"}, ctx))
    assert inspected["draft_id"] == "sad_bound"
    result = _run(_handler()({"action": "edit", "draft_edits": {"description": "新用途"}}, ctx))
    assert result["applied"] is True
    assert editor.edits == [({"description": "新用途"}, [])]
    assert len(bus.events) == 1
    assert bus.events[0].payload["draft_id"] == "sad_bound"
    assert bus.events[0].payload["draft_version"] == 4
    assert "新用途" not in str(bus.events[0].payload)


def test_bound_draft_append_uses_one_edit_without_inspect() -> None:
    editor = _BoundEditor()
    bus = _Bus()
    ctx = SimpleNamespace(parent_bus=bus, turn_id="t_append", team_id="team", subagent_draft_editor=editor)
    operation = {"field": "description", "operation": "append", "text": "测试版"}
    result = _run(_handler()({"action": "edit", "draft_text_operations": [operation]}, ctx))
    assert result["applied"] is True
    assert editor.edits == [({}, [operation])]
    assert len(bus.events) == 1


def test_draft_edit_requires_bound_editor(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT", "1")
    monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT_PCT", "100")
    result = _run(
        _handler()({"action": "edit", "draft_edits": {"description": "x"}}, SimpleNamespace(parent_bus=_Bus(), turn_id="t", team_id="team"))
    )
    assert result["outcome"] == "draft_not_bound"
    assert result["applied"] is False


def test_disabled_chat_edit_reports_availability_without_blame(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT", "0")
    result = _run(
        _handler()({"action": "edit", "draft_edits": {"description": "x"}}, SimpleNamespace(parent_bus=_Bus(), turn_id="t", team_id="team"))
    )
    assert result["outcome"] == "draft_chat_edit_unavailable"
    assert not result["applied"]
    assert "reopening it will not enable" in result["message"]


def _handler():
    # The registered handler is not exported; read it from a registry.
    reg = ToolRegistry()
    register_subagent_creator_trigger(reg)
    return reg.get(TOOL_NAME).handler


def test_registers_lead_only_creator_tool(monkeypatch) -> None:
    reg = ToolRegistry()
    assert register_subagent_creator_trigger(reg) is True
    assert reg.has(TOOL_NAME)
    spec = reg.get(TOOL_NAME)
    assert spec.allowed_for_roles == frozenset({"lead"})  # Lead-only


def _sufficient(purpose: str, **extra: Any) -> dict[str, Any]:
    return {
        "action": "continue",
        "purpose": purpose,
        "purpose_evidence": {"source_or_object": True, "action": True, "expected_result": True},
        **extra,
    }


def test_handler_emits_open_event_with_purpose_hint() -> None:
    bus = _Bus()
    ctx = SimpleNamespace(parent_bus=bus, turn_id="t1")
    out = _run(_handler()(_sufficient("  answer weekly sales questions with a chart  "), ctx))
    assert out["ok"] is True and out["opened"] is True
    assert len(bus.events) == 1
    ev = bus.events[0]
    assert ev.event_type == "cca.subagent_creator_open"
    assert ev.payload["purpose_hint"] == "answer weekly sales questions with a chart"  # trimmed


def test_handler_manual_action_opens_without_purpose_hint() -> None:
    bus = _Bus()
    out = _run(_handler()({"action": "manual"}, SimpleNamespace(parent_bus=bus, turn_id="t")))
    assert out["ok"] is True and out["outcome"] == "manual_opened"
    assert bus.events[0].payload["purpose_hint"] is None


def test_purpose_collection_requires_purpose_without_emitting() -> None:
    bus = _Bus()

    out = _run(
        _handler()(
            {
                "action": "continue",
                "purpose_evidence": {
                    "source_or_object": False,
                    "action": False,
                    "expected_result": False,
                },
            },
            SimpleNamespace(
                parent_bus=bus,
                turn_id="t",
                team_id="team_a",
            ),
        )
    )

    assert out["ok"] is True
    assert out["opened"] is False
    assert out["outcome"] == "purpose_required"
    assert bus.events == []


def test_purpose_collection_rejects_insufficient_purpose_without_emitting() -> None:
    bus = _Bus()

    out = _run(
        _handler()(
            {
                "action": "continue",
                "purpose": "帮我处理业务",
                "purpose_evidence": {
                    "source_or_object": True,
                    "action": False,
                    "expected_result": False,
                },
            },
            SimpleNamespace(
                parent_bus=bus,
                turn_id="t",
                team_id="team_a",
            ),
        )
    )

    assert out["outcome"] == "purpose_insufficient"
    assert bus.events == []


def test_purpose_collection_opens_prefilled_manual_fallback_when_sufficient() -> None:
    bus = _Bus()

    out = _run(
        _handler()(
            {
                "action": "continue",
                "purpose": "每周分析门店销售异常并生成趋势图",
                "purpose_evidence": {
                    "source_or_object": True,
                    "action": True,
                    "expected_result": True,
                },
            },
            SimpleNamespace(
                parent_bus=bus,
                turn_id="t",
                team_id="team_a",
            ),
        )
    )

    assert out["outcome"] == "fallback_opened"
    assert out["opened"] is True
    assert bus.events[0].payload["purpose_hint"] == "每周分析门店销售异常并生成趋势图"


def test_insufficient_purpose_stops_reprompting_after_once() -> None:
    context_store = _PurposeContextStore()
    ctx = SimpleNamespace(
        parent_bus=_Bus(),
        turn_id="t",
        team_id="team_a",
        user_id="user_a",
        session_id="session_a",
        subagent_authoring_context_store=context_store,
    )
    args = {
        "action": "continue",
        "purpose": "做分析",
        "purpose_evidence": {
            "source_or_object": True,
            "action": False,
            "expected_result": False,
        },
    }

    first = _run(_handler()(args, ctx))
    second = _run(_handler()(args, ctx))

    assert first["outcome"] == "purpose_insufficient"
    assert second["outcome"] == "purpose_options"
    assert second["actions"] == ["continue", "manual", "cancel"]


def test_purpose_collection_manual_and_cancel_actions() -> None:

    manual_bus = _Bus()
    manual = _run(
        _handler()(
            {"action": "manual"},
            SimpleNamespace(
                parent_bus=manual_bus,
                turn_id="t-manual",
                team_id="team_a",
            ),
        )
    )
    assert manual["outcome"] == "manual_opened"
    assert manual_bus.events[0].payload["purpose_hint"] is None

    cancel_bus = _Bus()
    cancelled = _run(
        _handler()(
            {"action": "cancel"},
            SimpleNamespace(
                parent_bus=cancel_bus,
                turn_id="t-cancel",
                team_id="team_a",
            ),
        )
    )
    assert cancelled["outcome"] == "cancelled"
    assert cancel_bus.events == []


def test_handler_truncates_long_purpose() -> None:
    bus = _Bus()
    out = _run(_handler()(_sufficient("x" * 500), SimpleNamespace(parent_bus=bus, turn_id="t")))
    assert out["ok"] is True
    assert len(bus.events[0].payload["purpose_hint"]) == 200  # _MAX_HINT


def test_handler_no_bus_graceful() -> None:
    out = _run(_handler()(_sufficient("summarize sales reports"), SimpleNamespace(parent_bus=None, turn_id="t")))
    assert out["ok"] is False and out["opened"] is False


def test_team_store_runtime_wiring_is_independent_of_creator_flag(monkeypatch) -> None:
    """Creator controls authoring, not Marketplace/runtime access to existing Agents."""
    from bowhead_core.turn_runner import _team_subagent_store

    orch = SimpleNamespace(user_store="STORE")
    assert _team_subagent_store(orch) == "STORE"
    assert _team_subagent_store(orch) == "STORE"


def test_lead_manifest_whitelists_the_trigger() -> None:
    """Dogfood root cause: the Lead advertise is (registry.to_schemas ∩ lead whitelist) +
    role-filter, so subagent_creator MUST be in the Lead's tool_whitelist (lead.yaml
    tools.allow) — else it's dropped and the Lead can never open the create card (it falls
    back to skill-creation). Registration stays the flag-gate; whitelisting is inert when off."""
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.registry import AgentRegistry

    wl = manifest_to_profile(AgentRegistry.bootstrap().get("lead")).tool_whitelist
    assert TOOL_NAME in wl  # subagent_creator
    assert "tool_creator" in wl  # P2 sibling (same advertise gate)


def test_handler_prefills_user_stated_slug_name() -> None:
    bus = _Bus()
    out = _run(
        _handler()(
            _sufficient("回答 GitHub 开源仓库的文档问题", name=" dogfood-wiki-helper "),
            SimpleNamespace(parent_bus=bus, turn_id="t"),
        )
    )
    assert out["opened"] is True
    payload = bus.events[0].payload
    assert payload["name_hint"] == "dogfood-wiki-helper"
    assert payload["display_name_hint"] is None


def test_handler_routes_non_slug_name_to_display_name() -> None:
    bus = _Bus()
    _run(_handler()({"action": "manual", "name": "文档问答助手"}, SimpleNamespace(parent_bus=bus, turn_id="t")))
    payload = bus.events[0].payload
    assert payload["name_hint"] is None
    assert payload["display_name_hint"] == "文档问答助手"


def test_handler_keeps_explicit_display_name_and_bounds_it() -> None:
    bus = _Bus()
    _run(
        _handler()(
            {"action": "manual", "name": "Wiki Helper", "display_name": "文" * 60},
            SimpleNamespace(parent_bus=bus, turn_id="t"),
        )
    )
    payload = bus.events[0].payload
    assert payload["name_hint"] == "wiki-helper"
    assert payload["display_name_hint"] == "文" * 40


def test_handler_without_stated_name_sends_no_identity_hints() -> None:
    bus = _Bus()
    _run(_handler()(_sufficient("summarize sales reports"), SimpleNamespace(parent_bus=bus, turn_id="t")))
    payload = bus.events[0].payload
    assert payload["name_hint"] is None and payload["display_name_hint"] is None


def test_purpose_collection_manual_fallback_carries_identity_hints() -> None:
    bus = _Bus()
    out = _run(
        _handler()(
            {
                "action": "continue",
                "purpose": "只回答 GitHub 开源仓库的文档问题，给出引用链接",
                "name": "dogfood-wiki-helper",
                "purpose_evidence": {"source_or_object": True, "action": True, "expected_result": True},
            },
            SimpleNamespace(parent_bus=bus, turn_id="t", team_id="team_a"),
        )
    )
    assert out["outcome"] == "fallback_opened"
    payload = bus.events[0].payload
    assert payload["name_hint"] == "dogfood-wiki-helper"
    assert "system_prompt" not in payload and "tool_refs" not in payload


@pytest.mark.parametrize("failure", [None, "delivery", "generation"])
def test_unified_chat_opens_saved_canvas_before_generation(monkeypatch, failure) -> None:
    draft = SimpleNamespace(id="sad_canvas", status="editing", row_version=1, definition=None, goal_summary="")
    bus = _RejectFirstBus() if failure == "delivery" else _Bus()
    service = SimpleNamespace(create_chat_canvas_draft=AsyncMock(return_value=draft), create_intent_draft=AsyncMock())

    async def generate(**kwargs):
        assert bus.events[0].payload["draft_id"] == "sad_canvas"
        assert bus.events[0].payload["draft_version"] == 1
        assert bus.events[0].persist_before_delivery
        assert kwargs["draft"] is draft
        if failure == "generation":
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(suggestion=object())

    service.generate_canvas_suggestion = AsyncMock(side_effect=generate)
    ctx = SimpleNamespace(
        parent_bus=bus,
        turn_id="t_canvas",
        team_id="team_a",
        user_id="user_a",
        session_id="s_canvas",
        workspace_id="ws_a",
        subagent_draft_service=service,
        language_context=None,
        subagent_draft_mcp_entitled=False,
    )
    result = _run(_handler()(_sufficient("Analyze weekly sales and return anomalies"), ctx))
    assert result["draft_id"] == "sad_canvas"
    service.create_chat_canvas_draft.assert_awaited_once()
    assert service.create_chat_canvas_draft.call_args.kwargs["workspace_id"] == "ws_a"
    assert service.create_chat_canvas_draft.call_args.kwargs["source_turn_id"] == "t_canvas"
    service.create_intent_draft.assert_not_awaited()
    if failure == "delivery":
        service.generate_canvas_suggestion.assert_not_awaited()
        assert result["outcome"] == "draft_saved_refresh_required"
    else:
        service.generate_canvas_suggestion.assert_awaited_once()
        assert result["opened"]
        assert result["outcome"] == ("draft_generation_unavailable" if failure else "draft_opened")
