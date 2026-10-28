"""kb-knowledge-tables sprint Day 1: KbExtractedRecordStore CRUD + SQL query tests.

kb-v2 Phase A Step 3 update: DTO now exposes v2 variant arrays. Assertions go
through ``_primary_value`` test helper which extracts the V1-style
{value, source_snippet, confidence, edited_by_user} from the primary variant.
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.kb.extracted_record_store import (
    QUERY_OPS,
    KbExtractedRecordStore,
    _validate_field_name,
)


async def _make_store(tmp_path) -> KbExtractedRecordStore:
    s = KbExtractedRecordStore(tmp_path / "rec.db")
    await s.initialize()
    return s


def _sample_fields(amount: int = 5_000_000, date_: str = "2024-01-15") -> dict:
    """V1-shape sample (auto-wrapped to v2 by ``store.create``)."""
    return {
        "合同金额": {
            "value": amount,
            "source_snippet": f"合同总金额为 {amount}",
            "source_page": 1,
            "confidence": 0.92,
        },
        "起始日": {
            "value": date_,
            "source_snippet": f"自{date_}起生效",
            "source_page": 1,
            "confidence": 0.95,
        },
    }


def _primary_value(dto, fname: str) -> dict[str, Any]:
    """Extract V1-style {value, source_snippet, confidence, edited_by_user} from
    the primary variant of a v2 DTO. Used in legacy assertions that pre-date the
    variant-array shape."""
    variants = dto.fields.get(fname, []) if dto and dto.fields else []
    if not variants:
        return {}
    primary = next((v for v in variants if v.get("is_primary")), variants[0])
    norm = primary.get("normalized") or {}
    # Re-extract value from normalized.amount (currency/number) or .value (string/date).
    raw_value: Any = norm.get("amount") if "amount" in norm else norm.get("value")
    if "raw_string" in norm:
        raw_value = norm["raw_string"]
    # amount is Decimal-as-string per PRD §A.1; coerce digit-only strings back to
    # int for legacy V1 equality assertions.
    if isinstance(raw_value, str) and raw_value.lstrip("-").replace(".", "", 1).isdigit():
        raw_value = float(raw_value) if "." in raw_value else int(raw_value)
    return {
        "value": raw_value,
        "source_snippet": primary.get("source_excerpt"),
        "confidence": primary.get("confidence"),
        "edited_by_user": primary.get("edited_by_user", False),
    }


# ─── _validate_field_name ─────────


def test_validate_field_name_ascii() -> None:
    _validate_field_name("amount")
    _validate_field_name("amount_v2")
    _validate_field_name("col-1")


def test_validate_field_name_chinese() -> None:
    _validate_field_name("合同金额")
    _validate_field_name("起始日")


def test_validate_field_name_rejects_quote() -> None:
    """SQL/json path injection 防御."""
    with pytest.raises(ValueError, match="非法字符"):
        _validate_field_name("amount'; DROP TABLE")


def test_validate_field_name_rejects_dot() -> None:
    """`.` 是 json path 分隔符, 名字含 . 会让 path 跳层."""
    with pytest.raises(ValueError, match="非法字符"):
        _validate_field_name("a.b")


def test_validate_field_name_rejects_empty() -> None:
    with pytest.raises(ValueError, match="field name 长度"):
        _validate_field_name("")


# ─── CRUD ─────────


async def test_create_and_get(tmp_path) -> None:
    s = await _make_store(tmp_path)
    fields = _sample_fields()
    rid = await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f1",
        fields=fields,
        extractor_model="qwen-plus",
        extraction_duration_ms=2300,
        verify_status="ready",
        created_by="u1",
    )
    assert rid.startswith("rec_")
    dto = await s.get(rid)
    assert dto is not None
    assert dto.workspace_id == "ws_a"
    assert dto.doc_type_id == "dt_x"
    assert dto.file_id == "f1"
    assert _primary_value(dto, "合同金额")["value"] == 5_000_000
    assert _primary_value(dto, "起始日")["source_snippet"].startswith("自2024-01-15")
    assert dto.verify_status == "ready"
    assert dto.edited_by_user == []
    assert dto.extractor_model == "qwen-plus"
    assert dto.extraction_duration_ms == 2300


async def test_create_invalid_verify_status(tmp_path) -> None:
    s = await _make_store(tmp_path)
    with pytest.raises(ValueError, match="verify_status"):
        await s.create(
            workspace_id="ws_a",
            doc_type_id="dt_x",
            file_id="f1",
            fields={},
            verify_status="bogus",
        )


async def test_list_for_doc_type_ws_isolated(tmp_path) -> None:
    """P0-R1: workspace_id 强制过滤防 cross-ws leak."""
    s = await _make_store(tmp_path)
    await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_shared",
        file_id="fa",
        fields=_sample_fields(1_000_000),
    )
    await s.create(
        workspace_id="ws_b",
        doc_type_id="dt_shared",
        file_id="fb",
        fields=_sample_fields(2_000_000),
    )
    a = await s.list_for_doc_type(workspace_id="ws_a", doc_type_id="dt_shared")
    b = await s.list_for_doc_type(workspace_id="ws_b", doc_type_id="dt_shared")
    assert len(a) == 1
    assert a[0].file_id == "fa"
    assert len(b) == 1
    assert b[0].file_id == "fb"


async def test_list_for_doc_type_pagination(tmp_path) -> None:
    s = await _make_store(tmp_path)
    for i in range(5):
        await s.create(
            workspace_id="ws_a",
            doc_type_id="dt_x",
            file_id=f"f{i}",
            fields={"X": {"value": i}},
        )
    page1 = await s.list_for_doc_type(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        limit=2,
        offset=0,
    )
    page2 = await s.list_for_doc_type(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        limit=2,
        offset=2,
    )
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r.record_id for r in page1}.isdisjoint({r.record_id for r in page2})


async def test_list_for_file(tmp_path) -> None:
    s = await _make_store(tmp_path)
    rid1 = await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f_target",
        fields=_sample_fields(),
    )
    await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f_other",
        fields=_sample_fields(),
    )
    items = await s.list_for_file("f_target")
    assert len(items) == 1
    assert items[0].record_id == rid1


async def test_count_for_doc_type(tmp_path) -> None:
    s = await _make_store(tmp_path)
    for i in range(3):
        await s.create(
            workspace_id="ws_a",
            doc_type_id="dt_x",
            file_id=f"f{i}",
            fields={},
        )
    n = await s.count_for_doc_type(workspace_id="ws_a", doc_type_id="dt_x")
    assert n == 3
    n_other = await s.count_for_doc_type(workspace_id="ws_b", doc_type_id="dt_x")
    assert n_other == 0


async def test_update_field_marks_corrected(tmp_path) -> None:
    """User 改字段值, 该字段进 user_corrected_fields, 后续 re-extract 不覆盖."""
    s = await _make_store(tmp_path)
    rid = await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f1",
        fields=_sample_fields(5_000_000),
    )
    ok = await s.update_field(
        record_id=rid,
        field_name="合同金额",
        new_value=4_500_000,
        modified_by="user42",
    )
    assert ok
    dto = await s.get(rid)
    pv = _primary_value(dto, "合同金额")
    assert pv["value"] == 4_500_000
    assert pv["edited_by_user"] is True
    # Preserve original confidence/snippet.
    assert pv["confidence"] == 0.92
    assert "合同金额" in dto.edited_by_user
    assert dto.last_modified_by == "user42"


async def test_update_field_invalid_name(tmp_path) -> None:
    s = await _make_store(tmp_path)
    rid = await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f1",
        fields=_sample_fields(),
    )
    with pytest.raises(ValueError):
        await s.update_field(
            record_id=rid,
            field_name="bad'name",
            new_value=1,
            modified_by="u1",
        )


async def test_update_field_returns_false_for_missing(tmp_path) -> None:
    s = await _make_store(tmp_path)
    ok = await s.update_field(
        record_id="rec_missing",
        field_name="X",
        new_value=1,
        modified_by="u1",
    )
    assert ok is False


async def test_update_verify_status(tmp_path) -> None:
    s = await _make_store(tmp_path)
    rid = await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f1",
        fields=_sample_fields(),
    )
    ok = await s.update_verify_status(rid, "approved", modified_by="reviewer")
    assert ok
    dto = await s.get(rid)
    assert dto.verify_status == "approved"
    assert dto.last_modified_by == "reviewer"


async def test_update_verify_status_invalid(tmp_path) -> None:
    s = await _make_store(tmp_path)
    rid = await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f1",
        fields=_sample_fields(),
    )
    with pytest.raises(ValueError):
        await s.update_verify_status(rid, "bogus", modified_by="u1")


async def test_update_field_v2_row_does_not_overwrite_fields_json_legacy(
    tmp_path,
    monkeypatch,
) -> None:
    """Lock tech-design §6 Q3: v2-row update keeps fields_json_legacy unchanged."""
    import aiosqlite

    s = await _make_store(tmp_path)
    rid = await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f1",
        fields=_sample_fields(),
    )
    # Pre-set a sentinel value in fields_json_legacy directly (simulates a prior
    # V1→v2 lazy-migrate having stashed the legacy backup).
    async with aiosqlite.connect(str(tmp_path / "rec.db")) as db:
        await db.execute(
            "UPDATE kb_extracted_records SET fields_json_legacy = ? WHERE record_id = ?",
            ('{"sentinel": "pre-existing-legacy"}', rid),
        )
        await db.commit()

    # Edit the field — v2 row path (schema_version=2 already since fresh create).
    ok = await s.update_field(
        record_id=rid,
        field_name="合同金额",
        new_value=4_500_000,
        modified_by="u1",
    )
    assert ok

    async with (
        aiosqlite.connect(str(tmp_path / "rec.db")) as db,
        db.execute(
            "SELECT fields_json_legacy FROM kb_extracted_records WHERE record_id = ?",
            (rid,),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == '{"sentinel": "pre-existing-legacy"}', f"v2-row update must NOT touch fields_json_legacy column (got: {row[0]!r})"


async def test_update_field_lazy_migrate_emits_audit_event(
    tmp_path,
    monkeypatch,
) -> None:
    """V1-row update_field path emits kb.record_v1_to_v2_migrate audit (per
    tech-design §A.5 + SRE P1 from ship-pre review)."""
    import aiosqlite

    # Construct V1-shape row directly via raw SQL — bypass v2 schema.
    db_path = tmp_path / "rec.db"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS kb_extracted_records ("
            "  record_id TEXT PRIMARY KEY, doc_type_id TEXT NOT NULL, "
            "  file_id TEXT NOT NULL, workspace_id TEXT NOT NULL, "
            "  fields_json TEXT NOT NULL DEFAULT '{}', "
            "  verify_status TEXT NOT NULL DEFAULT 'ready', "
            "  user_corrected_fields_json TEXT NOT NULL DEFAULT '[]', "
            "  extractor_model TEXT NOT NULL, extraction_duration_ms INTEGER, "
            "  extracted_at TEXT NOT NULL, last_modified_at TEXT NOT NULL, "
            "  last_modified_by TEXT NOT NULL, soft_deleted_at TEXT"
            ")"
        )
        import json as _json

        await db.execute(
            "INSERT INTO kb_extracted_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "rec_v1_x",
                "dt_x",
                "f1",
                "ws_a",
                _json.dumps({"合同金额": {"value": 5_000_000, "confidence": 0.9}}),
                "ready",
                "[]",
                "qwen-plus",
                1000,
                "2026-05-20T00:00:00+00:00",
                "2026-05-20T00:00:00+00:00",
                "u1",
                None,
            ),
        )
        await db.commit()
    # Now run v2 migration to add cols (schema_version, fields_json_legacy,
    # edited_by_user_json rename) → bridges V1 row to v2-readable.
    from bowhead_core.kb.extracted_record_store import migrate_v1_to_v2

    await migrate_v1_to_v2(db_path)
    # row is now schema_version=1 marker + variant-array readable via _row_to_dto

    captured: list[tuple[str, dict]] = []

    def _spy(event_type: str, payload: dict) -> None:
        captured.append((event_type, payload))

    monkeypatch.setattr(
        "bowhead_core.kb.extracted_record_store.emit_audit",
        _spy,
    )

    s = KbExtractedRecordStore(db_path)
    ok = await s.update_field(
        record_id="rec_v1_x",
        field_name="合同金额",
        new_value=4_500_000,
        modified_by="u1",
    )
    assert ok

    migrate_events = [(k, p) for k, p in captured if k == "kb.record_v1_to_v2_migrate"]
    assert len(migrate_events) == 1, f"expected 1 kb.record_v1_to_v2_migrate, got {captured!r}"
    payload = migrate_events[0][1]
    assert payload["record_id"] == "rec_v1_x"
    assert payload["workspace_id"] == "ws_a"
    assert "duration_ms" in payload and isinstance(payload["duration_ms"], int)


async def test_soft_delete(tmp_path) -> None:
    s = await _make_store(tmp_path)
    rid = await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f1",
        fields=_sample_fields(),
    )
    ok = await s.soft_delete(rid)
    assert ok
    assert await s.get(rid) is None


async def test_soft_delete_for_file_cascade(tmp_path) -> None:
    """File 删时级联软删 records.

    Note (Sprint 2 Phase B I-B4): the test deliberately creates 2 records with
    same (file_id, doc_type_id) to verify cascade. Production code never does
    this (`create()` defaults `dedup_check=True` which would raise); the test
    uses `dedup_check=False` to bypass the safety net for this specific scenario.
    """
    s = await _make_store(tmp_path)
    await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f_target",
        fields=_sample_fields(),
    )
    await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f_target",
        fields=_sample_fields(7_000_000),
        dedup_check=False,
    )
    await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="f_other",
        fields=_sample_fields(),
    )
    n = await s.soft_delete_for_file("f_target")
    assert n == 2
    remain = await s.list_for_doc_type(workspace_id="ws_a", doc_type_id="dt_x")
    assert len(remain) == 1
    assert remain[0].file_id == "f_other"


# ─── SQL query (Day 3) ──────────────────────────────────────────


async def test_query_by_field_filter_and_sort(tmp_path) -> None:
    """支持 json_extract filter + sort + cast_numeric (currency)."""
    s = await _make_store(tmp_path)
    amounts = [1_000_000, 5_000_000, 10_000_000, 20_000_000, 50_000_000]
    for i, a in enumerate(amounts):
        await s.create(
            workspace_id="ws_a",
            doc_type_id="dt_sales",
            file_id=f"f{i}",
            fields=_sample_fields(amount=a),
        )
    # Amount >10M, amount DESC, limit3.
    res = await s.query_by_field(
        workspace_id="ws_a",
        doc_type_id="dt_sales",
        where=[{"field": "合同金额", "op": ">", "value": 10_000_000}],
        sort_by="合同金额",
        sort_dir="DESC",
        limit=3,
        cast_numeric=["合同金额"],
    )
    assert len(res) == 2  # 20M, 50M
    vals = [_primary_value(r, "合同金额")["value"] for r in res]
    assert vals == [50_000_000, 20_000_000]


async def test_query_by_field_ws_isolated(tmp_path) -> None:
    """P0-R1: query 强制 workspace_id."""
    s = await _make_store(tmp_path)
    await s.create(
        workspace_id="ws_a",
        doc_type_id="dt_x",
        file_id="fa",
        fields=_sample_fields(amount=999_999_999),
    )
    await s.create(
        workspace_id="ws_b",
        doc_type_id="dt_x",
        file_id="fb",
        fields=_sample_fields(amount=1_000_000),
    )
    res = await s.query_by_field(
        workspace_id="ws_b",
        doc_type_id="dt_x",
        sort_by="合同金额",
        cast_numeric=["合同金额"],
    )
    assert len(res) == 1
    assert res[0].file_id == "fb"


async def test_query_by_field_invalid_op_rejected(tmp_path) -> None:
    s = await _make_store(tmp_path)
    with pytest.raises(ValueError, match="op"):
        await s.query_by_field(
            workspace_id="ws_a",
            doc_type_id="dt_x",
            where=[{"field": "x", "op": "OR 1=1", "value": 0}],
        )


async def test_query_by_field_invalid_field_rejected(tmp_path) -> None:
    """SQL injection via field name 防御."""
    s = await _make_store(tmp_path)
    with pytest.raises(ValueError, match="非法字符"):
        await s.query_by_field(
            workspace_id="ws_a",
            doc_type_id="dt_x",
            where=[{"field": "x'); DROP", "op": "=", "value": 1}],
        )


async def test_query_by_field_invalid_sort_dir(tmp_path) -> None:
    s = await _make_store(tmp_path)
    with pytest.raises(ValueError, match="sort_dir"):
        await s.query_by_field(
            workspace_id="ws_a",
            doc_type_id="dt_x",
            sort_dir="; DROP",
        )


async def test_query_by_field_default_sort_extracted_at(tmp_path) -> None:
    s = await _make_store(tmp_path)
    for i in range(3):
        await s.create(
            workspace_id="ws_a",
            doc_type_id="dt_x",
            file_id=f"f{i}",
            fields={"X": {"value": i}},
        )
    res = await s.query_by_field(workspace_id="ws_a", doc_type_id="dt_x")
    assert len(res) == 3


async def test_query_ops_whitelist() -> None:
    """所有支持的 op 在 whitelist 内."""
    expected = ("=", "!=", ">", ">=", "<", "<=", "LIKE", "IS NULL", "IS NOT NULL")
    assert expected == QUERY_OPS
