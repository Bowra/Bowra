"""KbFilesStore unit tests.

Covers:
- create / get / list / update / soft_delete / hard_delete
- P0-P1: workspace_id / team_id 字段 round trip
- P0-P2: update_scope 改 4 个 routing 字段
- P1-N3: list_soft_deleted_before for grace period cron
- P1-O6 + P1-S3: workspace_id index path
- update() whitelist 拒绝未知字段
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core.kb.files_store import KbFilesStore


@pytest.fixture
async def store(initialize_migrated_kb_files_database, tmp_path):
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    s = KbFilesStore(tmp_path / "kb_files.db")
    await s.initialize()
    return s


async def _make(store: KbFilesStore, **overrides) -> str:
    """Helper to create a file with sensible defaults."""
    defaults = {
        "artifact_id": "art_x",
        "filename": "test.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1024,
        "scope": "personal",
        "scope_id": "user_a",
        "workspace_id": "ws_42",
        "team_id": "team_5",
        "uploaded_by": "user_a",
    }
    defaults.update(overrides)
    return await store.create(**defaults)


# ─── create + get ─────────────────────────────────────


async def test_create_returns_file_id_with_kbf_prefix(store: KbFilesStore) -> None:
    file_id = await _make(store)
    assert file_id.startswith("kbf_")
    assert len(file_id) == len("kbf_") + 12


async def test_get_returns_dto_with_workspace_id_team_id(store: KbFilesStore) -> None:
    """[P0-P1] DTO 必含 workspace_id / team_id (round 4 fix)."""
    file_id = await _make(store, scope="workspace", scope_id="ws_42", workspace_id="ws_42", team_id="team_5")
    dto = await store.get(file_id)
    assert dto is not None
    assert dto.workspace_id == "ws_42"
    assert dto.team_id == "team_5"
    assert dto.scope == "space"  # Batch3/c1 read-normalize legacy to canonical.


async def test_get_nonexistent_returns_none(store: KbFilesStore) -> None:
    assert await store.get("kbf_nonexistent") is None


async def test_get_default_status_pending(store: KbFilesStore) -> None:
    file_id = await _make(store)
    dto = await store.get(file_id)
    assert dto is not None
    assert dto.parse_status == "pending"
    assert dto.chunks_count == 0
    assert dto.cite_count == 0


# ─── list_for_* ───────────────────────────────────────


async def test_list_for_ws_returns_workspace_files(store: KbFilesStore) -> None:
    """list_for_ws via workspace_id index (含 workspace + personal scope 文件)."""
    await _make(store, scope="workspace", scope_id="ws_42", workspace_id="ws_42")
    await _make(store, scope="personal", scope_id="user_a", workspace_id="ws_42", uploaded_by="user_a")
    await _make(store, scope="workspace", scope_id="ws_99", workspace_id="ws_99")  # Other space.

    results = await store.list_for_ws("ws_42")
    assert len(results) == 2
    assert all(r.workspace_id == "ws_42" for r in results)


async def test_list_for_ws_excludes_soft_deleted(store: KbFilesStore) -> None:
    file_id = await _make(store, workspace_id="ws_42")
    await store.soft_delete(file_id)
    results = await store.list_for_ws("ws_42")
    assert len(results) == 0


async def test_list_for_ws_sort_options(store: KbFilesStore) -> None:
    """sort whitelist 防 SQL injection, allowed values 走."""
    await _make(store, workspace_id="ws_42")
    # Should not raise
    await store.list_for_ws("ws_42", sort="cite_count desc")
    await store.list_for_ws("ws_42", sort="filename asc")
    # Unknown sort fallback to default (no error)
    await store.list_for_ws("ws_42", sort="DROP TABLE; --")


async def test_list_for_user_personal_only(store: KbFilesStore) -> None:
    await _make(store, scope="personal", scope_id="user_a", uploaded_by="user_a")
    await _make(store, scope="personal", scope_id="user_b", uploaded_by="user_b")
    await _make(store, scope="workspace", scope_id="ws_42", uploaded_by="user_a")  # Not returned.

    results = await store.list_for_user("user_a")
    assert len(results) == 1
    assert results[0].uploaded_by == "user_a"
    assert results[0].scope == "personal"


async def test_list_for_team(store: KbFilesStore) -> None:
    await _make(store, scope="team", scope_id="team_5", workspace_id="", team_id="team_5")
    await _make(store, scope="team", scope_id="team_99", workspace_id="", team_id="team_99")
    await _make(store, scope="workspace", scope_id="ws_42")  # Not returned.

    results = await store.list_for_team("team_5")
    assert len(results) == 1
    assert results[0].scope == "team"


async def test_list_all_active_excludes_deleted(store: KbFilesStore) -> None:
    f1 = await _make(store)
    f2 = await _make(store, filename="another.pdf")
    await store.soft_delete(f1)

    results = await store.list_all_active()
    file_ids = {r.file_id for r in results}
    assert f2 in file_ids
    assert f1 not in file_ids


# ─── list_soft_deleted_before (P1-N3) ─────────────────


async def test_list_soft_deleted_before_cutoff(store: KbFilesStore) -> None:
    """[P1-N3] 90 day grace cron: 仅返 soft_deleted_at 早于 cutoff 的."""
    f1 = await _make(store)
    await store.soft_delete(f1)

    # Future cutoff returns f1.
    future = datetime.now(tz=UTC) + timedelta(days=1)
    results = await store.list_soft_deleted_before(future)
    assert any(r.file_id == f1 for r in results)

    # Past cutoff excludes f1 (just soft-deleted, newer than past).
    past = datetime.now(tz=UTC) - timedelta(days=100)
    results = await store.list_soft_deleted_before(past)
    assert all(r.file_id != f1 for r in results)


async def test_list_soft_deleted_before_excludes_active(store: KbFilesStore) -> None:
    """Active (未 soft delete) 文件不返."""
    await _make(store)  # No soft delete.
    future = datetime.now(tz=UTC) + timedelta(days=1)
    results = await store.list_soft_deleted_before(future)
    assert len(results) == 0


# ─── update ───────────────────────────────────────────


async def test_update_parse_status(store: KbFilesStore) -> None:
    file_id = await _make(store)
    await store.update(
        file_id,
        parse_status="done",
        chunks_count=42,
        parse_layer="L0",
    )
    dto = await store.get(file_id)
    assert dto is not None
    assert dto.parse_status == "done"
    assert dto.chunks_count == 42
    assert dto.parse_layer == "L0"


async def test_update_rejects_unknown_field(store: KbFilesStore) -> None:
    """update() whitelist 拒绝未知字段 (SQL injection 防御)."""
    file_id = await _make(store)
    with pytest.raises(ValueError, match="unknown update fields"):
        await store.update(file_id, scope="team")  # Scope not allowlisted.


async def test_update_empty_fields_noop(store: KbFilesStore) -> None:
    file_id = await _make(store)
    # No fields → no error.
    await store.update(file_id)
    dto = await store.get(file_id)
    assert dto is not None


# ─── update_scope (P0-P2) ─────────────────────────────


async def test_update_scope_updates_4_routing_fields(store: KbFilesStore) -> None:
    """[P0-P2] scope change 必同时更新 scope/scope_id/workspace_id/team_id."""
    file_id = await _make(
        store,
        scope="personal",
        scope_id="user_a",
        workspace_id="ws_42",
        team_id="team_5",
    )
    # personal user_a → workspace ws_42
    await store.update_scope(
        file_id,
        scope="workspace",
        scope_id="ws_42",
        workspace_id="ws_42",
        team_id="team_5",
    )
    dto = await store.get(file_id)
    assert dto is not None
    assert dto.scope == "space"  # Batch3/c1 read-normalize legacy to canonical.
    assert dto.scope_id == "ws_42"
    assert dto.workspace_id == "ws_42"
    assert dto.team_id == "team_5"


async def test_update_scope_workspace_to_team(store: KbFilesStore) -> None:
    """workspace → team: workspace_id 改空 string."""
    file_id = await _make(
        store,
        scope="workspace",
        scope_id="ws_42",
        workspace_id="ws_42",
        team_id="team_5",
    )
    await store.update_scope(
        file_id,
        scope="team",
        scope_id="team_5",
        workspace_id="",
        team_id="team_5",
    )
    dto = await store.get(file_id)
    assert dto is not None
    assert dto.scope == "team"
    assert dto.workspace_id == ""


# ─── update_chunks_count (P1-Q2) ──────────────────────


async def test_update_chunks_count(store: KbFilesStore) -> None:
    """[P1-Q2] cross-coll migration 后刷新 chunks_count."""
    file_id = await _make(store)
    await store.update_chunks_count(file_id, 47)
    dto = await store.get(file_id)
    assert dto is not None
    assert dto.chunks_count == 47


# ─── soft_delete + hard_delete ────────────────────────


async def test_soft_delete_sets_timestamp(store: KbFilesStore) -> None:
    file_id = await _make(store)
    await store.soft_delete(file_id)
    dto = await store.get(file_id)
    assert dto is not None
    assert dto.soft_deleted_at is not None


async def test_hard_delete_removes_row(store: KbFilesStore) -> None:
    file_id = await _make(store)
    await store.hard_delete(file_id)
    assert await store.get(file_id) is None


# ─── count_for_ws ─────────────────────────────────────


async def test_count_for_ws_excludes_deleted(store: KbFilesStore) -> None:
    f1 = await _make(store, workspace_id="ws_42")
    await _make(store, workspace_id="ws_42", filename="b.pdf")
    await _make(store, workspace_id="ws_99")
    await store.soft_delete(f1)
    assert await store.count_for_ws("ws_42") == 1


# ─── increment_cite_count ─────────────────────────────


async def test_increment_cite_count(store: KbFilesStore) -> None:
    file_id = await _make(store)
    dto1 = await store.get(file_id)
    assert dto1 is not None
    assert dto1.cite_count == 0
    assert dto1.last_cited_at is None

    await store.increment_cite_count(file_id)
    dto2 = await store.get(file_id)
    assert dto2 is not None
    assert dto2.cite_count == 1
    assert dto2.last_cited_at is not None
