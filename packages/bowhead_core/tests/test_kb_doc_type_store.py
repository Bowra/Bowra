"""kb-knowledge-tables sprint Day 1: KbDocTypeStore CRUD tests (new schema).

V1.0 改语义: extract_schema (fields) 替 V3 columns + chunk_strategy.
"""

from __future__ import annotations

import pytest
from bowhead_core.kb.doc_type_store import (
    MAX_FIELD_DESC_LEN,
    MAX_FIELD_NAME_LEN,
    MAX_FIELDS_PER_SCHEMA,
    ExtractField,
    KbDocTypeStore,
)


async def _make_store(tmp_path) -> KbDocTypeStore:
    s = KbDocTypeStore(tmp_path / "dt.db")
    await s.initialize()
    return s


# ─── ExtractField validate ─────────


def test_extract_field_validate_ok() -> None:
    f = ExtractField(name="合同金额", type="currency", description="合同总价 RMB")
    f.validate()  # no raise


def test_extract_field_validate_empty_name() -> None:
    f = ExtractField(name="", type="string")
    with pytest.raises(ValueError, match="field name 长度"):
        f.validate()


def test_extract_field_validate_long_name() -> None:
    f = ExtractField(name="x" * (MAX_FIELD_NAME_LEN + 1))
    with pytest.raises(ValueError, match="field name 长度"):
        f.validate()


def test_extract_field_validate_invalid_type() -> None:
    f = ExtractField(name="x", type="invalid")
    with pytest.raises(ValueError, match="field type"):
        f.validate()


def test_extract_field_validate_long_desc() -> None:
    f = ExtractField(name="x", description="d" * (MAX_FIELD_DESC_LEN + 1))
    with pytest.raises(ValueError, match="field description"):
        f.validate()


# ─── doc_type CRUD ─────────


async def test_create_and_get(tmp_path) -> None:
    s = await _make_store(tmp_path)
    fields = [
        ExtractField(name="合同金额", type="currency", description="合同总金额 RMB", required=True, cross_verify=True),
        ExtractField(name="起始日", type="date", description="合同生效日期"),
        ExtractField(name="对方", type="string", extraction_hint="通常在合同首页"),
    ]
    dt_id = await s.create(
        workspace_id="ws_a",
        name="销售合同",
        created_by="u1",
        description="2024 销售合同集合",
        fields=fields,
        extractor_model="qwen-plus",
        cross_verify_enabled=True,
    )
    assert dt_id.startswith("dt_")
    dto = await s.get(dt_id)
    assert dto is not None
    assert dto.name == "销售合同"
    assert dto.extractor_model == "qwen-plus"
    assert dto.cross_verify_enabled is True
    assert dto.record_count == 0
    assert len(dto.fields) == 3
    assert dto.fields[0].name == "合同金额"
    assert dto.fields[0].type == "currency"
    assert dto.fields[0].required is True
    assert dto.fields[0].cross_verify is True
    assert dto.fields[2].extraction_hint == "通常在合同首页"


async def test_create_no_fields(tmp_path) -> None:
    """Empty fields 允许 (用户先创再加 fields)."""
    s = await _make_store(tmp_path)
    dt_id = await s.create(workspace_id="ws_a", name="空", created_by="u1")
    dto = await s.get(dt_id)
    assert dto is not None
    assert dto.fields == []


async def test_create_too_many_fields(tmp_path) -> None:
    s = await _make_store(tmp_path)
    too_many = [ExtractField(name=f"f{i}") for i in range(MAX_FIELDS_PER_SCHEMA + 1)]
    with pytest.raises(ValueError, match="V1 限单文档表字段"):
        await s.create(
            workspace_id="ws_a",
            name="X",
            created_by="u1",
            fields=too_many,
        )


async def test_create_invalid_field_type(tmp_path) -> None:
    s = await _make_store(tmp_path)
    with pytest.raises(ValueError, match="field type"):
        await s.create(
            workspace_id="ws_a",
            name="X",
            created_by="u1",
            fields=[ExtractField(name="x", type="enum")],
        )


async def test_list_for_ws_isolated(tmp_path) -> None:
    """P0-R1: list_for_ws 必须严格按 workspace_id 隔离."""
    s = await _make_store(tmp_path)
    await s.create(workspace_id="ws_a", name="销售合同", created_by="u1")
    await s.create(workspace_id="ws_a", name="员工合同", created_by="u1")
    await s.create(workspace_id="ws_b", name="财务回单", created_by="u2")
    a = await s.list_for_ws("ws_a")
    b = await s.list_for_ws("ws_b")
    assert {d.name for d in a} == {"销售合同", "员工合同"}
    assert {d.name for d in b} == {"财务回单"}


async def test_update_fields_replaces(tmp_path) -> None:
    s = await _make_store(tmp_path)
    dt_id = await s.create(
        workspace_id="ws_a",
        name="原名",
        created_by="u1",
        fields=[ExtractField(name="X")],
    )
    ok = await s.update(
        dt_id,
        name="新名",
        fields=[
            ExtractField(name="Y", type="currency"),
            ExtractField(name="Z", type="date"),
        ],
        cross_verify_enabled=False,
    )
    assert ok
    dto = await s.get(dt_id)
    assert dto.name == "新名"
    assert dto.cross_verify_enabled is False
    assert [f.name for f in dto.fields] == ["Y", "Z"]


async def test_update_partial(tmp_path) -> None:
    """update 只动指定字段, 其他字段保留."""
    s = await _make_store(tmp_path)
    dt_id = await s.create(
        workspace_id="ws_a",
        name="orig",
        created_by="u1",
        description="orig desc",
        fields=[ExtractField(name="X")],
        extractor_model="qwen-plus",
    )
    ok = await s.update(dt_id, name="新")
    assert ok
    dto = await s.get(dt_id)
    assert dto.name == "新"
    assert dto.description == "orig desc"
    assert len(dto.fields) == 1
    assert dto.extractor_model == "qwen-plus"


async def test_update_too_many_fields(tmp_path) -> None:
    s = await _make_store(tmp_path)
    dt_id = await s.create(workspace_id="ws_a", name="X", created_by="u1")
    too_many = [ExtractField(name=f"f{i}") for i in range(MAX_FIELDS_PER_SCHEMA + 1)]
    with pytest.raises(ValueError, match="V1 限单文档表字段"):
        await s.update(dt_id, fields=too_many)


async def test_update_returns_false_when_no_set(tmp_path) -> None:
    s = await _make_store(tmp_path)
    dt_id = await s.create(workspace_id="ws_a", name="X", created_by="u1")
    ok = await s.update(dt_id)
    assert ok is False


async def test_unique_per_ws(tmp_path) -> None:
    """Same name in same ws → SQL UNIQUE violation."""
    import aiosqlite

    s = await _make_store(tmp_path)
    await s.create(workspace_id="ws_a", name="重名", created_by="u1")
    with pytest.raises(aiosqlite.IntegrityError):
        await s.create(workspace_id="ws_a", name="重名", created_by="u1")


async def test_same_name_different_ws_ok(tmp_path) -> None:
    s = await _make_store(tmp_path)
    a = await s.create(workspace_id="ws_a", name="销售合同", created_by="u1")
    b = await s.create(workspace_id="ws_b", name="销售合同", created_by="u2")
    assert a != b


async def test_soft_delete_excludes(tmp_path) -> None:
    s = await _make_store(tmp_path)
    dt_id = await s.create(workspace_id="ws_a", name="X", created_by="u1")
    ok = await s.soft_delete(dt_id)
    assert ok
    assert await s.get(dt_id) is None
    assert await s.list_for_ws("ws_a") == []


async def test_soft_delete_returns_false_for_already_deleted(tmp_path) -> None:
    s = await _make_store(tmp_path)
    dt_id = await s.create(workspace_id="ws_a", name="X", created_by="u1")
    await s.soft_delete(dt_id)
    ok2 = await s.soft_delete(dt_id)
    assert ok2 is False


async def test_update_record_count_delta(tmp_path) -> None:
    """Cache invalidation: +/- record_count."""
    s = await _make_store(tmp_path)
    dt_id = await s.create(workspace_id="ws_a", name="X", created_by="u1")
    await s.update_record_count(dt_id, +3)
    dto = await s.get(dt_id)
    assert dto.record_count == 3
    await s.update_record_count(dt_id, -1)
    dto = await s.get(dt_id)
    assert dto.record_count == 2


async def test_to_dict_round_trip(tmp_path) -> None:
    """to_dict 含全字段, JSON 友好."""
    s = await _make_store(tmp_path)
    fields = [
        ExtractField(name="A", type="currency", required=True, cross_verify=True, extraction_hint="hint"),
    ]
    dt_id = await s.create(
        workspace_id="ws_a",
        name="X",
        created_by="u1",
        fields=fields,
    )
    dto = await s.get(dt_id)
    d = dto.to_dict()
    assert d["doc_type_id"] == dt_id
    assert d["fields"][0]["name"] == "A"
    assert d["fields"][0]["type"] == "currency"
    assert d["fields"][0]["required"] is True
    assert d["fields"][0]["cross_verify"] is True
    assert d["fields"][0]["extraction_hint"] == "hint"
    assert d["record_count"] == 0
    assert d["cross_verify_enabled"] is True
