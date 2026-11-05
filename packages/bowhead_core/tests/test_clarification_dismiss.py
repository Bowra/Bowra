"""Native and Web cancellation share real migrated card and Turn state."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from bowhead_core.agents.clarification_dismiss_tool import register_clarification_dismiss_tool
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.clarification_actions import GatewayClarificationActions
from bowhead_gateway.routes import clarifications
from bowhead_gateway.settings import Settings
from fastapi import FastAPI
from test_document_store import _sql
from test_ordinary_lead_continuation import ANSWER, prepare
from test_ordinary_lead_continuation import ctx as ctx
from test_ordinary_lead_continuation import migrated_db as migrated_db

pytestmark = pytest.mark.integration


async def action_case(ctx, monkeypatch, *, legacy_version=None):
    if legacy_version is None:
        store, card, worker, _ = await prepare(ctx, monkeypatch)
    else:
        worker = SimpleNamespace(user_id="u_doc", team_id="team_doc", workspace_id="ws_doc", session_id="s_doc")
        store = ClarificationStore(ctx.path)
        assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id="legacy-producer", lease_seconds=600) == "claimed"
        card = await store.put(
            turn_id="t_doc",
            user_id=worker.user_id,
            workspace_id=worker.workspace_id,
            session_id=worker.session_id,
            token="",
            rationale="Choose a training audience",
            candidates=[],
            kind="ambiguous_turn",
            resume_context={"__schema_version": legacy_version, "user_text": "Choose a training audience", "session_id": worker.session_id},
        )
        await _sql(ctx, "UPDATE turns SET status='clarification_pending' WHERE id='t_doc'")
        await TurnLogStore(ctx.path).record(
            turn_id="t_doc", event_type="done", payload={"status": "clarification_pending", "execution_generation": 1}
        )
        assert await ctx.sessions.mark_chat_spawn_dispatch_terminal(turn_id="t_doc", owner_id="legacy-producer")
    app = FastAPI()
    app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    app.state.clarification_store = store
    app.state.settings = Settings()
    app.state.session_store = ctx.sessions
    app.state.user_store = UserStore(ctx.path)
    app.state.entitlements = SimpleNamespace(assert_can_run_turn=AsyncMock())
    app.state.orchestrator = SimpleNamespace(session_store=ctx.sessions)
    tool_ctx = ToolExecutionContext(
        workspace_id=worker.workspace_id,
        member_uid="member",
        user_id=worker.user_id,
        turn_id="t_new",
        datarc=None,
        team_id=worker.team_id,
        session_id=worker.session_id,
        role="lead",
    )
    monkeypatch.setattr(clarifications, "_current_user_id", lambda _: worker.user_id)
    # Native and Web both still execute the real shared access-context builder.
    monkeypatch.setattr("bowhead_gateway.routes.auth._dev_user_fallback_enabled", lambda _: False)
    return app, store, card, tool_ctx


@pytest.mark.parametrize("surface", ["native", "web"])
@pytest.mark.parametrize("legacy_version", [None, 0, 1, 2])
async def test_cancellation_closes_original_pause_and_commits_one_invalidation(ctx, monkeypatch, surface, legacy_version):
    app, store, card, tool_ctx = await action_case(ctx, monkeypatch, legacy_version=legacy_version)
    # Delivery is tested by the existing invalidation suite; a durable intent must
    # survive even when immediate delivery fails after the state transaction.
    monkeypatch.setattr(
        "bowhead_gateway.clarification_invalidation.publish_pending_clarification_invalidations",
        AsyncMock(side_effect=RuntimeError("delivery unavailable")),
    )
    if surface == "native":
        registry = ToolRegistry()
        register_clarification_dismiss_tool(registry, lambda: GatewayClarificationActions(app))
        handler = registry.get("clarification_dismiss").handler
        result = await handler({}, tool_ctx)
        retry = await handler({"request_id": card.request_id}, tool_ctx)
    else:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/v1/clarifications/{card.request_id}/dismiss")
            assert response.status_code == 200, response.text
            result = response.json()
            retry = (await client.post(f"/api/v1/clarifications/{card.request_id}/dismiss")).json()
    assert result["dismissed"] is True
    assert retry["dismissed"] is True
    assert (await store.get(card.request_id)).status == "dismissed"
    assert await _sql(ctx, "SELECT status FROM turns WHERE id='t_doc'") == [("cancelled",)]
    assert await _sql(ctx, "SELECT target_status,reason_code FROM clarification_invalidation_outbox") == [("dismissed", "user_rephrase")]
    assert await _sql(ctx, "SELECT COUNT(*) FROM clarification_resolution_outbox") == [(0,)]


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM workspace_members WHERE user_id='u_doc'",
        "UPDATE workspaces SET status='archived' WHERE id='ws_doc'",
        "UPDATE users SET status='disabled' WHERE id='u_doc'",
        "UPDATE sessions SET deleted_at='2026-11-04' WHERE id='s_doc'",
    ],
)
async def test_native_dismiss_rechecks_current_authority(ctx, monkeypatch, mutation):
    app, store, card, tool_ctx = await action_case(ctx, monkeypatch)
    await _sql(ctx, mutation)
    result = await GatewayClarificationActions(app).dismiss(card.request_id, tool_ctx)
    assert result["ok"] is False
    assert (await store.get(card.request_id)).status == "pending"
    assert await _sql(ctx, "SELECT COUNT(*) FROM clarification_invalidation_outbox") == [(0,)]


@pytest.mark.parametrize("field,value", [("user_id", "u_other"), ("team_id", "team_other"), ("workspace_id", "ws_other"), ("session_id", "s_other")])
async def test_native_dismiss_does_not_follow_foreign_target(ctx, monkeypatch, field, value):
    app, store, card, tool_ctx = await action_case(ctx, monkeypatch)
    result = await GatewayClarificationActions(app).dismiss(card.request_id, replace(tool_ctx, **{field: value}))
    assert result["ok"] is False
    assert (await store.get(card.request_id)).status == "pending"


async def test_native_dismiss_does_not_overwrite_an_answer(ctx, monkeypatch):
    app, store, card, tool_ctx = await action_case(ctx, monkeypatch)
    await store.resolve_once(card.request_id, answers=[ANSWER])
    result = await GatewayClarificationActions(app).dismiss(card.request_id, tool_ctx)
    assert result["dismissed"] is False
    assert (await store.get(card.request_id)).answers == [ANSWER]


@pytest.mark.parametrize("owner_key", ["plan_reapprove_plan_id", "plan_midrun_plan_id", "reference_continuation"])
async def test_native_dismiss_does_not_claim_other_legacy_continuations(ctx, monkeypatch, owner_key):
    app, store, card, tool_ctx = await action_case(ctx, monkeypatch, legacy_version=2)
    await store.set_resume_context(card.request_id, {**card.resume_context, owner_key: "owned-elsewhere"})
    result = await GatewayClarificationActions(app).dismiss(card.request_id, tool_ctx)
    assert result["ok"] is False
    assert (await store.get(card.request_id)).status == "pending"
    assert (await ctx.sessions.get_turn("t_doc")).status == "clarification_pending"


async def test_native_dismiss_observes_readonly_entitlements(ctx, monkeypatch):
    from bowhead_core.entitlements import EntitlementDenied

    app, store, card, tool_ctx = await action_case(ctx, monkeypatch)
    app.state.entitlements.assert_can_run_turn.side_effect = EntitlementDenied("suspended", 403, "Team is read-only")
    result = await GatewayClarificationActions(app).dismiss(card.request_id, tool_ctx)
    assert result["ok"] is False
    assert (await store.get(card.request_id)).status == "pending"


@pytest.mark.parametrize("legacy_version", [None, 2])
async def test_native_cancellation_preserves_a_new_running_turn(ctx, monkeypatch, legacy_version):
    app, store, card, tool_ctx = await action_case(ctx, monkeypatch, legacy_version=legacy_version)
    await _sql(ctx, "UPDATE sessions SET active_turn_id=NULL WHERE id='s_doc'")
    await ctx.sessions.record_turn(turn_id="t_new", session_id="s_doc", user_text="Cancel the earlier question")
    await _sql(ctx, "UPDATE sessions SET active_turn_id='t_new' WHERE id='s_doc'")
    result = await GatewayClarificationActions(app).dismiss(None, tool_ctx)
    assert result["dismissed"] is True
    assert await _sql(ctx, "SELECT active_turn_id FROM sessions WHERE id='s_doc'") == [("t_new",)]
    assert (await ctx.sessions.get_turn("t_new")).status == "running"
    assert (await ctx.sessions.get_turn("t_doc")).status == "cancelled"


@pytest.mark.parametrize("legacy_version", [None, 2])
async def test_cancel_rejects_an_unsettled_producer(ctx, monkeypatch, legacy_version):
    app, store, card, tool_ctx = await action_case(ctx, monkeypatch, legacy_version=legacy_version)
    await _sql(ctx, "UPDATE chat_turn_spawn_dispatch SET state='spawned' WHERE turn_id='t_doc'")
    result = await GatewayClarificationActions(app).dismiss(card.request_id, tool_ctx)
    assert result["ok"] is False
    assert (await store.get(card.request_id)).status == "pending"


@pytest.mark.parametrize("legacy_version", [None, 2])
async def test_invalidation_write_failure_rolls_back_the_turn_and_card(ctx, monkeypatch, legacy_version):
    app, store, card, tool_ctx = await action_case(ctx, monkeypatch, legacy_version=legacy_version)
    await _sql(
        ctx,
        "CREATE TRIGGER fail_dismiss BEFORE INSERT ON clarification_invalidation_outbox BEGIN SELECT RAISE(ABORT,'controlled write failure'); END",
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="controlled write failure"):
        await GatewayClarificationActions(app).dismiss(card.request_id, tool_ctx)
    assert (await store.get(card.request_id)).status == "pending"
    assert (await ctx.sessions.get_turn("t_doc")).status == "clarification_pending"


async def test_ambiguous_question_targets_are_not_guessed(ctx, monkeypatch):
    app, store, card, tool_ctx = await action_case(ctx, monkeypatch)
    other = await store.put(
        turn_id="t_other", session_id="s_doc", user_id="u_doc", workspace_id="ws_doc", token="", rationale="Another question", candidates=[]
    )
    result = await GatewayClarificationActions(app).dismiss(None, tool_ctx)
    assert result["error"] == "clarification_target_ambiguous"
    assert {item["request_id"] for item in result["targets"]} == {card.request_id, other.request_id}
    assert (await store.get(card.request_id)).status == "pending"


@pytest.mark.parametrize("legacy_version", [None, 2])
async def test_pending_session_questions_remain_readable_and_scoped(ctx, monkeypatch, legacy_version):
    app, store, card, _ = await action_case(ctx, monkeypatch, legacy_version=legacy_version)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/clarifications/session/s_doc/pending")
        assert response.status_code == 200, response.text
        assert response.json()[0]["request_id"] == card.request_id
        assert response.json()[0]["can_dismiss"] is True
        await _sql(ctx, "UPDATE workspaces SET status='archived' WHERE id='ws_doc'")
        archived = await client.get("/api/v1/clarifications/session/s_doc/pending")
        assert archived.status_code == 200, archived.text
        assert archived.json()[0]["can_dismiss"] is False
        monkeypatch.setattr(clarifications, "_current_user_id", lambda _: "u_other")
        foreign = await client.get("/api/v1/clarifications/session/s_doc/pending")
        assert foreign.status_code == 404


async def test_native_dismiss_is_lead_only():
    registry = ToolRegistry()
    service = SimpleNamespace(dismiss=AsyncMock())
    register_clarification_dismiss_tool(registry, lambda: service)
    result = await registry.get("clarification_dismiss").handler({}, SimpleNamespace(role="analyst"))
    assert result["ok"] is False
    service.dismiss.assert_not_awaited()
