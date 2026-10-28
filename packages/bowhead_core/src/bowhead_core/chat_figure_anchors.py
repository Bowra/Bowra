"""Validate and repair chat figure anchors after allowlist translation.

Keep anchors whose IDs are known. Repair invalid anchors in document order only
when their count exactly matches the current turn's unreferenced figures, using
figure creation order. Otherwise strip them and orphaned figure captions through
the report path's existing implementation.

Unequal counts do not establish a one-to-one correspondence. Do not guess an
anchor's figure and attach evidence to the wrong argument. Record repair and
removal counts for diagnostics.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from bowhead_core.tool_category import canonical_tool_name

# ⚠ Match by **tool name**, not `result["kind"]`: `add_chart` returns **chart types**
# (observed "bar" / "line" / "pie" / "table" / "dual_axis"), not section kinds.
# Comparing it to "chart" would match nothing.
#
# This tuple is the **sole source of truth**, imported by `subagent._INLINEABLE_CANVAS_TOOLS`;
# keep the knowledge of inlineable artifacts in one place.
INLINEABLE_CANVAS_TOOLS: tuple[str, ...] = (
    "add_chart",
    "canvas_add_kpi_row",
    "canvas_add_data_table",
    "canvas_add_table",
    # Image charts (matplotlib maps / radar / Gantt / multiple panels: types native rendering cannot handle,
    # assigned to `add_image` by `compute.md`) **are inlineable too**.
    # 116 dogfood t_6787e8159e94 found mismatched ids (list `sec_*`, bubble `art_*`);
    # `_inline_image_artifact` lacked `section_id`, **not** because images should not be inline.
    # That path now supplies a `section_id` alias, unifying anchor namespace as `sec_*`.
    "add_image",
)

# Cross-module literal body-figure anchor (same character class as `report_writer_loop._FIGURE_ID_IN_RUN` /
# `export_ir._FIGURE_ANCHOR_RE` / `md.ts::_FIGURE_INLINE_RE`).
_FIGURE_RE = re.compile(r"\[figure:([A-Za-z0-9_]+)\]")


@dataclass(frozen=True)
class AnchorStats:
    """一次 `resolve_anchors` 的结果计数（喂日志/指标，不进用户面）。"""

    total: int = 0
    kept: int = 0
    repaired: int = 0
    stripped: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.repaired or self.stripped)


def figures_from_invocations(invocations: Iterable[Any] | None) -> list[str]:
    """本轮**可内联产物**的 section id，按建图序、去重保序。

    与 `subagent._canvas_sections_from_invocations` 同源判据（工具名 +
    结果里有 `section_id` + 无 error），但只要 id —— 位置修复要的就是这个顺序。
    """
    out: list[str] = []
    seen: set[str] = set()
    for inv in invocations or []:
        name = canonical_tool_name(str(getattr(inv, "tool_name", "") or ""))
        if name not in INLINEABLE_CANVAS_TOOLS:
            continue
        if getattr(inv, "error", None) is not None:
            continue
        res = getattr(inv, "result", None)
        if not isinstance(res, dict) or res.get("error"):
            continue
        sid = res.get("section_id") or res.get("id")
        if not sid:
            continue
        sid = str(sid)
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _anchor_ids_in_order(text: str) -> list[str]:
    """正文里出现的锚 id，**按文档序**；围栏区（```/~~~）里的方括号不算锚。"""
    ids: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        ids.extend(_FIGURE_RE.findall(line))
    return ids


def resolve_anchors(
    text: str,
    *,
    figures: Sequence[str],
    known_ids: Iterable[str],
) -> tuple[str, AnchorStats]:
    """单段正文的便捷入口 —— 校验 + 按位置修复 + 剥掉剩余无效锚。

    实现**只此一份**：转发给 `PayloadAnchorResolver`（跨字段那条路才是生产用的）。
    单独写一份「单文本版」的逻辑就是同一个判据两处实现，迟早分叉。

    Args:
        text: 待处理正文（peer 写的散文 / payload 的某个叙述字段）。
        figures: 本轮可内联产物的 section id，**按建图序**（位置修复的目标序列）。
        known_ids: 认得的 id 全集（本轮的图 + 本会话画布上真实存在的图/图片）。
            命中它的锚原样保留 —— 引用**跨轮旧图**在 chat 里同样合法，
            由前端按 `inline_artifacts` 决定渲不渲，不该在这里判死。

    Returns:
        `(新正文, 计数)`。无锚点 / 无证据时原样返回。
    """
    resolver = PayloadAnchorResolver(figures=figures, known_ids=known_ids)
    resolver.observe(text)
    resolver.plan()
    return resolver.apply(text), resolver.stats


class PayloadAnchorResolver:
    """跨**多个字段**做同一次判定的两趟解析器。

    ★ 为什么不能逐字段各判各的：条数判据的分母是「**本轮**未被引用的图」，而锚点
    散落在 `exec_summary` / 各 insight 的 narrative 里。逐字段跑的话，某字段只有 1 个
    锚而本轮有 6 张图 ⇒ 1 != 6 ⇒ 永远匹配不上、整批退化成剥。必须先把整个 payload
    的锚点收齐再决定。

    用法：`observe()` 走一趟收集 → `plan()` 定方案 → `apply()` 再走一趟改写。
    两趟都用 `RefAllowlist.map_payload_texts` 的同一份字段清单，顺序稳定。
    """

    def __init__(self, *, figures: Sequence[str], known_ids: Iterable[str]) -> None:
        self._figures = list(figures)
        self._known = {str(k) for k in known_ids if k}
        self._seen: list[str] = []
        self._queue: list[str] = []
        self.stats = AnchorStats()
        # Same fail-open criterion as the single-text entry (see `resolve_anchors`).
        self._blind = not self._known and not self._figures

    def observe(self, text: str) -> str:
        """第一趟：只收集，不改字。返回值即入参（供 `map_payload_texts` 直接用）。"""
        if text and "[figure:" in text:
            self._seen.extend(_anchor_ids_in_order(text))
        return text

    def plan(self) -> None:
        """按**整个 payload** 的锚点定方案。"""
        if self._blind or not self._seen:
            return
        invalid = [fid for fid in self._seen if fid not in self._known]
        if not invalid:
            return
        kept = {fid for fid in self._seen if fid in self._known}
        unused = [f for f in self._figures if f not in kept]
        if unused and len(invalid) == len(unused):
            self._queue = list(unused)

    def apply(self, text: str) -> str:
        """第二趟：改写 + 剥。"""
        if self._blind or not text or "[figure:" not in text:
            return text
        in_doc = _anchor_ids_in_order(text)
        if not in_doc:
            return text
        # ★ If nothing needs repair, **change no bytes**: `_strip_invalid_figure_anchors` below
        # uses `.strip()` and collapses 3+ blank lines; running unconditionally changes user-visible whitespace every turn.
        # This gate handles invalid anchors, not incidental body reformatting.
        valid = self._known | set(self._figures)
        if not self._queue and all(fid in valid for fid in in_doc):
            self.stats = AnchorStats(
                total=self.stats.total + len(in_doc),
                kept=self.stats.kept + len(in_doc),
                repaired=self.stats.repaired,
                stripped=self.stats.stripped,
            )
            return text
        repaired_before = len(self._queue)
        if self._queue:
            text = _rewrite_invalid_anchors(text, self._known, self._queue, consume=True)
        repaired = repaired_before - len(self._queue)

        from bowhead_core.report_writer_loop import _strip_invalid_figure_anchors

        # Issued numbers count as known, or the next sentence immediately strips repaired anchors.
        cleaned, stripped = _strip_invalid_figure_anchors(text, self._known | set(self._figures))
        self.stats = AnchorStats(
            total=self.stats.total + len(in_doc),
            kept=self.stats.kept + len(in_doc) - stripped - repaired,
            repaired=self.stats.repaired + repaired,
            stripped=self.stats.stripped + stripped,
        )
        return cleaned


def _rewrite_invalid_anchors(
    text: str,
    known: set[str],
    new_ids: list[str],
    *,
    consume: bool = False,
) -> str:
    """把**无效锚**按出现序依次改写成 `new_ids`，有效锚与围栏区不动。

    ⚠ 按**出现位置**消费而不是按 id 建映射再全局替换：同一个坏 id 完全可能出现两次
    （模型复述同一个数字），按 id 建 dict 会把两处塌缩成一条、也就绑到同一张图上。
    调用方已保证 `len(无效锚) == len(new_ids)`，所以这里逐个 pop 不会越界。

    ``consume=True`` 时**原地消费** `new_ids`：跨字段解析要让队列接着上一个字段往下走，
    每个字段各拿一份拷贝会把同一张图发给多个字段。
    """
    out: list[str] = []
    in_fence = False
    pending = new_ids if consume else list(new_ids)

    def _repl(m: re.Match[str]) -> str:
        if m.group(1) in known or not pending:
            return m.group(0)
        return f"[figure:{pending.pop(0)}]"

    for line in text.split("\n"):
        stripped_line = line.lstrip()
        if stripped_line.startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
            continue
        out.append(line if in_fence else _FIGURE_RE.sub(_repl, line))
    return "\n".join(out)


def resolve_run_figure_anchors(
    text: str,
    *,
    ref_allowlist: Any,
    invocations: Iterable[Any] | None,
) -> tuple[str, AnchorStats]:
    """一个 agent run 结束时，用**它自己那份 ν1.5 表**把答案里的图锚落定。

    ★ 为什么必须按 run 分表（116 dogfood `t_c83badd6c5ff` 逮到）：`_ν15_allowlist`
    建在 `SubagentRunner._legacy_run` 里，而 **Lead 自己也走 `_legacy_run`** ⇒ 一个 turn
    有**两份表、各自编号**。实测同两张图：peer 眼里是 17/19，Lead 眼里是 2/3。
    拿 A 的表去翻 B 写的号，会把锚点**绑到另一张图上**——比不翻更坏。
    而 `_ν_cleanup_ctx()` 在 run 的每个出口都会把 ctx 上的表还原掉，所以更下游
    （`lead_orchestrator` 的终答收口）**根本拿不到正确的表**。落点只能在这里。

    翻译之后仍无效的锚交给 `resolve_anchors`（位置修复 / 剥 / fail-open 判据同源）。
    """
    if not text or "[figure:" not in text:
        return text, AnchorStats()
    if ref_allowlist is not None:
        with contextlib.suppress(Exception):
            text = ref_allowlist.translate_figure_markers(text)
    flat = _flatten_nested(invocations)
    figures = figures_from_invocations(flat)
    known: set[str] = set(figures)
    if ref_allowlist is not None:
        with contextlib.suppress(Exception):
            known |= ref_allowlist.real_ids_of_kinds(("chart", "image"))
    return resolve_anchors(text, figures=figures, known_ids=known)


def _flatten_nested(invs: Iterable[Any] | None) -> list[Any]:
    """展开 `subagent_spawn` 的嵌套调用——Lead 自己没调 `add_chart`，图是 peer 画的。

    拿不到嵌套时退化成平铺列表：`figures` 为空只是**用不上位置修复**，
    翻译与「按 allowlist 认得的 id 保留」照常，不会误剥（known 非空即有证据）。
    """
    out: list[Any] = []
    for inv in invs or []:
        out.append(inv)
        nested = getattr(inv, "nested", None)
        if isinstance(nested, list):
            out.extend(_flatten_nested(nested))
    return out
