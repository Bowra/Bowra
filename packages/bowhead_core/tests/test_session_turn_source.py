"""Ordinary Turn writes atomically preserve the source used by document admission."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.chat_turn_source import ChatTurnUserSource, current_attachment_positions
from bowhead_core.session_store import SessionStore
from test_document_store import migrated_db as migrated_db

from bowhead_core import session_store as module

pytestmark = pytest.mark.integration


@pytest.fixture
async def store(tmp_path, migrated_db):
    path = tmp_path / "core.db"
    with closing(sqlite3.connect(migrated_db)) as original, closing(sqlite3.connect(path)) as target:
        original.backup(target)
    result = SessionStore(path)
    await result.ensure_session(session_id="s_source", workspace_id="ws_source", user_id="u_source")
    return result


@pytest.fixture
async def source(store):
    artifacts = ArtifactStore(db_path=store.db_path, backend=LocalBackend(Path(store.db_path).parent / "blobs"))
    ids = []
    for filename in ("data-a.json", "data-b.json", "template.pptx"):
        artifact = await artifacts.put_bytes(content=b"fixture", filename=filename, mime="application/octet-stream", created_by="u_source")
        ids.append(artifact.id)
    return ChatTurnUserSource(
        turn_id="t_source",
        session_id="s_source",
        user_id="u_source",
        workspace_id="ws_source",
        user_text="The first attachment is data. The second is the template.",
        attachment_ids=tuple(ids),
        attachment_refs=({"fileId": "art_bundle", "bundleArtifactIds": ids[:2]}, {"fileId": ids[2]}),
        parts=None,
    )


async def record(store, source):
    return await store.record_turn(turn_id=source.turn_id, session_id=source.session_id, user_text=source.user_text, user_source=source)


async def read(store):
    return await store.get_chat_turn_user_source(turn_id="t_source", user_id="u_source", workspace_id="ws_source")


async def test_turn_and_bundle_source_survive_store_reopen_and_exact_replay(store, source):
    first = await record(store, source)
    reopened = SessionStore(store.db_path)
    saved = await read(reopened)
    assert saved == source
    assert current_attachment_positions(saved) == dict(zip(source.attachment_ids, (1, 1, 2), strict=True))
    replay = await record(reopened, source)
    assert replay.id == first.id and replay.seq == first.seq
    assert await read(reopened) == source


async def test_source_failure_rolls_back_the_turn_before_worker_dispatch(store, source, monkeypatch):
    async def fail_source(*args, **kwargs):
        raise RuntimeError("injected source write failure")

    monkeypatch.setattr(module, "_insert_chat_turn_user_source_in_tx", fail_source)
    with pytest.raises(RuntimeError, match="injected source write failure"):
        await record(store, source)
    assert await store.get_turn(source.turn_id) is None
    assert await read(store) is None
    assert await store.get_active_turn(source.session_id) is None


@pytest.mark.parametrize("change", [{"user_id": "u_other"}, {"workspace_id": "ws_other"}])
async def test_source_scope_mismatch_cannot_commit_a_turn(store, source, change):
    with pytest.raises(ValueError, match="chat_turn_user_source_scope_conflict"):
        await record(store, replace(source, **change))
    assert await store.get_turn(source.turn_id) is None


@pytest.mark.parametrize("field", ["attachment_ids", "attachment_refs", "user_id", "workspace_id"])
async def test_replay_cannot_replace_the_source_identity(store, source, field):
    await record(store, source)
    changed = replace(source, **{field: () if field.startswith("attachment") else "other"})
    with pytest.raises(ValueError, match="chat_turn_user_source_conflict"):
        await record(store, changed)
    assert await read(store) == source


async def test_old_turn_without_source_cannot_be_backfilled_by_a_later_request(store, source):
    await store.record_turn(turn_id=source.turn_id, session_id=source.session_id, user_text=source.user_text)
    with pytest.raises(ValueError, match="chat_turn_user_source_conflict"):
        await record(store, source)
    assert await read(store) is None
    assert await store.get_turn(source.turn_id) is not None


async def test_exact_replay_does_not_repair_historical_attachment_associations(store, source):
    await record(store, source)
    with closing(sqlite3.connect(store.db_path)) as db:
        db.execute("DELETE FROM turn_attachments WHERE turn_id=?", (source.turn_id,))
        db.commit()
    await record(store, source)
    assert await store.list_turn_attachments(turn_id=source.turn_id) == []
    assert await read(store) == source


async def test_source_outliving_removed_inputs_preserves_identity_without_restoring_access(store, source):
    with closing(sqlite3.connect(store.db_path)) as db:
        db.execute("UPDATE artifacts SET deleted_at='2026-10-03' WHERE id=?", (source.attachment_ids[0],))
        db.execute("DELETE FROM artifacts WHERE id=?", (source.attachment_ids[1],))
        db.commit()
    await record(store, source)
    assert await store.list_turn_attachments(turn_id=source.turn_id) == [source.attachment_ids[2]]
    assert await read(store) == source
