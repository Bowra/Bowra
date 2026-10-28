"""Charts reuse consumer projections and stay gated on unsupported deployments."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import test_agents_routes as catalog_tests
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import UserStore

pytestmark = pytest.mark.integration

app_client = catalog_tests.app_client


@pytest.mark.asyncio
async def test_rankings_are_available_and_reject_invalid_categories(app_client, monkeypatch):
    client, _ = app_client
    enabled = await client.get("/api/v1/agents/rankings")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["reason"] == "window_incomplete"
    assert (await client.get("/api/v1/agents/rankings?domain=secret-custom-tag")).status_code == 422
    assert (await client.get("/api/v1/agents/rankings?ranking_kind=quality")).status_code == 422


@pytest.mark.asyncio
async def test_new_deployment_explains_complete_coverage_without_disclosing_counts(app_client, monkeypatch):
    client, _ = app_client
    result = await client.get("/api/v1/agents/rankings?ranking_kind=trending&domain=finance")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["kind"] == "agent_ranking_collection"
    assert data["ranking_kind"] == "trending" and data["domain_id"] == "finance"
    assert data["reason"] == "window_incomplete" and data["rows"] == []
    assert data["state"] == "insufficient_sample"
    assert result.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_complete_small_team_returns_privacy_safe_empty_chart(app_client, monkeypatch):
    client, db_path = app_client
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE agent_ranking_coverage SET complete_days_from='2026-01-01T00:00:00+00:00'")
    result = await client.get("/api/v1/agents/rankings")
    assert result.status_code == 200, result.text
    assert result.json()["rows"] == [] and "reason" not in result.json()
    assert result.json()["state"] == "insufficient_sample"


@pytest.mark.asyncio
async def test_real_successful_executions_produce_current_consumer_rows(app_client, monkeypatch):
    client, db_path = app_client
    workspace = await UserStore(db_path).create_local_workspace(team_id="team_1", name="Chart Space", creator_user_id="user_1")
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    timestamp = f"{yesterday}T12:00:00+00:00"
    monkeypatch.setattr("bowhead_core.agent_execution_binding._now", lambda: timestamp)
    monkeypatch.setattr("bowhead_core.session_store._now", lambda: timestamp)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE agent_ranking_coverage SET complete_days_from='2026-01-01T00:00:00+00:00'")
        for index in range(5):
            actor = f"usr_chart_{index}"
            db.execute(
                "INSERT INTO users(id,team_id,email,status,created_at) VALUES(?,'team_1',?,'active','2026-01-01')", (actor, f"{actor}@example.com")
            )
            db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES(?,?,'team_1','member','active','2026-01-01')",
                (f"tm_{actor}", actor),
            )
            db.execute(
                "INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES(?,?,'member','active','2026-01-01')",
                (workspace, actor),
            )
    sessions, executions = SessionStore(db_path), AgentExecutionBindingStore(db_path)
    for index in range(5):
        actor, session, turn = f"usr_chart_{index}", f"s_chart_{index}", f"t_chart_{index}"
        await sessions.ensure_session(session_id=session, workspace_id=workspace, user_id=actor)
        await sessions.record_turn(
            turn_id=turn,
            session_id=session,
            user_text="Compute totals",
            user_source=ChatTurnUserSource(turn, session, actor, workspace, "Compute totals", (), (), None),
        )
        binding = await executions.admit_system_execution(
            team_id="team_1",
            user_id=actor,
            turn_id=turn,
            profile_name="compute",
            dispatch_key="chart",
            definition={"llm_agent_type": "analyst", "name": "compute", "schema_version": 1},
            manifest_fingerprint="sha256:" + "1" * 64,
            override_version=None,
            system_required=True,
        )
        async with open_shared_ctx(db_path) as db:
            await executions.insert_root_binding_in_connection(db, binding=binding, user_id=actor, assignment_id=None, binding_source="detail")
            await db.commit()
        await executions.mark_running(binding.execution_id)
        await executions.mark_terminal(binding.execution_id, status="completed")
        await sessions.mark_completed(turn_id=turn, intent="analysis", final_answer="Totals", tokens_input=0, tokens_output=0, cost_usd=0)
    result = await client.get("/api/v1/agents/rankings?domain=general")
    assert result.status_code == 200, result.text
    row = result.json()["rows"][0]
    assert row["display_rank"] == 1 and row["item"]["agent_id"] == "system:compute"
    assert row["active_members_28d"] == 5 and row["hire_members_current"] == 6
    assert row["item"]["actions"]["view_detail"] is True
    assert "usr_chart" not in result.text and "Compute totals" not in result.text
    assert (await client.get("/api/v1/agents/rankings?domain=finance")).json()["rows"] == []
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_members SET status='removed' WHERE user_id='usr_chart_4'")
    assert (await client.get("/api/v1/agents/rankings")).json()["rows"] == []
