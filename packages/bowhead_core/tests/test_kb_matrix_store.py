"""KbMatrixStore UT — kb-matrix M2 块1（三表 + seq 事务 + 列生命周期 + cell 写路径）。

钉死 tech design 的实现不变量：
- seq 单调 + 结构性变更 bump structure_seq（§4.4）
- enqueue 幂等（queued/running 同键跳过）+ queued_from 四值（§5.6/FR-19）
- 取消回退：empty=删行、done=恢复原态含原 variants（§5.5）
- AI 写新值必清 verified/edited；failed 落态未写新值不动标记（review P1 精化）
- 转正 re-coercion 取值路径 normalized.value → raw，皆无跳过；edited 保留（§4.5）
- 缓存命中限同 team + done + edited=0（§5.6）
- run CAS：freeze 不覆盖 cancelling；resume 原子认领（§5.5）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.kb.matrix_store import (
    KbMatrixStore,
    cell_is_stale,
    is_raw_string_sentinel,
    new_run_id,
    normalize_column,
    normalize_matrix_value,
    validate_options,
)

if TYPE_CHECKING:
    from pathlib import Path

WS = "ws_mx"
TEAM = "team_mx"


@pytest.fixture
async def store(tmp_path: Path) -> KbMatrixStore:
    s = KbMatrixStore(tmp_path / "kb_matrix.db")
    await s.initialize()
    return s


async def _mk_matrix(store: KbMatrixStore, *, n_files: int = 2, n_cols: int = 2):
    files = [{"file_id": f"kbf_{i}", "char_count": 1000 * (i + 1)} for i in range(n_files)]
    columns = [{"name": f"列{i}", "type": "question", "question": f"问题{i}?"} for i in range(n_cols)]
    return await store.create_matrix(
        name="测试矩阵",
        workspace_id=WS,
        team_id=TEAM,
        created_by="u1",
        files=files,
        columns=columns,
    )


def _variants(value: str = "5000000", *, excerpt: str | None = "原文摘录") -> list[dict]:
    return [
        {
            "raw": None,
            "normalized": {"value": value},
            "effective_at": None,
            "is_primary": True,
            "extracted_by": "tier1",
            "confidence": 0.9,
            "source_excerpt": excerpt,
            "chunk_id": "chk_1",
            "page": 1,
        }
    ]


# ─── matrices / seq ─────────────────────────────────────────


async def test_create_and_get_matrix(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    assert m.matrix_id.startswith("mx_")
    assert m.last_seq == 0
    assert m.structure_seq == 0
    assert len(m.files) == 2
    assert len(m.columns) == 2
    assert m.columns[0]["column_id"].startswith("mxcol_")
    assert m.columns[0]["prompt_version"] == 1
    # Workspace-predicate defense: wrong ws cannot read.
    assert await store.get_matrix(m.matrix_id, workspace_id="ws_other") is None
    assert await store.get_matrix(m.matrix_id, workspace_id=WS) is not None


async def test_enqueue_assigns_monotonic_seq(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    run_id = new_run_id()
    n = await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col), ("kbf_1", col)],
        run_id=run_id,
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={"kbf_0": 1, "kbf_1": 1},
    )
    assert n == 2
    cells = sorted(await store.list_cells(m.matrix_id), key=lambda c: c.seq)
    assert [c.seq for c in cells] == [1, 2]
    assert all(c.status == "queued" and c.queued_from == "empty" for c in cells)
    assert cells[0].idempotency_key == f"kbf_0:{col}:1"
    updated = await store.get_matrix(m.matrix_id)
    assert updated is not None
    assert updated.last_seq == 2
    assert updated.structure_seq == 0  # Cell writes are not structural changes.


async def test_enqueue_idempotent_skips_inflight(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    kwargs: dict = {
        "run_id": new_run_id(),
        "workspace_id": WS,
        "team_id": TEAM,
        "column_versions": {col: 1},
        "file_versions": {"kbf_0": 1},
    }
    assert await store.enqueue_cells(m.matrix_id, [("kbf_0", col)], **kwargs) == 1
    # Same key reenqueue → skip (queued in-flight).
    assert await store.enqueue_cells(m.matrix_id, [("kbf_0", col)], **kwargs) == 0
    await store.mark_cells_running(m.matrix_id, [("kbf_0", col)])
    assert await store.enqueue_cells(m.matrix_id, [("kbf_0", col)], **kwargs) == 0


async def test_rename_bumps_structure_seq(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    assert await store.rename_matrix(m.matrix_id, "新名字", workspace_id=WS)
    updated = await store.get_matrix(m.matrix_id)
    assert updated is not None
    assert updated.name == "新名字"
    assert updated.structure_seq == updated.last_seq > 0
    # Wrong ws cannot mutate.
    assert not await store.rename_matrix(m.matrix_id, "x", workspace_id="ws_other")


async def test_soft_delete_cascades_cells(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=new_run_id(),
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    assert await store.soft_delete_matrix(m.matrix_id, workspace_id=WS)
    assert await store.get_matrix(m.matrix_id) is None
    assert await store.list_cells(m.matrix_id) == []


# ─── Cancellation rollback (§5.5) ──────────────────────────────


async def test_revert_empty_origin_deletes_row(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    run_id = new_run_id()
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=run_id,
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    assert await store.revert_queued_cells_for_run(run_id) == 1
    assert await store.get_cell(m.matrix_id, "kbf_0", col) is None  # empty = no row.


async def test_revert_done_origin_restores_variants(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    run1 = new_run_id()
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=run1,
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    await store.finalize_cell(
        m.matrix_id,
        "kbf_0",
        col,
        status="done",
        variants=_variants("原值"),
        prompt_version=1,
        file_version=1,
    )
    # Force-rerun done cell → queued_from=done.
    run2 = new_run_id()
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=run2,
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None
    assert cell.status == "queued"
    assert cell.queued_from == "done"
    # Cancel → restore original value unchanged.
    assert await store.revert_queued_cells_for_run(run2) == 1
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None
    assert cell.status == "done"
    assert cell.queued_from is None
    assert cell.run_id is None
    assert cell.variants[0]["normalized"] == {"value": "原值"}


# ─── Final-state invariants (review P1) ─────────────────────────


async def test_ai_write_clears_verified_and_edited(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=new_run_id(),
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="done", variants=_variants(), prompt_version=1, file_version=1)
    await store.set_cell_verified(m.matrix_id, "kbf_0", col, workspace_id=WS)
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None and cell.verified
    # Force-rerun verified cell → AI new value; pin phantom-verification regression.
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="done", variants=_variants("新AI值"), prompt_version=1, file_version=1)
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None
    assert not cell.verified
    assert not cell.edited_by_user


async def test_failed_finalize_keeps_marks_and_variants(store: KbMatrixStore) -> None:
    """failed 落态未写新值 → 旧值仍是人工确认过的，edited/verified/variants 保留。"""
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.edit_cell_value(
        m.matrix_id,
        "kbf_0",
        col,
        value="人工值",
        column={"type": "question", "prompt_version": 1},
        workspace_id=WS,
        team_id=TEAM,
    )
    await store.set_cell_verified(m.matrix_id, "kbf_0", col, workspace_id=WS)
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="failed", fail_reason="llm_invoke_failed")
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None
    assert cell.status == "failed"
    assert cell.edited_by_user  # No new value leaves marker unchanged.
    assert cell.verified
    assert cell.variants[0]["normalized"] == {"value": "人工值"}


# ─── Column lifecycle (§4.5) ────────────────────────────────────


async def test_update_column_name_keeps_prompt_version(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col_id = m.columns[0]["column_id"]
    updated = await store.update_column(m.matrix_id, col_id, workspace_id=WS, name="新列名")
    assert updated is not None
    assert updated["name"] == "新列名"
    assert updated["prompt_version"] == 1


async def test_update_column_question_bumps_prompt_version(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col_id = m.columns[0]["column_id"]
    updated = await store.update_column(m.matrix_id, col_id, workspace_id=WS, question="改后的问题?")
    assert updated is not None
    assert updated["prompt_version"] == 2
    mat = await store.get_matrix(m.matrix_id)
    assert mat is not None
    assert mat.structure_seq == mat.last_seq


async def test_promote_column_recoerces_cells(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    run_id = new_run_id()
    targets = [("kbf_0", col), ("kbf_1", col)]
    await store.enqueue_cells(
        m.matrix_id,
        targets,
        run_id=run_id,
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    # Cell1 purely numeric → currency; cell2 residual chars → raw_string sentinel → needs_review.
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="done", variants=_variants("5000000"), prompt_version=1, file_version=1)
    await store.finalize_cell(m.matrix_id, "kbf_1", col, status="done", variants=_variants("约100元"), prompt_version=1, file_version=1)
    promoted = await store.promote_column(m.matrix_id, col, "currency", workspace_id=WS)
    assert promoted is not None
    assert promoted["type"] == "currency"
    assert promoted["prompt_version"] == 1  # Promotion does not increment (FR-4 exception).
    c0 = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert c0 is not None
    assert c0.status == "done"
    assert c0.variants[0]["normalized"] == {"amount": "5000000", "currency": None}
    c1 = await store.get_cell(m.matrix_id, "kbf_1", col)
    assert c1 is not None
    assert c1.status == "needs_review"
    assert is_raw_string_sentinel(c1.variants[0]["normalized"])


async def test_promote_column_edited_cell_keeps_edited(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.edit_cell_value(
        m.matrix_id,
        "kbf_0",
        col,
        value="123",
        column={"type": "question", "prompt_version": 1},
        workspace_id=WS,
        team_id=TEAM,
    )
    await store.promote_column(m.matrix_id, col, "number", workspace_id=WS)
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None
    assert cell.edited_by_user  # Type reinterpretation, no AI overwrite.
    assert cell.variants[0]["normalized"] == {"amount": "123", "currency": None}


async def test_promote_column_skips_cell_without_value(store: KbMatrixStore) -> None:
    """取值路径（review P2）：normalized.value 与 raw 皆无 → 跳过保持现状。"""
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    run_id = new_run_id()
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=run_id,
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    null_variant = [
        {
            "raw": None,
            "normalized": None,
            "effective_at": None,
            "is_primary": True,
            "extracted_by": "tier1",
            "confidence": 0.0,
            "source_excerpt": None,
            "chunk_id": None,
            "page": None,
        }
    ]
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="done", variants=null_variant, prompt_version=1, file_version=1)
    await store.promote_column(m.matrix_id, col, "number", workspace_id=WS)
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None
    assert cell.status == "done"
    assert cell.variants[0]["normalized"] is None  # Untouched.


async def test_promote_column_uses_raw_after_keep_as_text(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    run_id = new_run_id()
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=run_id,
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    sentinel = [dict(_variants()[0], normalized={"raw_string": "500"})]
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="needs_review", variants=sentinel, prompt_version=1, file_version=1)
    kept = await store.keep_cell_as_text(m.matrix_id, "kbf_0", col, workspace_id=WS)
    assert kept is not None
    assert kept.status == "done"
    assert kept.variants[0]["normalized"] is None
    assert kept.variants[0]["raw"] == "500"
    # Promotion reads raw.
    await store.promote_column(m.matrix_id, col, "number", workspace_id=WS)
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None
    assert cell.variants[0]["normalized"] == {"amount": "500", "currency": None}


async def test_promote_non_question_column_rejected(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.promote_column(m.matrix_id, col, "number", workspace_id=WS)
    with pytest.raises(ValueError, match="question"):
        await store.promote_column(m.matrix_id, col, "date", workspace_id=WS)
    with pytest.raises(ValueError, match="promote target"):
        await store.promote_column(m.matrix_id, m.columns[1]["column_id"], "question", workspace_id=WS)


async def test_delete_column_cascades(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=new_run_id(),
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    assert await store.delete_column(m.matrix_id, col, workspace_id=WS)
    mat = await store.get_matrix(m.matrix_id)
    assert mat is not None
    assert len(mat.columns) == 1
    assert mat.structure_seq == mat.last_seq
    assert await store.get_cell(m.matrix_id, "kbf_0", col) is None


async def test_replace_files_cascades_removed(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col), ("kbf_1", col)],
        run_id=new_run_id(),
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    assert await store.replace_files(
        m.matrix_id,
        [{"file_id": "kbf_1", "char_count": 2000}, {"file_id": "kbf_9", "char_count": 5}],
        workspace_id=WS,
    )
    mat = await store.get_matrix(m.matrix_id)
    assert mat is not None
    assert [f["file_id"] for f in mat.files] == ["kbf_1", "kbf_9"]
    assert await store.get_cell(m.matrix_id, "kbf_0", col) is None  # File removal cascades soft deletion.
    assert await store.get_cell(m.matrix_id, "kbf_1", col) is not None


# ─── User writes ───────────────────────────────────────────────


async def test_edit_cell_value_wraps_by_type(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    cell = await store.edit_cell_value(
        m.matrix_id,
        "kbf_0",
        col,
        value="RMB 5,000,000",
        column={"type": "currency", "prompt_version": 1},
        workspace_id=WS,
        team_id=TEAM,
    )
    assert cell is not None
    assert cell.status == "done"
    assert cell.edited_by_user
    assert not cell.verified
    assert cell.variants[0]["normalized"] == {"amount": "5000000", "currency": None}
    assert cell.variants[0]["extracted_by"] == "user_edit"


async def test_set_verified_requires_done(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=new_run_id(),
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    with pytest.raises(ValueError, match="done"):
        await store.set_cell_verified(m.matrix_id, "kbf_0", col, workspace_id=WS)


# ─── Cache (§5.6) ───────────────────────────────────────────────


async def test_find_cached_cell_scoping(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=new_run_id(),
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    await store.finalize_cell(
        m.matrix_id,
        "kbf_0",
        col,
        status="done",
        variants=_variants(),
        prompt_version=1,
        file_version=1,
        cache_key="ck_abc",
    )
    assert await store.find_cached_cell(TEAM, "ck_abc") is not None
    assert await store.find_cached_cell("team_other", "ck_abc") is None  # Cross-team miss.
    # User-edited values excluded from cache pool.
    await store.edit_cell_value(
        m.matrix_id, "kbf_0", col, value="人工", column={"type": "string", "prompt_version": 1}, workspace_id=WS, team_id=TEAM
    )
    assert await store.find_cached_cell(TEAM, "ck_abc") is None


# ─── runs ───────────────────────────────────────────────────


async def test_run_lifecycle_and_cas(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    run = await store.create_run(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by="u1",
        cells_total=4,
        skipped_edited=1,
        credits_estimated=10,
    )
    assert run.run_id.startswith("mxrun_")
    assert run.status == "running"
    assert await store.count_active_batch_runs_for_matrix(m.matrix_id) == 1
    assert await store.count_active_runs_for_workspace(WS) == 1
    # freeze never overwrites cancelling (review P1 CAS).
    assert await store.set_run_status(run.run_id, "cancelling", expected_from=("running", "paused"))
    assert not await store.set_run_status(run.run_id, "paused", expected_from=("running",))
    got = await store.get_run(run.run_id)
    assert got is not None
    assert got.status == "cancelling"
    assert await store.set_run_status(run.run_id, "cancelled")
    got = await store.get_run(run.run_id)
    assert got is not None
    assert got.finished_at is not None
    assert await store.count_active_runs_for_workspace(WS) == 0


async def test_claim_resumable_runs_bumps_heartbeat(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    run = await store.create_run(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by="u1",
        cells_total=1,
        skipped_edited=0,
        credits_estimated=1,
    )
    # Artificially age heartbeat.
    import aiosqlite

    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE kb_matrix_runs SET heartbeat_at = '2020-01-01T00:00:00+00:00' WHERE run_id = ?",
            (run.run_id,),
        )
        await db.commit()
    assert len(await store.list_zombie_candidate_runs(900)) == 1
    claimed = await store.claim_resumable_runs()
    assert [r.run_id for r in claimed] == [run.run_id]
    # Atomic claim prevents scanner false kills (resume×zombie race, acceptance 3).
    assert await store.list_zombie_candidate_runs(900) == []


async def test_fail_pending_cells_for_run(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    run = await store.create_run(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by="u1",
        cells_total=2,
        skipped_edited=0,
        credits_estimated=1,
    )
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col), ("kbf_1", col)],
        run_id=run.run_id,
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    await store.mark_cells_running(m.matrix_id, [("kbf_0", col)])
    assert await store.fail_pending_cells_for_run(run.run_id, fail_reason="insufficient_credits") == 2
    cells = await store.list_cells(m.matrix_id)
    assert all(c.status == "failed" and c.fail_reason == "insufficient_credits" for c in cells)


async def test_reset_running_cells_to_queued(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    run_id = new_run_id()
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col), ("kbf_1", col)],
        run_id=run_id,
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    await store.mark_cells_running(m.matrix_id, [("kbf_0", col)])
    # done cells never roll back (cell state is checkpoint).
    await store.finalize_cell(m.matrix_id, "kbf_1", col, status="done", variants=_variants(), prompt_version=1, file_version=1)
    assert await store.reset_running_cells_to_queued(run_id) == 1
    c0 = await store.get_cell(m.matrix_id, "kbf_0", col)
    c1 = await store.get_cell(m.matrix_id, "kbf_1", col)
    assert c0 is not None and c0.status == "queued"
    assert c1 is not None and c1.status == "done"


# ─── Snapshot helpers ──────────────────────────────────────────


async def test_list_cells_since(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col), ("kbf_1", col)],
        run_id=new_run_id(),
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    assert len(await store.list_cells_since(m.matrix_id, 0)) == 2
    assert len(await store.list_cells_since(m.matrix_id, 1)) == 1
    assert await store.list_cells_since(m.matrix_id, 2) == []


async def test_status_summary(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col), ("kbf_1", col)],
        run_id=new_run_id(),
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="done", variants=_variants(), prompt_version=1, file_version=1)
    summary = await store.status_summary_for_matrix(m.matrix_id)
    assert summary == {"done": 1, "failed": 0, "needs_review": 0, "running": 0, "queued": 1}


async def test_purge_provenance_for_file(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id,
        [("kbf_0", col)],
        run_id=new_run_id(),
        workspace_id=WS,
        team_id=TEAM,
        column_versions={col: 1},
        file_versions={},
    )
    await store.finalize_cell(
        m.matrix_id,
        "kbf_0",
        col,
        status="done",
        variants=_variants("值", excerpt="敏感原文摘录"),
        prompt_version=1,
        file_version=1,
    )
    assert await store.purge_provenance_for_file("kbf_0") == 1
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None
    assert cell.variants[0]["source_excerpt"] is None
    assert cell.variants[0]["normalized"] == {"value": "值"}  # Retain value.


def test_normalize_column_rejects_bad_type() -> None:
    # Since M3 boolean is valid (FR-2 r5); test allowlist with actually invalid value.
    with pytest.raises(ValueError, match="column type"):
        normalize_column({"name": "x", "type": "percentage"})


# ═══ M3 additions (tech-design-m3) ══════════════════════════════
# stale predicate / options validation / three-type normalization / two undo ops / poisoned cache / multi-run counts and snapshot window.


def test_cell_is_stale_truth_table() -> None:
    """决策 2 单一权威谓词：prompt/file 两轴 × 状态集。"""
    # done + older column version → stale.
    assert cell_is_stale("done", 1, 2, 1, 1)
    # needs_review + older file version → stale.
    assert cell_is_stale("needs_review", 1, 1, 1, 2)
    # Both axes current → not stale.
    assert not cell_is_stale("done", 2, 2, 3, 3)
    # failed/queued/running not stale (rerun existing-results semantics).
    assert not cell_is_stale("failed", 1, 2, 1, 2)
    assert not cell_is_stale("queued", 1, 2, 1, 2)
    assert not cell_is_stale("running", 1, 2, 1, 2)
    # Unknown file_version_now (None) → prompt axis only.
    assert not cell_is_stale("done", 1, 1, 1, None)
    assert cell_is_stale("done", 1, 2, 1, None)


def test_validate_options_matrix() -> None:
    """FR-2 r5 options 约束（四路写入口共用）。"""
    assert validate_options("select", [" 正面 ", "负面"]) == ["正面", "负面"]
    assert validate_options("string", None) is None
    with pytest.raises(ValueError, match="options_required"):
        validate_options("select", None)
    with pytest.raises(ValueError, match="options_required"):
        validate_options("multi_select", [])
    with pytest.raises(ValueError, match="option_duplicate"):
        validate_options("select", ["a", "a"])
    with pytest.raises(ValueError, match="option_empty"):
        validate_options("select", ["a", "  "])
    with pytest.raises(ValueError, match="option_too_long"):
        validate_options("select", ["x" * 65])
    with pytest.raises(ValueError, match="options_too_many"):
        validate_options("select", [f"o{i}" for i in range(101)])
    with pytest.raises(ValueError, match="options_not_applicable"):
        validate_options("boolean", ["是", "否"])
    with pytest.raises(ValueError, match="options_not_applicable"):
        validate_options("currency", ["a"])


def test_normalize_column_cross_verify_question_rejected() -> None:
    """FR-39：question 列禁用交叉校验（value 精确匹配对自由文本全是噪声）。"""
    with pytest.raises(ValueError, match="cross_verify_not_applicable"):
        normalize_column({"name": "x", "type": "question", "question": "q?", "cross_verify": True})
    col = normalize_column({"name": "x", "type": "number", "question": "q?", "cross_verify": True})
    assert col["cross_verify"] is True


def test_normalize_matrix_value_select() -> None:
    col = {"type": "select", "options": ["正面", "中性", "负面"]}
    assert normalize_matrix_value("正面", col) == {"choice": "正面"}
    # Fold width/case/whitespace for matching.
    assert normalize_matrix_value(" 正面 ", col) == {"choice": "正面"}
    assert normalize_matrix_value("褒贬不一", col) == {"raw_string": "褒贬不一"}
    assert normalize_matrix_value(None, col) is None


def test_normalize_matrix_value_multi_select() -> None:
    col = {"type": "multi_select", "options": ["星舰 Pro", "星舰 Air", "星舰 mini"]}
    # JSON list + deduplicate in options-defined order.
    assert normalize_matrix_value(["星舰 mini", "星舰 Pro", "星舰 Pro"], col) == {"choices": ["星舰 Pro", "星舰 mini"]}
    # Ideographic-comma-separated string.
    assert normalize_matrix_value("星舰 Pro、星舰 Air", col) == {"choices": ["星舰 Pro", "星舰 Air"]}
    # JSON string form.
    assert normalize_matrix_value('["星舰 Air"]', col) == {"choices": ["星舰 Air"]}
    # Python repr single-quoted array string (real-stack dogfood: extractor degrades to string, then str(list) creates this shape).
    assert normalize_matrix_value("['星舰 Pro', '星舰 Air']", col) == {"choices": ["星舰 Pro", "星舰 Air"]}
    # Any unmatched option → whole-value sentinel.
    sentinel = normalize_matrix_value("星舰 Pro、星舰 Ultra", col)
    assert is_raw_string_sentinel(sentinel)


def test_normalize_matrix_value_boolean_and_legacy_delegation() -> None:
    col_b = {"type": "boolean"}
    assert normalize_matrix_value("是", col_b) == {"flag": True}
    assert normalize_matrix_value("FALSE", col_b) == {"flag": False}
    assert normalize_matrix_value(True, col_b) == {"flag": True}
    assert is_raw_string_sentinel(normalize_matrix_value("大概吧", col_b))
    # Existing types delegate v1 (currency-residue sentinel semantics unchanged).
    assert normalize_matrix_value("5000", {"type": "currency"}) == {"amount": "5000", "currency": None}
    assert is_raw_string_sentinel(normalize_matrix_value("约100元", {"type": "currency"}))
    assert normalize_matrix_value("文本", {"type": "question"}) == {"value": "文本"}


async def test_update_column_options_and_cv_bump(store: KbMatrixStore) -> None:
    """FR-4 r5：改 options / cross_verify 视同语义变更 → prompt_version 递增；
    PATCH 路径走 validate_options 同源校验（S-P2-7 绕过面封死）。"""
    m = await store.create_matrix(
        name="m",
        workspace_id=WS,
        team_id=TEAM,
        created_by="u1",
        files=[{"file_id": "kbf_0", "char_count": 100}],
        columns=[{"name": "情感", "type": "select", "question": "q?", "options": ["正面", "负面"]}],
    )
    col_id = m.columns[0]["column_id"]
    # Equal sets, order irrelevant (impl-diff review P2-1), no bump; same order/set unchanged.
    same = await store.update_column(m.matrix_id, col_id, workspace_id=WS, options=["正面", "负面"])
    assert same is not None and same["prompt_version"] == 1
    # Reordered same set → no bump (no false stale/rerun dialog), persist new columns_json order.
    reordered = await store.update_column(m.matrix_id, col_id, workspace_id=WS, options=["负面", "正面"])
    assert reordered is not None and reordered["prompt_version"] == 1
    assert reordered["options"] == ["负面", "正面"]
    # Actual set change (addition) → bump.
    updated = await store.update_column(m.matrix_id, col_id, workspace_id=WS, options=["正面", "中性", "负面"])
    assert updated is not None and updated["prompt_version"] == 2
    with pytest.raises(ValueError, match="options_too_many"):
        await store.update_column(m.matrix_id, col_id, workspace_id=WS, options=[f"o{i}" for i in range(101)])
    # cross_verify flip bumps.
    flipped = await store.update_column(m.matrix_id, col_id, workspace_id=WS, cross_verify=True)
    assert flipped is not None and flipped["prompt_version"] == 3 and flipped["cross_verify"] is True


async def test_update_column_cv_on_question_rejected(store: KbMatrixStore) -> None:
    m = await _mk_matrix(store)
    col_id = m.columns[0]["column_id"]
    with pytest.raises(ValueError, match="cross_verify_not_applicable"):
        await store.update_column(m.matrix_id, col_id, workspace_id=WS, cross_verify=True)


async def test_promote_to_select_with_options_clears_cache_key(store: KbMatrixStore) -> None:
    """FR-5 r5：{type, options} 转正 + 毒缓存收口路③（S-P1-2：re-coercion 后
    cache_key 仍指旧 question 型指纹 = 同文本列命中错形状值）。"""
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id, [("kbf_0", col)], run_id=new_run_id(), workspace_id=WS, team_id=TEAM, column_versions={col: 1}, file_versions={}
    )
    await store.finalize_cell(
        m.matrix_id, "kbf_0", col, status="done", variants=_variants("正面"), prompt_version=1, file_version=1, cache_key="ck_old"
    )
    promoted = await store.promote_column(m.matrix_id, col, "select", workspace_id=WS, options=["正面", "负面"])
    assert promoted is not None
    assert promoted["type"] == "select"
    assert promoted["options"] == ["正面", "负面"]
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None
    assert cell.status == "done"
    assert cell.variants[0]["normalized"] == {"choice": "正面"}
    assert cell.cache_key is None  # Cleanup path ③.
    assert not cell.cached
    assert await store.find_cached_cell(TEAM, "ck_old") is None
    # Select promotion without options → validation rejection.
    m2 = await _mk_matrix(store)
    col2 = m2.columns[0]["column_id"]
    with pytest.raises(ValueError, match="options_required"):
        await store.promote_column(m2.matrix_id, col2, "select", workspace_id=WS)


async def test_set_verified_false_and_noop(store: KbMatrixStore) -> None:
    """FR-12 r5：false 不要求 done（failed+verified 真实可达必须可撤，G-P2-3）；
    no-op 返回 changed=False（route 依此不落虚审计）。"""
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id, [("kbf_0", col)], run_id=new_run_id(), workspace_id=WS, team_id=TEAM, column_versions={col: 1}, file_versions={}
    )
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="done", variants=_variants(), prompt_version=1, file_version=1)
    cell, changed = await store.set_cell_verified(m.matrix_id, "kbf_0", col, workspace_id=WS, verified=True)
    assert cell is not None and cell.verified and changed
    # requeue (force includes verified cells) → failure → failed + verified=1 reachable.
    await store.enqueue_cells(
        m.matrix_id, [("kbf_0", col)], run_id=new_run_id(), workspace_id=WS, team_id=TEAM, column_versions={col: 1}, file_versions={}
    )
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="failed", fail_reason="timeout")
    cell = await store.get_cell(m.matrix_id, "kbf_0", col)
    assert cell is not None and cell.status == "failed" and cell.verified
    cell, changed = await store.set_cell_verified(m.matrix_id, "kbf_0", col, workspace_id=WS, verified=False)
    assert cell is not None and not cell.verified and changed
    # No-op: already 0, undo again → changed=False.
    cell, changed = await store.set_cell_verified(m.matrix_id, "kbf_0", col, workspace_id=WS, verified=False)
    assert cell is not None and not changed


async def test_revert_edited_clears_cache_key(store: KbMatrixStore) -> None:
    """FR-9 r5 + 毒缓存收口路②不变量：编辑→撤销→同键查缓存 miss。"""
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    await store.enqueue_cells(
        m.matrix_id, [("kbf_0", col)], run_id=new_run_id(), workspace_id=WS, team_id=TEAM, column_versions={col: 1}, file_versions={}
    )
    await store.finalize_cell(m.matrix_id, "kbf_0", col, status="done", variants=_variants(), prompt_version=1, file_version=1, cache_key="ck_poison")
    await store.edit_cell_value(
        m.matrix_id, "kbf_0", col, value="人工", column={"type": "question", "prompt_version": 1}, workspace_id=WS, team_id=TEAM
    )
    reverted = await store.revert_cell_edited(m.matrix_id, "kbf_0", col, workspace_id=WS)
    assert reverted is not None
    assert not reverted.edited_by_user
    assert reverted.cache_key is None
    assert not reverted.cached
    # Manual value remains current (variants unchanged).
    assert reverted.variants[0]["normalized"] == {"value": "人工"}
    # Invariant: after undo (done+edited=0), still never cached; key cleared.
    assert await store.find_cached_cell(TEAM, "ck_poison") is None
    # Undo unedited cell → 422 semantics.
    with pytest.raises(ValueError, match="not_edited"):
        await store.revert_cell_edited(m.matrix_id, "kbf_0", col, workspace_id=WS)
    # Cross-space → None (S1 predicate).
    assert await store.revert_cell_edited(m.matrix_id, "kbf_0", col, workspace_id="ws_other") is None


async def test_batch_run_counts_exempt_single_cell(store: KbMatrixStore) -> None:
    """决策 7：两级帽计数过滤 single_cell；list_runs_for_snapshot 排除 single_cell
    + 终态窗口 + 全局排序（run 兼容字段 = runs[0]）。"""
    m = await _mk_matrix(store)

    async def _run(kind: str, status: str = "running") -> str:
        r = await store.create_run(
            matrix_id=m.matrix_id,
            workspace_id=WS,
            team_id=TEAM,
            scope={"kind": kind, **({"file_id": "kbf_0", "column_id": "c"} if kind == "single_cell" else {})},
            initiated_by="u1",
            cells_total=1,
            skipped_edited=0,
            credits_estimated=1,
        )
        if status != "running":
            await store.set_run_status(r.run_id, status)
        return r.run_id

    await _run("all_pending")
    await _run("single_cell")
    await _run("single_cell")
    assert await store.count_active_batch_runs_for_matrix(m.matrix_id) == 1
    assert await store.count_active_runs_for_workspace(WS) == 1
    # Terminal window: 7 terminal batch runs → latest 5 + 1 active in snapshot; exclude single_cell.
    for _ in range(7):
        await _run("column", status="succeeded")
    snap_runs = await store.list_runs_for_snapshot(m.matrix_id)
    assert len(snap_runs) == 6  # 1 active + 5 terminal
    assert all(str(r.scope.get("kind")) != "single_cell" for r in snap_runs)
    # Global started_at DESC (ties use run_id DESC); newest first.
    assert snap_runs == sorted(snap_runs, key=lambda r: (r.started_at, r.run_id), reverse=True)


async def test_create_run_and_enqueue_zero_targets_rolls_back(store: KbMatrixStore) -> None:
    """决策 4：实际入队 0 → 整单回滚不留空 run；cells_total=实际入队数。"""
    m = await _mk_matrix(store)
    col = m.columns[0]["column_id"]
    # Occupy this cell first (in-flight).
    first = await store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "column", "column_id": col},
        initiated_by="u1",
        targets=[("kbf_0", col)],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={col: 1},
        file_versions={},
    )
    assert first is not None and first.cells_total == 1
    # Same-key enqueue → idempotent skip →0 →None, no residual run row.
    second = await store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "column", "column_id": col},
        initiated_by="u1",
        targets=[("kbf_0", col)],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={col: 1},
        file_versions={},
    )
    assert second is None
    assert await store.count_active_batch_runs_for_matrix(m.matrix_id) == 1
