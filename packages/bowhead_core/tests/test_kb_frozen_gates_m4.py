"""kb-matrix M4 决策 12: 冻结生态 gate 的 core 层（rows 原语 + worker gate③ + 分类候选②）。

journey review P0 实证：flag（auto_extract=off）单独形同虚设——五个 LIVE 面仍把
graduated 当活模板。本文件锁 core 半边；gateway 半边见
test_kb_frozen_gates_gateway_m4.py。
"""

from __future__ import annotations

import pytest
from bowhead_core.kb.doc_type_store import (
    SYSTEM_TEMPLATE_SEED_USER,
    KbDocTypeStore,
    is_column_group_template,
)
from bowhead_core.kb.extracted_rows_store import KbExtractedRowsStore

WS = "ws_f"


async def _rows(tmp_path) -> KbExtractedRowsStore:
    s = KbExtractedRowsStore(db_path=tmp_path / "rows.db")
    await s.initialize()
    return s


def _entry(file_id: str) -> dict:
    return {
        "file_id": file_id,
        "fields_v2": {
            "金额": [{"is_primary": True, "normalized": {"amount": "1", "currency": None}, "source_excerpt": "原文一段", "extracted_by": "tier1"}]
        },
        "edited_fields": [],
        "page": None,
    }


# ─── Gate④: file-delete cascade exemption + doc_type cascade + purge ──


@pytest.mark.anyio
async def test_soft_delete_for_file_exclusion(tmp_path) -> None:
    rows = await _rows(tmp_path)
    # Same file has row under both graduated and ordinary per-row doc_types.
    await rows.create_many_prewrapped([_entry("f1")], doc_type_id="dt_frozen", workspace_id=WS, created_by="u")
    await rows.create_many_prewrapped([_entry("f1")], doc_type_id="dt_live", workspace_id=WS, created_by="u")
    assert set(await rows.list_doc_type_ids_for_file("f1")) == {"dt_frozen", "dt_live"}
    n = await rows.soft_delete_for_file("f1", exclude_doc_type_ids=["dt_frozen"])
    assert n == 1  # Ordinary only deleted.
    # Graduated retained (chat count unchanged: point-in-time snapshot).
    assert await rows.count_for_doc_type(workspace_id=WS, doc_type_id="dt_frozen") == 1
    assert await rows.count_for_doc_type(workspace_id=WS, doc_type_id="dt_live") == 0
    # No exemption = old behavior.
    n2 = await rows.soft_delete_for_file("f1")
    assert n2 == 1


@pytest.mark.anyio
async def test_soft_delete_for_doc_type_cascade(tmp_path) -> None:
    rows = await _rows(tmp_path)
    await rows.create_many_prewrapped([_entry("f1"), _entry("f2")], doc_type_id="dt_g", workspace_id=WS, created_by="u")
    n = await rows.soft_delete_for_doc_type("dt_g", workspace_id=WS)
    assert n == 2
    assert await rows.count_for_doc_type(workspace_id=WS, doc_type_id="dt_g") == 0


@pytest.mark.anyio
async def test_purge_provenance_strips_excerpt_keeps_normalized(tmp_path) -> None:
    rows = await _rows(tmp_path)
    await rows.create_many_prewrapped([_entry("f1")], doc_type_id="dt_g", workspace_id=WS, created_by="u")
    n = await rows.purge_provenance_for_file("f1")
    assert n == 1
    got = await rows.query_rows(workspace_id=WS, doc_type_id="dt_g", limit=5)
    v = got[0].fields["金额"][0]
    assert v["source_excerpt"] is None  # Excerpt erased (FR-7 compliance).
    assert v["normalized"]["amount"] == "1"  # Value retained.


# ─── Gate③: worker extraction entry rejects (explicit-binding defense in depth) ──


@pytest.mark.anyio
async def test_worker_extraction_entry_refuses_frozen(tmp_path, monkeypatch) -> None:
    """worker._run_structured_extraction 对 auto_extract=off 的 doc_type 提前返回
    ——冻结快照/列组模板不是抽取目标（此前显式绑定路绕过该 flag）。"""
    from bowhead_core.kb.worker import KbWorker

    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    dt_frozen = await dt_store.create(
        workspace_id=WS,
        name="冻结快照",
        created_by="u",
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx",
        graduated_from_seq=1,
        graduated_mode="snapshot",
    )

    class _ExplodingExtractor:
        async def extract(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError("extractor must NOT be called for frozen doc_type")

    class _ExplodingRecStore:
        async def create(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError("record must NOT be written for frozen doc_type")

    worker = KbWorker(
        files_store=None,  # type: ignore[arg-type]  # gate returns before any store use
        store=None,  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        artifact_fetcher=None,
        doc_type_store=dt_store,
        extracted_record_store=_ExplodingRecStore(),  # type: ignore[arg-type]
        structured_extractor=_ExplodingExtractor(),  # type: ignore[arg-type]
    )

    class _File:
        file_id = "f1"
        doc_type_id = dt_frozen
        auto_classified_doc_type_id = None
        workspace_id = WS

    # No crash = gate works (two Exploding stubs prove no LLM/storage touched).
    await worker._run_structured_extraction(_File(), load_result=None)


# ─── Gate②: exclude classifier candidates (predicate; filter inline in classifier) ──


@pytest.mark.anyio
async def test_column_group_templates_identified_for_candidate_filter(tmp_path) -> None:
    dt = KbDocTypeStore(tmp_path / "dt.db")
    await dt.initialize()
    d_manual = await dt.create(workspace_id=WS, name="普通抽取型", created_by="u")
    d_seed = await dt.create(workspace_id=WS, name="内置模板", created_by=SYSTEM_TEMPLATE_SEED_USER)
    d_tpl = await dt.create(
        workspace_id=WS, name="存为模板", created_by="u", graduated_from_matrix_id="mx", graduated_from_seq=1, graduated_mode="template"
    )
    d_grad = await dt.create(
        workspace_id=WS,
        name="转正快照",
        created_by="u",
        cardinality="many",
        auto_extract_enabled=False,
        graduated_from_matrix_id="mx",
        graduated_from_seq=2,
        graduated_mode="snapshot",
    )
    all_dts = await dt.list_for_ws(WS)
    filtered = [c.doc_type_id for c in all_dts if not is_column_group_template(c)]
    assert filtered == [d_manual]  # Exclude seed/template/graduated.
    assert {d_seed, d_tpl, d_grad}.isdisjoint(set(filtered))
