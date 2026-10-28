"""kb-structured-tables Sprint 2 — Phase A UT.

Coverage (11 UT per tech-design §5):
  1. test_catalog_has_5_types
  2. test_each_catalog_keywords_distinct_ge_3
  3. test_each_catalog_fields_le_20
  4. test_migrate_kb_doc_types_add_columns_idempotent
  5. test_migrate_kb_files_add_auto_classified_idempotent
  6. test_lifespan_marker_get_set_has
  7. test_seed_catalog_for_ws_inserts_5
  8. test_seed_catalog_for_ws_idempotent_via_marker
  9. test_seed_catalog_vs_user_created_unique_no_conflict_warns (renamed from
     "unique_collision_skips" — same content)
  10. test_doc_type_store_row_to_dto_defensive_col_missing
  11. test_sample_keywords_validator_rejects_regex_meta_and_rtl
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.kb.catalog_seed import (
    SYSTEM_CATALOG_SEED_USER,
    seed_catalog_for_ws,
)
from bowhead_core.kb.doc_type_catalog import CATALOG, get_catalog
from bowhead_core.kb.doc_type_migration import (
    migrate_kb_doc_types_add_columns,
    migrate_kb_files_add_auto_classified,
)
from bowhead_core.kb.doc_type_store import (
    MAX_FIELDS_PER_SCHEMA,
    KbDocTypeStore,
    _validate_sample_keywords,
)
from bowhead_core.kb.lifespan_marker_store import KbLifespanMarkerStore

if TYPE_CHECKING:
    from pathlib import Path

# ─── Static catalog shape tests ───────────────────────────


def test_catalog_has_9_types() -> None:
    """Sprint 25 + Sprint 26 expanded catalog: 序时账→明细账 + 总账 + 利润表 + 资产负债表 + 现金流量表."""
    assert len(CATALOG) == 9
    assert len(get_catalog()) == 9
    names = {dt.name for dt in CATALOG}
    assert names == {
        # Sprint 25 cardinality=many (3)
        "明细账",
        "总账",
        "发生额及余额表",
        # Sprint 25+26 cardinality=one (6: 利润表+资产负债表+现金流量表+3 from Sprint 2).
        "利润表",
        "资产负债表",
        "现金流量表",
        "资产评估报告",
        "税务凭证",
        "政府补助证书",
    }


def test_each_catalog_keywords_distinct_ge_3() -> None:
    """I-A1-keywords: ≥3 distinct keywords after dedup (per PRD r2)."""
    for cdt in CATALOG:
        distinct = set(cdt.sample_keywords)
        assert len(distinct) >= 3, f"{cdt.name} has only {len(distinct)} distinct keywords: {cdt.sample_keywords}"


def test_each_catalog_fields_le_20() -> None:
    """I-A1-fields: ≤ MAX_FIELDS_PER_SCHEMA (V1 cap = 20)."""
    for cdt in CATALOG:
        assert len(cdt.fields) <= MAX_FIELDS_PER_SCHEMA, cdt.name


def test_catalog_per_row_types_now_enabled() -> None:
    """Sprint 25: cardinality=many types now auto_extract=True (per RowExtractor wire).

    Per Sprint 25 PRD §3.3 C2 — flip auto_extract for 明细账 + 总账 + 发生额表 to True
    so worker spawns _run_per_row_extraction. Sprint 2 had False as placeholder
    (extract not yet implemented).
    """
    by_name = {dt.name: dt for dt in CATALOG}
    for n in ("明细账", "总账", "发生额及余额表"):
        assert by_name[n].cardinality == "many"
        assert by_name[n].auto_extract_enabled is True
        assert len(by_name[n].dedup_key_fields) > 0, f"{n} needs dedup_key_fields for UNIQUE INDEX"
    for n in ("利润表", "资产评估报告", "税务凭证", "政府补助证书"):
        assert by_name[n].cardinality == "one"
        assert by_name[n].auto_extract_enabled is True
        assert by_name[n].dedup_key_fields == [], f"{n} cardinality=one no need dedup_key"


# ─── Migration idempotency tests ───────────────────────────


async def _create_pre_sprint2_doc_types_db(path: str | Path) -> None:
    """Create a kb_doc_types schema as it was BEFORE Sprint 2 (no 3 new cols)."""
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("""
            CREATE TABLE kb_doc_types (
                doc_type_id            TEXT PRIMARY KEY,
                workspace_id           TEXT NOT NULL,
                name                   TEXT NOT NULL,
                description            TEXT NOT NULL DEFAULT '',
                extract_schema_json    TEXT NOT NULL DEFAULT '{"fields": []}',
                record_count           INTEGER NOT NULL DEFAULT 0,
                extractor_model        TEXT NOT NULL DEFAULT 'qwen-plus',
                cross_verify_enabled   INTEGER NOT NULL DEFAULT 1,
                created_by             TEXT NOT NULL,
                created_at             TEXT NOT NULL,
                updated_at             TEXT NOT NULL,
                soft_deleted_at        TEXT,
                UNIQUE(workspace_id, name)
            )
        """)
        await db.execute(
            "INSERT INTO kb_doc_types "
            "(doc_type_id, workspace_id, name, created_by, created_at, updated_at) "
            "VALUES ('dt_legacy', 'kb_ws_legacy', 'legacy_doc', "
            "'user_x', '2026-01-01', '2026-01-01')",
        )
        await db.commit()


async def _create_pre_sprint2_files_db(path: str | Path) -> None:
    """Create a kb_files schema as it was BEFORE Sprint 2 (no auto_classified col)."""
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("""
            CREATE TABLE kb_files (
                file_id           TEXT PRIMARY KEY,
                artifact_id       TEXT NOT NULL,
                filename          TEXT NOT NULL,
                mime_type         TEXT NOT NULL,
                size_bytes        INTEGER NOT NULL,
                scope             TEXT NOT NULL,
                scope_id          TEXT NOT NULL,
                workspace_id      TEXT NOT NULL DEFAULT '',
                team_id            TEXT NOT NULL DEFAULT '',
                uploaded_by       TEXT NOT NULL,
                uploaded_at       TEXT NOT NULL,
                parse_status      TEXT NOT NULL DEFAULT 'pending',
                doc_type_id       TEXT,
                soft_deleted_at   TEXT
            )
        """)
        await db.commit()


async def test_migrate_kb_doc_types_add_columns_idempotent(tmp_path) -> None:
    """I-A3-1: PRAGMA sniff + ADD COLUMN x3 (cardinality / auto_extract_enabled /
    sample_keywords_json). Re-run = no-op (no exception, no dup cols)."""
    db = tmp_path / "kb_doc_types.db"
    await _create_pre_sprint2_doc_types_db(db)

    # First migrate: should add 3 cols
    await migrate_kb_doc_types_add_columns(db)
    async with aiosqlite.connect(str(db)) as conn:
        cols_rows = await (await conn.execute("SELECT name FROM pragma_table_info('kb_doc_types')")).fetchall()
        cols = {r[0] for r in cols_rows}
    assert "cardinality" in cols
    assert "auto_extract_enabled" in cols
    assert "sample_keywords_json" in cols

    # Re-run idempotent
    await migrate_kb_doc_types_add_columns(db)
    await migrate_kb_doc_types_add_columns(db)
    async with aiosqlite.connect(str(db)) as conn:
        cols_rows2 = await (await conn.execute("SELECT name FROM pragma_table_info('kb_doc_types')")).fetchall()
        cols2 = {r[0] for r in cols_rows2}
    assert cols2 == cols  # no duplicates added


async def test_migrate_kb_doc_types_skip_when_table_absent(tmp_path) -> None:
    """Soft-fail: table not yet initialized → skip + no exception."""
    db = tmp_path / "doc_types_missing.db"
    # No table created
    await migrate_kb_doc_types_add_columns(db)
    # Just verify no exception (DB now has the file empty / no kb_doc_types)


async def test_migrate_kb_files_add_auto_classified_idempotent(tmp_path) -> None:
    """I-A3-2: ADD COLUMN auto_classified_doc_type_id. Re-run = no-op."""
    db = tmp_path / "kb_files.db"
    await _create_pre_sprint2_files_db(db)

    await migrate_kb_files_add_auto_classified(db)
    async with aiosqlite.connect(str(db)) as conn:
        cols_rows = await (await conn.execute("SELECT name FROM pragma_table_info('kb_files')")).fetchall()
        cols = {r[0] for r in cols_rows}
    assert "auto_classified_doc_type_id" in cols

    # Re-run idempotent
    await migrate_kb_files_add_auto_classified(db)
    await migrate_kb_files_add_auto_classified(db)
    async with aiosqlite.connect(str(db)) as conn:
        cols_rows2 = await (await conn.execute("SELECT name FROM pragma_table_info('kb_files')")).fetchall()
        cols2 = {r[0] for r in cols_rows2}
    assert cols2 == cols


# ─── Lifespan marker store tests ───────────────────────────


async def test_lifespan_marker_get_set_has(tmp_path) -> None:
    store = KbLifespanMarkerStore(tmp_path / "markers.db")
    await store.initialize()

    assert await store.has_marker("k1") is False
    assert await store.get("k1") is None

    await store.set("k1", {"hello": "world", "count": 3})
    assert await store.has_marker("k1") is True
    val = await store.get("k1")
    assert val == {"hello": "world", "count": 3}

    # UPSERT overwrites
    await store.set("k1", {"hello": "updated"})
    val2 = await store.get("k1")
    assert val2 == {"hello": "updated"}

    # catalog_marker_key helper
    assert KbLifespanMarkerStore.catalog_marker_key("kb_ws_000") == ("catalog_seeded_at_ws:kb_ws_000")


# ─── Catalog seed tests ───────────────────────────


async def _make_seed_fixture(tmp_path):
    """Return (doc_type_store, marker_store) with schemas initialized + migrated."""
    dt_db = tmp_path / "kb_doc_types.db"
    rec_db = tmp_path / "kb_extracted_records.db"
    store = KbDocTypeStore(db_path=dt_db, records_db_path=rec_db)
    await store.initialize()
    # Ensure 3 new cols exist (Sprint 2 migrate run as in lifespan)
    await migrate_kb_doc_types_add_columns(dt_db)
    marker = KbLifespanMarkerStore(db_path=dt_db)
    await marker.initialize()
    return store, marker


async def test_seed_catalog_for_ws_inserts_9(tmp_path) -> None:
    """Sprint 26 catalog: seed inserts all 9 catalog doc_types into ws."""
    store, marker = await _make_seed_fixture(tmp_path)
    result = await seed_catalog_for_ws("kb_ws_seed_test", store, marker)
    assert result.inserted == 9
    assert result.skipped_already_present == 0
    assert result.skipped_via_marker is False

    dts = await store.list_for_ws("kb_ws_seed_test")
    assert len(dts) == 9
    names = {dt.name for dt in dts}
    assert names == {
        "明细账",
        "总账",
        "发生额及余额表",  # cardinality=many (Sprint 25 enabled)
        "利润表",
        "资产负债表",
        "现金流量表",  # cardinality=one Sprint 25+26 NEW
        "资产评估报告",
        "税务凭证",
        "政府补助证书",  # cardinality=one Sprint 2
    }
    # All catalog seeded with system marker (I-A4-1)
    assert all(dt.created_by == SYSTEM_CATALOG_SEED_USER for dt in dts)
    # Sprint 25 cardinality=many auto_extract=True + dedup_key_fields populated
    by_name = {dt.name: dt for dt in dts}
    assert by_name["明细账"].cardinality == "many"
    assert by_name["明细账"].auto_extract_enabled is True  # Sprint 25 flipped
    assert by_name["明细账"].sample_keywords  # non-empty list
    assert "凭证号" in by_name["明细账"].dedup_key_fields  # 明细账 Sprint 25 dedup.
    assert by_name["资产评估报告"].cardinality == "one"
    assert by_name["资产评估报告"].auto_extract_enabled is True


async def test_seed_catalog_for_ws_idempotent_via_marker(tmp_path) -> None:
    """I-A4-3: marker write gates re-runs. 2nd call returns skipped_via_marker."""
    store, marker = await _make_seed_fixture(tmp_path)
    result1 = await seed_catalog_for_ws("kb_ws_idem", store, marker)
    assert result1.inserted == 9  # Sprint 26 catalog has 9

    result2 = await seed_catalog_for_ws("kb_ws_idem", store, marker)
    assert result2.skipped_via_marker is True
    assert result2.inserted == 0

    # Still only 9 rows (no duplicates)
    dts = await store.list_for_ws("kb_ws_idem")
    assert len(dts) == 9


async def test_seed_catalog_vs_user_created_unique_no_conflict_warns(tmp_path) -> None:
    """I-A2 (PRD r2 + per P2-TD-1): user pre-created same-name doc_type before
    seed runs. Seed must NOT raise; skip via UNIQUE collision + warn audit + count.
    """
    store, marker = await _make_seed_fixture(tmp_path)

    # User pre-creates a doc_type with one of the catalog names
    await store.create(
        workspace_id="kb_ws_conflict",
        name="资产评估报告",  # collides with catalog
        created_by="user_alice",
        description="user's own version",
        fields=[],
    )

    # Now run seed — must NOT crash; should skip the colliding one + insert the 8 others
    result = await seed_catalog_for_ws("kb_ws_conflict", store, marker)
    assert result.inserted == 8  # Sprint 26: 9 catalog - 1 conflict = 8 inserted
    assert result.skipped_already_present == 1
    assert result.skipped_via_marker is False

    # Verify: ws has 9 doc_types total; the user-created one wins (created_by=user_alice)
    dts = await store.list_for_ws("kb_ws_conflict")
    assert len(dts) == 9
    by_name = {dt.name: dt for dt in dts}
    assert by_name["资产评估报告"].created_by == "user_alice"
    assert by_name["资产评估报告"].description == "user's own version"
    # Other 6 came from catalog seed (Sprint 25: 序时账 renamed → 明细账)
    assert by_name["明细账"].created_by == SYSTEM_CATALOG_SEED_USER


# ─── Defensive read tests ───────────────────────────


async def test_doc_type_store_row_to_dto_defensive_col_missing(tmp_path) -> None:
    """I-A5: NEW image reading a pre-ALTER row (no 3 new cols) must not crash."""
    db = tmp_path / "pre_alter_doc_types.db"
    # Create old schema + insert a row, WITHOUT running migrate (simulates NEW
    # image hitting a legacy DB before lifespan migrate runs).
    await _create_pre_sprint2_doc_types_db(db)

    rec_db = tmp_path / "kb_extracted_records.db"
    store = KbDocTypeStore(db_path=db, records_db_path=rec_db)
    # Don't run migrate — read directly. _row_to_dto must handle missing cols.
    dts = await store.list_for_ws("kb_ws_legacy")
    assert len(dts) == 1
    dt = dts[0]
    # Defaults filled in by _row_to_dto for missing cols (per I-A5)
    assert dt.cardinality == "one"  # default for missing
    assert dt.auto_extract_enabled is True  # default for missing
    assert dt.sample_keywords == []  # default for missing
    # Other cols still readable
    assert dt.name == "legacy_doc"
    assert dt.workspace_id == "kb_ws_legacy"


# ─── Validator tests ───────────────────────────


def test_sample_keywords_validator_rejects_regex_meta_and_rtl() -> None:
    """I-B8: validate_sample_keywords rejects regex meta + control / RTL / zero-width."""
    # OK cases
    _validate_sample_keywords([])
    _validate_sample_keywords(["凭证号", "借方", "贷方"])
    _validate_sample_keywords(["a", "b", "c"])

    # Reject: > 30 keywords
    with pytest.raises(ValueError, match="≤ 30"):
        _validate_sample_keywords([f"kw{i}" for i in range(31)])

    # Reject: empty / too long
    with pytest.raises(ValueError, match="长度"):
        _validate_sample_keywords([""])
    with pytest.raises(ValueError, match="长度"):
        _validate_sample_keywords(["x" * 33])

    # Reject: regex meta chars (`.` / `*` / `+` / `?` / `[` / `]` / `(` / `)` / `|` / `^` / `$` / `\`)
    for bad in [".any", "wild*card", "lots+", "maybe?", "[group]", "[]", "(g)", "alt|or", "^start", "end$", "esc\\n"]:
        with pytest.raises(ValueError, match="regex meta"):
            _validate_sample_keywords([bad])

    # Reject: control char (0x00 NUL)
    with pytest.raises(ValueError, match="控制字符|RTL|zero-width"):
        _validate_sample_keywords(["a\x00b"])

    # Reject: RTL override (U+202E)
    with pytest.raises(ValueError, match="控制字符|RTL|zero-width"):
        _validate_sample_keywords(["evil‮attack"])

    # Reject: non-str type
    with pytest.raises(ValueError, match="必须 str"):
        _validate_sample_keywords([123])  # type: ignore[list-item]


async def test_list_with_stats_returns_doc_types_and_aggregate(tmp_path) -> None:
    """Sprint 2 Phase C segment A: list_with_stats returns (doc_types, stats_map)
    where stats are computed in a single cross-DB sweep over kb_extracted_records.
    """
    dt_db = tmp_path / "kb_doc_types.db"
    rec_db = tmp_path / "kb_extracted_records.db"
    dt_store = KbDocTypeStore(db_path=dt_db, records_db_path=rec_db)
    await dt_store.initialize()
    from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore

    rec_store = KbExtractedRecordStore(db_path=rec_db)
    await rec_store.initialize()

    dt_id = await dt_store.create(
        workspace_id="ws_x",
        name="评估",
        created_by="t",
    )
    other_id = await dt_store.create(
        workspace_id="ws_x",
        name="税务",
        created_by="t",
    )
    # 2 records on `评估` (1 ready, 1 needs_review), 0 on `税务`
    await rec_store.create(
        workspace_id="ws_x",
        doc_type_id=dt_id,
        file_id="f1",
        fields={},
        verify_status="ready",
    )
    await rec_store.create(
        workspace_id="ws_x",
        doc_type_id=dt_id,
        file_id="f2",
        fields={},
        verify_status="needs_review",
    )

    doc_types, stats_map = await dt_store.list_with_stats("ws_x")
    assert len(doc_types) == 2
    assert dt_id in stats_map  # only doc_types with records appear in map
    assert stats_map[dt_id].total == 2
    assert stats_map[dt_id].needs_review_count == 1
    assert stats_map[dt_id].last_extracted_at is not None
    # `税务` has no records → no entry in map (caller defaults to empty DocTypeStats)
    assert other_id not in stats_map


def test_doc_type_store_create_validates_sample_keywords(tmp_path) -> None:
    """I-B8: KbDocTypeStore.create() applies _validate_sample_keywords()."""
    import asyncio

    async def _run():
        store = KbDocTypeStore(db_path=tmp_path / "validate.db")
        await store.initialize()

        # OK
        await store.create(
            workspace_id="ws",
            name="ok_dt",
            created_by="t",
            sample_keywords=["aa", "bb", "cc"],
        )

        # Bad: regex meta
        with pytest.raises(ValueError, match="regex meta"):
            await store.create(
                workspace_id="ws",
                name="bad_dt",
                created_by="t",
                sample_keywords=["[evil]"],
            )

        # Bad: cardinality enum
        with pytest.raises(ValueError, match="cardinality"):
            await store.create(
                workspace_id="ws",
                name="bad_dt_2",
                created_by="t",
                cardinality="invalid",  # type: ignore[arg-type]
            )

    asyncio.run(_run())
