"""kb-matrix M4 决策 13/15 + FR-44: chat 工具面接线（真 store 走 handler）。

- kb_list_tables：many 型发真实 row_count（P1-1：此前恒 record_count=0 = LLM 判
  空表）；seed/存为模板不进清单（schema 非数据表）；graduated 保留 + kind。
- kb_query_records aggregate：数值 op type-gate（string/select 拒）；coverage
  （included/total+排除样本——residual guard 挡下的值从静默变如实）；
  unit_verified caveat（未声明单位）；COUNT+group_by 分类干净无 caveat。
- cite label 扩 categorical（choice/flag——全分类表不再退化裸「第N行」）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bowhead_core.kb.doc_type_store import (
    SYSTEM_TEMPLATE_SEED_USER,
    ExtractField,
    KbDocTypeStore,
)
from bowhead_core.kb.extracted_rows_store import KbExtractedRowsStore
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

WS = "ws_h"


def _make_ctx(**overrides) -> ToolExecutionContext:
    defaults = {
        "workspace_id": WS,
        "member_uid": "member_test",
        "user_id": "user_a",
        "turn_id": "turn_test",
        "datarc": MagicMock(),
        "team_id": "team_h",
    }
    defaults.update(overrides)
    return ToolExecutionContext(**defaults)


async def _seed_graduated_with_rows(tmp_path):
    """转正快照（cardinality=many）+ 3 行：2 干净金额 + 1 raw_string 哨兵。"""
    dts = KbDocTypeStore(tmp_path / "dt.db")
    await dts.initialize()
    rows = KbExtractedRowsStore(db_path=tmp_path / "rows.db")
    await rows.initialize()
    dt_id = await dts.create(
        workspace_id=WS,
        name="供应商合同审阅 2026Q2",
        created_by="u1",
        description="由矩阵转正",
        fields=[
            ExtractField(name="合同金额", type="currency"),
            ExtractField(name="情感倾向", type="select", options=["正面", "负面"]),
            ExtractField(name="备注", type="string"),
        ],
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx_1",
        graduated_from_seq=7,
        graduated_mode="snapshot",
    )

    def _row(fid: str, amount_norm: dict, choice: str) -> dict:
        return {
            "file_id": fid,
            "fields_v2": {
                "合同金额": [
                    {"is_primary": True, "normalized": amount_norm, "extracted_by": "tier1", "source_excerpt": "…", "chunk_id": None, "page": None}
                ],
                "情感倾向": [{"is_primary": True, "normalized": {"choice": choice}, "extracted_by": "tier1"}],
            },
            "edited_fields": [],
            "page": None,
        }

    await rows.create_many_prewrapped(
        [
            _row("f1", {"amount": "300", "currency": None}, "正面"),
            _row("f2", {"amount": "500", "currency": None}, "负面"),
            _row("f3", {"raw_string": "$300/month"}, "正面"),
        ],
        doc_type_id=dt_id,
        workspace_id=WS,
        created_by="u1",
    )
    await dts.update_row_count(dt_id, 3)
    return dts, rows, dt_id


# ─── kb_list_tables ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tables_row_count_kind_and_template_exclusion(tmp_path) -> None:
    dts, rows, dt_grad = await _seed_graduated_with_rows(tmp_path)
    # Seed / save-template (schema, no data) excluded from list.
    await dts.create(workspace_id=WS, name="内置合同审阅", created_by=SYSTEM_TEMPLATE_SEED_USER)
    await dts.create(
        workspace_id=WS, name="我的存为模板", created_by="u1", graduated_from_matrix_id="mx_2", graduated_from_seq=1, graduated_mode="template"
    )
    # Legacy manual(cardinality=one) unchanged.
    await dts.create(workspace_id=WS, name="旧合同类型", created_by="u1")

    reg = build_default_registry()
    handler = reg._specs["kb_list_tables"].handler
    ctx = _make_ctx(kb_doc_type_store=dts)
    result = await handler({}, ctx)
    by_name = {i["name"]: i for i in result["items"]}
    # Graduated actual row count (P1-1 regression: ≠0) + kind.
    assert by_name["供应商合同审阅 2026Q2"]["record_count"] == 3
    assert by_name["供应商合同审阅 2026Q2"]["kind"] == "graduated"
    # Exclude seed/template column groups; retain manual.
    assert "内置合同审阅" not in by_name
    assert "我的存为模板" not in by_name
    assert by_name["旧合同类型"]["kind"] == "manual"


# ─── Honest kb_query_records aggregates ────────────────────────


@pytest.mark.asyncio
async def test_aggregate_coverage_and_unit_caveat(tmp_path) -> None:
    dts, rows, dt_id = await _seed_graduated_with_rows(tmp_path)
    reg = build_default_registry()
    handler = reg._specs["kb_query_records"].handler
    ctx = _make_ctx(kb_doc_type_store=dts, kb_extracted_rows_store=rows, kb_extracted_record_store=None)

    result = await handler(
        {"doc_type_id": dt_id, "aggregate": {"field": "合同金额", "op": "SUM"}},
        ctx,
    )
    assert result["mode"] == "aggregate"
    assert result["aggregate_result"][0]["value"] == 800.0  # 300+500 (SQL excludes sentinel).
    # Honest coverage: included/total + excluded samples ($300/month no longer silent).
    assert result["coverage"] == {"included": 2, "total": 3, "excluded_samples": ["$300/month"]}
    # Unit-unverified caveat (no dimension declared; M5 removes upon declaration).
    assert result["unit_verified"] is False
    assert "覆盖 2/3" in result["note"]
    assert "单位未声明" in result["note"]


@pytest.mark.asyncio
async def test_aggregate_type_gate_rejects_non_numeric_sum(tmp_path) -> None:
    dts, rows, dt_id = await _seed_graduated_with_rows(tmp_path)
    reg = build_default_registry()
    handler = reg._specs["kb_query_records"].handler
    ctx = _make_ctx(kb_doc_type_store=dts, kb_extracted_rows_store=rows, kb_extracted_record_store=None)
    # SUM select rejected (string .value CAST→0.0, not NULL, gives confidently wrong number).
    result = await handler(
        {"doc_type_id": dt_id, "aggregate": {"field": "情感倾向", "op": "SUM"}},
        ctx,
    )
    assert "aggregate_field_not_numeric" in result.get("error", "")


@pytest.mark.asyncio
async def test_aggregate_count_group_by_categorical_clean(tmp_path) -> None:
    dts, rows, dt_id = await _seed_graduated_with_rows(tmp_path)
    reg = build_default_registry()
    handler = reg._specs["kb_query_records"].handler
    ctx = _make_ctx(kb_doc_type_store=dts, kb_extracted_rows_store=rows, kb_extracted_record_store=None)
    # Category count (COUNT + group_by choice) clean, no caveat (.choice COALESCE arm works).
    result = await handler(
        {"doc_type_id": dt_id, "aggregate": {"field": "情感倾向", "op": "COUNT", "group_by": ["情感倾向"]}},
        ctx,
    )
    assert result["mode"] == "aggregate"
    dist = {r["group_keys"]["情感倾向"]: r["value"] for r in result["aggregate_result"]}
    assert dist == {"正面": 2, "负面": 1}
    assert "note" not in result and "coverage" not in result


# ─── query_rows cite label categorical ──────────────────────


@pytest.mark.asyncio
async def test_query_rows_cite_label_reads_choice(tmp_path) -> None:
    """全分类行的 cite label 取到 choice（不再退化裸「第N行」）。"""
    dts = KbDocTypeStore(tmp_path / "dt2.db")
    await dts.initialize()
    rows = KbExtractedRowsStore(db_path=tmp_path / "rows2.db")
    await rows.initialize()
    dt_id = await dts.create(
        workspace_id=WS,
        name="竞品报道分析",
        created_by="u1",
        fields=[ExtractField(name="情感倾向", type="select", options=["正面", "负面"])],
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx_9",
        graduated_from_seq=1,
        graduated_mode="snapshot",
    )
    await rows.create_many_prewrapped(
        [
            {
                "file_id": "f1",
                "fields_v2": {"情感倾向": [{"is_primary": True, "normalized": {"choice": "正面"}, "extracted_by": "tier1"}]},
                "edited_fields": [],
                "page": None,
            }
        ],
        doc_type_id=dt_id,
        workspace_id=WS,
        created_by="u1",
    )
    reg = build_default_registry()
    handler = reg._specs["kb_query_records"].handler
    ctx = _make_ctx(kb_doc_type_store=dts, kb_extracted_rows_store=rows, kb_extracted_record_store=None)
    result = await handler({"doc_type_id": dt_id}, ctx)
    assert result["mode"] == "rows" and result["count"] == 1
    # Citation registry has choice label (⚠ prior hasattr(entries) defense
    # never ran assertion, self-deceiving test; wave-1 contract review P1-2 caught it, real API all_citations()).
    from bowhead_core.citations import ensure_registry

    registry = ensure_registry(ctx)
    labels = [c.label for c in registry.all_citations()]
    assert labels, "citation registry 必须有注册行"
    assert any("情感倾向=正面" in label for label in labels)
