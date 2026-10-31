"""Real-worker context adoption, isolation, cleanup and source-access checks."""

import json
from uuid import uuid4

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.context_store import (
    ContextCompletion,
    ContextOrigin,
    ContextStoreError,
    publish_conversation_context_in_tx,
    read_context_generation_in_tx,
    write_context_in_tx,
)
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from bowhead_router.context_policy import ContextPolicy
from bowhead_router.types import Message
from test_agent_collaboration import collab as collaboration_fixture

collab = collaboration_fixture
pytestmark = pytest.mark.integration


async def readable(_messages):
    return None


def arguments(case, **changes):
    return {
        "worker": case.worker,
        "execution_id": case.requester,
        "lane": "ordinary",
        "expected_generation": None,
        "messages": [Message(role="user", content="Keep the exact source /policy/refunds.")],
        "policy": ContextPolicy(),
        "origin": ContextOrigin(provider="provider", model="model", configuration_version=1),
        "cipher": case.store.cipher,
        "validate_sources": readable,
        **changes,
    }


async def write(case, **changes):
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        result = await write_context_in_tx(db, **arguments(case, **changes))
        await db.commit()
        return result


async def read(case, identity, **changes):
    args = {
        key: value for key, value in arguments(case, **changes).items() if key in {"worker", "execution_id", "lane", "cipher", "validate_sources"}
    }
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN")
        return await read_context_generation_in_tx(db, generation_id=identity, **args)


async def test_compact_adopts_only_committed_generation_and_retries_identically(collab):
    source = await write(collab)
    compact = ContextCompletion(operation_id="mco_" + uuid4().hex, trigger="threshold", input_before=1000, input_after=600)
    candidate = [Message(role="user", content="Historical summary.")]
    args = {"expected_generation": source, "messages": candidate, "completion": compact}
    adopted = await write(collab, **args)
    assert await write(collab, **args) == adopted
    restored = await read(collab, adopted)
    assert restored.messages == candidate
    assert restored.source_refs == (source,)
    with pytest.raises(ContextStoreError, match="operation_changed"):
        await write(collab, **{**args, "messages": [Message(role="user", content="Changed retry.")]})
    with pytest.raises(ContextStoreError, match="head_changed"):
        await write(collab, expected_generation=source)
    async with (
        open_shared_ctx(collab.path) as db,
        db.execute("SELECT completion_json FROM model_context_generations WHERE id=?", (adopted,)) as cursor,
    ):
        assert json.loads((await cursor.fetchone())[0])["operation_id"] == compact.operation_id


async def test_failed_transaction_keeps_prior_head_and_no_completion(collab):
    source = await write(collab)
    compact = ContextCompletion(operation_id="mco_" + uuid4().hex, trigger="capacity", input_before=1000, input_after=600)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        candidate = await write_context_in_tx(db, **arguments(collab, expected_generation=source, completion=compact))
        await db.rollback()
        async with db.execute("SELECT generation_id FROM model_context_heads WHERE execution_id=?", (collab.requester,)) as cursor:
            assert (await cursor.fetchone())[0] == source
        async with db.execute("SELECT id FROM model_context_generations WHERE id=?", (candidate,)) as cursor:
            assert await cursor.fetchone() is None


@pytest.mark.parametrize("change", ["sibling", "lane", "worker", "session"])
async def test_context_read_cannot_cross_execution_lane_or_worker(collab, change):
    source = await write(collab)
    changed = {
        "sibling": {"execution_id": collab.recipient},
        "lane": {"lane": "coordination"},
        "worker": {"worker": collab.worker.model_copy(update={"generation": 2})},
        "session": {"worker": collab.worker.model_copy(update={"session_id": "s_other"})},
    }[change]
    with pytest.raises((ContextStoreError, OrdinaryContinuationUnavailable)):
        await read(collab, source, **changed)


async def test_source_revocation_rechecks_originals_after_repeated_summaries(collab):
    source = await write(collab)
    compact = ContextCompletion(operation_id="mco_" + uuid4().hex, trigger="threshold", input_before=1000, input_after=600)
    summary = await write(
        collab, expected_generation=source, completion=compact, messages=[Message(role="user", content="Summary without the source identifier.")]
    )
    checkpoint = await write(collab, expected_generation=summary, messages=[Message(role="user", content="Continued work.")])

    async def revoked(messages):
        if any("/policy/refunds" in message.content for message in messages):
            raise PermissionError("source_revoked")

    with pytest.raises(PermissionError, match="source_revoked"):
        await read(collab, checkpoint, validate_sources=revoked)
    with pytest.raises(PermissionError, match="source_revoked"):
        await write(collab, expected_generation=checkpoint, validate_sources=revoked)


@pytest.mark.parametrize("generation_count", [30, 130])
async def test_long_ancestry_revalidates_every_generation_without_per_generation_database_round_trips(collab, generation_count):
    head = None
    expected = []
    for index in range(generation_count):
        content = f"Historical source {index}"
        expected.append(content)
        head = await write(collab, expected_generation=head, messages=[Message(role="user", content=content)])
    validated = []

    async def validate(messages):
        validated.extend(message.content for message in messages)

    statements = []
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.set_trace_callback(statements.append)
        try:
            result = await read_context_generation_in_tx(
                db,
                worker=collab.worker,
                execution_id=collab.requester,
                lane="ordinary",
                generation_id=head,
                cipher=collab.store.cipher,
                validate_sources=validate,
            )
        finally:
            await db.set_trace_callback(None)
            await db.rollback()
    assert result.messages == [Message(role="user", content=expected[-1])]
    assert sorted(validated) == sorted(expected)
    context_reads = [sql for sql in statements if "model_context_generations" in sql or "model_context_segments" in sql]
    assert len(context_reads) <= 4, "Historical source validation must not schedule a database round trip for every generation"


async def test_accepted_stop_blocks_context_adoption(collab):
    source = await write(collab)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id=?", ("2026-10-29", collab.worker.turn_id))
        await db.commit()
    with pytest.raises(OrdinaryContinuationUnavailable):
        await write(collab, expected_generation=source)


async def test_only_root_lead_can_publish_current_ordinary_view(collab):
    child = await write(collab)
    lead = await write(collab, execution_id=collab.lead)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        args = {"worker": collab.worker, "expected_conversation_generation": None, "cipher": collab.store.cipher, "validate_sources": readable}
        with pytest.raises(ContextStoreError, match="publisher_unavailable"):
            await publish_conversation_context_in_tx(db, execution_id=collab.requester, generation_id=child, **args)
        await publish_conversation_context_in_tx(db, execution_id=collab.lead, generation_id=lead, **args)
        await db.commit()
    with pytest.raises(ContextStoreError, match="owner_mismatch"):
        await read(collab, lead)


async def test_session_soft_deletion_purges_encrypted_projections(collab):
    await write(collab)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE sessions SET deleted_at=? WHERE id=?", ("2026-10-29", collab.worker.session_id))
        await db.commit()
        for table in ("model_context_segments", "model_context_generations", "model_context_heads", "model_context_conversation_heads"):
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0


async def test_account_erasure_includes_context_projections(collab, tmp_path):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    from bowhead_core.account_deletion_ledger import AccountDeletionLedger
    from bowhead_core.account_deletion_service import AccountDeletionService
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
    from bowhead_core.session_store import SessionStore
    from bowhead_core.terminal_protocol_store import TerminalProtocolStore
    from bowhead_core.user_store import UserStore

    await write(collab)
    users = UserStore(collab.path)
    replacement = await users.get_user_by_email("owner@example.com")
    await users.add_member(workspace_id=collab.worker.workspace_id, user_id=replacement.id, role="admin")
    ledger = AccountDeletionLedger(tmp_path / "deletions.db", secret="synthetic-instance-secret")
    await ledger.initialize()
    service = AccountDeletionService(
        UserStore(collab.path),
        lifecycle=SessionLifecycleCoordinator(SessionStore(collab.path), TerminalProtocolStore(collab.path)),
        artifacts=ArtifactStore(db_path=collab.path, backend=LocalBackend(tmp_path / "blobs")),
        ledger=ledger,
        secret="synthetic-instance-secret",
        external_cleanup=AsyncMock(),
        clock=lambda: datetime.now(UTC) - timedelta(days=31),
    )
    await service.request(collab.worker.user_id)
    service.clock = lambda: datetime.now(UTC)
    assert await service.run_once() == {"completed": 1, "failed": 0}
    async with open_shared_ctx(collab.path) as db:
        for table in ("model_context_segments", "model_context_generations", "model_context_heads", "model_context_conversation_heads"):
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0
        async with db.execute("PRAGMA foreign_key_check") as cursor:
            assert await cursor.fetchall() == []
