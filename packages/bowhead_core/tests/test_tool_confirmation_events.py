"""Confirmation cards attached to the waiting tool step (FEATURE_INLINE_TOOL_CONFIRMATION)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.delivery_confirmation import DeliveryConfirmations
from bowhead_core.openapi_tool_source import mcp_entry
from bowhead_core.team_tool_confirmation import TeamToolConfirmations
from bowhead_core.team_tool_registry_view import build_team_specs
from bowhead_core.tool_confirmation_events import confirmation_callbacks
from bowhead_core.tool_registry import ToolExecutionContext, ToolStepRef

from bowhead_core import team_tool_confirmation as confirmation_mod

_TEAM = "team_a"
_SRC = "ots_crm"


class _Bus:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[tuple[str, dict, str | None]] = []
        self.fail = fail

    async def emit(self, event) -> bool:
        if self.fail:
            raise RuntimeError("bus closed")
        self.events.append((event.event_type, dict(event.payload), event.parent_agent_id))
        return True


def _ctx(bus: _Bus | None = None, **over) -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="usr_a",
        turn_id="t_1",
        datarc=None,  # type: ignore[arg-type]
        team_id=_TEAM,
        session_id="s_1",
    )
    ctx.team_tool_source_labels = {f"teammcp.{_SRC}.create_note": "CRM"}
    if bus is not None:
        ctx.tool_step = ToolStepRef(bus=bus, card_id="t_1", step_id="step_call_1", call_id="call_1", parent_agent_id=over.get("agent"))
    return ctx


async def _first_pending(queue: DeliveryConfirmations, owner: str = "usr_a") -> dict:
    for _ in range(200):
        items = queue.pending(owner)
        if items:
            return items[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no pending confirmation")


def _callbacks(ctx: ToolExecutionContext):
    return confirmation_callbacks(
        ctx, kind="email_send", requester_user_id="usr_a", title="Weekly update", source_label="a@example.com", choices=["reject", "approve"]
    )


@pytest.mark.asyncio
async def test_queue_announces_once_and_reports_each_outcome(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_INLINE_TOOL_CONFIRMATION", "1")
    for decide, outcome in (
        (lambda q, i: q.decide("usr_a", i, True), "approved"),
        (lambda q, i: q.decide("usr_a", i, True, for_session=True), "approved_session"),
        (lambda q, i: q.decide("usr_a", i, False), "rejected"),
    ):
        bus = _Bus()
        queue = DeliveryConfirmations(id_prefix="esc", timeout_seconds=5)
        on_pending, on_resolved = _callbacks(_ctx(bus, agent="sub_1"))
        waiting = asyncio.create_task(
            queue.request_snapshot(
                owner_user_id="usr_a", turn_id="t_1", session_id="s_1", payload={"body": "secret"}, on_pending=on_pending, on_resolved=on_resolved
            )
        )
        item = await _first_pending(queue)
        assert decide(queue, item["id"])
        await waiting
        (requested, req_payload, agent), (resolved, res_payload, _) = bus.events
        assert (requested, resolved) == ("confirmation_requested", "confirmation_resolved")
        assert agent == "sub_1"
        assert req_payload["confirmation_id"] == res_payload["confirmation_id"] == item["id"]
        assert req_payload["step_id"] == res_payload["step_id"] == "step_call_1"
        assert req_payload["card_id"] == "t_1" and req_payload["call_id"] == "call_1"
        assert req_payload["kind"] == "email_send" and req_payload["choices"] == ["reject", "approve"]
        assert 0 < req_payload["timeout_seconds"] <= 5
        # Labels only: the reviewed content stays in the owner-only queue.
        assert "secret" not in repr(req_payload) and "body" not in req_payload
        assert res_payload["outcome"] == outcome


@pytest.mark.asyncio
async def test_expiry_is_reported_and_cancellation_or_refusal_emits_nothing_more(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_INLINE_TOOL_CONFIRMATION", "1")
    bus = _Bus()
    on_pending, on_resolved = _callbacks(_ctx(bus))
    queue = DeliveryConfirmations(id_prefix="esc", timeout_seconds=0.05)
    assert (
        await queue.request_snapshot(owner_user_id="usr_a", turn_id="t", session_id="s", payload={}, on_pending=on_pending, on_resolved=on_resolved)
        == "expired"
    )
    assert [event[0] for event in bus.events] == ["confirmation_requested", "confirmation_resolved"]
    assert bus.events[1][1]["outcome"] == "expired"

    bus = _Bus()
    on_pending, on_resolved = _callbacks(_ctx(bus))
    queue = DeliveryConfirmations(id_prefix="esc", timeout_seconds=5)
    waiting = asyncio.create_task(
        queue.request_snapshot(owner_user_id="usr_a", turn_id="t", session_id="s", payload={}, on_pending=on_pending, on_resolved=on_resolved)
    )
    await _first_pending(queue)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    # Stop: the view derives "not run" from the Turn's terminal events.
    assert [event[0] for event in bus.events] == ["confirmation_requested"]
    assert queue.pending("usr_a") == []

    bus = _Bus()
    on_pending, on_resolved = _callbacks(_ctx(bus))
    assert (
        await queue.request_snapshot(owner_user_id="", turn_id="t", session_id="s", payload={}, on_pending=on_pending, on_resolved=on_resolved)
        == "unavailable"
    )
    assert bus.events == []


@pytest.mark.asyncio
async def test_no_card_without_the_switch_a_step_or_a_working_bus(monkeypatch) -> None:
    for switch, ctx in (("0", _ctx(_Bus())), ("1", _ctx()), ("1", _ctx(_Bus(fail=True)))):
        monkeypatch.setenv("FEATURE_INLINE_TOOL_CONFIRMATION", switch)
        on_pending, on_resolved = _callbacks(ctx)
        queue = DeliveryConfirmations(id_prefix="esc", timeout_seconds=5)
        waiting = asyncio.create_task(
            queue.request_snapshot(owner_user_id="usr_a", turn_id="t", session_id="s", payload={}, on_pending=on_pending, on_resolved=on_resolved)
        )
        item = await _first_pending(queue)
        # The queue alone decides; a missing or failed card never approves or blocks the call.
        assert queue.decide("usr_a", item["id"], True)
        assert await waiting == "approved"
        if ctx.tool_step is not None:
            assert ctx.tool_step.bus.events == []


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, structured_content={"done": True}, content=[])


def _mcp(name: str, annotations: dict) -> object:
    meta = {"name": name, "description": f"{name} tool", "input_schema": {"type": "object"}, "annotations": annotations}
    meta["destructive"] = bool(annotations.get("destructiveHint"))
    return mcp_entry(_SRC, meta)


@pytest.mark.asyncio
async def test_team_mcp_card_follows_the_waiting_call(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    monkeypatch.setenv("FEATURE_INLINE_TOOL_CONFIRMATION", "1")
    queue = TeamToolConfirmations(timeout_seconds=5)
    monkeypatch.setattr(confirmation_mod, "_default", queue)
    entries = [_mcp("lookup", {"readOnlyHint": True}), _mcp("create_note", {"readOnlyHint": False, "destructiveHint": False})]
    specs = {spec.schema.name.rsplit(".", 1)[-1]: spec for spec in build_team_specs(_TEAM, entries, _Client())}

    bus = _Bus()
    assert (await specs["lookup"].handler({}, _ctx(bus)))["ok"] is True
    assert bus.events == []

    running = asyncio.create_task(specs["create_note"].handler({"text": "hello"}, _ctx(bus)))
    item = await _first_pending(queue)
    assert queue.approve_for_session("usr_a", item["id"])
    assert (await running)["ok"] is True
    (_, requested, _), (_, resolved, _) = bus.events
    assert requested["kind"] == "team_mcp" and requested["category"] == "external_write"
    assert requested["title"] == "create_note" and requested["source_label"] == "CRM"
    assert requested["choices"] == ["reject", "approve", "approve_session"]
    assert requested["requester_user_id"] == "usr_a"
    assert "hello" not in repr(requested)
    assert resolved["outcome"] == "approved_session"

    # The session allowance now runs the same tool without a card.
    bus.events.clear()
    assert (await specs["create_note"].handler({"text": "again"}, _ctx(bus)))["ok"] is True
    assert bus.events == []


@pytest.mark.asyncio
async def test_runner_gives_each_call_its_own_step(monkeypatch) -> None:
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
    from bowhead_core.tool_registry import ToolRegistry, ToolSchema, ToolSpec
    from test_subagent import _router

    from bowhead_router import LLMResponse, ToolCall

    observed: list[ToolStepRef | None] = []

    async def probe(arguments, ctx):
        observed.append(ctx.tool_step)
        return {"ok": True}

    profile = SubagentProfile(name="step-probe", llm_agent_type="echo", tool_whitelist=("probe_step",), system_prompt="Return the tool result.")
    monkeypatch.setitem(PROFILES, profile.name, profile)
    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="probe_step", description="Check step", input_schema={}), handler=probe))
    router = _router(
        LLMResponse(content="", tool_calls=[ToolCall(id="call_probe", name="probe_step", arguments={})]), LLMResponse(content="Complete.")
    )
    parent = ToolExecutionContext(workspace_id="ws", member_uid="m", user_id="usr_a", turn_id="t_probe", datarc=None)  # type: ignore[arg-type]
    bus = TurnEventBus()
    await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name, task="Check the step", parent_turn_id="t_probe", parent_bus=bus, parent_ctx=parent
    )
    (step,) = observed
    assert step is not None
    assert (step.card_id, step.step_id, step.call_id) == ("t_probe", "step_call_probe", "call_probe")
    assert step.parent_agent_id
    started = [event for event in bus.snapshot() if event.event_type == "step_started"]
    assert any(event.payload["step_id"] == step.step_id for event in started)
    assert parent.tool_step is None
