"""kb-matrix M4 FR-42: 内核字段类型系统扩型（select/multi_select/boolean）。

覆盖（tech-design-m4 r3 §4.1 门）：
- SSOT 一致性：矩阵 MATRIX_COLUMN_TYPES / PROMOTABLE_TYPES 从内核 FIELD_TYPES 派生。
- ExtractField options/dimension：validate 归一、非法拒；序列化条件式发射
  （存量 4 型输出逐字节零变化）。
- options 落库 round-trip：create select 带 options → get() 读回完好
  （过 _fields_to_schema_json 落库序列化器——r2 review P1：漏它 = options 静默丢）。
- normalize_field_value 真值表（与矩阵 M3 语义同源；residual guard 原样）。
- _coalesce_field_expr 5-arm（存量 arm 前置不变；rows 侧同一权威）。
- wrap_extractor_result_to_variants 对新型字段产 choice/choices/flag。
"""

from __future__ import annotations

import json

import pytest
from bowhead_core.kb.doc_type_store import (
    FIELD_TYPES,
    ExtractField,
    KbDocTypeStore,
    _field_to_dict,
    validate_options,
)
from bowhead_core.kb.extracted_record_store import (
    _coalesce_field_expr,
    normalize_field_value,
    wrap_extractor_result_to_variants,
)
from bowhead_core.kb.extracted_rows_store import (
    _coalesce_field_expr as rows_coalesce,
)
from bowhead_core.kb.matrix_store import (
    MATRIX_COLUMN_TYPES,
    PROMOTABLE_TYPES,
)


async def _make_store(tmp_path) -> KbDocTypeStore:
    s = KbDocTypeStore(tmp_path / "dt.db")
    await s.initialize()
    return s


# ─── SSOT consistency (drift fails) ─────────────────────────────


def test_field_types_ssot() -> None:
    assert FIELD_TYPES == ("string", "number", "currency", "date", "select", "multi_select", "boolean")
    # Matrix types = kernel + matrix-only question; graduation targets kernel itself.
    assert (*FIELD_TYPES, "question") == MATRIX_COLUMN_TYPES
    assert PROMOTABLE_TYPES == FIELD_TYPES


def test_matrix_validate_options_is_kernel_fn() -> None:
    # Matrix re-export is identical kernel function, convergence not copy.
    from bowhead_core.kb.matrix_store import validate_options as mx_validate

    assert mx_validate is validate_options


# ─── ExtractField options/dimension ─────────────────────


def test_extract_field_select_options_normalized() -> None:
    f = ExtractField(name="情感倾向", type="select", options=[" 正面 ", "负面"])
    f.validate()
    assert f.options == ["正面", "负面"]


def test_extract_field_select_requires_options() -> None:
    f = ExtractField(name="情感倾向", type="select")
    with pytest.raises(ValueError, match="options_required"):
        f.validate()


def test_extract_field_legacy_type_rejects_options() -> None:
    f = ExtractField(name="金额", type="currency", options=["a"])
    with pytest.raises(ValueError, match="options_not_applicable"):
        f.validate()


def test_extract_field_boolean_rejects_options() -> None:
    f = ExtractField(name="是否提及价格", type="boolean", options=["是", "否"])
    with pytest.raises(ValueError, match="options_not_applicable"):
        f.validate()


def test_extract_field_dimension_placeholder() -> None:
    # M5 placeholder: dict accepted, nondict rejected, no M4 consumption.
    f = ExtractField(name="金额", type="currency", dimension={"kind": "currency", "unit": "万元"})
    f.validate()
    assert f.dimension == {"kind": "currency", "unit": "万元"}
    bad = ExtractField(name="金额", type="currency", dimension="万元")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="dimension"):
        bad.validate()


def test_legacy_field_serialization_byte_stable() -> None:
    """存量 4 型字段序列化输出**不含** options/dimension 键（条件式发射——
    零回归硬约束：老 doc_type 的 to_dict / 落库 JSON 形状逐字节不变）。"""
    f = ExtractField(name="合同金额", type="currency", description="RMB")
    d = _field_to_dict(f)
    assert set(d.keys()) == {"name", "type", "description", "required", "cross_verify", "extraction_hint"}
    g = ExtractField(name="情感倾向", type="select", options=["正面", "负面"])
    dg = _field_to_dict(g)
    assert dg["options"] == ["正面", "负面"]
    assert "dimension" not in dg


# ─── Persisted options round trip (real store, _fields_to_schema_json) ──


@pytest.mark.anyio
async def test_options_roundtrip_through_store(tmp_path) -> None:
    store = await _make_store(tmp_path)
    dt_id = await store.create(
        workspace_id="ws_t",
        name="竞品报道分析",
        created_by="u1",
        fields=[
            ExtractField(name="情感倾向", type="select", options=["正面", "中性", "负面"]),
            ExtractField(name="涉及产品", type="multi_select", options=["A", "B"]),
            ExtractField(name="是否提及价格", type="boolean"),
            ExtractField(name="合同金额", type="currency"),
        ],
    )
    dto = await store.get(dt_id)
    assert dto is not None
    by_name = {f.name: f for f in dto.fields}
    assert by_name["情感倾向"].options == ["正面", "中性", "负面"]
    assert by_name["涉及产品"].type == "multi_select"
    assert by_name["涉及产品"].options == ["A", "B"]
    assert by_name["是否提及价格"].type == "boolean"
    assert by_name["是否提及价格"].options is None
    assert by_name["合同金额"].options is None
    # to_dict API also includes options.
    api = dto.to_dict()
    api_by_name = {f["name"]: f for f in api["fields"]}
    assert api_by_name["情感倾向"]["options"] == ["正面", "中性", "负面"]
    assert "options" not in api_by_name["合同金额"]


@pytest.mark.anyio
async def test_store_rejects_invalid_new_type_fields(tmp_path) -> None:
    store = await _make_store(tmp_path)
    with pytest.raises(ValueError, match="options_required"):
        await store.create(
            workspace_id="ws_t",
            name="bad",
            created_by="u1",
            fields=[ExtractField(name="x", type="select")],
        )


# ─── normalize_field_value truth table (same M3 matrix semantics) ──


def test_normalize_select() -> None:
    opts = ["正面", "中性", "负面"]
    assert normalize_field_value("正面", "select", opts) == {"choice": "正面"}
    # Folded match: full/halfwidth, case, whitespace.
    assert normalize_field_value(" 正面 ", "select", opts) == {"choice": "正面"}
    assert normalize_field_value("褒贬不一", "select", opts) == {"raw_string": "褒贬不一"}
    assert normalize_field_value(None, "select", opts) is None


def test_normalize_multi_select() -> None:
    opts = ["星舰 Pro", "星舰 Air", "星舰 mini"]
    # JSON array / Python repr (M3 dogfood ship-blocker semantics migrated) / enumeration-comma separated.
    assert normalize_field_value('["星舰 Pro", "星舰 Air"]', "multi_select", opts) == {"choices": ["星舰 Pro", "星舰 Air"]}
    assert normalize_field_value("['星舰 Pro', '星舰 Air']", "multi_select", opts) == {"choices": ["星舰 Pro", "星舰 Air"]}
    assert normalize_field_value("星舰 Air、星舰 Pro", "multi_select", opts) == {"choices": ["星舰 Pro", "星舰 Air"]}
    assert normalize_field_value("星舰 Ultra", "multi_select", opts) == {"raw_string": "星舰 Ultra"}


def test_normalize_boolean() -> None:
    assert normalize_field_value("是", "boolean") == {"flag": True}
    assert normalize_field_value("No", "boolean") == {"flag": False}
    assert normalize_field_value("大概吧", "boolean") == {"raw_string": "大概吧"}


def test_normalize_legacy_delegation_unchanged() -> None:
    """存量四型 + question 委托 v1（residual guard 原样——聚合诚实的上游防线）。"""
    assert normalize_field_value("$300/month", "currency") == {"raw_string": "$300/month"}
    assert normalize_field_value("¥300", "currency") == {"amount": "300", "currency": None}
    assert normalize_field_value("hello", "question") == {"value": "hello"}
    assert normalize_field_value("2026-04-12", "date") == {"value": "2026-04-12"}


# ─── _coalesce_field_expr five arms + rows convergence ──────────


def test_coalesce_arms_additive() -> None:
    sql = _coalesce_field_expr("f1")
    arms = [
        "$.f1[0].normalized.amount",
        "$.f1[0].normalized.value",
        "$.f1[0].normalized.choice",
        "$.f1[0].normalized.flag",
        "$.f1.value",
    ]
    # Legacy amount/value arms first, V1 fallback last; new arms additive between.
    positions = [sql.index(a) for a in arms]
    assert positions == sorted(positions)


def test_rows_coalesce_is_same_authority() -> None:
    assert rows_coalesce is _coalesce_field_expr


# ─── wrap new types into choice/choices/flag ─────────────────────


def test_wrap_new_types_normalized() -> None:
    fields = {
        "情感倾向": {"value": "正面", "confidence": 0.9, "source_snippet": "…好评…"},
        "是否提及价格": {"value": "true", "confidence": 0.8, "source_snippet": None},
        "合同金额": {"value": "¥300", "confidence": 0.7, "source_snippet": None},
    }
    defs = [
        ExtractField(name="情感倾向", type="select", options=["正面", "负面"]),
        ExtractField(name="是否提及价格", type="boolean"),
        ExtractField(name="合同金额", type="currency"),
    ]
    out = wrap_extractor_result_to_variants(fields, defs)
    assert out["情感倾向"][0]["normalized"] == {"choice": "正面"}
    assert out["是否提及价格"][0]["normalized"] == {"flag": True}
    # Legacy currency byte-identical.
    assert out["合同金额"][0]["normalized"] == {"amount": "300", "currency": None}
    # Variant JSON-serializable for persistence.
    json.dumps(out, ensure_ascii=False)
