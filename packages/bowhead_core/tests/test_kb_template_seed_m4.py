"""kb-matrix M4 FR-41: 内置列组模板 seed（幂等 + kind 分桶 + 生态不扰动）。"""

from __future__ import annotations

import pytest
from bowhead_core.kb.doc_type_store import (
    SYSTEM_TEMPLATE_SEED_USER,
    KbDocTypeStore,
    derive_doc_type_kind,
    is_column_group_template,
)
from bowhead_core.kb.lifespan_marker_store import KbLifespanMarkerStore
from bowhead_core.kb.template_seed import TEMPLATE_SEEDS, seed_templates_for_ws

WS = "ws_seed"


@pytest.mark.anyio
async def test_seed_idempotent_and_kind_bucketed(tmp_path) -> None:
    dts = KbDocTypeStore(tmp_path / "dt.db")
    await dts.initialize()
    markers = KbLifespanMarkerStore(db_path=tmp_path / "dt.db")
    await markers.initialize()

    r1 = await seed_templates_for_ws(WS, dts, markers)
    assert r1.inserted == len(TEMPLATE_SEEDS) == 5
    # Rerun: marker short-circuit.
    r2 = await seed_templates_for_ws(WS, dts, markers)
    assert r2.skipped_via_marker is True and r2.inserted == 0

    items = await dts.list_for_ws(WS)
    seeds = [d for d in items if d.created_by == SYSTEM_TEMPLATE_SEED_USER]
    assert len(seeds) == 5
    names = {d.name for d in seeds}
    assert names == {"合同审阅", "银行流水核对", "发票台账", "竞品报道分析", "用户访谈提炼"}
    for d in seeds:
        # kind=seed (separate from catalog system_catalog_seed), shared ecosystem gate.
        assert derive_doc_type_kind(d) == "seed"
        assert is_column_group_template(d)  # Excluded from bind/classification/kb_list_tables.
        assert d.auto_extract_enabled is False  # F11: upload behavior unchanged in all spaces.
        # question column degraded (string + question in hint); all fields pass core validation.
        for f in d.fields:
            assert f.type in ("string", "number", "currency", "date")


@pytest.mark.anyio
async def test_seed_unique_collision_tolerated(tmp_path) -> None:
    dts = KbDocTypeStore(tmp_path / "dt.db")
    await dts.initialize()
    markers = KbLifespanMarkerStore(db_path=tmp_path / "dt.db")
    await markers.initialize()
    # User took "合同审阅" name → tolerate UNIQUE collision as skip (marker-drift/race fallback).
    await dts.create(workspace_id=WS, name="合同审阅", created_by="u_user")
    r = await seed_templates_for_ws(WS, dts, markers)
    assert r.inserted == 4 and r.skipped_already_present == 1
