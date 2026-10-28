from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_dependency_compiler import ExactSkillGrant
from bowhead_core.resource_dependency_projection import (
    ResourceDependencyBackfillService,
    ResourceDependencyEdge,
    ResourceDependencyProjection,
    edges_from_compilation,
    replace_subject_projection_conn,
    stable_edges_from_definition,
)
from bowhead_core.resource_usage_projection import (
    ResourceUsageProjectionService,
    ResourceUsageSelector,
)
from bowhead_core.user_store import TeamSubagent, UserStore


def _migrated_db(tmp_path: Path) -> Path:
    requested_path = tmp_path / "requested.db"
    assert upgrade(f"sqlite+aiosqlite:///{requested_path}", target="core") == ("core",)
    return tmp_path / "bowhead.db"


def _seed_working_copy(db_path: Path) -> None:
    timestamp = "2026-09-05T00:00:00+00:00"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active',?)",
            (timestamp,),
        )
        db.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,created_at,updated_at,definition_mode
            ) VALUES ('osa_1','team_1','agent-one',?,?,'revisioned')
            """,
            (timestamp, timestamp),
        )
        db.execute(
            """
            INSERT INTO team_subagent_working_copy(
              id,team_id,subagent_id,definition_json,listing_json,audience_json,
              validation_json,updated_by,created_at,updated_at
            ) VALUES (
              'awc_1','team_1','osa_1','{}','{}','{"mode":"private"}',
              '{"status":"not_validated"}','usr_1',?,?
            )
            """,
            (timestamp, timestamp),
        )
        db.commit()


def test_0039_creates_projection_schema_and_scope_guards(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path)
    _seed_working_copy(db_path)
    fingerprint = f"sha256:{'1' * 64}"
    with sqlite3.connect(db_path) as db:
        objects = {
            (str(row[0]), str(row[1]))
            for row in db.execute(
                """
                SELECT type,name FROM sqlite_schema
                WHERE name LIKE '%resource_dependency%'
                """
            )
        }
        assert ("table", "resource_dependency_subject_projection") in objects
        assert ("table", "resource_dependency_edge") in objects
        assert ("index", "idx_resource_dependency_reverse") in objects
        assert ("index", "idx_resource_dependency_parent") in objects
        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            db.execute(
                """
                INSERT INTO resource_dependency_subject_projection(
                  team_id,subject_kind,subject_ref,agent_id,subject_fingerprint,
                  status,reason_code,compiler_version,projected_at
                ) VALUES (
                  'team_1','agent_working_copy','awc_1','osa_wrong',?,
                  'complete',NULL,'v1','2026-09-05T00:00:00+00:00'
                )
                """,
                (fingerprint,),
            )


@pytest.mark.asyncio
async def test_working_projection_replaces_as_one_group(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path)
    _seed_working_copy(db_path)
    first_fingerprint = f"sha256:{'1' * 64}"
    second_fingerprint = f"sha256:{'2' * 64}"
    first = ResourceDependencyProjection(
        team_id="team_1",
        subject_kind="agent_working_copy",
        subject_ref="awc_1",
        agent_id="osa_1",
        subject_fingerprint=first_fingerprint,
        status="complete",
        reason_code=None,
    )
    first_edge = ResourceDependencyEdge(
        team_id="team_1",
        subject_kind="agent_working_copy",
        subject_ref="awc_1",
        agent_id="osa_1",
        resource_kind="skill",
        resource_ref="builtin:builtin:pdf",
        resource_parent_ref=None,
        resource_revision=f"sha256:{'3' * 64}",
        dependency_role="required",
        evidence_level="exact",
        subject_fingerprint=first_fingerprint,
    )
    second = ResourceDependencyProjection(
        team_id="team_1",
        subject_kind="agent_working_copy",
        subject_ref="awc_1",
        agent_id="osa_1",
        subject_fingerprint=second_fingerprint,
        status="complete",
        reason_code=None,
    )
    async with open_shared_ctx(str(db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await replace_subject_projection_conn(db, projection=first, edges=(first_edge,))
        await replace_subject_projection_conn(db, projection=second, edges=())
        await db.commit()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM resource_dependency_edge").fetchone() == (0,)
        assert db.execute("SELECT subject_fingerprint FROM resource_dependency_subject_projection").fetchone() == (second_fingerprint,)


def test_compilation_uses_resolved_source_id_without_reverse_parsing() -> None:
    grant = ExactSkillGrant(
        kind="builtin",
        id="builtin:pdf",
        name="pdf",
        permissions=("invoke", "read"),
        revision=f"sha256:{'4' * 64}",
        distribution="reference",
    )
    compilation = SimpleNamespace(
        compiler_version="agent-dependency-v1",
        skills=(SimpleNamespace(grant=grant, available=True),),
        tools=(
            SimpleNamespace(
                stable_ref="opaque-operation-ref",
                source_kind="team_mcp",
                source_id="ots_source",
                revision="rev-7",
                available=True,
                connection_ref="team_tool_source:ots_source",
            ),
        ),
    )
    projection, edges = edges_from_compilation(
        team_id="team_1",
        subject_kind="agent_revision",
        subject_ref="arv_1",
        agent_id="osa_1",
        subject_fingerprint=f"sha256:{'5' * 64}",
        compilation=compilation,  # type: ignore[arg-type]
    )
    operation = next(edge for edge in edges if edge.resource_kind == "tool_operation")
    assert projection.status == "complete"
    assert operation.resource_ref == "opaque-operation-ref"
    assert operation.resource_parent_ref == "ots_source"


def test_backfill_refuses_to_guess_team_tool_source() -> None:
    projection, edges = stable_edges_from_definition(
        team_id="team_1",
        subject_kind="agent_revision",
        subject_ref="arv_1",
        agent_id="osa_1",
        subject_fingerprint=f"sha256:{'6' * 64}",
        definition={"tool_refs": ["teammcp.looks.structured-but-is-opaque"]},
    )
    assert projection.status == "partial"
    assert all(edge.resource_parent_ref is None for edge in edges)
    assert all(edge.resource_kind != "connection" for edge in edges)


@pytest.mark.asyncio
async def test_backfill_maps_tool_source_only_from_authoritative_registry(
    tmp_path: Path,
) -> None:
    db_path = _migrated_db(tmp_path)
    _seed_working_copy(db_path)
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Metrics", "version": "1"},
        "paths": {
            "/metrics": {
                "get": {
                    "operationId": "query_metrics",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO team_tool_source(
              id,team_id,name,kind,openapi_spec_json,base_url,created_at,updated_at
            ) VALUES (
              'ots_metrics','team_1','Metrics','openapi',?,'https://example.com',?,?
            )
            """,
            (
                json.dumps(spec),
                "2026-09-05T00:00:00+00:00",
                "2026-09-05T00:00:00+00:00",
            ),
        )
        db.execute(
            """
            UPDATE team_subagent_working_copy
            SET definition_json=?
            WHERE id='awc_1'
            """,
            (json.dumps({"tool_refs": ["teammcp.ots_metrics.query_metrics"]}),),
        )
        db.commit()

    result = await ResourceDependencyBackfillService(str(db_path)).run_page(
        apply=True,
    )

    assert result.projected_count == 1
    with sqlite3.connect(db_path) as db:
        operation = db.execute(
            """
            SELECT resource_parent_ref,evidence_level
            FROM resource_dependency_edge
            WHERE resource_kind='tool_operation'
            """
        ).fetchone()
        connection = db.execute(
            """
            SELECT resource_ref
            FROM resource_dependency_edge
            WHERE resource_kind='connection'
            """
        ).fetchone()
    assert operation == ("ots_metrics", "stable_only")
    assert connection == ("team_tool_source:ots_metrics",)


@pytest.mark.asyncio
async def test_usage_projection_derives_current_state_and_actor_visibility(
    tmp_path: Path,
) -> None:
    db_path = _migrated_db(tmp_path)
    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name="Usage", max_seats=2)
    owner = await users.create_user(team_id=team.id, email="usage@example.com")
    await users.create_team_subagent(
        TeamSubagent(
            id="osa_usage",
            team_id=team.id,
            name="usage-agent",
            display_name="Usage Agent",
            visibility="team_wide",
            created_by=owner.id,
            created_at="2026-09-05T00:00:00+00:00",
            updated_at="2026-09-05T00:00:00+00:00",
        )
    )
    definition = {"skill_grants": [], "tool_refs": []}
    definition_json = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    definition_hash = f"sha256:{hashlib.sha256(definition_json.encode()).hexdigest()}"
    async with open_shared_ctx(str(db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
              definition_hash,dependency_fingerprint,capability_fingerprint,
              risk_compiler_version,risk_json,created_by,created_at
            ) VALUES (
              'arv_usage',?, 'osa_usage',1,'authored',?,'{}',?,
              'sha256:dep','sha256:cap','risk-v1','{}',?,'2026-09-05T00:00:00+00:00'
            )
            """,
            (team.id, definition_json, definition_hash, owner.id),
        )
        await db.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,status,
              deterministic_json,semantic_json,safe_summary_json,created_by,created_at
            ) VALUES (
              'asr_usage',?,'osa_usage','revision','arv_usage',?,
              'sha256:dep','safety-v1','none','pass','{}','{}','{}',?,
              '2026-09-05T00:00:00+00:00'
            )
            """,
            (team.id, definition_hash, owner.id),
        )
        await db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,
              audience_snapshot_json,audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_json,safety_report_id,activated_by,activated_at
            ) VALUES (
              'arl_usage',?,'osa_usage',1,'arv_usage','{}','sha256:aud',
              'sha256:cap','sha256:dep','{}','asr_usage',?,
              '2026-09-05T00:00:00+00:00'
            )
            """,
            (team.id, owner.id),
        )
        await db.execute(
            """
            UPDATE team_subagent
            SET live_revision_id='arv_usage',live_release_id='arl_usage',
                release_sequence=1,definition_mode='revisioned'
            WHERE id='osa_usage'
            """
        )
        projection = ResourceDependencyProjection(
            team_id=team.id,
            subject_kind="agent_revision",
            subject_ref="arv_usage",
            agent_id="osa_usage",
            subject_fingerprint=definition_hash,
            status="complete",
            reason_code=None,
        )
        edge = ResourceDependencyEdge(
            team_id=team.id,
            subject_kind="agent_revision",
            subject_ref="arv_usage",
            agent_id="osa_usage",
            resource_kind="skill",
            resource_ref="builtin:builtin:pdf",
            resource_parent_ref=None,
            resource_revision=f"sha256:{'7' * 64}",
            dependency_role="required",
            evidence_level="exact",
            subject_fingerprint=definition_hash,
        )
        await replace_subject_projection_conn(db, projection=projection, edges=(edge,))
        await db.commit()

    view = await ResourceUsageProjectionService(str(db_path)).usage(
        team_id=team.id,
        actor_user_id=owner.id,
        selector=ResourceUsageSelector(
            key="pdf",
            kind="skill",
            resource_id="pdf",
            display_name="PDF",
            resource_ref="builtin:builtin:pdf",
        ),
    )

    assert view.summary.current_agent_count == 1
    assert view.summary.completeness == "complete"
    assert view.groups["current"][0].display_name == "Usage Agent"
    assert view.groups["current"][0].readiness == "ready"


async def _team_with_one_agent(tmp_path: Path, *, definition_mode: str, status: str = "enabled"):
    """A team owning exactly one Agent, in the requested release/enable state. No projections."""
    db_path = _migrated_db(tmp_path)
    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name="Legacy", max_seats=2)
    owner = await users.create_user(team_id=team.id, email="legacy@example.com")
    await users.create_team_subagent(
        TeamSubagent(
            id="osa_legacy",
            team_id=team.id,
            name="legacy-agent",
            display_name="Legacy Agent",
            visibility="team_wide",
            created_by=owner.id,
            created_at="2026-09-06T00:00:00+00:00",
            updated_at="2026-09-06T00:00:00+00:00",
        )
    )
    async with open_shared_ctx(str(db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "UPDATE team_subagent SET definition_mode=?,status=? WHERE id='osa_legacy'",
            (definition_mode, status),
        )
        await db.commit()
    return db_path, team, owner


async def _tool_source_usage(db_path: Path, team, owner):
    """Usage for a tool source nothing references — the shape every 116 card is in today."""
    return await ResourceUsageProjectionService(str(db_path)).usage(
        team_id=team.id,
        actor_user_id=owner.id,
        selector=ResourceUsageSelector(
            key="ots_x",
            kind="tool_source",
            resource_id="ots_x",
            display_name="A tool source",
            resource_ref="team_tool_source:ots_x",
        ),
    )


@pytest.mark.asyncio
async def test_unprojectable_team_agents_block_the_unused_claim(tmp_path: Path) -> None:
    """Zero matches must not read as "not used by any Agent" while Agents exist that this
    pipeline cannot examine.

    A pre-cutover team keeps its real Agents in ``team_subagent`` as ``legacy_mutable``; the
    projection's subject universe is working-copies / revisions / system revisions, so their tool
    usage is never compiled. 116 measured: 6 such Agents, 0 dependency edges to any team tool
    source. Reporting ``complete`` there would state something nobody checked."""
    db_path, team, owner = await _team_with_one_agent(tmp_path, definition_mode="legacy_mutable")

    view = await _tool_source_usage(db_path, team, owner)

    assert view.summary.current_agent_count == 0
    assert view.summary.completeness == "partial"
    assert "team_agents_not_projectable" in view.summary.reason_codes


@pytest.mark.asyncio
async def test_usage_projection_normalizes_a_caller_owned_connection(tmp_path: Path) -> None:
    """Impact-preview callers pass a plain aiosqlite connection.

    The projection must not depend on the caller remembering ``Row`` mode: with a real
    team member the access resolver reads named columns, which used to crash only on
    populated deployments while empty fixtures passed.
    """
    db_path, team, owner = await _team_with_one_agent(
        tmp_path,
        definition_mode="legacy_mutable",
    )
    selector = ResourceUsageSelector(
        key="pdf",
        kind="skill",
        resource_id="pdf",
        display_name="PDF",
        resource_ref="builtin:builtin:pdf",
    )

    async with aiosqlite.connect(db_path) as db:
        assert db.row_factory is None
        views = await ResourceUsageProjectionService(str(db_path)).usage_many_in_connection(
            db,
            team_id=team.id,
            actor_user_id=owner.id,
            selectors=(selector,),
            governance_action_allowed=True,
            include_groups=True,
        )

    assert views["pdf"].summary.completeness == "partial"


@pytest.mark.asyncio
async def test_revisioned_team_agents_do_not_block_it(tmp_path: Path) -> None:
    """Positive control — otherwise the guard above is just "always partial" and proves nothing.

    Once cutover marks the Agent ``revisioned`` its revisions enter the subject universe, so the
    reason clears itself with no follow-up cleanup."""
    db_path, team, owner = await _team_with_one_agent(tmp_path, definition_mode="revisioned")

    view = await _tool_source_usage(db_path, team, owner)

    assert "team_agents_not_projectable" not in view.summary.reason_codes


@pytest.mark.asyncio
async def test_disabled_legacy_agents_do_not_block_it(tmp_path: Path) -> None:
    """Scoped to ENABLED Agents: a disabled one cannot be using anything, so it is not a gap."""
    db_path, team, owner = await _team_with_one_agent(tmp_path, definition_mode="legacy_mutable", status="disabled")

    view = await _tool_source_usage(db_path, team, owner)

    assert "team_agents_not_projectable" not in view.summary.reason_codes
