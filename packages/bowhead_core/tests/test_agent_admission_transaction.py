"""Exact Team/system Agent admission composes with real caller-owned SQLite writes."""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
import pytest_asyncio
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor
from bowhead_core.agent_behavior_materializer import default_agent_behavior_materializer
from bowhead_core.agent_execution_binding import (
    AgentExecutionBindingError,
    AgentExecutionBindingStore,
    ExactAgentBindingExpectation,
    ExactAgentExecutionBinding,
    project_system_agent_revision,
)
from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceCipher, ReferencePlan, ReferenceScope, reference_source_hash
from bowhead_core.chat_references import InputDocument
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.session_store import _insert_chat_turn_user_source_in_tx
from test_agent_execution_binding import _create_team_agent, _insert_release, _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _team_admission_case(tmp_path: Path) -> SimpleNamespace:
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    # Member Agents reach the roster only after the assignment cutover since 8e6f85c9a.
    from bowhead_core.agent_assignment_store import AgentAssignmentStore

    await AgentAssignmentStore(fixture.db_path).backfill_existing_access(apply=True, source_build="test-admission")
    with sqlite3.connect(fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        revision = db.execute("SELECT * FROM team_subagent_revision WHERE id='arv_exact_1'").fetchone()
        assignment_id = db.execute("SELECT id FROM agent_assignment WHERE agent_id='osa_exact'").fetchone()[0]
        epoch = db.execute("SELECT COALESCE((SELECT epoch FROM access_policy_state WHERE team_id=?),0)", (fixture.team_id,)).fetchone()[0]
    # Compile outside the admission transaction; the kernel reads the persisted
    # descriptor and never rebuilds a catalog or loads a resource from disk.
    materializer = default_agent_behavior_materializer()
    descriptor = materializer.compile_legacy_team_revision(
        revision_id=revision["id"],
        definition=json.loads(revision["definition_json"]),
        source_definition_hash=revision["definition_hash"],
        dependency_fingerprint=revision["dependency_fingerprint"],
        capability_fingerprint=revision["capability_fingerprint"],
    )
    async with open_shared_ctx(str(fixture.db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        await materializer.persist_team_revision_conn(
            db,
            revision_id=revision["id"],
            team_id=fixture.team_id,
            subagent_id="osa_exact",
            descriptor=descriptor,
            created_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
    expected = ExactAgentBindingExpectation(
        agent_id="osa_exact",
        assignment_id=assignment_id,
        revision_ref="arv_exact_1",
        release_id="arl_exact_1",
        behavior_fingerprint=descriptor.behavior_fingerprint,
    )
    return SimpleNamespace(
        method_name="admit_team_execution_in_connection",
        source_kind="team",
        fixture=fixture,
        store=AgentExecutionBindingStore(fixture.db_path),
        expected=expected,
        epoch=epoch,
        args={
            "team_id": fixture.team_id,
            "user_id": fixture.member_id,
            "turn_id": fixture.turn_id,
            "profile_name": "exact-agent",
            "agent_id": "osa_exact",
            "dispatch_source": "explicit",
            "dispatch_key": "reference:primary",
            "expected": expected,
        },
    )


async def _system_admission_case(tmp_path: Path) -> SimpleNamespace:
    fixture = await _setup(tmp_path)
    definition = {"llm_agent_type": "analyst", "name": "compute", "schema_version": 1}
    # Catalog initialization and compilation happen before any caller transaction.
    prepared = project_system_agent_revision(team_id=fixture.team_id, profile_name="compute", definition=definition)
    await AgentAssignmentStore(fixture.db_path).hire(
        team_id=fixture.team_id, user_id=fixture.member_id, agent_id=prepared.agent_id, agent_source_kind="system", live_access=True
    )
    with sqlite3.connect(fixture.db_path) as db:
        epoch = db.execute("SELECT COALESCE((SELECT epoch FROM access_policy_state WHERE team_id=?),0)", (fixture.team_id,)).fetchone()[0]
    expected = ExactAgentBindingExpectation(
        agent_id=prepared.agent_id,
        assignment_id=None,
        revision_ref=prepared.revision_ref,
        release_id=None,
        behavior_fingerprint=prepared.behavior_descriptor.behavior_fingerprint,
    )
    return SimpleNamespace(
        method_name="admit_system_execution_in_connection",
        source_kind="system",
        fixture=fixture,
        store=AgentExecutionBindingStore(fixture.db_path),
        expected=expected,
        epoch=epoch,
        args={
            "team_id": fixture.team_id,
            "user_id": fixture.member_id,
            "turn_id": fixture.turn_id,
            "profile_name": "compute",
            "dispatch_source": "explicit",
            "dispatch_key": "reference:primary",
            "definition": definition,
            "manifest_fingerprint": "sha256:" + "1" * 64,
            "override_version": None,
            "expected": expected,
            "prepared_revision": prepared,
        },
    )


@pytest_asyncio.fixture
async def admission_case(tmp_path: Path) -> SimpleNamespace:
    return await _team_admission_case(tmp_path)


@pytest_asyncio.fixture
async def system_admission_case(tmp_path: Path) -> SimpleNamespace:
    return await _system_admission_case(tmp_path)


@pytest_asyncio.fixture(params=["team", "system"])
async def transaction_case(tmp_path: Path, request: pytest.FixtureRequest) -> SimpleNamespace:
    return await (_team_admission_case(tmp_path) if request.param == "team" else _system_admission_case(tmp_path))


async def test_admission_requires_an_existing_transaction_before_sql(transaction_case: SimpleNamespace) -> None:
    case = transaction_case
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        statements: list[str] = []
        await db.set_trace_callback(statements.append)
        with pytest.raises(AgentExecutionBindingError, match="agent_admission_transaction_required"):
            await getattr(case.store, case.method_name)(db, **case.args)
        assert statements == []
        assert not db.in_transaction
        assert db.row_factory is None


@pytest.mark.parametrize("entry", ["team_transaction", "team_public", "system_required"])
@pytest.mark.parametrize("retry", [False, True], ids=["new_dispatch", "existing_dispatch"])
async def test_admission_rechecks_current_space_membership(admission_case: SimpleNamespace, entry: str, retry: bool) -> None:
    case = admission_case
    fixture = case.fixture
    # Keep a second administrator so removing this member is also permitted by
    # the normal membership endpoint's last-administrator protection.
    await fixture.users.add_member(workspace_id=fixture.workspace_id, user_id=fixture.owner_id, role="admin")
    assert await fixture.users.count_active_workspace_admins(fixture.workspace_id) == 2

    async def admit(dispatch_key: str) -> ExactAgentExecutionBinding:
        if entry == "system_required":
            return await case.store.admit_system_execution(
                team_id=fixture.team_id,
                user_id=fixture.member_id,
                turn_id=fixture.turn_id,
                profile_name="compute",
                dispatch_key=dispatch_key,
                definition={"llm_agent_type": "analyst", "name": "compute", "schema_version": 1},
                manifest_fingerprint="sha256:" + "1" * 64,
                override_version=None,
                system_required=True,
                enforce_current_override=False,
            )
        args = {**case.args, "dispatch_key": dispatch_key}
        if entry == "team_public":
            return await case.store.admit_team_execution(**args)
        async with open_shared_ctx(str(fixture.db_path)) as db:
            await db.execute("BEGIN IMMEDIATE")
            binding = await case.store.admit_team_execution_in_connection(db, **args)
            await db.commit()
            return binding

    first = await admit("membership:first")
    original_role = await fixture.users.get_member_role(fixture.workspace_id, fixture.member_id)
    assert original_role is not None
    assert await fixture.users.remove_member(fixture.workspace_id, fixture.member_id)
    # Use the real revocation API. It leaves the live Turn, team membership and
    # Agent assignment intact: none of those gates can mask the missing check.
    assert await fixture.users.get_member_role(fixture.workspace_id, fixture.member_id) is None
    assert await fixture.users.user_has_active_team_membership(fixture.member_id, fixture.team_id)
    dispatch_key = "membership:first" if retry else "membership:next"
    with pytest.raises(AgentExecutionBindingError, match="agent_turn_scope_invalid"):
        await admit(dispatch_key)
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT status FROM turns WHERE id=?", (fixture.turn_id,)).fetchone() == ("running",)
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (1,)
        assert db.execute("SELECT personal_state FROM agent_assignment WHERE agent_id='osa_exact'").fetchone() == ("active",)

    await fixture.users.add_member(workspace_id=fixture.workspace_id, user_id=fixture.member_id, role=original_role)
    restored = await admit(dispatch_key)
    assert (restored.execution_id == first.execution_id) is retry
    assert restored.revision_ref == first.revision_ref


@pytest.mark.parametrize("outcome", ["commit", "rollback", "plan_failure"])
async def test_reference_turn_plan_root_and_execution_share_one_commit(
    transaction_case: SimpleNamespace, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = transaction_case
    fixture = case.fixture
    turn_id = "t_reference_atomic"
    now = datetime.now(UTC).isoformat()
    expires = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    resource = {
        "kind": "agent",
        "source_kind": case.source_kind,
        "id": case.expected.agent_id,
        "version": case.expected.revision_ref,
        "assignment_id": case.expected.assignment_id,
        "release_id": case.expected.release_id,
    }
    resource = {key: value for key, value in resource.items() if value is not None}
    node_id = "00000000-0000-4000-8000-000000000001"
    document = InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                {"type": "reference", "node_id": node_id, "resource": resource, "label": "Exact Agent", "parameters": {}},
                {"type": "text", "text": " analyze profit"},
            ],
        }
    )
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id="s_m3b",
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        user_text=document.display_text(),
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        input_document=document,
    )
    scope = ReferenceScope(
        team_id=fixture.team_id, user_id=fixture.member_id, workspace_id=fixture.workspace_id, session_id="s_m3b", policy_epoch=case.epoch
    )
    references = ChatReferenceStore(ReferenceCipher(base64.urlsafe_b64encode(b"t" * 32).decode()))
    tables = ("chat_turn_user_sources", "chat_reference_plans", "turn_agent_execution", "turn_agent_binding", "chat_turn_spawn_dispatch")
    revision_tables = (
        ("system_agent_effective_revision", "system_agent_revision_behavior", "resource_dependency_subject_projection")
        if case.source_kind == "system"
        else ()
    )
    # An unusable store path proves that all reads, including access resolution,
    # use the provided connection rather than silently opening a second one.
    store = AgentExecutionBindingStore(fixture.db_path / "must-not-open.db")
    async with open_shared_ctx(str(fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES(?, 's_m3b',2,?,?)", (turn_id, source.user_text, now))
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now)
        args = {**case.args, "turn_id": turn_id}
        binding = await getattr(store, case.method_name)(db, **args)
        assert db.in_transaction and db.row_factory is None
        duplicate = await getattr(store, case.method_name)(db, **args)
        assert duplicate == binding
        assert binding.status == "admitted"
        assert db.in_transaction and db.row_factory is None
        await store.insert_root_binding_in_connection(
            db, binding=binding, user_id=fixture.member_id, assignment_id=case.expected.assignment_id, binding_source="mention"
        )
        await db.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,lease_expires_at,created_at,updated_at) VALUES(?,'s_m3b','start','claimed',?,?,?)",
            (turn_id, expires, now, now),
        )
        plan = ReferencePlan.model_validate(
            {
                "schema_version": 1,
                "source_turn_id": turn_id,
                "logical_source_turn_id": turn_id,
                "snapshot_owner_turn_id": turn_id,
                "source_hash": reference_source_hash(source),
                "admissions": [{"node_id": node_id, "resource": resource, "operation": "delegate", "policy_digest": binding.policy_fingerprint}],
            }
        )
        if outcome == "plan_failure":
            plan = plan.model_copy(update={"source_hash": "sha256:" + "0" * 64})
            with pytest.raises(ValueError, match="reference_source_changed"):
                await references.put_plan_in_connection(db, scope=scope, turn_id=turn_id, plan=plan, expires_at=expires)
        else:
            await references.put_plan_in_connection(db, scope=scope, turn_id=turn_id, plan=plan, expires_at=expires)
        with sqlite3.connect(fixture.db_path) as observer:
            assert observer.execute("SELECT COUNT(*) FROM turns WHERE id=?", (turn_id,)).fetchone() == (0,)
            for table in tables:
                assert observer.execute(f"SELECT COUNT(*) FROM {table} WHERE turn_id=?", (turn_id,)).fetchone() == (0,)
            for table in revision_tables:
                assert observer.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        if outcome == "commit":
            await db.commit()
        else:
            await db.rollback()
    with sqlite3.connect(fixture.db_path) as observer:
        expected_count = (1 if outcome == "commit" else 0,)
        assert observer.execute("SELECT COUNT(*) FROM turns WHERE id=?", (turn_id,)).fetchone() == expected_count
        for table in tables:
            assert observer.execute(f"SELECT COUNT(*) FROM {table} WHERE turn_id=?", (turn_id,)).fetchone() == expected_count
        for table in revision_tables:
            assert observer.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == expected_count
        if outcome == "commit":
            assert observer.execute(
                "SELECT dispatch_source,dispatch_key,revision_ref,release_id,status FROM turn_agent_execution WHERE turn_id=?", (turn_id,)
            ).fetchone() == ("explicit", "reference:primary", case.expected.revision_ref, case.expected.release_id, "admitted")


@pytest.mark.parametrize("denial", ["turn_scope", "assignment", "access", "expected_assignment", "expected_revision"])
async def test_transaction_admission_rechecks_access_without_rolling_back_caller(admission_case: SimpleNamespace, denial: str) -> None:
    case = admission_case
    args = dict(case.args)
    if denial == "turn_scope":
        # Both actors can use this Agent; only the Turn owner may bind its run.
        hired = await AgentAssignmentStore(case.fixture.db_path).hire(
            team_id=case.fixture.team_id, user_id=case.fixture.owner_id, agent_id="osa_exact", agent_source_kind="team", live_access=True
        )
        args["expected"] = replace(case.expected, assignment_id=hired.assignment.id)
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE sessions SET title='caller pending' WHERE id='s_m3b'")
        if denial == "turn_scope":
            args["user_id"] = case.fixture.owner_id
            code = "agent_turn_scope_invalid"
        elif denial == "assignment":
            await db.execute("UPDATE agent_assignment SET personal_state='disabled',row_version=row_version+1 WHERE agent_id='osa_exact'")
            code = "agent_assignment_inactive"
        elif denial == "access":
            await db.execute(
                "UPDATE team_subagent SET visibility='creator_only',audience_mode='private',row_version=row_version+1 WHERE id='osa_exact'"
            )
            code = "agent_access_denied"
        else:
            key = "assignment_id" if denial == "expected_assignment" else "revision_ref"
            args["expected"] = replace(case.expected, **{key: "different"})
            code = "agent_candidate_stale"
        with pytest.raises(AgentExecutionBindingError, match=code):
            await case.store.admit_team_execution_in_connection(db, **args)
        assert db.in_transaction and db.row_factory is None
        async with db.execute("SELECT title FROM sessions WHERE id='s_m3b'") as cursor:
            assert await cursor.fetchone() == ("caller pending",)
        await db.rollback()
    with sqlite3.connect(case.fixture.db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (0,)
        assert observer.execute("SELECT title FROM sessions WHERE id='s_m3b'").fetchone() != ("caller pending",)


async def test_public_admission_keeps_exact_expectation_and_dispatch_fields(admission_case: SimpleNamespace) -> None:
    case = admission_case
    with pytest.raises(AgentExecutionBindingError, match="agent_candidate_stale"):
        await case.store.admit_team_execution(**{**case.args, "expected": replace(case.expected, assignment_id="stale")})
    first = await case.store.admit_team_execution(**case.args)
    await _insert_release(case.fixture, sequence=2, prompt="revision two")
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(AgentExecutionBindingError, match="agent_candidate_stale"):
            await case.store.admit_team_execution_in_connection(
                db, **{**case.args, "expected": replace(case.expected, behavior_fingerprint="sha256:" + "0" * 64)}
            )
        # The live release changed, but a matching admitted dispatch still refers
        # to its original revision and its original behavior descriptor.
        replay = await case.store.admit_team_execution_in_connection(db, **case.args)
        assert replay == first
        assert replay.definition["system_prompt"] == "revision one"
        assert db.in_transaction and db.row_factory is None
        await db.rollback()
    with sqlite3.connect(case.fixture.db_path) as observer:
        assert observer.execute("SELECT dispatch_source,dispatch_key,agent_id,status FROM turn_agent_execution").fetchall() == [
            ("explicit", "reference:primary", "osa_exact", "admitted")
        ]


async def test_public_admission_preserves_helper_parent_and_explicit_agent(admission_case: SimpleNamespace) -> None:
    case = admission_case
    root = await case.store.admit_team_execution(**case.args)
    helper_args = {**case.args, "dispatch_source": "lead", "dispatch_key": "call:helper", "parent_execution_id": root.execution_id}
    with pytest.raises(AgentExecutionBindingError, match="agent_not_runnable"):
        await case.store.admit_team_execution(**{**helper_args, "agent_id": "osa_missing"})
    helper = await case.store.admit_team_execution(**helper_args)
    assert helper.execution_id != root.execution_id
    with sqlite3.connect(case.fixture.db_path) as observer:
        assert observer.execute(
            "SELECT parent_execution_id,dispatch_source,dispatch_key FROM turn_agent_execution WHERE id=?", (helper.execution_id,)
        ).fetchone() == (root.execution_id, "lead", "call:helper")
        assert observer.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (2,)


async def test_cancelled_storage_call_leaves_outer_transaction_owned_by_caller(
    transaction_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = transaction_case
    cancelled = anyio.get_cancelled_exc_class()
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE sessions SET title='caller pending' WHERE id='s_m3b'")
        execute = db.execute

        def interrupted_execute(sql: str, *args: object, **kwargs: object):
            if sql.lstrip().startswith("INSERT INTO turn_agent_execution("):
                raise cancelled()
            return execute(sql, *args, **kwargs)

        # Inject cancellation at the storage I/O boundary, keeping admission,
        # access resolution and all earlier SQL real.
        monkeypatch.setattr(db, "execute", interrupted_execute)
        with pytest.raises(cancelled):
            await getattr(case.store, case.method_name)(db, **case.args)
        assert db.in_transaction and db.row_factory is None
        async with db.execute("SELECT title FROM sessions WHERE id='s_m3b'") as cursor:
            assert await cursor.fetchone() == ("caller pending",)
        await db.rollback()
    with sqlite3.connect(case.fixture.db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (0,)
        assert observer.execute("SELECT title FROM sessions WHERE id='s_m3b'").fetchone() != ("caller pending",)


async def test_concurrent_transaction_admission_reuses_one_execution(transaction_case: SimpleNamespace) -> None:
    case = transaction_case
    first_admitted, second_waiting = anyio.Event(), anyio.Event()
    execution_ids: list[str] = []

    async def admit(first: bool) -> None:
        async with open_shared_ctx(str(case.fixture.db_path)) as db:
            if not first:
                await first_admitted.wait()
                second_waiting.set()
            await db.execute("BEGIN IMMEDIATE")
            binding = await getattr(case.store, case.method_name)(db, **case.args)
            execution_ids.append(binding.execution_id)
            if first:
                first_admitted.set()
                await second_waiting.wait()
            await db.commit()

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(admit, True)
            group.start_soon(admit, False)
    assert len(execution_ids) == 2 and execution_ids[0] == execution_ids[1]
    with sqlite3.connect(case.fixture.db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (1,)


async def test_system_missing_preparation_preserves_caller_and_never_loads_catalog(
    system_admission_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = system_admission_case

    def unexpected_catalog() -> None:
        raise AssertionError("catalog initialization is forbidden inside admission")

    monkeypatch.setattr("bowhead_core.agent_execution_binding.default_agent_behavior_materializer", unexpected_catalog)
    args = {key: value for key, value in case.args.items() if key != "prepared_revision"}
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE sessions SET title='caller pending' WHERE id='s_m3b'")
        statements: list[str] = []
        await db.set_trace_callback(statements.append)
        with pytest.raises(AgentExecutionBindingError, match="agent_system_revision_preparation_required"):
            await case.store.admit_system_execution_in_connection(db, **args)
        assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
        assert db.in_transaction and db.row_factory is None
        async with db.execute("SELECT title FROM sessions WHERE id='s_m3b'") as cursor:
            assert await cursor.fetchone() == ("caller pending",)
        binding = await case.store.admit_system_execution_in_connection(db, **case.args)
        # Both an existing dispatch and a different dispatch on the same snapshot
        # work without preparation, and preserve the immutable behavior sidecar.
        assert await case.store.admit_system_execution_in_connection(db, **args) == binding
        next_binding = await case.store.admit_system_execution_in_connection(db, **{**args, "dispatch_key": "next"})
        assert next_binding.revision_ref == binding.revision_ref
        assert next_binding.behavior_descriptor == binding.behavior_descriptor
        await db.rollback()


@pytest.mark.parametrize(
    "field", ["agent_id", "revision_ref", "definition_hash", "definition", "capability_fingerprint", "behavior_source", "behavior_fingerprint"]
)
async def test_system_prepared_revision_is_revalidated_before_sql(system_admission_case: SimpleNamespace, field: str) -> None:
    case = system_admission_case
    prepared = case.args["prepared_revision"]
    code = "agent_candidate_stale"
    if field.startswith("behavior_"):
        key = "source_definition_hash" if field == "behavior_source" else "behavior_fingerprint"
        prepared = replace(prepared, behavior_descriptor=prepared.behavior_descriptor.model_copy(update={key: "sha256:" + "0" * 64}))
        if field == "behavior_fingerprint":
            code = "agent_behavior_descriptor_invalid"
    else:
        prepared = replace(prepared, **{field: {"name": "different"} if field == "definition" else "different"})
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        statements: list[str] = []
        await db.set_trace_callback(statements.append)
        with pytest.raises(AgentExecutionBindingError, match=code):
            await case.store.admit_system_execution_in_connection(db, **{**case.args, "prepared_revision": prepared})
        assert statements == []
        assert db.in_transaction and db.row_factory is None
        await db.rollback()


@pytest.mark.parametrize("retry", [False, True], ids=["new_dispatch", "existing_dispatch"])
@pytest.mark.parametrize("denial", ["assignment", "override_changed", "override_disabled", "expected_revision", "space_membership"])
async def test_system_transaction_rechecks_current_authority_and_candidate(system_admission_case: SimpleNamespace, denial: str, retry: bool) -> None:
    case = system_admission_case
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        binding = await case.store.admit_system_execution_in_connection(db, **case.args)
        await db.commit()
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE sessions SET title='caller pending' WHERE id='s_m3b'")
        args = {**case.args, "dispatch_key": case.args["dispatch_key"] if retry else "next"}
        if denial == "assignment":
            await db.execute("UPDATE agent_assignment SET personal_state='disabled',row_version=row_version+1 WHERE agent_id='system:compute'")
            code = "agent_assignment_inactive"
        elif denial.startswith("override_"):
            await db.execute(
                "INSERT INTO team_builtin_subagent_override(team_id,name,enabled,row_version,created_at,updated_at) VALUES(?,'compute',?,1,'now','now')",
                (case.fixture.team_id, int(denial != "override_disabled")),
            )
            code = "agent_system_definition_changed" if denial == "override_changed" else "agent_not_runnable"
        elif denial == "space_membership":
            await db.execute(
                "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?",
                (case.fixture.workspace_id, case.fixture.member_id),
            )
            code = "agent_turn_scope_invalid"
        else:
            args["expected"] = replace(case.expected, revision_ref="stale")
            code = "agent_candidate_stale"
        with pytest.raises(AgentExecutionBindingError, match=code):
            await case.store.admit_system_execution_in_connection(db, **args)
        assert db.in_transaction and db.row_factory is None
        async with db.execute("SELECT title FROM sessions WHERE id='s_m3b'") as cursor:
            assert await cursor.fetchone() == ("caller pending",)
        await db.rollback()
        await db.execute("BEGIN IMMEDIATE")
        assert await case.store.admit_system_execution_in_connection(db, **case.args) == binding
        await db.rollback()
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (1,)


@pytest.mark.parametrize("change", ["none", "override", "assignment"])
async def test_system_public_prepares_without_write_lock_and_rechecks_afterward(system_admission_case: SimpleNamespace, change: str) -> None:
    case = system_admission_case
    materializer = default_agent_behavior_materializer()
    preparations = 0

    class ConcurrentPreparation:
        def compile_system_revision(self, **kwargs: Any) -> AgentBehaviorDescriptor:
            nonlocal preparations
            preparations += 1
            # A real competing writer must acquire the lock during preparation.
            # No revision/sidecar/execution may have escaped the first transaction.
            with sqlite3.connect(case.fixture.db_path, timeout=0) as db:
                db.execute("BEGIN IMMEDIATE")
                for table in ("system_agent_effective_revision", "system_agent_revision_behavior", "turn_agent_execution"):
                    assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
                if change == "override":
                    db.execute(
                        "INSERT INTO team_builtin_subagent_override(team_id,name,enabled,row_version,created_at,updated_at) VALUES(?,'compute',0,1,'now','now')",
                        (case.fixture.team_id,),
                    )
                elif change == "assignment":
                    db.execute("UPDATE agent_assignment SET personal_state='disabled',row_version=row_version+1 WHERE agent_id='system:compute'")
            return materializer.compile_system_revision(**kwargs)

    store = AgentExecutionBindingStore(case.fixture.db_path, behavior_materializer=ConcurrentPreparation())  # type: ignore[arg-type]
    args = {key: value for key, value in case.args.items() if key != "prepared_revision"}
    if change == "none":
        binding = await store.admit_system_execution(**args)
        # No preparation on either a replay or a new dispatch of this snapshot.
        assert await store.admit_system_execution(**args) == binding
        await store.admit_system_execution(**{**args, "dispatch_key": "next"})
    else:
        code = "agent_not_runnable" if change == "override" else "agent_assignment_inactive"
        with pytest.raises(AgentExecutionBindingError, match=code):
            await store.admit_system_execution(**args)
    assert preparations == 1
    with sqlite3.connect(case.fixture.db_path) as db:
        for table in ("system_agent_effective_revision", "system_agent_revision_behavior"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (int(change == "none"),)
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (2 if change == "none" else 0,)


async def test_system_public_keeps_exact_fields_and_retry_revision(system_admission_case: SimpleNamespace) -> None:
    case = system_admission_case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "INSERT INTO team_builtin_subagent_override(team_id,name,enabled,row_version,created_at,updated_at) VALUES(?,'compute',1,7,'now','now')",
            (case.fixture.team_id,),
        )
    args = {key: value for key, value in case.args.items() if key != "prepared_revision"}
    args["override_version"] = 7
    first = await case.store.admit_system_execution(**args)
    helper = await case.store.admit_system_execution(
        **{**args, "dispatch_key": "helper", "dispatch_source": "plan", "parent_execution_id": first.execution_id}
    )
    changed = {**args, "definition": {**args["definition"], "description": "new definition"}}
    assert await case.store.admit_system_execution(**changed) == first
    with pytest.raises(AgentExecutionBindingError, match="agent_candidate_stale"):
        await case.store.admit_system_execution(**{**changed, "dispatch_key": "stale"})
    with pytest.raises(AgentExecutionBindingError, match="agent_candidate_stale"):
        await case.store.admit_system_execution(**{**args, "expected": replace(case.expected, behavior_fingerprint="sha256:" + "0" * 64)})
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT parent_execution_id,dispatch_source,dispatch_key FROM turn_agent_execution WHERE id=?", (helper.execution_id,)
        ).fetchone() == (first.execution_id, "plan", "helper")
        assert db.execute("SELECT manifest_fingerprint,override_version FROM system_agent_effective_revision").fetchall() == [
            (args["manifest_fingerprint"], 7)
        ]
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM system_agent_revision_behavior").fetchone() == (1,)


async def test_system_public_required_and_override_bypass_are_independent(system_admission_case: SimpleNamespace) -> None:
    case = system_admission_case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE agent_assignment SET personal_state='disabled' WHERE agent_id='system:compute'")
        db.execute(
            "INSERT INTO team_builtin_subagent_override(team_id,name,enabled,row_version,created_at,updated_at) VALUES(?,'compute',0,1,'now','now')",
            (case.fixture.team_id,),
        )
    args = {key: value for key, value in case.args.items() if key != "prepared_revision"}
    args.update(system_required=True, enforce_current_override=False)
    binding = await case.store.admit_system_execution(**args)
    assert binding.agent_id == "system:compute"
    with pytest.raises(AgentExecutionBindingError, match="agent_not_runnable"):
        await case.store.admit_system_execution(**{**args, "enforce_current_override": True})
    with pytest.raises(AgentExecutionBindingError, match="agent_assignment_inactive"):
        await case.store.admit_system_execution(**{**args, "system_required": False})
