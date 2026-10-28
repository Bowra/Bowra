"""V7 kb-agentic-fix Day 3: KbFilesStore.*_for_ws_shared 4 case.

Tech design v1.1 §2.8 — workspace + team scope only, personal 跳 (防 cross-user leak).
"""

from __future__ import annotations

from bowhead_core.kb.files_store import KbFilesStore


async def _create(db: KbFilesStore, *, ws: str, scope: str, scope_id: str, uploader: str = "u1", filename: str = "f.docx") -> str:
    return await db.create(
        artifact_id=f"art_{filename}_{scope}",
        filename=filename,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=1024,
        scope=scope,
        scope_id=scope_id,
        workspace_id=ws,
        team_id="team_1",
        uploaded_by=uploader,
    )


async def test_list_for_ws_shared_excludes_personal(initialize_migrated_kb_files_database, tmp_path) -> None:
    """ws=A 含 1 personal + 1 workspace + 1 team → shared 仅返 workspace + team (2个)."""
    initialize_migrated_kb_files_database(tmp_path / "kb.db")
    db = KbFilesStore(tmp_path / "kb.db")
    await db.initialize()

    await _create(db, ws="ws_A", scope="personal", scope_id="u_alice", filename="personal.docx")
    fid_w = await _create(db, ws="ws_A", scope="workspace", scope_id="ws_A", filename="ws.docx")
    fid_o = await _create(db, ws="ws_A", scope="team", scope_id="team_1", filename="team.docx")

    shared = await db.list_for_ws_shared("ws_A")
    assert len(shared) == 2
    scopes = {f.scope for f in shared}
    assert scopes == {"space", "team"}  # Batch3/c1 canonical.
    file_ids = {f.file_id for f in shared}
    assert file_ids == {fid_w, fid_o}


async def test_count_for_ws_shared_excludes_personal(initialize_migrated_kb_files_database, tmp_path) -> None:
    """count_for_ws_shared 跳 personal scope."""
    initialize_migrated_kb_files_database(tmp_path / "kb.db")
    db = KbFilesStore(tmp_path / "kb.db")
    await db.initialize()

    await _create(db, ws="ws_A", scope="personal", scope_id="u_alice", filename="a.docx")
    await _create(db, ws="ws_A", scope="personal", scope_id="u_bob", filename="b.docx")
    await _create(db, ws="ws_A", scope="workspace", scope_id="ws_A", filename="w.docx")

    legacy = await db.list_for_ws("ws_A")
    assert len(legacy) == 3  # Legacy includes personal.
    assert await db.count_for_ws_shared("ws_A") == 1  # Shared only workspace.


async def test_list_for_ws_shared_respects_soft_delete(initialize_migrated_kb_files_database, tmp_path) -> None:
    """soft-deleted shared file 不在结果里."""
    initialize_migrated_kb_files_database(tmp_path / "kb.db")
    db = KbFilesStore(tmp_path / "kb.db")
    await db.initialize()

    fid1 = await _create(db, ws="ws_A", scope="workspace", scope_id="ws_A", filename="a.docx")
    fid2 = await _create(db, ws="ws_A", scope="workspace", scope_id="ws_A", filename="b.docx")
    await db.soft_delete(fid1)

    shared = await db.list_for_ws_shared("ws_A")
    assert len(shared) == 1
    assert shared[0].file_id == fid2


async def test_role_distribution_shared_returns_dict(initialize_migrated_kb_files_database, tmp_path) -> None:
    """V1 占位实现返 empty dict (V8 加 doc_type 后启)."""
    initialize_migrated_kb_files_database(tmp_path / "kb.db")
    db = KbFilesStore(tmp_path / "kb.db")
    await db.initialize()
    result = await db.role_distribution_shared("ws_A")
    assert isinstance(result, dict)
    assert result == {}
