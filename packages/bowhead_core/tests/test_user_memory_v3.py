"""V3 PRD-B · UserMemoryStore soft-delete + reset snapshot + iter unit tests.

Covers (≥12 cases per PRD-B Day 1 spec):
  - soft_delete / restore (returns True only if row affected)
  - locked entries protected from soft_delete + soft_delete_all
  - list_trashed / count_trashed
  - hard_delete_old_trash uses datetime() normalize (spike #19)
  - hard_delete_user purges active + trashed
  - soft_delete_all_for_user respects locked
  - save_reset_snapshot + hard_delete_old_reset_snapshots
  - iter_for_user async generator
  - upsert restores trashed entry (deleted_at = NULL)
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from bowhead_core.memory_privacy import MemoryIdempotencyConflictError
from bowhead_core.user_memory import (
    MemoryEntryLockedError,
    MemoryEntrySchemaError,
    MemoryEntryStateError,
    MemoryVersionConflictError,
    UserMemoryStore,
)


@pytest.fixture
async def store(tmp_path):
    s = UserMemoryStore(tmp_path / "memory.db")
    await s.initialize()
    return s


# ─── soft_delete + restore ───────────────────────────


@pytest.mark.asyncio
async def test_soft_delete_marks_deleted_at(store):
    e = await store.upsert(
        user_id="u1",
        kind="preference",
        key="favorite_metric",
        value="GMV",
    )
    ok = await store.soft_delete(user_id="u1", entry_id=e.id)
    assert ok is True

    # list_for_user skips trashed entries by default.
    actives = await store.list_for_user("u1")
    assert actives == []

    # list_for_user(include_trashed=True) includes them.
    all_entries = await store.list_for_user("u1", include_trashed=True)
    assert len(all_entries) == 1
    assert all_entries[0].deleted_at is not None


@pytest.mark.asyncio
async def test_soft_delete_locked_entry_blocked(store):
    e = await store.upsert(
        user_id="u1",
        kind="constraint",
        key="frozen",
        value=1,
        locked=True,
        lock_reason="datarc binding",
    )
    ok = await store.soft_delete(user_id="u1", entry_id=e.id)
    assert ok is False
    actives = await store.list_for_user("u1")
    assert len(actives) == 1  # still there


@pytest.mark.asyncio
async def test_soft_delete_idempotent_on_already_trashed(store):
    e = await store.upsert(
        user_id="u1",
        kind="preference",
        key="x",
        value="y",
    )
    assert await store.soft_delete(user_id="u1", entry_id=e.id) is True
    # Re-soft-delete: 0 rows affected (already deleted_at IS NOT NULL)
    assert await store.soft_delete(user_id="u1", entry_id=e.id) is False


@pytest.mark.asyncio
async def test_soft_delete_wrong_user_blocked(store):
    e = await store.upsert(
        user_id="u1",
        kind="preference",
        key="x",
        value="y",
    )
    ok = await store.soft_delete(user_id="u_other", entry_id=e.id)
    assert ok is False


@pytest.mark.asyncio
async def test_restore_clears_deleted_at(store):
    e = await store.upsert(user_id="u1", kind="preference", key="k", value="v")
    await store.soft_delete(user_id="u1", entry_id=e.id)
    ok = await store.restore(user_id="u1", entry_id=e.id)
    assert ok is True
    actives = await store.list_for_user("u1")
    assert len(actives) == 1
    assert actives[0].deleted_at is None


@pytest.mark.asyncio
async def test_restore_no_op_on_active_entry(store):
    e = await store.upsert(user_id="u1", kind="preference", key="k", value="v")
    ok = await store.restore(user_id="u1", entry_id=e.id)
    assert ok is False  # entry is not trashed


# ─── typed edit + version CAS ────────────────────────


@pytest.mark.asyncio
async def test_patch_product_value_updates_only_value_and_version(store):
    entry = await store.upsert_product_memory(
        user_id="u1",
        kind="constraint",
        key="report_order",
        value="detail first",
        workspace_id="ws_retail",
    )

    updated = await store.patch_product_value(
        user_id="u1",
        entry_id=entry.id,
        expected_version=1,
        edit_schema="text_v1",
        value="summary first",
    )

    assert updated.parsed_value() == "summary first"
    assert updated.version == 2
    assert updated.kind == entry.kind
    assert updated.key == entry.key
    assert updated.workspace_id == "ws_retail"
    assert updated.created_source_type == entry.created_source_type
    assert updated.last_modified_actor_type == "user"
    assert updated.last_modified_actor_id == "u1"


@pytest.mark.asyncio
async def test_patch_product_value_rejects_stale_version(store):
    entry = await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="chart",
        value="line",
    )
    await store.patch_product_value(
        user_id="u1",
        entry_id=entry.id,
        expected_version=1,
        edit_schema="text_v1",
        value="bar",
    )

    with pytest.raises(MemoryVersionConflictError) as exc:
        await store.patch_product_value(
            user_id="u1",
            entry_id=entry.id,
            expected_version=1,
            edit_schema="text_v1",
            value="pie",
        )

    assert exc.value.current.version == 2
    assert exc.value.current.parsed_value() == "bar"


@pytest.mark.asyncio
async def test_patch_product_value_rejects_wrong_schema(store):
    entry = await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="chart",
        value="line",
    )

    with pytest.raises(MemoryEntrySchemaError):
        await store.patch_product_value(
            user_id="u1",
            entry_id=entry.id,
            expected_version=1,
            edit_schema="boolean_v1",
            value=True,
        )


@pytest.mark.asyncio
async def test_patch_product_value_rejects_locked_or_trashed(store):
    locked = await store.upsert_product_memory(
        user_id="u1",
        kind="constraint",
        key="locked",
        value="fixed",
        locked=True,
    )
    with pytest.raises(MemoryEntryLockedError):
        await store.patch_product_value(
            user_id="u1",
            entry_id=locked.id,
            expected_version=1,
            edit_schema="text_v1",
            value="changed",
        )

    trashed = await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="trashed",
        value="old",
    )
    await store.soft_delete(
        user_id="u1",
        entry_id=trashed.id,
        expected_version=1,
    )
    with pytest.raises(MemoryEntryStateError):
        await store.patch_product_value(
            user_id="u1",
            entry_id=trashed.id,
            expected_version=2,
            edit_schema="text_v1",
            value="new",
        )


@pytest.mark.asyncio
async def test_delete_and_restore_reject_stale_versions(store):
    entry = await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="versioned",
        value="v1",
    )
    updated = await store.patch_product_value(
        user_id="u1",
        entry_id=entry.id,
        expected_version=1,
        edit_schema="text_v1",
        value="v2",
    )

    with pytest.raises(MemoryVersionConflictError):
        await store.soft_delete(
            user_id="u1",
            entry_id=entry.id,
            expected_version=1,
        )
    assert await store.soft_delete(
        user_id="u1",
        entry_id=entry.id,
        expected_version=updated.version,
    )
    with pytest.raises(MemoryVersionConflictError):
        await store.restore(
            user_id="u1",
            entry_id=entry.id,
            expected_version=updated.version,
        )
    assert await store.restore(
        user_id="u1",
        entry_id=entry.id,
        expected_version=updated.version + 1,
    )


# ─── list_trashed + count_trashed ────────────────────


@pytest.mark.asyncio
async def test_list_trashed_returns_only_trashed(store):
    a = await store.upsert(user_id="u1", kind="preference", key="a", value=1)
    await store.upsert(user_id="u1", kind="preference", key="b", value=2)
    await store.soft_delete(user_id="u1", entry_id=a.id)

    trashed = await store.list_trashed("u1")
    assert len(trashed) == 1
    assert trashed[0].id == a.id
    assert trashed[0].deleted_at is not None

    assert await store.count_trashed("u1") == 1


# ─── hard_delete_old_trash ──────────────────────────


@pytest.mark.asyncio
async def test_hard_delete_old_trash_respects_age(store, tmp_path):
    """trashed > 30 days 真删, < 30 days 保留. Uses datetime() normalize."""
    fresh = await store.upsert(user_id="u1", kind="preference", key="fresh", value=1)
    stale = await store.upsert(user_id="u1", kind="preference", key="stale", value=2)
    await store.soft_delete(user_id="u1", entry_id=fresh.id)
    await store.soft_delete(user_id="u1", entry_id=stale.id)

    # Set the stale entry's deleted_at directly to 60 days ago (simulate old trash).
    import aiosqlite

    async with aiosqlite.connect(str(tmp_path / "memory.db")) as db:
        await db.execute(
            "UPDATE memory_entries SET deleted_at=datetime('now', '-60 days') WHERE id=?",
            (stale.id,),
        )
        await db.commit()

    deleted = await store.hard_delete_old_trash(max_age_days=30)
    assert deleted == 1
    remaining = await store.list_for_user("u1", include_trashed=True)
    assert len(remaining) == 1
    assert remaining[0].id == fresh.id


# ─── hard_delete_user ───────────────────────────────


@pytest.mark.asyncio
async def test_hard_delete_user_purges_active_and_trashed(store):
    a = await store.upsert(user_id="u1", kind="preference", key="a", value=1)
    await store.upsert(user_id="u1", kind="preference", key="b", value=2)
    await store.soft_delete(user_id="u1", entry_id=a.id)

    # other user's data must be preserved
    await store.upsert(user_id="u2", kind="preference", key="c", value=3)

    n = await store.hard_delete_user("u1")
    assert n == 2  # both active + trashed for u1
    assert await store.list_for_user("u1", include_trashed=True) == []
    # u2 untouched
    assert len(await store.list_for_user("u2")) == 1


# ─── soft_delete_all_for_user ───────────────────────


@pytest.mark.asyncio
async def test_soft_delete_all_for_user_skips_locked(store):
    await store.upsert(user_id="u1", kind="preference", key="free1", value=1)
    await store.upsert(user_id="u1", kind="preference", key="free2", value=2)
    await store.upsert(
        user_id="u1",
        kind="datarc_binding",
        key="member_uid",
        value="m_xxx",
        locked=True,
        lock_reason="datarc bound",
    )

    n = await store.soft_delete_all_for_user("u1")
    assert n == 2  # only 2 unlocked
    actives = await store.list_for_user("u1")
    assert len(actives) == 1
    assert actives[0].locked is True
    assert actives[0].kind == "datarc_binding"


# ─── reset snapshots ────────────────────────────────


@pytest.mark.asyncio
async def test_reset_v3_replays_same_result_for_same_idempotency_key(store):
    await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="report_grain",
        value="monthly",
    )
    idempotency_key = str(uuid4())

    first = await store.reset_v3(
        user_id="u1",
        idempotency_key=idempotency_key,
    )
    replay = await store.reset_v3(
        user_id="u1",
        idempotency_key=idempotency_key,
    )

    assert replay == first
    assert await store.count_recent_reset_snapshots(user_id="u1") == 1


@pytest.mark.asyncio
async def test_restore_reset_v3_replays_after_snapshot_is_consumed(store):
    await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="report_grain",
        value="monthly",
    )
    reset = await store.reset_v3(
        user_id="u1",
        idempotency_key=str(uuid4()),
    )
    idempotency_key = str(uuid4())

    first = await store.restore_reset_v3(
        snapshot_id=reset.snapshot_id,
        user_id="u1",
        accessible_space_ids=set(),
        idempotency_key=idempotency_key,
    )
    replay = await store.restore_reset_v3(
        snapshot_id=reset.snapshot_id,
        user_id="u1",
        accessible_space_ids=set(),
        idempotency_key=idempotency_key,
    )

    assert first.status == "restored"
    assert replay == first


@pytest.mark.asyncio
async def test_reset_idempotency_key_cannot_be_reused_for_restore(store):
    await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="report_grain",
        value="monthly",
    )
    idempotency_key = str(uuid4())
    reset = await store.reset_v3(
        user_id="u1",
        idempotency_key=idempotency_key,
    )

    with pytest.raises(MemoryIdempotencyConflictError):
        await store.restore_reset_v3(
            snapshot_id=reset.snapshot_id,
            user_id="u1",
            accessible_space_ids=set(),
            idempotency_key=idempotency_key,
        )


@pytest.mark.asyncio
async def test_reset_snapshot_save_and_purge_old(store, tmp_path):
    snap_id = await store.save_reset_snapshot(
        user_id="u1",
        snapshot={"entries": [{"k": "v"}]},
    )
    assert snap_id.startswith("snap_")

    # Set the old snapshot to 60 days ago.
    import aiosqlite

    async with aiosqlite.connect(str(tmp_path / "memory.db")) as db:
        await db.execute(
            "UPDATE memory_reset_snapshots SET created_at=datetime('now', '-60 days') WHERE id=?",
            (snap_id,),
        )
        await db.commit()

    n = await store.hard_delete_old_reset_snapshots(max_age_days=30)
    assert n == 1


# ─── iter_for_user ──────────────────────────────────


@pytest.mark.asyncio
async def test_iter_for_user_yields_only_active_by_default(store):
    a = await store.upsert(user_id="u1", kind="preference", key="a", value=1)
    await store.upsert(user_id="u1", kind="preference", key="b", value=2)
    await store.soft_delete(user_id="u1", entry_id=a.id)

    keys = [e.key async for e in store.iter_for_user("u1")]
    assert keys == ["b"]

    keys_all = [e.key async for e in store.iter_for_user("u1", include_trashed=True)]
    assert sorted(keys_all) == ["a", "b"]


# ─── upsert restore-on-conflict ─────────────────────


@pytest.mark.asyncio
async def test_upsert_restores_trashed_entry(store):
    e = await store.upsert(user_id="u1", kind="preference", key="x", value="old")
    await store.soft_delete(user_id="u1", entry_id=e.id)

    # re-upsert same kind+key → restore
    e2 = await store.upsert(user_id="u1", kind="preference", key="x", value="new")
    assert e2.deleted_at is None
    actives = await store.list_for_user("u1")
    assert len(actives) == 1
    assert actives[0].parsed_value() == "new"


# ─── datarc_binding helper still skips trashed ──────


@pytest.mark.asyncio
async def test_get_datarc_member_uid_skips_trashed(store):
    e = await store.upsert(
        user_id="u1",
        kind="datarc_binding",
        key="member_uid",
        value="m_aaa",
    )
    assert await store.get_datarc_member_uid("u1") == "m_aaa"

    await store.soft_delete(user_id="u1", entry_id=e.id)
    # locked? No — datarc_binding here is unlocked. Soft-delete succeeds.
    # After trash, helper must return None.
    assert await store.get_datarc_member_uid("u1") is None


@pytest.mark.asyncio
async def test_typed_writers_assign_policy_role_and_schema(store):
    product = await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="report_style",
        value="concise",
    )
    operational = await store.upsert_operational_memory(
        user_id="u1",
        kind="tool_context",
        key="datarc/workspace",
        value={"id": "dw_1"},
        source="tool_runtime",
    )

    assert product.memory_role == "product_memory"
    assert product.value_schema == "text_v1"
    assert product.memory_policy_version == 1
    assert operational.memory_role == "operational"
    assert operational.value_schema == "object_v1"
    assert operational.memory_policy_version == 1


@pytest.mark.asyncio
async def test_content_and_lifecycle_mutations_advance_version(store):
    created = await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="metric",
        value="GMV",
    )
    updated = await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="metric",
        value="revenue",
    )
    assert updated.id == created.id
    assert updated.version == created.version + 1

    assert await store.soft_delete(user_id="u1", entry_id=created.id)
    trashed = (await store.list_for_user("u1", include_trashed=True))[0]
    assert trashed.version == updated.version + 1

    assert await store.restore(user_id="u1", entry_id=created.id)
    restored = (await store.list_for_user("u1"))[0]
    assert restored.version == trashed.version + 1
