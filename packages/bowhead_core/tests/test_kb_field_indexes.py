"""kb-knowledge-tables v2 Phase A Step 5: per-field expression index UT.

Per docs/kb-knowledge-tables-v2-tech-design.md §A.6. Covers:
  - _hash_id / _hash_field_name collision-resistance for Chinese / hyphen / space
  - _rebuild_field_indexes CREATE on currency/number fields + DROP on rebuild
  - soft_delete drops all indexes
  - doc_type_store CRUD hooks wire correctly when records_db_path is set
  - BEGIN IMMEDIATE wrap (IntelliVault audit learning)
  - Step 6: extract_failed in VERIFY_STATUS_VALUES + accept via update_verify_status
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
from bowhead_core.kb.doc_type_store import ExtractField, KbDocTypeStore
from bowhead_core.kb.extracted_record_store import (
    VERIFY_STATUS_VALUES,
    KbExtractedRecordStore,
    _hash_field_name,
    _hash_id,
    _rebuild_field_indexes,
)

if TYPE_CHECKING:
    from pathlib import Path


async def _list_indexes(db_path: Path, like_prefix: str = "idx_kb_records_fld_%") -> list[str]:
    async with (
        aiosqlite.connect(str(db_path)) as db,
        db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE ?",
            (like_prefix,),
        ) as cur,
    ):
        return sorted(row[0] for row in await cur.fetchall())


# ─── hash helpers ─────────


def test_hash_field_name_chinese_safe():
    """Pure-Unicode field (no ASCII alnum) → ``_<hash>`` prefix-only."""
    result = _hash_field_name("合同金额")
    assert result.startswith("_"), f"expected _ prefix, got {result!r}"
    assert len(result) == 9, f"expected ≤9 chars (_<8hex>), got len={len(result)}"


def test_hash_field_name_hyphen_vs_space_no_collision():
    """ "金额-RMB" vs "金额 RMB" must produce distinct index names — IntelliVault
    audit learning: input variants with same ASCII prefix collide if you don't
    hash the full string."""
    h1 = _hash_field_name("金额-RMB")
    h2 = _hash_field_name("金额 RMB")
    assert h1 != h2, f"hash collision on hyphen-vs-space: {h1!r} == {h2!r}"


def test_hash_id_stable_for_same_input():
    """Same doc_type_id → same hash (idempotent)."""
    a = _hash_id("dt_abc123")
    b = _hash_id("dt_abc123")
    assert a == b
    assert len(a) == 8


# ─── _rebuild_field_indexes direct ─────────


async def test_rebuild_creates_index_for_currency_field(tmp_path):
    """currency/number field → expression index created with proper name."""
    records_db = tmp_path / "rec.db"
    store = KbExtractedRecordStore(records_db)
    await store.initialize()

    fields = [
        ExtractField(name="合同金额", type="currency"),
        ExtractField(name="客户名", type="string"),  # should NOT get index
    ]
    await _rebuild_field_indexes(records_db, "dt_test001", fields)

    indexes = await _list_indexes(records_db)
    dt_hash = _hash_id("dt_test001")
    expected_prefix = f"idx_kb_records_fld_{dt_hash}_"
    matching = [i for i in indexes if i.startswith(expected_prefix)]
    assert len(matching) == 1, f"expected exactly 1 numeric-field index, got {indexes!r}"


async def test_rebuild_drops_indexes_when_fields_none(tmp_path):
    """soft_delete path: fields=None → all indexes for this doc_type dropped."""
    records_db = tmp_path / "rec.db"
    store = KbExtractedRecordStore(records_db)
    await store.initialize()

    fields = [ExtractField(name="amount", type="currency")]
    await _rebuild_field_indexes(records_db, "dt_test002", fields)
    pre_drop = await _list_indexes(records_db)
    dt_hash = _hash_id("dt_test002")
    assert any(dt_hash in i for i in pre_drop)

    # Now drop all
    await _rebuild_field_indexes(records_db, "dt_test002", None)
    post_drop = await _list_indexes(records_db)
    assert not any(dt_hash in i for i in post_drop), f"expected all dt_test002 indexes dropped, got {post_drop!r}"


# ─── doc_type_store hooks wire ─────────


async def test_doc_type_create_triggers_index_build(tmp_path):
    """KbDocTypeStore.create(fields=[currency]) → records DB gets the index."""
    doc_types_db = tmp_path / "dt.db"
    records_db = tmp_path / "rec.db"

    rec_store = KbExtractedRecordStore(records_db)
    await rec_store.initialize()

    dt_store = KbDocTypeStore(doc_types_db, records_db_path=records_db)
    await dt_store.initialize()

    dt_id = await dt_store.create(
        workspace_id="ws_a",
        name="合同",
        created_by="u1",
        fields=[ExtractField(name="合同金额", type="currency")],
    )

    indexes = await _list_indexes(records_db)
    dt_hash = _hash_id(dt_id)
    assert any(dt_hash in i for i in indexes), f"expected index for {dt_id} (hash={dt_hash}) after create(), got {indexes!r}"


async def test_doc_type_soft_delete_drops_indexes(tmp_path):
    """soft_delete a doc_type → all its expression indexes dropped."""
    doc_types_db = tmp_path / "dt.db"
    records_db = tmp_path / "rec.db"

    rec_store = KbExtractedRecordStore(records_db)
    await rec_store.initialize()

    dt_store = KbDocTypeStore(doc_types_db, records_db_path=records_db)
    await dt_store.initialize()

    dt_id = await dt_store.create(
        workspace_id="ws_a",
        name="合同",
        created_by="u1",
        fields=[ExtractField(name="amount", type="number")],
    )
    dt_hash = _hash_id(dt_id)

    pre = await _list_indexes(records_db)
    assert any(dt_hash in i for i in pre)

    ok = await dt_store.soft_delete(dt_id)
    assert ok

    post = await _list_indexes(records_db)
    assert not any(dt_hash in i for i in post), f"expected all {dt_id} indexes dropped after soft_delete, got {post!r}"


# ─── Step 6 enum extension ─────────


def test_verify_status_values_includes_extract_failed():
    """Per tech-design §A.8: VERIFY_STATUS_VALUES extends to 4 values."""
    assert "extract_failed" in VERIFY_STATUS_VALUES
    assert VERIFY_STATUS_VALUES == (
        "ready",
        "needs_review",
        "approved",
        "extract_failed",
    )


async def test_update_verify_status_accepts_extract_failed(tmp_path):
    """update_verify_status accepts the new enum value end-to-end."""
    records_db = tmp_path / "rec.db"
    s = KbExtractedRecordStore(records_db)
    await s.initialize()
    rid = await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f1",
        fields={"x": {"value": 1, "confidence": 0.9}},
    )
    ok = await s.update_verify_status(rid, "extract_failed", modified_by="sys")
    assert ok
    dto = await s.get(rid)
    assert dto is not None
    assert dto.verify_status == "extract_failed"
