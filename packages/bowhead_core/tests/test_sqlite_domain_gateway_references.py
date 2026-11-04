"""Gateway reference preparation and queued restoration span managed domains."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import anyio
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import close_pooled_readers
from bowhead_core.access_control import AccessControlStore
from bowhead_core.chat_reference_storage import ChatReferenceStore, QueuedReferenceInput, ReferenceCipher, ReferenceScope
from bowhead_core.session_store import SessionStore
from bowhead_core.skill_read_resolution import SkillReadSourceResolver
from core_legacy_database import upgrade as upgrade_legacy
from cryptography.fernet import Fernet
from test_reference_mcp_preparation import _case as mcp_case
from test_reference_mcp_preparation import _document as mcp_document
from test_reference_start_admission import start_case as start_case
from test_reference_start_preparation import activate, raw_document
from test_skill_read_receiver_integration import receiver_case as receiver_case

from bowhead_gateway import chat_reference_start, turn_service

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _migrate(path, monkeypatch):
    await close_pooled_readers()
    await anyio.to_thread.run_sync(lambda: upgrade_legacy(f"sqlite:///{path}"))
    await anyio.to_thread.run_sync(lambda: upgrade(f"sqlite:///{path}"))
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    return SessionStore(path)


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_split_gateway_skill_prepare_and_queued_restore(receiver_case, monkeypatch):
    case = receiver_case
    activate(case)
    store = await _migrate(case.case.fixture.db_path, monkeypatch)
    case.case.store = store
    scope = case.scope
    orchestrator = SimpleNamespace(session_store=store, reference_skill_sources_factory=lambda _: case.sources, subagent_runner=SimpleNamespace())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator, session_store=store)))
    document = raw_document(case)
    prepared = await chat_reference_start.prepare_reference_http_start(
        request,
        document,
        team_id=scope.team_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        requested_session_id=None,
        preferred_provider=None,
        preferred_model=None,
    )
    assert len(prepared.skill_reads.items) == 2
    session_id, parent, child = "s_domain_queue", "t_domain_parent", "t_domain_child"
    await store.ensure_session(session_id=session_id, workspace_id=scope.workspace_id, user_id=scope.user_id)
    await store.record_turn(turn_id=parent, session_id=session_id, user_text="parent")
    assert await store.claim_chat_spawn_dispatch_for_worker(turn_id=parent, owner_id="domain-worker") == "claimed"
    prepared = await chat_reference_start.prepare_reference_http_start(
        request,
        document,
        team_id=scope.team_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        requested_session_id=session_id,
        entry_mode="steer",
        preferred_provider=None,
        preferred_model=None,
    )
    common = dict(case.case.common)
    common.update(
        client_submission_id=str(uuid4()),
        requested_session_id=session_id,
        submission_kind="steer",
        request_fingerprint="a" * 64,
        accepted_request_fingerprints=("a" * 64,),
    )
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"
    queued = QueuedReferenceInput.model_validate(
        {"schema_version": 1, "input_document": document.model_dump(mode="json"), "attachment_ids": [], "attachment_refs": []}
    )
    cipher = ReferenceCipher(os.environ["BOWHEAD_FERNET_KEY"])
    receipt = await store.finalize_steer_chat_submission(
        team_id=scope.team_id,
        user_id=scope.user_id,
        client_submission_id=common["client_submission_id"],
        workspace_id=scope.workspace_id,
        requested_session_id=session_id,
        expected_parent_turn_id=parent,
        expected_execution_generation=1,
        text=document.display_text(),
        attachment_ids=[],
        submission_contract_version="chat-references.v1",
        model_control_batch="references",
        request_fingerprint="a" * 64,
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        reference_store=ChatReferenceStore(cipher),
        reference_scope=ReferenceScope(**scope.model_copy(update={"session_id": session_id}).model_dump(), policy_epoch=prepared.policy_epoch),
        reference_input=queued,
    )
    assert receipt.outcome == "buffered"
    restored = await turn_service.materialize_reference_steer_head(
        orchestrator,
        session_id=session_id,
        current_turn_id=parent,
        next_turn_id=child,
        team_id=scope.team_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    assert restored is not None
    assert await store.get_turn(child) is not None


async def test_split_gateway_mcp_preparation_accepts_core_authority_and_rejects_other_installation(tmp_path, monkeypatch):
    case, scope, agent, index, _source, read, _write, access = await mcp_case(tmp_path)
    store = await _migrate(case.fixture.db_path, monkeypatch)
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP", "1")

    async def unused_access():
        pytest.fail("MCP-only preparation must not resolve Skill access")

    sources = SkillReadSourceResolver(case.fixture.db_path, access_factory=unused_access, builtin_index=lambda: None)
    state = SimpleNamespace(
        orchestrator=SimpleNamespace(session_store=store, reference_skill_sources_factory=lambda _: sources, subagent_runner=SimpleNamespace()),
        session_store=store,
        access_control_store=access,
        reference_mcp_metadata=SimpleNamespace(current=lambda: index),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    document = mcp_document(agent, (read.resource, "Read margin", {"year": 2025}))
    assert chat_reference_start.reference_mcp_writer_ready(request)
    prepared = await chat_reference_start.prepare_reference_http_start(
        request,
        document,
        team_id=scope.team_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        requested_session_id=None,
        preferred_provider=None,
        preferred_model=None,
    )
    assert prepared.mcp_selection is not None and prepared.mcp_access is access
    foreign = tmp_path / "foreign" / "bowhead.db"
    state.access_control_store = AccessControlStore(foreign)
    assert not chat_reference_start.reference_mcp_writer_ready(request)
    state.access_control_store = access
    state.reference_mcp_metadata = SimpleNamespace(current=lambda: replace(index, db_path=Path(foreign)))
    assert not chat_reference_start.reference_mcp_writer_ready(request)
