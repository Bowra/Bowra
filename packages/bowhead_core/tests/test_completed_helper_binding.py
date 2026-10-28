"""Read-only reauthorization of an original completed ordinary Team helper."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
import pytest_asyncio
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_execution_binding import AgentExecutionBindingError, AgentExecutionBindingStore, ExactAgentExecutionBinding
from bowhead_core.agent_revision_store import AgentAudience, AgentRevisionStore
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.session_store import SessionStore
from bowhead_core.team_plugin_store import TeamPluginStore
from legacy_agent_release_fixture import seed_legacy_publication
from test_agent_execution_binding import _create_team_agent, _setup
from test_agent_revision_store import _Validator

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def completed_helper(tmp_path: Path, request: pytest.FixtureRequest) -> SimpleNamespace:
    fixture = await _setup(tmp_path)
    if getattr(request, "param", False):
        fixture = replace(fixture, owner_id=fixture.member_id)
    await _create_team_agent(fixture)
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=? WHERE singleton=1", (datetime.now(UTC).isoformat(),))
    store = AgentExecutionBindingStore(fixture.db_path)
    binding = await store.admit_team_execution(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="call:inspect-template",
    )
    await store.mark_running(binding.execution_id)
    await store.mark_terminal(binding.execution_id, status="completed")
    scope = ReferenceRequestScope(team_id=fixture.team_id, user_id=fixture.member_id, workspace_id=fixture.workspace_id, session_id="s_m3b")
    return SimpleNamespace(fixture=fixture, store=store, binding=binding, scope=scope)


async def read(case: SimpleNamespace, **changes: object) -> ExactAgentExecutionBinding:
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN")
        return await case.store.read_completed_helper_in_connection(
            db,
            **{
                "scope": case.scope,
                "turn_id": case.fixture.turn_id,
                "execution_id": case.binding.execution_id,
                "dispatch_key": "call:inspect-template",
                **changes,
            },
        )


async def publish_next(case: SimpleNamespace, **definition_updates: object) -> None:
    revisions = AgentRevisionStore(str(case.fixture.db_path), validator=_Validator(token="completed-helper-next"))
    working = await revisions.save_working_copy(
        team_id=case.scope.team_id,
        agent_id=case.binding.agent_id,
        actor_user_id=case.fixture.owner_id,
        definition={**case.binding.definition, "system_prompt": "revision two", **definition_updates},
        listing=case.binding.listing,
        audience=AgentAudience("private"),
        expected_row_version=None,
        expected_base_release_id=case.binding.release_id,
    )
    result = await seed_legacy_publication(
        revisions, team_id=case.scope.team_id, agent_id=case.binding.agent_id, actor_user_id=case.fixture.owner_id, working_copy_id=working.id
    )
    assert result.state == "activated" and result.release_id != case.binding.release_id


@pytest.mark.parametrize("turn_status", ["running", "clarification_pending", "failed"])
async def test_completed_helper_preserves_exact_binding_without_admitting_execution(completed_helper: SimpleNamespace, turn_status: str) -> None:
    case = completed_helper
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE turns SET status=? WHERE id=?", (turn_status, case.fixture.turn_id))
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN")
        await db.execute("PRAGMA query_only=1")
        row_factory = db.row_factory
        changes = db.total_changes
        actual = await case.store.read_completed_helper_in_connection(
            db, scope=case.scope, turn_id=case.fixture.turn_id, execution_id=case.binding.execution_id, dispatch_key="call:inspect-template"
        )
        assert db.in_transaction and db.row_factory is row_factory and db.total_changes == changes
    assert actual == replace(case.binding, status="completed")
    actual.definition["system_prompt"] = "local mutation"
    assert (await read(case)).definition["system_prompt"] == "revision one"
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM turn_agent_binding").fetchone() == (0,)


@pytest.mark.parametrize("field", ["team_id", "user_id", "workspace_id", "session_id"])
async def test_completed_helper_rejects_another_scope(completed_helper: SimpleNamespace, field: str) -> None:
    case = completed_helper
    with pytest.raises(AgentExecutionBindingError):
        await read(case, scope=case.scope.model_copy(update={field: "another_scope"}))


@pytest.mark.parametrize("session_id", [None, "s_another_owned_session"])
async def test_completed_helper_requires_the_original_owned_session(completed_helper: SimpleNamespace, session_id: str | None) -> None:
    case = completed_helper
    if session_id is not None:
        await SessionStore(case.fixture.db_path).ensure_session(
            session_id=session_id, workspace_id=case.scope.workspace_id, user_id=case.scope.user_id
        )
    with pytest.raises(AgentExecutionBindingError):
        await read(case, scope=case.scope.model_copy(update={"session_id": session_id}))


@pytest.mark.parametrize(
    "change",
    [
        "user",
        "team",
        "space",
        "team_member",
        "space_member",
        "session_deleted",
        "assignment_disabled",
        "assignment_revoked",
        "access_deny",
        "agent_disabled",
        "agent_policy",
        "assignment_policy",
        "release_cutover",
    ],
)
async def test_completed_helper_rechecks_current_scope_and_policy(completed_helper: SimpleNamespace, change: str) -> None:
    case = completed_helper
    statements = {
        "user": ("UPDATE users SET status='disabled' WHERE id=?", case.scope.user_id),
        "team": ("UPDATE teams SET status='disabled' WHERE id=?", case.scope.team_id),
        "space": ("UPDATE workspaces SET status='archived' WHERE id=?", case.scope.workspace_id),
        "team_member": ("UPDATE team_members SET status='removed' WHERE user_id=?", case.scope.user_id),
        "space_member": ("UPDATE workspace_members SET status='removed' WHERE user_id=?", case.scope.user_id),
        "session_deleted": ("UPDATE sessions SET deleted_at='now' WHERE id=?", case.scope.session_id),
        "assignment_disabled": ("UPDATE agent_assignment SET personal_state='disabled' WHERE user_id=?", case.scope.user_id),
        "assignment_revoked": ("UPDATE agent_assignment SET access_state='revoked' WHERE user_id=?", case.scope.user_id),
        "agent_disabled": ("UPDATE team_subagent SET status='disabled' WHERE id=?", case.binding.agent_id),
        "agent_policy": ("UPDATE team_subagent SET row_version=row_version+1 WHERE id=?", case.binding.agent_id),
        "assignment_policy": ("UPDATE agent_assignment SET row_version=row_version+1 WHERE user_id=?", case.scope.user_id),
        "release_cutover": ("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=NULL WHERE singleton=?", 1),
    }
    with sqlite3.connect(case.fixture.db_path) as db:
        if change == "access_deny":
            db.execute(
                "INSERT INTO resource_grants(id,team_id,resource_kind,resource_id,principal_type,principal_id,permission,effect,created_at,updated_at) "
                "VALUES ('rg_completed_helper_deny',?,'team_subagent',?,'user',?,'use','deny','2026-10-05','2026-10-05')",
                (case.scope.team_id, case.binding.agent_id, case.scope.user_id),
            )
        else:
            sql, parameter = statements[change]
            db.execute(sql, (parameter,))
    with pytest.raises(AgentExecutionBindingError):
        await read(case)


async def test_completed_helper_rejects_actual_rehire_of_the_same_assignment(completed_helper: SimpleNamespace) -> None:
    case = completed_helper
    with sqlite3.connect(case.fixture.db_path) as db:
        original_id = db.execute("SELECT id FROM agent_assignment WHERE user_id=?", (case.scope.user_id,)).fetchone()[0]
        db.execute("UPDATE agent_assignment SET personal_state='removed' WHERE id=?", (original_id,))
    await AgentAssignmentStore(case.fixture.db_path).hire(
        team_id=case.scope.team_id, user_id=case.scope.user_id, agent_id=case.binding.agent_id, agent_source_kind="team", live_access=True
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT id FROM agent_assignment WHERE user_id=?", (case.scope.user_id,)).fetchone() == (original_id,)
    with pytest.raises(AgentExecutionBindingError, match="agent_assignment_cycle_changed"):
        await read(case)


@pytest.mark.parametrize("cycle", ["invalid", "2026-01-01T00:00:00", "future"])
async def test_completed_helper_rejects_unproven_assignment_cycles(completed_helper: SimpleNamespace, cycle: str) -> None:
    case = completed_helper
    if cycle == "future":
        cycle = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE agent_assignment SET current_cycle_started_at=? WHERE user_id=?", (cycle, case.scope.user_id))
    with pytest.raises(AgentExecutionBindingError, match="agent_assignment_cycle_changed"):
        await read(case)


@pytest.mark.parametrize("completed_helper", [True], indirect=True, ids=["agent_owner"])
async def test_completed_helper_rejects_new_release_without_rebinding(completed_helper: SimpleNamespace) -> None:
    case = completed_helper
    await publish_next(case)
    with pytest.raises(AgentExecutionBindingError, match="agent_completed_helper_changed"):
        await read(case)
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT revision_ref FROM turn_agent_execution").fetchall() == [("arv_exact_1",)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("turn_id", "another_turn"),
        ("execution_id", "another_execution"),
        ("dispatch_key", "call:another-call"),
        ("dispatch_key", "reference:primary"),
        ("dispatch_key", "call:"),
    ],
)
async def test_completed_helper_requires_exact_original_dispatch(completed_helper: SimpleNamespace, field: str, value: str) -> None:
    with pytest.raises(AgentExecutionBindingError, match="agent_completed_helper_unavailable"):
        await read(completed_helper, **{field: value})


async def test_completed_helper_preserves_caller_transaction_after_denial_and_cancellation(
    completed_helper: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = completed_helper
    for cancel in (False, True):
        async with open_shared_ctx(str(case.fixture.db_path)) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("UPDATE sessions SET title='caller pending' WHERE id=?", (case.scope.session_id,))
            if cancel:
                execute = db.execute

                def interrupted(sql: str, *args, _execute=execute, **kwargs):
                    if sql.startswith("SELECT * FROM turn_agent_execution WHERE id="):
                        raise anyio.get_cancelled_exc_class()()
                    return _execute(sql, *args, **kwargs)

                monkeypatch.setattr(db, "execute", interrupted)
            else:
                await db.execute("UPDATE agent_assignment SET personal_state='disabled'")
            changes, row_factory = db.total_changes, db.row_factory
            with pytest.raises(anyio.get_cancelled_exc_class() if cancel else AgentExecutionBindingError):
                await case.store.read_completed_helper_in_connection(
                    db, scope=case.scope, turn_id=case.fixture.turn_id, execution_id=case.binding.execution_id, dispatch_key="call:inspect-template"
                )
            assert db.in_transaction and db.row_factory is row_factory and db.total_changes == changes
            assert (await (await db.execute("SELECT title FROM sessions WHERE id=?", (case.scope.session_id,))).fetchone())[0] == "caller pending"
            await db.rollback()


async def test_completed_helper_requires_a_transaction_before_sql(completed_helper: SimpleNamespace) -> None:
    case = completed_helper
    statements = []
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.set_trace_callback(statements.append)
        with pytest.raises(AgentExecutionBindingError, match="agent_admission_transaction_required"):
            await case.store.read_completed_helper_in_connection(
                db, scope=case.scope, turn_id=case.fixture.turn_id, execution_id=case.binding.execution_id, dispatch_key="call:inspect-template"
            )
    assert statements == []


@pytest.mark.parametrize("status", ["admitted", "running", "failed", "blocked", "cancelled"])
async def test_completed_helper_rejects_unsettled_or_unsuccessful_executions(completed_helper: SimpleNamespace, status: str) -> None:
    case = completed_helper
    binding = await case.store.admit_team_execution(
        team_id=case.scope.team_id, user_id=case.scope.user_id, turn_id=case.fixture.turn_id, profile_name="exact-agent", dispatch_key="call:other"
    )
    if status == "running":
        await case.store.mark_running(binding.execution_id)
    elif status != "admitted":
        await case.store.mark_terminal(binding.execution_id, status=status)
    with pytest.raises(AgentExecutionBindingError, match="agent_completed_helper_unavailable"):
        await read(case, execution_id=binding.execution_id, dispatch_key="call:other")


@pytest.mark.parametrize("dispatch", ["explicit", "plan", "system", "nested"])
async def test_completed_helper_rejects_another_execution_path(completed_helper: SimpleNamespace, dispatch: str) -> None:
    case = completed_helper
    binding = await case.store.admit_team_execution(
        team_id=case.scope.team_id,
        user_id=case.scope.user_id,
        turn_id=case.fixture.turn_id,
        profile_name="exact-agent",
        dispatch_key="call:other",
        dispatch_source="lead" if dispatch == "nested" else dispatch,
        parent_execution_id=case.binding.execution_id if dispatch == "nested" else None,
    )
    await case.store.mark_running(binding.execution_id)
    await case.store.mark_terminal(binding.execution_id, status="completed")
    with pytest.raises(AgentExecutionBindingError, match="agent_completed_helper_unavailable"):
        await read(case, execution_id=binding.execution_id, dispatch_key="call:other")


async def test_completed_helper_does_not_adopt_a_bound_root(completed_helper: SimpleNamespace) -> None:
    case = completed_helper
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        assignment_id = (await (await db.execute("SELECT id FROM agent_assignment WHERE user_id=?", (case.scope.user_id,))).fetchone())[0]
        await case.store.insert_root_binding_in_connection(
            db, binding=case.binding, user_id=case.scope.user_id, assignment_id=assignment_id, binding_source="detail"
        )
        await db.commit()
    with pytest.raises(AgentExecutionBindingError, match="agent_completed_helper_unavailable"):
        await read(case)


@pytest.mark.parametrize("completed_helper", [True], indirect=True, ids=["agent_owner"])
async def test_completed_helper_rejects_expanded_plugin_drift_on_the_same_revision(completed_helper: SimpleNamespace) -> None:
    case = completed_helper
    plugins = TeamPluginStore(str(case.fixture.db_path))
    plugin = await plugins.create(
        team_id=case.scope.team_id,
        actor_user_id=case.fixture.owner_id,
        name="Research",
        description="Read-only resources",
        tool_refs=["web_search"],
        skill_grants=[],
    )
    await publish_next(case, capability_ids=[plugin["id"]])
    binding = await case.store.admit_team_execution(
        team_id=case.scope.team_id, user_id=case.scope.user_id, turn_id=case.fixture.turn_id, profile_name="exact-agent", dispatch_key="call:plugin"
    )
    await case.store.mark_running(binding.execution_id)
    await case.store.mark_terminal(binding.execution_id, status="completed")
    assert (await read(case, execution_id=binding.execution_id, dispatch_key="call:plugin")).definition == binding.definition
    await plugins.update(
        team_id=case.scope.team_id,
        plugin_id=plugin["id"],
        actor_user_id=case.fixture.owner_id,
        expected_row_version=plugin["row_version"],
        name="Research",
        description="Changed resources",
        tool_refs=["web_fetch"],
        skill_grants=[],
    )
    with pytest.raises(AgentExecutionBindingError, match="agent_completed_helper_changed"):
        await read(case, execution_id=binding.execution_id, dispatch_key="call:plugin")
    with sqlite3.connect(case.fixture.db_path) as db:
        original = json.loads(
            db.execute("SELECT resolved_definition_json FROM turn_agent_execution WHERE id=?", (binding.execution_id,)).fetchone()[0]
        )
        assert original["tool_refs"] == ["bi_query", "web_search"]
