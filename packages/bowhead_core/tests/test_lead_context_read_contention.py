"""Lead context reads and the reference queue list coexist with an exclusive core writer.

A reproduced task failed at Lead start because ``ExecutionContextView.restore`` ran its owner
check inside a transaction on a write-capable connection. The check reads core, so beside a connection holding
core's exclusive lease it was refused at once. ``GET …/reference-queue`` answered 503 the same way. These reads
now run on the read-only pool, which takes no authority lease and still decides on the last commit.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.context_execution import ExecutionContextView
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domains import domain_database_path
from test_agent_collaboration import CollaborationFixture
from test_agent_collaboration import collab as collab
from test_agent_collaboration_read_contention import _hold_exclusive_core_lease
from test_agent_collaboration_read_contention import split_collab as split_collab

pytestmark = pytest.mark.integration


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def _view(fixture: CollaborationFixture) -> ExecutionContextView:
    # Production passes ``SessionStore.db_path``, which resolves to the execution file.
    return ExecutionContextView(
        db_path=domain_database_path(fixture.path, "execution"),
        worker=fixture.worker,
        execution_id="exe_lead_read_contention",
        lane="ordinary",
        cipher=fixture.store.cipher,
        validate_sources=_noop,
        activity=_noop,
    )


async def _beside_core_writer(fixture: CollaborationFixture, seconds: float, work) -> float:
    holding = asyncio.Event()
    holder = asyncio.create_task(_hold_exclusive_core_lease(fixture, holding, seconds))
    await holding.wait()
    started = time.monotonic()
    try:
        await asyncio.wait_for(work(), timeout=seconds * 2)
        return time.monotonic() - started
    finally:
        await holder


async def test_lead_context_restore_finishes_while_core_is_written(split_collab: CollaborationFixture) -> None:
    elapsed = await _beside_core_writer(split_collab, 1.0, _view(split_collab).restore)
    # Neither refused nor waiting for the writer: it reads the last commit at once.
    assert elapsed < 0.5


async def test_lead_owner_check_finishes_while_core_is_written(split_collab: CollaborationFixture) -> None:
    elapsed = await _beside_core_writer(split_collab, 1.0, _view(split_collab).require_owner)
    assert elapsed < 0.5


async def test_lead_context_reads_still_refuse_a_committed_revocation(split_collab: CollaborationFixture) -> None:
    fixture = split_collab
    await _view(fixture).restore()
    async with open_shared_ctx(str(fixture.path)) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        await writer.execute(
            "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (fixture.worker.workspace_id, fixture.worker.user_id),
        )
        await writer.commit()
    with pytest.raises(ValueError):
        await _view(fixture).restore()
    with pytest.raises(ValueError):
        await _view(fixture).require_owner()


async def test_reference_queue_list_reads_while_core_is_written(split_collab: CollaborationFixture) -> None:
    fixture = split_collab
    sessions = SessionStore(fixture.path)

    async def listed() -> None:
        assert (
            await sessions.list_queued_reference_submissions(
                team_id=fixture.worker.team_id,
                user_id=fixture.worker.user_id,
                workspace_id=fixture.worker.workspace_id,
                session_id="s_without_queue",
                auth_session_generation_hash="unused",
                reference_store=None,
            )
            == ()
        )

    elapsed = await _beside_core_writer(fixture, 1.0, listed)
    assert elapsed < 0.5
