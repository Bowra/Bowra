"""KB 90-day grace 硬删 sweeper（P1-N3 / 矩阵 FR-7 接线）。

**这是登记在案的合规债的兑现点**，不是新设计。`docs/sprints/kb-matrix/tech-design-m2.md:11`
写明：`files_store.list_soft_deleted_before` / `hard_delete`、`store.hard_delete_file`、
事件 `kb.hard_deleted_after_grace`、两个 `purge_provenance_for_file` **全是无调用点的
构件，接线归"未来的 grace cron 工作项"**；在此之前「硬删时 purge 摘录」全链 inert，
删掉的文件其原文摘录随软删无限驻留。本模块就是那个工作项。

语义（务必照抄设计，别按直觉写）：
- **软删 = 隐藏**（chunks `is_active=False` / 行 `soft_deleted_at` / 级联 records+rows）；
  宽限期内可恢复。本 sweeper 只处理**已过宽限期**的。
- **硬删 ≠ 删掉所有沾边的行**。矩阵 M4 gate④/FR-7 定死：**graduated 冻结快照行是时点
  冻结、不随源文件删除改写** —— 硬删时的正确动作是 **purge 摘录（source_excerpt → None），
  行与 normalized 值保留**。`purge_provenance_for_file` 正是为此而造。
  ⚠ 按直觉「DELETE FROM rows WHERE file_id=?」会摧毁故意与源文件解耦的冻结快照。
- records 侧设计未要求 purge（其 source_excerpt 随 record 软删隐藏）→ 不越权扩范围。

默认 dry-run（`BOWHEAD_KB_GRACE_SWEEPER=1` 才真删）：这是**不可逆**操作，且当前 prod
KB 几乎无用量，宁可先观测日志再放开。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from bowhead_core.kb.files_store import KbFilesStore
    from bowhead_core.kb.store import KbStore

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 86_400  # Daily is enough: 90-day grace needs no finer precision.
_DEFAULT_GRACE_DAYS = 90
_FLAG_ENV = "BOWHEAD_KB_GRACE_SWEEPER"


async def sweep_kb_grace_once(
    *,
    files_store: KbFilesStore,
    kb_store: KbStore,
    rows_store: Any | None = None,
    matrix_store: Any | None = None,
    grace_days: int = _DEFAULT_GRACE_DAYS,
    enabled: bool | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """扫一轮：把超过宽限期的软删文件真正抹掉。返回计数（便于 UT 断言 + 观测）。

    `enabled=False`（默认）= dry-run：只统计 + 记日志，不动任何数据。
    """
    grace_days = max(1, grace_days)
    if enabled is None:
        enabled = os.environ.get(_FLAG_ENV, "0").strip() == "1"
    now = now or datetime.now(tz=UTC)
    cutoff = now - timedelta(days=grace_days)

    stale = await files_store.list_soft_deleted_before(cutoff)
    counts = {"candidates": len(stale), "purged": 0, "chunks_deleted": 0, "rows_purged": 0, "cells_purged": 0, "failed": 0}
    if not stale:
        return counts
    if not enabled:
        log.info(
            "kb_grace_sweeper.dry_run candidates=%d cutoff=%s — 设 %s=1 才真删",
            len(stale),
            cutoff.isoformat(timespec="seconds"),
            _FLAG_ENV,
        )
        return counts

    for file in stale:
        try:
            # ① chunks (irreversible hard deletion in chromadb)
            n_chunks = await kb_store.hard_delete_file(file)
            # ② FR-7: purge excerpts (**retain rows/normalized**; see module docstring)
            n_rows = 0
            if rows_store is not None:
                n_rows = await rows_store.purge_provenance_for_file(file.file_id)
            n_cells = 0
            if matrix_store is not None:
                n_cells = await matrix_store.purge_provenance_for_file(file.file_id)
            # ③ File row last: earlier failures leave soft_deleted_at for rescanning;
            #    deleting the row first orphans the file and prevents excerpt purging forever.
            await files_store.hard_delete(file.file_id)

            counts["purged"] += 1
            counts["chunks_deleted"] += n_chunks
            counts["rows_purged"] += n_rows
            counts["cells_purged"] += n_cells
            emit_audit(
                "kb.hard_deleted_after_grace",
                {
                    "file_id": file.file_id,
                    "grace_days": grace_days,
                    "chunks_deleted": n_chunks,
                    "rows_purged": n_rows,
                    "cells_purged": n_cells,
                },
            )
        except Exception as exc:  # noqa: BLE001 — One file failure must not stop the sweep.
            counts["failed"] += 1
            log.warning("kb_grace_sweeper.file_failed file=%s err=%s", file.file_id, exc)

    log.info(
        "kb_grace_sweeper.tick purged=%d chunks=%d rows_purged=%d cells_purged=%d failed=%d",
        counts["purged"],
        counts["chunks_deleted"],
        counts["rows_purged"],
        counts["cells_purged"],
        counts["failed"],
    )
    return counts


async def run_kb_grace_sweeper(
    *,
    files_store: KbFilesStore,
    kb_store: KbStore,
    rows_store: Any | None = None,
    matrix_store: Any | None = None,
    interval_s: int = _DEFAULT_INTERVAL_S,
    grace_days: int = _DEFAULT_GRACE_DAYS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """周期循环（形状照抄 notification_sweeper.run_notification_sweeper）。"""
    log.info(
        "kb_grace_sweeper.started interval_s=%s grace_days=%s enabled=%s",
        interval_s,
        grace_days,
        os.environ.get(_FLAG_ENV, "0").strip() == "1",
    )
    try:
        while True:
            try:
                await sweep_kb_grace_once(
                    files_store=files_store,
                    kb_store=kb_store,
                    rows_store=rows_store,
                    matrix_store=matrix_store,
                    grace_days=grace_days,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("kb_grace_sweeper.tick_failed err=%s", exc)
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        log.info("kb_grace_sweeper.cancelled")
        raise
    finally:
        log.info("kb_grace_sweeper.stopped")


def env_interval_s() -> int:
    raw = os.environ.get("BOWHEAD_KB_GRACE_SWEEPER_INTERVAL_S")
    if not raw:
        return _DEFAULT_INTERVAL_S
    try:
        return max(300, int(raw))
    except ValueError:
        return _DEFAULT_INTERVAL_S


def env_grace_days() -> int:
    raw = os.environ.get("BOWHEAD_KB_GRACE_DAYS")
    if not raw:
        return _DEFAULT_GRACE_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_GRACE_DAYS
