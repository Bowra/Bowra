"""S5 persisted-v1 Gateway/factory integration matrix.

These tests deliberately seed the paused N generation through the real
``TerminalRuntimeFactory`` and then cross the public HTTP decision routes.
Route fixtures that return ``version=None`` cannot certify this contract.
"""

from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import os
import sqlite3
import traceback
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import bowhead_gateway.terminal_protocol_rollout as terminal_rollout
import httpx2
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.access_control import AccessControlStore
from bowhead_core.approval_store import ApprovalStore
from bowhead_core.clarification_store import (
    ClarificationCandidate,
    ClarificationStore,
)
from bowhead_core.events import EventBusRegistry, OrchestratorEvent
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.plan_store import PlanStore
from bowhead_core.session_store import SessionStore
from bowhead_core.terminal_generation_bus import GenerationEventBusRegistry
from bowhead_core.terminal_protocol import TerminalProtocolMalformedError
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
)
from bowhead_core.terminal_runtime_factory import (
    TerminalRuntimeFactory,
    TerminalRuntimePreAdmissionCancelled,
    TerminalRuntimeRegistry,
    TerminalWriterSnapshot,
)
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_core.types import TurnRequest
from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from bowhead_gateway.terminal_approval_runner import TerminalApprovalRunner


@pytest.fixture(autouse=True)
def _enable_dormant_terminal_writer_for_protocol_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", base64.urlsafe_b64encode(b"terminal-test-instance-root-key!").decode())
    monkeypatch.setenv("BOWHEAD_PRIVACY_LEDGER_DB", str(tmp_path / "bowhead.db.privacy"))
    monkeypatch.setattr(
        terminal_rollout,
        "_FRESH_TERMINAL_V1_ADMISSION_RELEASED",
        True,
    )


pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_WRITER = TerminalWriterSnapshot(
    instance_id="gateway_1",
    lease_epoch=7,
    admission_epoch=3,
    rollout_config_generation="rollout_1",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Entitlements:
    async def assert_can_run_turn(self, _team_id: str) -> None:
        return None


class _Orchestrator:
    def __init__(
        self,
        session_store: SessionStore,
        event_registry: EventBusRegistry,
    ) -> None:
        self.session_store = session_store
        self.event_registry = event_registry
        self.dispatch_owner_id = "gateway-test"
        self.calls: list[TurnRequest] = []
        # Sessions whose v1 turn must stay active until the test sets the event.
        self.held_sessions: dict[str, asyncio.Event] = {}

    async def run_turn(self, request: TurnRequest) -> Any:
        self.calls.append(request)
        runtime = request.terminal_runtime
        if runtime is None:
            return SimpleNamespace(tokens_input=0, tokens_output=0)
        hold = self.held_sessions.get(request.session_id)
        if hold is not None:
            await hold.wait()
        await runtime.bus.emit(
            OrchestratorEvent(
                event_type="final_answer",
                turn_id=request.turn_id,
                payload={"text": "resumed exactly once"},
            )
        )
        await runtime.bus.emit(
            OrchestratorEvent(
                event_type="done",
                turn_id=request.turn_id,
                payload={
                    "status": "completed",
                    "final_answer": "resumed exactly once",
                    "tokens_input": 0,
                    "tokens_output": 0,
                    "cost_usd": 0,
                },
            )
        )
        return SimpleNamespace(tokens_input=0, tokens_output=0)


class _ProviderExecutor:
    approver_member_uid = "m_1"

    def __init__(self) -> None:
        self.calls = 0

    async def execute_effect(
        self,
        _approval_id: str,
        *,
        effect_user_id: str,
        idempotency_key: str,
    ) -> tuple[bool, dict[str, object], None]:
        assert effect_user_id == "u_1"
        assert idempotency_key
        self.calls += 1
        return True, {"provider": "applied", "call": self.calls}, None


def _initialize_db(path: str) -> None:
    now = datetime.now(tz=UTC)
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at,created_by_user_id) VALUES ('team_1','Team','active',?,'u_1')",
            (now.isoformat(),),
        )
        connection.execute(
            "INSERT INTO users(id,team_id,email,email_verified,display_name,status,created_at) "
            "VALUES ('u_1','team_1','owner@example.test',1,'Owner','active',?)",
            (now.isoformat(),),
        )
        connection.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES ('tm_1','u_1','team_1','team_admin','active',?)",
            (now.isoformat(),),
        )
        # Since f411780cf, clarification resume also uses unified credit admission (balance <=0 gives 402); fund both team pool and member.
        # Credit-cycle reset restores balance to quota (user_store monthly reset), so set both quota and balance.
        connection.execute("UPDATE teams SET team_credit_quota=100000,team_credit_balance=100000 WHERE id='team_1'")
        connection.execute("UPDATE users SET credit_quota=100000,credit_balance=100000 WHERE id='u_1'")
        connection.execute(
            "INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at) VALUES ('ws_1','team_1','Space','active',?,?)",
            (now.isoformat(), now.isoformat()),
        )
        connection.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at,status) VALUES ('ws_1','u_1','admin',?,'active')",
            (now.isoformat(),),
        )
        connection.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents) "
            "VALUES ('s_1','ws_1','u_1','Session',?,?,'active',0,0,'[]')",
            (now.isoformat(), now.isoformat()),
        )
        connection.execute(
            "INSERT INTO terminal_protocol_writer_lease("
            "lease_name,owner_instance_id,epoch,admission_state,admission_epoch,"
            "rollout_config_generation,lease_expires_at,heartbeat_at) "
            "VALUES ('v1_writer','gateway_1',7,'open',3,'rollout_1',?,?)",
            (
                (now + timedelta(minutes=5)).isoformat(),
                now.isoformat(),
            ),
        )
        connection.commit()


async def _build_app(
    db_path: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    factory_fault: TerminalProtocolFaultPoint | None = None,
) -> tuple[Any, TerminalProtocolStore, TerminalRuntimeFactory, _Orchestrator]:
    user_store = UserStore(db_path)
    await user_store.initialize()
    access_store = AccessControlStore(db_path)
    await access_store.initialize()
    session_store = SessionStore(db_path)
    await session_store.initialize()
    clarification_store = ClarificationStore(db_path)
    await clarification_store.initialize()
    approval_store = ApprovalStore(db_path)
    await approval_store.initialize()
    user_memory_store = UserMemoryStore(db_path)
    await user_memory_store.initialize()
    project_memory_store = ProjectMemoryStore(db_path)
    await project_memory_store.initialize()
    store = TerminalProtocolStore(db_path)
    runtimes = TerminalRuntimeRegistry()
    buses = GenerationEventBusRegistry()
    fired = False

    def inject(point: TerminalProtocolFaultPoint) -> None:
        nonlocal fired
        if not fired and point is factory_fault:
            fired = True
            raise RuntimeError(f"injected:{point.value}")

    factory = TerminalRuntimeFactory(
        store,
        buses,
        runtimes,
        _WRITER,
        fault_injector=inject if factory_fault is not None else None,
    )
    turn_log_store = TurnLogStore(db_path)
    await turn_log_store.initialize()
    orchestrator = _Orchestrator(
        session_store,
        EventBusRegistry(
            default_log_sink=OrderedTurnLogSink(turn_log_store),
        ),
    )
    app = create_app()
    settings = Settings()
    settings.auth_mode = "dev"
    settings.dev_default_user = "u_1"
    app.state.settings = settings
    app.state.deployment_mode = "saas"
    app.state.user_store = user_store
    app.state.access_control_store = access_store
    app.state.session_store = session_store
    app.state.turn_log_store = turn_log_store
    app.state.clarification_store = clarification_store
    app.state.approval_store = approval_store
    app.state.user_memory_store = user_memory_store
    app.state.project_memory_store = project_memory_store
    app.state.terminal_protocol_store = store
    app.state.terminal_runtime_factory = factory
    app.state.terminal_writer_service = SimpleNamespace(
        ready=True,
        fresh_admission_ready=(terminal_rollout.fresh_terminal_v1_admission_enabled()),
        writer_snapshot=lambda: _WRITER,
    )
    app.state.terminal_runtime_registry = runtimes
    app.state.terminal_generation_bus_registry = buses
    app.state.entitlements = _Entitlements()
    app.state.orchestrator = orchestrator
    app.state.notification_service = None

    async def datarc_context(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            active_datarc_project=None,
            datarc_project_for_calls=None,
            datarc_client=None,
            datarc_unavailable=False,
            vector_client=None,
            active_datarc_workspace_name="",
            accessible_datarc_workspaces=(),
            datarc_absence_reason="",
        )

    async def team_runtime(*_args: Any, **_kwargs: Any) -> tuple[object, object]:
        return object(), object()

    monkeypatch.setattr(
        "bowhead_gateway.datarc_context.resolve_datarc_turn_context",
        datarc_context,
    )
    monkeypatch.setattr(
        "bowhead_gateway.terminal_pending_execution.resolve_terminal_team_runtime",
        team_runtime,
    )
    return app, store, factory, orchestrator


async def _seed_pause(
    app: Any,
    factory: TerminalRuntimeFactory,
    *,
    status: str,
    resumable: bool = True,
) -> Any:
    auth_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="need a decision",
        turn_id="t_1",
        team_id="team_1",
        terminal_auth_fence_vector_digest=auth_digest,
    )
    runtime = await factory.admit_web_turn(
        request,
        request_id="req_1",
        presentation_locale="zh-CN",
    )
    await runtime.start_execution()
    source = None
    if status == "clarification_pending":
        source = await app.state.clarification_store.put(
            turn_id="t_1",
            user_id="u_1",
            workspace_id="ws_1",
            token="",
            rationale="choose a region",
            candidates=(
                ClarificationCandidate(
                    value="north",
                    label="North",
                    confidence="high",
                ),
            ),
            kind="options",
            session_id="s_1",
            resume_context=(
                {
                    "session_id": "s_1",
                    "workspace_id": "ws_1",
                    "team_id": "team_1",
                    "user_id": "u_1",
                    "member_uid": "m_1",
                    "user_text": "analyze sales",
                }
                if resumable
                else None
            ),
        )
    elif status == "approval_pending":
        source = await app.state.approval_store.enqueue(
            turn_id="t_1",
            session_id="s_1",
            workspace_id="ws_1",
            member_uid="m_1",
            user_id="u_1",
            tool_name="fake_write",
            arguments={"value": 7},
            reason="test write",
            risk_level="high",
        )
    else:  # pragma: no cover - test helper contract
        raise AssertionError(status)
    await runtime.bus.emit(
        OrchestratorEvent(
            event_type="done",
            turn_id="t_1",
            payload={"status": status},
        )
    )
    await runtime.retire()
    return source


async def _wait_for_receipt(
    db_path: str,
    *,
    generation: int,
    turn_id: str = "t_1",
    attempts: int = 100,
) -> tuple[str, str | None]:
    for _ in range(attempts):
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT status,reason_code FROM turn_terminal_receipts WHERE turn_id=? AND generation=?",
                (turn_id, generation),
            ).fetchone()
        if row is not None:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"receipt for generation {generation} did not commit")


async def _drain_turn_tasks() -> None:
    from bowhead_gateway.routes.chat import _inflight_tasks

    tasks = tuple(task for task in _inflight_tasks if task.get_name() == "turn_resume:t_1")
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


_GATEWAY_JWT_SECRET = "test-terminal-s5-gateway-jwt-secret-32b"
# Upper bound for the spawned hard-kill Gateway to start and hit its fault
# point. Only a hung child should approach it.
_HARD_KILL_CHILD_EXIT_TIMEOUT_SECONDS = 90
# The hard-kill test runs three phases back to back: the cold child (bounded
# above), the wait for the child's real 30s writer lease to expire, and a second
# full Gateway lifespan (startup, recovery, shutdown). Measured: ~55s alone on a
# loaded machine, ~90s with ten CPU burners; in a full `-n 6` suite run the child
# needed ~42s from startup to ready and the global 120s timeout fired during the
# second lifespan's shutdown. The test-level bound covers the child bound,
# the lease wait and a second lifespan under the same contention, so the child
# bound above still fires first and reports its own assertion.
_HARD_KILL_TEST_TIMEOUT_SECONDS = _HARD_KILL_CHILD_EXIT_TIMEOUT_SECONDS + 30 + 120


def _configure_real_gateway_environment(db_path: str) -> None:
    """Configure the spawned process exactly like a small SaaS Gateway."""

    os.environ["BOWHEAD_DB_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["BOWHEAD_DEPLOYMENT_MODE"] = "saas"
    os.environ["BOWHEAD_SANDBOX_BACKEND"] = "subprocess"
    os.environ["BOWHEAD_ASSEMBLY_ROUTER"] = "mock"
    os.environ["BOWHEAD_AUTH_MODE"] = "dev"
    os.environ["BOWHEAD_DEV_DEFAULT_USER"] = "u_1"
    os.environ["FEATURE_DURABLE_CHAT_EXECUTION_V1"] = "rollout"
    os.environ["FEATURE_DURABLE_CHAT_EXECUTION_V1_PCT"] = "100"
    # lifespan derives artifact-fetch key from JWT secret, rejecting startup without it; spawned processes do not inherit monkeypatch.
    os.environ["BOWHEAD_JWT_SECRET"] = _GATEWAY_JWT_SECRET
    os.environ["BOWHEAD_FERNET_KEY"] = base64.urlsafe_b64encode(b"terminal-test-instance-root-key!").decode()
    os.environ["BOWHEAD_PRIVACY_LEDGER_DB"] = db_path + ".privacy"


async def _hard_kill_resume_child_main(
    db_path: str,
    request_id: str,
) -> None:
    """Start a real Gateway and die after the durable N+1 open commit."""

    import bowhead_core.terminal_protocol_store as protocol_store_module
    import bowhead_gateway.datarc_context as datarc_context_module
    import bowhead_gateway.terminal_pending_execution as pending_module

    # The production lease remains real.  One second keeps this process-level
    # test bounded while still requiring boot two to wait for natural expiry;
    # the parent never clears or rewrites the crashed process' lease.
    protocol_store_module._RECOVERY_LEASE_SECONDS = 1

    async def datarc_context(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            active_datarc_project=None,
            datarc_project_for_calls=None,
            datarc_client=None,
            datarc_unavailable=False,
            vector_client=None,
            active_datarc_workspace_name="",
            accessible_datarc_workspaces=(),
            datarc_absence_reason="",
        )

    async def team_runtime(
        *_args: Any,
        **_kwargs: Any,
    ) -> tuple[object, object]:
        return object(), object()

    datarc_context_module.resolve_datarc_turn_context = datarc_context
    pending_module.resolve_terminal_team_runtime = team_runtime

    app = create_app()
    async with app.router.lifespan_context(app):
        writer_service = app.state.terminal_writer_service
        fired = False

        def kill_after_open(point: TerminalProtocolFaultPoint) -> None:
            nonlocal fired
            if not fired and point is TerminalProtocolFaultPoint.RESUME_AFTER_OPEN_BEFORE_CLAIM:
                fired = True
                # This is intentionally not an exception: exception unwinding
                # would execute lifespan shutdown and release the writer lease.
                os._exit(73)

        crash_clock = datetime.now(tz=UTC) - timedelta(minutes=5)
        crash_store = TerminalProtocolStore(
            db_path,
            _clock=lambda: crash_clock,
            _fault_injector=kill_after_open,
        )
        runtimes = TerminalRuntimeRegistry()
        factory = TerminalRuntimeFactory(
            crash_store,
            GenerationEventBusRegistry(),
            runtimes,
            writer_service.writer_snapshot(),
            writer_snapshot_provider=writer_service.writer_snapshot,
            worker_task_observer=writer_service.observe_worker_task,
            writer_ready=writer_service.start,
        )
        app.state.terminal_protocol_store = crash_store
        app.state.terminal_runtime_factory = factory
        app.state.terminal_runtime_registry = runtimes
        app.state.entitlements = _Entitlements()
        app.state.orchestrator = _Orchestrator(
            app.state.session_store,
            app.state.orchestrator.event_registry,
        )

        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://gateway",
            headers={"x-bowhead-user-id": "u_1"},
        ) as client:
            resolved = await client.post(
                f"/api/v1/clarifications/{request_id}/resolve",
                json={"chosen_value": "north", "remember": False},
            )
            if resolved.status_code != 200:
                raise AssertionError(resolved.text)
            # The call cannot return: TerminalProtocolStore commits N+1 and
            # invokes the hard-kill point before the runtime can claim it.
            await client.post(f"/api/v1/clarifications/{request_id}/resume_turn")
    raise AssertionError("hard-kill fault point did not fire")


def _hard_kill_resume_child(db_path: str, request_id: str) -> None:
    """Multiprocessing entry point with diagnostic exit codes."""

    _configure_real_gateway_environment(db_path)
    # ``spawn`` starts a fresh interpreter, so the parent's autouse monkeypatch
    # is intentionally absent. This test exercises the dormant v1 recovery
    # implementation, not production admission; enable it inside the child.
    terminal_rollout._FRESH_TERMINAL_V1_ADMISSION_RELEASED = True
    try:
        asyncio.run(_hard_kill_resume_child_main(db_path, request_id))
    except BaseException:
        traceback.print_exc()
        os._exit(72)
    os._exit(71)


def _assert_exact_n_plus_one(db_path: str, expected: tuple[str, str | None]) -> None:
    with sqlite3.connect(db_path) as connection:
        generations = connection.execute("SELECT generation,state FROM turn_generations WHERE turn_id='t_1' ORDER BY generation").fetchall()
        receipts = connection.execute("SELECT status,reason_code FROM turn_terminal_receipts WHERE turn_id='t_1' AND generation=2").fetchall()
        turn_usage = connection.execute(
            "SELECT SUM(cost_amount),SUM(input_tokens+output_tokens) FROM turn_generations WHERE turn_id='t_1'"
        ).fetchone()
    assert generations == [(1, "closed"), (2, "closed")]
    assert receipts == [expected]
    assert turn_usage == (0.0, 0)


@pytest.mark.parametrize(
    "writer_config",
    [None, "off", "rollout"],
)
async def test_typed_start_requires_internal_release_and_explicit_writer(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    writer_config: str | None,
) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    with sqlite3.connect(db_path) as connection:
        now = datetime.now(tz=UTC).isoformat()
        connection.execute(
            "UPDATE users SET credit_balance=999999 WHERE id='u_1'",
        )
        connection.execute(
            "UPDATE teams SET team_credit_balance=999999 WHERE id='team_1'",
        )
        connection.execute(
            "INSERT INTO turns(id,session_id,seq,user_text,status,started_at,ended_at) VALUES ('t_planning_source','s_1',1,'短分析','failed',?,?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO chat_turn_user_sources("
            "turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,"
            "attachment_refs_json,parts_json,created_at) "
            "VALUES ('t_planning_source','s_1','u_1','ws_1','短分析','[]','[]',NULL,?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents) "
            "VALUES ('s_plan','ws_1','u_1','Plan Session',?,?,'active',0,0,'[]')",
            (now, now),
        )
        connection.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents) "
            "VALUES ('s_plan_spawn_failure','ws_1','u_1','Plan Spawn Failure Session',?,?,'active',0,0,'[]')",
            (now, now),
        )
        for session_id, title in (
            ("s_plan_pre_admission", "Plan Pre-admission Failure Session"),
            ("s_plan_readiness_cancel", "Plan Readiness Cancel Session"),
            ("s_plan_admission_unknown", "Plan Admission Unknown Session"),
            ("s_plan_stale_owner", "Plan Stale Owner Session"),
            ("s_plan_action_race", "Plan Action Race Session"),
        ):
            connection.execute(
                "INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents) "
                "VALUES (?,?, 'u_1',?,?,?,'active',0,0,'[]')",
                (session_id, "ws_1", title, now, now),
            )
        connection.commit()
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"terminal-protocol-replay-key-32b").decode().rstrip("="),
    )
    if writer_config is None:
        monkeypatch.delenv(
            "FEATURE_DURABLE_CHAT_EXECUTION_V1",
            raising=False,
        )
    else:
        monkeypatch.setenv(
            "FEATURE_DURABLE_CHAT_EXECUTION_V1",
            writer_config,
        )
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1_PCT", "100")
    app, store, _factory, _orchestrator = await _build_app(
        db_path,
        monkeypatch,
    )
    plan_store = PlanStore(db_path)
    await plan_store.initialize()
    app.state.plan_store = plan_store
    app.state.orchestrator.plan_store = plan_store
    plan = await app.state.plan_store.create_plan(
        session_id="s_plan",
        workspace_id="ws_1",
        user_id="u_1",
        title="Durable plan",
    )
    spawn_failure_plan = await app.state.plan_store.create_plan(
        session_id="s_plan_spawn_failure",
        workspace_id="ws_1",
        user_id="u_1",
        title="Durable plan spawn failure",
    )
    pre_admission_plan = await app.state.plan_store.create_plan(
        session_id="s_plan_pre_admission",
        workspace_id="ws_1",
        user_id="u_1",
        title="Durable plan pre-admission failure",
    )
    readiness_cancel_plan = await app.state.plan_store.create_plan(
        session_id="s_plan_readiness_cancel",
        workspace_id="ws_1",
        user_id="u_1",
        title="Durable plan readiness cancellation",
    )
    admission_unknown_plan = await app.state.plan_store.create_plan(
        session_id="s_plan_admission_unknown",
        workspace_id="ws_1",
        user_id="u_1",
        title="Durable plan admission unknown",
    )
    stale_owner_plan = await app.state.plan_store.create_plan(
        session_id="s_plan_stale_owner",
        workspace_id="ws_1",
        user_id="u_1",
        title="Durable plan stale owner",
    )
    action_race_plan = await app.state.plan_store.create_plan(
        session_id="s_plan_action_race",
        workspace_id="ws_1",
        user_id="u_1",
        title="Durable plan action race",
    )
    attachment = SimpleNamespace(
        id="art_terminal",
        filename="terminal.pdf",
        mime="application/pdf",
        size=11,
    )

    class AttachmentStore:
        async def get_ready_uploads_for_chat(
            self,
            artifact_ids: list[str],
            *,
            user_id: str,
            workspace_id: str,
        ) -> dict[str, Any]:
            assert artifact_ids == [attachment.id]
            assert user_id == "u_1"
            assert workspace_id == "ws_1"
            return {attachment.id: attachment}

    app.state.artifact_store = AttachmentStore()

    async def resolve_user_or_stub(_request: Any) -> tuple[str, str]:
        return "u_1", "team_1"

    monkeypatch.setattr(
        "bowhead_gateway.routes.auth.resolve_user_or_stub",
        resolve_user_or_stub,
    )

    async def gate_turn_http(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "bowhead_gateway._entitlement_gate.gate_turn_http",
        gate_turn_http,
    )

    from bowhead_gateway.turn_service import TeamModelRuntimeResolution

    async def resolve_team_model_runtime(*_args: Any, **_kwargs: Any) -> TeamModelRuntimeResolution:
        return TeamModelRuntimeResolution(
            router=object(),
            kb_retriever=object(),
            result="team_safe_success",
            reason="resolved",
        )

    monkeypatch.setattr(
        "bowhead_gateway.turn_service.resolve_team_model_runtime",
        resolve_team_model_runtime,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"x-bowhead-user-id": "u_1"},
    ) as client:
        legacy = await client.post(
            "/api/v1/chat/send",
            json={"workspace_id": "ws_1", "message": "legacy reader"},
        )
        capable = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={"workspace_id": "ws_1", "message": "header-only reader"},
        )
        action_body = {
            "workspace_id": "ws_1",
            "message": "v1 action",
            "attachments": [attachment.id],
            "attachment_refs": [
                {
                    "fileId": attachment.id,
                    "filename": attachment.filename,
                    "mime": attachment.mime,
                    "size": attachment.size,
                }
            ],
            "user_profile": {"language": "en-US"},
            "client_info": {
                "action_kind": "start",
                "request_id": "act_start_33333333333333333333333333333333",
                "user_msg_id": "u_s_33333333333333333333333333333333",
            },
        }
        action_without_reader = await client.post(
            "/api/v1/chat/send",
            json=action_body,
        )
        action = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json=action_body,
        )
        planning_action = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={
                "workspace_id": "ws_1",
                "message": "请做一个全面经营分析报告",
                "planning_mode": "planning",
                "client_info": {
                    "action_kind": "start",
                    "request_id": "act_start_44444444444444444444444444444444",
                    "user_msg_id": "u_s_44444444444444444444444444444444",
                },
            },
        )
        plan_action_race = None
        plan_action_race_retry = None
        plan_action_race_other = None
        plan_action_race_wrong_session = None
        if writer_config == "rollout":
            race_entered = asyncio.Event()
            race_release = asyncio.Event()
            real_race_admit = _factory.admit_web_turn

            async def hold_plan_after_claim(
                turn_request: TurnRequest,
                **kwargs: Any,
            ) -> Any:
                if turn_request.plan_execution_id == action_race_plan.id:
                    race_entered.set()
                    await race_release.wait()
                return await real_race_admit(turn_request, **kwargs)

            monkeypatch.setattr(
                _factory,
                "admit_web_turn",
                hold_plan_after_claim,
            )
            race_body = {
                "workspace_id": "ws_1",
                "session_id": "s_plan_action_race",
                "message": "",
                "plan_execution_id": action_race_plan.id,
                "client_info": {
                    "action_kind": "start",
                    "request_id": ("act_start_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
                    "user_msg_id": ("u_s_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
                },
            }
            first_race_task = asyncio.create_task(
                client.post(
                    "/api/v1/chat/send",
                    headers={
                        "x-bowhead-terminal-protocol-capability": "1",
                    },
                    json=race_body,
                ),
            )
            await asyncio.wait_for(race_entered.wait(), timeout=30)
            plan_action_race_retry = await client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json=race_body,
            )
            plan_action_race_other = await client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json={
                    **race_body,
                    "client_info": {
                        "action_kind": "start",
                        "request_id": ("act_start_cccccccccccccccccccccccccccccccc"),
                        "user_msg_id": ("u_s_cccccccccccccccccccccccccccccccc"),
                    },
                },
            )
            plan_action_race_wrong_session = await client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json={**race_body, "session_id": "s_plan"},
            )
            race_release.set()
            plan_action_race = await first_race_task
            monkeypatch.setattr(
                _factory,
                "admit_web_turn",
                real_race_admit,
            )
        plan_action_body = {
            "workspace_id": "ws_1",
            "session_id": "s_plan",
            "message": "",
            "plan_execution_id": plan.id,
            "expected_plan_revision": plan.current_rev,
            "client_info": {
                "action_kind": "start",
                "request_id": "act_start_55555555555555555555555555555555",
                "user_msg_id": "u_s_55555555555555555555555555555555",
            },
        }
        # Keep the plan turn active so the next distinct start deterministically meets
        # the session-level busy check (the worker otherwise races the next request).
        plan_turn_hold = asyncio.Event()
        app.state.orchestrator.held_sessions["s_plan"] = plan_turn_hold
        plan_action_results = await asyncio.gather(
            client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json=plan_action_body,
            ),
            client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json=plan_action_body,
            ),
        )
        if writer_config == "rollout":
            successful_plan_actions = [result for result in plan_action_results if result.status_code == 200]
            assert successful_plan_actions
            plan_action = successful_plan_actions[0]
            [plan_action_replay] = [result for result in plan_action_results if result is not plan_action]
            plan_action_lookup = await client.get(
                "/api/v1/chat/actions/act_start_55555555555555555555555555555555",
            )
        else:
            plan_action, plan_action_replay = plan_action_results
            plan_action_lookup = None
        plan_different_action = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={
                **plan_action_body,
                "client_info": {
                    "action_kind": "start",
                    "request_id": ("act_start_56565656565656565656565656565656"),
                    "user_msg_id": ("u_s_56565656565656565656565656565656"),
                },
            },
        )
        plan_turn_hold.set()
        if writer_config == "rollout":
            # Later requests run against an idle session again.
            await _wait_for_receipt(
                db_path,
                generation=1,
                turn_id=plan_action.json()["turn_id"],
                attempts=3000,
            )
        plan_action_conflict = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={**plan_action_body, "plan_execution_id": "plan_changed"},
        )
        plan_revision_conflict = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={**plan_action_body, "expected_plan_revision": plan.current_rev + 1},
        )
        plan_action_flag_off_replay = None
        if writer_config == "rollout":
            monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
            plan_action_flag_off_replay = await client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json=plan_action_body,
            )
            monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
            import bowhead_gateway.turn_service as turn_service

            real_spawn_turn = turn_service.spawn_turn

            async def abort_then_fail_spawn(
                _orchestrator: Any,
                turn_request: Any,
                **_kwargs: Any,
            ) -> None:
                await turn_request.terminal_runtime.abort_worker_start_failed()
                raise RuntimeError("worker task creation failed")

            monkeypatch.setattr(
                turn_service,
                "spawn_turn",
                abort_then_fail_spawn,
            )
            plan_spawn_failure = await client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json={
                    "workspace_id": "ws_1",
                    "session_id": "s_plan_spawn_failure",
                    "message": "",
                    "plan_execution_id": spawn_failure_plan.id,
                    "client_info": {
                        "action_kind": "start",
                        "request_id": "act_start_66666666666666666666666666666666",
                        "user_msg_id": "u_s_66666666666666666666666666666666",
                    },
                },
            )
            monkeypatch.setattr(
                turn_service,
                "spawn_turn",
                real_spawn_turn,
            )
            real_fresh_admission_ready = _factory._fresh_admission_ready
            monkeypatch.setattr(
                _factory,
                "_fresh_admission_ready",
                lambda: False,
            )
            plan_pre_admission_failure = await client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json={
                    "workspace_id": "ws_1",
                    "session_id": "s_plan_pre_admission",
                    "message": "",
                    "plan_execution_id": pre_admission_plan.id,
                    "client_info": {
                        "action_kind": "start",
                        "request_id": "act_start_77777777777777777777777777777777",
                        "user_msg_id": "u_s_77777777777777777777777777777777",
                    },
                },
            )
            monkeypatch.setattr(
                _factory,
                "_fresh_admission_ready",
                real_fresh_admission_ready,
            )

            real_admit_web_turn = _factory.admit_web_turn

            async def cancel_before_terminal_store(
                turn_request: TurnRequest,
                **kwargs: Any,
            ) -> Any:
                if turn_request.plan_execution_id == readiness_cancel_plan.id:
                    raise TerminalRuntimePreAdmissionCancelled(
                        "terminal writer readiness cancelled",
                    )
                return await real_admit_web_turn(turn_request, **kwargs)

            monkeypatch.setattr(
                _factory,
                "admit_web_turn",
                cancel_before_terminal_store,
            )
            with pytest.raises(
                TerminalRuntimePreAdmissionCancelled,
                match="terminal writer readiness cancelled",
            ):
                await client.post(
                    "/api/v1/chat/send",
                    headers={"x-bowhead-terminal-protocol-capability": "1"},
                    json={
                        "workspace_id": "ws_1",
                        "session_id": "s_plan_readiness_cancel",
                        "message": "",
                        "plan_execution_id": readiness_cancel_plan.id,
                        "client_info": {
                            "action_kind": "start",
                            "request_id": "act_start_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "user_msg_id": "u_s_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        },
                    },
                )
            monkeypatch.setattr(
                _factory,
                "admit_web_turn",
                real_admit_web_turn,
            )

            real_store_fault_injector = store._fault_injector
            admission_fault_fired = False

            def fail_after_admission_commit(
                point: TerminalProtocolFaultPoint,
            ) -> None:
                nonlocal admission_fault_fired
                if not admission_fault_fired and point is TerminalProtocolFaultPoint.ADMISSION_AFTER_COMMIT:
                    admission_fault_fired = True
                    raise RuntimeError("admission commit acknowledgement lost")

            monkeypatch.setattr(
                store,
                "_fault_injector",
                fail_after_admission_commit,
            )
            plan_admission_unknown = await client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json={
                    "workspace_id": "ws_1",
                    "session_id": "s_plan_admission_unknown",
                    "message": "",
                    "plan_execution_id": admission_unknown_plan.id,
                    "client_info": {
                        "action_kind": "start",
                        "request_id": "act_start_88888888888888888888888888888888",
                        "user_msg_id": "u_s_88888888888888888888888888888888",
                    },
                },
            )
            monkeypatch.setattr(
                store,
                "_fault_injector",
                real_store_fault_injector,
            )

            real_bind_execution_turn = plan_store.bind_execution_turn
            stale_owner_bind_calls = 0

            async def replace_plan_owner_after_admission(
                plan_id: str,
                turn_id: str,
            ) -> bool:
                nonlocal stale_owner_bind_calls
                stale_owner_bind_calls += 1
                assert plan_id == stale_owner_plan.id
                assert turn_id == "t_99999999999999999999999999999999"
                if stale_owner_bind_calls == 1:
                    return await real_bind_execution_turn(plan_id, turn_id)
                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        "UPDATE plans SET execution_turn_id='t_newer_plan_owner' WHERE id=? AND execution_turn_id=?",
                        (plan_id, turn_id),
                    )
                    connection.commit()
                return False

            monkeypatch.setattr(
                plan_store,
                "bind_execution_turn",
                replace_plan_owner_after_admission,
            )
            plan_stale_owner = await client.post(
                "/api/v1/chat/send",
                headers={"x-bowhead-terminal-protocol-capability": "1"},
                json={
                    "workspace_id": "ws_1",
                    "session_id": "s_plan_stale_owner",
                    "message": "",
                    "plan_execution_id": stale_owner_plan.id,
                    "client_info": {
                        "action_kind": "start",
                        "request_id": "act_start_99999999999999999999999999999999",
                        "user_msg_id": "u_s_99999999999999999999999999999999",
                    },
                },
            )
            monkeypatch.setattr(
                plan_store,
                "bind_execution_turn",
                real_bind_execution_turn,
            )
        else:
            plan_spawn_failure = None
            plan_pre_admission_failure = None
            plan_admission_unknown = None
            plan_stale_owner = None
        terminal_submission_body = {
            "workspace_id": "ws_1",
            "message": "v1 composer start",
            "client_submission_id": "22222222-2222-4222-8222-222222222222",
            "submission_kind": "start",
            "submission_contract_version": "chat-submit.v1",
            "model_control_batch": "a",
        }
        terminal_submission = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json=terminal_submission_body,
        )
        terminal_submission_replay = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json=terminal_submission_body,
        )
        planning = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={
                "workspace_id": "ws_1",
                "message": "请做一个全面经营分析报告",
                "planning_mode": "planning",
            },
        )
        auto_planning = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={
                "workspace_id": "ws_1",
                "message": "请做一个全面经营分析报告",
            },
        )
        canonical_replay = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={
                "workspace_id": "ws_1",
                "session_id": "s_1",
                "message": "",
                "client_submission_id": "11111111-1111-4111-8111-111111111111",
                "submission_kind": "replay",
                "submission_contract_version": "chat-submit.v1",
                "model_control_batch": "a",
                "replay_source_turn_id": "t_planning_source",
            },
        )
        action_replay = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json=action_body,
        )
        action_conflict = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={**action_body, "message": "changed v1 action"},
        )
        action_plan_field_conflict = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={**action_body, "plan_execution_id": "plan_added"},
        )
        action_planning_field_conflict = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={**action_body, "planning_mode": "planning"},
        )
        action_lookup = (
            await client.get(
                "/api/v1/chat/actions/act_start_33333333333333333333333333333333",
            )
            if writer_config == "rollout"
            else None
        )

    assert legacy.status_code == 200, legacy.text
    assert "terminal_protocol_version" not in legacy.json()
    assert capable.status_code == 200, capable.text
    assert "terminal_protocol_version" not in capable.json()
    assert action_without_reader.status_code == 409, action_without_reader.text
    assert action_without_reader.json()["detail"]["error"]["code"] == "client_refresh_required"
    assert terminal_submission.status_code == 200, terminal_submission.text
    assert terminal_submission_replay.status_code == 200, terminal_submission_replay.text
    assert terminal_submission_replay.json() == terminal_submission.json()
    assert planning.status_code == 200, planning.text
    assert "terminal_protocol_version" not in planning.json()
    assert auto_planning.status_code == 200, auto_planning.text
    assert "terminal_protocol_version" not in auto_planning.json()
    assert canonical_replay.status_code == 200, canonical_replay.text
    assert "terminal_protocol_version" not in canonical_replay.json()
    if writer_config == "rollout":
        assert action.status_code == 200, action.text
        assert action_replay.status_code == 200, action_replay.text
        assert action_replay.json() == action.json()
        assert action_conflict.status_code == 409, action_conflict.text
        assert action_conflict.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")
        assert action_lookup is not None
        assert action_lookup.status_code == 200, action_lookup.text
        assert action_lookup.json() == {
            "action_id": "act_start_33333333333333333333333333333333",
            "action_kind": "start",
            "outcome": "admitted",
            "session_id": action.json()["session_id"],
            "turn_id": action.json()["turn_id"],
            "execution_generation": 1,
            "terminal_protocol_version": 1,
            "stream_url": action.json()["stream_url"],
        }
        assert action.json()["session_id"] == "s_33333333333333333333333333333333"
        assert action.json()["turn_id"] == "t_33333333333333333333333333333333"
        assert action.json()["execution_generation"] == 1
        assert action.json()["terminal_protocol_version"] == 1
        assert planning_action.status_code == 200, planning_action.text
        assert planning_action.json()["terminal_protocol_version"] == 1
        assert plan_action_race is not None
        assert plan_action_race.status_code == 200, plan_action_race.text
        assert plan_action_race_retry is not None
        assert plan_action_race_retry.status_code == 503
        assert plan_action_race_retry.json()["detail"]["error"]["code"] == ("terminal_dependency_recovering")
        assert plan_action_race_other is not None
        assert plan_action_race_other.status_code == 409
        assert plan_action_race_other.json()["detail"]["error"]["code"] == ("plan_already_running")
        assert plan_action_race_wrong_session is not None
        assert plan_action_race_wrong_session.status_code == 409
        assert plan_action.status_code == 200, plan_action.text
        assert plan_action_replay.status_code in {200, 503}, plan_action_replay.text
        if plan_action_replay.status_code == 200:
            assert plan_action_replay.json() == plan_action.json()
        else:
            assert plan_action_replay.json()["detail"]["error"]["code"] == ("terminal_dependency_recovering")
        assert plan_action_lookup is not None
        assert plan_action_lookup.status_code == 200, plan_action_lookup.text
        assert plan_action_lookup.json()["turn_id"] == plan_action.json()["turn_id"]
        assert plan_action.json()["terminal_protocol_version"] == 1
        assert plan_different_action.status_code == 409
        # Session-level admission runs before the plan status check: a distinct start
        # into a session with an active turn is session_turn_active. plan_already_running
        # for an idle session is covered by plan_action_race_other above.
        assert plan_different_action.json()["detail"]["error"]["code"] == ("session_turn_active")
        assert plan_action_conflict.status_code == 409, plan_action_conflict.text
        assert plan_action_conflict.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")
        assert plan_revision_conflict.status_code == 409
        assert plan_revision_conflict.json()["detail"]["error"]["code"] == "terminal_protocol_admission_rejected"
        assert plan_action_flag_off_replay is not None
        assert plan_action_flag_off_replay.status_code == 200, plan_action_flag_off_replay.text
        assert plan_action_flag_off_replay.json() == plan_action.json()
        assert plan_spawn_failure is not None
        assert plan_spawn_failure.status_code == 503, plan_spawn_failure.text
        assert plan_spawn_failure.json()["detail"]["error"]["code"] == ("turn_dispatch_unavailable")
        assert plan_pre_admission_failure is not None
        assert plan_pre_admission_failure.status_code == 503
        assert plan_pre_admission_failure.json()["detail"]["error"]["code"] == ("terminal_protocol_writer_not_ready")
        assert plan_admission_unknown is not None
        assert plan_admission_unknown.status_code == 503
        assert plan_stale_owner is not None
        assert plan_stale_owner.status_code == 409
        assert plan_stale_owner.json()["detail"]["error"]["code"] == ("plan_execution_claim_lost")
        assert stale_owner_bind_calls == 2
    else:
        assert action.status_code == action_replay.status_code == 409
        assert action_conflict.status_code == 409
        assert action_plan_field_conflict.status_code == 409
        assert action_planning_field_conflict.status_code == 409
        assert action.json()["detail"]["error"]["code"] == ("client_refresh_required")
        assert planning_action.status_code == 409, planning_action.text
        assert planning_action.json()["detail"]["error"]["code"] == "client_refresh_required"
        assert plan_action.status_code == 409, plan_action.text
        assert plan_action_race is None
        assert plan_action_race_retry is None
        assert plan_action_race_other is None
        assert plan_action_race_wrong_session is None
        assert plan_action_replay.status_code == 409, plan_action_replay.text
        assert plan_different_action.status_code == 409
        assert plan_action_conflict.status_code == 409, plan_action_conflict.text
        assert plan_action_flag_off_replay is None
        assert plan_spawn_failure is None
        assert plan_pre_admission_failure is None
        assert plan_admission_unknown is None
        assert plan_stale_owner is None
    _, _, legacy_version = await store.read_persisted_turn_scope(
        turn_id=legacy.json()["turn_id"],
    )
    _, _, capable_version = await store.read_persisted_turn_scope(
        turn_id=capable.json()["turn_id"],
    )
    _, _, terminal_submission_version = await store.read_persisted_turn_scope(
        turn_id=terminal_submission.json()["turn_id"],
    )
    _, _, planning_version = await store.read_persisted_turn_scope(
        turn_id=planning.json()["turn_id"],
    )
    _, _, auto_planning_version = await store.read_persisted_turn_scope(
        turn_id=auto_planning.json()["turn_id"],
    )
    _, _, canonical_replay_version = await store.read_persisted_turn_scope(
        turn_id=canonical_replay.json()["turn_id"],
    )
    assert legacy_version is None
    assert capable_version is None
    assert terminal_submission_version is None
    assert planning_version is None
    assert auto_planning_version is None
    assert canonical_replay_version is None
    with sqlite3.connect(db_path) as connection:
        receipt = connection.execute(
            "SELECT actual_session_id,turn_id FROM chat_submission_receipts WHERE client_submission_id='22222222-2222-4222-8222-222222222222'",
        ).fetchall()
        assert receipt == [
            (
                terminal_submission.json()["session_id"],
                terminal_submission.json()["turn_id"],
            )
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_user_sources WHERE turn_id=?",
            (terminal_submission.json()["turn_id"],),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (terminal_submission.json()["turn_id"],),
        ).fetchone() == (1,)
        if writer_config == "rollout":
            assert connection.execute("SELECT presentation_locale FROM turns WHERE id='t_33333333333333333333333333333333'").fetchone() == ("en-US",)
            action_source = connection.execute(
                "SELECT attachment_ids_json,attachment_refs_json FROM chat_turn_user_sources WHERE turn_id='t_33333333333333333333333333333333'",
            ).fetchone()
            assert action_source is not None
            assert json.loads(action_source[0]) == [attachment.id]
            assert json.loads(action_source[1]) == [
                {
                    "fileId": attachment.id,
                    "filename": attachment.filename,
                    "mime": attachment.mime,
                    "size": attachment.size,
                }
            ]
            planning_envelope = connection.execute(
                "SELECT execution_intent_json FROM turn_generations WHERE turn_id=? AND generation=1",
                (planning_action.json()["turn_id"],),
            ).fetchone()
            assert planning_envelope is not None
            assert json.loads(planning_envelope[0])["planning_mode"] == ("planning")
            plan_envelope = connection.execute(
                "SELECT execution_intent_json FROM turn_generations WHERE turn_id=? AND generation=1",
                (plan_action.json()["turn_id"],),
            ).fetchone()
            assert plan_envelope is not None
            assert json.loads(plan_envelope[0])["plan_execution_id"] == plan.id
            assert json.loads(plan_envelope[0])["expected_plan_revision"] == plan.current_rev
            assert connection.execute(
                "SELECT status,execution_turn_id FROM plans WHERE id=?",
                (spawn_failure_plan.id,),
            ).fetchone() == ("blocked", None)
            assert connection.execute(
                "SELECT status,execution_turn_id FROM plans WHERE id=?",
                (pre_admission_plan.id,),
            ).fetchone() == ("blocked", None)
            assert connection.execute(
                "SELECT status,execution_turn_id FROM plans WHERE id=?",
                (readiness_cancel_plan.id,),
            ).fetchone() == ("blocked", None)
            assert connection.execute(
                "SELECT COUNT(*) FROM turns WHERE id='t_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT COUNT(*) FROM turn_generations WHERE turn_id='t_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT status,execution_turn_id FROM plans WHERE id=?",
                (admission_unknown_plan.id,),
            ).fetchone() == (
                "running",
                "t_88888888888888888888888888888888",
            )
            assert connection.execute(
                "SELECT status,execution_turn_id FROM plans WHERE id=?",
                (stale_owner_plan.id,),
            ).fetchone() == ("running", "t_newer_plan_owner")
            assert connection.execute(
                "SELECT state,recovery_reason,lease_expires_at<=? FROM turn_generations WHERE turn_id=? AND generation=1",
                (
                    datetime.now(tz=UTC).isoformat(),
                    "t_99999999999999999999999999999999",
                ),
            ).fetchone() == (
                "running",
                "worker_start_failed",
                1,
            )
        action_counts = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM sessions WHERE id='s_33333333333333333333333333333333'),
              (SELECT COUNT(*) FROM turns WHERE id='t_33333333333333333333333333333333'),
              (SELECT COUNT(*) FROM turn_generations WHERE turn_id='t_33333333333333333333333333333333'),
              (SELECT COUNT(*) FROM chat_turn_user_sources WHERE turn_id='t_33333333333333333333333333333333')
            """,
        ).fetchone()
        assert action_counts == ((1, 1, 1, 1) if writer_config == "rollout" else (0, 0, 0, 0))


async def test_persisted_v1_resolve_double_resume_and_flag_off(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "off")
    app, _store, factory, orchestrator = await _build_app(
        db_path,
        monkeypatch,
    )
    clarification = await _seed_pause(
        app,
        factory,
        status="clarification_pending",
    )
    metric_names: list[str] = []

    def metric_inc(name: str, **_kwargs: Any) -> None:
        metric_names.append(name)

    monkeypatch.setattr("bowhead_core.metrics.inc", metric_inc)

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"x-bowhead-user-id": "u_1"},
    ) as client:
        resolved = await client.post(
            f"/api/v1/clarifications/{clarification.request_id}/resolve",
            json={"chosen_value": "north", "remember": False},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["decision_id"]
        resolve_replay = await client.post(
            f"/api/v1/clarifications/{clarification.request_id}/resolve",
            json={"chosen_value": "north", "remember": False},
        )
        assert resolve_replay.status_code == 200, resolve_replay.text
        assert resolve_replay.json()["decision_id"] == resolved.json()["decision_id"]
        first, replay = await asyncio.gather(
            client.post(f"/api/v1/clarifications/{clarification.request_id}/resume_turn"),
            client.post(f"/api/v1/clarifications/{clarification.request_id}/resume_turn"),
        )
        assert {first.status_code, replay.status_code}.issubset({200, 202})
        assert 200 in {first.status_code, replay.status_code}
        completed_response = first if first.status_code == 200 else replay
        accepted_response = replay if first.status_code == 200 else first
        assert completed_response.json()["turn_id"] == "t_1"
        if accepted_response.status_code == 202:
            assert accepted_response.json()["state"] == "decision_recorded"

    assert await _wait_for_receipt(db_path, generation=2) == (
        "completed",
        None,
    )
    assert len(orchestrator.calls) == 1
    assert metric_names.count("bowhead_clarification_custom_answer_total") == 1
    _assert_exact_n_plus_one(db_path, ("completed", None))
    await _drain_turn_tasks()


async def test_persisted_v1_stream_replays_checkpoint_and_current_receipt(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    app, _store, factory, _orchestrator = await _build_app(
        db_path,
        monkeypatch,
    )
    await _seed_pause(
        app,
        factory,
        status="clarification_pending",
    )

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"x-bowhead-user-id": "u_1"},
    ) as client:
        response = await client.get("/api/v1/chat/stream/t_1")
        history_response = await client.get("/api/v1/turns/t_1/events")
        replay_response = await client.get("/api/v1/turns/t_1/replay")

    assert response.status_code == 200, response.text
    frames = [block.splitlines() for block in response.text.replace("\r\n", "\n").strip().split("\n\n")]
    assert frames[0][0] == "event: stream.checkpoint"
    checkpoint = json.loads(next(line[6:] for line in frames[0] if line.startswith("data: ")))
    assert checkpoint["terminal_protocol_version"] == 1
    assert checkpoint["current_generation"] == 1
    assert checkpoint["current_receipt_id"]

    done_frame = next(frame for frame in frames if "event: done" in frame)
    done_payload = json.loads(next(line[6:] for line in done_frame if line.startswith("data: ")))
    assert next(line for line in done_frame if line.startswith("id: ")) == f"id: {done_payload['event_seq']}"
    assert done_payload["terminal_protocol_version"] == 1
    assert done_payload["terminal_current"] is True
    assert done_payload["terminal_receipt_id"] == checkpoint["current_receipt_id"]

    assert history_response.status_code == 200, history_response.text
    history = history_response.json()
    assert history["terminal_protocol_version"] == 1
    assert history["current_generation"] == 1
    assert history["events"][-1]["terminal_current"] is True
    assert history["events"][-1]["event_id"]
    assert history["events"][-1]["payload"]["event_seq"] == history["events"][-1]["seq"]

    assert replay_response.status_code == 200, replay_response.text
    assert "event: stream.checkpoint" in replay_response.text
    assert "event: done" in replay_response.text


async def test_v1_read_surfaces_distinguish_malformed_scope_from_dependency_outage(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    app, _store, factory, _orchestrator = await _build_app(
        db_path,
        monkeypatch,
    )
    await _seed_pause(app, factory, status="clarification_pending")

    class _MalformedProtocolStore:
        async def read_persisted_turn_scope(self, *, turn_id: str) -> Any:
            assert turn_id == "t_1"
            raise TerminalProtocolMalformedError("malformed persisted scope")

    app.state.terminal_protocol_store = _MalformedProtocolStore()
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"x-bowhead-user-id": "u_1"},
    ) as client:
        responses = [
            await client.get("/api/v1/chat/stream/t_1"),
            await client.get("/api/v1/turns/t_1/events"),
            await client.get("/api/v1/turns/t_1/replay"),
        ]

    assert [response.status_code for response in responses] == [500, 500, 500]
    assert all(response.json()["detail"]["error"]["code"] == "terminal_protocol_malformed" for response in responses)


async def test_persisted_v1_resume_task_creation_failure_is_durable(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "off")
    app, _store, factory, orchestrator = await _build_app(
        db_path,
        monkeypatch,
        factory_fault=TerminalProtocolFaultPoint.RESUME_BEFORE_TASK_CREATE,
    )
    clarification = await _seed_pause(
        app,
        factory,
        status="clarification_pending",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"x-bowhead-user-id": "u_1"},
    ) as client:
        resolved = await client.post(
            f"/api/v1/clarifications/{clarification.request_id}/resolve",
            json={"chosen_value": "north", "remember": False},
        )
        # Resolve now atomically owns the first resume attempt; task creation
        # failure is therefore projected by this same request rather than a
        # second lossy resolve→resume call.
        assert resolved.status_code in {202, 503}, resolved.text

    assert await _wait_for_receipt(db_path, generation=2) == (
        "aborted",
        "worker_start_failed",
    )
    assert orchestrator.calls == []
    _assert_exact_n_plus_one(
        db_path,
        ("aborted", "worker_start_failed"),
    )


async def test_persisted_v1_dismiss_replay_commits_one_control_generation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "off")
    app, _store, factory, _orchestrator = await _build_app(
        db_path,
        monkeypatch,
    )
    clarification = await _seed_pause(
        app,
        factory,
        status="clarification_pending",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"x-bowhead-user-id": "u_1"},
    ) as client:
        first = await client.post(f"/api/v1/clarifications/{clarification.request_id}/dismiss")
        replay = await client.post(f"/api/v1/clarifications/{clarification.request_id}/dismiss")
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["dismissed"] is True
    assert replay.json()["dismissed"] is True
    assert await _wait_for_receipt(db_path, generation=2) == (
        "cancelled",
        "clarification_dismissed",
    )
    _assert_exact_n_plus_one(
        db_path,
        ("cancelled", "clarification_dismissed"),
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM clarification_requests WHERE request_id=?",
            (clarification.request_id,),
        ).fetchone() == ("dismissed",)
        assert connection.execute("SELECT COUNT(*) FROM turn_control_decisions").fetchone() == (1,)


async def test_persisted_v1_resolve_without_resume_first_and_replay_match(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "off")
    app, _store, factory, orchestrator = await _build_app(
        db_path,
        monkeypatch,
    )
    clarification = await _seed_pause(
        app,
        factory,
        status="clarification_pending",
        resumable=False,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"x-bowhead-user-id": "u_1"},
    ) as client:
        first = await client.post(
            f"/api/v1/clarifications/{clarification.request_id}/resolve",
            json={"chosen_value": "north", "remember": False},
        )
        replay = await client.post(
            f"/api/v1/clarifications/{clarification.request_id}/resolve",
            json={"chosen_value": "north", "remember": False},
        )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json() == replay.json()
    assert first.json()["decision_state"] == "terminal_control_committed"
    assert orchestrator.calls == []
    assert await _wait_for_receipt(db_path, generation=2) == (
        "cancelled",
        "clarification_resolved",
    )
    _assert_exact_n_plus_one(
        db_path,
        ("cancelled", "clarification_resolved"),
    )


async def test_persisted_v1_approval_provider_runs_once_on_replay(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "off")
    app, _store, factory, _orchestrator = await _build_app(
        db_path,
        monkeypatch,
    )
    approval = await _seed_pause(
        app,
        factory,
        status="approval_pending",
    )
    provider = _ProviderExecutor()

    async def executor(*_args: Any, **_kwargs: Any) -> _ProviderExecutor:
        return provider

    monkeypatch.setattr(
        "bowhead_gateway.terminal_pending_execution.build_terminal_approval_executor",
        executor,
    )
    from bowhead_gateway.terminal_decision_authorization import (
        TerminalDecisionAuthorizer,
    )

    app.state.terminal_approval_runner = TerminalApprovalRunner(
        factory=factory,
        approval_store=app.state.approval_store,
        authorizer=TerminalDecisionAuthorizer(app=app),
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"x-bowhead-user-id": "u_1"},
    ) as client:
        first = await client.post(
            f"/api/v1/approvals/{approval.id}/approve",
            json={},
        )
        replay = await client.post(
            f"/api/v1/approvals/{approval.id}/approve",
            json={},
        )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["status"] == "executed"
    assert replay.json()["status"] == "executed"
    assert first.json()["decisionId"] == replay.json()["decisionId"]
    assert first.json()["decisionState"] == replay.json()["decisionState"]
    assert provider.calls == 1
    assert await _wait_for_receipt(db_path, generation=2) == (
        "completed",
        None,
    )
    _assert_exact_n_plus_one(db_path, ("completed", None))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_attempts WHERE state='settled'").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_control_decisions").fetchone() == (1,)


async def test_persisted_v1_approval_reject_replay_commits_one_control_generation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "off")
    app, _store, factory, _orchestrator = await _build_app(
        db_path,
        monkeypatch,
    )
    approval = await _seed_pause(
        app,
        factory,
        status="approval_pending",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"x-bowhead-user-id": "u_1"},
    ) as client:
        first = await client.post(
            f"/api/v1/approvals/{approval.id}/reject",
            json={"reason": "deny"},
        )
        replay = await client.post(
            f"/api/v1/approvals/{approval.id}/reject",
            json={"reason": "deny"},
        )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["status"] == "rejected"
    assert replay.json()["status"] == "rejected"
    assert await _wait_for_receipt(db_path, generation=2) == (
        "cancelled",
        "approval_rejected",
    )
    _assert_exact_n_plus_one(
        db_path,
        ("cancelled", "approval_rejected"),
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_control_decisions").fetchone() == (1,)


@pytest.mark.timeout(_HARD_KILL_TEST_TIMEOUT_SECONDS)
async def test_real_gateway_hard_kill_after_open_recovers_without_n_plus_two(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed Gateway leaves a lease; boot two must recover, not rerun."""

    monkeypatch.chdir(tmp_path)
    db_path = str(tmp_path / "bowhead.db")
    _initialize_db(db_path)
    # Both are real Gateway startups: first upgrade every SQLite target to head, as in production (since a763f7abc, lifespan
    # reads files DB's kb_rebuild_jobs; upgrading core alone fails during startup).
    upgrade(f"sqlite+aiosqlite:///{db_path}")
    seed_app, _store, seed_factory, _orchestrator = await _build_app(
        db_path,
        monkeypatch,
    )
    clarification = await _seed_pause(
        seed_app,
        seed_factory,
        status="clarification_pending",
    )
    # The helper writer is not a running process.  Relinquish only that setup
    # lease before boot one; after the child dies, the test never edits the
    # production lease and boot two must wait for its natural expiry.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE terminal_protocol_writer_lease
            SET owner_instance_id=NULL,admission_state='closed',
                admission_epoch=admission_epoch+1,lease_expires_at=NULL
            WHERE lease_name='v1_writer'
            """
        )
        connection.commit()

    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=_hard_kill_resume_child,
        args=(db_path, clarification.request_id),
        name="terminal-hard-kill-gateway",
    )
    child.start()
    # The child is a cold ``spawn`` interpreter that imports the whole Gateway
    # and runs a full lifespan before reaching the fault point: about 8-12s on
    # an idle machine and 17-27s under xdist plus CPU contention. The contract
    # is only that it reaches the fault point and exits 73, so wait on the exit
    # itself with a bound that is generous relative to cold-start time.
    await asyncio.to_thread(child.join, _HARD_KILL_CHILD_EXIT_TIMEOUT_SECONDS)
    if child.is_alive():
        child.kill()
        await asyncio.to_thread(child.join, 5)
        raise AssertionError("hard-kill Gateway child did not exit")
    assert child.exitcode == 73

    with sqlite3.connect(db_path) as connection:
        generation_rows = connection.execute(
            """
            SELECT generation,state,claim_epoch,execution_started_at
            FROM turn_generations WHERE turn_id='t_1'
            ORDER BY generation
            """
        ).fetchall()
        decision_state = connection.execute("SELECT state FROM turn_control_decisions").fetchone()
        lease_row = connection.execute(
            """
            SELECT owner_instance_id,lease_expires_at
            FROM terminal_protocol_writer_lease
            WHERE lease_name='v1_writer'
            """
        ).fetchone()
    assert len(generation_rows) == 2
    assert generation_rows[0][:3] == (1, "closed", 1)
    assert generation_rows[0][3] is not None
    assert generation_rows[1] == (2, "opened", 0, None)
    assert decision_state == ("generation_opened",)
    assert lease_row is not None
    assert lease_row[0]
    assert lease_row[1]

    lease_expiry = datetime.fromisoformat(str(lease_row[1]).replace("Z", "+00:00"))
    while datetime.now(tz=UTC) <= lease_expiry:
        await asyncio.sleep(0.05)

    for key, value in {
        "BOWHEAD_DB_URL": f"sqlite+aiosqlite:///{db_path}",
        "BOWHEAD_DEPLOYMENT_MODE": "saas",
        "BOWHEAD_SANDBOX_BACKEND": "subprocess",
        "BOWHEAD_ASSEMBLY_ROUTER": "mock",
        "BOWHEAD_AUTH_MODE": "dev",
        "BOWHEAD_DEV_DEFAULT_USER": "u_1",
        "FEATURE_DURABLE_CHAT_EXECUTION_V1": "rollout",
        "FEATURE_DURABLE_CHAT_EXECUTION_V1_PCT": "100",
        "BOWHEAD_JWT_SECRET": _GATEWAY_JWT_SECRET,
    }.items():
        monkeypatch.setenv(key, value)
    import bowhead_core.terminal_protocol_store as protocol_store_module

    monkeypatch.setattr(
        protocol_store_module,
        "_RECOVERY_LEASE_SECONDS",
        1,
    )

    recovered = create_app()
    async with recovered.router.lifespan_context(recovered):
        assert await _wait_for_receipt(db_path, generation=2) == (
            "aborted",
            "terminal_projection_not_committed",
        )
        recovered_orchestrator = _Orchestrator(
            recovered.state.session_store,
            recovered.state.orchestrator.event_registry,
        )
        recovered.state.orchestrator = recovered_orchestrator
        recovered.state.entitlements = _Entitlements()
        transport = httpx2.ASGITransport(app=recovered)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://gateway",
            headers={"x-bowhead-user-id": "u_1"},
        ) as client:
            replay = await client.post(f"/api/v1/clarifications/{clarification.request_id}/resume_turn")
        assert replay.status_code in {200, 202, 409}, replay.text
        assert recovered_orchestrator.calls == []
        assert recovered.state.terminal_runtime_registry._workers == {}
        assert await recovered.state.terminal_protocol_store.count_recovery_inventory() == 0

    _assert_exact_n_plus_one(
        db_path,
        ("aborted", "terminal_projection_not_committed"),
    )
