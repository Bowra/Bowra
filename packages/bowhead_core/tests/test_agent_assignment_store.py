from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_assignment_store import (
    AgentAssignmentAccessError,
    AgentAssignmentCutoverCompleteError,
    AgentAssignmentStore,
    AgentAssignmentUnavailableError,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.builtin_subagent_override_store import (
    BuiltinSubagentDisplayNameConflictError,
    BuiltinSubagentOverride,
    BuiltinSubagentOverrideStore,
)
from bowhead_core.team_subagent_store import (
    TeamSubagentDeleteDependencyError,
    TeamSubagentNameConflictError,
    TeamSubagentStore,
)
from bowhead_core.user_store import TeamSubagent, UserStore


def _seed(db_path) -> None:
    timestamp = "2026-08-30T00:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES (?,?,?,?)",
            ("team_1", "Team", "active", timestamp),
        )
        connection.execute(
            "INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("ws_1", "team_1", "Space", "active", timestamp, timestamp),
        )
        for user_id in ("owner_1", "user_1"):
            connection.execute(
                "INSERT INTO users(id,team_id,email,status,created_at) VALUES (?,?,?,?,?)",
                (user_id, "team_1", f"{user_id}@example.com", "active", timestamp),
            )
            connection.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
                (f"tm_{user_id}", user_id, "team_1", "member", "active", timestamp),
            )
        connection.execute(
            "UPDATE teams SET created_by_user_id='owner_1' WHERE id='team_1'",
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,system_prompt,tool_refs_json,
              skills_json,visibility,created_by,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "osa_1",
                "team_1",
                "finance-helper",
                "Finance Helper",
                "Helps analyze finance data",
                "Analyze carefully",
                '["bi_query"]',
                "[]",
                "team_wide",
                "owner_1",
                "enabled",
                timestamp,
                timestamp,
            ),
        )
        connection.commit()


@pytest.fixture
def marketplace_db(tmp_path):
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    _seed(db_path)
    return db_path


@pytest.mark.asyncio
async def test_hire_disable_enable_and_unhire_keep_schedule_semantics(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)
    hired = await store.hire(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        agent_source_kind="team",
        live_access=True,
    )
    assert hired.assignment.personal_state == "active"

    timestamp = "2026-08-30T01:00:00+00:00"
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            """
            INSERT INTO scheduled_jobs(
              id,user_id,workspace_id,name,cron,query,status,created_at,updated_at,recipe_ref
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "sj_1",
                "user_1",
                "ws_1",
                "Daily finance",
                "0 9 * * *",
                "Analyze",
                "active",
                timestamp,
                timestamp,
                json.dumps({"form": "team_subagent", "id_or_name": "osa_1"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO scheduled_jobs(
              id,user_id,workspace_id,name,cron,query,status,created_at,updated_at,recipe_ref
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "sj_2",
                "user_1",
                "ws_1",
                "Legacy-name finance",
                "0 10 * * *",
                "Analyze",
                "active",
                timestamp,
                timestamp,
                json.dumps({"form": "team_subagent", "id_or_name": "finance-helper"}),
            ),
        )
        connection.commit()

    disabled = await store.set_personal_state(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        personal_state="disabled",
        expected_row_version=hired.assignment.row_version,
        live_access=True,
        runnable=True,
    )
    assert disabled.paused_schedule_ids == ("sj_1", "sj_2")
    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            "SELECT id,status,pause_reason FROM scheduled_jobs ORDER BY id",
        ).fetchall() == [
            ("sj_1", "paused", f"agent_personal_disabled:{hired.assignment.id}"),
            ("sj_2", "paused", f"agent_personal_disabled:{hired.assignment.id}"),
        ]

    enabled = await store.set_personal_state(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        personal_state="active",
        expected_row_version=disabled.assignment.row_version,
        live_access=True,
        runnable=True,
    )
    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute("SELECT status FROM scheduled_jobs WHERE id='sj_1'").fetchone() == ("paused",)

    impact = await store.unhire_impact(team_id="team_1", user_id="user_1", agent_id="osa_1")
    removed = await store.unhire(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        expected_row_version=enabled.assignment.row_version,
        expected_impact_digest=impact.digest,
        schedule_action=None,
    )
    assert removed.assignment.personal_state == "removed"
    assert await store.admissible_agent_ids(team_id="team_1", user_id="user_1") == frozenset()


@pytest.mark.asyncio
async def test_personal_disable_does_not_pause_same_name_schedule_in_another_team_space(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)
    hired = await store.hire(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        agent_source_kind="team",
        live_access=True,
    )
    timestamp = "2026-08-30T01:00:00+00:00"
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_2','Other','active',?)",
            (timestamp,),
        )
        connection.execute(
            "INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at) VALUES ('ws_2','team_2','Other space','active',?,?)",
            (timestamp, timestamp),
        )
        for job_id, workspace_id in (("sj_same_team", "ws_1"), ("sj_other_team", "ws_2")):
            connection.execute(
                """
                INSERT INTO scheduled_jobs(
                  id,user_id,workspace_id,name,cron,query,status,created_at,updated_at,recipe_ref
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    "user_1",
                    workspace_id,
                    job_id,
                    "0 9 * * *",
                    "Analyze",
                    "active",
                    timestamp,
                    timestamp,
                    json.dumps({"form": "team_subagent", "id_or_name": "finance-helper"}),
                ),
            )
        connection.commit()

    impact = await store.unhire_impact(team_id="team_1", user_id="user_1", agent_id="osa_1")
    assert [item.id for item in impact.jobs] == ["sj_same_team"]
    disabled = await store.set_personal_state(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        personal_state="disabled",
        expected_row_version=hired.assignment.row_version,
        live_access=True,
        runnable=True,
    )
    assert disabled.paused_schedule_ids == ("sj_same_team",)
    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            "SELECT id,status FROM scheduled_jobs WHERE id IN ('sj_same_team','sj_other_team') ORDER BY id",
        ).fetchall() == [("sj_other_team", "active"), ("sj_same_team", "paused")]


@pytest.mark.asyncio
async def test_backfill_preserves_every_pre_m1_consumer_and_opens_cutover(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            "UPDATE agent_marketplace_schema_meta SET assignment_cutover_complete_at=NULL",
        )
        connection.commit()

    dry_run = await store.backfill_existing_access(apply=False, source_build="test")
    assert dry_run.expected == 2
    assert dry_run.cutover_completed_at is None

    applied = await store.backfill_existing_access(apply=True, source_build="test")
    assert applied.expected == 2
    assert applied.inserted == 2
    assert await store.cutover_complete() is True
    assert await store.admissible_agent_ids(team_id="team_1", user_id="user_1") == frozenset({"osa_1"})

    with pytest.raises(AgentAssignmentCutoverCompleteError):
        await store.backfill_existing_access(apply=True, source_build="test")

    post_cutover_read = await store.backfill_existing_access(apply=False, source_build="test")
    assert post_cutover_read == applied


@pytest.mark.asyncio
async def test_backfill_resumes_failed_run_from_durable_member_cursor(marketplace_db, monkeypatch) -> None:
    store = AgentAssignmentStore(marketplace_db)
    original_apply_batch = store._apply_backfill_batch
    calls = 0

    async def flaky_apply_batch(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated interruption")
        return await original_apply_batch(**kwargs)

    monkeypatch.setattr(store, "_apply_backfill_batch", flaky_apply_batch)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await store.backfill_existing_access(
            apply=True,
            source_build="interrupted",
            batch_size=1,
        )

    with sqlite3.connect(marketplace_db) as connection:
        failed = connection.execute(
            """
            SELECT status,cursor_team_id,cursor_user_id,inserted_count,error_count
            FROM agent_assignment_backfill_run
            """,
        ).fetchone()
    assert failed == ("failed", "team_1", "owner_1", 1, 1)

    resumed = await AgentAssignmentStore(marketplace_db).backfill_existing_access(
        apply=True,
        source_build="resumed",
        batch_size=1,
    )
    assert resumed.expected == 2
    assert resumed.inserted == 2
    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            "SELECT status,cursor_user_id,error_count FROM agent_assignment_backfill_run",
        ).fetchone() == ("completed", "user_1", 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_assignment_backfill_run",
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_backfill_resume_reuses_fixed_snapshot_when_live_source_changes(marketplace_db, monkeypatch) -> None:
    store = AgentAssignmentStore(marketplace_db)

    async def fail_name_backfill(**_kwargs) -> None:
        raise RuntimeError("simulated name failure")

    monkeypatch.setattr(store, "_backfill_names_for_run", fail_name_backfill)
    with pytest.raises(RuntimeError, match="simulated name failure"):
        await store.backfill_existing_access(apply=True, source_build="failed")
    with sqlite3.connect(marketplace_db) as connection:
        before = connection.execute(
            "SELECT snapshot_at,expected_count,expected_digest FROM agent_assignment_backfill_run",
        ).fetchone()
        connection.execute(
            "UPDATE team_subagent SET visibility='creator_only',audience_mode='private' WHERE id='osa_1'",
        )
        connection.commit()

    resumed = await AgentAssignmentStore(marketplace_db).backfill_existing_access(
        apply=True,
        source_build="changed",
    )
    assert resumed.expected == 2
    with sqlite3.connect(marketplace_db) as connection:
        assert (
            connection.execute(
                "SELECT snapshot_at,expected_count,expected_digest FROM agent_assignment_backfill_run",
            ).fetchone()
            == before
        )
        assert (
            connection.execute(
                "SELECT assignment_cutover_complete_at FROM agent_marketplace_schema_meta",
            ).fetchone()[0]
            is not None
        )


@pytest.mark.asyncio
async def test_backfill_does_not_expand_after_cutover(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)
    await store.backfill_existing_access(apply=True, source_build="test")
    assignment = await store.get(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
    )
    assert assignment is not None
    impact = await store.unhire_impact(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
    )
    await store.unhire(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        expected_row_version=assignment.row_version,
        expected_impact_digest=impact.digest,
        schedule_action=None,
    )
    with sqlite3.connect(marketplace_db) as connection:
        timestamp = "2026-08-30T04:30:00+00:00"
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,system_prompt,tool_refs_json,
              skills_json,visibility,created_by,status,created_at,updated_at
            ) VALUES ('osa_later','team_1','later','Later Agent','Later','Later','[]','[]',
                      'team_wide','owner_1','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        connection.commit()

    with pytest.raises(AgentAssignmentCutoverCompleteError):
        await store.backfill_existing_access(apply=True, source_build="later")
    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_assignment WHERE agent_id='osa_later'",
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT personal_state FROM agent_assignment
            WHERE team_id='team_1' AND user_id='user_1' AND agent_id='osa_1'
            """,
        ).fetchone() == ("removed",)


@pytest.mark.asyncio
async def test_assignment_schema_readiness_checks_all_m1_tables_and_columns(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)
    await store.initialize()
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute("DROP TABLE agent_name_conflict_group")
        connection.commit()
    with pytest.raises(AgentAssignmentUnavailableError, match="agent_name_conflict_group"):
        await store.initialize()


@pytest.mark.asyncio
async def test_assignment_schema_readiness_rejects_missing_key_column(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    with sqlite3.connect(db_path) as connection:
        connection.execute("ALTER TABLE agent_catalog_projection DROP COLUMN projection_hash")
        connection.commit()
    with pytest.raises(AgentAssignmentUnavailableError, match="projection_hash"):
        await AgentAssignmentStore(db_path).initialize()


@pytest.mark.asyncio
async def test_assignment_schema_readiness_rejects_missing_single_owner_constraint(marketplace_db) -> None:
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute("DROP INDEX uq_agent_assignment_backfill_single_running")
        connection.commit()
    with pytest.raises(AgentAssignmentUnavailableError, match="single-running-backfill"):
        await AgentAssignmentStore(marketplace_db).initialize()


@pytest.mark.asyncio
async def test_backfill_uses_all_active_team_memberships_not_users_home_team(marketplace_db) -> None:
    timestamp = "2026-08-30T04:40:00+00:00"
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at,created_by_user_id) VALUES (?,?,?,?,?)",
            ("team_2", "Second", "active", timestamp, "owner_1"),
        )
        for user_id in ("owner_1", "user_1"):
            connection.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
                (f"tm_team2_{user_id}", user_id, "team_2", "member", "active", timestamp),
            )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,system_prompt,tool_refs_json,
              skills_json,visibility,created_by,status,created_at,updated_at
            ) VALUES ('osa_2','team_2','second','Second','Second','Second','[]','[]',
                      'team_wide','owner_1','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        connection.commit()

    result = await AgentAssignmentStore(marketplace_db).backfill_existing_access(apply=True)

    assert result.expected == 4
    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_assignment WHERE team_id='team_2' AND agent_id='osa_2'",
        ).fetchone() == (2,)


@pytest.mark.asyncio
async def test_backfill_active_lease_rejects_second_owner(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)
    timestamp = "2026-08-31T00:00:00+00:00"
    await store._start_or_resume_backfill(
        source_build="owner-a",
        timestamp=timestamp,
        owner_token="owner-a",
    )
    from bowhead_core.agent_assignment_store import AgentAssignmentBackfillLeaseError

    with pytest.raises(AgentAssignmentBackfillLeaseError, match="another process owns"):
        await store._start_or_resume_backfill(
            source_build="owner-b",
            timestamp="2026-08-31T00:00:01+00:00",
            owner_token="owner-b",
        )


@pytest.mark.asyncio
async def test_backfill_completion_timestamp_is_transactional_and_monotonic(marketplace_db) -> None:
    await AgentAssignmentStore(marketplace_db).backfill_existing_access(apply=True)
    with sqlite3.connect(marketplace_db) as connection:
        run = connection.execute(
            "SELECT started_at,snapshot_at,snapshot_completed_at,updated_at,finished_at FROM agent_assignment_backfill_run",
        ).fetchone()
        marker = connection.execute(
            "SELECT assignment_cutover_complete_at FROM agent_marketplace_schema_meta",
        ).fetchone()[0]
    assert run[4] == marker == run[3]
    assert run[4] >= max(run[0], run[1], run[2])


@pytest.mark.asyncio
async def test_suspended_recoverable_team_keeps_system_name_claims(marketplace_db) -> None:
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute("UPDATE teams SET status='suspended' WHERE id='team_1'")
        connection.commit()

    await AgentAssignmentStore(marketplace_db).backfill_existing_access(apply=True)

    with sqlite3.connect(marketplace_db) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM agent_name_claim WHERE team_id='team_1' AND holder_kind='system'",
        ).fetchone()[0]
    assert count > 0


@pytest.mark.asyncio
async def test_legacy_v1_repair_fails_closed_when_agent_changed_after_marker(
    marketplace_db,
) -> None:
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            "UPDATE team_subagent SET updated_at=? WHERE id='osa_1'",
            ("2026-08-30T02:00:00+00:00",),
        )
        connection.commit()

    with pytest.raises(
        AgentAssignmentUnavailableError,
        match="manual reconciliation required",
    ):
        await AgentAssignmentStore(marketplace_db)._legacy_v1_eligible_agent_ids(
            team_id="team_1",
            cutover_at="2026-08-30T01:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_legacy_v1_repair_fails_closed_on_unprovable_custom_grant(
    marketplace_db,
) -> None:
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute("UPDATE team_subagent SET visibility='private' WHERE id='osa_1'")
        connection.execute(
            """
            INSERT INTO resource_grants(
              id,team_id,resource_kind,resource_id,principal_type,principal_id,
              permission,effect,created_at,updated_at
            ) VALUES (
              'grant_malformed','team_1','team_subagent','osa_1','user','user_1',
              'use','allow','not-a-timestamp','2026-08-30T00:00:00+00:00'
            )
            """,
        )
        connection.commit()

    with pytest.raises(
        AgentAssignmentUnavailableError,
        match="manual reconciliation required",
    ):
        await AgentAssignmentStore(marketplace_db)._legacy_v1_custom_access_was_active(
            team_id="team_1",
            user_id="user_1",
            agent_id="osa_1",
            access_reason="user_grant",
            cutover_at="2026-08-30T01:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_backfill_converges_claim_created_during_upgrade_window(marketplace_db) -> None:
    timestamp = "2026-08-30T04:45:00+00:00"
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,system_prompt,tool_refs_json,
              skills_json,visibility,created_by,status,created_at,updated_at
            ) VALUES ('osa_2','team_1','finance-helper-new','Finance Helper','Duplicate',
                      'Duplicate','[]','[]','team_wide','owner_1','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        normalized_key = hashlib.sha256(b"finance helper").hexdigest()
        connection.execute(
            """
            INSERT INTO agent_name_claim(
              team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
            ) VALUES ('team_1','*',?,'agent','osa_2','osa_2',?)
            """,
            (normalized_key, timestamp),
        )
        connection.commit()

    await AgentAssignmentStore(marketplace_db).backfill_existing_access(
        apply=True,
        source_build="claim-window",
    )
    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            """
            SELECT holder_kind,agent_id FROM agent_name_claim
            WHERE team_id='team_1' AND normalized_key=?
            """,
            (normalized_key,),
        ).fetchone() == ("legacy_conflict", None)
        assert connection.execute(
            """
            SELECT agent_id FROM agent_name_conflict_group
            WHERE team_id='team_1' AND normalized_key=? AND resolved_at IS NULL
            ORDER BY agent_id
            """,
            (normalized_key,),
        ).fetchall() == [("osa_1",), ("osa_2",)]


@pytest.mark.asyncio
async def test_lead_roster_becomes_sticky_after_assignment_cutover(marketplace_db) -> None:
    from bowhead_core.agents.plan_admission import list_visible_team_subagents

    definitions = UserStore(marketplace_db)
    before_cutover = await list_visible_team_subagents(
        definitions,
        team_id="team_1",
        user_id="user_1",
    )
    # 8e6f85c9a: an incomplete assignment cutover fails closed instead of using legacy visibility.
    assert before_cutover == []

    assignments = AgentAssignmentStore(marketplace_db)
    await assignments.backfill_existing_access(apply=True, source_build="test")
    # A live release is also required since 8e6f85c9a.
    from types import SimpleNamespace

    from test_agent_legacy_deployment_dispatch import _publish_release

    await _publish_release(
        SimpleNamespace(db_path=marketplace_db, team_id="team_1", owner_id="owner_1"),
        agent_id="osa_1",
        name="finance-helper",
        display_name="Finance Helper",
    )
    after_cutover = await list_visible_team_subagents(
        definitions,
        team_id="team_1",
        user_id="user_1",
    )
    assert [name for name, _description, _refs in after_cutover] == [
        "finance-helper",
    ]
    assignment = await assignments.get(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
    )
    assert assignment is not None
    impact = await assignments.unhire_impact(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
    )
    await assignments.unhire(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        expected_row_version=assignment.row_version,
        expected_impact_digest=impact.digest,
        schedule_action=None,
    )

    assert (
        await list_visible_team_subagents(
            definitions,
            team_id="team_1",
            user_id="user_1",
        )
        == []
    )


@pytest.mark.asyncio
async def test_team_default_preserves_opt_out_and_repairs_new_members(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)
    applied = await store.set_team_default(
        team_id="team_1",
        agent_id="osa_1",
        actor_user_id="owner_1",
        enabled=True,
    )
    assert applied.affected_count == 0
    assert (await store.get(team_id="team_1", user_id="user_1", agent_id="osa_1")).source == "team_default"  # type: ignore[union-attr]

    assignment = await store.get(team_id="team_1", user_id="user_1", agent_id="osa_1")
    assert assignment is not None
    impact = await store.unhire_impact(team_id="team_1", user_id="user_1", agent_id="osa_1")
    await store.unhire(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        expected_row_version=assignment.row_version,
        expected_impact_digest=impact.digest,
        schedule_action=None,
    )
    await store.set_team_default(
        team_id="team_1",
        agent_id="osa_1",
        actor_user_id="owner_1",
        enabled=False,
    )
    await store.set_team_default(
        team_id="team_1",
        agent_id="osa_1",
        actor_user_id="owner_1",
        enabled=True,
    )
    assert (await store.get(team_id="team_1", user_id="user_1", agent_id="osa_1")).personal_state == "removed"  # type: ignore[union-attr]

    timestamp = "2026-08-30T03:00:00+00:00"
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            "INSERT INTO users(id,team_id,email,status,created_at) VALUES (?,?,?,?,?)",
            ("user_2", "team_1", "user_2@example.com", "active", timestamp),
        )
        connection.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
            ("tm_user_2", "user_2", "team_1", "member", "active", timestamp),
        )
        connection.commit()
    assert await store.admissible_agent_ids(team_id="team_1", user_id="user_2") == frozenset({"osa_1"})


@pytest.mark.asyncio
async def test_unhiring_a_former_team_default_preserves_future_opt_out(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)
    await store.set_team_default(
        team_id="team_1",
        agent_id="osa_1",
        actor_user_id="owner_1",
        enabled=True,
    )
    await store.set_team_default(
        team_id="team_1",
        agent_id="osa_1",
        actor_user_id="owner_1",
        enabled=False,
    )
    assignment = await store.get(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
    )
    assert assignment is not None
    assert assignment.source == "former_team_default"
    impact = await store.unhire_impact(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
    )
    await store.unhire(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        expected_row_version=assignment.row_version,
        expected_impact_digest=impact.digest,
        schedule_action=None,
    )

    await store.set_team_default(
        team_id="team_1",
        agent_id="osa_1",
        actor_user_id="owner_1",
        enabled=True,
    )
    repaired = await store.get(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
    )
    assert repaired is not None
    assert repaired.personal_state == "removed"


@pytest.mark.asyncio
async def test_team_default_never_overrides_live_resource_denies(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)
    timestamp = "2026-08-30T03:30:00+00:00"
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            """
            INSERT INTO resource_grants(
              id,team_id,resource_kind,resource_id,principal_type,principal_id,
              permission,effect,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "grant_deny_user_1",
                "team_1",
                "team_subagent",
                "osa_1",
                "user",
                "user_1",
                "use",
                "deny",
                timestamp,
                timestamp,
            ),
        )
        connection.commit()

    with pytest.raises(AgentAssignmentAccessError):
        await store.set_team_default(
            team_id="team_1",
            agent_id="osa_1",
            actor_user_id="owner_1",
            enabled=True,
        )

    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_default_policy WHERE team_id='team_1' AND agent_id='osa_1'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_team_default_rejects_a_stale_agent_definition(marketplace_db) -> None:
    store = AgentAssignmentStore(marketplace_db)

    with pytest.raises(AgentAssignmentAccessError):
        await store.set_team_default(
            team_id="team_1",
            agent_id="osa_1",
            actor_user_id="owner_1",
            enabled=True,
            expected_agent_row_version=999,
        )

    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_default_policy WHERE team_id='team_1' AND agent_id='osa_1'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_deleting_agent_revokes_assignments_and_disables_team_default(marketplace_db) -> None:
    assignments = AgentAssignmentStore(marketplace_db)
    await assignments.set_team_default(
        team_id="team_1",
        agent_id="osa_1",
        actor_user_id="owner_1",
        enabled=True,
    )
    user_assignment = await assignments.get(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
    )
    assert user_assignment is not None
    impact = await assignments.unhire_impact(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
    )
    await assignments.unhire(
        team_id="team_1",
        user_id="user_1",
        agent_id="osa_1",
        expected_row_version=user_assignment.row_version,
        expected_impact_digest=impact.digest,
        schedule_action=None,
    )

    deleted = await TeamSubagentStore(marketplace_db).delete_team_subagent(
        "team_1",
        "osa_1",
        actor_user_id="owner_1",
        audit_action=AuditAction.TEAM_SUBAGENT_DELETED,
    )
    assert deleted is True

    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            "SELECT enabled,policy_version,row_version,changed_by FROM agent_default_policy WHERE team_id='team_1' AND agent_id='osa_1'",
        ).fetchone() == (0, 2, 2, "owner_1")
        assert connection.execute(
            "SELECT DISTINCT personal_state,access_state FROM agent_assignment WHERE team_id='team_1' AND agent_id='osa_1'",
        ).fetchall() == [("removed", "revoked")]


@pytest.mark.asyncio
async def test_deleting_agent_rejects_legacy_name_schedule_binding(marketplace_db) -> None:
    timestamp = "2026-08-30T03:45:00+00:00"
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            """
            INSERT INTO scheduled_jobs(
              id,user_id,workspace_id,name,cron,query,status,created_at,updated_at,recipe_ref
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "sj_legacy_name",
                "user_1",
                "ws_1",
                "Legacy-name finance",
                "0 10 * * *",
                "Analyze",
                "paused",
                timestamp,
                timestamp,
                json.dumps({"form": "team_subagent", "id_or_name": "finance-helper"}),
            ),
        )
        connection.commit()

    with pytest.raises(TeamSubagentDeleteDependencyError) as conflict:
        await TeamSubagentStore(marketplace_db).delete_team_subagent(
            "team_1",
            "osa_1",
            actor_user_id="owner_1",
            audit_action=AuditAction.TEAM_SUBAGENT_DELETED,
        )
    assert conflict.value.code == "agent_delete_schedule_conflict"


@pytest.mark.asyncio
async def test_deleting_agent_ignores_other_team_legacy_name_schedule(marketplace_db) -> None:
    timestamp = "2026-08-30T03:50:00+00:00"
    with sqlite3.connect(marketplace_db) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_2','Other','active',?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO users(id,team_id,email,status,created_at)
            VALUES ('other_user','team_2','other@example.com','active',?)
            """,
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at)
            VALUES ('ws_other','team_2','Other space','active',?,?)
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO scheduled_jobs(
              id,user_id,workspace_id,name,cron,query,status,created_at,updated_at,recipe_ref
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "sj_other_team",
                "other_user",
                "ws_other",
                "Other finance",
                "0 10 * * *",
                "Analyze",
                "paused",
                timestamp,
                timestamp,
                json.dumps({"form": "team_subagent", "id_or_name": "finance-helper"}),
            ),
        )
        connection.commit()

    assert await TeamSubagentStore(marketplace_db).delete_team_subagent(
        "team_1",
        "osa_1",
        actor_user_id="owner_1",
        audit_action=AuditAction.TEAM_SUBAGENT_DELETED,
    )
    with sqlite3.connect(marketplace_db) as connection:
        assert connection.execute(
            "SELECT status FROM scheduled_jobs WHERE id='sj_other_team'",
        ).fetchone() == ("paused",)


@pytest.mark.asyncio
async def test_effective_display_name_claim_is_team_unique_and_released_on_delete(marketplace_db) -> None:
    assignments = AgentAssignmentStore(marketplace_db)
    await assignments.backfill_existing_access(apply=True, source_build="test")
    store = TeamSubagentStore(marketplace_db)
    timestamp = "2026-08-30T04:00:00+00:00"

    def agent(agent_id: str, name: str, display_name: str) -> TeamSubagent:
        return TeamSubagent(
            id=agent_id,
            team_id="team_1",
            name=name,
            display_name=display_name,
            description="Test",
            system_prompt="Test",
            tool_refs=[],
            max_iterations=5,
            skills=[],
            visibility="creator_only",
            created_by="owner_1",
            created_at=timestamp,
            updated_at=timestamp,
        )

    await store.create_team_subagent(
        agent("osa_2", "operations-helper", "Operations Partner"),
        actor_user_id="owner_1",
        audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
        require_active_owner=True,
    )
    with pytest.raises(TeamSubagentNameConflictError) as conflict:
        await store.create_team_subagent(
            agent("osa_3", "operations-helper-two", " operations   partner "),
            actor_user_id="owner_1",
            audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
            require_active_owner=True,
        )
    assert conflict.value.code == "display_name_unavailable"

    assert await store.delete_team_subagent(
        "team_1",
        "osa_2",
        actor_user_id="owner_1",
        audit_action=AuditAction.TEAM_SUBAGENT_DELETED,
    )
    await store.create_team_subagent(
        agent("osa_3", "operations-helper-two", "Operations Partner"),
        actor_user_id="owner_1",
        audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
        require_active_owner=True,
    )


@pytest.mark.asyncio
async def test_system_and_localized_marketplace_names_are_reserved(marketplace_db) -> None:
    assignments = AgentAssignmentStore(marketplace_db)
    await assignments.backfill_existing_access(apply=True, source_build="test")
    store = TeamSubagentStore(marketplace_db)
    timestamp = "2026-08-30T05:00:00+00:00"

    for agent_id, routing_name, display_name in (
        ("osa_2", "localized-system-collision", "业务增长分析师"),
        ("osa_3", "future-system-collision", "HR"),
    ):
        with pytest.raises(TeamSubagentNameConflictError) as conflict:
            await store.create_team_subagent(
                TeamSubagent(
                    id=agent_id,
                    team_id="team_1",
                    name=routing_name,
                    display_name=display_name,
                    description="Test",
                    system_prompt="Test",
                    tool_refs=[],
                    max_iterations=5,
                    skills=[],
                    visibility="creator_only",
                    created_by="owner_1",
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                actor_user_id="owner_1",
                audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
                require_active_owner=True,
            )
        assert conflict.value.code == "display_name_unavailable"


@pytest.mark.asyncio
async def test_builtin_override_name_claim_blocks_team_collision_and_releases_override(marketplace_db) -> None:
    assignments = AgentAssignmentStore(marketplace_db)
    await assignments.backfill_existing_access(apply=True, source_build="test")
    builtin_store = BuiltinSubagentOverrideStore(str(marketplace_db))

    with pytest.raises(BuiltinSubagentDisplayNameConflictError):
        await builtin_store.upsert(
            BuiltinSubagentOverride(
                team_id="team_1",
                name="consultant",
                display_name="Finance Helper",
            ),
            expected_version=None,
            actor_user_id="owner_1",
            audit_action=AuditAction.BUILTIN_SUBAGENT_OVERRIDE_UPDATED,
        )

    saved = await builtin_store.upsert(
        BuiltinSubagentOverride(
            team_id="team_1",
            name="consultant",
            display_name="Executive Storyteller",
        ),
        expected_version=None,
        actor_user_id="owner_1",
        audit_action=AuditAction.BUILTIN_SUBAGENT_OVERRIDE_UPDATED,
    )
    team_store = TeamSubagentStore(marketplace_db)
    timestamp = "2026-08-30T06:00:00+00:00"
    conflicting_agent = TeamSubagent(
        id="osa_2",
        team_id="team_1",
        name="executive-storyteller",
        display_name=" executive storyteller ",
        description="Test",
        system_prompt="Test",
        tool_refs=[],
        max_iterations=5,
        skills=[],
        visibility="creator_only",
        created_by="owner_1",
        created_at=timestamp,
        updated_at=timestamp,
    )
    with pytest.raises(TeamSubagentNameConflictError):
        await team_store.create_team_subagent(
            conflicting_agent,
            actor_user_id="owner_1",
            audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
            require_active_owner=True,
        )

    assert await builtin_store.delete(
        "team_1",
        "consultant",
        actor_user_id="owner_1",
        audit_action=AuditAction.BUILTIN_SUBAGENT_OVERRIDE_CLEARED,
    )
    await team_store.create_team_subagent(
        conflicting_agent,
        actor_user_id="owner_1",
        audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
        require_active_owner=True,
    )
    assert saved.display_name == "Executive Storyteller"
