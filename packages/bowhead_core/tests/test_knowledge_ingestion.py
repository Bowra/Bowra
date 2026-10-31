"""Atomic explicit membership, destination and removal/publication boundaries."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.ingestion import KnowledgeAction, KnowledgeWriteError


@pytest.fixture
async def members(tmp_path, initialize_migrated_kb_files_database):
    path = tmp_path / "kb_files.db"
    initialize_migrated_kb_files_database(path)
    store = KbFilesStore(path)
    await store.initialize()
    return store


def action(ref="button_1"):
    return KnowledgeAction(ref, "kb_ui", "u_1", "team_1", "ws_1")


def source():
    return {
        "artifact_id": "art_one",
        "filename": "Policy.md",
        "mime_type": "text/markdown",
        "size_bytes": 10,
        "scope": "personal",
        "scope_id": "u_1",
        "workspace_id": "ws_1",
        "team_id": "team_1",
        "uploaded_by": "u_1",
    }


async def test_concurrent_add_has_one_membership_and_one_parse_owner(members):
    results = await asyncio.gather(*[members.create_admitted(action=action(), source_fingerprint="sha:one", **source()) for _ in range(5)])
    assert len({item[0] for item in results}) == 1
    assert sum(item[1] for item in results) == 1
    assert len({item[2] for item in results}) == 1
    file = await members.get(results[0][0])
    assert file.ingestion_action_id == results[0][2]
    with sqlite3.connect(members._db_path) as db:
        assert db.execute("SELECT count(*) FROM kb_membership_operations").fetchone()[0] == 1


async def test_same_key_cannot_change_destination_or_bytes(members):
    await members.create_admitted(action=action(), source_fingerprint="sha:one", **source())
    for values, fingerprint in [({**source(), "scope": "team", "scope_id": "team_1"}, "sha:one"), (source(), "sha:changed")]:
        with pytest.raises(KnowledgeWriteError, match="intent_conflict"):
            await members.create_admitted(action=action(), source_fingerprint=fingerprint, **values)
    with pytest.raises(KnowledgeWriteError, match="destination_mismatch"):
        await members.create_admitted(action=action("button_2"), source_fingerprint="sha:one", **{**source(), "scope": "team", "scope_id": "team_1"})


async def test_removal_retires_replay_but_fresh_explicit_action_can_readd(members):
    file_id, _, operation_id = await members.create_admitted(action=action(), source_fingerprint="sha:one", **source())
    before = await members.get(file_id)
    removal = await members.remove_admitted(file_id, action=action("remove_1"))
    assert await members.remove_admitted(file_id, action=action("remove_1")) == removal
    assert (await members.get(file_id)).soft_deleted_at
    assert (await members.get(file_id)).version == before.version + 1
    assert (await members.get_operation(operation_id))["state"] == "removed"
    with pytest.raises(KnowledgeWriteError, match="operation_removed"):
        await members.create_admitted(action=action(), source_fingerprint="sha:one", **source())
    replacement, created, _ = await members.create_admitted(action=action("new_add"), source_fingerprint="sha:one", **source())
    assert replacement != file_id and created


async def test_closed_admission_never_revives_automatic_addition(members, monkeypatch):
    file_id, _, _ = await members.create_admitted(action=action(), source_fingerprint="sha:one", **source())
    monkeypatch.setenv("FEATURE_KNOWLEDGE_EXPLICIT_INGESTION", "0")
    with pytest.raises(KnowledgeWriteError, match="admission_closed"):
        await members.create_admitted(action=action("other"), source_fingerprint="sha:one", **source())
    await members.remove_admitted(file_id, action=action("remove"))
    assert (await members.get(file_id)).soft_deleted_at


async def test_bundle_cannot_create_children_after_parent_removal(members):
    parent, _, _ = await members.create_admitted(action=action(), source_fingerprint="zip", **{**source(), "artifact_id": "art_zip"})
    await members.remove_admitted(parent, action=action("cancel_bundle"))
    with pytest.raises(KnowledgeWriteError, match="bundle_operation_retired"):
        await members.create_admitted(action=action("child"), parent_file_id=parent, source_fingerprint="child", **source())
    assert await members.find_active_by_artifact("art_one") is None


async def test_removal_during_vector_write_never_publishes(members, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bowhead_core.kb.store import KbStore
    from bowhead_core.kb.worker import KbWorker

    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "0")
    path = tmp_path / "source.md"
    path.write_text("# Policy\n\nRevenue is 123 units.\n")
    fid, _, _ = await members.create_admitted(
        action=action(), source_fingerprint="source", **{**source(), "artifact_id": str(path), "size_bytes": path.stat().st_size}
    )
    index = KbStore(str(tmp_path / "chroma"))
    write = index.add_chunks

    async def remove_after_vector_write(file, chunks):
        await write(file, chunks)
        await members.remove_admitted(fid, action=action("remove_during_parse"))

    index.add_chunks = remove_after_vector_write
    worker = KbWorker(
        files_store=members,
        store=index,
        embedder=SimpleNamespace(embed_batch=AsyncMock(side_effect=lambda texts: [[0.1, 0.2, 0.3] for _ in texts])),
        ingestion_authorizer=AsyncMock(return_value=True),
    )
    await worker.parse_and_index(fid)
    current = await members.get(fid)
    assert current.soft_deleted_at
    assert current.active_generation_id is None
    with sqlite3.connect(members._db_path) as db:
        assert not db.execute("SELECT 1 FROM kb_file_generations WHERE state='published'").fetchone()


async def test_revoked_admission_fails_pending_work_without_model_calls(members, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bowhead_core.kb.store import KbStore
    from bowhead_core.kb.worker import KbWorker

    fid, _, _ = await members.create_admitted(action=action(), source_fingerprint="source", **source())
    embedder = SimpleNamespace(embed_batch=AsyncMock())
    worker = KbWorker(
        files_store=members, store=KbStore(str(tmp_path / "chroma")), embedder=embedder, ingestion_authorizer=AsyncMock(return_value=False)
    )
    await worker.parse_and_index(fid)
    file = await members.get(fid)
    assert file.parse_status == "failed"
    assert file.parse_error == "knowledge_current_authorization_denied"
    assert file.active_generation_id is None
    assert embedder.embed_batch.await_count == 0


async def test_stale_authorization_failure_does_not_overwrite_running_parse(members):
    fid, _, _ = await members.create_admitted(action=action(), source_fingerprint="source", **source())
    before = await members.get(fid)
    assert await members.claim_parse_start(fid)
    await members.fail_unstarted_ingestion(before, reason="knowledge_current_authorization_denied")
    current = await members.get(fid)
    assert current.parse_status == "parsing"
    assert current.parse_error is None
