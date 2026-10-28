"""Approval store + Orchestrator 短路径 + Executor 端到端."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bi_mock.main import app as bi_mock_app
from bowhead_core.access_control import AccessControlStore
from bowhead_core.approval_executor import ApprovalExecutor
from bowhead_core.events import EventBusRegistry, OrchestratorEvent
from bowhead_core.tool_registry import (
    AmbiguousToolEffectOutcomeError,
    ToolRegistry,
    ToolSpec,
)
from bowhead_core.user_store import UserStore
from bowhead_router.types import ToolSchema

from bowhead_core import (
    ApprovalStore,
    build_default_registry,
)
from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
)

if TYPE_CHECKING:
    from pathlib import Path


class _EventSink:
    def __init__(self) -> None:
        self.rows: list[OrchestratorEvent] = []

    async def __call__(self, event: OrchestratorEvent) -> None:
        self.rows.append(event)


class _TurnLogFloor:
    def __init__(self, seq: int) -> None:
        self.seq = seq

    async def max_seq(self, _turn_id: str) -> int:
        return self.seq


async def _approved_probe(
    tmp_path: Path,
    *,
    name: str,
    handler,
) -> tuple[ApprovalStore, object, ApprovalExecutor]:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name=name,
                description="approval effect outcome probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=handler,
            needs_approval=True,
        )
    )
    store = ApprovalStore(db_path=str(tmp_path / f"{name}.db"))
    await store.initialize()
    approval = await store.enqueue(
        turn_id="t",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id="u",
        tool_name=name,
        arguments={},
        reason="probe",
    )
    await store.set_decision(
        approval_id=approval.id,
        status="approved",
        decided_by="admin",
    )
    return (
        store,
        approval,
        ApprovalExecutor(
            approval_store=store,
            tool_registry=registry,
            datarc=None,
        ),
    )


@pytest.mark.asyncio
async def test_legacy_executor_keeps_timeout_projection_compatibility(
    tmp_path: Path,
) -> None:
    async def handler(_args, _ctx):
        raise TimeoutError("response lost")

    _store, approval, executor = await _approved_probe(
        tmp_path,
        name="legacy_timeout_probe",
        handler=handler,
    )
    updated = await executor.execute(approval.id)
    assert updated is not None
    assert updated.status == "execution_failed"
    assert "TimeoutError" in (updated.execution_error or "")


@pytest.mark.asyncio
async def test_v1_effect_timeout_propagates_unknown_without_projection(
    tmp_path: Path,
) -> None:
    async def handler(_args, _ctx):
        raise TimeoutError("response lost")

    store, approval, executor = await _approved_probe(
        tmp_path,
        name="effect_timeout_probe",
        handler=handler,
    )
    with pytest.raises(TimeoutError, match="response lost"):
        await executor.execute_effect(
            approval.id,
            effect_user_id="admin",
            idempotency_key="decision_1",
        )
    assert (await store.get(approval.id)).status == "approved"


@pytest.mark.asyncio
async def test_v1_effect_unexpected_handler_error_is_ambiguous(
    tmp_path: Path,
) -> None:
    async def handler(_args, _ctx):
        raise RuntimeError("provider may have committed")

    store, approval, executor = await _approved_probe(
        tmp_path,
        name="effect_runtime_error_probe",
        handler=handler,
    )
    with pytest.raises(AmbiguousToolEffectOutcomeError) as exc_info:
        await executor.execute_effect(
            approval.id,
            effect_user_id="admin",
            idempotency_key="decision_1",
        )
    assert "RuntimeError" in exc_info.value.error_kind
    assert (await store.get(approval.id)).status == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["team_role_unavailable", "effect_member_missing"],
)
async def test_v1_effect_identity_failure_never_calls_provider(
    tmp_path: Path,
    failure: str,
) -> None:
    provider_calls = 0

    async def handler(_args, _ctx):
        nonlocal provider_calls
        provider_calls += 1
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="v1_identity_probe",
                description="v1 identity probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=handler,
        )
    )
    store = ApprovalStore(db_path=str(tmp_path / f"{failure}.db"))
    await store.initialize()
    approval = await store.enqueue(
        turn_id="t",
        session_id="s",
        workspace_id="ws_1",
        member_uid="requester_member",
        user_id="requester",
        tool_name="v1_identity_probe",
        arguments={},
        reason="probe",
        datarc_workspace_id=("dp_1" if failure == "effect_member_missing" else None),
    )
    await store.set_decision(
        approval_id=approval.id,
        status="approved",
        decided_by="admin",
    )

    class Users:
        async def get_workspace_team_id(self, _workspace_id: str) -> str:
            return "team_1"

        async def get_member_role(
            self,
            _workspace_id: str,
            _user_id: str,
        ) -> str:
            return "ws_admin"

        async def get_team_role(
            self,
            _user_id: str,
            _team_id: str,
        ) -> str:
            if failure == "team_role_unavailable":
                raise RuntimeError("role store unavailable")
            return "team_admin"

    executor = ApprovalExecutor(
        approval_store=store,
        tool_registry=registry,
        datarc=object(),
        active_datarc_project=approval.datarc_workspace_id,
        approver_member_uid="",
        user_store=Users(),
        target_team_id="team_1",
    )

    ok, result, error = await executor.execute_effect(
        approval.id,
        effect_user_id="admin",
        idempotency_key="decision_1",
    )

    assert ok is False
    assert result is None
    assert error is not None
    assert failure.split("_", 1)[0] in error
    assert provider_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_kind", "ambiguous"),
    [("timeout", True), ("protocol", True), ("upstream_5xx", True), ("upstream_4xx", False)],
)
async def test_v1_effect_classifies_provider_error_envelopes(
    tmp_path: Path,
    error_kind: str,
    ambiguous: bool,
) -> None:
    async def handler(_args, _ctx):
        return {
            "ok": False,
            "error_kind": error_kind,
            "error": "provider failed",
        }

    _store, approval, executor = await _approved_probe(
        tmp_path,
        name=f"effect_envelope_{error_kind}",
        handler=handler,
    )
    if ambiguous:
        with pytest.raises(AmbiguousToolEffectOutcomeError):
            await executor.execute_effect(
                approval.id,
                effect_user_id="admin",
                idempotency_key="decision_1",
            )
    else:
        ok, result, error = await executor.execute_effect(
            approval.id,
            effect_user_id="admin",
            idempotency_key="decision_1",
        )
        assert ok is False
        assert result is None
        assert error == "provider failed"


# ─── ApprovalStore ─────────────────────────────────────────
async def test_store_enqueue_list_decide_execute(tmp_path: Path) -> None:
    store = ApprovalStore(db_path=str(tmp_path / "ap.db"))
    await store.initialize()

    a = await store.enqueue(
        turn_id="t1",
        session_id="s1",
        workspace_id="w",
        member_uid="m",
        user_id="u",
        tool_name="bi_update_measures",
        arguments={"payload": {"x": 1}, "reason": "rename"},
        reason="rename",
        risk_level="high",
    )
    assert a.status == "pending"
    assert a.id.startswith("apr_")

    pending = await store.list_pending()
    assert len(pending) == 1

    approved = await store.set_decision(
        approval_id=a.id,
        status="approved",
        decided_by="admin",
    )
    assert approved is not None and approved.status == "approved"

    # Idempotent: a second decision does not overwrite the first.
    again = await store.set_decision(
        approval_id=a.id,
        status="rejected",
        decided_by="admin2",
    )
    assert again is not None and again.status == "approved"

    # Record execution.
    updated = await store.record_execution(
        approval_id=a.id,
        ok=False,
        result=None,
        error="bi write not implemented",
    )
    assert updated is not None
    assert updated.status == "execution_failed"
    assert updated.execution_error == "bi write not implemented"


# ─── Orchestrator approval gating ──────────────────────────
@pytest.fixture
async def datarc_pointing_to_mock():
    import httpx2

    from bowhead_bi_bridge import DatarcClient as DC

    transport = httpx2.ASGITransport(app=bi_mock_app)

    class _M(DC):
        def __init__(self) -> None:
            self._base_url = "http://bi-mock/"
            self._auth_key = "dev"
            self._timeout = 10
            self._http = httpx2.AsyncClient(
                transport=transport,
                base_url="http://bi-mock/",
                headers={"Authorization": "Bearer dev"},
                timeout=10,
            )

    c = _M()
    try:
        yield c
    finally:
        await c.aclose()


def _scripted_router(*responses: LLMResponse) -> Router:
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = list(responses)
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    r = Router(reg)
    for t in ("lead", "modeler", "analyst", "echo", "judge"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


# ─────────────────────────────────────────────────────────────
# DELETED: Sprint θ Phase 8c-B
#
# - test_orchestrator_enqueues_approval_for_write_tool
#
# Asserted that orchestrator-level approval queueing fires when the LLM tool
# loop hits a needs_approval=True tool. After Phase 8c-B-1 (A2 strict), the
# turn_runner LLM tool loop is gone — all dispatch routes through
# lead_orchestrator → SubagentRunner. Approval queueing was not ported to
# SubagentRunner; this is a **known gap** flagged in
# phase-8c-b-test-adapt-strategy.md §4.
#
# ApprovalStore / ApprovalExecutor unit tests below are unaffected — they
# exercise the approval state machine + executor directly without requiring an
# LLM loop integration.
# ─────────────────────────────────────────────────────────────


# ─── Executor (handler errors must still finish the approval state machine and record the error) ───
@pytest.mark.integration
async def test_executor_runs_handler_and_records_error(
    datarc_pointing_to_mock,
    tmp_path: Path,
) -> None:
    """批准后 handler 抛错 → status=execution_failed 且 error 落库。

    2026-07-21：原来这里借 build_default_registry 里 4 个 BI 写 stub「恒 raise」的
    行为做断言；那 4 个工具已整族删除（datarc 无写 API），改用本地合成的 raising
    tool —— 被测的是 executor 的错误记录路径，与具体是哪个工具无关。
    """
    store = ApprovalStore(db_path=str(tmp_path / "ap.db"))
    await store.initialize()

    async def _raising_handler(_args, _ctx):
        raise RuntimeError("write backend not implemented")

    registry = build_default_registry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="approval_failing_probe",
                description="approval executor error-path probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_raising_handler,
            needs_approval=True,
            risk_level="high",
        ),
    )

    a = await store.enqueue(
        turn_id="t",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id="u",
        tool_name="approval_failing_probe",
        arguments={"payload": {}, "reason": "x"},
        reason="x",
        risk_level="high",
    )
    await store.set_decision(approval_id=a.id, status="approved", decided_by="admin")

    executor = ApprovalExecutor(
        approval_store=store,
        tool_registry=registry,
        datarc=datarc_pointing_to_mock,
        active_datarc_project="w",  # Batch 1/c3: explicitly seed Datarc workspace; no workspace_id fallback.
    )
    updated = await executor.execute(a.id)
    assert updated is not None
    # tool_registry.execute catches handler exceptions → invocation.error != None → execution_failed.
    assert updated.status == "execution_failed"
    assert updated.execution_error and "write backend not implemented" in updated.execution_error


@pytest.mark.asyncio
async def test_executor_threads_datarc_runtime_context_to_handler(tmp_path: Path) -> None:
    captured = {}
    datarc_client = object()
    vector_client = object()

    async def _handler(_args, ctx):
        captured["ctx"] = ctx
        return {"project": ctx.resolve_datarc_project()}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="approval_context_probe",
                description="approval context probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_handler,
        ),
    )
    store = ApprovalStore(db_path=str(tmp_path / "ap.db"))
    await store.initialize()
    approval = await store.enqueue(
        turn_id="t",
        session_id="s",
        workspace_id="ws_bowhead",
        member_uid="m",
        user_id="u",
        tool_name="approval_context_probe",
        arguments={},
        reason="probe",
    )
    await store.set_decision(approval_id=approval.id, status="approved", decided_by="admin")

    executor = ApprovalExecutor(
        approval_store=store,
        tool_registry=registry,
        datarc=datarc_client,
        active_datarc_project="dp_approval",
        vector_client=vector_client,
    )

    updated = await executor.execute(approval.id)

    assert updated is not None
    assert updated.status == "executed"
    ctx = captured["ctx"]
    assert ctx.active_datarc_project == "dp_approval"
    assert ctx.resolve_datarc_project() == "dp_approval"
    assert ctx.datarc is datarc_client
    assert ctx.vector_client is vector_client


@pytest.mark.asyncio
async def test_executor_persists_executed_event_after_closed_turn_bus(tmp_path: Path) -> None:
    async def _handler(_args, _ctx):
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="approval_event_probe",
                description="approval event probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_handler,
        ),
    )
    store = ApprovalStore(db_path=str(tmp_path / "ap.db"))
    await store.initialize()
    approval = await store.enqueue(
        turn_id="t_closed",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id="u",
        tool_name="approval_event_probe",
        arguments={},
        reason="probe",
    )
    await store.set_decision(
        approval_id=approval.id,
        status="approved",
        decided_by="admin",
    )

    sink = _EventSink()
    event_registry = EventBusRegistry(default_log_sink=sink)
    bus = await event_registry.get_or_create("t_closed")
    await bus.emit(
        OrchestratorEvent(
            event_type="turn_started",
            turn_id="t_closed",
            payload={},
        )
    )
    await bus.emit(
        OrchestratorEvent(
            event_type="done",
            turn_id="t_closed",
            payload={"status": "completed"},
        )
    )

    executor = ApprovalExecutor(
        approval_store=store,
        tool_registry=registry,
        datarc=None,
        event_registry=event_registry,
        turn_log_store=_TurnLogFloor(2),
    )
    updated = await executor.execute(approval.id)

    assert updated is not None and updated.status == "executed"
    executed = [event for event in sink.rows if event.event_type == "approval.executed"]
    assert len(executed) == 1
    assert executed[0].payload["seq"] == 3
    assert "approval.executed" not in [event.event_type for event in bus.snapshot()]


@pytest.mark.asyncio
async def test_executor_seeds_executed_event_when_turn_bus_is_absent(tmp_path: Path) -> None:
    async def _handler(_args, _ctx):
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="approval_replay_probe",
                description="approval replay probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_handler,
        ),
    )
    store = ApprovalStore(db_path=str(tmp_path / "ap.db"))
    await store.initialize()
    approval = await store.enqueue(
        turn_id="t_absent",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id="u",
        tool_name="approval_replay_probe",
        arguments={},
        reason="probe",
    )
    await store.set_decision(
        approval_id=approval.id,
        status="approved",
        decided_by="admin",
    )

    sink = _EventSink()
    event_registry = EventBusRegistry(default_log_sink=sink)
    executor = ApprovalExecutor(
        approval_store=store,
        tool_registry=registry,
        datarc=None,
        event_registry=event_registry,
        turn_log_store=_TurnLogFloor(16),
    )
    updated = await executor.execute(approval.id)

    assert updated is not None and updated.status == "executed"
    executed = [event for event in sink.rows if event.event_type == "approval.executed"]
    assert len(executed) == 1
    assert executed[0].payload["seq"] == 17
    assert "t_absent" not in event_registry._buses


@pytest.mark.asyncio
async def test_executor_persists_failed_event_for_early_unregistered_tool(tmp_path: Path) -> None:
    store = ApprovalStore(db_path=str(tmp_path / "ap.db"))
    await store.initialize()
    approval = await store.enqueue(
        turn_id="t_early_failure",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id="u",
        tool_name="removed_write_tool",
        arguments={},
        reason="probe",
    )
    await store.set_decision(
        approval_id=approval.id,
        status="approved",
        decided_by="admin",
    )

    sink = _EventSink()
    event_registry = EventBusRegistry(default_log_sink=sink)
    executor = ApprovalExecutor(
        approval_store=store,
        tool_registry=ToolRegistry(),
        datarc=None,
        event_registry=event_registry,
        turn_log_store=_TurnLogFloor(8),
    )
    updated = await executor.execute(approval.id)

    assert updated is not None and updated.status == "execution_failed"
    executed = [event for event in sink.rows if event.event_type == "approval.executed"]
    assert len(executed) == 1
    assert executed[0].payload == {
        "approval_id": approval.id,
        "card_id": "t_early_failure",
        "error": "tool 'removed_write_tool' not registered",
        "evt_seq": 9,
        "ok": False,
        "result_preview": None,
        "result_token_count": -1,
        "seq": 9,
    }
    assert "t_early_failure" not in event_registry._buses


@pytest.mark.asyncio
async def test_executor_rechecks_resource_grants_before_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    from bowhead_cli.migrate import upgrade
    from bowhead_core.session_store import SessionStore

    db = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{db}", target="core") == ("core",)
    users = UserStore(db)
    await users.initialize()
    access = AccessControlStore(db)
    await access.initialize()
    team = await users.create_team(name="Acme", max_seats=10)
    user = await users.create_user(
        team_id=team.id,
        email="user@example.com",
        password="Passw0rd!",
    )
    ws_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=user.id,
    )
    sessions = SessionStore(db)
    await sessions.initialize()
    await sessions.ensure_session(session_id="s", workspace_id=ws_id, user_id=user.id)
    await sessions.record_turn(turn_id="t", session_id="s", user_text="ordinary approved operation")
    await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="tool",
        resource_id="approval_probe",
        principal_type="user",
        principal_id=user.id,
        permission="use",
        effect="deny",
        created_by=user.id,
    )

    called = False

    async def _handler(_args, _ctx):
        nonlocal called
        called = True
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="approval_probe",
                description="approval probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_handler,
        ),
    )
    store = ApprovalStore(db_path=str(tmp_path / "ap.db"))
    await store.initialize()
    approval = await store.enqueue(
        turn_id="t",
        session_id="s",
        workspace_id=ws_id,
        member_uid="m",
        user_id=user.id,
        tool_name="approval_probe",
        arguments={},
        reason="probe",
    )
    await store.set_decision(
        approval_id=approval.id,
        status="approved",
        decided_by="admin",
    )
    executor = ApprovalExecutor(
        approval_store=store,
        tool_registry=registry,
        datarc=object(),
        user_store=users,
        access_control_store=access,
    )

    updated = await executor.execute(approval.id)

    assert updated is not None
    assert updated.status == "execution_failed"
    assert updated.execution_error is not None
    assert "resource grant denied" in updated.execution_error
    assert called is False
