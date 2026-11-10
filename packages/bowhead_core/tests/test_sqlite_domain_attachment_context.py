"""Model input proofs share installation identity across execution and content files."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import anyio
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.context_attachments import ContextAttachment, _authorize
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from bowhead_core.ordinary_lead_input_context import session_input_artifact
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_runtime import connect as diagnostic_connect
from core_legacy_database import upgrade as upgrade_legacy


@pytest.fixture(scope="module")
def context_case(tmp_path_factory):
    directory = tmp_path_factory.mktemp("attachment-domains")
    core = directory / "bowhead.db"
    backend = LocalBackend(directory / "blobs")
    upgrade_legacy(f"sqlite:///{core}")
    with sqlite3.connect(core) as db:
        db.executescript(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active','t');"
            "INSERT INTO users(id,team_id,email,created_at) VALUES ('u_1','team_1','user@example.test','t');"
            "INSERT INTO team_members(id,user_id,team_id,role,created_at) VALUES ('tm_1','u_1','team_1','member','t');"
            "INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at,revision) VALUES ('ws_1','team_1','Space','active','t','t',1);"
            "INSERT INTO workspace_members(workspace_id,user_id,joined_at) VALUES ('ws_1','u_1','t');"
            "INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents) VALUES ('s_1','ws_1','u_1','Session','t','t','active',0,0,'[]');"
        )
    legacy = ArtifactStore(core, backend)

    async def write():
        await legacy.initialize()
        return await legacy.put_bytes(
            content=b"retained input",
            filename="source.txt",
            mime="text/plain",
            created_by="u_1",
            workspace_id="ws_1",
            session_id="s_1",
            bind_chat_upload=True,
        )

    art = anyio.run(write)
    upgrade(f"sqlite:///{core}")
    ctx = SimpleNamespace(
        turn_id="turn_current",
        session_id="s_1",
        user_id="u_1",
        workspace_id="ws_1",
        session_store=SessionStore(core),
        artifact_store=ArtifactStore(core, backend),
    )
    source = ChatTurnUserSource(
        turn_id=ctx.turn_id,
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        user_text="Inspect",
        attachment_ids=(art.id,),
        attachment_refs=({"fileId": art.id, "filename": art.filename, "mime": art.mime, "size": art.size},),
        parts=None,
    )
    yield directory, ctx, source, art


@pytest.mark.asyncio
async def test_split_attachment_proofs_authorize_and_hold_content_fence_through_caller_transaction(context_case):
    _directory, ctx, source, art = context_case
    file = ContextAttachment(artifact_id=art.id, filename=art.filename, mime=art.mime, size=art.size)
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert [item.id for item in await _authorize(ctx, db, source, [file])] == [art.id]
        restored, provenance = await session_input_artifact(ctx, db, art.id)
        assert restored.id == art.id and provenance == "upload"
        async with diagnostic_connect(ctx.artifact_store.db_path) as concurrent:
            with pytest.raises(sqlite3.OperationalError) as error:
                await concurrent.execute("UPDATE artifacts SET deleted_at='later' WHERE id=?", (art.id,))
            assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            await concurrent.rollback()
        await db.rollback()


@pytest.mark.asyncio
async def test_attachment_context_rejects_foreign_installation_before_reading(context_case, tmp_path):
    _directory, original, source, art = context_case
    ctx = SimpleNamespace(**vars(original))
    ctx.artifact_store = ArtifactStore(tmp_path / "foreign" / "bowhead_content.db", LocalBackend(tmp_path / "foreign-blobs"))
    file = ContextAttachment(artifact_id=art.id, filename=art.filename, mime=art.mime, size=art.size)
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN")
        with pytest.raises(ValueError, match="context_attachment_store_unavailable"):
            await _authorize(ctx, db, source, [file])
        with pytest.raises(OrdinaryContinuationUnavailable):
            await session_input_artifact(ctx, db, art.id)
        await db.rollback()
