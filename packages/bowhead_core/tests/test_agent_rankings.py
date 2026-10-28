"""Real execution ledgers qualify team charts without exposing small cohorts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_catalog_store import AgentCatalogStore
from bowhead_core.agent_eval_store import AgentEvalStore, EvalRunSubject
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.agent_rankings import AgentRankingStore
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.session_store import SessionStore
from test_agent_eval_store import _cases
from test_agent_execution_binding import _create_team_agent, _insert_release, _setup

pytestmark = pytest.mark.integration

NOW = datetime(2030, 2, 1, tzinfo=UTC)


@pytest.fixture
async def ranking_db(tmp_path):
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE teams SET created_by_user_id=? WHERE id=?", (fixture.owner_id, fixture.team_id))
        for index in range(8):
            actor = f"usr_chart_{index}"
            db.execute(
                "INSERT INTO users(id,team_id,email,status,created_at) VALUES(?,?,?,'active','2026-01-01')",
                (actor, fixture.team_id, f"{actor}@example.com"),
            )
            db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES(?,?,?,'member','active','2026-01-01')",
                (f"tm_chart_{index}", actor, fixture.team_id),
            )
            db.execute(
                "INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES(?,?,'member','active','2026-01-01')",
                (fixture.workspace_id, actor),
            )
        db.execute(
            "INSERT OR IGNORE INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES(?,?,'member','active','2026-01-01')",
            (fixture.workspace_id, fixture.owner_id),
        )
        db.execute("UPDATE agent_ranking_coverage SET complete_days_from='2026-01-01T00:00:00+00:00'")
    assignments = AgentAssignmentStore(fixture.db_path)
    for index in range(8):
        await assignments.hire(
            team_id=fixture.team_id, user_id=f"usr_chart_{index}", agent_id="osa_exact", agent_source_kind="team", live_access=True
        )
    return fixture


async def completed_use(
    fixture, monkeypatch, *, actor, day="2030-01-30", suffix="", origin="normal", source="detail", status="completed", child=False, system=None
):
    timestamp = f"{day}T12:00:00+00:00"
    monkeypatch.setattr("bowhead_core.agent_execution_binding._now", lambda: timestamp)
    monkeypatch.setattr("bowhead_core.session_store._now", lambda: timestamp)
    sessions = SessionStore(fixture.db_path)
    session_id = f"s_chart_{actor}_{day}_{suffix}"
    turn_id = f"t_chart_{actor}_{day}_{suffix}"
    await sessions.ensure_session(session_id=session_id, workspace_id=fixture.workspace_id, user_id=actor, origin=origin)
    await sessions.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="Summarize the report",
        user_source=ChatTurnUserSource(turn_id, session_id, actor, fixture.workspace_id, "Summarize the report", (), (), None),
    )
    executions = AgentExecutionBindingStore(fixture.db_path)
    if system:
        binding = await executions.admit_system_execution(
            team_id=fixture.team_id,
            user_id=actor,
            turn_id=turn_id,
            profile_name=system,
            dispatch_key="chart",
            definition={"llm_agent_type": "analyst", "name": system, "schema_version": 1},
            manifest_fingerprint="sha256:" + "1" * 64,
            override_version=None,
            system_required=True,
        )
        assignment_id = None
    else:
        binding = await executions.admit_team_execution(
            team_id=fixture.team_id, user_id=actor, turn_id=turn_id, profile_name="exact-agent", dispatch_key="chart"
        )
        with sqlite3.connect(fixture.db_path) as db:
            assignment_id = db.execute(
                "SELECT id FROM agent_assignment WHERE team_id=? AND user_id=? AND agent_id='osa_exact'", (fixture.team_id, actor)
            ).fetchone()[0]
    if child:
        child_binding = await executions.admit_team_execution(
            team_id=fixture.team_id,
            user_id=actor,
            turn_id=turn_id,
            profile_name="exact-agent",
            dispatch_key="child",
            parent_execution_id=binding.execution_id,
        )
        await executions.mark_running(child_binding.execution_id)
        await executions.mark_terminal(child_binding.execution_id, status="completed")
    async with open_shared_ctx(fixture.db_path) as db:
        await executions.insert_root_binding_in_connection(db, binding=binding, user_id=actor, assignment_id=assignment_id, binding_source=source)
        await db.commit()
    await executions.mark_running(binding.execution_id)
    await executions.mark_terminal(binding.execution_id, status=status)
    # A root execution can finish successfully while its containing Turn fails.
    await sessions.mark_completed(turn_id=turn_id, intent="analysis", final_answer="Report summary", tokens_input=0, tokens_output=0, cost_usd=0)
    return turn_id


async def collection(fixture, kind="popular", *, items=None, now=NOW, user_id=None, domain=None):
    sources = await AgentCatalogStore(fixture.db_path).list_team_sources(team_id=fixture.team_id)
    item = {
        "agent_id": "osa_exact",
        "display_name": "Exact Agent",
        "summary": "Summarize reports",
        "source": {"kind": "team"},
        "facets": {"domains": ["finance"]},
        "availability": {"discoverable": True, "hireable": True},
        "actions": {"view_detail": True},
        "safety": {"status": "pass"},
    }
    return await AgentRankingStore(fixture.db_path).collection(
        team_id=fixture.team_id,
        user_id=user_id or fixture.member_id,
        ranking_kind=kind,
        domain_id=domain,
        items=items if items is not None else [item],
        team_row_versions={source.id: source.row_version for source in sources},
        system_override_versions={},
        now=now,
    )


@pytest.mark.asyncio
async def test_successful_members_cross_privacy_floor_without_counting_retries(ranking_db, monkeypatch):
    fixture = ranking_db
    for index in range(4):
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}")
    hidden = await collection(fixture)
    assert hidden["state"] == "insufficient_sample"
    assert hidden["rows"] == []
    for suffix in ("first", "retry", "another-task"):
        await completed_use(fixture, monkeypatch, actor="usr_chart_4", suffix=suffix)
    ranked = await collection(fixture)
    assert ranked["state"] == "ready"
    assert ranked["rows"][0]["active_members_28d"] == 5
    assert ranked["rows"][0]["hire_members_current"] == 9
    assert ranked["rows"][0]["display_rank"] == 1
    with sqlite3.connect(fixture.db_path) as db:
        stored = db.execute("SELECT rows_json FROM agent_ranking_snapshot WHERE state='ready'").fetchone()[0]
        assert "usr_chart" not in stored and "Report summary" not in stored
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE agent_ranking_snapshot SET rows_json='[]'")


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", ["owner", "agent_eval", "workflow_dryrun", "schedule", "failed", "child", "turn_failed", "today", "outside"])
async def test_non_qualifying_fifth_member_cannot_create_a_chart(ranking_db, monkeypatch, excluded):
    fixture = ranking_db
    for index in range(4):
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}")
    options = {"actor": "usr_chart_4"}
    if excluded == "owner":
        options["actor"] = fixture.owner_id
        await AgentAssignmentStore(fixture.db_path).hire(
            team_id=fixture.team_id, user_id=fixture.owner_id, agent_id="osa_exact", agent_source_kind="team", live_access=True
        )
    elif excluded in {"agent_eval", "workflow_dryrun"}:
        options["origin"] = excluded
    elif excluded == "schedule":
        options["source"] = "schedule"
    elif excluded == "failed":
        options["status"] = "failed"
    elif excluded == "child":
        options.update(child=True, status="failed")
    elif excluded == "turn_failed":
        pass
    elif excluded == "today":
        options["day"] = "2030-02-01"
    else:
        options["day"] = "2030-01-03"
    turn = await completed_use(fixture, monkeypatch, **options)
    if excluded == "turn_failed":
        with sqlite3.connect(fixture.db_path) as db:
            db.execute("UPDATE turns SET status='failed' WHERE id=?", (turn,))
    assert (await collection(fixture))["rows"] == []


@pytest.mark.asyncio
async def test_trending_requires_two_private_complete_windows_and_positive_absolute_growth(ranking_db, monkeypatch):
    fixture = ranking_db
    for index in range(5):
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}", day="2030-01-20")
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}", day="2030-01-30")
    assert (await collection(fixture, "trending"))["rows"] == []
    await completed_use(fixture, monkeypatch, actor="usr_chart_5", day="2030-01-25")
    ranked = await collection(fixture, "trending")
    assert ranked["rows"][0]["active_members_7d"] == 6
    assert ranked["rows"][0]["active_members_previous_7d"] == 5
    assert ranked["window_start"] == "2030-01-25T00:00:00+00:00"
    assert ranked["comparison_window_end"] == ranked["window_start"]


@pytest.mark.asyncio
async def test_membership_hires_access_and_erasure_invalidate_previous_snapshot(ranking_db, monkeypatch):
    fixture = ranking_db
    turns = [await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}") for index in range(5)]
    assert (await collection(fixture))["state"] == "ready"
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("DELETE FROM turns WHERE id=?", (turns[0],))
    assert (await collection(fixture))["rows"] == []
    await completed_use(fixture, monkeypatch, actor="usr_chart_0", suffix="new")
    assert (await collection(fixture))["state"] == "ready"
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_members SET status='removed' WHERE user_id='usr_chart_0'")
    assert (await collection(fixture))["rows"] == []
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_members SET status='active' WHERE user_id='usr_chart_0'")
        db.execute("UPDATE team_subagent SET visibility='creator_only' WHERE id='osa_exact'")
    assert (await collection(fixture))["rows"] == []


@pytest.mark.asyncio
async def test_incomplete_coverage_has_no_backfilled_chart(ranking_db, monkeypatch):
    fixture = ranking_db
    for index in range(5):
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}")
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE agent_ranking_coverage SET complete_days_from='2030-01-15T00:00:00+00:00'")
    popular = await collection(fixture)
    assert popular["reason"] == "window_incomplete" and popular["rows"] == []
    assert popular["available_at"] == "2030-02-12T00:00:00+00:00"
    assert (await collection(fixture, "trending"))["state"] == "insufficient_sample"
    assert "reason" not in await collection(fixture, "trending")


@pytest.mark.asyncio
async def test_disabled_system_agent_is_removed_and_implicit_adoption_is_not_a_usage_count(ranking_db, monkeypatch):
    fixture = ranking_db
    item = {
        "agent_id": "system:compute",
        "display_name": "Compute",
        "summary": "Compute results",
        "source": {"kind": "system"},
        "facets": {"domains": ["general"]},
        "availability": {"discoverable": True, "runnable": True},
        "actions": {"view_detail": True},
        "safety": {"status": "managed"},
    }
    assert (await collection(fixture, items=[item]))["rows"] == []
    for index in range(5):
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}", system="compute")
    assert (await collection(fixture, items=[item]))["rows"][0]["active_members_28d"] == 5
    with sqlite3.connect(fixture.db_path) as db:
        db.execute(
            "INSERT INTO team_builtin_subagent_override(team_id,name,enabled,row_version,created_at,updated_at) VALUES(?,'compute',0,1,'2030-01-31','2030-01-31')",
            (fixture.team_id,),
        )
    assert (await collection(fixture, items=[item]))["rows"] == []


@pytest.mark.asyncio
async def test_current_adoption_floor_and_repeat_use_determine_order(ranking_db, monkeypatch):
    fixture = ranking_db
    team_item = (await collection(fixture))["rows"]
    assert team_item == []
    system_item = {
        "agent_id": "system:compute",
        "display_name": "Compute",
        "summary": "Compute",
        "source": {"kind": "system"},
        "facets": {"domains": ["general"]},
        "availability": {"discoverable": True, "runnable": True},
        "actions": {"view_detail": True},
        "safety": {"status": "managed"},
    }
    team_item = {**system_item, "agent_id": "osa_exact", "source": {"kind": "team"}, "safety": {"status": "pass"}}
    for index in range(5):
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}", suffix="team")
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}", suffix="system", system="compute")
    items = [team_item, system_item]
    assert [row["item"]["agent_id"] for row in (await collection(fixture, items=items))["rows"]] == ["osa_exact", "system:compute"]
    await completed_use(fixture, monkeypatch, actor="usr_chart_0", day="2030-01-29", system="compute")
    assert [row["item"]["agent_id"] for row in (await collection(fixture, items=items))["rows"]] == ["system:compute", "osa_exact"]
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE agent_assignment SET personal_state='disabled' WHERE user_id IN ('usr_chart_4','usr_chart_5','usr_chart_6','usr_chart_7')")
    assert (await collection(fixture, items=items))["rows"][1]["hire_members_current"] == 5
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE agent_assignment SET personal_state='disabled' WHERE user_id='usr_chart_3'")
    final = await collection(fixture, items=items)
    assert len(final["rows"]) == 1 and final["rows"][0]["item"]["agent_id"] == "system:compute"


@pytest.mark.asyncio
async def test_current_critical_eval_failure_removes_a_rank_even_when_evidence_is_blocked(ranking_db, monkeypatch):
    fixture = ranking_db
    await _insert_release(
        fixture,
        sequence=2,
        prompt="Current report instructions",
        capability_fingerprint="sha256:" + "1" * 64,
        dependency_fingerprint="sha256:" + "2" * 64,
    )
    for index in range(5):
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}")
    assert (await collection(fixture))["state"] == "ready"
    with sqlite3.connect(fixture.db_path) as db:
        live = db.execute("SELECT live_revision_id,live_release_id FROM team_subagent WHERE id='osa_exact'").fetchone()
        hashes = db.execute("SELECT capability_fingerprint,dependency_fingerprint FROM team_subagent_release WHERE id=?", (live[1],)).fetchone()
    evaluations = AgentEvalStore(fixture.db_path)
    suite = await evaluations.create_suite(
        team_id=fixture.team_id,
        agent_id="osa_exact",
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Critical contracts",
        visibility="owner",
        actor_user_id=fixture.owner_id,
    )
    revision = await evaluations.create_suite_revision(
        suite_id=suite.suite_id,
        definition={},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=_cases()[:1],
        actor_user_id=fixture.owner_id,
    )
    current_suite = await evaluations.read_suite(suite.suite_id)
    await evaluations.update_suite(
        suite_id=suite.suite_id, actor_user_id=fixture.owner_id, expected_row_version=current_suite.row_version, state="active"
    )
    subject = EvalRunSubject(
        team_id=fixture.team_id,
        agent_id="osa_exact",
        agent_source_kind="team",
        revision_ref=live[0],
        release_id=live[1],
        capability_fingerprint=hashes[0],
        dependency_fingerprint=hashes[1],
        model_config_hash="sha256:" + "1" * 64,
    )
    run = await evaluations.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="critical-chart",
        actor_user_id=fixture.owner_id,
    )
    await evaluations.mark_run_running(run.run_id)
    await evaluations.record_case_result(
        run_id=run.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="failed",
        deterministic={"passed": False, "critical_failure": True},
    )
    await evaluations.mark_run_terminal(run.run_id, status="failed", summary={"case_count": 1, "critical_failure": True})
    await evaluations.project_evidence(
        team_id=fixture.team_id,
        agent_source_kind="team",
        agent_id="osa_exact",
        suite_id=suite.suite_id,
        subject_fingerprint=subject.fingerprint,
        state="blocked",
        current_run_id=run.run_id,
    )
    assert (await collection(fixture))["rows"] == []


@pytest.mark.asyncio
async def test_fresh_security_denial_excludes_usage_even_before_assignment_reconciliation(ranking_db, monkeypatch):
    fixture = ranking_db
    for index in range(5):
        await completed_use(fixture, monkeypatch, actor=f"usr_chart_{index}")
    assert (await collection(fixture))["state"] == "ready"
    with sqlite3.connect(fixture.db_path) as db:
        db.execute(
            """INSERT INTO resource_grants(id,team_id,resource_kind,resource_id,principal_type,
                   principal_id,permission,effect,created_by,created_at,updated_at)
                   VALUES('rg_chart_deny',?,'team_subagent','osa_exact','user','usr_chart_4','use','deny',?,'2030-01-31','2030-01-31')""",
            (fixture.team_id, fixture.owner_id),
        )
        assert db.execute("SELECT access_state FROM agent_assignment WHERE user_id='usr_chart_4'").fetchone()[0] == "granted"
    assert (await collection(fixture))["rows"] == []
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE resource_grants SET revoked_at='2030-01-31' WHERE id='rg_chart_deny'")
    assert (await collection(fixture))["state"] == "ready"
    assert (await collection(fixture, user_id="usr_from_another_team"))["rows"] == []
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_subagent SET visibility='creator_only' WHERE id='osa_exact'")
    # Management visibility never admits a private Agent to a team chart.
    assert (await collection(fixture, user_id=fixture.owner_id))["rows"] == []
