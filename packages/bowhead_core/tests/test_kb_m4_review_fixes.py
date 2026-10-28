"""kb-matrix M4 wave-1 三视角 review 修复批回归锁。

- P1-1/P1-2/P2-1（列命名缝）：重复列名/超白名单 charset/超长 → GraduationError
  结构化码（此前：重复=fields_v2 静默 last-wins 丢数据+确信错 SUM；「金额（万元）」
  = 转正成功但永不可查）。
- P1-3（date 编造精度）：双 default 技巧——月/年粒度与裸数字回落 None。
- P2-2（AI 变体 sanitize）：全变体 normalized 字符串清洗（不只 user_edit*）。
- P2-4（并发自愈翻倍）：flip_graduated_snapshot 绝对计数 winner-only。
- date MIN/MAX（契约 P1-1）：文本序聚合正确（CAST 是 2026.0 垃圾）；SUM date 拒。
- P3-6：幽灵字段聚合拒。
- 内核重名字段守卫（create/update duplicate_field_names）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bowhead_core.kb.doc_type_store import ExtractField, KbDocTypeStore
from bowhead_core.kb.extracted_rows_store import KbExtractedRowsStore
from bowhead_core.kb.graduation import (
    GraduationError,
    _sanitize_variants,
    normalize_date_iso,
    validate_graduation_columns,
)
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

WS = "ws_rf"


def _make_ctx(**overrides) -> ToolExecutionContext:
    defaults = {
        "workspace_id": WS,
        "member_uid": "m",
        "user_id": "u",
        "turn_id": "t",
        "datarc": MagicMock(),
        "team_id": "o",
    }
    defaults.update(overrides)
    return ToolExecutionContext(**defaults)


# ─── Column-naming seam (P1-1/P1-2/P2-1) ─────────────────────────


def test_duplicate_column_names_rejected() -> None:
    cols = [{"column_id": "c1", "name": "金额", "type": "currency"}, {"column_id": "c2", "name": " 金额 ", "type": "select", "options": ["高"]}]
    with pytest.raises(GraduationError) as ei:
        validate_graduation_columns(cols)
    assert ei.value.code == "duplicate_column_names"
    assert ei.value.detail["columns"] == ["金额"]


def test_invalid_charset_column_name_rejected() -> None:
    # Fullwidth brackets common Chinese financial headers: graduation accepts, row query always rejects; catch early.
    cols = [{"column_id": "c1", "name": "金额（万元）", "type": "currency"}]
    with pytest.raises(GraduationError) as ei:
        validate_graduation_columns(cols)
    assert ei.value.code == "invalid_column_name"
    assert "金额（万元）" in ei.value.detail["columns"]


def test_column_name_too_long_rejected() -> None:
    cols = [{"column_id": "c1", "name": "长" * 101, "type": "string"}]
    with pytest.raises(GraduationError) as ei:
        validate_graduation_columns(cols)
    assert ei.value.code == "column_name_too_long"


def test_valid_columns_pass() -> None:
    validate_graduation_columns(
        [
            {"column_id": "c1", "name": "合同金额", "type": "currency"},
            {"column_id": "c2", "name": "sign_date-2", "type": "date"},
        ]
    )


# ─── Date dual default (P1-3) ───────────────────────────────────


def test_date_iso_partial_dates_fall_back_none() -> None:
    # Month/year granularity and bare numbers: dateutil fills today, fabricates precision; None fallback to original.
    assert normalize_date_iso("2026-04") is None
    assert normalize_date_iso("2026年4月") is None
    assert normalize_date_iso("2026") is None
    assert normalize_date_iso("300") is None
    # Complete components normalize normally.
    assert normalize_date_iso("2026-04-12") == "2026-04-12"
    assert normalize_date_iso("2026年4月12日") == "2026-04-12"


# ─── AI variant sanitization (security P2-2) ─────────────────────


def test_sanitize_covers_ai_variant_normalized() -> None:
    dirty = "值‮反向​"
    variants = [
        {
            "is_primary": True,
            "extracted_by": "tier1",
            "normalized": {"value": dirty, "raw_string": dirty, "choices": [dirty, "净"]},
            "raw": dirty,
            "source_excerpt": dirty,
        },
    ]
    out = _sanitize_variants(variants)
    v = out[0]
    assert "‮" not in v["normalized"]["value"] and "​" not in v["normalized"]["value"]
    assert "‮" not in v["normalized"]["raw_string"]
    assert all("‮" not in x for x in v["normalized"]["choices"])
    assert "‮" not in v["raw"] and "‮" not in v["source_excerpt"]


# ─── Flip absolute count, winner only (P2-4) ─────────────────────


@pytest.mark.anyio
async def test_flip_graduated_snapshot_winner_only_absolute(tmp_path) -> None:
    dts = KbDocTypeStore(tmp_path / "dt.db")
    await dts.initialize()
    dt_id = await dts.create(
        workspace_id=WS,
        name="p",
        created_by="u",
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx",
        graduated_from_seq=1,
        graduated_mode="pending_snapshot",
    )
    assert await dts.flip_graduated_snapshot(dt_id, row_count=7) is True
    # Loser: nonpending no-op, no double count from concurrent healing.
    assert await dts.flip_graduated_snapshot(dt_id, row_count=7) is False
    dto = await dts.get(dt_id)
    assert dto is not None and dto.graduated_mode == "snapshot" and dto.row_count == 7


# ─── Kernel duplicate-field guard ───────────────────────────────


@pytest.mark.anyio
async def test_store_rejects_duplicate_field_names(tmp_path) -> None:
    dts = KbDocTypeStore(tmp_path / "dt.db")
    await dts.initialize()
    with pytest.raises(ValueError, match="duplicate_field_names"):
        await dts.create(
            workspace_id=WS, name="dup", created_by="u", fields=[ExtractField(name="金额", type="currency"), ExtractField(name="金额", type="string")]
        )


# ─── Date MIN/MAX text order + ghost-field gate (contract P1-1 / P3-6) ──


async def _seed_dates(tmp_path):
    dts = KbDocTypeStore(tmp_path / "dt.db")
    await dts.initialize()
    rows = KbExtractedRowsStore(db_path=tmp_path / "rows.db")
    await rows.initialize()
    dt_id = await dts.create(
        workspace_id=WS,
        name="合同审阅 Q2",
        created_by="u",
        fields=[ExtractField(name="签约日期", type="date"), ExtractField(name="金额", type="currency")],
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx",
        graduated_from_seq=1,
        graduated_mode="snapshot",
    )
    entries = []
    for i, d in enumerate(["2026-03-05", "2025-11-20", "2026-01-02"]):
        entries.append(
            {
                "file_id": f"f{i}",
                "fields_v2": {"签约日期": [{"is_primary": True, "normalized": {"value": d}, "extracted_by": "tier1"}]},
                "edited_fields": [],
                "page": None,
            }
        )
    await rows.create_many_prewrapped(entries, doc_type_id=dt_id, workspace_id=WS, created_by="u")
    return dts, rows, dt_id


@pytest.mark.asyncio
async def test_date_min_max_text_order_correct(tmp_path) -> None:
    """「最早签约日期」——转正 ISO 归一就是为了这个（CAST AS REAL 只得 2026.0 垃圾）。"""
    dts, rows, dt_id = await _seed_dates(tmp_path)
    reg = build_default_registry()
    handler = reg._specs["kb_query_records"].handler
    ctx = _make_ctx(kb_doc_type_store=dts, kb_extracted_rows_store=rows, kb_extracted_record_store=None)
    r_min = await handler({"doc_type_id": dt_id, "aggregate": {"field": "签约日期", "op": "MIN"}}, ctx)
    assert r_min["aggregate_result"][0]["value"] == "2025-11-20"
    assert r_min["coverage"] == {"included": 3, "total": 3, "excluded_samples": []}
    # Graduated no legacy-unnormalized caveat; date unitless, no unit_verified key.
    assert "unit_verified" not in r_min
    r_max = await handler({"doc_type_id": dt_id, "aggregate": {"field": "签约日期", "op": "MAX"}}, ctx)
    assert r_max["aggregate_result"][0]["value"] == "2026-03-05"
    # Date SUM still rejected (meaningless), copy points to MIN/MAX.
    r_sum = await handler({"doc_type_id": dt_id, "aggregate": {"field": "签约日期", "op": "SUM"}}, ctx)
    assert "aggregate_field_not_numeric" in r_sum["error"] and "MIN/MAX" in r_sum["error"]


@pytest.mark.asyncio
async def test_unknown_field_aggregate_rejected(tmp_path) -> None:
    dts, rows, dt_id = await _seed_dates(tmp_path)
    reg = build_default_registry()
    handler = reg._specs["kb_query_records"].handler
    ctx = _make_ctx(kb_doc_type_store=dts, kb_extracted_rows_store=rows, kb_extracted_record_store=None)
    r = await handler({"doc_type_id": dt_id, "aggregate": {"field": "改名后的幽灵字段", "op": "SUM"}}, ctx)
    assert "aggregate_field_unknown" in r["error"]
