"""Writer transactions on another domain read core's current authority, and wait for a short core write.

116 (1.7.62, 2026-11-22): beside Space and group admin writes, a Lead start (``admit_system_execution``),
a sandbox upload (``put_bytes``) and the request recorder failed at once inside ``BEGIN IMMEDIATE``
when they read core. Waiting there would have read data from before the write they waited for, because
SQLite starts a snapshot of every read-only attached file at ``BEGIN IMMEDIATE``. The same snapshot let
a revocation committed between BEGIN and the first core read go unseen. Writer transactions now take
their writer lock through a gate write on the real migrated layout.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.sqlite_domains import domain_database_path
from test_agent_collaboration import CollaborationFixture
from test_agent_collaboration import collab as collab
from test_agent_collaboration_read_contention import split_collab as split_collab

pytestmark = pytest.mark.integration


def _scope(fixture: CollaborationFixture) -> ReferenceRequestScope:
    return ReferenceRequestScope(team_id=fixture.worker.team_id, user_id=fixture.worker.user_id, workspace_id=fixture.worker.workspace_id)


async def _write_core(fixture: CollaborationFixture, holding: asyncio.Event | None, seconds: float, *, revoke: bool) -> None:
    async with open_shared_ctx(str(fixture.path)) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        if revoke:
            await writer.execute(
                "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?",
                (fixture.worker.workspace_id, fixture.worker.user_id),
            )
        else:
            await writer.execute("UPDATE workspaces SET status='archived' WHERE id=?", (fixture.worker.workspace_id,))
        assert writer._conn._leases.get("core") == 2
        if holding is not None:
            holding.set()
        await asyncio.sleep(seconds)
        if revoke:
            await writer.commit()
        else:
            await writer.rollback()


async def test_a_revocation_committed_after_begin_immediate_is_seen(split_collab: CollaborationFixture) -> None:
    fixture = split_collab
    async with open_shared_ctx(str(domain_database_path(fixture.path, "execution"))) as db:
        await require_reference_request_scope(db, _scope(fixture))
        await db.commit()
        await db.execute("BEGIN IMMEDIATE")
        # Nothing fences core yet, so the revocation commits now.
        await _write_core(fixture, None, 0, revoke=True)
        with pytest.raises(ValueError, match="reference_scope_unavailable"):
            await require_reference_request_scope(db, _scope(fixture))
        await db.rollback()


@pytest.mark.parametrize("revoke", [False, True])
async def test_a_writer_transaction_waits_for_a_short_core_write(split_collab: CollaborationFixture, revoke: bool) -> None:
    fixture = split_collab
    holding = asyncio.Event()
    holder = asyncio.create_task(_write_core(fixture, holding, 0.3, revoke=revoke))
    await holding.wait()
    started = time.monotonic()
    try:
        async with open_shared_ctx(str(domain_database_path(fixture.path, "execution"))) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if revoke:
                    # The wait ends with the revocation committed; the read sees it.
                    with pytest.raises(ValueError, match="reference_scope_unavailable"):
                        await require_reference_request_scope(db, _scope(fixture))
                else:
                    await require_reference_request_scope(db, _scope(fixture))
            finally:
                await db.rollback()
    finally:
        await holder
    assert 0.2 < time.monotonic() - started < 1.0
