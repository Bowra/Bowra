"""Canvas 展示规范序（canonical report order）——**单源**.

A3 (report-edit-audit, 口径已拍=seq 权威化): 库里 seq 就是唯一权威序（页面按它渲染、
导出按它出块、用户拖拽改它）。为此 compose 落库时就得写展示规范序，而这份 rank 必须与
前端 `report-view.tsx preprocessSections` 的 rank **逐字对齐**（双端对拍测试钉死），
否则"服务端写的序"和"前端认的序"两套口径又会分叉——正是 P1-1 的病根。

来源：writer-loop 在 compose 时才 emit 头部 section（eyebrow/h1/meta），它们的 seq 因此
**晚于** peer-loop 期落库的 code_block / 裸 chart。不重排则 python 代码块领跑报告。

设计口径（与 FE 同）：
    header 领跑 (eyebrow → h1 → meta → kpi_row) → body (默认) → sources → code/sql 附录沉底
同 rank 内保持传入相对序（稳定排序）：narrative→[figure:] 的织入序与多写手章序不能被打乱。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

_FALSY = {"0", "false", "no", "off"}
_SEQ_AUTHORITATIVE_FLAG = "FEATURE_REPORT_EDIT_SYNC_V2"


def is_seq_authoritative() -> bool:
    """A3 flag: whether database seq is the authoritative display order (default on since 2026-10-14; 0 disables).

    翻开前 compose 仍写 weave 序、存量 canvas 未迁移，故前端必须继续用
    `preprocessSections` 的 rank 强排兜底；翻开（且迁移作业跑完）后前端信任 seq，
    用户拖拽的顺序才不会被规范排序打回（P1-4 的 B 口径）。

    ⚠ 翻 flag 与跑迁移作业必须是**同一次操作**（设计 review F6）：flag 关时 compose
    写 weave 序，"先迁移后翻 flag"会让已迁移 canvas 的新 turn 又被排回旧序 → 混合态。
    """
    return os.environ.get(_SEQ_AUTHORITATIVE_FLAG, "1").strip().lower() not in _FALSY


# Match `rank()` in packages/bowhead_frontend/components/canvas/report-view.tsx exactly.
# Changes here require matching frontend changes and test_canvas_rank_parity.py.
_HEADER_RANKS: dict[str, int] = {
    "eyebrow": 0,
    "h1": 1,
    "meta": 2,
    "kpi_row": 3,  # KPI band leads the body (design: header → KPI → prose)
}
_APPENDIX_KINDS: frozenset[str] = frozenset({"code_block", "sql_block"})
_BODY_RANK = 50
# canvas-elements sources-merge batch: sources belong at the **document end**, after the body and before code appendices.
# Once merged into a Canvas singleton, seq freezes at the first turn's position; without a separate rank it lands amid later-turn body text.
_SOURCES_RANK = 90
_APPENDIX_RANK = 100


class _HasKind(Protocol):
    kind: str


def canonical_rank(kind: str) -> int:
    """kind → 展示序 rank（小的在前）。未知 kind 落 body 档（与 FE 的 `return 50` 同）。"""
    if kind in _HEADER_RANKS:
        return _HEADER_RANKS[kind]
    if kind == "sources":
        return _SOURCES_RANK
    if kind in _APPENDIX_KINDS:
        return _APPENDIX_RANK
    return _BODY_RANK


def _segment_bounds(kinds: Sequence[str]) -> list[int]:
    """多报告分段起点（★116 dogfood 1.0.93 P2）。

    plan 多腿轮里 ψ-E 每腿各出一份完整报告（各带 h1）顺序落库；全局 rank 强排
    会把所有 h1（rank 1）、所有 kpi_row（rank 3）各自归堆——两腿报告被拆散互插
    （F 轮实测两 h1 连排）。规则：**第二个及以后的 h1 起新段**（它是下一份报告
    的头），紧邻其前的 eyebrow 随行；单 h1 画布=单段，行为与原全局排序逐字节同。
    与 FE `report-view.tsx preprocessSections` 的分段逻辑**逐字对齐**
    （test_canvas_rank_parity.py + 双端各自的多腿 fixture 钉住）。
    """
    bounds = [0]
    seen_h1 = False
    for i, kind in enumerate(kinds):
        if kind != "h1":
            continue
        if seen_h1:
            start = i
            if i > 0 and kinds[i - 1] == "eyebrow" and start > bounds[-1]:
                start = i - 1  # This leg's eyebrow enters the new segment with its h1.
            if start > bounds[-1]:
                bounds.append(start)
        seen_h1 = True
    return bounds


def canonical_order(sections: Sequence[_HasKind | Any]) -> list[str]:
    """按展示规范序返回 section id 列表（稳定：同 rank 保持传入相对序）。
    多 h1 画布按报告分段各自排序（段间保持传入相对序），见 `_segment_bounds`。"""
    kinds = [str(s.kind) for s in sections]
    bounds = _segment_bounds(kinds)
    out: list[str] = []
    for bi, start in enumerate(bounds):
        end = bounds[bi + 1] if bi + 1 < len(bounds) else len(kinds)
        indexed = list(enumerate(sections[start:end], start=start))
        indexed.sort(key=lambda pair: (canonical_rank(str(pair[1].kind)), pair[0]))
        out.extend(str(section.id) for _, section in indexed)
    return out


def is_canonically_ordered(sections: Iterable[_HasKind | Any]) -> bool:
    """当前顺序是否已是展示规范序（分段内 rank 单调不减）。迁移作业用来跳过 no-op canvas。"""
    secs = list(sections)
    kinds = [str(s.kind) for s in secs]
    bounds = set(_segment_bounds(kinds))
    last = -1
    for i, kind in enumerate(kinds):
        if i in bounds:
            last = -1
        rank = canonical_rank(kind)
        if rank < last:
            return False
        last = rank
    return True
