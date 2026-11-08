"""Collaboration reads coexist with authority writers and retain current access checks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import close_pooled_readers, open_shared_ctx
from bowhead_core.agent_collaboration import CollaborationStore, CollaborationUnavailable
from bowhead_core.agent_collaboration_runtime import CollaborationRuntime
from test_agent_collaboration import CollaborationFixture
from test_agent_collaboration import collab as collab

from bowhead_core import sqlite_capacity

pytestmark = pytest.mark.integration


@pytest.fixture
async def split_collab(collab: CollaborationFixture) -> AsyncIterator[CollaborationFixture]:
    await close_pooled_readers()
    baseline = sqlite_capacity._used
    await asyncio.to_thread(upgrade, f"sqlite:///{collab.path}")
    collab.store = CollaborationStore(collab.path, cipher=collab.store.cipher, clock=lambda: collab.clock[0])
    try:
        yield collab
    finally:
        await close_pooled_readers()
        assert sqlite_capacity._used == baseline, "Collaboration reads must release their physical connections after pool cleanup"


async def read_collaboration(fixture: CollaborationFixture, method: str, question: dict) -> dict | list[dict] | None:
    arguments = {}
    if method in {"roster", "inbox"}:
        arguments["execution_id"] = fixture.recipient
    if method in {"roster", "inbox", "read", "projection"}:
        arguments["authorize"] = fixture.authorize
    if method == "read":
        arguments["operation_id"] = question["operation_id"]
    return await getattr(fixture.store, method)(fixture.worker, **arguments)


@pytest.mark.parametrize("method", ["admission", "roster", "inbox", "read", "projection", "pending_publications"])
async def test_reads_coexist_with_revocation_and_observe_its_commit(split_collab: CollaborationFixture, method: str) -> None:
    fixture = split_collab
    question = await fixture.ask(required=False)
    expected = await read_collaboration(fixture, method, question)
    assert expected
    async with open_shared_ctx(str(fixture.path)) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        try:
            await writer.execute(
                "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?",
                (fixture.worker.workspace_id, fixture.worker.user_id),
            )
            assert await asyncio.wait_for(read_collaboration(fixture, method, question), timeout=2) == expected
            await writer.commit()
        finally:
            await writer.rollback()
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        await read_collaboration(fixture, method, question)
    with pytest.raises(CollaborationUnavailable, match="collaboration_worker_unavailable"):
        await fixture.ask(identity=fixture.identity(step="after-revocation"))
    assert await fixture.count() == 1


async def test_cancelled_inbox_read_preserves_delivery_cursor_and_write_fence(split_collab: CollaborationFixture) -> None:
    fixture = split_collab
    question = await fixture.ask(recipient_id=fixture.lead, required=False)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def paused_authorize(db, worker, sender, recipient, content) -> None:
        await fixture.authorize(db, worker, sender, recipient, content)
        if content["body"] == "Which refund definition applies?":
            entered.set()
            await blocked.wait()

    runtime = CollaborationRuntime(fixture.store, fixture.worker, authorize=paused_authorize)
    reading = asyncio.create_task(runtime.context(fixture.lead))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        async with open_shared_ctx(str(fixture.path)) as authority_writer:
            await authority_writer.execute(
                "INSERT INTO access_policy_state(team_id,epoch,updated_at) VALUES(?,1,?) "
                "ON CONFLICT(team_id) DO UPDATE SET epoch=epoch+1,updated_at=excluded.updated_at",
                (fixture.worker.team_id, "2026-11-07T00:00:00+00:00"),
            )
            await authority_writer.commit()
        async with open_shared_ctx(fixture.store.db_path) as writer:
            await writer.execute(
                "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id=?",
                ("2026-11-07T00:00:00+00:00", fixture.worker.turn_id),
            )
            await writer.commit()
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
    finally:
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
    runtime.authorize = fixture.authorize
    context = await runtime.context(fixture.lead)
    assert [item["operation_id"] for item in context["items"]] == [question["operation_id"]]
    assert runtime._cursors == {}
    with pytest.raises(CollaborationUnavailable, match="collaboration_worker_unavailable"):
        await fixture.store.complete(fixture.worker, execution_id=fixture.lead)
    async with open_shared_ctx(str(fixture.path)) as writer:
        await writer.execute(
            "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (fixture.worker.workspace_id, fixture.worker.user_id),
        )
        await writer.commit()
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        await runtime.context(fixture.lead)
    assert await fixture.count() == 1
