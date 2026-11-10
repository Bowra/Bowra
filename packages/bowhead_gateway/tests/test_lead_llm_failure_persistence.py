"""bi-absent-turn Phase 1 (R4): Lead LLM 抛非超时异常时 turn 必须落终态。

spike-r4.0（docs/sprints/bi-absent-turn-2026-07-29/spike-r4.0.md）实证的缺陷：
subagent.py 的 Lead 失败分支直发 done 绕过 lead_orchestrator._emit_event 的
done chokepoint（card_finalized + D6 hide gate + persist）→ turns 行搁浅
``running``，重启后被 session_store 改写成假因 ``interrupted``。

本测试 = spike 固化（boom 故障组 + control 对照组），R4.6 的 6 项判据逐项断言。
这条路径在 116 prod 一次都没跑过（587 turn 零样本），没有别的守卫。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from typing import TYPE_CHECKING

import pytest
import respx
from cryptography.fernet import Fernet
from gateway_legacy_database import TARGET_FILES, upgrade

if TYPE_CHECKING:
    from pathlib import Path


class SyntheticProviderError(RuntimeError):
    """Synthetic non-timeout provider failure."""


@pytest.fixture
async def r4_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真 gateway lifespan（prod 同款 wiring）+ tmp SQLite + mock LLM。"""
    monkeypatch.chdir(tmp_path)
    db_file = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{(db_file).with_name('bowhead.db')}") == tuple(TARGET_FILES), "迁移未跑到 head"
    monkeypatch.setenv("BOWHEAD_DB_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("BOWHEAD_LLM_PROVIDER", "mock")
    monkeypatch.setenv("BOWHEAD_PRIVACY_LEDGER_DB", str(tmp_path / "privacy" / "deletions.db"))
    # lifespan derives the artifact-fetch key from BOWHEAD_JWT_SECRET, rejecting startup without it; no host env/.env dependency.
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-lead-llm-failure-jwt-secret-32bytes")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    # Batch 2-a §3.2 ②: assembly Router selection decoupled from `BOWHEAD_ENV`, **default refusal**.
    # This file's control group is the repository's only business test truly depending on assembly router echo responses
    # (real lifespan, normal turn through process router), so **explicitly** declare that here.
    # Do not move this to conftest: global declaration would run the whole suite on fake responses, allowing new
    # bypasses silently through the gate, leaving §5.1 guards only production alerts and zero pre-ship signals.
    monkeypatch.setenv("BOWHEAD_ASSEMBLY_ROUTER", "mock")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    # conftest defaults dev fallback=1; licensing SEC-8 forbids local+fallback → use saas as on 116.
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")

    from bowhead_gateway.main import create_app

    app = create_app()
    if app.state.edition == "community":
        from bowhead_core.installation import community_setup_token, initialize_installation, setup_community_owner

        identity = await initialize_installation(str(db_file), "community", admitted_fresh=True)
        await setup_community_owner(
            str(db_file),
            setup_token=community_setup_token(identity),
            team_name="Synthetic Model Failure Team",
            email="setup-owner@example.test",
            password="SyntheticSetupOwner7!",
        )
    async with app.router.lifespan_context(app):
        yield app, db_file


async def _run_seeded_turn(app, *, text: str, team_router=None, live_events=None, through_worker=False, worker_team_id=None) -> str:
    """复刻 chat.py:_persist_turn_row_for_stream 前置两行后跑一个 turn。"""
    from bowhead_core.types import TurnRequest
    from bowhead_gateway.turn_service import admit_legacy_chat_execution

    orch = app.state.orchestrator
    store = orch.session_store
    sid = f"s_r4{uuid.uuid4().hex[:8]}"
    tid = f"t_r4{uuid.uuid4().hex[:8]}"
    request = TurnRequest(
        session_id=sid,
        workspace_id="ws_r4_test",
        user_id="u_r4",
        member_uid="",
        text=text,
        turn_id=tid,
        team_router=team_router,
        team_id=worker_team_id,
    )
    bus = await admit_legacy_chat_execution(orch, request)
    try:
        await store.ensure_session(
            session_id=sid,
            workspace_id="ws_r4_test",
            user_id="u_r4",
        )
        if through_worker:
            from bowhead_gateway.routes.chat import _persist_turn_row_for_stream
            from bowhead_gateway.turn_service import spawn_turn

            await _persist_turn_row_for_stream(orch, request)
            worker = await spawn_turn(orch, request, team_id=worker_team_id, admitted_legacy_bus=bus, cleanup_grace_s=0)
            await worker
        else:
            await store.record_turn(turn_id=tid, session_id=sid, user_text=text)
            await orch.run_turn(request)
    finally:
        if live_events is not None:
            live_events.extend(bus.snapshot())
        await orch.event_registry.close(tid, expected=bus)
    return tid


def _read_turn(db_file: Path, turn_id: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    db = sqlite3.connect(db_file)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT status, error, error_kind, ended_at, final_answer FROM turns WHERE id=?",
        (turn_id,),
    ).fetchone()
    events = db.execute(
        "SELECT event_type, payload_json FROM turn_events WHERE turn_id=? ORDER BY seq",
        (turn_id,),
    ).fetchall()
    db.close()
    return row, events


async def _wait_done_event(db_file: Path, turn_id: str) -> None:
    """turn_events 持久化是 fire-and-forget，轮询到 done 落库为止。"""
    for _ in range(50):
        _, events = _read_turn(db_file, turn_id)
        if any(e["event_type"] == "done" for e in events):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("done event never persisted")


async def test_lead_llm_failure_persists_terminal_state(r4_app) -> None:
    """R4.6 boom 组：非超时异常 → 6 项判据全过（今天全 ❌，见 spike §2 表）。"""
    app, db_file = r4_app
    orch = app.state.orchestrator

    async def _boom(*a, **kw):
        # api_key=sk-… matches scrub rules → also verify R4.3 credentials are not persisted.
        raise SyntheticProviderError("provider down; api_key=sk-verysecret12345abc")

    async def _boom_stream(*a, **kw):
        # Router.stream is an async generator (caller uses `async for`); replacement must match,
        # otherwise failure is the replacement's own TypeError, not the provider exception.
        raise SyntheticProviderError("provider down; api_key=sk-verysecret12345abc")
        yield  # pragma: no cover - makes this an async generator

    orch.router.invoke = _boom
    if hasattr(orch.router, "stream"):
        orch.router.stream = _boom_stream

    tid = await _run_seeded_turn(app, text="帮我分析一下去年营业额")
    await _wait_done_event(db_file, tid)
    row, events = _read_turn(db_file, tid)

    # ① Terminal failed (no longer stranded running).
    assert row is not None
    assert row["status"] == "failed"
    # ② error = stable reason code; ③ error_kind = actual exception type.
    assert row["error"] == "lead_internal_error"
    assert row["error_kind"] == "SyntheticProviderError"
    # ④ Nonempty ended_at; ⑤ chat turn feedback D3: no system failure sentence is persisted or
    # published as the answer; the failure card states the failure from reason_code.
    assert row["ended_at"]
    assert row["final_answer"] in ("", None)
    # ⑥ Event sequence includes step_list_card_finalized (chokepoint only); done exactly once (idempotent).
    types = [e["event_type"] for e in events]
    assert "step_list_card_finalized" in types
    assert types.count("done") == 1
    assert "final_answer" not in types
    # Progressive disclosure contract (user-facing-error-progressive-disclosure decisions 5/8): SSE payload
    # includes no raw exception text/type; error_kind persists only in turns via _pi_turn_state (asserted above);
    # payload contains only stable code + typed diagnostics projection.
    done_payload = json.loads(next(e["payload_json"] for e in events if e["event_type"] == "done"))
    assert done_payload.get("error") == "lead_internal_error"
    # D5: one recovery action, identical on the persisted turn_failed and done.
    failed_payload = json.loads(next(e["payload_json"] for e in events if e["event_type"] == "turn_failed"))
    assert done_payload["recovery_action"] == failed_payload["recovery_action"] == "retry"
    assert "error_detail" not in done_payload
    assert "SyntheticProviderError" not in json.dumps(done_payload)
    diag = done_payload.get("diagnostics")
    assert isinstance(diag, dict) and diag.get("schema_version") == 1
    all_json = "".join(e["payload_json"] or "" for e in events)
    assert "sk-verysecret12345abc" not in all_json
    assert "provider down" not in all_json


async def test_lead_llm_control_turn_unchanged(r4_app) -> None:
    """R4.6 control 组：正常 mock turn 的持久化与事件序不因 R4.1 改变。"""
    app, db_file = r4_app
    tid = await _run_seeded_turn(app, text="帮我分析一下去年营业额")
    await _wait_done_event(db_file, tid)
    row, events = _read_turn(db_file, tid)

    assert row is not None
    assert row["status"] == "completed"
    assert row["error"] is None
    assert row["ended_at"]
    assert row["final_answer"]
    types = [e["event_type"] for e in events]
    assert "step_list_card_finalized" in types
    assert "canvas.done" in types
    assert types.count("done") == 1


@respx.mock
async def test_observed_model_failure_through_mcp_keeps_live_and_saved_diagnosis(r4_app, monkeypatch) -> None:
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.openapi_tool_source import build_team_mcp_server, connect_team_mcp

    from bowhead_router import AgentBinding, ProviderConfig, ProviderRegistry, Router

    app, db_file = r4_app
    orch = app.state.orchestrator
    # The physical usage reader requires the same Space/Team binding as a
    # request admitted by the HTTP route, before allowing a provider dispatch.
    team_id = "team_r4_test"

    async with open_shared_ctx(str(db_file)) as db:
        await db.execute(
            "INSERT INTO teams(id,name,status,created_at,created_by_user_id) VALUES (?,'Synthetic failure Team','active',datetime('now'),'u_r4')",
            (team_id,),
        )
        await db.execute(
            "INSERT INTO users(id,team_id,email,email_verified,display_name,status,created_at) VALUES ('u_r4',?,'failure@example.test',1,'Synthetic user','active',datetime('now'))",
            (team_id,),
        )
        await db.execute(
            "INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at) VALUES ('ws_r4_test',?,'Synthetic failure Space','active',datetime('now'),datetime('now'))",
            (team_id,),
        )
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES ('tm_r4','u_r4',?,'team_admin','active',datetime('now'))",
            (team_id,),
        )
        await db.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at,status) VALUES ('ws_r4_test','u_r4','admin',datetime('now'),'active')",
        )
        await db.commit()
    route = respx.post("https://provider.example/v1/messages").respond(
        500,
        json={"error": {"type": "api_error", "message": "Synthetic upstream unavailable"}},
    )
    registry = ProviderRegistry()
    registry.load(
        [ProviderConfig(name="team_model", type="anthropic_compatible", base_url="https://provider.example", api_key="test-key", models=["model"])]
    )
    team_router = Router(registry)
    team_router.max_retries = 3
    team_router.bind(AgentBinding(agent_type="lead", primary=("team_model", "model")))

    async def no_tools(*args, **kwargs):
        raise AssertionError("A failed model call cannot execute a Tool")

    server = build_team_mcp_server([], no_tools)
    run_turn = orch.run_turn
    wrapped_failures = []

    async def run_with_mcp(request):
        try:
            async with connect_team_mcp(server):
                return await run_turn(request)
        except ExceptionGroup as exc:
            wrapped_failures.append(exc)
            raise

    monkeypatch.setattr(orch, "run_turn", run_with_mcp)
    live = []
    try:
        tid = await _run_seeded_turn(
            app, text="Create a small HTML file.", team_router=team_router, live_events=live, through_worker=True, worker_team_id=team_id
        )
    finally:
        await registry.close_all()
    assert route.call_count == 1
    assert len(wrapped_failures) == 1
    row, events = _read_turn(db_file, tid)
    assert row["status"] == "failed"
    assert row["error"] == "llm_provider_unavailable"
    assert row["error_kind"] == "llm_failed"
    assert row["final_answer"] in (None, "")
    saved = [(event["event_type"], json.loads(event["payload_json"])) for event in events if event["event_type"] in {"turn_failed", "done"}]
    assert [kind for kind, _ in saved] == ["turn_failed", "done"]
    replayed = await app.state.turn_log_store.list_events(tid, event_types=("turn_failed", "done"))
    assert [(event.event_type, event.payload) for event in replayed] == saved
    assert [(event.event_type, event.payload) for event in live if event.event_type in {"turn_failed", "done"}] == saved
    failed, done = (payload for _, payload in saved)
    assert failed["reason_code"] == done["reason_code"] == "llm_provider_unavailable"
    assert failed["diagnostics"] == done["diagnostics"]
    assert failed["recovery_action"] == done["recovery_action"] == "copy_diagnostics"
    diagnosis = done["diagnostics"]
    assert diagnosis["retryable"] is False
    assert diagnosis["provider_message"] == "Synthetic upstream unavailable"
    assert "http_status=500" in diagnosis["safe_detail"]
    assert "outcome=unknown" in diagnosis["safe_detail"]
    assert "TaskGroup" not in json.dumps(saved)


@respx.mock
async def test_observed_invalid_model_key_reaches_failure_card_diagnostics(r4_app) -> None:
    from bowhead_router.invocation import physical_observation_scope

    from bowhead_router import AgentBinding, ProviderConfig, ProviderRegistry, Router

    app, db_file = r4_app
    route = respx.post("https://api.openai.com/v1/chat/completions").respond(
        401,
        json={"error": {"code": "invalid_api_key", "message": "Invalid API key"}},
    )
    registry = ProviderRegistry()
    registry.load(
        [
            ProviderConfig(
                name="team_model", type="openai_compatible", base_url="https://api.openai.com/v1", api_key="invalid-test-key", models=["model"]
            )
        ]
    )
    adapter = registry.get("team_model")
    team_router = Router(registry)
    team_router.bind(AgentBinding(agent_type="lead", primary=("team_model", "model")))

    class ObservedAttempt:
        settled = False

        async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
            assert evidence_code == "provider_http_auth_rejected/v1"
            self.settled = True
            return True

        async def complete(self, *, usage, response_digest: str) -> bool:
            raise AssertionError("A rejected key cannot complete the model call")

        async def unknown(self, *, reason_code: str) -> None:
            raise AssertionError(f"An HTTP 401 response must not become unknown: {reason_code}")

    class Observer:
        def __init__(self) -> None:
            self.attempts: list[ObservedAttempt] = []

        async def begin(self, spec, *, request, body, family) -> ObservedAttempt:
            attempt = ObservedAttempt()
            self.attempts.append(attempt)
            return attempt

    observer = Observer()
    try:
        with physical_observation_scope(observer):
            tid = await _run_seeded_turn(app, text="What is the sales volume?", team_router=team_router)
    finally:
        await adapter.close()

    await _wait_done_event(db_file, tid)
    row, events = _read_turn(db_file, tid)
    assert len(route.calls) == 1
    assert len(observer.attempts) == 1 and observer.attempts[0].settled
    assert row is not None and row["status"] == "failed"
    assert row["error"] == "llm_provider_auth_failed"
    assert row["error_kind"] == "llm_failed"
    failed_payload = json.loads(next(event["payload_json"] for event in events if event["event_type"] == "turn_failed"))
    done_payload = json.loads(next(event["payload_json"] for event in events if event["event_type"] == "done"))
    assert failed_payload["reason"] == done_payload["reason_code"] == "llm_provider_auth_failed"
    assert failed_payload["diagnostics"] == done_payload["diagnostics"]
    assert done_payload["diagnostics"]["provider_message"] == "Invalid API key"
    assert done_payload["diagnostics"]["code"] == "llm_provider_auth_failed"
    assert "http_status=401" in done_payload["diagnostics"]["safe_detail"]
    assert "upstream_code=invalid_api_key" in done_payload["diagnostics"]["safe_detail"]
    assert done_payload["recovery_action"] == "copy_diagnostics"


@respx.mock
async def test_observed_model_rate_limit_reaches_failure_card_diagnostics(r4_app) -> None:
    from bowhead_router.invocation import physical_observation_scope

    from bowhead_router import AgentBinding, ProviderConfig, ProviderRegistry, Router

    app, db_file = r4_app
    route = respx.post("https://api.openai.com/v1/chat/completions").respond(
        429,
        json={"error": {"code": "rate_limit_exceeded", "message": "Too many requests"}},
    )
    registry = ProviderRegistry()
    registry.load(
        [ProviderConfig(name="team_model", type="openai_compatible", base_url="https://api.openai.com/v1", api_key="test-key", models=["model"])]
    )
    adapter = registry.get("team_model")
    team_router = Router(registry)
    # One configured retry: two stream attempts, then the single invoke fallback.
    team_router.max_retries = 1
    team_router.bind(AgentBinding(agent_type="lead", primary=("team_model", "model")))

    class ObservedAttempt:
        settled = False

        async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
            assert evidence_code == "provider_http_rate_limited/v1"
            self.settled = True
            return True

        async def complete(self, *, usage, response_digest: str) -> bool:
            raise AssertionError("A rate-limited request cannot complete")

        async def unknown(self, *, reason_code: str) -> None:
            raise AssertionError(f"An explicit HTTP 429 must not become unknown: {reason_code}")

    class Observer:
        def __init__(self) -> None:
            self.attempts: list[ObservedAttempt] = []

        async def begin(self, spec, *, request, body, family) -> ObservedAttempt:
            attempt = ObservedAttempt()
            self.attempts.append(attempt)
            return attempt

    observer = Observer()
    try:
        with physical_observation_scope(observer):
            tid = await _run_seeded_turn(app, text="What is the sales volume?", team_router=team_router)
    finally:
        await adapter.close()

    await _wait_done_event(db_file, tid)
    row, events = _read_turn(db_file, tid)
    assert len(route.calls) == 3
    assert len(observer.attempts) == 3 and all(attempt.settled for attempt in observer.attempts)
    assert row is not None and row["status"] == "failed"
    assert row["error"] == "llm_provider_rate_limited"
    failed_payload = json.loads(next(event["payload_json"] for event in events if event["event_type"] == "turn_failed"))
    done_payload = json.loads(next(event["payload_json"] for event in events if event["event_type"] == "done"))
    assert failed_payload["reason"] == done_payload["reason_code"] == "llm_provider_rate_limited"
    assert failed_payload["diagnostics"] == done_payload["diagnostics"]
    assert done_payload["diagnostics"]["code"] == "llm_provider_rate_limited"
    assert "http_status=429" in done_payload["diagnostics"]["safe_detail"]
    assert done_payload["recovery_action"] == "retry"


@pytest.mark.parametrize("outcome", ["recovered", "empty", "length"])
async def test_empty_lead_answer_live_and_persisted_terminal_agree(r4_app, outcome):
    from bowhead_router import LLMChunk, TokenUsage

    app, db_file = r4_app
    orch = app.state.orchestrator
    calls = []

    async def stream(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2 and outcome == "recovered":
            yield LLMChunk(delta="Recovered answer.")
        yield LLMChunk(
            finish_reason="length" if outcome == "length" else "stop",
            usage=TokenUsage(input_tokens=100, output_tokens=4000 if outcome == "length" else 1),
        )

    orch.router.stream = stream
    live = []
    tid = await _run_seeded_turn(app, text="Create the poster from the supplied copy.", live_events=live)
    await _wait_done_event(db_file, tid)
    row, events = _read_turn(db_file, tid)
    assert len(calls) == (3 if outcome == "length" else 2)
    assert row["status"] == ("completed" if outcome == "recovered" else "failed")
    payloads = [(e["event_type"], json.loads(e["payload_json"])) for e in events]
    done = [p for t, p in payloads if t == "done"]
    assert len(done) == 1
    assert [(e.event_type, e.payload) for e in live if e.event_type in {"done", "turn_failed"}] == [
        (t, p) for t, p in payloads if t in {"done", "turn_failed"}
    ]
    if outcome != "recovered":
        reason = "lead_output_exhausted" if outcome == "length" else "lead_empty_answer"
        assert row["error"] == row["error_kind"] == reason
        assert row["final_answer"] in (None, "")
        assert done[0]["error"] == reason and done[0]["status"] == "error"
        assert next(p for t, p in payloads if t == "turn_failed")["reason"] == reason
        assert not any(t == "final_answer" for t, p in payloads)


async def _renew_without_writing(**_kwargs) -> str:
    """Keep a lapsed lease lapsed: only recovery may change its ownership here."""
    return "renewed"


def _lapse_dispatch_lease(db_file: Path, turn_id: str) -> None:
    from datetime import UTC, datetime, timedelta

    with sqlite3.connect(db_file) as connection:
        updated = connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=? AND state='spawned'",
            ((datetime.now(tz=UTC) - timedelta(seconds=40)).isoformat(), turn_id),
        )
        assert updated.rowcount == 1
        connection.commit()


async def _start_generation_then_fail(app, db_file: Path, request, *, started_seconds_ago: float, steer_text: str | None = None):
    """Emulate TurnRunner's generation start, an event-loop stall past the lease, then a grouped failure."""
    import time
    from datetime import UTC, datetime, timedelta

    from bowhead_core.events import OrchestratorEvent

    orch = app.state.orchestrator
    bus, _floor = await orch.event_registry.projection_checkpoint(request.turn_id)
    assert bus is not None
    bus._turn_wall_t0_perf = time.perf_counter() - started_seconds_ago
    await bus.emit(
        OrchestratorEvent(
            event_type="turn_started",
            turn_id=request.turn_id,
            payload={"started_at": (datetime.now(UTC) - timedelta(seconds=started_seconds_ago)).isoformat()},
        )
    )
    if steer_text is not None:
        assert await orch.session_store.append_steer_buffer(
            session_id=request.session_id,
            text=steer_text,
            expected_turn_id=request.turn_id,
        )
    await asyncio.to_thread(_lapse_dispatch_lease, db_file, request.turn_id)
    raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("synthetic helper stream dropped")])


def _terminal_rows(db_file: Path, turn_id: str) -> list[tuple[str, dict]]:
    _row, events = _read_turn(db_file, turn_id)
    return [(e["event_type"], json.loads(e["payload_json"])) for e in events if e["event_type"] in {"turn_failed", "done"}]


async def test_worker_failure_with_lapsed_lease_publishes_its_timed_terminal(r4_app, monkeypatch) -> None:
    """Chat dogfood 2026-11-08 F2/F7: an empty steer buffer cannot suppress the worker's own ``done``.

    The lease lapsed during a stall but nobody took it over. Nothing is buffered,
    so the pre-terminal hook must not fence on the lease; the worker's failure
    ``done`` (a direct emit that bypasses Lead) carries the generation duration
    and agrees live, persisted and on replay.
    """
    app, db_file = r4_app
    orch = app.state.orchestrator
    monkeypatch.setattr(orch.session_store, "heartbeat_chat_spawn_dispatch", _renew_without_writing)

    async def fail(request):
        await _start_generation_then_fail(app, db_file, request, started_seconds_ago=2.0)

    monkeypatch.setattr(orch, "run_turn", fail)
    live = []
    tid = await _run_seeded_turn(app, text="Draw the prime counts.", live_events=live, through_worker=True)

    saved = _terminal_rows(db_file, tid)
    assert [kind for kind, _ in saved] == ["turn_failed", "done"]
    assert [(event.event_type, event.payload) for event in live if event.event_type in {"turn_failed", "done"}] == saved
    replayed = await app.state.turn_log_store.list_events(tid, event_types=("turn_failed", "done"))
    assert [(event.event_type, event.payload) for event in replayed] == saved
    failed, done = (payload for _, payload in saved)
    assert done["status"] == "failed"
    assert done["reason_code"] == failed["reason_code"] == "lead_internal_error"
    assert 2_000 <= done["duration_ms"] < 60_000
    with sqlite3.connect(db_file) as connection:
        # The lapsed-but-untaken lease still released this Turn's active fence.
        assert connection.execute(
            "SELECT s.active_turn_id FROM sessions s JOIN turns t ON t.session_id=s.id WHERE t.id=?",
            (tid,),
        ).fetchone() == (None,)


async def test_lapsed_lease_with_accepted_steer_defers_to_consistent_recovery(r4_app, monkeypatch) -> None:
    """Chat dogfood 2026-11-08 F2/F7: accepted steer work keeps the fence, logs compactly, recovers consistently.

    The parent terminal is withheld (accepted work must become discoverable
    first), the failure is logged without the grouped exception context, and the
    recovered ``done`` repeats the persisted ``turn_failed`` and the generation
    duration instead of a contradictory generic failure.
    """
    from structlog.testing import capture_logs

    app, db_file = r4_app
    orch = app.state.orchestrator
    store = orch.session_store
    monkeypatch.setattr(store, "heartbeat_chat_spawn_dispatch", _renew_without_writing)

    async def fail(request):
        await _start_generation_then_fail(app, db_file, request, started_seconds_ago=3.0, steer_text="Use green bars.")

    monkeypatch.setattr(orch, "run_turn", fail)
    live = []
    with capture_logs() as logs:
        tid = await _run_seeded_turn(app, text="Draw the prime counts.", live_events=live, through_worker=True)

    assert not any(event.event_type == "done" for event in live)
    assert [kind for kind, _ in _terminal_rows(db_file, tid)] == ["turn_failed"]
    hook_failures = [entry for entry in logs if entry["event"] == "steer.preterminal_materialize_failed"]
    assert hook_failures == [
        {
            "event": "steer.preterminal_materialize_failed",
            "log_level": "warning",
            "parent_turn": tid,
            "error_type": "RuntimeError",
            "reason": "legacy_parent_dispatch_not_active",
        }
    ]
    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?", (tid,)).fetchone() == ("drain_pending",)

    assert tid in await store.recover_expired_chat_spawn_dispatches()

    saved = _terminal_rows(db_file, tid)
    assert [kind for kind, _ in saved] == ["turn_failed", "done"]
    replayed = await app.state.turn_log_store.list_events(tid, event_types=("turn_failed", "done"))
    assert [(event.event_type, event.payload) for event in replayed] == saved
    failed, done = (payload for _, payload in saved)
    row, events = _read_turn(db_file, tid)
    assert done["status"] == "failed"
    assert done["reason_code"] == failed["reason_code"] == "lead_internal_error"
    assert done["error"] == failed["message"] == "internal_error"
    assert done["error_kind"] == row["error_kind"] == "lead_internal_error"
    assert done["diagnostics"] == failed["diagnostics"]
    assert done["recovery_action"] == failed["recovery_action"]
    started = next(json.loads(e["payload_json"])["started_at"] for e in events if e["event_type"] == "turn_started")
    from datetime import datetime

    expected_ms = int((datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(started)).total_seconds() * 1000)
    assert done["duration_ms"] == expected_ms > 0
