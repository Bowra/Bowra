"""M156-M3b · Clarification HITL routes 集成测试."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import aiosqlite
import bowhead_gateway.turn_service as turn_service
import httpx2
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.clarification_store import (
    ClarificationCandidate,
    ClarificationStore,
)
from bowhead_core.events import get_event_registry
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.clarifications import (
    QUESTION_SKIP_TEXT,
    QUESTION_SKIP_VALUE,
)
from bowhead_gateway.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path, monkeypatch, completed_community_user_store):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    clarify = ClarificationStore(tmp_path / "c.db")
    await clarify.initialize()
    with sqlite3.connect(tmp_path / "c.db") as connection:
        connection.execute(
            "CREATE TABLE clarification_resolution_outbox ("
            "request_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, "
            "state TEXT NOT NULL, channel TEXT NOT NULL, work_json TEXT NOT NULL, "
            "payload_json TEXT, lease_owner TEXT, lease_expires_at TEXT, "
            "attempt_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE clarification_invalidation_outbox ("
            "request_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, "
            "event_id TEXT NOT NULL UNIQUE, target_status TEXT NOT NULL, "
            "reason_code TEXT NOT NULL, actor_kind TEXT NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'pending', lease_owner TEXT, "
            "lease_expires_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, delivered_at TEXT)"
        )
    # M158-11 / Q5: PreferenceWriter on resolve needs user_memory + project_memory stores.
    from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

    user_mem = UserMemoryStore(tmp_path / "m.db")
    await user_mem.initialize()
    proj_mem = ProjectMemoryStore(tmp_path / "m.db")
    await proj_mem.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.settings.auth_mode = "dev"
    app.state.clarification_store = clarify
    app.state.user_memory_store = user_mem
    app.state.project_memory_store = proj_mem
    app.state.user_store = _LegacyTeamResolver(completed_community_user_store.db_path)
    if app.state.edition == "community":
        from bowhead_core.entitlements import CommunityResolver

        app.state.entitlements = CommunityResolver(mode="saas", user_store=app.state.user_store)
    app.state.terminal_protocol_store = _LegacyProtocolStore(clarify)
    from bowhead_gateway import turn_service

    async def _legacy_team_runtime(*_args, **_kwargs):
        return turn_service.TeamModelRuntimeResolution(
            router=object(),
            result="team_safe_success",
            reason="test_runtime",
        )

    monkeypatch.setattr(
        turn_service,
        "resolve_team_model_runtime",
        _legacy_team_runtime,
    )
    from bowhead_core.events import get_event_registry

    registry = get_event_registry()
    old_sink = registry._default_log_sink

    async def _accept_durable_event(_event) -> bool:
        return True

    registry.set_log_sink(_accept_durable_event)
    try:
        yield app
    finally:
        registry.set_log_sink(old_sink)


@pytest.fixture
async def http(app_ctx):
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield c


async def _drain_preference_writers() -> None:
    """等 PreferenceWriter 的 fire-and-forget 写完（确定性，不用 sleep 轮询）."""
    import asyncio

    from bowhead_gateway.routes.clarifications import _preference_writer_tasks

    while _preference_writer_tasks:
        await asyncio.gather(*tuple(_preference_writer_tasks), return_exceptions=True)


class _LegacyTeamResolver:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.credit_balance = 1_000
        self.credit_events: list[dict] = []

    async def get_user(self, user_id: str):
        assert user_id in {"u_dev_alice", "alice"}
        return SimpleNamespace(
            id=user_id,
            team_id="team_legacy",
            status="active",
            credit_balance=self.credit_balance,
            credit_quota=1_000,
        )

    async def get_team(self, team_id: str):
        assert team_id
        return SimpleNamespace(
            id=team_id,
            status="active",
            default_user_tier="free",
            tier_credits={},
            team_credit_balance=100_000,
            team_credit_quota=100_000,
        )

    async def record_credit_exhaustion_event(self, **kwargs):
        self.credit_events.append(kwargs)
        return {"id": "cex_test", **kwargs}

    async def get_user_team_id(self, user_id: str) -> str:
        assert user_id in {"u_dev_alice", "alice"}
        return "team_legacy"

    async def get_team_role(self, user_id: str, team_id: str) -> str:
        assert user_id in {"u_dev_alice", "alice"}
        assert team_id
        return "member"

    async def get_workspace_team_id(self, workspace_id: str) -> str:
        assert workspace_id
        return "team_resume" if workspace_id == "42" else "team_legacy"

    async def get_member_role(self, workspace_id: str, user_id: str) -> str:
        assert workspace_id
        assert user_id in {"u_dev_alice", "alice"}
        return "member"

    async def get_workspace(self, workspace_id: str) -> dict[str, str]:
        assert workspace_id
        return {"id": workspace_id, "status": "active"}


class _LegacyProtocolStore:
    def __init__(self, clarify: ClarificationStore) -> None:
        self._clarify = clarify

    async def read_persisted_turn_protocol_binding(
        self,
        *,
        team_id: str,
        turn_id: str,
    ) -> tuple[str, None]:
        assert team_id
        async with (
            aiosqlite.connect(self._clarify.db_path) as connection,
            connection.execute(
                """
                SELECT session_id,resume_context_json
                FROM clarification_requests
                WHERE turn_id=? ORDER BY created_at DESC LIMIT 1
                """,
                (turn_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        session_id = row[0] if row is not None else None
        if not session_id and row is not None and row[1]:
            context = json.loads(row[1])
            if isinstance(context, dict):
                session_id = context.get("session_id")
        return str(session_id or f"legacy:{turn_id}"), None


@pytest.fixture
async def seeded_request(app_ctx):
    """预埋一条 pending clarify 请求."""
    store: ClarificationStore = app_ctx.state.clarification_store
    # P0 #2: user_id must match settings.dev_default_user or _current_user_id rejects it.
    return await store.put(
        turn_id="t_test",
        user_id="u_dev_alice",
        workspace_id="42",
        token="晶科",
        rationale="多个候选",
        candidates=[
            ClarificationCandidate(value="晶科能源股份", label="晶科能源股份", confidence="high"),
            ClarificationCandidate(value="晶科科技", label="晶科科技", confidence="med"),
        ],
    )


@pytest.mark.integration
async def test_list_for_turn(http, seeded_request) -> None:
    r = await http.get(f"/api/v1/clarifications/turn/{seeded_request.turn_id}")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["token"] == "晶科"


@pytest.mark.integration
async def test_list_for_turn_filters_other_users(http, seeded_request, app_ctx) -> None:
    store = app_ctx.state.clarification_store
    await store.put(
        turn_id=seeded_request.turn_id,
        user_id="other_user",
        workspace_id="42",
        token="OTHER",
        rationale="",
        candidates=[],
    )

    r = await http.get(f"/api/v1/clarifications/turn/{seeded_request.turn_id}")

    assert r.status_code == 200
    items = r.json()["items"]
    assert [item["token"] for item in items] == ["晶科"]


@pytest.mark.integration
async def test_get_one(http, seeded_request) -> None:
    r = await http.get(f"/api/v1/clarifications/{seeded_request.request_id}")
    assert r.status_code == 200
    assert r.json()["token"] == "晶科"


async def _seed_bounded_card(app_ctx, request_id: str, extra: dict) -> None:
    marker = {
        "contract": "effect-free-questions.v1",
        "round": 1,
        "generation": 1,
        "prior_request_id": None,
        "question_hashes": ["sha256:" + "0" * 64],
    }
    context = {"user_text": "Analyze profit", "__schema_version": 2, "question_round": marker, **extra}
    async with aiosqlite.connect(app_ctx.state.clarification_store.db_path) as db:
        await db.execute(
            "INSERT INTO clarification_requests (request_id, turn_id, user_id, workspace_id, token, rationale, "
            "candidates_json, status, created_at, kind, multi_select, session_id, resume_context_json, tool) "
            "VALUES (?, 't_bounded', 'u_dev_alice', '42', '', 'Which period?', '[]', 'pending', "
            "'2026-10-09T00:00:00+00:00', 'ambiguous_turn', 0, NULL, ?, NULL)",
            (request_id, json.dumps(context, ensure_ascii=False)),
        )
        await db.commit()


@pytest.mark.integration
async def test_get_one_returns_bounded_card_questions(http, app_ctx) -> None:
    """Bounded cards used to return questions: [] because the round marker keeps only hashes."""
    questions = [{"question": "看哪个区域?", "options": ["east", "west"], "type": "single_select", "option_labels": ["华东", "华西"]}]
    await _seed_bounded_card(app_ctx, "clr_bounded", {"card_questions": questions})
    await _seed_bounded_card(app_ctx, "clr_bounded_old", {})

    r = await http.get("/api/v1/clarifications/clr_bounded")
    assert r.status_code == 200, r.text
    assert r.json()["questions"] == questions
    # Cards written before the questions were persisted keep the old empty list.
    old = await http.get("/api/v1/clarifications/clr_bounded_old")
    assert old.status_code == 200, old.text
    assert old.json()["questions"] == []


@pytest.mark.integration
async def test_get_one_404(http) -> None:
    r = await http.get("/api/v1/clarifications/nonexistent")
    assert r.status_code == 404


@pytest.mark.integration
async def test_slow_resolution_winner_cannot_be_overwritten_by_loser(
    http,
    seeded_request,
    app_ctx,
    monkeypatch,
) -> None:
    original_record = app_ctx.state.clarification_store.record_resolution_outcome
    entered = asyncio.Event()

    async def slow_record(**kwargs):
        entered.set()
        await asyncio.sleep(3)
        return await original_record(**kwargs)

    monkeypatch.setattr(app_ctx.state.clarification_store, "record_resolution_outcome", slow_record)
    url = f"/api/v1/clarifications/{seeded_request.request_id}/resolve"
    winner_task = asyncio.create_task(
        http.post(
            url,
            json={"chosen_value": "晶科能源股份", "kind": "dim_value"},
        ),
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    loser = await http.post(
        url,
        json={"chosen_value": "晶科科技", "kind": "dim_value"},
    )
    winner = await winner_task

    assert loser.status_code == 503
    assert loser.json()["detail"]["error"]["code"] == "clarification_resolution_pending"
    assert winner.status_code == 200
    outcome = await app_ctx.state.clarification_store.get_resolution_outcome(
        seeded_request.request_id,
    )
    assert outcome is not None
    assert outcome["chosen_value"] == "晶科能源股份"


@pytest.mark.integration
async def test_expired_web_resolution_recovers_frozen_gateway_work_once(
    http,
    seeded_request,
    app_ctx,
    tmp_path: Path,
) -> None:
    """A crashed winner is recovered from its exact frozen request, not retry input."""
    from bowhead_core.events import get_event_registry
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    clarify: ClarificationStore = app_ctx.state.clarification_store
    frozen_payload = {
        "chosen_value": "晶科能源股份",
        "chosen_values": ["晶科能源股份"],
        "kind": "dim_value",
    }
    resolved, transitioned = await clarify.resolve_once(
        seeded_request.request_id,
        "晶科能源股份",
        chosen_values=["晶科能源股份"],
        resolution_channel="web",
        resolution_work={"payload": frozen_payload},
        resolution_owner="crashed-web-owner",
    )
    assert transitioned is True
    assert resolved is not None
    with sqlite3.connect(clarify.db_path) as connection:
        connection.execute(
            "UPDATE clarification_resolution_outbox SET lease_expires_at='1970-01-01T00:00:00+00:00' WHERE request_id=? AND state='pending'",
            (seeded_request.request_id,),
        )
        connection.commit()

    turn_log = TurnLogStore(str(tmp_path / "recovered-resolution-turn-log.db"))
    await turn_log.initialize()
    app_ctx.state.turn_log_store = turn_log
    sink = OrderedTurnLogSink(turn_log)
    registry = get_event_registry()
    old_sink = registry._default_log_sink
    registry.set_log_sink(sink)
    url = f"/api/v1/clarifications/{seeded_request.request_id}/resolve"
    try:
        recovered = await http.post(
            url,
            json={"chosen_value": "晶科科技", "kind": "dim_value"},
        )
        replay = await http.post(
            url,
            json={"chosen_value": "晶科科技", "kind": "dim_value"},
        )
        await sink.drain()
    finally:
        registry.set_log_sink(old_sink)

    assert recovered.status_code == 200
    assert replay.status_code == 200
    for response in (recovered, replay):
        assert response.json()["chosen_value"] == "晶科能源股份"
        assert response.json()["chosen_values"] == ["晶科能源股份"]

    with sqlite3.connect(clarify.db_path) as connection:
        outbox = connection.execute(
            "SELECT state,work_json,payload_json,attempt_count FROM clarification_resolution_outbox WHERE request_id=?",
            (seeded_request.request_id,),
        ).fetchone()
    assert outbox is not None
    assert outbox[0] == "completed"
    assert json.loads(str(outbox[1])) == {"payload": frozen_payload}
    assert json.loads(str(outbox[2]))["chosen_value"] == "晶科能源股份"
    assert int(outbox[3]) == 2

    rows = await turn_log.list_events(seeded_request.turn_id)
    resolved_events = [row for row in rows if row.event_type == "clarification.resolved"]
    assert len(resolved_events) == 1
    assert resolved_events[0].id == (f"evt_clarification_resolution_{seeded_request.request_id}")
    assert resolved_events[0].payload["chosen_value"] == "晶科能源股份"


@pytest.mark.integration
async def test_resolve_marks_status(http, seeded_request, app_ctx) -> None:
    await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resolve",
        json={"chosen_value": "晶科能源股份"},
    )
    # Individual status=resolved.
    r = await http.get(f"/api/v1/clarifications/{seeded_request.request_id}")
    assert r.json()["status"] == "resolved"
    # Turn pending list empty.
    r2 = await http.get(f"/api/v1/clarifications/turn/{seeded_request.turn_id}")
    assert r2.json()["items"] == []


@pytest.mark.integration
async def test_resolve_structured_answers_project_legacy_values(
    http,
    app_ctx,
    app_with_orch,
) -> None:
    store: ClarificationStore = app_ctx.state.clarification_store
    req = await store.put(
        turn_id="t_answers",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="多问 clarification",
        candidates=[],
        kind="ambiguous_turn",
        resume_context={"user_text": "帮我做个分析", "session_id": "s_answers"},
    )

    payload = {
        "chosen_values": ["legacy should be ignored"],
        "answers": [
            {"question": "主要看哪个区域?", "source": "custom", "answer": "华中"},
            {"question": "重点看哪些指标?", "source": "skip", "skipped": True},
        ],
    }
    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json=payload,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answers"][0]["question"] == "主要看哪个区域?"
    assert body["answers"][0]["source"] == "custom"
    assert body["answers"][0]["answer"] == "华中"
    assert body["answers"][1]["question"] == "重点看哪些指标?"
    assert body["answers"][1]["source"] == "skip"
    assert body["answers"][1]["skipped"] is True
    assert body["chosen_values"] == [
        "主要看哪个区域?: 华中",
        f"重点看哪些指标?: {QUESTION_SKIP_VALUE}",
    ]
    assert body["skipped"] is False
    assert QUESTION_SKIP_VALUE not in str(body["answers"])

    post = await store.get(req.request_id)
    assert post is not None
    assert post.chosen_values == body["chosen_values"]
    assert post.resume_context is not None
    assert post.resume_context == req.resume_context
    assert post.answers == body["answers"]

    replay = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "ignored"},
    )
    assert replay.status_code == 200, replay.text
    replay_body = replay.json()
    assert replay_body["answers"][0]["source"] == "custom"
    assert replay_body["answers"][0]["answer"] == "华中"
    assert replay_body["answers"][1]["source"] == "skip"
    assert replay_body["answers"][1]["skipped"] is True


@pytest.mark.integration
async def test_resolve_404(http) -> None:
    r = await http.post(
        "/api/v1/clarifications/missing/resolve",
        json={"chosen_value": "X"},
    )
    assert r.status_code == 404


@pytest.mark.integration
async def test_resolve_cross_user_denied(http, app_ctx) -> None:
    """P0 #2: cross-user resolve is indistinguishable from a missing row."""
    store = app_ctx.state.clarification_store
    other_req = await store.put(
        turn_id="t_other",
        user_id="other_user",
        workspace_id="42",
        token="X",
        rationale="",
        candidates=[],
    )
    # Explicit dev fallback with no X-Bowhead-User-Id -> u_dev_alice != other_user.
    r = await http.post(
        f"/api/v1/clarifications/{other_req.request_id}/resolve",
        json={"chosen_value": "X"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "clarification_not_found"


@pytest.mark.integration
async def test_get_one_cross_user_denied(http, app_ctx) -> None:
    store = app_ctx.state.clarification_store
    other_req = await store.put(
        turn_id="t_other_get",
        user_id="other_user",
        workspace_id="42",
        token="X",
        rationale="",
        candidates=[],
    )

    r = await http.get(f"/api/v1/clarifications/{other_req.request_id}")

    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "clarification_not_found"


@pytest.mark.integration
async def test_dismiss_cross_user_denied_and_owner_allowed(http, app_ctx) -> None:
    store = app_ctx.state.clarification_store
    other_req = await store.put(
        turn_id="t_other_dismiss",
        user_id="other_user",
        workspace_id="42",
        token="X",
        rationale="",
        candidates=[],
    )

    denied = await http.post(f"/api/v1/clarifications/{other_req.request_id}/dismiss")
    assert denied.status_code == 404
    assert denied.json()["detail"]["code"] == "clarification_not_found"

    owner_req = await store.put(
        turn_id="t_owner_dismiss",
        user_id="u_dev_alice",
        workspace_id="42",
        token="Y",
        rationale="",
        candidates=[],
        resume_context={"user_text": "original question"},
    )
    allowed = await http.post(f"/api/v1/clarifications/{owner_req.request_id}/dismiss")
    assert allowed.status_code == 200
    assert allowed.json()["dismissed"] is True
    assert allowed.json()["original_user_text"] == "original question"


@pytest.mark.integration
async def test_resolve_with_user_header(http, app_ctx) -> None:
    """P0 #2: 带 X-Bowhead-User-Id header 匹配 owner → 通过."""
    store = app_ctx.state.clarification_store
    req = await store.put(
        turn_id="t_h",
        user_id="alice",
        workspace_id="42",
        token="GMV",
        rationale="",
        candidates=[],
    )
    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "营业额"},
        headers={"X-Bowhead-User-Id": "alice"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.integration
async def test_resolve_task_plan_writes_preference_memory(http, app_ctx) -> None:
    """M158-11 / Q5 fix: PreferenceWriter on resolve (kind=task_plan) 写 user_memory +
    project_memory. fire-and-forget asyncio.create_task, 等 scheduler 跑完后验."""

    store = app_ctx.state.clarification_store
    req = await store.put(
        turn_id="t_tp",
        user_id="u_dev_alice",
        workspace_id="ws_paradigm",
        token="__task_plan__",
        rationale="拆 sub_q 校准",
        candidates=[
            ClarificationCandidate(value="0", label="竞品份额对比", confidence="high"),
            ClarificationCandidate(value="1", label="白地市场识别", confidence="high"),
        ],
        kind="task_plan",
        multi_select=True,
    )
    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_values": ["0", "1"]},
    )
    assert r.status_code == 200

    # PreferenceWriter is fire-and-forget; await its task set directly, never fixed-budget polling:
    # 1s can be insufficient under parallel load, testing machine load rather than behavior (eliminates flakes).
    await _drain_preference_writers()

    # Verify user_memory: one preference for each of two candidate labels.
    user_items = await app_ctx.state.user_memory_store.list_for_user("u_dev_alice")
    pick_entries = [e for e in user_items if e.kind == "preference" and e.key.startswith("task_plan_pick_")]
    assert len(pick_entries) == 2
    keys = {e.key for e in pick_entries}
    assert "task_plan_pick_竞品份额对比" in keys
    assert "task_plan_pick_白地市场识别" in keys

    # Verify project_memory: source data aggregated across users (kind=task_plan_pick_count).
    proj_items = await app_ctx.state.project_memory_store.list_for_workspace("ws_paradigm")
    proj_pick = [e for e in proj_items if e.kind == "task_plan_pick_count"]
    assert len(proj_pick) == 2


@pytest.mark.integration
async def test_resolve_task_plan_does_not_write_for_token_kind(
    http,
    seeded_request,
    app_ctx,
) -> None:
    """token kind clarify resolve 不应触发 PreferenceWriter (paradigm v2 仅 task_plan)."""
    import asyncio

    r = await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resolve",
        json={"chosen_value": "晶科能源股份"},
    )
    assert r.status_code == 200
    await asyncio.sleep(0.1)

    user_items = await app_ctx.state.user_memory_store.list_for_user("u_dev_alice")
    pick_entries = [e for e in user_items if e.kind == "preference" and e.key.startswith("task_plan_pick_")]
    assert len(pick_entries) == 0  # Token kind writes no preferences.


@pytest.mark.integration
async def test_resolve_validation(http, seeded_request) -> None:
    # Empty chosen_value.
    r = await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resolve",
        json={"chosen_value": ""},
    )
    assert r.status_code == 422


@pytest.mark.integration
async def test_resolve_structured_answer_rejects_blank_question(http, app_ctx) -> None:
    store: ClarificationStore = app_ctx.state.clarification_store
    req = await store.put(
        turn_id="t_blank_question",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="bad structured answer",
        candidates=[],
        kind="ambiguous_turn",
    )

    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={
            "answers": [
                {"question": "   ", "source": "custom", "answer": "华中"},
            ],
        },
    )

    assert r.status_code == 422


# ─────────────────────────────────────────────────────────────
# Sprint chat-v4-streaming Phase 4C step 4 — resume_turn endpoint
# ─────────────────────────────────────────────────────────────


class _StubOrchestrator:
    """No-op orchestrator that records run_turn calls but does not actually
    drive any agent. Lets us assert resume_turn endpoint spawns the task with
    correct TurnRequest shape without booting LLM router / canvas stores."""

    def __init__(self, db_path: str) -> None:
        self.calls: list = []
        self.session_store = _StubResumeSessionStore(db_path)
        self.event_registry = get_event_registry()

    async def run_turn(self, req):
        self.calls.append(req)
        import asyncio

        await asyncio.sleep(0.05)
        self.session_store._statuses[req.turn_id] = "completed"

        # Return a TurnResult-like object so credit deduct path doesn't crash.
        class _Stub:
            tokens_input = 0
            tokens_output = 0

        return _Stub()


class _StubResumeSessionStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._generations: dict[str, int] = {}
        self._statuses: dict[str, str] = {}
        self._leases: dict[str, tuple[str, str | None, int]] = {}

    async def advance_turn_execution_generation(self, *, turn_id: str) -> int | None:
        if self._statuses.get(turn_id, "clarification_pending") != "clarification_pending":
            return None
        generation = self._generations.get(turn_id, 1) + 1
        self._generations[turn_id] = generation
        self._statuses[turn_id] = "running"
        self._leases[turn_id] = ("claimed", None, generation)
        return generation

    async def get_turn(self, turn_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            execution_generation=self._generations.get(turn_id, 1),
            status=self._statuses.get(turn_id, "clarification_pending"),
        )

    async def is_turn_resume_ready(self, turn_id: str) -> bool:
        return self._statuses.get(turn_id, "clarification_pending") == "clarification_pending"

    async def get_active_resume_dispatch_generation(self, *, turn_id: str):
        lease = self._leases.get(turn_id)
        if lease is None or lease[0] != "spawned":
            return None
        return lease[2]

    async def rollback_turn_resume_generation(self, **kwargs) -> bool:
        turn_id = kwargs["turn_id"]
        owner_id = kwargs["owner_id"]
        lease = self._leases.get(turn_id)
        if lease is None:
            return False
        matches = (lease[0] == "claimed" and owner_id is None) or (lease[0] == "spawned" and lease[1] == owner_id)
        if not matches:
            return False
        self._leases[turn_id] = ("terminal", None, lease[2])
        self._statuses[turn_id] = "clarification_pending"
        return True

    async def claim_chat_spawn_dispatch_for_worker(
        self,
        *,
        turn_id: str,
        owner_id: str,
    ) -> str:
        lease = self._leases.get(turn_id)
        if lease is None or lease[0] != "claimed":
            return "rejected"
        self._leases[turn_id] = ("spawned", owner_id, lease[2])
        return "claimed"

    async def heartbeat_chat_spawn_dispatch(
        self,
        *,
        turn_id: str,
        owner_id: str,
        **_kwargs,
    ) -> str:
        lease = self._leases.get(turn_id)
        return "renewed" if lease is not None and lease[0] == "spawned" and lease[1] == owner_id else "lost"

    async def mark_chat_spawn_dispatch_terminal(
        self,
        *,
        turn_id: str,
        owner_id: str | None = None,
        **_kwargs,
    ) -> bool:
        lease = self._leases.get(turn_id)
        if lease is None or lease[0] not in {"claimed", "spawned"}:
            return False
        if owner_id is not None and lease[1] != owner_id:
            return False
        self._leases[turn_id] = ("terminal", None, lease[2])
        return True

    async def revive_session(self, *_args, **_kwargs) -> bool:
        return False

    async def materialize_steer_follow_up(self, **_kwargs) -> list[str]:
        return []


# After team-connection consolidation, resumed Datarc workspace comes from the **per-user ladder** (accessible set + preferences),
# not ws-keyed project_memory defaults. Doubles below complete the actual chain: saved team connection
# → client → accessible set → ladder (sole accessible automatically selected) → member_uid re-resolved for selected workspace.
class _ResumeConnStore:
    async def resolve_active_binding(self, team_id: str):
        return SimpleNamespace(id="conn_resume", base_url="https://datarc.example"), "auth_resume", "revision_resume"

    async def list_connections(self, team_id: str) -> list:
        return [SimpleNamespace(id="conn_resume", base_url="https://datarc.example")]

    async def get_connection_auth(self, conn_id: str, team_id: str) -> str:
        return "auth_resume"


class _ResumeDatarcFactory:
    def get_client(self, base_url: str, auth_key: str) -> object:
        return _RESUME_DATARC


class _ResumeToolContextService:
    async def accessible_workspaces(self, datarc_client: object, user_id: str) -> tuple:
        from bowhead_gateway.connector_context_service import DatarcWorkspaceRef

        return (DatarcWorkspaceRef("dp_resume", "复用工作区"),)

    async def accessible_workspaces_ex(
        self,
        datarc_client: object,
        user_id: str,
    ) -> tuple[tuple, str]:
        return await self.accessible_workspaces(datarc_client, user_id), ""


class _ResumeMemberResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def resolve(self, identity: str, datarc_workspace_id: str, *, datarc: object = None) -> str:
        self.calls.append((identity, datarc_workspace_id, datarc))
        return "m_42"


_RESUME_DATARC = object()


def _seed_per_user_datarc(app, monkeypatch) -> _ResumeMemberResolver:
    """把 resume 的 per-user 北极九章链路播全，返回 member_resolver 假件供断言。"""
    from bowhead_gateway.routes import chat as chat_routes

    app.state.datarc_connection_store = _ResumeConnStore()
    app.state.datarc_client_factory = _ResumeDatarcFactory()
    app.state.tool_context_service = _ResumeToolContextService()
    resolver = _ResumeMemberResolver()
    app.state.member_resolver = resolver

    async def _email(_request) -> str:
        return "alice@example.com"

    # Identity must be authenticated user's verified email; dev fallback lacks verification, so replace that layer here.
    # This test targets resumed TurnRequest shape, not authentication.
    monkeypatch.setattr(chat_routes, "_caller_verified_email", _email)
    return resolver


@pytest.fixture
async def app_with_orch(app_ctx):
    """app_ctx + stub orchestrator wired so resume_turn can spawn its task."""
    app_ctx.state.orchestrator = _StubOrchestrator(app_ctx.state.clarification_store.db_path)
    return app_ctx


@pytest.mark.integration
async def test_resume_turn_404_missing(http) -> None:
    r = await http.post("/api/v1/clarifications/missing/resume_turn")
    assert r.status_code == 404


@pytest.mark.integration
async def test_resume_turn_409_pending(http, seeded_request) -> None:
    """Not yet resolved → 409 (cannot resume a pending clarification)."""
    r = await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resume_turn",
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "clarification_resume_not_ready"


@pytest.mark.integration
async def test_resume_turn_409_no_resume_context(http, seeded_request) -> None:
    """Resolved but resume_context never set (legacy/pre-sprint) → 409."""
    # Resolve first.
    await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resolve",
        json={"chosen_value": "晶科能源股份"},
    )
    # No set_resume_context call → resume_context is None.
    r = await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resume_turn",
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "clarification_resume_unavailable"


@pytest.mark.integration
@pytest.mark.parametrize("version", [3, "2", True, None, -1, "document-v3"])
@pytest.mark.parametrize("endpoint,answered", [("resolve", False), ("resolve", True), ("resume_turn", True)])
async def test_unsupported_resume_snapshot_preserves_card_without_dispatch(http, app_with_orch, monkeypatch, version, endpoint, answered) -> None:
    from unittest.mock import AsyncMock

    store = app_with_orch.state.clarification_store
    snapshot = {"user_text": "Analyze profit", "session_id": "s_unsupported_snapshot", "__schema_version": version}
    if version == "document-v3":
        from pathlib import Path

        snapshot = json.loads((Path(__file__).resolve().parents[3] / "contracts/chat-documents/planning-resume-v3.json").read_text())
        snapshot.update(user_id="u_dev_alice", workspace_id="42", turn_id="t_unsupported_snapshot")
    request = await store.put(
        turn_id="t_unsupported_snapshot",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="Choose a period",
        candidates=[ClarificationCandidate(value="a", label="A"), ClarificationCandidate(value="b", label="B")],
        kind="options",
        resume_context=snapshot,
    )
    if answered:
        await store.resolve(request.request_id, "a")
    before = await store.get(request.request_id)
    spawn = AsyncMock(return_value=SimpleNamespace(execution_generation=2))
    monkeypatch.setattr(turn_service, "spawn_resume_turn", spawn)
    response = await http.post(
        f"/api/v1/clarifications/{request.request_id}/{endpoint}", json={"chosen_value": "b"} if endpoint == "resolve" else None
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "clarification_resume_unavailable"
    assert "action" not in response.json()
    assert await store.get(request.request_id) == before
    assert await store.get_resolution_work_state(request.request_id) is None
    spawn.assert_not_awaited()
    assert app_with_orch.state.orchestrator.session_store._generations == {}


@pytest.mark.integration
@pytest.mark.parametrize("version", ["legacy", 0, 1, 2])
async def test_readable_snapshot_keeps_answer_after_preworker_failure(http, app_with_orch, monkeypatch, version) -> None:
    from bowhead_gateway.routes import chat as chat_routes

    store = app_with_orch.state.clarification_store
    request = await store.put(
        turn_id=f"t_snapshot_retry_{version}",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="Choose a period",
        candidates=[ClarificationCandidate(value="a", label="A"), ClarificationCandidate(value="b", label="B")],
        kind="options",
        resume_context={"user_text": "Analyze profit", "session_id": "s_snapshot_retry", "__schema_version": version},
    )
    if version == "legacy":
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute(
                "UPDATE clarification_requests SET resume_context_json=json_remove(resume_context_json,'$.__schema_version') WHERE request_id=?",
                (request.request_id,),
            )
            await db.commit()
    orchestrator = app_with_orch.state.orchestrator
    session_store = orchestrator.session_store

    real_spawn = turn_service.spawn_turn
    attempts = 0

    async def fail_before_first_worker(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Worker scheduling unavailable")
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(turn_service, "spawn_turn", fail_before_first_worker)
    with pytest.raises(RuntimeError, match="Worker scheduling unavailable"):
        await http.post(f"/api/v1/clarifications/{request.request_id}/resolve", json={"chosen_value": "a"})
    original = await store.get(request.request_id)
    assert original.status == "resolved" and original.chosen_value == "a"
    paused = await session_store.get_turn(request.turn_id)
    assert paused.status == "clarification_pending" and paused.execution_generation == 2
    retry = await http.post(f"/api/v1/clarifications/{request.request_id}/resolve", json={"chosen_value": "b"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["action"] == "turn_resume"
    assert retry.json()["chosen_value"] == "a"
    assert retry.json()["resume_turn_id"] == request.turn_id
    assert retry.json()["execution_generation"] == 3
    await asyncio.gather(*(task for task in tuple(chat_routes._inflight_tasks) if task.get_name() == f"turn_resume:{request.turn_id}"))
    assert len(app_with_orch.state.orchestrator.calls) == 1
    assert await store.get(request.request_id) == original


@pytest.mark.integration
@pytest.mark.parametrize("endpoint", ["resolve", "resume_turn"])
async def test_snapshot_version_rejection_keeps_owner_authorization_first(http, app_with_orch, endpoint) -> None:
    store = app_with_orch.state.clarification_store
    request = await store.put(
        turn_id="t_other_owner_snapshot",
        user_id="u_other",
        workspace_id="42",
        token="",
        rationale="Choose a period",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="options",
        resume_context={"user_text": "Analyze profit", "session_id": "s_other_owner_snapshot", "__schema_version": 3},
    )
    await store.resolve(request.request_id, "a")
    response = await http.post(
        f"/api/v1/clarifications/{request.request_id}/{endpoint}", json={"chosen_value": "a"} if endpoint == "resolve" else None
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "clarification_not_found"


@pytest.mark.integration
@pytest.mark.parametrize("member_failure", [False, True], ids=["datarc-available", "expired-datarc-key"])
async def test_resume_turn_happy_path(http, seeded_request, app_with_orch, monkeypatch, member_failure) -> None:
    """resolved + resume_context present → 200, spawns turn with same turn_id."""
    import asyncio

    store: ClarificationStore = app_with_orch.state.clarification_store
    resolver = _seed_per_user_datarc(app_with_orch, monkeypatch)
    if member_failure:
        from bowhead_bi_bridge import BIPermissionError

        async def rejected_member(identity, datarc_workspace_id, *, datarc=None):
            resolver.calls.append((identity, datarc_workspace_id, datarc))
            raise BIPermissionError("access rejected", source="http")

        monkeypatch.setattr(resolver, "resolve", rejected_member)
    from bowhead_gateway import turn_service

    team_router = object()

    async def _team_runtime(*_args, **_kwargs):
        return turn_service.TeamModelRuntimeResolution(
            router=team_router,
            result="team_safe_success",
            reason="resolved",
        )

    monkeypatch.setattr(
        turn_service,
        "resolve_team_model_runtime",
        _team_runtime,
    )
    # Snapshot resume_context as Lead would at sentinel emit.
    ok = await store.set_resume_context(
        seeded_request.request_id,
        {
            "user_text": "晶科 GMV 同比",
            "parent_turn_id": seeded_request.turn_id,
            "session_id": "s_test",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "member_uid": "m_42",
            # Datarc connections are team settings: no team_id means no connection/client;
            # snapshot must carry team, or resume only gets datarc_unavailable.
            "team_id": "team_resume",
            "kb_role": "user",
            "paradigm_v2": False,
            "preferred_provider": None,
            "preferred_model": None,
            "dispatch_window": [],
            "response_kind_pre_flight": None,
            "snapshot_at": "2026-05-18T00:00:00Z",
        },
    )
    assert ok

    # Resolve.
    rv = await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resolve",
        json={"chosen_value": "晶科能源股份"},
    )
    assert rv.status_code == 200
    assert rv.json()["action"] == "noop"  # default kind=token (not turn_resume)
    # NOTE: token kind currently maps to noop per Phase 4C step 3 logic
    # (ambiguous_turn / options is the gated set). For this UT we still
    # exercise resume_turn endpoint directly to verify its validation +
    # spawn path independent of resolve's action hint.

    # Resume.
    rr = await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resume_turn",
    )
    assert rr.status_code == 200
    body = rr.json()
    assert body["ok"] is True
    assert body["turn_id"] == seeded_request.turn_id  # same turn_id (Option A)
    assert body["stream_url"] == f"/api/v1/chat/stream/{seeded_request.turn_id}"

    # Verify orchestrator was invoked with correct TurnRequest shape.
    orch: _StubOrchestrator = app_with_orch.state.orchestrator
    # Let the asyncio.create_task schedule.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if orch.calls:
            break
    assert len(orch.calls) == 1
    req = orch.calls[0]
    assert req.turn_id == seeded_request.turn_id
    assert req.resume_turn_id == seeded_request.turn_id
    assert req.team_router is team_router
    assert req.text == "晶科 GMV 同比"
    assert req.workspace_id == "42"
    # Per-user ladder automatically selects sole accessible workspace, no ws-keyed project_memory default reads.
    if member_failure:
        assert req.active_datarc_project is None
        assert req.member_uid == ""
        assert req.datarc_client is None
        assert req.datarc_unavailable is True
        assert req.datarc_absence_reason == "connection_rejected"
    else:
        assert req.active_datarc_project == "dp_resume"
        assert req.member_uid == "m_42"
    assert resolver.calls == [("alice@example.com", "dp_resume", _RESUME_DATARC)]
    assert req.user_id == "u_dev_alice"


@pytest.mark.integration
async def test_resume_turn_team_router_failure_is_safe_and_spawns_nothing(
    http,
    seeded_request,
    app_with_orch,
    monkeypatch,
) -> None:
    from bowhead_gateway import turn_service

    store: ClarificationStore = app_with_orch.state.clarification_store
    _seed_per_user_datarc(app_with_orch, monkeypatch)
    assert await store.set_resume_context(
        seeded_request.request_id,
        {
            "user_text": "继续",
            "session_id": "s_test",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "team_id": "team_resume",
        },
    )
    await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resolve",
        json={"chosen_value": "晶科能源股份"},
    )

    async def _unavailable(*_args, **_kwargs):
        return turn_service.TeamModelRuntimeResolution(
            result="resolver_error",
            reason="router_build_failed",
        )

    monkeypatch.setattr(
        turn_service,
        "resolve_team_model_runtime",
        _unavailable,
    )

    response = await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resume_turn",
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "team_model_runtime_unavailable"
    assert "DEPLOYMENT" not in response.text
    assert app_with_orch.state.orchestrator.calls == []


@pytest.mark.integration
async def test_resume_turn_credit_exhausted_is_402_and_spawns_nothing(
    http,
    seeded_request,
    app_with_orch,
    monkeypatch,
) -> None:
    """续跑与 chat/send 同一 credit 准入：余额 ≤0 ⇒ 402，不起任何模型工作。"""
    store: ClarificationStore = app_with_orch.state.clarification_store
    _seed_per_user_datarc(app_with_orch, monkeypatch)
    assert await store.set_resume_context(
        seeded_request.request_id,
        {
            "user_text": "继续",
            "session_id": "s_test",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "team_id": "team_resume",
        },
    )
    await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resolve",
        json={"chosen_value": "晶科能源股份"},
    )
    users = app_with_orch.state.user_store
    users.credit_balance = -50

    response = await http.post(
        f"/api/v1/clarifications/{seeded_request.request_id}/resume_turn",
    )

    assert response.status_code == 402
    assert response.json()["detail"]["error"]["code"] == "user_credit_exhausted"
    assert app_with_orch.state.orchestrator.calls == []
    assert [event["code"] for event in users.credit_events] == ["user_credit_exhausted"]


@pytest.mark.integration
async def test_resolve_blocked_by_credit_keeps_card_pending_and_resumes_after_topup(
    app_with_orch,
    http,
) -> None:
    """额度拦在答案被消费之前：行保持 pending（卡片还在、刷新也还在），充值后再点同一张卡就继续。

    2026-09-20 116 dogfood：先前把闸放在 resolve 的 CAS 之后，行已 resolved 而卡片随即从
    对话里消失，这一轮永远停在 clarification_pending，用户没有任何可点的入口继续。
    """
    import asyncio

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_credit_resume",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="ambiguous query: 怎么样",
        candidates=[
            ClarificationCandidate(value="营收", label="营收", confidence="high"),
            ClarificationCandidate(value="销量", label="销量", confidence="high"),
        ],
        kind="ambiguous_turn",
        multi_select=False,
        session_id="s_credit_resume",
        resume_context={
            "user_text": "怎么样",
            "parent_turn_id": "t_credit_resume",
            "session_id": "s_credit_resume",
            "member_uid": "m_42",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "team_id": None,
            "kb_role": "user",
        },
    )
    users = app_with_orch.state.user_store
    users.credit_balance = 0

    blocked = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "营收"},
    )

    assert blocked.status_code == 402
    assert blocked.json()["detail"]["error"]["code"] == "user_credit_exhausted"
    kept = await store.get(req.request_id)
    assert kept is not None
    assert kept.status == "pending", "答案不该被消费——消费了卡片就没了，用户再也点不回来"
    assert kept.chosen_value is None
    orch = app_with_orch.state.orchestrator
    assert orch.calls == []

    users.credit_balance = 500
    retried = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "营收"},
    )

    assert retried.status_code == 200, retried.text
    assert retried.json()["action"] == "turn_resume"
    for _ in range(20):
        await asyncio.sleep(0.05)
        if orch.calls:
            break
    assert [call.turn_id for call in orch.calls] == ["t_credit_resume"]
    settled = await store.get(req.request_id)
    assert settled is not None and settled.status == "resolved"
    assert settled.chosen_value == "营收"


@pytest.mark.integration
@pytest.mark.parametrize(("credit_balance", "reaches_decision"), [(0, False), (500, True)])
async def test_terminal_resolve_credit_gate_runs_before_recording_the_decision(
    app_with_orch,
    http,
    monkeypatch,
    credit_balance,
    reaches_decision,
) -> None:
    """持久化聊天执行（V1）：续跑决策只有额度准入通过后才会被记录。

    恢复进程（TerminalPendingExecutionRecovery）会在重启后自动执行已记录的续跑决策、且不再查额度；
    这条用例钉住「记录」本身就在准入之后——恢复只会完成当时已准入的工作（与已开始的 turn
    跑完照实扣同一语义），不会替余额为 0 的用户起新的模型工作。
    """
    from bowhead_gateway.routes import clarifications as clarification_routes

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_terminal_credit",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="terminal credit gate",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="options",
        resume_context={"user_text": "continue", "session_id": "s_terminal_credit"},
    )
    decision_calls: list[str] = []

    class _ReachedDecision(Exception):
        pass

    class _Factory:
        async def read_existing_source_decision(self, **_kwargs):
            decision_calls.append("read_existing_source_decision")
            raise _ReachedDecision

        async def record_source_decision(self, **_kwargs):
            decision_calls.append("record_source_decision")
            raise _ReachedDecision

    async def _terminal_factory(_request, _clarification, *, user_id):
        return _Factory(), "team_legacy", "s_terminal_credit"

    monkeypatch.setattr(clarification_routes, "_terminal_clarification_factory", _terminal_factory)
    app_with_orch.state.user_store.credit_balance = credit_balance

    try:
        response = await http.post(
            f"/api/v1/clarifications/{req.request_id}/resolve",
            json={"chosen_value": "a"},
        )
    except _ReachedDecision:
        response = None

    if reaches_decision:
        assert decision_calls, "正对照：有额度时应当走到记录决策这一步"
    else:
        assert response is not None and response.status_code == 402
        assert response.json()["detail"]["error"]["code"] == "user_credit_exhausted"
        assert decision_calls == []
    row = await store.get(req.request_id)
    assert row is not None and row.status == "pending"


@pytest.mark.integration
async def test_fast_resolve_before_pause_settles_keeps_card_pending(
    http,
    app_with_orch,
    monkeypatch,
) -> None:
    from bowhead_gateway.routes import clarifications as clarification_routes

    monkeypatch.setattr(
        turn_service,
        "LEGACY_CLARIFICATION_SETTLEMENT_WAIT_SECONDS",
        0.0,
    )

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_fast_click",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="fast click",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="options",
        resume_context={"user_text": "continue", "session_id": "s_fast"},
    )
    session_store = app_with_orch.state.orchestrator.session_store
    session_store._statuses[req.turn_id] = "running"
    emitted: list[str] = []

    async def _unexpected_emit(*_args, **_kwargs) -> None:
        emitted.append("event")

    monkeypatch.setattr(
        clarification_routes,
        "_ensure_clarification_resolved_event",
        _unexpected_emit,
    )

    response = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "a"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "clarification_resume_not_ready"
    stored = await store.get(req.request_id)
    assert stored is not None
    assert stored.status == "pending"
    assert stored.chosen_value is None
    assert emitted == []
    assert app_with_orch.state.orchestrator.calls == []


@pytest.mark.integration
async def test_fast_resolve_waits_for_pause_settlement_then_resumes(
    http,
    app_with_orch,
    monkeypatch,
) -> None:

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_fast_click_settles",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="fast click settles",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="options",
        resume_context={
            "user_text": "continue",
            "session_id": "s_fast_settles",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "team_id": None,
        },
    )
    session_store = app_with_orch.state.orchestrator.session_store
    session_store._statuses[req.turn_id] = "running"
    dispatch_state = {"settled": False}
    probe_results: list[bool] = []

    async def _is_turn_resume_ready(turn_id: str) -> bool:
        ready = bool(session_store._statuses.get(turn_id) == "clarification_pending" and dispatch_state["settled"])
        probe_results.append(ready)
        return ready

    monkeypatch.setattr(
        session_store,
        "is_turn_resume_ready",
        _is_turn_resume_ready,
        raising=False,
    )
    monkeypatch.setattr(
        turn_service,
        "LEGACY_CLARIFICATION_SETTLEMENT_WAIT_SECONDS",
        0.2,
    )
    monkeypatch.setattr(
        turn_service,
        "LEGACY_CLARIFICATION_SETTLEMENT_POLL_SECONDS",
        0.005,
    )

    async def _settle_pause() -> None:
        await asyncio.sleep(0.02)
        session_store._statuses[req.turn_id] = "clarification_pending"
        await asyncio.sleep(0.02)
        dispatch_state["settled"] = True

    settlement = asyncio.create_task(_settle_pause())
    response = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "a"},
    )
    await settlement

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "turn_resume"
    assert body["stream_url"] == f"/api/v1/chat/stream/{req.turn_id}"
    stored = await store.get(req.request_id)
    assert stored is not None
    assert stored.status == "resolved"
    assert stored.chosen_value == "a"
    assert False in probe_results
    assert probe_results[-1] is True


@pytest.mark.integration
async def test_concurrent_resolve_replays_success_when_turn_advances_during_wait(
    http,
    app_with_orch,
    monkeypatch,
) -> None:
    """A losing waiter must replay the winning resolve, not return a late 409."""
    from bowhead_gateway.routes import clarifications as clarification_routes

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_concurrent_settlement",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="concurrent settlement",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="options",
        resume_context={
            "user_text": "continue",
            "session_id": "s_concurrent_settlement",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "team_id": None,
        },
    )
    session_store = app_with_orch.state.orchestrator.session_store
    session_store._statuses[req.turn_id] = "clarification_pending"
    entered = 0
    entered_lock = asyncio.Lock()
    both_waiting = asyncio.Event()

    async def _ordered_wait(*_args, **_kwargs) -> bool:
        nonlocal entered
        async with entered_lock:
            entered += 1
            position = entered
            if entered == 2:
                both_waiting.set()
        await both_waiting.wait()
        if position == 1:
            return True
        while True:
            current = await store.get(req.request_id)
            if current is not None and current.status == "resolved" and session_store._statuses.get(req.turn_id) == "running":
                return False
            await asyncio.sleep(0.001)

    monkeypatch.setattr(
        clarification_routes,
        "wait_for_legacy_turn_resume_ready",
        _ordered_wait,
    )

    first, second = await asyncio.wait_for(
        asyncio.gather(
            http.post(
                f"/api/v1/clarifications/{req.request_id}/resolve",
                json={"chosen_value": "a"},
            ),
            http.post(
                f"/api/v1/clarifications/{req.request_id}/resolve",
                json={"chosen_value": "a"},
            ),
        ),
        timeout=1.0,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["stream_url"] == second.json()["stream_url"]
    await asyncio.sleep(0.1)
    assert len(app_with_orch.state.orchestrator.calls) == 1


@pytest.mark.integration
async def test_resolved_retry_repairs_event_before_resuming(
    http,
    app_with_orch,
    monkeypatch,
) -> None:
    from bowhead_gateway.routes import clarifications as clarification_routes

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_event_repair",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="repair",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="options",
        resume_context={"user_text": "continue", "session_id": "s_repair"},
    )
    app_with_orch.state.orchestrator.session_store._statuses[req.turn_id] = "clarification_pending"
    order: list[str] = []
    emit_attempt = 0

    async def _flaky_event(*_args, **_kwargs) -> None:
        nonlocal emit_attempt
        emit_attempt += 1
        if emit_attempt == 1:
            order.append("event_failed")
            raise RuntimeError("durable sink unavailable")
        order.append("event_repaired")

    async def _resume(*_args, **_kwargs):
        order.append("resume")
        return SimpleNamespace(
            stream_url=f"/api/v1/chat/stream/{req.turn_id}",
            execution_generation=2,
        )

    monkeypatch.setattr(
        clarification_routes,
        "_ensure_clarification_resolved_event",
        _flaky_event,
    )
    monkeypatch.setattr(clarification_routes, "resume_turn", _resume)

    first = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "a"},
    )
    assert first.status_code == 503
    stored = await store.get(req.request_id)
    assert stored is not None and stored.status == "resolved"
    assert order == ["event_failed"]

    retry = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "different ignored value"},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["chosen_value"] == "a"
    assert retry.json()["execution_generation"] == 2
    assert order == ["event_failed", "event_repaired", "resume"]


@pytest.mark.integration
async def test_terminal_resume_retry_reattaches_without_new_generation(
    http,
    app_with_orch,
    monkeypatch,
) -> None:
    from bowhead_gateway import turn_service

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_terminal_retry",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="lost response",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="options",
        resume_context={"user_text": "continue", "session_id": "s_terminal"},
    )
    assert await store.resolve(req.request_id, "a", chosen_values=["a"])
    session_store = app_with_orch.state.orchestrator.session_store
    session_store._statuses[req.turn_id] = "completed"
    session_store._generations[req.turn_id] = 4

    async def _must_not_spawn(*_args, **_kwargs):
        raise AssertionError("terminal retry must not spawn")

    monkeypatch.setattr(turn_service, "spawn_resume_turn", _must_not_spawn)

    response = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resume_turn",
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "request_id": req.request_id,
        "turn_id": req.turn_id,
        "stream_url": f"/api/v1/chat/stream/{req.turn_id}",
        "execution_generation": 4,
    }


def test_merge_clarified_user_text_keeps_all_answers() -> None:
    """B1 unit: _merge_clarified_user_text must keep EVERY answer, not just #1."""
    from bowhead_gateway.routes.clarifications import _merge_clarified_user_text

    out = _merge_clarified_user_text(
        "帮我做个全面分析",
        ["分析方向: 综合经营报告", "时间范围: 今年至今"],
    )
    assert "帮我做个全面分析" in out  # original ask preserved
    assert "综合经营报告" in out  # answer 1
    assert "今年至今" in out  # answer 2 (old code dropped this)

    # single answer — same inline shape as before
    assert _merge_clarified_user_text("做个ppt", ["销售业绩"]) == "做个ppt (已明确: 销售业绩)"
    # original empty → picks alone
    assert _merge_clarified_user_text("", ["A", "B"]) == "A; B"
    # no usable answers → original unchanged
    assert _merge_clarified_user_text("orig", []) == "orig"
    assert _merge_clarified_user_text("orig", ["", "  "]) == "orig"


def test_render_legacy_values_for_resume_keeps_question_colons() -> None:
    from bowhead_gateway.routes.clarifications import _render_legacy_values_for_resume

    rendered = _render_legacy_values_for_resume(
        [
            f"Ratio (a:b): {QUESTION_SKIP_VALUE}",
            "URL question: https://example.com/a:b",
        ]
    )

    assert rendered[0] == f"Ratio (a:b): {QUESTION_SKIP_TEXT}"
    assert rendered[1] == "URL question: https://example.com/a:b"


@pytest.mark.integration
async def test_resume_turn_multi_answer_merges_all_picks(http, app_with_orch) -> None:
    """B1 (clarification-true-continuation): a multi-question clarification must
    carry ALL the user's answers into the resumed turn, not just the first.

    Regression for prod t_f2f6a88ad3cc — a 2-question clarification (分析方向 +
    时间范围) resumed with only the first answer, so the rerun dropped the time
    filter. The empty-token (ambiguous_turn) path merges chosen_values into
    user_text; the old code passed only chosen_value.
    """
    import asyncio

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_multi",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="多问 clarification",
        candidates=[],
        kind="ambiguous_turn",
        resume_context={
            "user_text": "帮我做个全面分析",
            "parent_turn_id": "t_multi",
            "session_id": "s_test",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "member_uid": "m_42",
            "team_id": None,
            "kb_role": "user",
            "paradigm_v2": False,
            "preferred_provider": None,
            "preferred_model": None,
            "dispatch_window": [],
            "response_kind_pre_flight": None,
            "snapshot_at": "2026-06-20T00:00:00Z",
        },
    )

    rv = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_values": ["分析方向: 综合经营报告", "时间范围: 今年至今"]},
    )
    assert rv.status_code == 200, rv.text
    assert rv.json()["stream_url"] == f"/api/v1/chat/stream/{req.turn_id}"
    assert rv.json()["execution_generation"] == 2

    orch: _StubOrchestrator = app_with_orch.state.orchestrator
    for _ in range(20):
        await asyncio.sleep(0.05)
        if orch.calls:
            break
    assert len(orch.calls) == 1
    text = orch.calls[0].text
    assert "综合经营报告" in text  # answer 1 survives
    assert "今年至今" in text  # answer 2 survives (the B1 bug)
    assert "帮我做个全面分析" in text  # original ask preserved


@pytest.mark.integration
async def test_resume_turn_structured_question_skip_renders_natural_text(
    http,
    app_with_orch,
) -> None:
    import asyncio

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_question_skip",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="多问 clarification",
        candidates=[],
        kind="ambiguous_turn",
        resume_context={
            "user_text": "帮我做个全面分析",
            "parent_turn_id": "t_question_skip",
            "session_id": "s_question_skip",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "member_uid": "m_42",
            "team_id": None,
            "kb_role": "user",
            "paradigm_v2": False,
            "preferred_provider": None,
            "preferred_model": None,
            "dispatch_window": [],
            "response_kind_pre_flight": None,
            "snapshot_at": "2026-06-20T00:00:00Z",
        },
    )

    rv = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={
            "answers": [
                {"question": "主要看哪个区域?", "source": "custom", "answer": "华中"},
                {"question": "重点看哪些指标?", "source": "skip", "skipped": True},
            ],
        },
    )
    assert rv.status_code == 200, rv.text
    assert rv.json()["stream_url"] == f"/api/v1/chat/stream/{req.turn_id}"
    retained = await store.get(req.request_id)
    assert retained.resume_context == req.resume_context
    assert retained.answers == rv.json()["answers"]
    assert [answer["source"] for answer in retained.answers] == ["custom", "skip"]

    orch: _StubOrchestrator = app_with_orch.state.orchestrator
    for _ in range(20):
        await asyncio.sleep(0.05)
        if orch.calls:
            break
    assert len(orch.calls) == 1
    text = orch.calls[0].text
    assert "主要看哪个区域?: 华中" in text
    assert f"重点看哪些指标?: {QUESTION_SKIP_TEXT}" in text
    assert QUESTION_SKIP_VALUE not in text
    assert "跳过（由模型根据上下文判断）" not in text


@pytest.mark.integration
async def test_resume_turn_revalidates_current_workspace_membership(
    http,
    app_with_orch,
    tmp_path: Path,
    completed_community_user_store,
) -> None:
    users = completed_community_user_store
    await users.initialize()
    access = AccessControlStore(users.db_path)
    await access.initialize()
    team = await users.create_team(name="Acme", max_seats=10)
    user = await users.create_user(
        team_id=team.id,
        email="resume-member@example.com",
        password="Passw0rd!",
    )
    ws_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=user.id,
    )
    app_with_orch.state.user_store = users
    if app_with_orch.state.edition == "community":
        from bowhead_core.entitlements import CommunityResolver

        app_with_orch.state.entitlements = CommunityResolver(mode="saas", user_store=users)
    app_with_orch.state.access_control_store = access

    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_resume_authz",
        user_id=user.id,
        workspace_id=ws_id,
        token="region",
        rationale="pick one",
        candidates=[
            ClarificationCandidate(value="north", label="North", confidence="high"),
        ],
    )
    assert await store.set_resume_context(
        req.request_id,
        {
            "user_text": "Sales north trend",
            "parent_turn_id": req.turn_id,
            "session_id": "s_resume_authz",
            "workspace_id": ws_id,
            "user_id": user.id,
            "member_uid": "m_resume",
            "team_id": team.id,
            "access_context": {
                "user_id": user.id,
                "team_id": team.id,
                "workspace_id": ws_id,
                "workspace_role": "member",
                "group_ids": ["grp_stale"],
            },
            "kb_role": "user",
            "paradigm_v2": False,
        },
    )
    await store.resolve(req.request_id, "north")
    assert await users.remove_member(ws_id, user.id) is True

    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resume_turn",
        headers={"X-Bowhead-User-Id": user.id},
    )

    assert r.status_code == 403
    assert r.json()["detail"]["error"]["code"] == "ws_access_denied"
    assert r.json()["detail"]["error"]["diagnostics"]["diagnostic_code"] == ("rule_clarification_failure")
    assert "workspace_id" not in r.json()["detail"]["error"]
    assert app_with_orch.state.orchestrator.calls == []


@pytest.mark.integration
async def test_resolve_returns_turn_resume_for_atomic_put(
    app_with_orch,
    http,
) -> None:
    """0.26.2 fix — when caller uses `put(..., resume_context=...)` atomic
    write AND kind is in the turn-resume-eligible set, /resolve must
    return action="turn_resume" (not silent fall-back to noop).

    Prod evidence: all 7 ambiguous_turn rows in s_e5f180688e54 had
    resume_context_json=NULL → /resolve fell to noop → FE chat-send →
    visual duplicate turn. The atomic put path closes that gap.

    This is the LOCAL dogfood equivalent for the 0.26.2 ship that's
    failing on prod ship (aliyun mirror timeout). It exercises the
    full backend chain: store.put with resume_context= → /resolve
    HTTP → action=turn_resume returned.
    """
    store: ClarificationStore = app_with_orch.state.clarification_store
    # Use the new atomic path — resume_context passed at put-time.
    req = await store.put(
        turn_id="t_atomic_dogfood",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="ambiguous query: 怎么样",
        candidates=[
            ClarificationCandidate(value="营收", label="营收", confidence="high"),
            ClarificationCandidate(value="销量", label="销量", confidence="high"),
            ClarificationCandidate(value="利润", label="利润", confidence="high"),
        ],
        kind="ambiguous_turn",
        multi_select=False,
        session_id="s_atomic_dogfood",
        resume_context={
            "user_text": "怎么样",
            "parent_turn_id": "t_atomic_dogfood",
            "session_id": "s_atomic_dogfood",
            "member_uid": "m_42",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "team_id": None,
            "kb_role": "user",
            "paradigm_v2": False,
            "preferred_provider": None,
            "preferred_model": None,
            "dispatch_window": [],
            "response_kind_pre_flight": None,
        },
    )

    # Verify DB: resume_context_json persisted (key 0.26.2 fix; previously
    # silent failure left NULL).
    fetched = await store.get(req.request_id)
    assert fetched is not None
    assert fetched.resume_context is not None, "atomic put failed to persist resume_context_json — 0.26.2 fix broken"
    assert fetched.resume_context["user_text"] == "怎么样"

    # Verify /resolve: action == turn_resume, triggering FE same-turn-resume
    # instead of chat-send creating a new turn.
    rv = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "营收"},
    )
    assert rv.status_code == 200
    body = rv.json()
    assert body["action"] == "turn_resume", (
        f"expected turn_resume (atomic resume_context written + kind=ambiguous_turn), "
        f"got action={body['action']!r}; FE will fall back to chat-send → "
        f"prod visual duplicate bug reproduces"
    )
    assert body["resume_turn_id"] == req.turn_id


@pytest.mark.integration
async def test_resolve_returns_noop_when_resume_context_omitted(
    app_with_orch,
    http,
) -> None:
    """Back-compat: callers that omit resume_context still get noop.

    Verifies the new optional `resume_context=` param doesn't break legacy
    callers (token kind etc.) that don't pass it.
    """
    store: ClarificationStore = app_with_orch.state.clarification_store
    req = await store.put(
        turn_id="t_legacy_noop",
        user_id="u_dev_alice",
        workspace_id="42",
        token="aoeu",
        rationale="legacy token kind",
        candidates=[
            ClarificationCandidate(value="opt1", label="opt1", confidence="high"),
        ],
        kind="token",  # not in turn-resume-eligible set
        session_id="s_legacy",
        # resume_context= omitted on purpose
    )
    rv = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "opt1"},
    )
    assert rv.status_code == 200
    assert rv.json()["action"] == "noop"


@pytest.mark.integration
async def test_resume_turn_cross_user_denied(http, app_with_orch) -> None:
    """resume_turn checks user ownership same as resolve (P0 #2 parity)."""
    store: ClarificationStore = app_with_orch.state.clarification_store
    other_req = await store.put(
        turn_id="t_other_resume",
        user_id="bob",
        workspace_id="42",
        token="X",
        rationale="",
        candidates=[],
    )
    await store.set_resume_context(
        other_req.request_id,
        {
            "user_text": "x",
            "parent_turn_id": "t_other_resume",
            "workspace_id": "42",
            "user_id": "bob",
        },
    )
    await store.resolve(other_req.request_id, "X")

    # Explicit dev fallback default user is u_dev_alice != bob.
    r = await http.post(
        f"/api/v1/clarifications/{other_req.request_id}/resume_turn",
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "clarification_not_found"


# ─────────────────────────────────────────────────────────────────────────────
# dingsiyan #11 (2026-07-07): resolve→resume→stream delivery contract.
#
# Encode two actual-device incidents:
# - t_1ccf9e0aec11: clarification.resolved emitted through unseeded new bus → payload seq=1,
#   colliding with turn_started; FE replay dedup drops card-close reversal ('面板不收起/点 N 遍');
# - t_f2f6a88ad3cc residual window: closed gen1 bus swallows resolved without persistence;
#   done(clarification_pending) remains last → replay retains stale done →
#   FE markTerminal removes watchdog. Placeholder turn.resumed closes both gaps.
# ─────────────────────────────────────────────────────────────────────────────

_D11_RESUME_CTX = {
    "user_text": "重试",
    "session_id": "s_d11",
    "workspace_id": "42",
    "user_id": "u_dev_alice",
    "member_uid": "m_42",
    "team_id": None,
    "kb_role": "user",
    "paradigm_v2": False,
    "preferred_provider": None,
    "preferred_model": None,
    "dispatch_window": [],
    "response_kind_pre_flight": None,
    "snapshot_at": "2026-07-07T00:00:00Z",
}


@pytest.fixture
async def delivery_ctx(app_with_orch, tmp_path: Path):
    """app_with_orch + 真 TurnLogStore + registry sink 接到该 store（用完还原）."""
    from bowhead_core.events import get_event_registry
    from bowhead_core.turn_log_store import TurnLogStore

    tls = TurnLogStore(str(tmp_path / "d11_turnlog.db"))
    await tls.initialize()
    app_with_orch.state.turn_log_store = tls
    registry = get_event_registry()
    old_sink = registry._default_log_sink

    async def _sink(event):  # Await synchronously (main.py fire-and-forget; tests need determinism).
        await tls.record(
            turn_id=event.turn_id,
            event_type=event.event_type,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )

    registry.set_log_sink(_sink)
    yield app_with_orch, tls
    registry.set_log_sink(old_sink)


async def _seed_d11_clarification(app, turn_id: str):
    store: ClarificationStore = app.state.clarification_store
    return await store.put(
        turn_id=turn_id,
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="Lead clarification_ask",
        candidates=[
            ClarificationCandidate(value="报告A", label="报告A"),
            ClarificationCandidate(value="报告B", label="报告B"),
        ],
        kind="ambiguous_turn",
        resume_context=dict(_D11_RESUME_CTX),
    )


async def _seed_paused_rows(tls, turn_id: str, n_pre: int = 15) -> None:
    """模拟 pause 前已落库的行：1..n_pre-1 普通事件 + 最后一行 paused done."""
    for i in range(1, n_pre):
        await tls.record(
            turn_id=turn_id,
            event_type="turn_started" if i == 1 else "narrative.delta",
            payload={"seq": i, "evt_seq": i},
        )
    await tls.record(
        turn_id=turn_id,
        event_type="done",
        payload={"status": "clarification_pending", "seq": n_pre, "evt_seq": n_pre},
    )


@pytest.mark.integration
async def test_d11_resolve_persists_resolved_with_seeded_seq(http, delivery_ctx) -> None:
    """>5s 常态（bus 已 pop）：resolved 必须以 max_seq+1 落库，不许 seq=1 撞号."""
    app, tls = delivery_ctx
    req = await _seed_d11_clarification(app, "t_d11_seed")
    await _seed_paused_rows(tls, "t_d11_seed", n_pre=15)

    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "报告B"},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "turn_resume"

    rows = await tls.list_events("t_d11_seed")
    resolved = [x for x in rows if x.event_type == "clarification.resolved"]
    assert len(resolved) == 1
    # Old bug: payload seq1 (new bus starts at 0) collides with turn_started, dropped by FE dedup.
    assert resolved[0].payload["seq"] == 16


@pytest.mark.integration
async def test_d11_resolve_survives_closed_registered_bus(http, delivery_ctx) -> None:
    """<5s 窗口：closed gen1 bus 还挂在 registry。老实现 emit 早退连库都不落."""
    from bowhead_core.events import OrchestratorEvent, get_event_registry

    app, tls = delivery_ctx
    req = await _seed_d11_clarification(app, "t_d11_closed")
    registry = get_event_registry()
    bus = await registry.get_or_create("t_d11_closed")
    await bus.emit(OrchestratorEvent(event_type="turn_started", turn_id="t_d11_closed", payload={}))
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_d11_closed", payload={"status": "clarification_pending"}))
    assert bus._closed  # done → terminal → closed, still registered (<5s window).

    try:
        r = await http.post(
            f"/api/v1/clarifications/{req.request_id}/resolve",
            json={"chosen_value": "报告A"},
        )
        assert r.status_code == 200
        rows = await tls.list_events("t_d11_closed")
        resolved = [x for x in rows if x.event_type == "clarification.resolved"]
        assert len(resolved) == 1  # Old implementation: 0 rows, swallowed by dead bus.
        assert resolved[0].payload["seq"] == 3  # turn_started=1, done=2 → 3
    finally:
        await registry.close("t_d11_closed")


@pytest.mark.integration
async def test_d11_resume_emits_turn_resumed_and_unstales_paused_done(
    http,
    delivery_ctx,
) -> None:
    """resume 起跑必须先落 turn.resumed → stale paused-done 不再是最后一行，
    _replay_rows_for_stream 才能把它滤掉（t_f2f6a88ad3cc 残窗关死）。"""
    import asyncio

    from bowhead_core.events import get_event_registry
    from bowhead_gateway.routes.chat import _replay_rows_for_stream

    app, tls = delivery_ctx
    req = await _seed_d11_clarification(app, "t_d11_resume")
    await _seed_paused_rows(tls, "t_d11_resume", n_pre=15)

    rv = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "报告B"},
    )
    assert rv.status_code == 200
    assert rv.json()["stream_url"] == f"/api/v1/chat/stream/{req.turn_id}"
    assert rv.json()["execution_generation"] == 2

    orch = app.state.orchestrator
    for _ in range(40):
        await asyncio.sleep(0.05)
        if orch.calls:
            break
    assert orch.calls, "resume worker 未起跑"

    try:
        rows = await tls.list_events("t_d11_resume")
        resolved = next(x for x in rows if x.event_type == "clarification.resolved")
        resumed = [x for x in rows if x.event_type == "turn.resumed"]
        assert len(resumed) == 1
        assert resumed[0].payload["request_id"] == req.request_id
        assert resumed[0].payload["chosen_values"] == ["报告B"]
        assert resumed[0].payload["seq"] > resolved.payload["seq"]

        # Replay filter: later events after paused done require filtering stale done.
        replayed = _replay_rows_for_stream(rows)
        stale = [x for x in replayed if x.event_type == "done" and (x.payload or {}).get("status") == "clarification_pending"]
        assert not stale
    finally:
        await get_event_registry().close("t_d11_resume")


class _BlockingOrchestrator:
    """卡在 run_turn 的 stub —— 并发双 resume 测试用（让第一个 worker 保持活着）."""

    def __init__(self, db_path: str) -> None:
        self.calls: list = []
        self.release = __import__("asyncio").Event()
        self.session_store = _StubResumeSessionStore(db_path)
        self.event_registry = get_event_registry()

    async def run_turn(self, req):
        self.calls.append(req)
        await self.release.wait()
        self.session_store._statuses[req.turn_id] = "completed"

        class _S:
            tokens_input = 0
            tokens_output = 0

        return _S()


@pytest.mark.integration
async def test_d11_concurrent_resume_spawns_single_worker(
    http,
    delivery_ctx,
    monkeypatch,
) -> None:
    """并发双击/重连双发 resume：spawn 锁内 re-check → 只起一个 worker.

    review 测试 #1: 端点 early-check `resume_already_in_flight` 与 spawn 之间隔着
    `turn_seq_seed`（一个 await），某些交错下请求 A 先完整 spawn、请求 B 才到
    early-check → B 走**旧幂等早退**（不进新锁）→ 即便删掉 `_resume_spawn_lock`
    测试也绿（=假绿）。这里 monkeypatch `resume_already_in_flight` 恒 False，把两个
    请求都逼过 early-check 进入 `spawn_resume_turn` 锁内 re-check —— 只有真正的锁 +
    `_inflight_tasks` re-check 才能拦住双 spawn。删锁则本测试红。
    """
    import asyncio

    import bowhead_gateway.turn_service as ts_mod
    from bowhead_core.events import get_event_registry

    app, tls = delivery_ctx
    req = await _seed_d11_clarification(app, "t_d11_race")
    await _seed_paused_rows(tls, "t_d11_race", n_pre=3)

    # Force both atomic resolve+resume requests past early checks (endpoint locally imports this name,
    # so patch turn_service), leaving only the locked spawn
    # recheck to prevent double spawning.
    monkeypatch.setattr(ts_mod, "resume_already_in_flight", lambda _tid: False)

    orch = _BlockingOrchestrator(app.state.clarification_store.db_path)
    app.state.orchestrator = orch
    try:
        r1, r2 = await asyncio.gather(
            http.post(
                f"/api/v1/clarifications/{req.request_id}/resolve",
                json={"chosen_value": "报告A"},
            ),
            http.post(
                f"/api/v1/clarifications/{req.request_id}/resolve",
                json={"chosen_value": "报告A"},
            ),
        )
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["stream_url"] == r2.json()["stream_url"]
        await asyncio.sleep(0.2)
        assert len(orch.calls) == 1, "并发 resume 双 spawn（spawn 锁 re-check 失效）"
    finally:
        orch.release.set()
        await asyncio.sleep(0.05)
        await get_event_registry().close("t_d11_race")


# ─────────────────────────────────────────────────────────────
# scheduled-recipe-runs S3 · 116 dogfood P1-A: scheduled-origin readiness gate.
# ─────────────────────────────────────────────────────────────


class _DispatchGatedSessionStore(_StubResumeSessionStore):
    """Model the shared Turn-state + dispatch-settlement resume fence."""

    def __init__(self, db_path: str, *, ready: bool) -> None:
        super().__init__(db_path)
        self._ready = ready

    async def is_turn_resume_ready(self, _turn_id: str) -> bool:
        return self._ready


class _SchedRunRow:
    def __init__(self, job_id: str, turn_id: str) -> None:
        self.id = "sr_1"
        self.job_id = job_id
        self.turn_id = turn_id
        self.status = "awaiting_input"


class _SchedStoreStub:
    """只实现出身反查 + job 读取（`_scheduled_run_for_turn` / resume 水合用到的两个）。"""

    def __init__(self, *, turn_id: str | None) -> None:
        self._turn_id = turn_id
        self.lookups: list[str] = []

    async def get_run_by_turn(self, turn_id: str):
        self.lookups.append(turn_id)
        if self._turn_id is not None and turn_id == self._turn_id:
            return _SchedRunRow("sj_1", turn_id)
        return None

    async def get(self, _job_id: str):
        return SimpleNamespace(
            id="sj_1",
            name="周报",
            datarc_workspace_id=None,
            recipe_ref=None,
            user_id="u_dev_alice",
            workspace_id="42",
        )


async def _seed_scheduled_clarification(app, turn_id: str = "t_sched_dogfood"):
    store: ClarificationStore = app.state.clarification_store
    return await store.put(
        turn_id=turn_id,
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="Lead clarification_ask: 复购率口径不明",
        candidates=[
            ClarificationCandidate(value="多次购买会员数 / 总会员数", label="多次购买会员数 / 总会员数"),
            ClarificationCandidate(value="回头客人数 / 总消费人数", label="回头客人数 / 总消费人数"),
        ],
        kind="ambiguous_turn",
        multi_select=False,
        session_id="s_sched_dogfood",
        resume_context={
            "user_text": "算一下本月的复购率，并按门店排名",
            "parent_turn_id": turn_id,
            "session_id": "s_sched_dogfood",
            "member_uid": "m_42",
            "workspace_id": "42",
            "user_id": "u_dev_alice",
            "team_id": None,
            "kb_role": "user",
            "paradigm_v2": False,
            "preferred_provider": None,
            "preferred_model": None,
            "dispatch_window": [],
            "response_kind_pre_flight": None,
        },
    )


@pytest.mark.integration
async def test_scheduled_origin_resolve_waits_for_dispatch_settlement(
    app_with_orch,
    http,
    monkeypatch,
) -> None:

    monkeypatch.setattr(
        turn_service,
        "LEGACY_CLARIFICATION_SETTLEMENT_WAIT_SECONDS",
        0.0,
    )
    session_store = _DispatchGatedSessionStore(app_with_orch.state.clarification_store.db_path, ready=False)
    app_with_orch.state.orchestrator.session_store = session_store
    sched = _SchedStoreStub(turn_id="t_sched_dogfood")
    app_with_orch.state.scheduled_store = sched
    req = await _seed_scheduled_clarification(app_with_orch)

    blocked = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "多次购买会员数 / 总会员数"},
    )

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "clarification_resume_not_ready"
    pending = await app_with_orch.state.clarification_store.get(req.request_id)
    assert pending is not None and pending.status == "pending"

    session_store._ready = True
    settled = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "多次购买会员数 / 总会员数"},
    )

    assert settled.status_code == 200, settled.text
    assert settled.json()["action"] == "turn_resume"
    assert sched.lookups
    stored = await app_with_orch.state.clarification_store.get(req.request_id)
    assert stored is not None
    assert stored.status == "resolved"


@pytest.mark.integration
async def test_scheduled_origin_skip_waits_for_dispatch_settlement(
    app_with_orch,
    http,
    monkeypatch,
) -> None:

    monkeypatch.setattr(
        turn_service,
        "LEGACY_CLARIFICATION_SETTLEMENT_WAIT_SECONDS",
        0.0,
    )
    session_store = _DispatchGatedSessionStore(app_with_orch.state.clarification_store.db_path, ready=False)
    app_with_orch.state.orchestrator.session_store = session_store
    app_with_orch.state.scheduled_store = _SchedStoreStub(turn_id="t_sched_skip")
    req = await _seed_scheduled_clarification(app_with_orch, turn_id="t_sched_skip")

    blocked = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"skip": True},
    )

    assert blocked.status_code == 409
    pending = await app_with_orch.state.clarification_store.get(req.request_id)
    assert pending is not None and pending.status == "pending"

    session_store._ready = True
    settled = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"skip": True},
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["skipped"] is True


@pytest.mark.integration
async def test_scheduled_origin_dismiss_waits_for_dispatch_settlement(
    app_with_orch,
    http,
    monkeypatch,
) -> None:

    monkeypatch.setattr(
        turn_service,
        "LEGACY_CLARIFICATION_SETTLEMENT_WAIT_SECONDS",
        0.0,
    )
    session_store = _DispatchGatedSessionStore(app_with_orch.state.clarification_store.db_path, ready=False)
    app_with_orch.state.orchestrator.session_store = session_store
    app_with_orch.state.scheduled_store = _SchedStoreStub(turn_id="t_sched_dismiss")
    req = await _seed_scheduled_clarification(
        app_with_orch,
        turn_id="t_sched_dismiss",
    )

    blocked = await http.post(
        f"/api/v1/clarifications/{req.request_id}/dismiss",
    )
    assert blocked.status_code == 409
    pending = await app_with_orch.state.clarification_store.get(req.request_id)
    assert pending is not None and pending.status == "pending"

    session_store._ready = True
    settled = await http.post(
        f"/api/v1/clarifications/{req.request_id}/dismiss",
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["dismissed"] is True


@pytest.mark.integration
async def test_interactive_chat_still_waits_for_dispatch_settlement(
    app_with_orch,
    http,
) -> None:
    """普通 chat 与 scheduled ingress 都必须等同一 dispatch settlement fence。"""
    app_with_orch.state.orchestrator.session_store = _DispatchGatedSessionStore(app_with_orch.state.clarification_store.db_path, ready=False)
    app_with_orch.state.scheduled_store = _SchedStoreStub(turn_id=None)  # Reverse lookup miss = ordinary chat.
    req = await _seed_scheduled_clarification(app_with_orch, turn_id="t_interactive")

    rv = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "多次购买会员数 / 总会员数"},
    )

    assert rv.status_code == 409
    assert rv.json()["detail"]["code"] == "clarification_resume_not_ready"
    stored = await app_with_orch.state.clarification_store.get(req.request_id)
    assert stored is not None
    assert stored.status == "pending"  # Card remains answerable, never consumed.


@pytest.mark.integration
async def test_scheduled_lookup_failure_fails_closed(
    app_with_orch,
    http,
) -> None:
    """反查抛错 → 503；ownership 未知时不能绕过 scheduled awaiting 状态机。"""

    class _BoomStore:
        async def get_run_by_turn(self, _turn_id: str):
            raise RuntimeError("scheduled store down")

    app_with_orch.state.orchestrator.session_store = _DispatchGatedSessionStore(app_with_orch.state.clarification_store.db_path, ready=False)
    app_with_orch.state.scheduled_store = _BoomStore()
    req = await _seed_scheduled_clarification(app_with_orch, turn_id="t_boom")

    rv = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "多次购买会员数 / 总会员数"},
    )

    assert rv.status_code == 503
    assert rv.json()["detail"]["error"]["code"] == "clarification_store_unavailable"
