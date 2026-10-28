"""Baseline for per-call tool authorization guards before the policy chain.

Pins each guard's denial shape, its position relative to the step card, and
which guards a direct ``ToolRegistry.execute`` caller passes through. See
docs/design/tool-call-policy-chain-tech-design.md (step 0). The migration
must keep every assertion here unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.documents.turn_policy import DocumentTurnState
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSchema, ToolSpec
from bowhead_core.types import ToolInvocation, utc_now_iso
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope, skill_operation_denial

from bowhead_router import ToolCall


class _Handlers:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def make(self, name: str):
        async def handler(args: dict, ctx: ToolExecutionContext) -> dict:
            self.calls.append(name)
            return {"ok": True}

        return handler


def _registry(handlers: _Handlers, *names: str, roles: frozenset[str] | None = None, needs_approval: bool = False) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        spec = ToolSpec(
            schema=ToolSchema(name=name, description=name, input_schema={"type": "object"}),
            handler=handlers.make(name),
            needs_approval=needs_approval,
        )
        if roles is not None:
            spec.allowed_for_roles = roles
        registry.register(spec)
    return registry


def _ctx(*, role: str | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_policy",
        member_uid="m_policy",
        user_id="u_policy",
        turn_id="t_policy",
        datarc=None,  # type: ignore[arg-type]  # Handlers here never call Datarc.
        role=role,
    )


def _skill_scope() -> ExactSkillRuntimeScope:
    return ExactSkillRuntimeScope(read_names=("method", "runner"), invoke_names=("runner",))


def _assert_denied_without_dispatch(inv: ToolInvocation, handlers: _Handlers) -> None:
    assert handlers.calls == []
    assert inv.duration_ms == 0
    assert inv.ended_at is not None


# ── Registry stage: guards inside ToolRegistry.execute ──────────────────────


async def test_role_acl_denial_is_error_only_with_metric(monkeypatch) -> None:
    metrics: list[tuple[str, dict]] = []
    monkeypatch.setattr("bowhead_core.metrics.inc", lambda name, labels=None, **_: metrics.append((name, labels or {})))
    handlers = _Handlers()
    registry = _registry(handlers, "bi_probe", roles=frozenset({"analyst"}))

    inv = await registry.execute("bi_probe", {"x": 1}, _ctx(role="lead"))

    _assert_denied_without_dispatch(inv, handlers)
    assert inv.result is None
    assert inv.error == (
        "ToolPermissionError: role='lead' cannot call tool='bi_probe', allowed_roles=['analyst']. hint: use 'subagent_spawn' to delegate to analyst."
    )
    assert metrics == [("bowhead_lead_bi_attempts_total", {"role": "lead", "tool": "bi_probe"})]


async def test_admit_skill_scope_denial_carries_result_and_error() -> None:
    handlers = _Handlers()
    registry = _registry(handlers, "skill_invoke")
    ctx = _ctx()
    ctx.skill_runtime_scope = _skill_scope()
    arguments = {"name": "method"}

    inv = await registry.admit("skill_invoke", arguments, ctx, call_id="call_admit")

    assert handlers.calls == []
    expected = skill_operation_denial(ctx, "skill_invoke", arguments)
    assert expected is not None and expected["error_kind"] == "skill_unavailable"
    assert inv is not None
    assert inv.result == expected
    assert inv.error == "ToolPermissionError: skill_unavailable"
    assert inv.call_id == "call_admit"
    assert await registry.admit("skill_invoke", {"name": "runner"}, ctx, call_id="call_allowed") is None


async def test_admit_records_the_rejection_for_ordinary_lead_retention() -> None:
    handlers = _Handlers()
    registry = _registry(handlers, "skill_invoke")
    ctx = _ctx()
    ctx.skill_runtime_scope = _skill_scope()
    ctx.ordinary_lead_state = SimpleNamespace(checkpoint=None, tool_materials={})
    ctx.ordinary_call_identity = ("sub_policy", "call_retained")

    inv = await registry.admit("skill_invoke", {"name": "method"}, ctx, call_id="call_retained")

    assert inv is not None
    recorded = ctx.ordinary_lead_state.tool_materials[("sub_policy", "call_retained")]
    assert recorded["tool"] == "skill_invoke"
    assert recorded["kind"] == "rejected"
    assert recorded["error"] == "ToolPermissionError: skill_unavailable"


async def test_document_latch_denial_is_result_only() -> None:
    handlers = _Handlers()
    registry = _registry(handlers, "kb_search")
    ctx = _ctx()
    ctx.document_turn_state = DocumentTurnState(generation_selected=True, native_in_batch=True)

    inv = await registry.execute("kb_search", {}, ctx)

    _assert_denied_without_dispatch(inv, handlers)
    assert inv.error is None
    assert inv.result == {
        "ok": False,
        "error": "document_workflow_required",
        "error_kind": "validation",
        "retryable": False,
        "document_terminal": False,
        "hint": "This request has selected private document review. Continue with template_document_submit.",
    }


async def test_datarc_capability_denial_is_error_only() -> None:
    handlers = _Handlers()
    registry = _registry(handlers, "bi_query")

    inv = await registry.execute("bi_query", {}, _ctx())

    _assert_denied_without_dispatch(inv, handlers)
    assert inv.result is None
    assert inv.error is not None
    assert inv.error.startswith("DatarcUnavailableError: tool='bi_query' needs a BI data source (datarc)")


async def test_reference_skill_context_denial_runs_before_spec_lookup(monkeypatch) -> None:
    denial = {"ok": False, "error_kind": "reference_skill_unavailable"}

    async def deny(ctx: Any, name: str) -> dict:
        return denial

    monkeypatch.setattr("bowhead_skill.runtime_scope.reference_skill_context_denial", deny)
    registry = ToolRegistry()

    inv = await registry.execute("not_registered", {"a": 1}, _ctx())

    assert inv.result == denial
    assert inv.error == "ToolPermissionError: reference_skill_unavailable"
    assert inv.duration_ms == 0


async def test_unknown_tool_without_reference_denial_raises_key_error() -> None:
    with pytest.raises(KeyError):
        await ToolRegistry().execute("not_registered", {}, _ctx())


async def test_reference_skill_operation_denial_carries_result_and_error(monkeypatch) -> None:
    denial = {"ok": False, "error_kind": "reference_skill_unavailable", "skill": "method"}

    async def deny(ctx: Any, name: str, arguments: dict) -> dict:
        return denial

    monkeypatch.setattr("bowhead_skill.runtime_scope.reference_skill_operation_denial", deny)
    handlers = _Handlers()
    registry = _registry(handlers, "skill_read")

    inv = await registry.execute("skill_read", {"name": "method"}, _ctx())

    _assert_denied_without_dispatch(inv, handlers)
    assert inv.result == denial
    assert inv.error == "ToolPermissionError: reference_skill_unavailable"


async def test_document_latch_wins_over_role_acl() -> None:
    handlers = _Handlers()
    registry = _registry(handlers, "bi_probe", roles=frozenset({"analyst"}))
    ctx = _ctx(role="lead")
    ctx.document_turn_state = DocumentTurnState(generation_selected=True, native_in_batch=True)

    inv = await registry.execute("bi_probe", {}, ctx)

    assert inv.error is None
    assert inv.result["error"] == "document_workflow_required"


async def test_role_acl_wins_over_skill_scope() -> None:
    handlers = _Handlers()
    registry = _registry(handlers, "skill_invoke", roles=frozenset({"analyst"}))
    ctx = _ctx(role="lead")
    ctx.skill_runtime_scope = _skill_scope()

    inv = await registry.execute("skill_invoke", {"name": "method"}, ctx)

    assert inv.result is None
    assert inv.error is not None and inv.error.startswith("ToolPermissionError: role='lead'")


async def test_direct_execute_does_not_consult_the_approval_gate() -> None:
    """Approval belongs to the presentation stage in ``_execute_tool``.

    Direct callers such as ``ApprovalExecutor`` run an already approved call,
    so ``execute`` itself must never consult the gate.
    """
    consulted: list[str] = []

    async def maybe_gate(**kwargs: Any) -> None:
        consulted.append(kwargs["tool_name"])

    handlers = _Handlers()
    registry = _registry(handlers, "team_write", needs_approval=True)
    ctx = _ctx()
    ctx.approval_gate = SimpleNamespace(maybe_gate=maybe_gate)

    inv = await registry.execute("team_write", {}, ctx)

    assert consulted == []
    assert handlers.calls == ["team_write"]
    assert inv.result == {"ok": True}


# ── Wrapper stage: guards in SubagentRunner._execute_tool ───────────────────


class _StubRouter:
    async def invoke(self, *a: Any, **kw: Any) -> Any:
        raise NotImplementedError

    def stream(self, *a: Any, **kw: Any) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        return None


async def _run_wrapper(registry: ToolRegistry, ctx: ToolExecutionContext, tc: ToolCall) -> tuple[ToolInvocation, list[str]]:
    runner = SubagentRunner(router=_StubRouter(), tool_registry=registry)  # type: ignore[arg-type]
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def consume() -> None:
        async for event in bus.subscribe():
            captured.append(event)

    consumer = asyncio.create_task(consume())
    inv = await runner._execute_tool(
        tc,
        ctx,
        bus,
        parent_turn_id=ctx.turn_id,
        subagent_id="lead_root",
        running_profile="lead",
        concurrency_group_id="grp_policy",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id=ctx.turn_id, payload={}))
    await consumer
    return inv, [event.event_type for event in captured]


async def test_wrapper_skill_scope_denial_returns_before_the_step_card() -> None:
    handlers = _Handlers()
    registry = _registry(handlers, "skill_invoke")
    ctx = _ctx(role="lead")
    ctx.skill_runtime_scope = _skill_scope()

    inv, events = await _run_wrapper(registry, ctx, ToolCall(id="call_skill", name="skill_invoke", arguments={"name": "method"}))

    assert handlers.calls == []
    assert "step_started" not in events
    assert inv.call_id == "call_skill"
    assert inv.error == "ToolPermissionError: skill_unavailable"
    assert inv.result == skill_operation_denial(ctx, "skill_invoke", {"name": "method"})


async def test_wrapper_approval_gate_returns_before_the_step_card() -> None:
    seen: list[dict[str, Any]] = []

    async def maybe_gate(**kwargs: Any) -> ToolInvocation:
        seen.append(kwargs)
        return ToolInvocation(
            tool_name=kwargs["tool_name"],
            arguments={},
            result={"ok": True, "status": "approval_pending", "approval_id": "apr_policy"},
            started_at=utc_now_iso(),
            ended_at=utc_now_iso(),
        )

    handlers = _Handlers()
    registry = _registry(handlers, "team_write", needs_approval=True)
    ctx = _ctx(role="lead")
    ctx.approval_gate = SimpleNamespace(maybe_gate=maybe_gate)

    inv, events = await _run_wrapper(registry, ctx, ToolCall(id="call_write", name="team_write", arguments={"reason": "r"}))

    assert handlers.calls == []
    assert "step_started" not in events
    assert inv.call_id == "call_write"
    assert inv.result["status"] == "approval_pending"
    assert len(seen) == 1
    assert seen[0]["tool_call_id"] == "call_write"
    assert seen[0]["arguments"] == {"reason": "r"}
    assert seen[0]["bus"] is not None


async def test_admit_keeps_the_gate_call_id_for_subagent_spawn() -> None:
    async def maybe_gate(**kwargs: Any) -> ToolInvocation:
        return ToolInvocation(tool_name=kwargs["tool_name"], arguments={}, call_id="tl_gate", result={"status": "approval_pending"})

    handlers = _Handlers()
    registry = _registry(handlers, "subagent_spawn", needs_approval=True)
    ctx = _ctx()
    ctx.approval_gate = SimpleNamespace(maybe_gate=maybe_gate)

    inv = await registry.admit("subagent_spawn", {}, ctx, call_id="call_spawn")

    assert inv is not None
    assert inv.call_id == "tl_gate"


async def test_wrapper_approval_gate_failure_is_an_error_without_dispatch() -> None:
    async def maybe_gate(**kwargs: Any) -> None:
        raise RuntimeError("gate offline")

    handlers = _Handlers()
    registry = _registry(handlers, "team_write", needs_approval=True)
    ctx = _ctx(role="lead")
    ctx.approval_gate = SimpleNamespace(maybe_gate=maybe_gate)

    inv, events = await _run_wrapper(registry, ctx, ToolCall(id="call_write", name="team_write", arguments={}))

    assert handlers.calls == []
    assert "step_started" not in events
    assert inv.call_id == "call_write"
    assert inv.arguments == {}
    assert inv.error == "approval_gate_failed: RuntimeError: gate offline"


async def test_wrapper_registry_denial_follows_the_step_card() -> None:
    handlers = _Handlers()
    registry = _registry(handlers, "bi_probe", roles=frozenset({"analyst"}))
    ctx = _ctx(role="lead")

    inv, events = await _run_wrapper(registry, ctx, ToolCall(id="call_role", name="bi_probe", arguments={}))

    assert handlers.calls == []
    assert "step_started" in events
    assert inv.error is not None and inv.error.startswith("ToolPermissionError: role='lead'")


def test_policy_chain_order_and_stages() -> None:
    from bowhead_core.tool_call_policy import TOOL_CALL_POLICIES, PolicyStage

    assert [(p.name, p.stage, p.before_spec_lookup) for p in TOOL_CALL_POLICIES] == [
        ("skill_scope", PolicyStage.BEFORE_PRESENTATION, False),
        ("approval", PolicyStage.BEFORE_PRESENTATION, False),
        ("reference_skill_context", PolicyStage.BEFORE_DISPATCH, True),
        ("document_latch", PolicyStage.BEFORE_DISPATCH, False),
        ("role_acl", PolicyStage.BEFORE_DISPATCH, False),
        ("skill_scope", PolicyStage.BEFORE_DISPATCH, False),
        ("reference_skill_operation", PolicyStage.BEFORE_DISPATCH, False),
        ("resource_grants", PolicyStage.BEFORE_DISPATCH, False),
        ("datarc_capability", PolicyStage.BEFORE_DISPATCH, False),
        ("connector_binding", PolicyStage.BEFORE_DISPATCH, False),
    ]
