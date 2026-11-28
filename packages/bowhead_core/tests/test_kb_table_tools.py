"""kb-knowledge-tables sprint Day 3: kb.list_tables / kb.query_records / kb.get_record tool tests.

走 ToolRegistry.execute() 实测 (而不是直调 handler) — 验:
- ws_id 强制走 ctx (LLM 不能传 ws_id 绕)
- doc_type_id required
- cross-ws leak 防御
- SQL where/sort/limit 正常
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest
from bowhead_core.agent_knowledge_policy import bind_knowledge_scope, require_supported_knowledge_access
from bowhead_core.kb.doc_type_store import ExtractField, KbDocTypeStore
from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
from bowhead_core.kb.extracted_rows_store import KbExtractedRowsStore, RowInput
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.permission import KbPermission, ScopeContext
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    build_default_registry,
)

TEAM = "team_t"


class _ReadRuleAuthorizer:
    """The KB read rule without resource grants, as the gateway's ``kb_file_authorizer`` applies it."""

    async def __call__(self, file, user_id, team_id, workspace_id, **_options):
        return (await self.batch([file], user_id, team_id, workspace_id))[file.file_id]

    async def batch(self, files, user_id, team_id, workspace_id, **_options):
        caller = ScopeContext(user_id=user_id, team_id=team_id, workspace_id=workspace_id)
        return {f.file_id: not f.soft_deleted_at and f.team_id == team_id and KbPermission.can_read(f, caller) for f in files}


class _SpaceFiles:
    """Files store for the tests below whose records come from ws_a's Space files f0-f2."""

    def __init__(self) -> None:
        self._files = {
            file_id: KbFileDTO(
                file_id=file_id,
                artifact_id=f"art_{file_id}",
                filename=f"{file_id}.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                scope="space",
                scope_id="ws_a",
                workspace_id="ws_a",
                team_id=TEAM,
                uploaded_by="u1",
                uploaded_at="2026-01-01T00:00:00Z",
            )
            for file_id in ("f0", "f1", "f2")
        }

    async def get(self, file_id):
        return self._files.get(file_id)

    async def list_access_rows_for_scopes(self, *, team_id, personal_owner_id, space_id, team_scope_id):
        return [f for f in self._files.values() if f.team_id == team_id and f.workspace_id == space_id]


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
        team_id=TEAM,
        kb_doc_type_store=dt_store,
        kb_extracted_record_store=rec_store,
        kb_files_store=_SpaceFiles(),
        kb_file_authorizer=_ReadRuleAuthorizer(),
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
    assert inv.result["note"] == "Record not found or deleted"


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
    assert inv.result["note"] == "Record not found or deleted"


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


# ─── Ordinary turns read records only from readable files ────────────
# The worker stores a personal file's records and rows under its upload Space, so a Space
# filter alone served them to every member of that Space.

OWNER, MEMBER = "u_owner", "u_member"


@pytest.fixture
async def shared_space(tmp_path, initialize_migrated_kb_files_database):
    """OWNER's personal file, a Space file and a Team file in ws_a, plus a ws_b file; one record and one row each."""
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files = KbFilesStore(tmp_path / "kb_files.db")
    await files.initialize()
    with sqlite3.connect(tmp_path / "kb_files.db") as db:
        for file_id, scope, scope_id, workspace_id in (
            ("kbf_personal", "personal", OWNER, "ws_a"),
            ("kbf_space", "space", "ws_a", "ws_a"),
            ("kbf_team", "team", TEAM, ""),
            ("kbf_other_space", "space", "ws_b", "ws_b"),
        ):
            db.execute(
                "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,workspace_id,team_id,uploaded_by,uploaded_at,parse_status) "
                "VALUES (?,?,?,'application/pdf',1,?,?,?,?,?,'2026-11-27T00:00:00Z','done')",
                (file_id, f"art_{file_id}", f"{file_id}.pdf", scope, scope_id, workspace_id, TEAM, OWNER),
            )
    dt_store, rec_store = await _build_stores(tmp_path)
    rows_store = KbExtractedRowsStore(tmp_path / "rows.db")
    await rows_store.initialize()
    contracts = await dt_store.create(workspace_id="ws_a", name="Contracts", created_by=OWNER, fields=[ExtractField(name="amount", type="number")])
    ledger = await dt_store.create(
        workspace_id="ws_a", name="Ledger", created_by=OWNER, fields=[ExtractField(name="amount", type="number")], cardinality="many"
    )
    records = {}
    for file_id in ("kbf_personal", "kbf_space", "kbf_team"):
        records[file_id] = await rec_store.create(workspace_id="ws_a", doc_type_id=contracts, file_id=file_id, fields={"amount": {"value": 1}})
        await rows_store.create_many(
            rows=[RowInput(row_seq=1, page=1, fields={"amount": {"value": 1}}, dedup_key_hash=1)],
            file_id=file_id,
            doc_type_id=ledger,
            workspace_id="ws_a",
        )
    records["kbf_other_space"] = await rec_store.create(workspace_id="ws_b", doc_type_id=contracts, file_id="kbf_other_space", fields={})

    def ctx_for(user_id):
        return ToolExecutionContext(
            workspace_id="ws_a",
            member_uid=user_id,
            user_id=user_id,
            turn_id="t1",
            datarc=MagicMock(),
            team_id=TEAM,
            kb_doc_type_store=dt_store,
            kb_extracted_record_store=rec_store,
            kb_extracted_rows_store=rows_store,
            kb_files_store=files,
            kb_file_authorizer=_ReadRuleAuthorizer(),
        )

    return ctx_for, {"contracts": contracts, "ledger": ledger}, records


async def test_ordinary_turn_lists_and_queries_only_readable_files(shared_space) -> None:
    ctx_for, tables, _records = shared_space
    reg = build_default_registry()
    for user_id, expected in ((MEMBER, ["kbf_space", "kbf_team"]), (OWNER, ["kbf_personal", "kbf_space", "kbf_team"])):
        listed = await reg.execute("kb_list_tables", {}, ctx_for(user_id))
        assert listed.error is None
        assert {item["name"]: item["record_count"] for item in listed.result["items"]} == {"Contracts": len(expected), "Ledger": len(expected)}
        queried = await reg.execute("kb_query_records", {"doc_type_id": tables["contracts"]}, ctx_for(user_id))
        assert sorted(record["file_id"] for record in queried.result["records"]) == expected
        rows = await reg.execute("kb_query_records", {"doc_type_id": tables["ledger"]}, ctx_for(user_id))
        assert sorted(row["file_id"] for row in rows.result["rows"]) == expected
        counted = await reg.execute(
            "kb_query_records", {"doc_type_id": tables["ledger"], "aggregate": {"field": "amount", "op": "COUNT"}}, ctx_for(user_id)
        )
        assert counted.result["aggregate_result"] == [{"group_keys": {}, "value": len(expected)}]


async def test_unreadable_record_gets_the_same_note_as_another_spaces_record(shared_space) -> None:
    ctx_for, _tables, records = shared_space
    reg = build_default_registry()
    personal = await reg.execute("kb_get_record", {"record_id": records["kbf_personal"]}, ctx_for(MEMBER))
    other_space = await reg.execute("kb_get_record", {"record_id": records["kbf_other_space"]}, ctx_for(MEMBER))
    missing = await reg.execute("kb_get_record", {"record_id": "rec_missing"}, ctx_for(MEMBER))
    assert personal.result == other_space.result == missing.result == {"record": None, "note": "Record not found or deleted"}

    # An Agent limited to the Space file answers the same for a readable file outside its sources
    # (its result also carries the usual provenance of the Agent's own sources).
    agent = ctx_for(MEMBER)
    policy = require_supported_knowledge_access({"knowledge_access": {"schema_version": 1, "mode": "selected_files", "file_ids": ["kbf_space"]}})
    agent.knowledge_scope = bind_knowledge_scope(agent, policy, revision="arv_test")
    outside_sources = await reg.execute("kb_get_record", {"record_id": records["kbf_team"]}, agent)
    assert {key: outside_sources.result[key] for key in ("record", "note")} == personal.result

    for user_id, file_id in ((MEMBER, "kbf_space"), (MEMBER, "kbf_team"), (OWNER, "kbf_personal")):
        found = await reg.execute("kb_get_record", {"record_id": records[file_id]}, ctx_for(user_id))
        assert found.result["record"]["file_id"] == file_id


async def test_ordinary_turn_without_file_access_check_reads_no_records(shared_space) -> None:
    ctx_for, tables, _records = shared_space
    ctx = ctx_for(MEMBER)
    ctx.kb_file_authorizer = None
    reg = build_default_registry()
    for name, args in (("kb_list_tables", {}), ("kb_query_records", {"doc_type_id": tables["contracts"]})):
        result = (await reg.execute(name, args, ctx)).result
        assert result["reason_code"] == "knowledge_source_authorization_unavailable"
        assert not result.get("items") and not result.get("records")
