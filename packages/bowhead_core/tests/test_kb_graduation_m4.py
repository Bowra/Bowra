"""kb-matrix M4 FR-40: 转正装配 + 血缘原语 + 直存 + 自愈 sweep + 迁移。

覆盖（tech-design-m4 r3 §4.2/§9 门）：
- build_graduation_fields：question 仅降 question（文本进 hint）；新三型保真 options。
- normalize_date_iso：ISO 幂等 / 中式 / 西式 / 乱格式回落 None（不哨兵化）。
- build_graduation_entries：done/needs_review 进、failed/缺格缺席（null 语义）、
  edited → 行级字段表、date 归 ISO、sanitize（bidi/user_edit 串）、
  **fields_v2 非空**（P0 回归：绕 create_many re-wrap 的直存不丢字段）。
- doc_type_store 血缘原语：create 3 列 round-trip / find_graduated（幂等键）/
  name_exists / set_graduated_mode flip / list_for_ws 隐 pending / soft_delete
  墓碑让名 / kind 派生。
- create_many_prewrapped：真库直存非空 + 幂等重放 UNIQUE skip。
- sweep：pending+rows>0 → flip+计数（自愈）；pending+0 rows → 回收；
  template/snapshot 永不触碰。
- migrate_kb_doc_types_add_m4_columns：幂等两跑。
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.kb.doc_type_migration import migrate_kb_doc_types_add_m4_columns
from bowhead_core.kb.doc_type_store import (
    ExtractField,
    KbDocTypeStore,
    derive_doc_type_kind,
    is_column_group_template,
    is_frozen_snapshot,
)
from bowhead_core.kb.extracted_rows_store import KbExtractedRowsStore
from bowhead_core.kb.graduation import (
    build_graduation_entries,
    build_graduation_fields,
    compose_description,
    normalize_date_iso,
    sweep_pending_graduations,
)
from bowhead_core.kb.matrix_store import MatrixCellDTO

WS = "ws_g"


async def _dt_store(tmp_path) -> KbDocTypeStore:
    s = KbDocTypeStore(tmp_path / "dt.db")
    await s.initialize()
    return s


async def _rows_store(tmp_path) -> KbExtractedRowsStore:
    s = KbExtractedRowsStore(db_path=tmp_path / "rows.db")
    await s.initialize()
    return s


def _cell(
    file_id: str,
    column_id: str,
    *,
    status: str = "done",
    variants: list[dict[str, Any]] | None = None,
    edited: bool = False,
) -> MatrixCellDTO:
    return MatrixCellDTO(
        matrix_id="mx_t",
        file_id=file_id,
        column_id=column_id,
        workspace_id=WS,
        team_id="team_g",
        status=status,
        queued_from=None,
        variants=variants
        if variants is not None
        else [
            {
                "raw": None,
                "normalized": {"value": "v"},
                "effective_at": None,
                "is_primary": True,
                "extracted_by": "tier1",
                "confidence": 0.9,
                "source_excerpt": "原文摘录",
                "chunk_id": "chk_1",
                "page": 1,
            }
        ],
        fail_reason=None,
        fail_hint=None,
        edited_by_user=edited,
        verified=False,
        cached=False,
        prompt_version=1,
        file_version=1,
        content_hash=None,
        cache_key=None,
        idempotency_key=None,
        run_id=None,
        seq=1,
        created_at="2026-07-16T00:00:00+00:00",
        updated_at="2026-07-16T00:00:00+00:00",
    )


# ─── build_graduation_fields ────────────────────────────────


def test_fields_question_degrades_only_question() -> None:
    cols = [
        {"column_id": "c1", "name": "核心主张", "type": "question", "question": "报道核心论点？", "extraction_hint": ""},
        {"column_id": "c2", "name": "情感倾向", "type": "select", "question": "倾向？", "options": ["正面", "负面"]},
        {"column_id": "c3", "name": "是否提及价格", "type": "boolean", "question": "提价格？"},
        {"column_id": "c4", "name": "合同金额", "type": "currency", "question": "金额？"},
    ]
    fields = build_graduation_fields(cols)
    by = {f.name: f for f in fields}
    # question → string, question text in hint (semantics preserved).
    assert by["核心主张"].type == "string"
    assert "报道核心论点" in by["核心主张"].extraction_hint
    # Preserve three new types (⚠ _degraded_field also degrades select/boolean, review P3-9).
    assert by["情感倾向"].type == "select"
    assert by["情感倾向"].options == ["正面", "负面"]
    assert by["是否提及价格"].type == "boolean"
    assert by["合同金额"].type == "currency"
    for f in fields:
        f.validate()  # All pass kernel validation (graduation create runs it).


# ─── normalize_date_iso ─────────────────────────────────────


def test_date_iso() -> None:
    assert normalize_date_iso("2026-04-12") == "2026-04-12"  # Idempotent.
    assert normalize_date_iso("2026年4月12日") == "2026-04-12"  # Chinese format.
    assert normalize_date_iso("2026/04/12") == "2026-04-12"
    assert normalize_date_iso("面议") is None  # None fallback (caller keeps original, no sentinel).
    assert normalize_date_iso("") is None


# ─── build_graduation_entries ───────────────────────────────


def test_entries_assembly_p0_nonempty_and_states() -> None:
    files = [{"file_id": "f1", "char_count": 10}, {"file_id": "f2", "char_count": 10}]
    cols = [
        {"column_id": "c1", "name": "金额", "type": "currency"},
        {"column_id": "c2", "name": "签署日期", "type": "date"},
        {"column_id": "c3", "name": "备注", "type": "string"},
    ]
    cells = [
        _cell(
            "f1",
            "c1",
            variants=[
                {
                    "is_primary": True,
                    "normalized": {"amount": "300", "currency": None},
                    "extracted_by": "tier1",
                    "source_excerpt": "金额三百",
                    "chunk_id": "k1",
                    "page": 2,
                }
            ],
        ),
        _cell(
            "f1",
            "c2",
            variants=[
                {
                    "is_primary": True,
                    "normalized": {"value": "2026年4月12日"},
                    "extracted_by": "tier1",
                    "source_excerpt": "签署于…",
                    "chunk_id": None,
                    "page": None,
                }
            ],
        ),
        _cell("f1", "c3", status="failed", variants=[]),
        _cell(
            "f2",
            "c1",
            status="needs_review",
            variants=[
                {
                    "is_primary": True,
                    "normalized": {"raw_string": "$300/month"},
                    "extracted_by": "tier1",
                    "source_excerpt": "…",
                    "chunk_id": None,
                    "page": None,
                }
            ],
        ),
        _cell(
            "f2",
            "c3",
            edited=True,
            variants=[
                {
                    "is_primary": True,
                    "normalized": {"value": "人工备注"},
                    "extracted_by": "user_edit",
                    "raw": "人工备注",
                    "source_excerpt": None,
                    "chunk_id": None,
                    "page": None,
                }
            ],
        ),
    ]
    entries, stats = build_graduation_entries(files, cols, cells)
    by_file = {e["file_id"]: e for e in entries}
    # P0 regression: direct-stored fields **nonempty** (alternative to lossy rewrap actually holds data).
    f1 = by_file["f1"]["fields_v2"]
    assert f1["金额"][0]["normalized"] == {"amount": "300", "currency": None}
    # Preserve provenance keys (citation registry reads source_excerpt/chunk_id/page).
    assert f1["金额"][0]["source_excerpt"] == "金额三百"
    assert f1["金额"][0]["chunk_id"] == "k1"
    assert f1["金额"][0]["page"] == 2
    # Dates ISO, graduation-confined.
    assert f1["签署日期"][0]["normalized"]["value"] == "2026-04-12"
    # Failed → missing field (null semantics).
    assert "备注" not in f1
    # needs_review retains sentinel (excluded aggregate, honest coverage).
    assert by_file["f2"]["fields_v2"]["金额"][0]["normalized"] == {"raw_string": "$300/month"}
    # Edited → row-level fields table (F10 mapping).
    assert by_file["f2"]["edited_fields"] == ["备注"]
    assert stats["cells_included"] == 4
    assert stats["cells_absent"] == 2  # f1/c3 failed + f2/c2 missing.
    assert stats["cells_needs_review"] == 1


def test_entries_sanitize_bidi_and_user_edit() -> None:
    files = [{"file_id": "f1"}]
    cols = [{"column_id": "c1", "name": "备注", "type": "string"}]
    dirty = "正常‮反向​文本"
    cells = [
        _cell(
            "f1",
            "c1",
            edited=True,
            variants=[
                {
                    "is_primary": True,
                    "normalized": {"value": dirty},
                    "extracted_by": "user_edit",
                    "raw": dirty,
                    "source_excerpt": dirty,
                    "chunk_id": None,
                    "page": None,
                },
            ],
        )
    ]
    entries, _ = build_graduation_entries(files, cols, cells)
    v = entries[0]["fields_v2"]["备注"][0]
    for field_val in (v["source_excerpt"], v["raw"], v["normalized"]["value"]):
        assert "‮" not in field_val and "​" not in field_val
    # Deep-copy leaves original cells untouched.
    assert "‮" in cells[0].variants[0]["raw"]


# ─── doc_type_store lineage primitives ──────────────────────────


@pytest.mark.anyio
async def test_graduation_meta_roundtrip_and_primitives(tmp_path) -> None:
    store = await _dt_store(tmp_path)
    dt_id = await store.create(
        workspace_id=WS,
        name="供应商合同审阅 2026Q2",
        created_by="u1",
        fields=[ExtractField(name="金额", type="currency")],
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx_1",
        graduated_from_seq=42,
        graduated_mode="pending_snapshot",
    )
    dto = await store.get(dt_id)
    assert dto is not None
    assert (dto.graduated_from_matrix_id, dto.graduated_from_seq, dto.graduated_mode) == ("mx_1", 42, "pending_snapshot")
    assert derive_doc_type_kind(dto) == "graduated"
    assert is_frozen_snapshot(dto) and is_column_group_template(dto)
    # Pending hidden from list.
    assert all(d.doc_type_id != dt_id for d in await store.list_for_ws(WS))
    # Idempotency hit / miss.
    assert [d.doc_type_id for d in await store.find_graduated_for_matrix(WS, "mx_1", seq=42)] == [dt_id]
    assert await store.find_graduated_for_matrix(WS, "mx_1", seq=43) == []
    # Flip → visible.
    assert await store.set_graduated_mode(dt_id, "snapshot")
    assert any(d.doc_type_id == dt_id for d in await store.list_for_ws(WS))
    # to_dict kind/lineage additive.
    d = (await store.get(dt_id)).to_dict()  # type: ignore[union-attr]
    assert d["kind"] == "graduated" and d["graduated_from_matrix_id"] == "mx_1"
    # Name collision → soft-delete tombstone frees name.
    assert await store.name_exists(WS, "供应商合同审阅 2026Q2")
    assert await store.soft_delete(dt_id)
    assert not await store.name_exists(WS, "供应商合同审阅 2026Q2")


@pytest.mark.anyio
async def test_m4_migration_idempotent(tmp_path) -> None:
    store = await _dt_store(tmp_path)
    del store
    await migrate_kb_doc_types_add_m4_columns(tmp_path / "dt.db")
    await migrate_kb_doc_types_add_m4_columns(tmp_path / "dt.db")  # Two runs safe (PRAGMA sniff).
    # Normal read/write after migration.
    store2 = KbDocTypeStore(tmp_path / "dt.db")
    dt_id = await store2.create(
        workspace_id=WS, name="x", created_by="u", graduated_mode="template", graduated_from_matrix_id="mx", graduated_from_seq=1
    )
    dto = await store2.get(dt_id)
    assert dto is not None and dto.graduated_mode == "template"
    assert derive_doc_type_kind(dto) == "template"


# ─── create_many_prewrapped ─────────────────────────────────


@pytest.mark.anyio
async def test_prewrapped_direct_store_and_idempotent_replay(tmp_path) -> None:
    rows = await _rows_store(tmp_path)
    entries = [
        {
            "file_id": "f1",
            "fields_v2": {
                "金额": [
                    {
                        "is_primary": True,
                        "normalized": {"amount": "300", "currency": None},
                        "extracted_by": "tier1",
                        "source_excerpt": "…",
                        "chunk_id": "k",
                        "page": 1,
                    }
                ]
            },
            "edited_fields": [],
            "page": None,
        },
        {
            "file_id": "f2",
            "fields_v2": {"金额": [{"is_primary": True, "normalized": {"value": "n/a"}, "extracted_by": "user_edit"}]},
            "edited_fields": ["金额"],
            "page": None,
        },
    ]
    r1 = await rows.create_many_prewrapped(entries, doc_type_id="dt_x", workspace_id=WS, created_by="u1")
    assert (r1.inserted_count, r1.skipped_dup_count) == (2, 0)
    # Read back nonempty fields (P0) + edited_by_user row-level mapping.
    got = await rows.query_rows(workspace_id=WS, doc_type_id="dt_x", limit=10)
    by_file = {r.file_id: r for r in got}
    assert by_file["f1"].fields["金额"][0]["normalized"]["amount"] == "300"
    assert by_file["f2"].edited_by_user == ["金额"]
    assert by_file["f1"].row_seq == 1
    # Idempotent replay: same-file UNIQUE skip.
    r2 = await rows.create_many_prewrapped(entries, doc_type_id="dt_x", workspace_id=WS, created_by="u1")
    assert (r2.inserted_count, r2.skipped_dup_count) == (0, 2)
    assert await rows.count_for_doc_type(workspace_id=WS, doc_type_id="dt_x") == 2


# ─── Sweep self-healing ─────────────────────────────────────────


@pytest.mark.anyio
async def test_sweep_flips_reclaims_and_never_touches_templates(tmp_path) -> None:
    dt = await _dt_store(tmp_path)
    rows = await _rows_store(tmp_path)
    future = "9999-01-01T00:00:00+00:00"  # Future threshold makes all pending stale.

    # ① pending +rows>0 →flip + row_count heals.
    dt_ok = await dt.create(
        workspace_id=WS,
        name="有数据",
        created_by="u",
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx_a",
        graduated_from_seq=1,
        graduated_mode="pending_snapshot",
    )
    await rows.create_many_prewrapped(
        [
            {
                "file_id": "f1",
                "fields_v2": {"金额": [{"is_primary": True, "normalized": {"amount": "1", "currency": None}}]},
                "edited_fields": [],
                "page": None,
            }
        ],
        doc_type_id=dt_ok,
        workspace_id=WS,
        created_by="u",
    )
    # ② pending +0 rows →reclaim (tombstone frees name).
    dt_empty = await dt.create(
        workspace_id=WS,
        name="空残留",
        created_by="u",
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx_b",
        graduated_from_seq=1,
        graduated_mode="pending_snapshot",
    )
    # ③ Never touch templates (r2 predicate wrongly deleted these).
    dt_tpl = await dt.create(
        workspace_id=WS, name="合法模板", created_by="u", graduated_from_matrix_id="mx_c", graduated_from_seq=1, graduated_mode="template"
    )

    counts = await sweep_pending_graduations(dt, rows, older_than_iso=future)
    assert counts == {"flipped": 1, "reclaimed": 1}
    ok = await dt.get(dt_ok)
    assert ok is not None and ok.graduated_mode == "snapshot" and ok.row_count == 1
    assert await dt.get(dt_empty) is None  # Soft delete (get filters).
    assert not await dt.name_exists(WS, "空残留")  # Name freed.
    tpl = await dt.get(dt_tpl)
    assert tpl is not None and tpl.graduated_mode == "template"  # Untouched.


def test_compose_description() -> None:
    d = compose_description("供应商合同审阅 2026Q2", 12, 7, "2026-07-16")
    assert "供应商合同审阅 2026Q2" in d and "12" in d and "7" in d
