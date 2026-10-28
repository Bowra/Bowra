"""Canonical input -> current published candidate -> actual atomic acceptance."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingError, AgentExecutionBindingStore
from bowhead_core.chat_reference_preparation import ReferenceStartPreparationError, prepare_reference_team_start_admission
from bowhead_core.chat_references import InputDocument, ReferenceNode
from test_reference_start_admission import _assert_no_acceptance
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

from bowhead_core import chat_reference_preparation as preparation

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


def raw_document(fixture, *, skills=True):
    document = fixture.case.args["reference_start"].input_document
    nodes = [node.model_dump(mode="python") for node in document.nodes]
    nodes[0]["label"] = "ANOTHER DISPLAY NAME"
    if skills:
        nodes = [
            {
                "type": "reference",
                "node_id": str(uuid4()),
                "resource": resource.model_dump(mode="python"),
                "label": "METHOD",
                "parameters": {"year": year},
            }
            for resource, year in ((fixture.selected, 2026), (fixture.child, 2025))
        ] + nodes
    return InputDocument.model_validate({"schema_version": 1, "nodes": nodes})


def activate(fixture):
    with sqlite3.connect(fixture.case.fixture.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at='2026-09-14T00:00:00+00:00' WHERE singleton=1")


async def prepare(fixture, *, document=None, **patch):
    return await prepare_reference_team_start_admission(
        fixture.case.fixture.db_path,
        **{"input_document": document or raw_document(fixture), "scope": fixture.scope, "sources": fixture.sources, **patch},
    )


@pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True)
async def test_raw_document_derives_server_identity_and_reaches_real_finalizer(receiver_case):
    fixture = receiver_case
    activate(fixture)
    document = raw_document(fixture)
    result = await prepare(fixture, document=document)
    assert result.input_document == document
    assert result.profile_name == "exact-agent" and result.expected == fixture.case.args["reference_start"].expected
    assert len(result.skill_reads.items) == 2
    assert len([node for node in result.input_document.nodes if isinstance(node, ReferenceNode)]) == 3
    _assert_no_acceptance(fixture.case)
    fixture.case.args.update(reference_start=result, user_text=document.display_text())
    receipt = await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    assert receipt.outcome == "started"
    with sqlite3.connect(fixture.case.fixture.db_path) as db:
        assert db.execute("SELECT runtime_profile_name,revision_ref FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == (
            result.profile_name,
            result.expected.revision_ref,
        )


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_agent_only_projection_uses_read_only_transaction_without_packages(receiver_case, monkeypatch):
    fixture = receiver_case
    activate(fixture)
    original_open = preparation.open_shared_ctx
    denied = []

    def authorizer(action, table, *_args):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            denied.append(table)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @asynccontextmanager
    async def readonly(*args, **kwargs):
        async with original_open(*args, **kwargs) as db:
            await db.set_authorizer(authorizer)
            yield db

    def no_files(*args, **kwargs):
        raise AssertionError("package read in Agent candidate projection")

    monkeypatch.setattr(preparation, "open_shared_ctx", readonly)
    monkeypatch.setattr(Path, "read_bytes", no_files)
    monkeypatch.setattr(Path, "read_text", no_files)
    result = await prepare(fixture, document=raw_document(fixture, skills=False), sources=None)
    assert result.expected.agent_id == "osa_exact" and result.skill_reads is None and result.skill_sources is None
    assert denied == []
    _assert_no_acceptance(fixture.case)


@pytest.mark.parametrize(
    "change", ["cutover", "assignment", "disabled", "access_deny", "membership", "revision", "release", "assignment_id", "scope", "database"]
)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_current_identity_and_authority_reject_before_reading_packages(receiver_case, change, monkeypatch, tmp_path):
    fixture = receiver_case
    activate(fixture)
    document = raw_document(fixture)
    with sqlite3.connect(fixture.case.fixture.db_path) as db:
        statements = {
            "cutover": "UPDATE team_subagent_schema_meta SET release_semantics_activated_at=NULL",
            "assignment": "UPDATE agent_assignment SET personal_state='disabled'",
            "disabled": "UPDATE team_subagent SET status='disabled'",
            "membership": "UPDATE team_members SET status='removed'",
        }
        if change in statements:
            db.execute(statements[change])
        elif change == "access_deny":
            db.execute(
                "INSERT INTO resource_grants(id,team_id,resource_kind,resource_id,principal_type,principal_id,permission,effect,created_at,updated_at) "
                "VALUES ('rg_reference_deny',?,'team_subagent','osa_exact','user',?,'use','deny','2026-09-14','2026-09-14')",
                (fixture.scope.team_id, fixture.scope.user_id),
            )
    for node in document.nodes:
        if isinstance(node, ReferenceNode) and node.resource.kind == "agent" and change in {"revision", "release", "assignment_id"}:
            object.__setattr__(node.resource, {"revision": "version", "release": "release_id"}.get(change, change), "stale_identity")
    patch = {}
    if change == "scope":
        patch["scope"] = fixture.scope.model_copy(update={"workspace_id": "ws_unrelated"})
    elif change == "database":
        fixture.sources.db_path = tmp_path / "another.db"
    calls = []

    async def no_prepare(*args, **kwargs):
        calls.append(args)
        raise AssertionError("unauthorized package preparation")

    monkeypatch.setattr(fixture.sources, "prepare", no_prepare)
    with pytest.raises(ReferenceStartPreparationError):
        await prepare(fixture, document=document, **patch)
    assert calls == []
    _assert_no_acceptance(fixture.case)


@pytest.mark.parametrize("receiver_case", ["admission_root_only"], indirect=True)
async def test_full_baseline_is_preserved_and_dependencies_cannot_escape_it(receiver_case, monkeypatch, request):
    fixture = receiver_case
    activate(fixture)
    original = fixture.sources.prepare
    seen = []

    async def capture(resource, **kwargs):
        seen.append(resource.id)
        return await original(resource, **kwargs)

    monkeypatch.setattr(fixture.sources, "prepare", capture)
    document = raw_document(fixture)
    if request.node.callspec.params["receiver_case"].endswith("root_only"):
        document = document.model_copy(
            update={"nodes": [node for node in document.nodes if not isinstance(node, ReferenceNode) or node.resource != fixture.child]}
        )
    with pytest.raises(ReferenceStartPreparationError):
        await prepare(fixture, document=document)
    assert seen == (["builtin:method"] if request.node.callspec.params["receiver_case"].endswith("root_only") else [])
    _assert_no_acceptance(fixture.case)


@pytest.mark.parametrize("change", ["assignment", "epoch", "unused_source"])
@pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True)
async def test_changes_during_file_preparation_reject_the_entire_candidate(receiver_case, monkeypatch, change):
    fixture = receiver_case
    activate(fixture)
    original = preparation.prepare_skill_read_closure

    async def changed(*args, **kwargs):
        closure = await original(*args, **kwargs)
        if change == "unused_source":
            await fixture.sources.instances.upsert(
                user_id=fixture.scope.user_id, name="unused", source_type="builtin", source_path=str(fixture.root / "unused"), enabled=False
            )
        else:
            with sqlite3.connect(fixture.case.fixture.db_path) as db:
                if change == "assignment":
                    # Same stable assignment, but a new current employment cycle.
                    db.execute("UPDATE agent_assignment SET row_version=row_version+1,current_cycle_started_at='2026-09-14T00:00:00+00:00'")
                else:
                    db.execute(
                        "INSERT INTO access_policy_state(team_id,epoch,updated_at) VALUES(?,1,'now') ON CONFLICT(team_id) DO UPDATE SET epoch=epoch+1",
                        (fixture.scope.team_id,),
                    )
        return closure

    monkeypatch.setattr(preparation, "prepare_skill_read_closure", changed)
    with pytest.raises(ReferenceStartPreparationError):
        await prepare(fixture)
    _assert_no_acceptance(fixture.case)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_input_mutation_during_first_await_cannot_replace_original_parameters(receiver_case, monkeypatch):
    fixture = receiver_case
    activate(fixture)
    document = raw_document(fixture)
    original = AgentExecutionBindingStore.project_reference_team_agent_in_connection

    async def changed(self, *args, **kwargs):
        document.nodes[0].parameters["year"] = 2030
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AgentExecutionBindingStore, "project_reference_team_agent_in_connection", changed)
    result = await prepare(fixture, document=document)
    assert result.input_document.nodes[0].parameters == {"year": 2026}
    assert document.nodes[0].parameters == {"year": 2030}


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_projection_requires_transaction_and_preserves_caller_factory(receiver_case):
    fixture = receiver_case
    activate(fixture)
    store = AgentExecutionBindingStore(fixture.case.fixture.db_path)
    resource = fixture.case.args["reference_start"].input_document.nodes[0].resource
    async with open_shared_ctx(fixture.case.fixture.db_path) as db:
        with pytest.raises(AgentExecutionBindingError, match="transaction_required"):
            await store.project_reference_team_agent_in_connection(db, scope=fixture.scope, resource=resource)
        await db.execute("BEGIN")
        original_factory = db.row_factory
        result = await store.project_reference_team_agent_in_connection(db, scope=fixture.scope, resource=resource)
        assert result.expected.revision_ref == resource.version and db.in_transaction and db.row_factory is original_factory
        await db.rollback()


@pytest.mark.parametrize("change", ["assignment", "unused_source"])
@pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True)
async def test_prepared_candidate_does_not_authorize_acceptance_after_revocation(receiver_case, change):
    fixture = receiver_case
    activate(fixture)
    prepared = await prepare(fixture)
    fixture.case.args.update(reference_start=prepared, user_text=prepared.input_document.display_text())
    if change == "assignment":
        with sqlite3.connect(fixture.case.fixture.db_path) as db:
            db.execute("UPDATE agent_assignment SET personal_state='disabled'")
    else:
        await fixture.sources.instances.upsert(
            user_id=fixture.scope.user_id, name="unused", source_type="builtin", source_path=str(fixture.root / "unused"), enabled=False
        )
    with pytest.raises((ValueError, AgentExecutionBindingError)):
        await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    _assert_no_acceptance(fixture.case)


@pytest.mark.parametrize("receiver_case", ["admission_selected_invoke", "admission_unused_invoke"], indirect=True)
async def test_invocation_grants_preserve_original_read_selection(receiver_case):
    fixture = receiver_case
    activate(fixture)
    document = raw_document(fixture)
    result = await prepare(fixture, document=document)
    assert result.input_document == document
    assert {item.proof.resource.id for item in result.skill_reads.items} == {"builtin:method", "builtin:support"}
    _assert_no_acceptance(fixture.case)
