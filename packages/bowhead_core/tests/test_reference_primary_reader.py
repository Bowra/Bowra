"""Real accepted references feed the immutable runtime materializer, without writes."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_execution import read_reference_primary_in_connection
from bowhead_core.chat_reference_storage import ReferenceScope
from bowhead_core.subagent import SubagentRunner
from test_agent_execution_binding import _insert_release
from test_reference_start_admission import start_case as _start_case_fixture

start_case = _start_case_fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _prepare_case(case: SimpleNamespace) -> SimpleNamespace:
    fixture = case.fixture
    case.runner = SubagentRunner(router=None, tool_registry=None, team_subagent_store=fixture.users)
    case.ctx = SimpleNamespace(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        session_id="s_reference",
        turn_id="t_reference",
        granted_team_subagent_ids=None,
        builtin_subagent_overrides=None,
    )
    if case.agent.source_kind == "system":
        # Use the actual production snapshot projection so profile preparation
        # validates executable fields, not the admission-only minimal fixture.
        prepared = await case.runner.project_system_behavior_candidate(profile_name="compute", parent_ctx=case.ctx)
        assert prepared is not None
        admission = case.args["reference_start"]
        node = admission.input_document.nodes[0]
        admission.input_document.nodes[0] = node.model_copy(update={"resource": node.resource.model_copy(update={"version": prepared.revision_ref})})
        case.args["reference_start"] = replace(
            admission,
            system_revision=prepared,
            expected=replace(
                admission.expected, revision_ref=prepared.revision_ref, behavior_fingerprint=prepared.behavior_descriptor.behavior_fingerprint
            ),
        )
    case.scope = ReferenceScope(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        session_id="s_reference",
        policy_epoch=case.agent.epoch,
    )
    # An unusable configured path proves the reader stays on the supplied DB.
    case.bindings = AgentExecutionBindingStore(fixture.db_path.parent / "missing" / "unused.db")
    return case


@pytest_asyncio.fixture
async def accepted_case(start_case: SimpleNamespace) -> SimpleNamespace:
    case = await _prepare_case(start_case)
    await case.store.finalize_started_chat_submission(**case.args)
    return case


async def _read(case: SimpleNamespace):
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        changes = db.total_changes  # Connection-wide cumulative count; compare this transaction's delta on reused connections.
        result = await read_reference_primary_in_connection(db, scope=case.scope, turn_id="t_reference", bindings=case.bindings)
        assert db.in_transaction and db.row_factory is None and db.total_changes == changes
        await db.rollback()
        return result


@pytest.mark.parametrize("behavior_enabled", [False, True])
async def test_reserved_primary_materializes_the_exact_profile_without_writing(
    accepted_case: SimpleNamespace, behavior_enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = accepted_case
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", str(int(behavior_enabled)))
    first = await _read(case)
    profile = await case.runner.resolve_by_agent_revision(first.binding, case.ctx)
    assert profile.name == first.binding.runtime_profile_name
    if first.binding.agent_source_kind == "team":
        assert profile.system_prompt.endswith(first.binding.definition["system_prompt"])
        assert "## Final answer transport" in profile.system_prompt
    else:
        assert profile.system_prompt == first.binding.definition["system_prompt"]
    assert profile.behavior_descriptor == (first.binding.behavior_descriptor if behavior_enabled else None)
    assert first.source.input_document == case.args["reference_start"].input_document
    assert first.plan.admissions[0].policy_digest == first.binding.policy_fingerprint
    assert first.binding.status == "admitted"
    # A consumer mutating its prepared copy cannot poison the next read.
    first.binding.definition["system_prompt"] = "tampered local copy"
    again = await _read(case)
    assert again.binding.definition["system_prompt"] != "tampered local copy"
    assert again.binding.execution_id == first.binding.execution_id
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == (1,)
        assert db.execute("SELECT owner_id,state FROM chat_turn_spawn_dispatch WHERE turn_id='t_reference'").fetchone() == (None, "claimed")
        assert db.execute("SELECT COUNT(*) FROM turn_events WHERE turn_id='t_reference'").fetchone() == (0,)


async def test_running_primary_is_readable_without_creating_a_second_run(accepted_case: SimpleNamespace) -> None:
    case = accepted_case
    first = await _read(case)
    await AgentExecutionBindingStore(case.fixture.db_path).mark_running(first.binding.execution_id)
    second = await _read(case)
    assert second.binding.execution_id == first.binding.execution_id and second.binding.status == "running"


@pytest.mark.parametrize(
    "denial",
    [
        "space_member",
        "team_member",
        "user",
        "workspace",
        "session_deleted",
        "policy_epoch",
        "assignment",
        "grant",
        "rehire",
        "terminal_turn",
        "terminal_execution",
        "missing_root",
        "missing_plan",
        "expired_plan",
    ],
)
async def test_read_rechecks_current_authority_and_saved_facts(accepted_case: SimpleNamespace, denial: str, monkeypatch: pytest.MonkeyPatch) -> None:
    case = accepted_case
    with sqlite3.connect(case.fixture.db_path) as db:
        if denial == "space_member":
            db.execute("UPDATE workspace_members SET status='removed' WHERE user_id=?", (case.fixture.member_id,))
        elif denial == "team_member":
            db.execute("UPDATE team_members SET status='removed' WHERE user_id=?", (case.fixture.member_id,))
        elif denial == "user":
            db.execute("UPDATE users SET status='disabled' WHERE id=?", (case.fixture.member_id,))
        elif denial == "workspace":
            db.execute("UPDATE workspaces SET status='archived' WHERE id=?", (case.fixture.workspace_id,))
        elif denial == "session_deleted":
            db.execute("UPDATE sessions SET deleted_at='now' WHERE id='s_reference'")
        elif denial == "policy_epoch":
            case.scope = case.scope.model_copy(update={"policy_epoch": case.scope.policy_epoch + 1})
        elif denial == "assignment":
            db.execute("UPDATE agent_assignment SET personal_state='disabled'")
        elif denial == "grant":
            db.execute("UPDATE agent_assignment SET access_state='revoked'")
        elif denial == "rehire":
            # The real hire writer below reuses the ID and starts a new cycle.
            db.execute("UPDATE agent_assignment SET personal_state='removed'")
        elif denial == "terminal_turn":
            db.execute("DROP TRIGGER trg_reference_primary_completion")
            db.execute("UPDATE turns SET status='completed' WHERE id='t_reference'")
        elif denial == "terminal_execution":
            db.execute(
                "UPDATE turn_agent_execution SET status='blocked',failure_code='test',ended_at=? WHERE turn_id='t_reference'",
                (datetime.now(tz=UTC).isoformat(),),
            )
        elif denial == "missing_root":
            db.execute("DELETE FROM turn_agent_binding WHERE turn_id='t_reference'")
        elif denial == "missing_plan":
            db.execute("DROP TRIGGER trg_reference_plan_parent_delete")  # Historical corruption, not a supported writer.
            db.execute("DELETE FROM chat_reference_plans WHERE turn_id='t_reference'")
        else:
            import bowhead_core.chat_reference_storage as storage

            monkeypatch.setattr(storage, "_now", lambda: datetime.now(tz=UTC) + timedelta(days=31))
    if denial == "rehire":
        await AgentAssignmentStore(case.fixture.db_path).hire(
            team_id=case.fixture.team_id,
            user_id=case.fixture.member_id,
            agent_id=case.agent.expected.agent_id,
            agent_source_kind=case.agent.source_kind,
            live_access=True,
        )
    with pytest.raises((ValueError, RuntimeError)):
        await _read(case)
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM chat_submission_receipts WHERE turn_id='t_reference'").fetchone() == (1,)


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
async def test_team_rename_and_new_release_do_not_replace_reserved_revision(accepted_case: SimpleNamespace) -> None:
    case = accepted_case
    first = await _read(case)
    await _insert_release(case.fixture, sequence=2, prompt="new revision prompt")
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE team_subagent SET name='renamed-agent',row_version=row_version+1 WHERE id='osa_exact'")
    current = await _read(case)
    profile = await case.runner.resolve_by_agent_revision(current.binding, case.ctx)
    assert current.binding == first.binding
    assert profile.name == "exact-agent" and profile.system_prompt.endswith("revision one")
    assert "new revision prompt" not in profile.system_prompt


@pytest.mark.parametrize("start_case", ["system"], indirect=True)
@pytest.mark.parametrize("enabled", [False, True])
async def test_system_override_change_does_not_reauthorize_the_old_execution(accepted_case: SimpleNamespace, enabled: bool) -> None:
    case = accepted_case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "INSERT INTO team_builtin_subagent_override(team_id,name,enabled,row_version,created_at,updated_at) VALUES(?,'compute',?,1,'now','now')",
            (case.fixture.team_id, int(enabled)),
        )
    with pytest.raises(RuntimeError, match="agent_system_definition_changed" if enabled else "agent_not_runnable"):
        await _read(case)


@pytest.mark.parametrize("start_case", ["system"], indirect=True)
async def test_system_deduplicated_revision_uses_execution_policy_not_old_override_metadata(start_case: SimpleNamespace) -> None:
    case = await _prepare_case(start_case)
    admission = case.args["reference_start"]
    prepared = admission.system_revision
    assert prepared is not None
    await AgentExecutionBindingStore(case.fixture.db_path).admit_system_execution(
        team_id=case.fixture.team_id,
        user_id=case.fixture.member_id,
        turn_id=case.fixture.turn_id,
        profile_name="compute",
        dispatch_key="older",
        definition=prepared.definition,
        manifest_fingerprint=admission.manifest_fingerprint,
        override_version=None,
        expected=admission.expected,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "INSERT INTO team_builtin_subagent_override(team_id,name,enabled,row_version,created_at,updated_at) VALUES(?,'compute',1,7,'now','now')",
            (case.fixture.team_id,),
        )
    case.args["reference_start"] = replace(admission, override_version=7)
    await case.store.finalize_started_chat_submission(**case.args)
    result = await _read(case)
    assert result.binding.revision_ref == prepared.revision_ref
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT override_version FROM system_agent_effective_revision").fetchall() == [(None,)]
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (2,)


async def test_no_transaction_is_rejected_before_sql(accepted_case: SimpleNamespace) -> None:
    case = accepted_case
    statements = []
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.set_trace_callback(statements.append)
        with pytest.raises(ValueError, match="reference_transaction_required"):
            await read_reference_primary_in_connection(db, scope=case.scope, turn_id="t_reference", bindings=case.bindings)
        admission = case.args["reference_start"]
        with pytest.raises(RuntimeError, match="agent_admission_transaction_required"):
            await case.bindings.read_reference_primary_in_connection(
                db,
                team_id=case.scope.team_id,
                user_id=case.scope.user_id,
                turn_id="t_reference",
                resource=admission.input_document.nodes[0].resource,
                policy_fingerprint="sha256:" + "0" * 64,
            )
    assert statements == []


@pytest.mark.parametrize("field", ["team_id", "user_id", "workspace_id", "session_id"])
async def test_read_cannot_cross_the_requested_scope(accepted_case: SimpleNamespace, field: str) -> None:
    case = accepted_case
    case.scope = case.scope.model_copy(update={field: "another_scope"})
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        await _read(case)


@pytest.mark.parametrize("mode", ["read", "denied", "cancel"])
async def test_reader_preserves_caller_transaction_on_success_denial_and_cancel(
    accepted_case: SimpleNamespace, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    case = accepted_case
    cancelled = anyio.get_cancelled_exc_class()
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE sessions SET title='caller pending' WHERE id='s_reference'")
        if mode == "denied":
            await db.execute("UPDATE agent_assignment SET personal_state='disabled'")
        elif mode == "cancel":
            execute = db.execute

            def interrupted(sql, *args, **kwargs):
                if sql.startswith("SELECT * FROM turn_agent_binding "):
                    raise cancelled()
                return execute(sql, *args, **kwargs)

            monkeypatch.setattr(db, "execute", interrupted)
        changes = db.total_changes
        if mode == "read":
            await read_reference_primary_in_connection(db, scope=case.scope, turn_id="t_reference", bindings=case.bindings)
        else:
            with pytest.raises(cancelled if mode == "cancel" else RuntimeError):
                await read_reference_primary_in_connection(db, scope=case.scope, turn_id="t_reference", bindings=case.bindings)
        assert db.in_transaction and db.row_factory is None and db.total_changes == changes
        async with db.execute("SELECT title FROM sessions WHERE id='s_reference'") as cursor:
            assert await cursor.fetchone() == ("caller pending",)
        with sqlite3.connect(case.fixture.db_path) as observer:
            assert observer.execute("SELECT title FROM sessions WHERE id='s_reference'").fetchone() != ("caller pending",)
        await db.rollback()


@pytest.mark.parametrize("field", ["policy", "resource", "primary_id"])
async def test_corrupt_binding_is_rejected_without_guessing_an_execution(accepted_case: SimpleNamespace, field: str) -> None:
    case = accepted_case
    # Explicitly simulate a damaged persisted ledger. Normal application writes
    # cannot bypass these immutable triggers; the reader must still fail closed.
    with sqlite3.connect(case.fixture.db_path) as db:
        triggers = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='turn_agent_binding'").fetchall()
        for (name,) in triggers:
            db.execute(f'DROP TRIGGER "{name}"')
        if field == "policy":
            db.execute("UPDATE turn_agent_binding SET policy_fingerprint=? WHERE turn_id='t_reference'", ("sha256:" + "0" * 64,))
        elif field == "resource":
            db.execute("UPDATE turn_agent_binding SET requested_agent_id='osa_different' WHERE turn_id='t_reference'")
        else:
            db.execute("UPDATE turn_agent_binding SET primary_execution_id='aex_missing' WHERE turn_id='t_reference'")
    with pytest.raises(RuntimeError, match="reference_primary_mismatch"):
        await _read(case)


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("change", ["audience", "disabled", "safety"])
async def test_team_current_access_and_safety_remain_mandatory(accepted_case: SimpleNamespace, change: str) -> None:
    case = accepted_case
    if change == "safety":
        await _insert_release(case.fixture, sequence=2, prompt="new revision", safety_content_hash="sha256:" + "0" * 64)
    with sqlite3.connect(case.fixture.db_path) as db:
        if change == "audience":
            db.execute("UPDATE team_subagent SET visibility='creator_only',audience_mode='private' WHERE id='osa_exact'")
            code = "agent_access_denied"
        elif change == "disabled":
            db.execute("UPDATE team_subagent SET status='disabled' WHERE id='osa_exact'")
            code = "agent_not_runnable"
        else:
            code = "agent_live_release_inconsistent"
    with pytest.raises(RuntimeError, match=code):
        await _read(case)


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
async def test_bound_safety_corruption_is_rejected_after_a_new_safe_release(accepted_case: SimpleNamespace) -> None:
    case = accepted_case
    await _insert_release(case.fixture, sequence=2, prompt="new safe revision")
    with sqlite3.connect(case.fixture.db_path) as db:
        # Corruption injection only; safety reports are immutable in normal writes.
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='agent_safety_report'").fetchall():
            db.execute(f'DROP TRIGGER "{name}"')
        db.execute("UPDATE agent_safety_report SET status='blocked' WHERE subject_ref='arv_exact_1'")
    with pytest.raises(RuntimeError, match="agent_safety_blocked"):
        await _read(case)
