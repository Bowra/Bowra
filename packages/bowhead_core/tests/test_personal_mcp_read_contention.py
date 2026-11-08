"""Normal confirmation polling must not fence unrelated document retention writes."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import close_pooled_readers
from bowhead_core.artifact_store import ArtifactStore
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.personal_mcp import PersonalMcpStore
from test_document_store import SCOPE, Context
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

from bowhead_core import sqlite_capacity, sqlite_domain_runtime

pytestmark = pytest.mark.integration


@pytest.fixture
async def split_document(ctx: Context) -> AsyncIterator[SimpleNamespace]:
    receipt = await ctx.submit()
    await close_pooled_readers()
    baseline = sqlite_capacity._used
    path = ctx.path.with_name("bowhead.db")

    def migrate() -> None:
        with closing(sqlite3.connect(ctx.path)) as source, closing(sqlite3.connect(path)) as destination:
            source.backup(destination)
        upgrade(f"sqlite:///{path}")

    await asyncio.to_thread(migrate)
    try:
        yield SimpleNamespace(path=path, job_id=receipt.job_id, artifacts=ArtifactStore(path, ctx.artifacts.backend))
    finally:
        await close_pooled_readers()
        assert sqlite_capacity._used == baseline


@pytest.mark.parametrize("cancel_reader", [False, True])
async def test_pending_confirmation_read_allows_input_freeze_without_authority_retry(
    split_document: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, cancel_reader: bool
) -> None:
    case = split_document
    entered, release = asyncio.Event(), asyncio.Event()
    fetchall = aiosqlite.Cursor.fetchall
    reading = None
    conflicts = []
    original_busy = sqlite_domain_runtime._busy

    async def pause_pending_result(cursor):
        if asyncio.current_task() is reading:
            entered.set()
            await release.wait()
        return await fetchall(cursor)

    def observe_conflict():
        error = original_busy()
        conflicts.append(error)
        return error

    monkeypatch.setattr(aiosqlite.Cursor, "fetchall", pause_pending_result)
    monkeypatch.setattr(sqlite_domain_runtime, "_busy", observe_conflict)
    registry = PersonalMcpStore(case.path)
    reading = asyncio.create_task(registry.pending(SCOPE.user_id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        frozen = await DocumentInputStore(case.artifacts).freeze(scope=SCOPE, job_id=case.job_id)
        assert len(frozen) == 1 and not reading.done()
        assert conflicts == [], "A pure confirmation read must not require authority-transaction retries"
        if cancel_reader:
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
        else:
            release.set()
            assert await reading == []
    finally:
        release.set()
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
    assert await registry.pending(SCOPE.user_id) == []
