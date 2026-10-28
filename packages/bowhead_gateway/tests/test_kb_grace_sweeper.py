"""KB 90-day grace 硬删 sweeper 测试（P1-N3 / FR-7 接线）。

关键断言不是"跑通了"，而是**语义对**：
- graduated 冻结快照行必须 purge 摘录而非删行（按直觉写会摧毁它们）
- 默认 dry-run（不可逆操作不该默认开）
- 未过宽限期的文件不能被碰
- 单文件失败不能停掉整轮
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_gateway.kb_grace_sweeper import sweep_kb_grace_once


def _file(fid: str = "kbf_1"):
    return SimpleNamespace(file_id=fid, filename="a.pdf")


def _stores(stale: list | None = None):
    files_store = MagicMock()
    files_store.list_soft_deleted_before = AsyncMock(return_value=stale if stale is not None else [])
    files_store.hard_delete = AsyncMock()
    kb_store = MagicMock()
    kb_store.hard_delete_file = AsyncMock(return_value=7)
    rows_store = MagicMock()
    rows_store.purge_provenance_for_file = AsyncMock(return_value=3)
    matrix_store = MagicMock()
    matrix_store.purge_provenance_for_file = AsyncMock(return_value=2)
    return files_store, kb_store, rows_store, matrix_store


@pytest.mark.asyncio
async def test_explicit_disabled_touches_nothing() -> None:
    """enabled=False 只统计不动数据。"""
    files_store, kb_store, rows_store, matrix_store = _stores([_file()])

    counts = await sweep_kb_grace_once(
        files_store=files_store,
        kb_store=kb_store,
        rows_store=rows_store,
        matrix_store=matrix_store,
        enabled=False,
    )

    assert counts["candidates"] == 1
    assert counts["purged"] == 0
    kb_store.hard_delete_file.assert_not_awaited()
    files_store.hard_delete.assert_not_awaited()
    rows_store.purge_provenance_for_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_is_dry_run_when_env_unset(monkeypatch) -> None:
    """⚠ 回归闸：**硬删不可逆 → 默认必须 dry-run**。

    走真默认路径(`enabled=None` → 读 env)，不是传 `enabled=False` 绕过它 ——
    后者测不到默认值被翻成 True 的回归(实验验证过：那样写改默认它仍绿)。
    """
    monkeypatch.delenv("BOWHEAD_KB_GRACE_SWEEPER", raising=False)
    files_store, kb_store, rows_store, matrix_store = _stores([_file()])

    counts = await sweep_kb_grace_once(
        files_store=files_store,
        kb_store=kb_store,
        rows_store=rows_store,
        matrix_store=matrix_store,
    )  # ← Omit enabled to use the default.

    assert counts["purged"] == 0, "默认竟然真删了 —— 不可逆操作默认必须 dry-run"
    kb_store.hard_delete_file.assert_not_awaited()
    files_store.hard_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_env_flag_on_enables_real_delete(monkeypatch) -> None:
    """BOWHEAD_KB_GRACE_SWEEPER=1 → 真删（默认路径的另一半）。"""
    monkeypatch.setenv("BOWHEAD_KB_GRACE_SWEEPER", "1")
    files_store, kb_store, rows_store, matrix_store = _stores([_file()])

    counts = await sweep_kb_grace_once(
        files_store=files_store,
        kb_store=kb_store,
        rows_store=rows_store,
        matrix_store=matrix_store,
    )

    assert counts["purged"] == 1
    files_store.hard_delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_enabled_purges_chunks_excerpts_then_row() -> None:
    """enabled=True 走全套：chunks 真删 + 摘录 purge + 行删 + 埋点。"""
    files_store, kb_store, rows_store, matrix_store = _stores([_file("kbf_x")])

    counts = await sweep_kb_grace_once(
        files_store=files_store,
        kb_store=kb_store,
        rows_store=rows_store,
        matrix_store=matrix_store,
        enabled=True,
    )

    assert counts == {
        "candidates": 1,
        "purged": 1,
        "chunks_deleted": 7,
        "rows_purged": 3,
        "cells_purged": 2,
        "failed": 0,
    }
    kb_store.hard_delete_file.assert_awaited_once()
    rows_store.purge_provenance_for_file.assert_awaited_once_with("kbf_x")
    matrix_store.purge_provenance_for_file.assert_awaited_once_with("kbf_x")
    files_store.hard_delete.assert_awaited_once_with("kbf_x")


@pytest.mark.asyncio
async def test_frozen_snapshot_rows_are_purged_not_deleted() -> None:
    """⚠ 回归闸（M4 gate④ / FR-7）：graduated 冻结快照行是**时点冻结**，硬删时
    只 purge 摘录、**保留行**。按直觉写 `DELETE FROM rows WHERE file_id=?` 会摧毁
    故意与源文件解耦的冻结快照 —— 本测试锁死 sweeper 只调 purge、绝不调任何删行 API。
    """
    files_store, kb_store, rows_store, matrix_store = _stores([_file()])
    # Future sweeper row-deletion calls would touch these mocks and fail the assertions.
    rows_store.hard_delete_for_file = AsyncMock()
    rows_store.soft_delete_for_file = AsyncMock()
    matrix_store.hard_delete_for_file = AsyncMock()

    await sweep_kb_grace_once(
        files_store=files_store,
        kb_store=kb_store,
        rows_store=rows_store,
        matrix_store=matrix_store,
        enabled=True,
    )

    rows_store.purge_provenance_for_file.assert_awaited_once()  # Required call.
    rows_store.hard_delete_for_file.assert_not_awaited()  # Forbidden call.
    rows_store.soft_delete_for_file.assert_not_awaited()
    matrix_store.hard_delete_for_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_cutoff_is_grace_days_ago() -> None:
    """宽限期算对：cutoff = now - grace_days（早于它的才是候选）。"""
    files_store, kb_store, rows_store, matrix_store = _stores([])
    now = datetime(2026, 7, 17, tzinfo=UTC)

    await sweep_kb_grace_once(
        files_store=files_store,
        kb_store=kb_store,
        rows_store=rows_store,
        matrix_store=matrix_store,
        grace_days=90,
        enabled=True,
        now=now,
    )

    (cutoff,), _ = files_store.list_soft_deleted_before.call_args
    assert cutoff == now - timedelta(days=90)


@pytest.mark.asyncio
async def test_one_file_failure_does_not_stop_the_batch() -> None:
    """单文件炸了要继续扫剩下的（否则一个坏文件永久卡死整个合规清理）。"""
    files_store, kb_store, rows_store, matrix_store = _stores([_file("bad"), _file("good")])
    kb_store.hard_delete_file = AsyncMock(side_effect=[RuntimeError("chroma boom"), 5])

    counts = await sweep_kb_grace_once(
        files_store=files_store,
        kb_store=kb_store,
        rows_store=rows_store,
        matrix_store=matrix_store,
        enabled=True,
    )

    assert counts["failed"] == 1
    assert counts["purged"] == 1  # good still processed.
    files_store.hard_delete.assert_awaited_once_with("good")  # Must not delete bad's row.


@pytest.mark.asyncio
async def test_row_deleted_last_so_failure_is_retryable() -> None:
    """行本体必须最后删：前面炸了还能靠 soft_deleted_at 重扫；先删行会让文件
    变无主孤儿、摘录永远 purge 不掉。"""
    files_store, kb_store, rows_store, matrix_store = _stores([_file("kbf_z")])
    rows_store.purge_provenance_for_file = AsyncMock(side_effect=RuntimeError("purge boom"))

    counts = await sweep_kb_grace_once(
        files_store=files_store,
        kb_store=kb_store,
        rows_store=rows_store,
        matrix_store=matrix_store,
        enabled=True,
    )

    assert counts["failed"] == 1
    files_store.hard_delete.assert_not_awaited()  # Row remains → retry next round.
