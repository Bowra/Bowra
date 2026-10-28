#!/usr/bin/env python
"""存量正文的图锚**位置归一**作业（默认只读 dry-run）。

为什么是业务作业而不是 Alembic revision：本项零 schema 变更，且
`docs/design/数据库迁移.md` 明写「大数据回填采用有进度、可重入的业务作业，不隐藏在 revision 中」。

为什么放在**包内** `bowhead_core/scripts/`（而不是仓根 `scripts/`）：仓根 `scripts/` 不进镜像
（`deploy/Dockerfile.app` 只 COPY `packages/`，测试机 `/app/scripts` 实测不存在），而生产库不可能
拷到有源码的机器上跑。包内模块随 `bowhead_core` 进镜像，目标机直接 `-m` 调用。先例：
`bowhead_core/scripts/space_admin_preflight.py`。

做什么：
- 扫全部 narrative/deck/callout 段里带 `[figure:` 的正文，按线上**同一个** normalizer 归一；
  口径必须与写入口完全一致：`apply()` 走 `patch_section`，那条路会再归一一次，
  若 dry-run 用的策略跟它不同，操作员批准的 diff 就不是真正落库的内容（review 四轮 #1）；
- 任何会**删正文**的段（无效锚、重复锚连同其「图N：」图注）一律逐条打印前后全文，
  不受 `--sample` 限制——只给聚合数等于让人盲签；
- 默认只打印影响面（不写库）；`--apply` 才写，写入走 `CanvasStore.patch_section`
  （`by_turn_id="job:figure_anchor_normalize"`，留 revision 可审计）；
- 幂等、可重入：normalizer 是不动点，重复跑第二遍必然零改动；
- 折叠（superseded）段也洗——历史报告区同样会渲染。

用法：
    # 目标机（镜像内）
    docker exec bowhead-app /app/.venv/bin/python -m bowhead_core.scripts.figure_anchor_normalize \\
        --db /app/.data/sqlite/bowhead.db
    # 确认影响面后再写（先备份 data/sqlite/，含 -wal/-shm）
    ... --db /app/.data/sqlite/bowhead.db --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from bowhead_core.canvas_store import CanvasRevConflictError, CanvasStore
from bowhead_core.figure_anchor_layout import (
    FIGURE_KINDS,
    MARKDOWN_PAYLOAD_KINDS,
    figure_prose_title,
    normalize_figure_anchors,
)

JOB_TURN_ID = "job:figure_anchor_normalize"


def _out(msg: str = "") -> None:
    """输出照 `space_admin_preflight` 的先例走 stdout，不用 print（仓里 T20 规则是开的）。"""
    sys.stdout.write(f"{msg}\n")


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _figures_of_canvas(con: sqlite3.Connection, canvas_id: str) -> dict[str, str]:
    kinds = tuple(sorted(FIGURE_KINDS))
    placeholders = ",".join("?" * len(kinds))
    rows = con.execute(
        f"SELECT id, kind, payload FROM canvas_sections WHERE canvas_id=? AND kind IN ({placeholders})",  # noqa: S608 — placeholders generated from the constant set's length
        (canvas_id, *kinds),
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        # Match storage ingress: missing titles stay empty; mid-sentence anchors lose only their token.
        # figure_title's kind fallback would insert machine terms such as data_table into user prose.
        out[str(row["id"])] = figure_prose_title(payload, str(row["kind"]))
    return out


def collect(db_path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """只读扫描：返回需要归一的段（不写库）。"""
    con = _open_read_only(db_path)
    try:
        kinds = tuple(sorted(MARKDOWN_PAYLOAD_KINDS))
        placeholders = ",".join("?" * len(kinds))
        rows = con.execute(
            f"SELECT id, canvas_id, kind, payload, rev, superseded FROM canvas_sections "  # noqa: S608 — same as above
            f"WHERE kind IN ({placeholders}) AND payload LIKE '%[figure:%' ORDER BY canvas_id, seq",
            kinds,
        ).fetchall()
        figures_cache: dict[str, dict[str, str]] = {}
        todo: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                sys.stderr.write(f"  ! {row['id']} 的 payload 不是合法 JSON，跳过\n")
                continue
            md = payload.get("markdown")
            if not isinstance(md, str) or "[figure:" not in md:
                continue
            canvas_id = str(row["canvas_id"])
            if canvas_id not in figures_cache:
                figures_cache[canvas_id] = _figures_of_canvas(con, canvas_id)
            new_md, stats = normalize_figure_anchors(md, figures_cache[canvas_id])
            if not stats["changed"]:
                continue
            todo.append(
                {
                    "section_id": str(row["id"]),
                    "canvas_id": canvas_id,
                    "kind": str(row["kind"]),
                    "rev": int(row["rev"]),
                    "superseded": bool(row["superseded"]),
                    "stats": stats,
                    "before": md,
                    "after": new_md,
                }
            )
            if limit is not None and len(todo) >= limit:
                break
        return todo
    finally:
        con.close()


async def apply(db_path: Path, todo: list[dict[str, Any]]) -> tuple[int, int, int]:
    """写库：走 `patch_section`（留 revision）。返回 (成功, 失败, 跳过)。

    ⚠ CAS 冲突算**跳过**不算失败：那是文档里写明的正常情况（扫描与写入之间有人动过这段），
    处置方式就是复跑一次。混进 failed 会让例行的「该复跑了」在运维或 CI 眼里长得像作业炸了。
    """
    store = CanvasStore(db_path=db_path)
    ok = failed = skipped = 0
    for item in todo:
        try:
            updated = await store.patch_section(
                section_id=item["section_id"],
                patch={"markdown": item["after"]},
                by_turn_id=JOB_TURN_ID,
                canvas_id=item["canvas_id"],
                # ⚠ CAS is required: `after` comes from the read-only snapshot at collect() time, while this job runs
                # against the **live production database**. Any intervening change (user editing, RC-8 writeback,
                # or an active turn) would be overwritten with its body text by whole-section replacement without CAS.
                # Including rev exposes conflicts as skipped sections; rerun once to resolve them.
                expected_rev=item["rev"],
            )
        except CanvasRevConflictError:
            sys.stderr.write(f"  ~ {item['section_id']} 扫描之后被人改过，跳过（复跑一次即可）\n")
            skipped += 1
            continue
        except Exception as exc:  # noqa: BLE001 — one section's failure must not interrupt the whole job
            sys.stderr.write(f"  ! {item['section_id']} 写入失败：{exc!r}\n")
            failed += 1
            continue
        if updated is None:
            sys.stderr.write(f"  ! {item['section_id']} 未找到\n")
            failed += 1
            continue
        ok += 1
        if ok % 100 == 0:
            _out(f"  … 已写入 {ok} 段")
    return ok, failed, skipped


def _print_report(todo: list[dict[str, Any]], sample: int) -> None:
    total = {"moved": 0, "stripped": 0, "titled": 0}
    canvases = {item["canvas_id"] for item in todo}
    for item in todo:
        for key in total:
            total[key] += item["stats"][key]
    _out(f"需要归一：{len(todo)} 段，跨 {len(canvases)} 个画布（其中已折叠 {sum(1 for x in todo if x['superseded'])} 段）")
    _out(f"锚：挪位 {total['moved']} · 摘掉 {total['stripped']} · 换成标题 {total['titled']}")
    # Print **every** section that loses content, regardless of --sample: whoever approves --apply must see which body text disappears.
    # Aggregate counts and just the first few diffs amount to blind approval (review round 3 #8).
    removing = [x for x in todo if x["stats"]["stripped"]]
    for item in removing:
        _out(f"\n!!! 将删除内容 {item['section_id']}（canvas={item['canvas_id']} rev={item['rev']}）")
        _out(f"  前：{item['before']!r}")
        _out(f"  后：{item['after']!r}")
    # Compare IDs, not whole dicts with `in`: multi-KB before/after bodies cause real stalls during database-wide backfills.
    removing_ids = {x["section_id"] for x in removing}
    for item in [x for x in todo if x["section_id"] not in removing_ids][:sample]:
        _out(f"\n--- {item['section_id']}（canvas={item['canvas_id']} rev={item['rev']}）")
        _out(f"  前：{item['before'][:160]!r}")
        _out(f"  后：{item['after'][:160]!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="存量正文图锚位置归一（默认只读 dry-run）。")
    parser.add_argument("--db", required=True, type=Path, help="canvas 所在 sqlite 库路径")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只打印影响面）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少段（排查用）")
    parser.add_argument("--sample", type=int, default=5, help="打印几条前后对照")
    args = parser.parse_args()

    if not args.db.exists():
        parser.error(f"库不存在：{args.db}")

    todo = collect(args.db, limit=args.limit)
    _print_report(todo, args.sample)
    if not todo:
        _out("\n无需处理。")
        return
    if not args.apply:
        _out("\n这是 dry-run，没有写库。确认无误后加 --apply（先备份 data/sqlite/，含 -wal/-shm）。")
        return
    ok, failed, skipped = asyncio.run(apply(args.db, todo))
    _out(f"\n写入完成：成功 {ok}，失败 {failed}，跳过 {skipped}（跳过=扫描后被人改过，复跑一次即可）。")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
