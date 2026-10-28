"""方案A (report-edit-audit dogfood P1) — delete revision 恢复.

口径：删除确认框承诺「删除后可从历史版本恢复」，drawer 只回滚 patch 兑现不了。
本批把 delete revision 升级为恢复 envelope（见 test_canvas_cascade_delete 的
envelope 断言），store 提供 restore_deleted_sections：按 revision 整组（宿主+
级联子图同 batch）原 id 原 seq 回插；旧格式记录明确拒绝。
"""

from __future__ import annotations

import json

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.canvas_store import CanvasRestoreUnsupportedError

from bowhead_core import CanvasStore


@pytest.fixture
async def store(tmp_path):
    s = CanvasStore(tmp_path / "restore.db")
    await s.initialize()
    return s


async def _canvas(store: CanvasStore, session: str = "s_r", user: str = "usr_1"):
    return await store.ensure_canvas(session_id=session, workspace_id="ws_1", user_id=user)


async def _delete_revision_id(store: CanvasStore, canvas_id: str, section_id: str) -> str:
    async with (
        open_shared_ctx(str(store.db_path)) as db,
        db.execute(
            "SELECT id FROM canvas_revisions WHERE canvas_id=? AND op='delete' AND section_id=?",
            (canvas_id, section_id),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.asyncio
async def test_restore_single_section_back_in_place(store) -> None:
    cv = await _canvas(store)
    a = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "第一段"})
    b = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "第二段"})
    await store.delete_section(section_id=a.id, canvas_id=cv.id)
    rv = await _delete_revision_id(store, cv.id, a.id)

    restored = await store.restore_deleted_sections(canvas_id=cv.id, revision_id=rv)

    assert restored == [a.id]
    sections = await store.list_sections(cv.id)
    by_id = {s.id: s for s in sections}
    assert by_id[a.id].payload["markdown"] == "第一段"
    assert by_id[a.id].kind == "narrative"
    # Original seq free → restore original position (a before b).
    ordered = [s.id for s in sections]
    assert ordered.index(a.id) < ordered.index(b.id)
    # Restoration writes an op=append revision.
    async with (
        open_shared_ctx(str(store.db_path)) as db,
        db.execute(
            "SELECT COUNT(*) FROM canvas_revisions WHERE canvas_id=? AND op='append' AND section_id=?",
            (cv.id, a.id),
        ) as cur,
    ):
        appends = int((await cur.fetchone())[0])
    assert appends == 2  # Original append + restoration append


@pytest.mark.asyncio
async def test_restore_cascade_group_from_host_revision(store) -> None:
    cv = await _canvas(store)
    c1 = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"t": 1}})
    c2 = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"t": 2}})
    host = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"[figure:{c1.id}]\n\n[figure:{c2.id}]"},
    )
    deleted = await store.delete_section(section_id=host.id, canvas_id=cv.id, cascade=True)
    assert set(deleted) == {host.id, c1.id, c2.id}
    rv = await _delete_revision_id(store, cv.id, host.id)

    restored = await store.restore_deleted_sections(canvas_id=cv.id, revision_id=rv)

    assert set(restored) == {host.id, c1.id, c2.id}
    ids = {s.id for s in await store.list_sections(cv.id)}
    assert {host.id, c1.id, c2.id} <= ids


@pytest.mark.asyncio
async def test_restore_from_child_revision_restores_whole_group(store) -> None:
    """从级联子图的 revision 恢复也整组回来（batch 关联，不看点的是哪行）。"""
    cv = await _canvas(store)
    c1 = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"t": 1}})
    host = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"[figure:{c1.id}]"},
    )
    await store.delete_section(section_id=host.id, canvas_id=cv.id, cascade=True)
    rv_child = await _delete_revision_id(store, cv.id, c1.id)

    restored = await store.restore_deleted_sections(canvas_id=cv.id, revision_id=rv_child)

    assert set(restored) == {host.id, c1.id}


@pytest.mark.asyncio
async def test_restore_seq_taken_appends_to_tail(store) -> None:
    cv = await _canvas(store)
    a = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "A"})
    b = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "B"})
    await store.delete_section(section_id=a.id, canvas_id=cv.id)
    rv = await _delete_revision_id(store, cv.id, a.id)
    # Move b to a's vacated seq (reorder completion/renumbering puts b at seq 1).
    await store.reorder_sections(canvas_id=cv.id, ordered_ids=[b.id])

    restored = await store.restore_deleted_sections(canvas_id=cv.id, revision_id=rv)

    assert restored == [a.id]
    sections = await store.list_sections(cv.id)
    ordered = [s.id for s in sections]
    # Original position occupied → append, without displacing existing rows.
    assert ordered == [b.id, a.id]
    seqs = [s.seq for s in sections]
    assert len(seqs) == len(set(seqs))


@pytest.mark.asyncio
async def test_restore_idempotent_second_call_conflict(store) -> None:
    cv = await _canvas(store)
    a = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "A"})
    await store.delete_section(section_id=a.id, canvas_id=cv.id)
    rv = await _delete_revision_id(store, cv.id, a.id)
    assert await store.restore_deleted_sections(canvas_id=cv.id, revision_id=rv) == [a.id]

    # Second attempt: all already present → empty list (route returns 409 from this).
    assert await store.restore_deleted_sections(canvas_id=cv.id, revision_id=rv) == []


@pytest.mark.asyncio
async def test_restore_wrong_canvas_or_missing_is_none(store) -> None:
    cv = await _canvas(store)
    other = await store.ensure_canvas(session_id="s_other", workspace_id="ws_1", user_id="usr_2")
    a = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "A"})
    await store.delete_section(section_id=a.id, canvas_id=cv.id)
    rv = await _delete_revision_id(store, cv.id, a.id)

    # Cross-Canvas (IDOR rule: treat as nonexistent).
    assert await store.restore_deleted_sections(canvas_id=other.id, revision_id=rv) is None
    # Nonexistent revision.
    assert await store.restore_deleted_sections(canvas_id=cv.id, revision_id="rv_nope") is None
    # Revision with a non-delete operation.
    async with (
        open_shared_ctx(str(store.db_path)) as db,
        db.execute(
            "SELECT id FROM canvas_revisions WHERE canvas_id=? AND op='append' LIMIT 1",
            (cv.id,),
        ) as cur,
    ):
        append_rv = str((await cur.fetchone())[0])
    assert await store.restore_deleted_sections(canvas_id=cv.id, revision_id=append_rv) is None


@pytest.mark.asyncio
async def test_restore_legacy_revision_unsupported(store) -> None:
    """envelope 上线前的历史删除记录（裸 payload）明确拒绝，不猜。"""
    cv = await _canvas(store)
    async with open_shared_ctx(str(store.db_path)) as db:
        await db.execute(
            "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
            "VALUES ('rv_legacy', ?, 'sec_old', 'delete', ?, NULL, NULL, '2026-01-01T00:00:00+00:00')",
            (cv.id, json.dumps({"markdown": "旧格式"})),
        )
        await db.commit()

    with pytest.raises(CanvasRestoreUnsupportedError):
        await store.restore_deleted_sections(canvas_id=cv.id, revision_id="rv_legacy")


@pytest.mark.asyncio
async def test_restored_section_meta_preserved(store) -> None:
    """turn_id/owner 等行级元数据随 envelope 完整往返。"""
    cv = await _canvas(store)
    a = await store.append_section(
        canvas_id=cv.id,
        kind="chart",
        payload={"spec": {"kind": "bar"}},
        by_turn_id="t_123",
        owner="peer_x",
    )
    await store.delete_section(section_id=a.id, canvas_id=cv.id)
    rv = await _delete_revision_id(store, cv.id, a.id)

    await store.restore_deleted_sections(canvas_id=cv.id, revision_id=rv)

    async with (
        open_shared_ctx(str(store.db_path)) as db,
        db.execute("SELECT turn_id, owner, kind FROM canvas_sections WHERE id=?", (a.id,)) as cur,
    ):
        row = await cur.fetchone()
    assert (row[0], row[1], row[2]) == ("t_123", "peer_x", "chart")
