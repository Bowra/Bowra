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
from bowhead_cli.migrate import TARGET_FILES, upgrade
from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from pathlib import Path


class SyntheticProviderError(RuntimeError):
    """Synthetic non-timeout provider failure."""


@pytest.fixture
async def r4_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真 gateway lifespan（prod 同款 wiring）+ tmp SQLite + mock LLM。"""
    monkeypatch.chdir(tmp_path)
    db_file = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{db_file}") == tuple(TARGET_FILES), "迁移未跑到 head"
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


async def _run_seeded_turn(app, *, text: str, team_router=None, live_events=None) -> str:
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
    )
    bus = await admit_legacy_chat_execution(orch, request)
    try:
        await store.ensure_session(
            session_id=sid,
            workspace_id="ws_r4_test",
            user_id="u_r4",
        )
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
