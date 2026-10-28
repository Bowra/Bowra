"""kb-knowledge-tables sprint Day 3: kb.list_tables / kb.query_records / kb.get_record tool tests.

走 ToolRegistry.execute() 实测 (而不是直调 handler) — 验:
- ws_id 强制走 ctx (LLM 不能传 ws_id 绕)
- doc_type_id required
- cross-ws leak 防御
- SQL where/sort/limit 正常
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bowhead_core.kb.doc_type_store import ExtractField, KbDocTypeStore
from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    build_default_registry,
)


async def _build_stores(tmp_path):
    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    rec_store = KbExtractedRecordStore(tmp_path / "rec.db")
    await rec_store.initialize()
    return dt_store, rec_store


def _ctx(workspace_id: str = "ws_a", *, dt_store=None, rec_store=None):
    """Build minimal ToolExecutionContext for kb.* table tools."""
    return ToolExecutionContext(
        workspace_id=workspace_id,
        member_uid="m1",
        user_id="u1",
        turn_id="t1",
        datarc=MagicMock(),  # not used by these tools
        kb_doc_type_store=dt_store,
        kb_extracted_record_store=rec_store,
    )


# ─── kb.list_tables ────────────────────


@pytest.mark.asyncio
async def test_list_tables_returns_ws_doc_types(tmp_path) -> None:
    dt_store, rec_store = await _build_stores(tmp_path)
    fields = [
        ExtractField(name="合同金额", type="currency"),
        ExtractField(name="起始日", type="date"),
    ]
    dt_id = await dt_store.create(
        workspace_id="ws_a",
        name="销售合同",
        created_by="u1",
        fields=fields,
    )
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_list_tables",
        {},
        _ctx(dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.error is None
    items = inv.result["items"]
    assert len(items) == 1
    assert items[0]["doc_type_id"] == dt_id
    assert items[0]["name"] == "销售合同"
    assert len(items[0]["fields"]) == 2
    assert items[0]["fields"][0]["name"] == "合同金额"


@pytest.mark.asyncio
async def test_list_tables_isolates_ws(tmp_path) -> None:
    """ws_b 看不到 ws_a 的 doc_types."""
    dt_store, rec_store = await _build_stores(tmp_path)
    await dt_store.create(workspace_id="ws_a", name="A的", created_by="u1")
    await dt_store.create(workspace_id="ws_b", name="B的", created_by="u2")
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_list_tables",
        {},
        _ctx(workspace_id="ws_b", dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.error is None
    names = [i["name"] for i in inv.result["items"]]
    assert names == ["B的"]


@pytest.mark.asyncio
async def test_list_tables_ignores_llm_ws_id(tmp_path) -> None:
    """LLM 试图传 workspace_id="ws_other" → 被忽略, 仍走 ctx.workspace_id."""
    dt_store, rec_store = await _build_stores(tmp_path)
    await dt_store.create(workspace_id="ws_a", name="A的", created_by="u1")
    await dt_store.create(workspace_id="ws_other", name="不应看到", created_by="u9")
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_list_tables",
        {"workspace_id": "ws_other"},
        _ctx(workspace_id="ws_a", dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.error is None
    names = [i["name"] for i in inv.result["items"]]
    assert names == ["A的"]


@pytest.mark.asyncio
async def test_list_tables_no_store(tmp_path) -> None:
    """store None → empty items + actionable `hint` (0.40.3 Bug 6 renamed
    `note` → `hint` per Anthropic tool-output guidance: hints steer agent
    toward kb_search when tables unavailable)."""
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_list_tables",
        {},
        _ctx(workspace_id="ws_a", dt_store=None, rec_store=None),
    )
    assert inv.error is None
    assert inv.result["items"] == []
    assert "kb_search" in inv.result["hint"]


# ─── kb.query_records ────────────────────


@pytest.mark.asyncio
async def test_query_records_filter_and_sort(tmp_path) -> None:
    dt_store, rec_store = await _build_stores(tmp_path)
    dt_id = await dt_store.create(
        workspace_id="ws_a",
        name="销售合同",
        created_by="u1",
        fields=[ExtractField(name="合同金额", type="currency")],
    )
    for i, amt in enumerate([1_000_000, 5_000_000, 10_000_000]):
        await rec_store.create(
            workspace_id="ws_a",
            doc_type_id=dt_id,
            file_id=f"f{i}",
            fields={"合同金额": {"value": amt, "confidence": 0.9}},
        )
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_query_records",
        {
            "doc_type_id": dt_id,
            "where": [{"field": "合同金额", "op": ">", "value": 2_000_000}],
            "sort_by": "合同金额",
            "sort_dir": "DESC",
            "cast_numeric": ["合同金额"],
        },
        _ctx(dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.error is None
    records = inv.result["records"]
    assert len(records) == 2
    # v2 shape: fields[fname] is variant array; primary variant carries
    # normalized payload. Without doc_type_fields hint, currency degrades to
    # {value: str} (str because numbers are stringified at normalize boundary).
    assert records[0]["fields"]["合同金额"][0]["normalized"]["value"] == "10000000"
    assert records[1]["fields"]["合同金额"][0]["normalized"]["value"] == "5000000"


@pytest.mark.asyncio
async def test_query_records_missing_doc_type_id(tmp_path) -> None:
    dt_store, rec_store = await _build_stores(tmp_path)
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_query_records",
        {},
        _ctx(dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.error is None  # tool returned dict with error key, not raised
    assert inv.result["records"] == []
    assert "doc_type_id required" in inv.result["error"]


@pytest.mark.asyncio
async def test_query_records_invalid_op(tmp_path) -> None:
    """SQL injection via op rejected gracefully."""
    dt_store, rec_store = await _build_stores(tmp_path)
    dt_id = await dt_store.create(workspace_id="ws_a", name="X", created_by="u1")
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_query_records",
        {
            "doc_type_id": dt_id,
            "where": [{"field": "x", "op": "OR 1=1", "value": 0}],
        },
        _ctx(dt_store=dt_store, rec_store=rec_store),
    )
    # tool wraps ValueError → error in result.
    assert "invalid query" in inv.result["error"]


@pytest.mark.asyncio
async def test_query_records_invalid_field_name(tmp_path) -> None:
    """SQL/json-path injection via field name rejected."""
    dt_store, rec_store = await _build_stores(tmp_path)
    dt_id = await dt_store.create(workspace_id="ws_a", name="X", created_by="u1")
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_query_records",
        {
            "doc_type_id": dt_id,
            "where": [{"field": "x'); DROP", "op": "=", "value": 1}],
        },
        _ctx(dt_store=dt_store, rec_store=rec_store),
    )
    assert "invalid query" in inv.result["error"]


@pytest.mark.asyncio
async def test_query_records_ws_isolated(tmp_path) -> None:
    """LLM 给 ws_a 的 doc_type_id, caller 在 ws_b → 0 records."""
    dt_store, rec_store = await _build_stores(tmp_path)
    dt_a = await dt_store.create(workspace_id="ws_a", name="A的", created_by="u1")
    await rec_store.create(
        workspace_id="ws_a",
        doc_type_id=dt_a,
        file_id="f1",
        fields={"x": {"value": 1}},
    )
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_query_records",
        {"doc_type_id": dt_a},
        _ctx(workspace_id="ws_b", dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.error is None
    assert inv.result["records"] == []


# ─── kb.get_record ────────────────────


@pytest.mark.asyncio
async def test_get_record_returns_full_dto(tmp_path) -> None:
    dt_store, rec_store = await _build_stores(tmp_path)
    dt_id = await dt_store.create(workspace_id="ws_a", name="销售", created_by="u1")
    rid = await rec_store.create(
        workspace_id="ws_a",
        doc_type_id=dt_id,
        file_id="f1",
        fields={"金额": {"value": 5_000_000, "source_snippet": "...", "confidence": 0.9}},
    )
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_get_record",
        {"record_id": rid},
        _ctx(dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.error is None
    rec = inv.result["record"]
    assert rec["record_id"] == rid
    assert rec["fields"]["金额"][0]["normalized"]["value"] == "5000000"


@pytest.mark.asyncio
async def test_get_record_cross_ws_blocked(tmp_path) -> None:
    """ws_b caller 给 ws_a 的 record_id → 不返 (defense-in-depth: 不暴露存在性)."""
    dt_store, rec_store = await _build_stores(tmp_path)
    dt_id = await dt_store.create(workspace_id="ws_a", name="A的", created_by="u1")
    rid = await rec_store.create(
        workspace_id="ws_a",
        doc_type_id=dt_id,
        file_id="f1",
        fields={"x": {"value": 1}},
    )
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_get_record",
        {"record_id": rid},
        _ctx(workspace_id="ws_b", dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.result["record"] is None
    # Cross-ws returns uniform not-found; never distinguish missing/cross-ws, preventing enumeration.
    assert "记录不存在" in inv.result["note"]


@pytest.mark.asyncio
async def test_get_record_empty_ctx_ws_fails_closed(tmp_path) -> None:
    """P1 fix: ctx.workspace_id 缺时 fail closed (拒绝, 不 fall through)."""
    dt_store, rec_store = await _build_stores(tmp_path)
    dt_id = await dt_store.create(workspace_id="ws_a", name="A的", created_by="u1")
    rid = await rec_store.create(
        workspace_id="ws_a",
        doc_type_id=dt_id,
        file_id="f1",
        fields={"x": {"value": 1}},
    )
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_get_record",
        {"record_id": rid},
        _ctx(workspace_id="", dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.result["record"] is None
    assert "workspace_id missing" in inv.result["error"]


@pytest.mark.asyncio
async def test_get_record_missing(tmp_path) -> None:
    dt_store, rec_store = await _build_stores(tmp_path)
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_get_record",
        {"record_id": "rec_missing"},
        _ctx(dt_store=dt_store, rec_store=rec_store),
    )
    assert inv.result["record"] is None
    assert "记录不存在" in inv.result["note"]


@pytest.mark.asyncio
async def test_get_record_no_id(tmp_path) -> None:
    dt_store, rec_store = await _build_stores(tmp_path)
    reg = build_default_registry()
    inv = await reg.execute(
        "kb_get_record",
        {},
        _ctx(dt_store=dt_store, rec_store=rec_store),
    )
    assert "record_id required" in inv.result["error"]
