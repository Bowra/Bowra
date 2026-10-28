"""M3-B exact Agent admission, idempotency, and materialization contracts."""

from __future__ import annotations

import atexit
import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_execution_binding import (
    AgentExecutionBindingError,
    AgentExecutionBindingStore,
    ExactAgentBindingExpectation,
    TurnAgentBindingInput,
    project_system_agent_revision,
)
from bowhead_core.scheduled_agent_binding import (
    ScheduledAgentBinding,
    ScheduledAgentBindingError,
    ScheduledAgentBindingStore,
)
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentRunner, _agent_attribution_payload, _system_profile_snapshot
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.user_store import TeamSubagent, UserStore

_NOW = "2026-09-04T00:00:00+00:00"


def test_scheduled_binding_uses_shared_turn_entry_envelope() -> None:
    scheduled = ScheduledAgentBinding(
        job_id="sj_1",
        assignment_id="asa_1",
        agent_id="osa_1",
        agent_source_kind="team",
        revision_ref="arv_1",
        release_id="arl_1",
        capability_fingerprint="sha256:cap",
        dependency_fingerprint="sha256:dep",
        follow_mode="pinned",
        state="ready",
        confirmed_by="user_1",
        confirmed_at=_NOW,
        row_version=2,
    )

    envelope = TurnAgentBindingInput.from_scheduled(scheduled)

    assert envelope.binding_source == "schedule"
    assert envelope.agent_id == "osa_1"
    assert envelope.schedule_job_id == "sj_1"
    assert envelope.schedule_binding_row_version == 2
    with pytest.raises(ValueError, match="binding source is invalid"):
        replace(envelope, binding_source="other")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source kind is invalid"):
        replace(envelope, agent_source_kind="other")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="binding is incomplete"):
        replace(envelope, schedule_binding_row_version=True)


@dataclass(frozen=True, slots=True)
class _Fixture:
    db_path: Path
    users: UserStore
    team_id: str
    owner_id: str
    member_id: str
    workspace_id: str
    turn_id: str


_MIGRATED_CORE: list[Path] = []


def _migrated_core_template() -> Path:
    """Migrate one pristine core database per process; revisions are deterministic.

    Running every revision from an empty file cost about 8 seconds per test and
    dominated the reference suites that build on ``_setup``.
    """
    if not _MIGRATED_CORE:
        directory = Path(tempfile.mkdtemp(prefix="bowhead-core-head-"))
        atexit.register(shutil.rmtree, directory, True)
        assert upgrade(f"sqlite+aiosqlite:///{directory / 'bowhead.db'}", target="core") == ("core",)
        _MIGRATED_CORE.append(directory / "bowhead.db")
    return _MIGRATED_CORE[0]


async def _setup(tmp_path: Path) -> _Fixture:
    db_path = tmp_path / "bowhead.db"
    # Callers may pass a fresh subdirectory; the migration this copy replaced created it implicitly.
    tmp_path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_migrated_core_template()) as source, sqlite3.connect(db_path) as destination:
        source.backup(destination)
    # The deployment migrator resolves the canonical core filename from the
    # URL's directory, mirroring the real multi-database startup path (a no-op at head).
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    users = UserStore(db_path)
    sessions = SessionStore(db_path)
    await users.initialize()
    await sessions.initialize()
    team = await users.create_team(name="M3-B", max_seats=4)
    owner = await users.create_user(team_id=team.id, email="owner@example.com")
    member = await users.create_user(team_id=team.id, email="member@example.com")
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="M3-B Space",
        creator_user_id=member.id,
    )
    session = await sessions.ensure_session(
        session_id="s_m3b",
        workspace_id=workspace_id,
        user_id=member.id,
    )
    turn = await sessions.record_turn(
        turn_id="t_m3b",
        session_id=session.id,
        user_text="run exact agent",
    )
    return _Fixture(
        db_path=db_path,
        users=users,
        team_id=team.id,
        owner_id=owner.id,
        member_id=member.id,
        workspace_id=workspace_id,
        turn_id=turn.id,
    )


async def _create_team_agent(
    fixture: _Fixture,
    *,
    safety_content_hash: str | None = None,
) -> None:
    await fixture.users.create_team_subagent(
        TeamSubagent(
            id="osa_exact",
            team_id=fixture.team_id,
            name="exact-agent",
            display_name="Exact Agent",
            description="Safe exact execution",
            system_prompt="legacy prompt",
            tool_refs=["bi_query"],
            visibility="team_wide",
            created_by=fixture.owner_id,
            created_at=_NOW,
            updated_at=_NOW,
        ),
    )
    await _insert_release(
        fixture,
        sequence=1,
        prompt="revision one",
        safety_content_hash=safety_content_hash,
    )
    await AgentAssignmentStore(fixture.db_path).hire(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_exact",
        agent_source_kind="team",
        live_access=True,
    )


async def _create_legacy_team_agent(fixture: _Fixture) -> None:
    await fixture.users.create_team_subagent(
        TeamSubagent(
            id="osa_legacy",
            team_id=fixture.team_id,
            name="legacy-agent",
            display_name="Legacy Agent",
            description="Safe legacy execution",
            system_prompt="legacy prompt",
            tool_refs=["bi_query"],
            visibility="team_wide",
            created_by=fixture.owner_id,
            created_at=_NOW,
            updated_at=_NOW,
        ),
    )
    await AgentAssignmentStore(fixture.db_path).hire(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_legacy",
        agent_source_kind="team",
        live_access=True,
    )


async def _insert_release(
    fixture: _Fixture,
    *,
    sequence: int,
    prompt: str,
    safety_content_hash: str | None = None,
    capability_fingerprint: str | None = None,
    dependency_fingerprint: str | None = None,
    skill_grants: list[dict] | None = None,
    runtime_extensions: dict | None = None,
    definition_overrides: dict | None = None,
) -> tuple[str, str]:
    revision_id = f"arv_exact_{sequence}"
    release_id = f"arl_exact_{sequence}"
    safety_id = f"asr_exact_{sequence}"
    definition = {
        "max_iterations": 8,
        "model": "analyst",
        "output_schema": None,
        "skill_grants": skill_grants if skill_grants is not None else [],
        "system_prompt": prompt,
        "tool_refs": ["bi_query"],
    }
    if runtime_extensions is not None:
        definition["runtime_extensions"] = runtime_extensions
    if definition_overrides is not None:
        definition.update(definition_overrides)
    listing = {
        "description": f"Safe exact execution v{sequence}",
        "display_name": "Exact Agent",
        "name": "exact-agent",
    }
    definition_json = json.dumps(
        definition,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    definition_hash = f"sha256:{hashlib.sha256(definition_json.encode()).hexdigest()}"
    # Production shape: the safety report hashes {definition, listing}, NOT the
    # definition alone.  Seeding definition_hash here made the fixture agree with a
    # join condition no real writer could satisfy.
    content_json = json.dumps(
        {"definition": definition, "listing": listing},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    content_hash = f"sha256:{hashlib.sha256(content_json.encode()).hexdigest()}"
    capability_fingerprint = capability_fingerprint or f"sha256:cap{sequence}"
    dependency_fingerprint = dependency_fingerprint or f"sha256:dep{sequence}"
    async with aiosqlite.connect(fixture.db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
              definition_hash,dependency_fingerprint,capability_fingerprint,
              risk_compiler_version,risk_json,created_by,created_at
            ) VALUES (?,?,?,?, 'authored',?,?,?,?,?,'risk-v1','{}',?,?)
            """,
            (
                revision_id,
                fixture.team_id,
                "osa_exact",
                sequence,
                definition_json,
                json.dumps(listing, sort_keys=True),
                definition_hash,
                dependency_fingerprint,
                capability_fingerprint,
                fixture.owner_id,
                _NOW,
            ),
        )
        await db.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,status,
              deterministic_json,semantic_json,safe_summary_json,created_by,created_at
            ) VALUES (?,?,?,'revision',?,?,?,'safety-v1','none','pass','{}','{}','{}',?,?)
            """,
            (
                safety_id,
                fixture.team_id,
                "osa_exact",
                revision_id,
                safety_content_hash or content_hash,
                dependency_fingerprint,
                fixture.owner_id,
                _NOW,
            ),
        )
        if runtime_extensions is not None:
            from bowhead_core.agent_runtime_extension_store import retain_extension_policy_in_connection

            await retain_extension_policy_in_connection(
                db,
                team_id=fixture.team_id,
                agent_id="osa_exact",
                revision_ref=revision_id,
                definition=definition,
                audience_mode="teamwide",
                created_at=_NOW,
            )
        await db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,predecessor_release_id,
              audience_snapshot_json,audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_json,safety_report_id,activated_by,activated_at
            ) VALUES (?,?,?,?,?,?, ?,?,?,?,?,?,?,?)
            """,
            (
                release_id,
                fixture.team_id,
                "osa_exact",
                sequence,
                revision_id,
                f"arl_exact_{sequence - 1}" if sequence > 1 else None,
                json.dumps({"effective_mode": "teamwide"}) if runtime_extensions is not None else "{}",
                f"sha256:aud{sequence}",
                capability_fingerprint,
                dependency_fingerprint,
                "{}",
                safety_id,
                fixture.owner_id,
                _NOW,
            ),
        )
        await db.execute(
            """
            UPDATE team_subagent
            SET live_revision_id=?,live_release_id=?,release_sequence=?,
                definition_mode='revisioned',row_version=row_version+1,updated_at=?
            WHERE team_id=? AND id='osa_exact'
            """,
            (
                revision_id,
                release_id,
                sequence,
                _NOW,
                fixture.team_id,
            ),
        )
        await db.commit()
    return revision_id, release_id


@pytest.mark.asyncio
async def test_team_plugin_update_affects_new_execution_only(tmp_path: Path) -> None:
    fixture = await _setup(tmp_path)
    catalog = TeamPluginStore(str(fixture.db_path))
    plugin = await catalog.create(
        team_id=fixture.team_id,
        actor_user_id=fixture.owner_id,
        name="Research",
        description="Shared resources",
        tool_refs=["web_search"],
        skill_grants=[],
    )
    await fixture.users.create_team_subagent(
        TeamSubagent(
            id="osa_exact",
            team_id=fixture.team_id,
            name="exact-agent",
            display_name="Exact Agent",
            description="Shared capability",
            system_prompt="legacy prompt",
            tool_refs=["bi_query"],
            visibility="team_wide",
            created_by=fixture.owner_id,
            created_at=_NOW,
            updated_at=_NOW,
        ),
    )
    await _insert_release(
        fixture,
        sequence=1,
        prompt="revision one",
        definition_overrides={"capability_ids": [plugin["id"]]},
    )
    await AgentAssignmentStore(fixture.db_path).hire(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_exact",
        agent_source_kind="team",
        live_access=True,
    )
    binding_store = AgentExecutionBindingStore(fixture.db_path)
    first = await binding_store.admit_team_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="cap:first",
    )
    assert first.definition["tool_refs"] == ["bi_query", "web_search"]
    await catalog.update(
        team_id=fixture.team_id,
        plugin_id=plugin["id"],
        actor_user_id=fixture.owner_id,
        expected_row_version=plugin["row_version"],
        name="Research",
        description="Updated resources",
        tool_refs=["web_fetch"],
        skill_grants=[],
    )
    replay = await binding_store.admit_team_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="cap:first",
    )
    second = await binding_store.admit_team_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="cap:second",
    )
    assert replay.definition["tool_refs"] == ["bi_query", "web_search"]
    assert second.definition["tool_refs"] == ["bi_query", "web_fetch"]
    assert second.definition["resolved_capabilities"] == [{"id": plugin["id"], "revision": 2}]


@pytest.mark.asyncio
async def test_team_dispatch_rechecks_access_and_reuses_exact_bound_revision(
    tmp_path: Path,
) -> None:
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    store = AgentExecutionBindingStore(fixture.db_path)

    first = await store.admit_team_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="call:one",
    )
    assert first.revision_ref == "arv_exact_1"
    assert first.definition["system_prompt"] == "revision one"
    async with aiosqlite.connect(fixture.db_path) as db:
        async with db.execute(
            """
            SELECT id FROM agent_assignment
            WHERE team_id=? AND user_id=? AND agent_id='osa_exact'
            """,
            (fixture.team_id, fixture.member_id),
        ) as cursor:
            assignment = await cursor.fetchone()
        assert assignment is not None
        await db.execute("BEGIN IMMEDIATE")
        await store.insert_root_binding_in_connection(
            db,
            binding=first,
            user_id=fixture.member_id,
            assignment_id=str(assignment[0]),
            binding_source="detail",
        )
        await db.commit()
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute(
            "SELECT requested_agent_id,revision_ref FROM turn_agent_binding",
        ).fetchone() == ("osa_exact", "arv_exact_1")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE turn_agent_execution SET ended_at=? WHERE id=?",
                (_NOW, first.execution_id),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="turn_agent_binding is immutable",
        ):
            db.execute(
                "UPDATE turn_agent_binding SET revision_ref='arv_exact_2'",
            )

    await _insert_release(fixture, sequence=2, prompt="revision two")
    replay = await store.admit_team_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="call:one",
    )
    current = await store.admit_team_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="call:two",
    )
    assert replay.execution_id == first.execution_id
    assert replay.revision_ref == "arv_exact_1"
    assert replay.definition["system_prompt"] == "revision one"
    assert current.revision_ref == "arv_exact_2"
    assert current.definition["system_prompt"] == "revision two"

    detail = await store.read_turn_binding_detail(
        team_id=fixture.team_id,
        turn_id=fixture.turn_id,
    )
    assert detail["root_binding"] == {
        "agent_id": "osa_exact",
        "agent_source_kind": "team",
        "agent_revision_id": "arv_exact_1",
        "agent_release_id": "arl_exact_1",
        "binding_source": "detail",
        "capability_fingerprint": "sha256:cap1",
        "created_at": detail["root_binding"]["created_at"],
    }
    assert [row["agent_revision_id"] for row in detail["executions"]] == [
        "arv_exact_1",
        "arv_exact_2",
    ]
    assert detail["executions"][0]["display_name_snapshot"] == "Exact Agent"
    assert "definition_json" not in detail["executions"][0]
    assert "policy_fingerprint" not in detail["executions"][0]

    attribution = _agent_attribution_payload(first)
    assert attribution == {
        "agent_id": "osa_exact",
        "agent_revision_id": "arv_exact_1",
        "agent_release_id": "arl_exact_1",
        "agent_execution_id": first.execution_id,
        "display_name_snapshot": "Exact Agent",
    }

    await store.mark_running(first.execution_id)
    await store.mark_terminal(first.execution_id, status="completed")
    with pytest.raises(
        AgentExecutionBindingError,
        match="agent_execution_already_terminal",
    ):
        await store.admit_team_execution(
            team_id=fixture.team_id,
            user_id=fixture.member_id,
            turn_id=fixture.turn_id,
            profile_name="exact-agent",
            dispatch_key="call:one",
        )

    async with aiosqlite.connect(fixture.db_path) as db:
        await db.execute(
            """
            UPDATE agent_assignment SET personal_state='disabled',row_version=row_version+1
            WHERE team_id=? AND user_id=? AND agent_id='osa_exact'
            """,
            (fixture.team_id, fixture.member_id),
        )
        await db.commit()
    with pytest.raises(AgentExecutionBindingError, match="agent_assignment_inactive"):
        await store.admit_team_execution(
            team_id=fixture.team_id,
            user_id=fixture.member_id,
            turn_id=fixture.turn_id,
            profile_name="exact-agent",
            dispatch_key="call:blocked",
        )
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM turn_agent_execution WHERE dispatch_key='call:blocked'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_partial_import_requires_current_revision_before_dispatch(
    tmp_path: Path,
) -> None:
    fixture = await _setup(tmp_path)
    await _create_legacy_team_agent(fixture)
    runner = SubagentRunner(
        router=None,  # type: ignore[arg-type]
        tool_registry=None,  # type: ignore[arg-type]
        team_subagent_store=fixture.users,
    )
    ctx = SimpleNamespace(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        granted_team_subagent_ids=None,
        builtin_subagent_overrides=None,
    )

    # A partial baseline import cannot execute mutable legacy instructions,
    # even before the deployment release marker is installed.
    with pytest.raises(AgentExecutionBindingError, match="agent_not_runnable"):
        await runner.admit_execution_binding(
            profile_name="legacy-agent",
            parent_turn_id=fixture.turn_id,
            parent_ctx=ctx,
            dispatch_key="call:partial-import",
            agent_id="osa_legacy",
        )

    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (0,)


@pytest.mark.asyncio
async def test_legacy_release_compatibility_rejects_team_system_name_collision(
    tmp_path: Path,
) -> None:
    fixture = await _setup(tmp_path)
    await _create_legacy_team_agent(fixture)
    await AgentAssignmentStore(fixture.db_path).ensure_marketplace_cutover(
        source_build="test-legacy-release-name-collision",
    )
    async with aiosqlite.connect(fixture.db_path) as db:
        await db.execute(
            "UPDATE team_subagent SET name='compute' WHERE team_id=? AND id='osa_legacy'",
            (fixture.team_id,),
        )
        await db.commit()
    runner = SubagentRunner(
        router=None,  # type: ignore[arg-type]
        tool_registry=None,  # type: ignore[arg-type]
        team_subagent_store=fixture.users,
    )
    ctx = SimpleNamespace(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        granted_team_subagent_ids=None,
        builtin_subagent_overrides=None,
    )

    with pytest.raises(AgentExecutionBindingError, match="agent_not_runnable"):
        await runner.admit_execution_binding(
            profile_name="compute",
            parent_turn_id=fixture.turn_id,
            parent_ctx=ctx,
            dispatch_key="call:legacy-system-name-collision",
            agent_id="osa_legacy",
        )

    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (0,)


@pytest.mark.asyncio
async def test_scheduled_agent_uses_latest_plugin_on_new_execution(tmp_path: Path) -> None:
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    catalog = TeamPluginStore(str(fixture.db_path))
    plugin = await catalog.create(
        team_id=fixture.team_id,
        actor_user_id=fixture.owner_id,
        name="Scheduled research",
        description="Shared resources",
        tool_refs=["web_search"],
        skill_grants=[],
    )
    await _insert_release(
        fixture,
        sequence=2,
        prompt="revision two",
        definition_overrides={"capability_ids": [plugin["id"]]},
    )
    bindings = ScheduledAgentBindingStore(fixture.db_path)
    seed = await bindings.prepare_current_team_binding(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_exact",
        confirmed_by=fixture.member_id,
    )
    job = await ScheduledJobStore(str(fixture.db_path)).create(
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        name="Capability schedule",
        cron="0 9 * * *",
        query="run research",
        recipe_ref=json.dumps({"form": "team_subagent", "id_or_name": "osa_exact"}),
        agent_binding=seed,
    )
    pin = await bindings.validate_for_fire(job_id=job.id, team_id=fixture.team_id, user_id=fixture.member_id)
    execution_store = AgentExecutionBindingStore(fixture.db_path)

    async def admit(key: str):
        return await execution_store.admit_scheduled_team_execution(
            team_id=fixture.team_id,
            user_id=fixture.member_id,
            turn_id=fixture.turn_id,
            profile_name="exact-agent",
            dispatch_key=key,
            job_id=job.id,
            binding_row_version=pin.row_version,
        )

    first = await admit("schedule:capability:first")
    assert "web_search" in first.definition["tool_refs"]
    await catalog.update(
        team_id=fixture.team_id,
        plugin_id=plugin["id"],
        actor_user_id=fixture.owner_id,
        expected_row_version=plugin["row_version"],
        name="Scheduled research",
        description="Updated resources",
        tool_refs=["web_fetch"],
        skill_grants=[],
    )
    replay = await admit("schedule:capability:first")
    second = await admit("schedule:capability:second")
    assert "web_search" in replay.definition["tool_refs"]
    assert "web_fetch" in second.definition["tool_refs"]
    assert "web_search" not in second.definition["tool_refs"]


@pytest.mark.asyncio
async def test_schedule_pin_pauses_on_fingerprint_drift_and_reconfirms_latest(
    tmp_path: Path,
) -> None:
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    bindings = ScheduledAgentBindingStore(fixture.db_path)
    seed = await bindings.prepare_current_team_binding(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_exact",
        confirmed_by=fixture.member_id,
    )
    jobs = ScheduledJobStore(str(fixture.db_path))
    job = await jobs.create(
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        name="Pinned Agent",
        cron="0 9 * * *",
        query="run exact agent",
        recipe_ref=json.dumps(
            {"form": "team_subagent", "id_or_name": "osa_exact"},
        ),
        agent_binding=seed,
    )
    first_pin = await bindings.validate_for_fire(
        job_id=job.id,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
    )
    assert await bindings.read_many([job.id, "missing", job.id]) == {
        job.id: first_pin,
    }
    first = await AgentExecutionBindingStore(
        fixture.db_path,
    ).admit_scheduled_team_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="call:schedule-v1",
        job_id=job.id,
        binding_row_version=first_pin.row_version,
    )
    assert first.revision_ref == "arv_exact_1"
    assert first.definition["system_prompt"] == "revision one"

    await _insert_release(fixture, sequence=2, prompt="revision two")
    with pytest.raises(
        ScheduledAgentBindingError,
        match="scheduled_recipe_changed",
    ):
        await bindings.validate_for_fire(
            job_id=job.id,
            team_id=fixture.team_id,
            user_id=fixture.member_id,
        )
    drifted = await bindings.read(job.id)
    paused = await jobs.get(job.id)
    assert drifted is not None and drifted.state == "needs_reconfirm"
    assert paused is not None and paused.status == "paused"
    assert paused.pause_reason == "scheduled_recipe_changed"

    new_seed = await bindings.prepare_current_team_binding(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_exact",
        confirmed_by=fixture.member_id,
    )
    assert await jobs.patch(
        job.id,
        status="active",
        agent_binding=new_seed,
    )
    second_pin = await bindings.validate_for_fire(
        job_id=job.id,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
    )
    assert second_pin.revision_ref == "arv_exact_2"
    # One increment records the drift pause; the next records reconfirmation.
    assert second_pin.row_version == first_pin.row_version + 2
    second = await AgentExecutionBindingStore(
        fixture.db_path,
    ).admit_scheduled_team_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="call:schedule-v2",
        job_id=job.id,
        binding_row_version=second_pin.row_version,
    )
    assert second.revision_ref == "arv_exact_2"
    assert second.definition["system_prompt"] == "revision two"


@pytest.mark.asyncio
async def test_schedule_create_rechecks_audience_in_job_write_transaction(
    tmp_path: Path,
) -> None:
    """A stale prepared seed cannot outlive an Audience revocation."""
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    bindings = ScheduledAgentBindingStore(fixture.db_path)
    seed = await bindings.prepare_current_team_binding(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_exact",
        confirmed_by=fixture.member_id,
    )
    async with aiosqlite.connect(fixture.db_path) as db:
        await db.execute(
            """
            UPDATE team_subagent
            SET visibility='creator_only',row_version=row_version+1
            WHERE team_id=? AND id='osa_exact'
            """,
            (fixture.team_id,),
        )
        await db.commit()

    jobs = ScheduledJobStore(str(fixture.db_path))
    with pytest.raises(
        ScheduledAgentBindingError,
        match="scheduled_recipe_lost",
    ):
        await jobs.create(
            user_id=fixture.member_id,
            workspace_id=fixture.workspace_id,
            name="Stale Audience",
            cron="0 9 * * *",
            query="must not run",
            recipe_ref=json.dumps(
                {"form": "team_subagent", "id_or_name": "osa_exact"},
            ),
            agent_binding=seed,
        )

    assert await jobs.list_for_user(fixture.member_id) == []


@pytest.mark.asyncio
async def test_schedule_pin_requires_reconfirm_for_prompt_only_release(
    tmp_path: Path,
) -> None:
    """A new live revision is user-visible drift even when dependencies match."""

    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    bindings = ScheduledAgentBindingStore(fixture.db_path)
    seed = await bindings.prepare_current_team_binding(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_exact",
        confirmed_by=fixture.member_id,
    )
    jobs = ScheduledJobStore(str(fixture.db_path))
    job = await jobs.create(
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        name="Pinned prompt",
        cron="0 9 * * *",
        query="run exact agent",
        recipe_ref=json.dumps(
            {"form": "team_subagent", "id_or_name": "osa_exact"},
        ),
        agent_binding=seed,
    )

    await _insert_release(
        fixture,
        sequence=2,
        prompt="prompt changed without dependency drift",
        capability_fingerprint=seed.capability_fingerprint,
        dependency_fingerprint=seed.dependency_fingerprint,
    )

    with pytest.raises(
        ScheduledAgentBindingError,
        match="scheduled_recipe_changed",
    ):
        await bindings.validate_for_fire(
            job_id=job.id,
            team_id=fixture.team_id,
            user_id=fixture.member_id,
        )
    binding = await bindings.read(job.id)
    paused = await jobs.get(job.id)
    assert binding is not None and binding.state == "needs_reconfirm"
    assert paused is not None and paused.pause_reason == "scheduled_recipe_changed"


@pytest.mark.asyncio
async def test_schedule_pin_can_pause_after_owner_becomes_inactive(
    tmp_path: Path,
) -> None:
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    bindings = ScheduledAgentBindingStore(fixture.db_path)
    seed = await bindings.prepare_current_team_binding(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_exact",
        confirmed_by=fixture.member_id,
    )
    jobs = ScheduledJobStore(str(fixture.db_path))
    job = await jobs.create(
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        name="Owner access revoked",
        cron="0 9 * * *",
        query="q",
        recipe_ref=json.dumps(
            {"form": "team_subagent", "id_or_name": "osa_exact"},
        ),
        agent_binding=seed,
    )
    async with aiosqlite.connect(fixture.db_path) as db:
        await db.execute(
            "UPDATE users SET status='disabled' WHERE id=?",
            (fixture.member_id,),
        )
        await db.commit()

    with pytest.raises(ScheduledAgentBindingError, match="scheduled_recipe_lost"):
        await bindings.validate_for_fire(
            job_id=job.id,
            team_id=fixture.team_id,
            user_id=fixture.member_id,
        )

    paused_binding = await bindings.read(job.id)
    paused_job = await jobs.get(job.id)
    assert paused_binding is not None and paused_binding.state == "paused"
    assert paused_job is not None and paused_job.status == "paused"
    assert paused_job.pause_reason == "scheduled_recipe_lost"
    async with aiosqlite.connect(fixture.db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                """
                UPDATE scheduled_job_agent_binding
                SET agent_id='osa_other',row_version=row_version+1
                WHERE job_id=?
                """,
                (job.id,),
            )


@pytest.mark.asyncio
async def test_legacy_schedule_binding_migrates_only_stable_agent_id(
    tmp_path: Path,
) -> None:
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    jobs = ScheduledJobStore(str(fixture.db_path))
    bindings = ScheduledAgentBindingStore(fixture.db_path)
    exact_job = await jobs.create(
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        name="Legacy exact",
        cron="0 9 * * *",
        query="q",
        recipe_ref=json.dumps(
            {"form": "team_subagent", "id_or_name": "osa_exact"},
        ),
    )
    migrated = await bindings.bind_legacy_team_job(
        job_id=exact_job.id,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id="osa_exact",
    )
    assert migrated.agent_id == "osa_exact"
    assert migrated.revision_ref == "arv_exact_1"

    name_job = await jobs.create(
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        name="Legacy name",
        cron="0 10 * * *",
        query="q",
        recipe_ref=json.dumps(
            {"form": "team_subagent", "id_or_name": "exact-agent"},
        ),
    )
    with pytest.raises(ScheduledAgentBindingError, match="scheduled_recipe_lost"):
        await bindings.bind_legacy_team_job(
            job_id=name_job.id,
            team_id=fixture.team_id,
            user_id=fixture.member_id,
            agent_id="exact-agent",
        )
    assert await bindings.read(name_job.id) is None


@pytest.mark.asyncio
async def test_team_dispatch_rejects_safety_report_for_different_content(
    tmp_path: Path,
) -> None:
    fixture = await _setup(tmp_path)
    await _create_team_agent(
        fixture,
        safety_content_hash="sha256:different-content",
    )

    with pytest.raises(
        AgentExecutionBindingError,
        match="agent_live_release_inconsistent",
    ):
        await AgentExecutionBindingStore(fixture.db_path).admit_team_execution(
            team_id=fixture.team_id,
            user_id=fixture.member_id,
            turn_id=fixture.turn_id,
            profile_name="exact-agent",
            dispatch_key="call:safety-mismatch",
        )


@pytest.mark.asyncio
async def test_stable_team_agent_id_cannot_resolve_to_same_named_system_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    runner = SubagentRunner(
        router=None,  # type: ignore[arg-type]
        tool_registry=None,  # type: ignore[arg-type]
        team_subagent_store=fixture.users,
    )
    ctx = SimpleNamespace(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        granted_team_subagent_ids=None,
        builtin_subagent_overrides=None,
    )

    with pytest.raises(AgentExecutionBindingError, match="agent_not_runnable"):
        await runner.admit_execution_binding(
            profile_name="compute",
            parent_turn_id=fixture.turn_id,
            parent_ctx=ctx,
            dispatch_key="call:kind-conflict",
            agent_id="osa_exact",
        )


@pytest.mark.asyncio
async def test_team_source_lookup_failure_does_not_fall_through_unbound(
    tmp_path: Path,
) -> None:
    fixture = await _setup(tmp_path)

    class _UnavailableTeamStore:
        db_path = fixture.db_path

        async def get_team_subagent_by_name(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise sqlite3.OperationalError("database unavailable")

    runner = SubagentRunner(
        router=None,  # type: ignore[arg-type]
        tool_registry=None,  # type: ignore[arg-type]
        team_subagent_store=_UnavailableTeamStore(),
    )
    ctx = SimpleNamespace(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        granted_team_subagent_ids=None,
        builtin_subagent_overrides=None,
    )

    with pytest.raises(
        AgentExecutionBindingError,
        match="agent_admission_unavailable",
    ):
        await runner.admit_execution_binding(
            profile_name="legacy-custom-name",
            parent_turn_id=fixture.turn_id,
            parent_ctx=ctx,
            dispatch_key="call:lookup-failure",
        )


@pytest.mark.asyncio
async def test_runner_system_binding_snapshots_effective_profile_and_tracks_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.subagent import PROFILES

    monkeypatch.setitem(PROFILES, "compute", manifest_to_profile(AgentRegistry.bootstrap().get("compute")))
    fixture = await _setup(tmp_path)
    runner = SubagentRunner(
        router=None,  # type: ignore[arg-type]
        tool_registry=None,  # type: ignore[arg-type]
        team_subagent_store=fixture.users,
    )
    ctx = SimpleNamespace(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        granted_team_subagent_ids=None,
        builtin_subagent_overrides=None,
    )
    binding = await runner.admit_execution_binding(
        profile_name="compute",
        parent_turn_id=fixture.turn_id,
        parent_ctx=ctx,
        dispatch_key="call:system",
    )
    assert binding is not None
    assert binding.agent_id == "system:compute"
    assert str(binding.definition["behavior_catalog_fingerprint"]).startswith("sha256:")
    assert binding.behavior_descriptor is not None
    profile = await runner.resolve_by_agent_revision(binding, ctx)
    assert profile.name == "compute"
    assert profile.system_prompt == binding.definition["system_prompt"]
    assert profile.behavior_descriptor is None

    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    exact_profile = await runner.resolve_by_agent_revision(binding, ctx)
    assert exact_profile.behavior_descriptor == binding.behavior_descriptor
    with pytest.raises(
        AgentExecutionBindingError,
        match="agent_behavior_descriptor_unavailable",
    ):
        await runner.resolve_by_agent_revision(
            replace(binding, behavior_descriptor=None),
            ctx,
        )

    async with aiosqlite.connect(fixture.db_path) as db:
        await db.execute(
            """
            INSERT INTO team_builtin_subagent_override(
              team_id,name,enabled,row_version,created_at,updated_at
            ) VALUES (?,'compute',0,1,?,?)
            """,
            (fixture.team_id, _NOW, _NOW),
        )
        await db.commit()
    with pytest.raises(AgentExecutionBindingError, match="agent_not_runnable"):
        await runner.admit_execution_binding(
            profile_name="compute",
            parent_turn_id=fixture.turn_id,
            parent_ctx=ctx,
            dispatch_key="call:system-disabled",
        )

    store = AgentExecutionBindingStore(fixture.db_path)
    await store.mark_running(binding.execution_id)
    await store.mark_running(binding.execution_id)
    await store.mark_terminal(binding.execution_id, status="completed")
    await store.mark_terminal(binding.execution_id, status="completed")
    with pytest.raises(
        AgentExecutionBindingError,
        match="agent_execution_transition_conflict",
    ):
        await store.mark_running(binding.execution_id)

    with sqlite3.connect(fixture.db_path) as db:
        row = db.execute(
            "SELECT status,started_at,ended_at FROM turn_agent_execution WHERE id=?",
            (binding.execution_id,),
        ).fetchone()
        assert row is not None and row[0] == "completed"
        assert row[1] is not None and row[2] is not None
        behavior = db.execute(
            "SELECT descriptor_json FROM system_agent_revision_behavior WHERE revision_id=?",
            (binding.revision_ref,),
        ).fetchone()
        assert behavior is not None
        # Actual startup pins the primary data Capability. Without a reviewed
        # implementation, its descriptor must not claim a proven role.
        descriptor = json.loads(behavior[0])
        assert descriptor["verification_state"] == "blocked"
        assert "primary_capability_unproven:cap_data_query@1" in descriptor["reason_codes"]
        with pytest.raises(
            sqlite3.IntegrityError,
            match="system_agent_effective_revision is immutable",
        ):
            db.execute(
                "UPDATE system_agent_effective_revision SET definition_json='{}' WHERE id=?",
                (binding.revision_ref,),
            )


@pytest.mark.asyncio
async def test_behavior_sidecar_failure_rolls_back_new_system_revision(tmp_path: Path) -> None:
    fixture = await _setup(tmp_path)
    with sqlite3.connect(fixture.db_path) as db:
        db.execute(
            """
            CREATE TRIGGER fail_system_behavior BEFORE INSERT ON system_agent_revision_behavior
            BEGIN SELECT RAISE(ABORT, 'forced behavior failure'); END
            """,
        )
        db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced behavior failure"):
        await AgentExecutionBindingStore(fixture.db_path).admit_system_execution(
            team_id=fixture.team_id,
            user_id=fixture.member_id,
            turn_id=fixture.turn_id,
            profile_name="compute",
            dispatch_key="call:system-behavior-failure",
            definition={"llm_agent_type": "analyst", "name": "compute", "schema_version": 1},
            manifest_fingerprint="sha256:" + "1" * 64,
            override_version=None,
            system_required=True,
            enforce_current_override=False,
        )

    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM system_agent_effective_revision").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM system_agent_revision_behavior").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (0,)


@pytest.mark.asyncio
async def test_existing_system_revision_does_not_load_shadow_materializer(tmp_path: Path) -> None:
    fixture = await _setup(tmp_path)
    definition = {"llm_agent_type": "analyst", "name": "compute", "schema_version": 1}
    manifest_fingerprint = "sha256:" + "1" * 64
    first = await AgentExecutionBindingStore(fixture.db_path).admit_system_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="compute",
        dispatch_key="call:system-first",
        definition=definition,
        manifest_fingerprint=manifest_fingerprint,
        override_version=None,
        system_required=True,
        enforce_current_override=False,
    )

    class _UnexpectedMaterializer:
        def compile_system_revision(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("existing revision must not compile a behavior sidecar")

    replay = await AgentExecutionBindingStore(
        fixture.db_path,
        behavior_materializer=_UnexpectedMaterializer(),  # type: ignore[arg-type]
    ).admit_system_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="compute",
        dispatch_key="call:system-replay",
        definition=definition,
        manifest_fingerprint=manifest_fingerprint,
        override_version=None,
        system_required=True,
        enforce_current_override=False,
    )

    assert replay.revision_ref == first.revision_ref


@pytest.mark.asyncio
async def test_system_candidate_identity_is_rechecked_before_execution_insert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.registry import AgentRegistry

    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    fixture = await _setup(tmp_path)
    profile = manifest_to_profile(
        AgentRegistry.bootstrap().get("marketing-analyst"),
    )
    definition = _system_profile_snapshot(profile)
    projected = project_system_agent_revision(
        team_id=fixture.team_id,
        profile_name=profile.name,
        definition=definition,
    )
    assert projected.behavior_descriptor.verification_state == "reviewed"
    assert projected.behavior_descriptor.eligible_lead_role_ids == ("image",)
    expected = ExactAgentBindingExpectation(
        agent_id=projected.agent_id,
        assignment_id=None,
        revision_ref=projected.revision_ref,
        release_id=None,
        behavior_fingerprint=projected.behavior_descriptor.behavior_fingerprint,
    )
    store = AgentExecutionBindingStore(fixture.db_path)
    binding = await store.admit_system_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name=profile.name,
        dispatch_key="call:image-candidate",
        definition=definition,
        manifest_fingerprint="sha256:" + "1" * 64,
        override_version=None,
        system_required=True,
        enforce_current_override=False,
        expected=expected,
    )
    assert binding.revision_ref == projected.revision_ref
    assert binding.behavior_descriptor == projected.behavior_descriptor

    changed_definition = {**definition, "description": "changed after roster"}
    with pytest.raises(AgentExecutionBindingError, match="agent_candidate_stale"):
        await store.admit_system_execution(
            team_id=fixture.team_id,
            user_id=fixture.member_id,
            turn_id=fixture.turn_id,
            profile_name=profile.name,
            dispatch_key="call:stale-image-candidate",
            definition=changed_definition,
            manifest_fingerprint="sha256:" + "1" * 64,
            override_version=None,
            system_required=True,
            enforce_current_override=False,
            expected=expected,
        )
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM turn_agent_execution WHERE dispatch_key='call:stale-image-candidate'",
        ).fetchone() == (0,)


def _sqlite_row(values: dict[str, object]) -> sqlite3.Row:
    """A real sqlite Row (its `in` compares values, not column names)."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    columns = ", ".join(f"? AS {name}" for name in values)
    return connection.execute(f"SELECT {columns}", tuple(values.values())).fetchone()  # noqa: S608


@pytest.mark.parametrize("with_resolved_column", [True, False])
def test_team_binding_prefers_resolved_definition_and_tolerates_pre_0123_rows(with_resolved_column: bool) -> None:
    """0123 后读 resolved_definition_json；0123 前的行没有这列时退回快照定义，不抛 IndexError。"""
    from bowhead_core.agent_execution_binding import _binding_from_rows

    execution: dict[str, object] = {
        "id": "axe_1",
        "team_id": "team_1",
        "turn_id": "t_1",
        "agent_id": "sa_1",
        "agent_source_kind": "team",
        "revision_ref": "rev_1",
        "release_id": None,
        "runtime_profile_name": "sa_1",
        "capability_fingerprint": "cap",
        "policy_fingerprint": "pol",
        "status": "admitted",
    }
    if with_resolved_column:
        execution["resolved_definition_json"] = json.dumps({"system_prompt": "resolved"})
    snapshot = _sqlite_row(
        {
            "definition_json": json.dumps({"system_prompt": "snapshot"}),
            "listing_json": json.dumps({"display_name": "Agent"}),
        }
    )

    binding = _binding_from_rows(_sqlite_row(execution), snapshot)

    assert binding.definition["system_prompt"] == ("resolved" if with_resolved_column else "snapshot")
