"""续轮「更新画布报告」sprint 的纯函数与开关。

设计权威：``docs/sprints/canvas-update-turn-2026-09-03/tech-design.md``（批 A / B0）。

这里只放**纯判据**与 bus 记账，不碰 store、不发事件，好让 hot path
（``lead_orchestrator`` / ``report_writer_loop`` / ``subagent_return_tool``）只换判据、
单测直接打这些函数。三条总则（tech-design §0）：

- 判据一律按「结果」不按「调用」：write_report 成功由 handler 自己在 bus 上记账，
  不依赖 invocation 快照的时序（并行工具批次里 ``write_report`` 与 ``subagent_return``
  同批时快照拿不到结果）。
- 区分「谁写的正文」与「chrome 渲过没有」：亲笔判据（放行收尾器）看前者；
  eager 卡片去重看后者。两者混用会打掉 plan 多章文档的封面/KPI/建议池
  （plan 各章由 eager 以 chapter_mode 落库，不渲文档级 chrome，靠收尾器补）。
- α 折叠只管普通轮；plan 执行轮的折叠归 ``supersede_stale_owner_sections``。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from bowhead_core.figure_anchor_layout import FIGURE_KINDS

logger = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# Same shape as report_final_gate._FIGURE_REF_RE; avoid importing the gate's heavy dependencies into the pure-function layer.
_FIGURE_REF_RE = re.compile(r"\[figure:([a-zA-Z0-9_]+)\]")

# Batch B0 switch (on by default; 0 disables). Batch A reads it only to change the not_owner hint text.
_SECTION_REVISE_ENV = "FEATURE_CANVAS_SECTION_REVISE"
# Batch A2 kill switch (on by default): 0 restores the authorship criterion to owner starting with sub_.
_EAGER_CHROME_DEDUP_ENV = "BOWHEAD_EAGER_CHROME_DEDUP"


def section_revise_enabled() -> bool:
    """`FEATURE_CANVAS_SECTION_REVISE` gates section-level revision (batch B0); default on, 0 disables."""
    return os.environ.get(_SECTION_REVISE_ENV, "1").strip().lower() in _TRUE_VALUES


def eager_chrome_dedup_enabled() -> bool:
    """`BOWHEAD_EAGER_CHROME_DEDUP`——eager 卡片去重（批 A2），默认开。"""
    return os.environ.get(_EAGER_CHROME_DEDUP_ENV, "1").strip().lower() in _TRUE_VALUES


# ─── Section metadata ────────────────────────────────────────────────────────


def _payload(section: Any) -> dict[str, Any]:
    p = getattr(section, "payload", None)
    return p if isinstance(p, dict) else {}


def _meta(section: Any) -> dict[str, Any]:
    m = _payload(section).get("_meta")
    return m if isinstance(m, dict) else {}


def is_eager_composed(section: Any) -> bool:
    """ψ-E eager 卡片路写的段（``compose_canvas_from_payload`` 带 peer_id 时盖的章）。"""
    return _meta(section).get("composed_by") == "eager"


def is_eager_chapter(section: Any) -> bool:
    """eager 以 chapter_mode 写的章：plan 轮的腿，或 B0 revise/append 档下 write_report 没写成时的卡片路
    （`no_document_chrome`）。章不渲文档级 chrome；收尾器按轮次/档位决定补不补。"""
    return is_eager_composed(section) and bool(_meta(section).get("chapter"))


def is_peer_authored_narrative(section: Any) -> bool:
    """「本轮有 peer 亲笔 narrative」的唯一判据（放行收尾器 / 跳过缺省代笔）。

    - ``write_report`` 落的段（owner=sub_*、无 eager 标记）→ 亲笔。
    - eager **章**（plan 轮 chapter_mode；B0 revise/append 档的卡片路同样落章）→ 亲笔：文档级 chrome
      还没渲，收尾器要补（revise/append 档下收尾器只补建议/局限/来源，不发 h1/KPI）。
    - eager **非章**（普通轮卡片）→ **不算**：eager 已把 h1/meta/kpi_row/recs/caveats/sources
      整套渲完，收尾器再来一套就是同轮双 chrome（spike S5b）。
    kill switch 关时退回旧判据（只看 owner 前缀）。
    """
    if str(getattr(section, "kind", "") or "") != "narrative":
        return False
    if not str(getattr(section, "owner", "") or "").startswith("sub_"):
        return False
    if not eager_chrome_dedup_enabled():
        return True
    return not (is_eager_composed(section) and not is_eager_chapter(section))


# ─── α folding (batch A1) ─────────────────────────────────────────────────────


def turn_opened_new_document(this_turn_sections: Iterable[Any]) -> bool:
    """本轮 append 了 h1 ⇒ 立了一份新文档（eager 的 h1、亲笔收尾器的 h1、plan 章 h1 都算）。"""
    return any(str(getattr(s, "kind", "") or "") == "h1" for s in this_turn_sections)


def referenced_figure_ids(this_turn_sections: Iterable[Any]) -> set[str]:
    """本轮 narrative 正文里引用的 ``[figure:id]``——这些图哪怕是上一轮的也不能折。"""
    out: set[str] = set()
    for s in this_turn_sections:
        if str(getattr(s, "kind", "") or "") != "narrative":
            continue
        md = _payload(s).get("markdown")
        if isinstance(md, str) and md:
            out.update(_FIGURE_REF_RE.findall(md))
    return out


def prior_block_ids(
    visible_sections: Iterable[Any],
    *,
    this_turn_id: str | None,
    exclude_ids: Iterable[str] = (),
) -> list[str]:
    """旧块 = 可见、未 superseded、append 轮不是本轮、且不在 exclude_ids 里的段。

    ``Section.turn_id`` 冻结在 append 轮（patch 不改），正是「谁立的」。以前按
    ``seq < 本轮最小 seq`` 判，被 ``promote_document_chrome`` 的整份重编 seq 打瞎
    （本轮 chrome 排到 seq 1 ⇒ 旧块空集）。

    ★ ``sources`` 段在**画布作用域下**永不折叠（canvas-elements 来源合并批，
    判据共用 ``sources_resolver.is_protected_sources_section``，跟着 flag 走——
    flag 关时每轮各有一份来源段，不折它们只会让当前报告里越堆越多）：`[N]` 已是画布作用域，
    整张画布只有这一段，它一旦被 supersede，下一轮就找不到它、从 ``[1]`` 重建一份，
    此前所有轮次正文里的 `[N]` 立刻集体指错。今天它能活着只是因为「本轮 patch 过」
    恰好进了 ``exclude_ids``——那是**副作用**不是保证：upsert 是 best-effort，
    一次失败（两次 rev 冲突 / entries 为空提前返回）就会让它落进旧块。
    """
    from bowhead_core.sources_resolver import is_protected_sources_section

    excluded = set(exclude_ids)
    return [
        s.id
        for s in visible_sections
        if not getattr(s, "superseded", False)
        and not is_protected_sources_section(getattr(s, "kind", ""))
        and (getattr(s, "turn_id", None) or None) != this_turn_id
        and s.id not in excluded
    ]


# ─── Successful write_report bookkeeping (batch A3) ───────────────────────────

_WRITE_OK_ATTR = "_write_report_ok_writers"


def note_write_report_ok(bus: Any, writer: str | None) -> None:
    """``write_report`` 成功（append 或 update 返 ok）时由 handler 调用，在 turn 级 bus 上记账。"""
    if bus is None or not writer:
        return
    with contextlib.suppress(Exception):  # Optional bookkeeping must never fail the write
        ledger = getattr(bus, _WRITE_OK_ATTR, None)
        if not isinstance(ledger, set):
            ledger = set()
            setattr(bus, _WRITE_OK_ATTR, ledger)
        ledger.add(str(writer))


def write_report_succeeded(bus: Any, writer: str | None, invocations: Iterable[Any] | None) -> bool:
    """本 peer 有没有**成功**落过 write_report（ψ-E eager 抑制的判据）。

    优先看 bus 记账（不受 invocation 快照时序影响）；bus 缺失或没记到时回落到
    invocations 里 ``result.get("ok") is True``（``ToolInvocation.result`` 就是 handler
    返回的 dict；失败形状是 ``{"error": ...}`` 无 ``ok``）。
    """
    if bus is not None and writer:
        ledger = getattr(bus, _WRITE_OK_ATTR, None)
        if isinstance(ledger, set) and str(writer) in ledger:
            return True
    for inv in invocations or []:
        if (getattr(inv, "tool_name", "") or "") != "write_report":
            continue
        res = getattr(inv, "result", None)
        if isinstance(res, dict) and res.get("ok") is True:
            return True
    return False


# ─── Mixed turns: render document chrome once per turn (canvas-elements tech-design-mixed-turn-chrome) ───


def other_writer_succeeded(bus: Any, writer: str | None) -> bool:
    """本轮账本里有**不是本 peer** 的成功 writer——混合轮判据（ψ-E eager 走章模式的依据）。

    116 A3 / C5：peer 1 亲笔成功后崩，Lead 重派的 peer 2 回 payload；peer 2 不在账本里，
    既有的「本 peer 用过笔」压制判不到，eager 照渲整套外壳 ⇒ 同轮两份报告。判据只看账本
    （handler 服务端记账），不看 payload 内容。kill switch 关时恒 False（回到今天）。
    """
    if bus is None or not eager_chrome_dedup_enabled():
        return False
    ledger = getattr(bus, _WRITE_OK_ATTR, None)
    if not isinstance(ledger, set) or not ledger:
        return False
    me = str(writer) if writer else None
    return any(str(w) != me for w in ledger)


def strip_mixed_turn_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """混合轮里后到的 payload 交给 eager 的形状。

    ``title`` 永远去掉：混合轮里它是**文档标题**（与亲笔那份同名），渲出来就是第二个 h1；
    章标题在 chapter_mode 下另有来源。``exec_summary`` / ``kpis`` 只在**还有别的可渲内容**
    （``insights``）时才去——否则这份 payload 会被剥成空壳，这个 peer 的产出整个静默消失
    （review 第 4 条：薄 payload 只有 exec_summary + kpis 时，收尾器也不会替它渲正文）。
    图 / sources 照落；recommendations / caveats 由 chapter_mode 交文档级池。"""
    p = dict(payload or {})
    p.pop("title", None)
    if p.get("insights"):
        p.pop("exec_summary", None)
        p.pop("kpis", None)
    return p


def _chrome_piece_of(section: Any) -> str | None:
    kind = str(getattr(section, "kind", "") or "")
    style = str(_payload(section).get("style") or "")
    if kind == "h1":
        return "h1"
    if kind == "kpi_row":
        return "kpi_row"
    if kind in ("meta", "eyebrow") or (kind == "narrative" and style == "byline"):
        return "byline"
    if kind == "recommendations" or (kind == "narrative" and style == "recommendations"):
        return "recommendations"
    if kind == "narrative" and style == "caveats":
        return "caveats"
    return None


def turn_chrome_present(this_turn_sections: Iterable[Any]) -> set[str]:
    """本轮已渲过的**文档级**外壳件：``{"h1","byline","kpi_row","recommendations","caveats"}`` 的子集。

    只数 eager **非章**落的段（``composed_by=eager`` 且 ``chapter`` 假——它渲的是整套文档外壳）和
    owner 为空的段（Lead 末轮 / 收尾器发的）。eager 章（plan 腿、混合轮后到的 payload）与亲笔段的
    h1 / KPI 带是章的内容，不算——否则 plan 文档的封面会被自己的章挡掉。kill switch 关时恒空集。
    """
    if not eager_chrome_dedup_enabled():
        return set()
    out: set[str] = set()
    for s in this_turn_sections:
        if getattr(s, "superseded", False):
            continue
        owner = str(getattr(s, "owner", "") or "")
        document_level = (is_eager_composed(s) and not is_eager_chapter(s)) or not owner
        if not document_level:
            continue
        piece = _chrome_piece_of(s)
        if piece is not None:
            out.add(piece)
    return out


def rendered_document_pool_text(this_turn_sections: Iterable[Any]) -> str:
    """本轮已渲过的**文档级**建议 / 局限段的正文，拼成一段文本供**内容级**去重。

    为什么不能整块跳过（review 第 1 条）：收尾器拿的是**聚合** payload，它的建议 / 局限池
    可能来自多个 peer，而 eager 只渲过其中一个 peer 的那几条。整块跳过会把别的 peer 的条目
    一起丢掉；provenance 那几条更是守卫的机器判定，模型自己不会写，丢了就是「指控整条消失」
    （`canvas_composer` 里那条长注释说的就是它）。所以按条比对：渲过的不再渲，没渲过的照补。
    """
    if not eager_chrome_dedup_enabled():
        return ""
    parts: list[str] = []
    for s in this_turn_sections:
        if getattr(s, "superseded", False):
            continue
        if _chrome_piece_of(s) not in ("recommendations", "caveats"):
            continue
        pl = _payload(s)
        for key in ("markdown", "text"):
            v = pl.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
        for item in pl.get("items") or []:
            if isinstance(item, dict):
                parts.append(str(item.get("action") or ""))
    return "\n".join(parts)


_AUTHORED_TITLE_ATTR = "_authored_doc_title"


def note_authored_title(bus: Any, title: str | None) -> None:
    """亲笔正文首行 ``# 标题`` 在 bake 里会被剥掉（`_strip_leading_h1`）；写入前记到 turn 级 bus，
    payload 丢失（peer 崩在 subagent_return 之前）时收尾器拿它当 h1。只记第一次。"""
    if bus is None:
        return
    t = str(title or "").strip()
    if not t:
        return
    with contextlib.suppress(Exception):
        if not getattr(bus, _AUTHORED_TITLE_ATTR, None):
            setattr(bus, _AUTHORED_TITLE_ATTR, t)


def authored_title(bus: Any) -> str | None:
    if bus is None or not eager_chrome_dedup_enabled():
        return None
    v = getattr(bus, _AUTHORED_TITLE_ATTR, None)
    if not isinstance(v, str):
        return None
    return v.strip() or None


# ─── V4 commit markers (batch A3) ─────────────────────────────────────────────


def mark_turn_committed_to_v4(bus: Any) -> None:
    """Mark V4 ownership without suppressing an unperformed final composition.

    Set _psi_skip_final_compose_only only after _psi_compose_done is true, so a failed
    eager write cannot prevent the final cards from rendering.
    """
    if bus is None:
        return
    with contextlib.suppress(Exception):
        bus._turn_committed_to_v4 = True
    if getattr(bus, "_psi_compose_done", False):
        with contextlib.suppress(Exception):
            bus._psi_skip_final_compose_only = True


# ─── Batch B0: section-level revision ─────────────────────────────────────────

CANVAS_MODES: tuple[str, ...] = ("new_report", "revise", "append")
# Merge multiple spawns in one turn (like delivery's report-wins rule): revision > chapter append > new report.
_CANVAS_MODE_RANK = {"revise": 2, "append": 1, "new_report": 0}
_LEAD_CANVAS_MODE_ATTR = "_lead_canvas_mode"
_REVISED_IDS_ATTR = "_revised_section_ids"


def normalize_canvas_mode(raw: Any) -> str | None:
    v = str(raw or "").strip().lower()
    return v if v in CANVAS_MODES else None


def note_lead_canvas_mode(bus: Any, mode: str | None) -> str | None:
    """spawn handler 记 Lead 的 `canvas_mode` 到 turn 级 bus；多 spawn 取最强档。返回生效值。"""
    mode = normalize_canvas_mode(mode)
    if bus is None or mode is None:
        return lead_canvas_mode(bus)
    with contextlib.suppress(Exception):
        cur = normalize_canvas_mode(getattr(bus, _LEAD_CANVAS_MODE_ATTR, None))
        if cur is None or _CANVAS_MODE_RANK[mode] > _CANVAS_MODE_RANK[cur]:
            setattr(bus, _LEAD_CANVAS_MODE_ATTR, mode)
    return lead_canvas_mode(bus)


def lead_canvas_mode(bus: Any) -> str | None:
    return normalize_canvas_mode(getattr(bus, _LEAD_CANVAS_MODE_ATTR, None)) if bus is not None else None


# ─── Batch B2-3: missing-argument fallback + preserve the original report ───────
#
# 116 live testing (tech-design-b2 §2.6): both requests to change chapter 3's chart to January–June omitted canvas_mode; after three refusals,
# Lead gave up and told the user the system temporarily could not accept Canvas revisions and to retry later, inventing an outage for an argument error.

CANVAS_MODE_DEFAULT = "append"  # Decision ③: a wrong guess adds a section; guessing revise changes unrequested sections
_CANVAS_MODE_MISSES_ATTR = "_lead_canvas_mode_misses"
_KEEP_PRIOR_ATTR = "_lead_keep_prior_report"


def canvas_mode_miss_should_default(bus: Any, batch: Any) -> bool:
    """记一次「Lead 没给出合法档位」，返回这次该不该按默认档放行。

    只有**更早一步**已经被拒过、Lead 看过拒绝提示仍没给，才放行（review P2-1）：同一步里并行的兄弟 spawn
    还没机会看到提示，第二个若直接放行，等于替 Lead 把它没选的档位定了。``batch`` 是 Lead 这一步的编号
    （`_lead_spawn_batch_seq`）；取不到时退回「本轮第二次」的计数口径。bus 为 None ⇒ 恒不放行（只拒，与今天一致）。"""
    return _earlier_step_already_refused(bus, _CANVAS_MODE_MISSES_ATTR, batch)


def _earlier_step_already_refused(bus: Any, attr: str, batch: Any) -> bool:
    """「拒一次、Lead 下一步仍不对就放行」的共用判据：只有**更早一步**已被拒过才返回 True（同一步并行的兄弟都拒）。
    第一次调用只记账返回 False。``batch`` 取不到时退回「本轮第二次」。bus 为 None ⇒ 恒 False（只拒）。"""
    if bus is None:
        return False
    first = getattr(bus, attr, None)
    if first is None:
        with contextlib.suppress(Exception):
            setattr(bus, attr, ("batch", batch) if batch is not None else ("count", 1))
        return False
    kind, value = first
    if kind == "batch" and batch is not None:
        return batch != value
    return True


def note_keep_prior_report(bus: Any, keep: Any) -> None:
    """拍板 ⑤「听用户的」：用户说没说「原报告别动」只有 Lead 知道（它读原话），由它在 new_report 档传 `keep_prior_report`。
    服务端不按关键词判。只记 True；同轮任一 spawn 说保留就保留。"""
    if bus is None or keep is not True:
        return
    with contextlib.suppress(Exception):
        setattr(bus, _KEEP_PRIOR_ATTR, True)


def keep_prior_report(bus: Any) -> bool:
    return bool(getattr(bus, _KEEP_PRIOR_ATTR, False)) if bus is not None else False


# ─── Batch B2-5: Lead explicitly gives chapter scope; never modify outside it ───
#
# 116 live testing (tech-design-b2 §2.5): a request to revise only chapter 3 also changed three final overall-conclusion sections written by another executor.
# The conclusion began with first-half figures, its second part and cover KPI stayed full-year: contradictory. Decision ②: strictly never modify outside scope, including summaries.
# Lead gives chapter/section names from the user's words (e.g. chapter 3/monthly trends); the server resolves them against actual report chapters into section IDs.

_REVISE_SCOPE_ATTR = "_lead_revise_scope"
_SCOPE_UNBOUNDED = "*"
# 用户点名的章节对不上报告时记下是哪一步：此后（更晚的步）本轮不再派活，只能 clarification_ask 问用户或直接回复。
_SCOPE_MISSES_ATTR = "_lead_revise_scope_misses"
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_ORDINAL_RE = re.compile(r"^第\s*([0-9]+|[一二三四五六七八九十]+)\s*(?:章|节|部分|篇)$")
_SCOPE_NORM_RE = re.compile(r"[\s「」『』《》“”\"'·:：,，。、()（）\-—_#*]+")


@dataclass(frozen=True)
class ScopeUnit:
    """可被点名的一块：plan 文档的一章（章 h1 + 该章全部段），或非 plan 文档里一段带标题的上一轮正文。"""

    title: str
    section_ids: frozenset[str]
    # 116 dogfood (1.3.43, `s_549979486a1b`): a plan chapter h1 may be an uninformative table+insight label; its first body subheading
    # (e.g. women's wear dominates...) actually describes the chapter. Use it for named scope and the list shown to Lead.
    aliases: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n for n in (self.title, *self.aliases) if n))


def _norm_scope_text(s: str) -> str:
    return _SCOPE_NORM_RE.sub("", str(s or "")).lower()


def _cn_ordinal(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    if tok == "十":
        return 10
    if len(tok) == 2 and tok[0] == "十":
        return 10 + _CN_DIGITS.get(tok[1], 0)
    if len(tok) == 2 and tok[1] == "十":
        return _CN_DIGITS.get(tok[0], 0) * 10
    return _CN_DIGITS.get(tok) if len(tok) == 1 else None


_TITLE_ORDINAL_RE = re.compile(
    # A digit immediately after `3.` indicates a decimal (`1.5%增长的原因`, `2.3亿元营收`), not an ordinal; review P2-1.
    r"^\s*(?:第\s*([0-9]+|[一二三四五六七八九十]+)\s*(?:章|节|部分|篇)|[（(]\s*([0-9]+|[一二三四五六七八九十]+)\s*[）)]|([0-9]+|[一二三四五六七八九十]+)\s*(?:、|[.．](?!\d)))"
)


def _title_ordinal(title: str) -> int | None:
    """标题开头显式写的编号（「三、」「3.」「第三章」「（三）」）；没有 ⇒ None。"""
    m = _TITLE_ORDINAL_RE.match(str(title or ""))
    if not m:
        return None
    tok = next(g for g in m.groups() if g)
    return _cn_ordinal(tok)


def _first_heading_level(section: Any) -> int:
    """正文里第一个 markdown 标题的级别（`##` ⇒ 2）；没有标题 ⇒ 0。"""
    for line in str(_payload(section).get("markdown") or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return len(s) - len(s.lstrip("#"))
    return 0


def revise_scope_units(sections: Sequence[Any], *, this_turn_id: str | None) -> list[ScopeUnit]:
    """这份画布上可被点名的单元，按出现顺序：plan 章（章 h1 + 该章全部段），**加上**章之外按标题分组的上一轮正文。

    章之外的分组：一段以 `##` 级标题开头的正文开一个单元，其后更深一级的小节（`###`）与没有标题的段并进来，
    直到下一个同级 / 更高级标题、文档级 h1 或 plan 章为止。

    116 dogfood（1.3.46，`s_030bdd27d75a`）：plan 只有一步落成了带章 h1 的 plan 章，品类 / 门店 / 季节性 / 结论四部分是
    `## 标题 + ### 小节` 的普通正文——原先「有 plan 章就只列 plan 章」⇒ 只剩一个单元，退回清单里唯一的选项是月度章，
    Lead 照着选，「改第二章（品类）」改成了月度章。"""
    vis = [s for s in sections if not getattr(s, "superseded", False) and not getattr(s, "hidden_for_chat_mode", False)]
    chapters: dict[tuple[str, str], dict[str, Any]] = {}
    for s in sorted(vis, key=lambda x: int(getattr(x, "seq", 0) or 0)):  # Chapter positions/first subheadings follow document order
        if not is_eager_chapter(s):
            continue
        key = _chapter_key(s)
        unit = chapters.setdefault(key, {"title": "", "heading": "", "ids": set(), "pos": int(getattr(s, "seq", 0) or 0)})
        unit["ids"].add(str(s.id))
        kind = str(getattr(s, "kind", "") or "")
        if kind == "h1" and not unit["title"]:
            unit["title"] = str(_payload(s).get("text") or "").strip()
        elif kind == "narrative" and not unit["heading"] and _first_heading_level(s):
            unit["heading"] = _section_heading(s)
    # Plan chapters whose h1 never materialized (116 dogfood 1.3.47 `s_6f8297c12cd1`: four monthly-chapter sections had chapter meta but no h1)
    # use the first body subheading as their name. Requiring h1 hid the entire chapter and excluded its sections from grouping below, so named scope never matched.
    ordered: list[tuple[int, ScopeUnit]] = [
        (
            u["pos"],
            ScopeUnit(
                title=u["title"] or u["heading"],
                section_ids=frozenset(u["ids"]),
                aliases=(u["heading"],) if u["heading"] and u["title"] else (),
            ),
        )
        for u in chapters.values()
        if u["title"] or u["heading"]
    ]
    chapter_ids = {sid for u in chapters.values() for sid in u["ids"]}
    group: dict[str, Any] | None = None

    def _close() -> None:
        if group is not None:
            ordered.append((group["pos"], ScopeUnit(title=group["title"], section_ids=frozenset(group["ids"]))))

    for s in sorted(vis, key=lambda x: int(getattr(x, "seq", 0) or 0)):
        kind = str(getattr(s, "kind", "") or "")
        if str(s.id) in chapter_ids or kind == "h1":
            _close()
            group = None
            continue
        if kind != "narrative" or not is_prior_turn_section(s, this_turn_id):
            continue
        if is_provenance_section(s) or _payload(s).get("style") in _TAIL_NARRATIVE_STYLES:
            # Recommendations/caveats/sources are document-level tails, not nameable sections (review P1-2). Also **close the current group**:
            # otherwise untitled body text after the tail rejoins the preceding section, admitting unrelated sections into revision scope.
            _close()
            group = None
            continue
        level = _first_heading_level(s)
        if level and (group is None or level <= group["level"]):
            _close()
            group = {"title": _section_heading(s), "level": level, "ids": {str(s.id)}, "pos": int(getattr(s, "seq", 0) or 0)}
        elif group is not None:
            group["ids"].add(str(s.id))  # Merge deeper subsections/untitled text into the current group
    _close()
    return [u for _pos, u in sorted(ordered, key=lambda t: t[0])]


def resolve_revise_scope(units: Sequence[ScopeUnit], items: Iterable[Any]) -> tuple[set[str], list[str]]:
    """把 Lead 给的章名 / 节名解析成允许改的段 id。返回 (允许的 id, 解析不了的原词)。

    判据是结构化的：「第 N 章 / 节 / 部分」只认标题里**显式写了**编号 N 的单元（「三、」「3.」「第三章」「（三）」）；
    否则按规整后的名字（标题或正文首个小标题）**唯一**包含匹配（任一方向）。匹配到零个或多个都算解析不了——
    回给 Lead 带内容的章节清单让它按内容重给，不猜。

    ⚠ **不按位置数**（116 dogfood 1.3.43 `s_549979486a1b`）：plan 有一步失败时章会缺一个（门店章没出来），
    用户说的「第二章（品类）」按位置落到了月度章上——改错了章，比不限范围更糟。"""
    allowed: set[str] = set()
    unresolved: list[str] = []
    for raw in items:
        item = str(raw or "").strip()
        if not item:
            continue
        m = _ORDINAL_RE.match(item)
        if m:
            n = _cn_ordinal(m.group(1))
            numbered = [u for u in units if n is not None and any(_title_ordinal(name) == n for name in u.names)]
            if len(numbered) == 1:
                allowed |= numbered[0].section_ids
            else:
                unresolved.append(item)
            continue
        key = _norm_scope_text(item)
        hits = (
            [u for u in units if any(key in _norm_scope_text(n) or (_norm_scope_text(n) and _norm_scope_text(n) in key) for n in u.names)]
            if key
            else []
        )
        if len(hits) == 1:
            allowed |= hits[0].section_ids
        else:
            unresolved.append(item)
    return allowed, unresolved


def note_revise_scope(bus: Any, allowed_ids: Iterable[str] | None) -> None:
    """记本轮修订允许改的段。同轮多个修订 spawn 取并集；**任一个没限范围 ⇒ 整轮不限**（不误伤那位 peer 的正常修订）。"""
    if bus is None:
        return
    with contextlib.suppress(Exception):
        cur = getattr(bus, _REVISE_SCOPE_ATTR, None)
        if allowed_ids is None or cur == _SCOPE_UNBOUNDED:
            setattr(bus, _REVISE_SCOPE_ATTR, _SCOPE_UNBOUNDED)
            return
        setattr(bus, _REVISE_SCOPE_ATTR, frozenset(set(cur or ()) | {str(i) for i in allowed_ids}))


def revise_scope_bounded(bus: Any) -> bool:
    """本轮 Lead 点名了修订范围（且没被同轮无范围的修订 spawn 解除）。只决定封面 chrome / KPI 带跟不跟着改；
    段级拦不拦看 `out_of_revise_scope`。"""
    cur = getattr(bus, _REVISE_SCOPE_ATTR, None) if bus is not None else None
    return cur is not None and cur != _SCOPE_UNBOUNDED


def out_of_revise_scope(bus: Any, section_id: str) -> bool:
    """这段在本轮的修订范围之外吗。没设范围 / 整轮不限 ⇒ False。"""
    cur = getattr(bus, _REVISE_SCOPE_ATTR, None) if bus is not None else None
    if cur is None or cur == _SCOPE_UNBOUNDED:
        return False
    return str(section_id) not in cur


def note_revise_scope_unresolved(bus: Any, batch: Any = None) -> None:
    """用户点名的章节对不上报告：记下是哪一步（同一步并行的兄弟照常判，更晚的步一律不再派活）。"""
    if bus is not None:
        _earlier_step_already_refused(bus, _SCOPE_MISSES_ATTR, batch)


def revise_scope_must_ask(bus: Any, batch: Any = None) -> bool:
    """更早一步点名的章节对不上 ⇒ 本轮不再派活，只能 `clarification_ask` 问用户或直接回复。

    116 dogfood（1.3.56，`t_8c8b80040f9c` / `t_eb052e2762c8`）：此前回给 Lead 章节清单让它重给，
    「第五章」被它从清单里硬挑成第四部分改掉；「客户画像」被它去掉范围重派，写手新写一章、回复却说「就地改写完成」。
    分不出「Lead 挑的就是用户说的」与「硬挑」（要分就得拿用户原话做文本比对）⇒ 一律交给用户选（用户拍板）。"""
    first = getattr(bus, _SCOPE_MISSES_ATTR, None) if bus is not None else None
    if first is None:
        return False
    kind, value = first
    return not (kind == "batch" and batch is not None and batch == value)


def scope_unit_display_names(units: Sequence[ScopeUnit]) -> list[str]:
    """每个可点名单元给用户 / 写手看的名字：它名字里（标题、正文首个小标题）**第一个能唯一对上它**的那个；都对不上就用标题。去重。

    review（2026-10-01）：老 plan 报告两章 h1 都是「表+insight」——只用标题当选项，卡片上两个一模一样的选项，
    选了下一轮仍对不上（`resolve_revise_scope` 同名不猜），只能再问一次。"""
    out: list[str] = []
    for u in units:
        pick = next((n for n in u.names if resolve_revise_scope(units, [n]) == (set(u.section_ids), [])), u.title)
        if pick and pick not in out:
            out.append(pick)
    return out


_QUESTION_MAX_CHARS = 200  # clarification_tool 把 question 截到 200 字（schema maxLength 同值）
_LISTED_NAME_MAX_CHARS = 24  # 列在问题里的单个章名上限；前缀照样能被 resolve_revise_scope 的包含匹配对上

_SCOPE_QUESTION_TEXT = {
    "zh": {
        "ordinal": "没法确定「{named}」指的是报告里的哪一部分",
        "missing": "报告里没有找到「{named}」",
        "unknown": "没有在报告里找到你说的那一部分",
        "ask": "，你想改哪一部分？",
        "new": "都不是，新加一章",
        "list": "，你想改哪一部分？报告里有：{names}{more}。也可以回复「新加一章」。",
        "more": " 等 {n} 部分",
        "sep": "；",
    },
    "en": {
        "ordinal": "I couldn't tell which part of the report 「{named}」 refers to",
        "missing": "I couldn't find 「{named}」 in the report",
        "unknown": "I couldn't find the part you mentioned in the report",
        "ask": ". Which part do you want to change?",
        "new": "None of these — add a new section",
        "list": '. Which part do you want to change? The report has: {names}{more}. You can also reply "add a new section".',
        "more": " and {rest} more",
        "sep": "; ",
    },
}


def revise_scope_question(units: Sequence[ScopeUnit], unresolved: Iterable[str], *, lang: str = "zh-CN") -> dict[str, Any] | None:
    """点名对不上时给 Lead 的现成问法（`clarification_ask` 的一个 question）。报告里没有可点名的部分 ⇒ None（调用方不按范围拦）。

    选项是每部分能唯一对上它的名字 +「都不是，新加一章」；多于 3 部分时卡片放不下（每题最多 4 个选项）⇒ 自由回答、章名列在问题里，
    问句放在前面、整句不超过 200 字（超出就截短章名、列不下的写「等 N 部分」——review：此前整句被截断，后面的章和问句都丢了）。
    「第 N 章」类说法措辞是「没法确定指哪一部分」：章确实可能存在、只是标题里没写编号，说「没找到」像系统看不见报告。"""
    names = scope_unit_display_names(units)
    if not names:
        return None
    t = _SCOPE_QUESTION_TEXT["en" if str(lang or "").lower().startswith("en") else "zh"]
    items = [str(x).strip() for x in unresolved if str(x or "").strip()]
    named = "」「".join(items)
    if not items:
        head = t["unknown"]
    elif all(_ORDINAL_RE.match(x) for x in items):
        head = t["ordinal"].format(named=named)
    else:
        head = t["missing"].format(named=named)
    if len(names) <= 3:
        return {"question": head + t["ask"], "type": "single_select", "options": [*names, t["new"]]}
    shown = [n if len(n) <= _LISTED_NAME_MAX_CHARS else n[: _LISTED_NAME_MAX_CHARS - 1] + "…" for n in names]
    for k in range(len(shown), 0, -1):
        more = t["more"].format(n=len(shown), rest=len(shown) - k) if k < len(shown) else ""  # zh 写总数、en 写剩下几部分
        text = head + t["list"].format(names=t["sep"].join(shown[:k]), more=more)
        if len(text) <= _QUESTION_MAX_CHARS:
            return {"question": text, "type": "free_text", "options": []}
    return {"question": (head + t["ask"])[:_QUESTION_MAX_CHARS], "type": "free_text", "options": []}


def find_prior_visible_h1(sections: Iterable[Any], this_turn_id: str | None) -> Any | None:
    """画布上**上一轮**的可见报告标题：**最后一个**未折叠、非本轮的 h1（画布上同时可见多份报告时，续轮改的是最新那份）；没有 ⇒ None。
    spawn handler（canvas_mode 必填的触发条件 + 修订说明里的文档标题）与 Lead 最近几轮块的
    「续轮档位」提示同一口径（review 修：提示此前只看 canvas_id，落过一张图的 chat 会话也被说成
    「已有报告」）。本轮的 h1 不算：同轮第一个 peer 的 eager h1 不能触发到第二个 peer 身上。
    批 B2-3 review：`keep_prior_report` 让两份报告同时可见，取第一个会把下一轮的修订说明指向旧报告。"""
    found = None
    for s in sections:
        if not is_document_h1(s):
            continue
        if (getattr(s, "turn_id", None) or None) != (this_turn_id or None):
            found = s
    return found


# ─── Batch B1-0: event helpers (bus-level, shared by three call sites) ──────────


async def emit_sections_superseded(bus: Any, *, canvas_id: str, turn_id: str | None, section_ids: list[str], folded_by: str | None = None) -> None:
    """`canvas.sections_superseded`（既有事件）——A1 折叠、B1-2 收旧图、B1-1 折章 KPI 共用一个发点。

    `folded_by`（B1-2）随事件上线：折叠原因只写在段 `_meta.folded_by` 里的话，**live 这一轮到不了前端**
    （FE `onSectionsSuperseded` 只合 `superseded`/`superseded_by_turn`，`_meta` 要等下次 hydrate），
    于是刚折的图在当前这轮被渲成「较早的报告已被取代」——正是 B1-2 要修的那个形状（三路 review 同时逮到）。
    best-effort：发事件失败不带崩折叠本身。"""
    if bus is None or not section_ids:
        return
    from bowhead_core.events import OrchestratorEvent

    payload: dict[str, Any] = {
        "turn_id": turn_id,
        "canvas_id": canvas_id,
        "superseded_section_ids": list(section_ids),
        "superseded_by_turn": turn_id,
    }
    if folded_by:
        payload["folded_by"] = folded_by
    try:
        await bus.emit(OrchestratorEvent(event_type="canvas.sections_superseded", turn_id=turn_id, payload=payload))  # type: ignore[arg-type]
    except Exception as e:  # Emit failure must not break persisted folding
        logger.warning("canvas_revise.sections_superseded_emit_failed turn=%s n=%d err=%s", turn_id, len(section_ids), e)


async def emit_section_patched(bus: Any, *, turn_id: str | None, canvas_id: str, section: Any, open_turn: bool = False) -> None:
    """`canvas.section_patched`（既有事件；契约一 update→patched）。payload 带 full section 作 patch（FE late-join upsert）
    与顶层 `seq`（IM 插入位，FE 忽略）。`open_turn` 先发一帧 `canvas.turn_streaming`（bus exactly-once）。
    `report_tools._emit_section_patched(ctx)` 与 B1 收尾器的 chrome patch 共用。best-effort。"""
    if bus is None or section is None:
        return
    from bowhead_core.events import OrchestratorEvent

    try:
        if open_turn:
            with contextlib.suppress(Exception):
                await bus.emit(OrchestratorEvent(event_type="canvas.turn_streaming", turn_id=turn_id or "", payload={"turn_id": turn_id}))  # type: ignore[arg-type]
        rs = section.to_report_section()
        kind = rs.get("kind") if isinstance(rs, dict) else getattr(section, "kind", None)
        await bus.emit(
            OrchestratorEvent(
                event_type="canvas.section_patched",  # type: ignore[arg-type]
                turn_id=turn_id or "",
                payload={
                    "canvas_id": canvas_id,
                    "section_id": section.id,
                    "kind": kind,
                    "patch": rs,
                    "rev": getattr(section, "rev", None),
                    "seq": getattr(section, "seq", None),
                },
            )
        )
    except Exception as e:  # Emit failure must not break writes; retain a warning (review #6: original report_tools emitter warned)
        logger.warning("canvas_revise.section_patched_emit_failed turn=%s section=%s err=%s", turn_id, getattr(section, "id", None), e)


# ─── Batch B1-0: document blocks ──────────────────────────────────────────────

_TAIL_KINDS: frozenset[str] = frozenset({"sources", "recommendations"})
_APPENDIX_KINDS: frozenset[str] = frozenset({"code_block", "sql_block"})
_CHROME_KINDS: frozenset[str] = frozenset({"eyebrow", "meta", "kpi_row"})


def is_document_h1(section: Any) -> bool:
    """文档级 h1 = kind==h1 且未折叠且**不是 eager 章**（plan 章 h1 由 eager 以 chapter_mode 落、
    `_meta.chapter=true`，不算文档边界）。owner 不参与判定：亲笔收尾器写的 h1 owner None，
    卡片路 eager 写的 owner=peer_id 且 chapter=false。"""
    return str(getattr(section, "kind", "") or "") == "h1" and not getattr(section, "superseded", False) and not is_eager_chapter(section)


_TAIL_NARRATIVE_STYLES: frozenset[str] = frozenset({"caveats", "recommendations"})


def is_recommendations_section(section: Any) -> bool:
    """建议段：文档形投影把 `kind=recommendations` 投影成
    `narrative(style="recommendations")`（`canvas_doc_projection.py`）⇒ 两种形状都要认，
    否则现产的报告里建议段既不算尾部区、也永远 patch 不到（review #3/#4）。"""
    kind = str(getattr(section, "kind", "") or "")
    return kind == "recommendations" or (kind == "narrative" and _payload(section).get("style") == "recommendations")


def is_system_caveat_section(section: Any) -> bool:
    """系统生成的局限段：plan 缺腿提示（`_meta.gap`）。它不是作者写的，修订轮重写作者局限时不许覆盖它
    （FIX-2 / 2026-06-25：某条腿失败时报告看起来是完整的）。⚠ 打标前落库的老缺腿段没有这个标记，
    是已知残留（设计 §7）——新写的都带。"""
    return _meta(section).get("gap") is True


def _is_guard_caveat_line(line: str) -> bool:
    """守卫生成的 caveat 行（造数指控）——整组重写时保留。

    判据复用 `provenance_caveats`（四种语言的前缀只在 analysis_payload 一处维护）。review（2026-10-01）：
    此前这里手写了中英两个前缀，德 / 法报告修订时守卫的指控行认不出、被写手整组重写掉——闸被拆了。"""
    from bowhead_core.analysis_payload import provenance_caveats

    return bool(provenance_caveats({"caveats": [line]}))


def _is_tail(section: Any) -> bool:
    kind = str(getattr(section, "kind", "") or "")
    if kind in _TAIL_KINDS:
        return True
    return kind == "narrative" and _payload(section).get("style") in _TAIL_NARRATIVE_STYLES


def is_provenance_section(section: Any) -> bool:
    """溯源提示段：B1 起打 `_meta.provenance=true`；存量按 markdown 首行是「数据可信度提示」类标题识别。"""
    if _meta(section).get("provenance") is True:
        return True
    md = _payload(section).get("markdown")
    if not isinstance(md, str):
        return False
    head = md.strip().split("\n", 1)[0].strip("*# ")
    return head.startswith(_provenance_headings())


def _provenance_headings() -> tuple[str, ...]:
    """「数据可信度提示」类标题：取 report_chrome_text 里每种支持语言的 provenance_heading（只在那一处维护），外加两个老英文写法。
    review（2026-10-01）：此前手写了中英两种，德 / 法报告里没打 `_meta.provenance` 的存量溯源段认不出、会被当成作者局限段覆盖。"""
    from bowhead_core.language_context import SUPPORTED_LANGUAGES
    from bowhead_core.report_chrome_text import chrome_text

    headings = [chrome_text("provenance_heading", lang).strip("*# ") for lang in SUPPORTED_LANGUAGES]
    return tuple(dict.fromkeys([*filter(None, headings), "Data reliability", "Data confidence", "Provenance"]))


@dataclass(frozen=True)
class DocumentBlock:
    """包住某一段的那份报告（可见段视图）。"""

    h1: Any
    eyebrow: Any | None
    meta: Any | None
    kpi_row: Any | None
    body: list[Any] = field(default_factory=list)
    sources: Any | None = None
    caveats: list[Any] = field(default_factory=list)
    provenance: Any | None = None
    recommendations: Any | None = None
    tail: list[Any] = field(default_factory=list)

    @property
    def tail_start_seq(self) -> int | None:
        return int(getattr(self.tail[0], "seq", 0)) if self.tail else None

    @property
    def section_ids(self) -> set[str]:
        ids = {self.h1.id}
        for s in (self.eyebrow, self.meta, self.kpi_row, self.sources, self.provenance, self.recommendations):
            if s is not None:
                ids.add(s.id)
        ids.update(s.id for s in self.body)
        ids.update(s.id for s in self.caveats)
        return ids


def _visible(sections: Iterable[Any]) -> list[Any]:
    """文档块只看用户看得见的段：折叠的（superseded）与 chat-mode 隐藏的都不算
    （调用方可能传 `include_hidden=True` 的全量——落位要整份序，但块结构不该含隐藏段）。"""
    out = [s for s in sections if not getattr(s, "superseded", False) and not getattr(s, "hidden_for_chat_mode", False)]
    out.sort(key=lambda s: int(getattr(s, "seq", 0) or 0))
    return out


def _trailing_tail(body: Sequence[Any], ignore_ids: frozenset[str] = frozenset()) -> list[Any]:
    """尾部区 = block **末尾连续**的一段 sources/recommendations/caveats-narrative。附录 code/sql 忽略但不打断；
    `ignore_ids`（本轮刚 append、还没落位的段）视为不存在——否则加章后尾部区会被自己「打断」成空。"""
    tail: list[Any] = []
    for s in reversed(body):
        if s.id in ignore_ids:
            continue
        kind = str(getattr(s, "kind", "") or "")
        if kind in _APPENDIX_KINDS:
            continue
        if _is_tail(s):
            tail.append(s)
        else:
            break
    tail.reverse()
    return tail


def _log_if_ambiguous(vis: Sequence[Any], h1_idx: int) -> None:
    """设计 §1 的已知模糊面：同一轮落了多个「文档级」h1（BI 兜底 h1、A2 前无 `_meta.chapter` 的章 h1、
    auto_run 章 h1 chapter=false）⇒ 一份 plan 文档被切成几个小块。按字面判、只留痕。"""
    h1 = vis[h1_idx]
    turn = getattr(h1, "turn_id", None)  # Section uses turn_id, not by_turn_id; the wrong field prevents logging
    if not turn:
        return
    siblings = [s.id for s in vis if s is not h1 and is_document_h1(s) and getattr(s, "turn_id", None) == turn]
    if siblings:
        logger.info("document_block.ambiguous h1=%s turn=%s sibling_h1s=%s", h1.id, turn, siblings)


def _build_block(vis: Sequence[Any], h1_idx: int) -> DocumentBlock:
    h1 = vis[h1_idx]
    _log_if_ambiguous(vis, h1_idx)
    eyebrow = vis[h1_idx - 1] if h1_idx > 0 and str(getattr(vis[h1_idx - 1], "kind", "") or "") == "eyebrow" else None
    # Close the block at the next document-level h1.
    end = len(vis)
    for j in range(h1_idx + 1, len(vis)):
        if is_document_h1(vis[j]):
            end = j
            break
    # The next report's eyebrow immediately precedes the closing h1 (`canonical_order` assigns it to that next h1);
    # it is outside this block (review #1: including it in body interrupts the tail and gives it dual ownership).
    if end < len(vis) and end - 1 > h1_idx and str(getattr(vis[end - 1], "kind", "") or "") == "eyebrow":
        end -= 1
    rest = list(vis[h1_idx + 1 : end])
    meta = kpi_row = None
    chrome_idx: set[int] = set()
    seen_exec = False
    for i, s in enumerate(rest):
        k = str(getattr(s, "kind", "") or "")
        if k in _CHROME_KINDS:
            if k == "meta" and meta is None:
                meta = s
            elif k == "kpi_row" and kpi_row is None:
                kpi_row = s
            chrome_idx.add(i)
            continue
        # `_build_sections_from_payload` emits h1 → meta → narrative(executive) → kpi_row; canonical ordering moves
        # kpi_row before body, but manual reordering (`has_manual_reorder`) suppresses sorting. Allow the chrome run to skip one
        # executive narrative while seeking kpi_row (review #5). That narrative remains body text.
        if k == "narrative" and _payload(s).get("style") == "executive" and not seen_exec and kpi_row is None:
            seen_exec = True
            continue
        break
    body = [s for i, s in enumerate(rest) if i not in chrome_idx]
    tail = _trailing_tail(body)
    sources = next((s for s in body if str(getattr(s, "kind", "") or "") == "sources"), None)
    recommendations = next((s for s in body if is_recommendations_section(s)), None)
    caveats = [s for s in body if str(getattr(s, "kind", "") or "") == "narrative" and _payload(s).get("style") == "caveats"]
    provenance = next((s for s in caveats if is_provenance_section(s)), None)
    return DocumentBlock(
        h1=h1,
        eyebrow=eyebrow,
        meta=meta,
        kpi_row=kpi_row,
        body=body,
        sources=sources,
        caveats=caveats,
        provenance=provenance,
        recommendations=recommendations,
        tail=tail,
    )


def document_block_for(sections: Iterable[Any], anchor_id: str) -> DocumentBlock | None:
    """包住 anchor 段的文档块：anchor 之前最近的文档级 h1 开块，下一个文档级 h1 关块。
    anchor 本身是文档级 h1 也算。找不到 ⇒ None（B1 各项对 None 一律不做）。"""
    vis = _visible(sections)
    pos = next((i for i, s in enumerate(vis) if s.id == anchor_id), None)
    if pos is None:
        return None
    for i in range(pos, -1, -1):
        if is_document_h1(vis[i]):
            return _build_block(vis, i)
    return None


def latest_document_block(sections: Iterable[Any]) -> DocumentBlock | None:
    """画布**最后一个**可见文档块（append 档与 `find_prior_visible_h1` 指的同一份报告：多份可见时是最新那份）。
    批 B2-3 review：原先取第一个——`keep_prior_report` 让新旧两份同时可见后，「再加一节」会落进旧报告。"""
    vis = _visible(sections)
    last = None
    for i, s in enumerate(vis):
        if is_document_h1(s):
            last = i
    return _build_block(vis, last) if last is not None else None


def insert_before_tail(sections: Iterable[Any], block: DocumentBlock, moving_ids: Iterable[str]) -> list[str] | None:
    """把 `moving_ids`（本轮 append 的 body 段）整体移到 block 尾部区之前；返回**整份** ordered_ids
    （含折叠段与 chat-mode 隐藏段：它们原地不动，可见段按新序填回自己原本占的槽位——`reorder_sections` 是
    「传入的排前面 + 未列出者尾随」，只传可见序会把折叠历史/隐藏段搬到当前报告后面，同 `_apply_canonical_order`
    的处置；review #2 + 隐藏段那半）。block 无尾部区、moving 为空或已在位 ⇒ None（no-op）。
    ⚠ `sections` 要传 `list_sections(..., include_hidden=True)`，否则隐藏段不在序里就会被尾随到最后。
    moving 里不在可见集里的 id（隐藏/已折）直接剔掉，不让一个隐藏段把整轮落位废掉（review #7）。"""
    all_secs = sorted(sections, key=lambda s: int(getattr(s, "seq", 0) or 0))

    def _movable(s: Any) -> bool:
        return not getattr(s, "superseded", False) and not getattr(s, "hidden_for_chat_mode", False)

    vis = [s for s in all_secs if _movable(s)]
    ids = [s.id for s in vis]
    moving = [m for m in dict.fromkeys(moving_ids) if m in set(ids)]
    if not moving:
        return None
    moving_set = set(moving)
    tail = _trailing_tail(block.body, frozenset(moving_set))
    if not tail:
        return None
    rest = [i for i in ids if i not in moving_set]
    tail_first = tail[0].id
    if tail_first not in rest:
        return None
    at = rest.index(tail_first)
    new_visible = rest[:at] + [i for i in ids if i in moving_set] + rest[at:]
    if new_visible == ids:
        return None
    filler = iter(new_visible)
    return [next(filler) if _movable(s) else s.id for s in all_secs]


_KPI_BAND_MAX = 8  # Match report_writer_loop._emit_kpi_band's kpi_cells[:8] for revision patches


def build_kpi_cells(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """payload.kpis → kpi_row.cells（收尾器口径 `lbl/val/sub/subTone`，FE `KPICell` 就是它）。
    从 `_emit_kpi_band` 原地抽出，收尾器与 B1-1 chrome patch 共用。"""
    cells: list[dict[str, Any]] = []
    for k in (payload or {}).get("kpis") or []:
        if not (isinstance(k, dict) and k.get("label") and k.get("value") not in (None, "")):
            continue
        cell: dict[str, Any] = {"lbl": str(k["label"])[:60], "val": str(k["value"])[:40]}
        delta = k.get("delta")
        if delta:
            txt = str(delta)[:40]
            if not any(g in txt for g in ("▲", "▼", "↑", "↓")):
                head = txt.lstrip()[:1]
                if head == "+":
                    txt = f"▲ {txt}"
                elif head in ("-", "−"):
                    txt = f"▼ {txt}"
            cell["sub"] = txt
            senti = str(k.get("sentiment") or "").strip().lower()
            if senti == "good":
                cell["subTone"] = "pos"
            elif senti in ("bad", "warning"):
                cell["subTone"] = "neg"
        elif k.get("sub") or k.get("unit"):
            cell["sub"] = str(k.get("sub") or k.get("unit"))[:40]
        cells.append(cell)
    return cells


async def canvas_has_prior_report(canvas_store: Any, session_id: str | None, this_turn_id: str | None) -> bool:
    """`find_prior_visible_h1` 的取数版：任何失败（无 store / 无画布 / 异常）都按「没有」。"""
    if canvas_store is None or not session_id:
        return False
    try:
        cv = await canvas_store.get_canvas_by_session(session_id)
        if cv is None:
            return False
        return find_prior_visible_h1(await canvas_store.list_sections(cv.id), this_turn_id) is not None
    except Exception:
        return False


async def turn_revised_prior_narratives(canvas_store: Any, session_id: str | None, turn_id: str | None) -> list[Any]:
    """本轮就地改过的**上一轮** narrative（按 `canvas_revisions` 账本，与修订收尾器同口径）。
    取数版：flag 关 / 无 store / 无画布 / 异常一律返 []。Lead 末轮「无内容不 compose」闸与准入用它——
    canvas-writer 用 canvas_patch_section 改段的轮 payload 是薄的、本轮没 append 任何段，
    但修订本身就是产出（116 `t_6c1cd1b92dfa`：闸把它拦在 compose 外 ⇒ 修订收尾器整轮不跑）。"""
    if not section_revise_enabled() or canvas_store is None or not session_id or not turn_id:
        return []
    try:
        cv = await canvas_store.get_canvas_by_session(session_id)
        if cv is None:
            return []
        touched = await canvas_store.section_ids_touched_by_turn(turn_id)
        if not touched:
            return []
        return revised_prior_narratives(await canvas_store.list_sections(cv.id), touched_ids=touched, this_turn_id=turn_id)
    except Exception:
        return []


def no_document_chrome(bus: Any) -> bool:
    """本轮是否**不得**新立文档 chrome（h1/eyebrow/meta/KPI 带）：flag 开、Lead 档位是 revise/append、
    且不是 plan 执行轮（plan 文档的封面另有归属）。收尾器、ψ-E eager 卡片路、缺省代笔卡片路三处
    同一口径——review 修：此前只有收尾器读档位，compute 在 revise 档下 write_report 失败/没写时，
    eager 照渲整套 h1+KPI ⇒ 新文档 ⇒ α 折叠把旧报告折掉，正是本 sprint 要修的形态。"""
    if not section_revise_enabled():
        return False
    if getattr(bus, "_d6_is_plan_execution", False):
        return False
    return lead_canvas_mode(bus) in ("append", "revise")


def strip_document_chrome_payload(payload: dict[str, Any] | None, bus: Any = None) -> dict[str, Any]:
    """卡片路在 revise/append 档下的 payload：去掉会渲成 h1 的 ``title``（其余交 chapter_mode 处理：
    eyebrow/meta/建议/局限不渲，章该有的 exec_summary/kpis/卡片/图照落）。revise 档再去 ``kpis`` 与
    ``exec_summary``——修订轮不渲 KPI 带（修订收尾器与亲笔收尾器都不渲）；修订说明里 exec_summary 是
    「一句说清改了什么」的变更说明，卡片路只在 write_report 没写成时才走到，把它渲成正文就是把失败
    说成成功（fix-round review）。剩下的 insights/图/来源按证据落在旧报告后面。"""
    p = dict(payload or {})
    p.pop("title", None)
    if bus is not None and lead_canvas_mode(bus) == "revise":
        p.pop("kpis", None)
        p.pop("exec_summary", None)
    return p


def canvas_mode_context_line(mode: str | None, doc_title: str | None) -> str:
    """Sub-task context 里给 compute 的操作说明（一行）。new_report / None → 空串（现状）。"""
    title = f"《{doc_title}》" if doc_title else ""
    if mode == "revise":
        return (
            f"- **canvas_mode**: revise —— 画布上已有报告{title}，本轮是它的**修订**，不是再写一份："
            "先 `read_report` 拿 outline（要看正文就 `include_full=true` 或 `section_ids=[...]`）；"
            "只改受影响的段——`write_report` 传 `section_id`+`expected_rev`，只碰 outline 里 `editable=true` 的正文段，"
            "未受影响的段一个字不动；正文只有一段就只改那一段。**绝不**不传 section_id 另写一篇（那会在旧正文后面再挂一份）。"
            "改写时保留原文里不受影响的 `[figure:id]` 锚点，不写 [N]；口径变了的图用 `add_chart` 新出、正文引新 id，"
            '有 `remove_chart` 工具时，**被新图取代的上一轮旧图用 `remove_chart(reason="replaced")` 收起**，'
            '用户要求去掉某张图就 `remove_chart(reason="removed")`（正文还引用着它就先改掉那处锚）'
            "——光改文字不会让图消失，图没真的收起就别说已替换/删除。"
            "改数/改口径时先 `read_report(include_full=true)`，**凡提到被改指标的段都要改**（导语、结论、各节），只改一段不算完成。"
            "章标题（outline 里 `kind=h1`、`editable=true` 的段）若写着被改掉的口径（如「2026年1-9月…」），也用 "
            '`write_report(section_id=<它的 id>, expected_rev=…, content="新标题")` 改掉——content 只写标题这一行；报告大标题别这样改，走 payload.title。'
            "payload：`exec_summary` 一句说清改了哪几段；`sources` 照填；**口径（时间/范围/指标定义）变了** ⇒ `title`（需改才填）、"
            "`kpis`（**整条带的全部指标**按新口径给，别只给变了的那个）、`recommendations`/`caveats`（受影响则整组重写）——系统据此就地换标题/KPI 带/建议/局限；"
            "纯措辞修订这些都别填；`insights` 不填；`delivery_surface='report'`。"
        )
    if mode == "append":
        return (
            f"- **canvas_mode**: append —— 在画布报告{title}末尾**新增一节**：`write_report` 不传 section_id，正文第一行写 `## <章名>`，"
            "不写全文导语、不写文档级结论（那两样系统已有）；payload 的 title/kpis 别填（这轮不渲它们）；`delivery_surface='report'`。"
        )
    return ""


def is_prior_turn_section(section: Any, this_turn_id: str | None) -> bool:
    """「上一轮建的段」——outline 的 `prior_turn`/`editable` 与 `_do_update` 的跨轮放行**同一口径**。

    ``Section.turn_id`` 冻结在 append 轮；老库里 owner 早于 turn_id 加列，存在 ``turn_id IS NULL``
    的行——它们的写手必已退出，按上一轮处理。本轮 turn_id 未知（空串/None）时一律 False：
    没有「本轮」这个基准，就不能把任何段判成跨轮而绕开 owner 锁（review P2-3）。
    """
    if not this_turn_id:
        return False
    return (getattr(section, "turn_id", None) or None) != this_turn_id


REVISABLE_KINDS: frozenset[str] = frozenset({"narrative"})
"""跨轮就地改只对**正文段**开放（review 修）：`write_report update` 写的是 ``payload.markdown``，
h1/kpi_row/chart/insight_card 的渲染读的是别的键——patch 上去 ok:true 却什么都看不见，且
``revised_prior_narratives`` 只认 narrative ⇒ 修订收尾器不命中、卡片路再立一份文档。标题/KPI
带的就地改是批 B1（chrome 就地 patch），图用 `add_chart` 重出。"""


def is_chapter_title(section: Any) -> bool:
    """plan 章自己的标题段（h1 + chapter meta）——不是文档大标题（那个由修订收尾器按 payload.title 换）。"""
    return getattr(section, "kind", None) == "h1" and is_eager_chapter(section)


def is_cross_turn_editable(section: Any, this_turn_id: str | None) -> bool:
    """flag 开时上一轮的段能否被本轮写手就地改：上一轮建的 + 正文段或章标题 + 未折叠。
    outline 的 ``editable`` 与 ``_do_update`` 的放行**同一口径**。

    章标题（116 dogfood 1.3.49 `s_18c8b86f3003`）：整份改成 1-6 月后封面标题换了，章标题还是「2026年1-9月…」——
    写手改不了 h1、收尾器只管封面、时间窗也不是数字 token（残留扫描看不见），没有任何一环会动它。
    章标题走 `write_report` 的专用分支（只写 `payload.text`），不进正文的 bake。"""
    return (
        is_prior_turn_section(section, this_turn_id)
        and (getattr(section, "kind", None) in REVISABLE_KINDS or is_chapter_title(section))
        # Document projection's byline (style=byline) is chrome, not body; single-body-section revision must not select it.
        and (getattr(section, "payload", None) or {}).get("style") != "byline"
        and not getattr(section, "superseded", False)
    )


def revised_prior_narratives(sections: Iterable[Any], *, touched_ids: Iterable[str], this_turn_id: str | None) -> list[Any]:
    """本轮 patch 过的**上一轮** narrative（修订轮的判据）。纯函数：调用方给 sections 与 touched 集。"""
    touched = set(touched_ids)
    return [
        s
        for s in sections
        if s.id in touched
        and str(getattr(s, "kind", "") or "") == "narrative"
        and (getattr(s, "turn_id", None) or None) != this_turn_id
        and not getattr(s, "superseded", False)
    ]


def note_revised_section_ids(bus: Any, ids: Iterable[str]) -> None:
    if bus is None:
        return
    with contextlib.suppress(Exception):
        setattr(bus, _REVISED_IDS_ATTR, [str(i) for i in ids])


def revised_section_ids(bus: Any) -> list[str]:
    v = getattr(bus, _REVISED_IDS_ATTR, None) if bus is not None else None
    return [str(i) for i in v] if isinstance(v, list | tuple) else []


# ─── Batch B1-4 / B1-5: append placement + provenance merging (IO helpers shared by both finishers) ───

_NOT_BODY_KINDS: frozenset[str] = frozenset({"h1", "eyebrow", "meta", "kpi_row", "sources", "recommendations", "code_block", "sql_block"})


async def place_appended_before_tail(canvas_store: Any, bus: Any, *, canvas_id: str, turn_id: str | None) -> int:
    """B1-4：把本轮 append 的 **body** 段（正文/图/卡片；不含 chrome、来源、附录、caveats 类 narrative）
    移到画布第一个可见文档块的尾部区之前。返回移动的段数（0 = no-op）。best-effort。"""
    if not turn_id:
        return 0
    try:
        if await canvas_store.has_manual_reorder(canvas_id):
            # User reordered this document → the server must not undo it (same deference as `_apply_canonical_order`);
            # `FEATURE_REPORT_EDIT_SYNC_V2` means database seq is the sole authoritative order, changed by user dragging.
            logger.info("compose_report.append_place_skip turn=%s reason=manual_order", turn_id)
            return 0
        all_secs = await canvas_store.list_sections(canvas_id, include_hidden=True)
        appended = await canvas_store.list_sections_appended_by_turn(turn_id)
        moving = [
            s.id
            for s in appended
            if str(getattr(s, "kind", "") or "") not in _NOT_BODY_KINDS
            and not _is_tail(s)  # Caveats/recommendations projected as narrative are tail, not movable body
            and not getattr(s, "superseded", False)
        ]
        if not moving:
            return 0
        block = latest_document_block(all_secs)
        if block is None:
            return 0
        order = insert_before_tail(all_secs, block, moving)
        if order is None:
            return 0
        # Log the target seq using the same placement rules, ignoring this turn's newly appended sections;
        # reading block.tail directly lets new trailing sections empty it → 116 R8 logged before_seq=None despite moving two sections.
        _tail = _trailing_tail(block.body, frozenset(moving))
        before_seq = int(getattr(_tail[0], "seq", 0)) if _tail else None
        await canvas_store.reorder_sections(canvas_id=canvas_id, ordered_ids=order, by_turn_id=turn_id)
        from bowhead_core.events import OrchestratorEvent

        with contextlib.suppress(Exception):
            await bus.emit(
                OrchestratorEvent(
                    event_type="canvas.reordered",  # type: ignore[arg-type]
                    turn_id=turn_id,
                    payload={"canvas_id": canvas_id, "order": order, "reason": "append_place"},  # FE canvas-sse.ts reads order
                )
            )
        logger.info("compose_report.append_place turn=%s moved=%d before_seq=%s", turn_id, len(moving), before_seq)
        return len(moving)
    except Exception:
        logger.warning("compose_report.append_place_failed turn=%s", turn_id, exc_info=True)
        return 0


async def upsert_provenance_section(
    canvas_store: Any, bus: Any, *, canvas_id: str, turn_id: str | None, anchor_id: str | None, heading: str, lines: list[str]
) -> str | None:
    """B1-5：溯源提示段合并进文档块。块内已有溯源段（`_meta.provenance=true` 或按标题识别）⇒ 按行去重后 patch；
    没有 ⇒ append 新段并打 `_meta.provenance=true`。返回 "patched" / "added" / None。"""
    # Multiple sections with the same alias+values in one turn yield identical rows (review: two "9.9亿（关键指标带）" rows); deduplicate lines first, preserving order.
    lines = list(dict.fromkeys(ln.strip() for ln in lines if isinstance(ln, str) and ln.strip()))
    if not lines:
        return None
    try:
        all_secs = await canvas_store.list_sections(canvas_id)
        block = document_block_for(all_secs, anchor_id) if anchor_id else latest_document_block(all_secs)
        existing = block.provenance if block is not None else None
        if existing is not None:
            old_md = str(_payload(existing).get("markdown") or "")
            # Strip only the list prefix `- `, not lstrip("- "), which also consumes the content's negative sign
            # (`- -26.69% 的口径…` → `26.69% …`), so the same row is treated as new and appended again every turn.
            have = {ln.strip()[2:].strip() for ln in old_md.split("\n") if ln.strip().startswith("- ")}
            new_lines = [ln for ln in lines if ln not in have]
            if not new_lines:
                return "patched"
            md = old_md.rstrip("\n") + "\n" + "\n".join(f"- {ln}" for ln in new_lines)
            stored = await canvas_store.patch_section(
                section_id=existing.id,
                patch={"markdown": md, "_meta": {"provenance": True}},
                expected_rev=getattr(existing, "rev", None),
                by_turn_id=turn_id,
                caller="lead",
                canvas_id=canvas_id,
                skip_owner_check=True,
            )
            if stored is not None:
                await emit_section_patched(bus, turn_id=turn_id, canvas_id=canvas_id, section=stored)
            return "patched"
        sec = await canvas_store.append_section(
            canvas_id=canvas_id,
            kind="narrative",
            payload={"markdown": heading + "\n\n" + "\n".join(f"- {ln}" for ln in lines), "style": "caveats", "_meta": {"provenance": True}},
            by_turn_id=turn_id,
        )
        from bowhead_core.events import OrchestratorEvent

        with contextlib.suppress(Exception):
            rs = sec.to_report_section()
            await bus.emit(
                OrchestratorEvent(  # type: ignore[arg-type]
                    event_type="canvas.section_added",
                    turn_id=turn_id or "",
                    payload={"canvas_id": canvas_id, "section": rs, "seq": getattr(sec, "seq", None)},
                )
            )
        return "added"
    except Exception:
        return None


# ─── Batch B1-1: patch chrome in place ────────────────────────────────────────


def _updated_on_pill(lang: str) -> str:
    from bowhead_core.report_chrome_text import chrome_text

    return chrome_text("updated_on", lang, date=dt.date.today().isoformat())


def _pill_prefix(lang: str) -> str:
    """「更新于 {date}」模板去掉日期后的前缀（zh「更新于 」/ en「Updated 」），按前缀识别那一枚 pill。"""
    from bowhead_core.report_chrome_text import chrome_text

    tpl = chrome_text("updated_on", lang, date="\x00")
    return tpl.split("\x00", 1)[0]


def _find_updated_pill(pills: Sequence[Any], lang: str) -> tuple[int | None, str]:
    """meta 里「更新于 {date}」那一枚 pill 的位置和它的语言。本轮语言优先，其余支持的语言兜底。

    只按本轮语言的前缀找会漏：报告写成时是中文、修订这一轮被判成英文（产品默认已是 en-US，
    没设语言偏好的用户就是这样）⇒ 找不到「Updated 」⇒ `meta:no_updated_pill`，画布上的日期停在旧的。"""
    from bowhead_core.language_context import SUPPORTED_LANGUAGES

    for cand in dict.fromkeys((lang, *SUPPORTED_LANGUAGES)):
        prefix = _pill_prefix(cand)
        idx = next((i for i, pl in enumerate(pills) if isinstance(pl, str) and prefix and pl.startswith(prefix)), None)
        if idx is not None:
            return idx, cand
    return None, lang


async def _patch_and_emit(canvas_store: Any, bus: Any, *, canvas_id: str, turn_id: str | None, section: Any, patch: dict[str, Any]) -> bool:
    stored = await canvas_store.patch_section(
        section_id=section.id,
        patch=patch,
        expected_rev=getattr(section, "rev", None),
        by_turn_id=turn_id,
        caller="lead",
        canvas_id=canvas_id,
        skip_owner_check=True,
    )
    if stored is None:
        return False
    await emit_section_patched(bus, turn_id=turn_id, canvas_id=canvas_id, section=stored)
    return True


async def patch_document_chrome(
    canvas_store: Any,
    bus: Any,
    *,
    canvas_id: str,
    turn_id: str | None,
    block: DocumentBlock,
    payload: dict[str, Any] | None,
    lang: str = "en-US",
    touch_meta: bool = True,
) -> dict[str, list[str]]:
    """B1-1：payload 给了什么就 patch 什么；meta 只换「更新于」那一枚 pill。返回 {patched:[kinds], skipped:[reasons]}。
    所有 patch `caller="lead"` + `skip_owner_check=True`（卡片路文档的 chrome owner=peer_id）。best-effort，逐项独立。"""
    payload = payload or {}
    patched: list[str] = []
    skipped: list[str] = []

    async def _try(kind: str, coro: Any) -> None:
        try:
            if await coro:
                patched.append(kind)
            else:
                skipped.append(f"{kind}:not_found")
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{kind}:{type(exc).__name__}")

    title = str(payload.get("title") or "").strip()
    if title:
        if block.h1 is not None and str(_payload(block.h1).get("text") or "").strip() != title:
            await _try("h1", _patch_and_emit(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section=block.h1, patch={"text": title}))
        else:
            skipped.append("h1:same")
    eyebrow = str(payload.get("eyebrow") or "").strip()
    if eyebrow:
        if block.eyebrow is not None:
            await _try(
                "eyebrow", _patch_and_emit(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section=block.eyebrow, patch={"text": eyebrow})
            )
        else:
            skipped.append("eyebrow:no_section")
    cells = build_kpi_cells(payload)[:_KPI_BAND_MAX]  # Match _emit_kpi_band's cap; revision must not widen the KPI band
    if cells:
        if block.kpi_row is not None:
            old_n = len(_payload(block.kpi_row).get("cells") or [])
            logger.info("compose_report.revise_chrome_kpi turn=%s kpi_cells old=%d new=%d", turn_id, old_n, len(cells))
            await _try(
                "kpi_row", _patch_and_emit(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section=block.kpi_row, patch={"cells": cells})
            )
        else:
            skipped.append("kpi_row:no_section")  # Revision creates no new chrome
    recs = payload.get("recommendations") or []
    if recs:
        if block.recommendations is not None:
            from bowhead_core.canvas_composer import build_recommendation_items

            items = build_recommendation_items(list(recs))
            if items:
                # The payload key is markdown, not items; choose by the existing section shape or no current reports can be patched.
                if str(getattr(block.recommendations, "kind", "") or "") == "narrative":
                    from bowhead_core.canvas_doc_projection import recommendations_markdown

                    patch: dict[str, Any] = {"markdown": recommendations_markdown(items, lang)}
                else:
                    patch = {"items": items}
                await _try(
                    "recommendations",
                    _patch_and_emit(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section=block.recommendations, patch=patch),
                )
        else:
            skipped.append("recommendations:no_section")
    from bowhead_core.analysis_payload import non_provenance_caveats

    cav = [c for c in non_provenance_caveats(payload) if isinstance(c, str) and c.strip()]
    if cav:
        # Never let author caveats overwrite system-generated honesty sections: skip provenance (`_meta.provenance`) and plan missing-leg notices
        # (`_meta.gap`, FIX-2 / 2026-06-25 production incident: a failed leg made the report appear complete).
        target = next((c for c in block.caveats if not is_provenance_section(c) and not is_system_caveat_section(c)), None)
        if target is not None:
            old_md = str(_payload(target).get("markdown") or "")
            head = old_md.split("\n", 1)[0] if old_md.strip().startswith("**") else ""
            # Preserve guard-generated fabrication warnings in old sections during whole-group rewrites; models will not accuse themselves, so deleting these dismantles the gate.
            keep = [
                ln.strip()[2:].strip() for ln in old_md.split("\n") if ln.strip().startswith("- ") and _is_guard_caveat_line(ln.strip()[2:].strip())
            ]
            body = [c.strip() for c in cav] + [k for k in keep if k not in {c.strip() for c in cav}]
            md = (head + "\n\n" if head else "") + "\n".join(f"- {c}" for c in body)
            await _try("caveats", _patch_and_emit(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section=target, patch={"markdown": md}))
        else:
            skipped.append("caveats:no_section")
    if touch_meta and block.meta is not None:
        pills = list(_payload(block.meta).get("pills") or [])
        idx, pill_lang = _find_updated_pill(pills, lang)
        if idx is not None:
            # 按报告里这枚 pill 自己的语言换日期：中文报告在英文轮里修订，仍是「更新于 …」，不混进一枚「Updated …」
            new_pill = _updated_on_pill(pill_lang)
            if pills[idx] != new_pill:
                pills[idx] = new_pill
                await _try(
                    "meta", _patch_and_emit(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section=block.meta, patch={"pills": pills})
                )
            else:
                skipped.append("meta:same_day")
        else:
            skipped.append("meta:no_updated_pill")  # Leave the card path's intent pill unchanged
    logger.info("compose_report.revise_chrome turn=%s patched=%s skipped=%s", turn_id, patched, skipped)
    return {"patched": patched, "skipped": skipped}


async def fold_revised_chapter_kpis(
    canvas_store: Any, bus: Any, *, canvas_id: str, turn_id: str | None, block: DocumentBlock, revised_ids: Iterable[str]
) -> list[str]:
    """B1-1（plan 文档）：payload 带文档级 kpis 时，被修订章的同 owner 同 turn 的章 kpi_row 折叠——
    否则封面 KPI=新口径、章 KPI=旧口径互相矛盾。只折 eager 章（`is_eager_chapter`）的 kpi_row。
    ⚠ 文档级 kpi_row 不存在时**不折**：那样折完整份文档一条 KPI 带都不剩（review #11）。best-effort。"""
    if block.kpi_row is None:
        logger.info("compose_report.revise_fold_chapter_kpis_skip turn=%s reason=no_document_kpi_row", turn_id)
        return []
    revised = set(revised_ids)
    owners: set[tuple[str, str]] = set()
    for s in block.body:
        if s.id in revised and is_eager_chapter(s):
            owners.add((str(getattr(s, "owner", "") or ""), str(getattr(s, "turn_id", "") or "")))
    if not owners:
        return []
    fold = [
        s.id
        for s in block.body
        if str(getattr(s, "kind", "") or "") == "kpi_row"
        and is_eager_chapter(s)
        and (str(getattr(s, "owner", "") or ""), str(getattr(s, "turn_id", "") or "")) in owners
        and not getattr(s, "superseded", False)
    ]
    if not fold:
        return []
    try:
        folded = await _mark_folded_by_revise(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section_ids=fold)
    except Exception:
        logger.warning("compose_report.revise_fold_chapter_kpis_failed turn=%s", turn_id, exc_info=True)
        return []
    logger.info("compose_report.revise_fold_chapter_kpis turn=%s n=%d", turn_id, len(folded))
    return folded


def _chapter_key(section: Any) -> tuple[str, str]:
    """eager 章的身份 = (owner, turn)：plan 每步一个 `sub_ps_*` peer，同一轮落的一章。"""
    return (str(getattr(section, "owner", "") or ""), str(getattr(section, "turn_id", "") or ""))


def chapter_scoped_revision(
    block: DocumentBlock, revised_ids: Iterable[str], *, payload: dict[str, Any] | None = None, this_turn_id: str | None = None
) -> list[tuple[str, str]]:
    """修订只动了 plan 文档里的**一部分章**时，返回被修订章的 key；整份文档改口径或根本不是 plan 章 ⇒ 空列表。

    116 dogfood R12（`s_bae4bff9d319`）：用户说「只把第二章改成 1-6 月，其它章节不动」，收尾器却把
    payload.kpis 当**文档级** KPI 写进封面带（5 格 → 3 格，第一/三章还是全年），又把第二章自己的
    KPI 带折掉——结果封面和其它章互相矛盾、第二章一条 KPI 都没有。章范围的修订，KPI 属于那一章。

    判「部分章」必须排除两种**假的部分**（独立 review 实测）：
    ① 写手想改整份文档、只是没改完（改了章1/章2、章3 还是全年）——payload 带着与文档 h1 **不同的标题**
       就是文档级信号 ⇒ 按整份文档走（封面换新、章3 残留照样被完整性提示点名）；
    ② 分母里有「根本没有可修订正文」的章（只有 h1 + kpi_row）或本轮刚落的章——它们永远进不了 revised，
       算进分母会让任何修订都变成「部分章」。⇒ 分母只数**上一轮落的、含未折叠 narrative 的章**。"""
    title = str((payload or {}).get("title") or "").strip()
    doc_title = str(_payload(block.h1).get("text") or "").strip() if block.h1 is not None else ""
    if title and title != doc_title:
        return []
    revised = set(revised_ids)
    all_chapters: set[tuple[str, str]] = set()
    touched: set[tuple[str, str]] = set()
    for s in block.body:
        if not is_eager_chapter(s) or getattr(s, "superseded", False):
            continue
        key = _chapter_key(s)
        if str(getattr(s, "kind", "") or "") == "narrative" and (not this_turn_id or key[1] != this_turn_id):
            all_chapters.add(key)
        if s.id in revised:
            touched.add(key)
    if not touched or touched >= all_chapters:
        return []
    return sorted(touched)


async def patch_chapter_kpi_row(
    canvas_store: Any, bus: Any, *, canvas_id: str, turn_id: str | None, block: DocumentBlock, chapter: tuple[str, str], payload: dict[str, Any]
) -> bool:
    """章范围修订：payload.kpis 写进**那一章自己的** kpi_row（整条带替换，同 `_emit_kpi_band` 截 8 格）。
    文档封面带与其它章不动。该章没有 kpi_row ⇒ 不新立（修订轮不造 chrome），返回 False。"""
    cells = build_kpi_cells(payload)[:_KPI_BAND_MAX]
    if not cells:
        return False
    target = next(
        (
            s
            for s in block.body
            if str(getattr(s, "kind", "") or "") == "kpi_row"
            and is_eager_chapter(s)
            and _chapter_key(s) == chapter
            and not getattr(s, "superseded", False)
        ),
        None,
    )
    if target is None:
        logger.info("compose_report.revise_chapter_kpi_skip turn=%s chapter=%s reason=no_chapter_kpi_row", turn_id, chapter[0])
        return False
    old_n = len(_payload(target).get("cells") or [])
    ok = await _patch_and_emit(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section=target, patch={"cells": cells})
    logger.info("compose_report.revise_chapter_kpi turn=%s chapter=%s kpi_cells old=%d new=%d ok=%s", turn_id, chapter[0], old_n, len(cells), ok)
    return bool(ok)


async def _mark_folded_by_revise(
    canvas_store: Any,
    bus: Any,
    *,
    canvas_id: str,
    turn_id: str | None,
    section_ids: list[str],
    reason: str | None = None,
    open_turn: bool = False,
) -> list[str]:
    """折叠 + 打 `_meta.folded_by="revise"` + 发事件（B1-2 与折章 KPI 共用）。

    `folded_by` 既写进段 `_meta`（hydrate/导出读得到），又随 `canvas.sections_superseded` 上线
    （live 这一轮 FE 才分得出「已替换的图表」和「较早的报告」）。逐段 patch 后**补发
    `canvas.section_patched`**：FE 的 superseded reducer 不合 `_meta`，只靠事件字段还不够让
    已在内存里的段带上标记。

    ``reason``（B2-2）：`remove_chart` 传 replaced / removed，写进 `_meta.fold_reason`，账单据此如实说「换掉了 / 删掉了」。
    ``open_turn``（B2-2 review）：从工具路径调用时，这一轮可能只有这几帧画布事件——先开轮一帧
    `canvas.turn_streaming`（bus exactly-once），与 `write_report` 跨轮改段同口径。"""
    folded = list(await canvas_store.mark_sections_superseded(section_ids=section_ids, superseded_by_turn=turn_id) or [])
    _meta_patch: dict[str, Any] = {"folded_by": "revise", **({"fold_reason": reason} if reason else {})}
    for sid in folded:
        with contextlib.suppress(Exception):
            stored = await canvas_store.patch_section(
                section_id=sid,
                patch={"_meta": _meta_patch},
                by_turn_id=turn_id,
                caller="lead",
                canvas_id=canvas_id,
                skip_owner_check=True,
            )
            if stored is not None:
                await emit_section_patched(bus, turn_id=turn_id, canvas_id=canvas_id, section=stored, open_turn=open_turn)
    await emit_sections_superseded(bus, canvas_id=canvas_id, turn_id=turn_id, section_ids=folded, folded_by="revise")
    return folded


# ─── Batch B1-7: revision completeness (warn only; no changes) ─────────────────

# 116 dogfood R12: `1-12月` yielded `-12`, mislabeling correct full-year numbers as pre-revision stale values.
# A number must not immediately follow a digit/decimal point (range hyphens are not signs), nor precede date units (月/日/年/号/季度/周).
# ⚠ Negative lookahead must include `\.\d`: otherwise `45.2 日` backtracks to truncated `45` (observed in review), which matches unrelated
# `45家` and is printed verbatim in user-visible provenance text.
_NUM_TOKEN_RE = re.compile(r"(?<![\d.])[-+−]?\d[\d,]*(?:\.\d+)?(?!\.\d|\d|\s*(?:月|日|年|号|季度|周))\s*(?:%|‰|亿元|万元|亿|万|元|次|件|天|小时)?")


# ⚠ Do not accept `.` as a separator (review P1): `2026.5亿元` / `1985.3万元` are numbers, not dates; excluding them blinds stale-value scans.
_DATE_RE = re.compile(r"(?<![\d.])(?:19|20)\d\d[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?(?![\d.])(?!\s*(?:%|‰|亿|万|元|次|件|天|小时))")


def number_tokens(text: str) -> set[str]:
    """带单位的数字 token（去空白、去千分位），过滤掉纯个位数/年份样式。"""
    out: set[str] = set()
    # `2026-09-24` / `2026/6/30` are entire dates; splitting yields fragments like 09/24, written into provenance as stale values (116 1.3.49).
    for m in _NUM_TOKEN_RE.finditer(_DATE_RE.sub(" ", text or "")):
        tok = re.sub(r"\s+", "", m.group(0)).replace(",", "").replace("−", "-")
        digits = re.sub(r"[^0-9]", "", tok)
        if len(digits) < 2:
            continue
        if re.fullmatch(r"(19|20)\d\d", digits) and not any(u in tok for u in ("%", "‰", "元", "亿", "万")):
            continue  # Year
        out.add(tok)
    return out


def rendered_text(section: Any) -> str:
    """渲染面读的字段：narrative.markdown / kpi_row.cells[].val+sub / recommendations.items / h1.text。"""
    kind = str(getattr(section, "kind", "") or "")
    p = _payload(section)
    if kind == "kpi_row":
        parts: list[str] = []
        for c in p.get("cells") or []:
            if isinstance(c, dict):
                parts.extend(str(c.get(k) or "") for k in ("lbl", "val", "sub"))
        return " ".join(parts)
    if kind == "recommendations":  # Recommendations projected as narrative use the Markdown branch below
        return " ".join(str(it.get("action") or it.get("text") or "") for it in (p.get("items") or []) if isinstance(it, dict))
    if kind == "h1":
        return str(p.get("text") or "")
    return str(p.get("markdown") or "")


def stale_values_after_revision(
    block: DocumentBlock,
    revised: Sequence[Any],
    prev_by_id: dict[str, dict[str, Any]],
    *,
    chapter_scope: Iterable[tuple[str, str]] | None = None,
) -> list[tuple[Any, list[str]]]:
    """B1-7：本轮被改掉的旧值（首条 prev 有、现文没有）仍出现在块内其它可见段（正文 / kpi_row / 建议）的渲染字段里 ⇒ 列出。
    只看渲染字段：新值写进 `spec.kpis` 这种不渲染键不算改了。

    ⚠ 两侧都按 **token 集合**比，不做子串匹配：`"40"`（「40 家门店」）会子串命中别处的
    `1,405 万元` / KPI `4,012`，于是给正确的段扣一顶「仍是旧值」的帽子，而这行文案是
    **用户可见的**（写进溯源段）。review 用 §0 自己的例句实证过这个误报。"""
    revised_ids = {s.id for s in revised}
    dropped: set[str] = set()
    for s in revised:
        prev = prev_by_id.get(s.id)
        if not prev:
            continue
        old = number_tokens(str(prev.get("markdown") or ""))
        new = number_tokens(rendered_text(s))
        dropped |= old - new
    if not dropped:
        return []
    # 116 dogfood R12: only chapter 2 of a plan document changed; full-year values like `116.23亿元` in chapters 1/3 were correct (their basis stayed unchanged),
    # yet provenance called them pre-revision stale values. For **chapter-scoped revision**, do not scan other chapters or the cover KPI band.
    # ⚠ Scope comes from the caller (same `chapter_scoped_revision` decision as chrome patching). Inferring it from revised chapters
    # would mistake an unfinished whole-document basis change for chapter scope, hiding residues precisely when a warning is needed (independent review observed this).
    scope = set(chapter_scope or ())
    out: list[tuple[Any, list[str]]] = []
    for s in block.body + ([block.kpi_row] if block.kpi_row is not None else []):
        if s.id in revised_ids or getattr(s, "superseded", False) or is_provenance_section(s):
            continue
        if scope and is_eager_chapter(s) and _chapter_key(s) not in scope:
            continue
        if scope and block.kpi_row is not None and s.id == block.kpi_row.id:
            # Chapter-scoped revision leaves the cover KPI band (whole-document summary) unchanged; do not label its values stale
            continue
        kind = str(getattr(s, "kind", "") or "")
        if kind not in ("narrative", "kpi_row", "recommendations"):
            continue
        present = number_tokens(rendered_text(s))
        hits = sorted(dropped & present)
        if hits:
            out.append((s, hits))
    return out


def kpi_band_will_be_rebuilt(chapter_scope: Sequence[Any], *, bounded: bool) -> bool:
    """payload 给了 kpis 时，修订收尾器真会把它写进某条 KPI 带吗（与 `report_writer_loop` 的 KPI 分支同一口径）：
    整份文档修订 ⇒ 封面带；只改一章 ⇒ 该章的带；改了多章（分不清归哪章）/ 点名了范围却认不出章 ⇒ 不写。"""
    if len(chapter_scope) > 1:
        return False
    return bool(chapter_scope) or not bounded


async def stale_kpi_after_revision(
    canvas_store: Any, *, canvas_id: str | None, turn_id: str | None, payload: dict[str, Any] | None = None, bus: Any = None
) -> list[str]:
    """批 B2-5（拍板 ④：KPI 要跟着口径更新）：本轮改掉的旧值仍留在**这一轮会重建的那条** KPI 带里的有哪些。
    116 R8：正文改成 1-6 月 91.17 亿、KPI 带还是全年 119.94 亿——收尾器只能提示，因为 payload 没给新 KPI。
    收尾器这一轮不会写 KPI（多章 / 点名范围认不出章）⇒ 空：要来了也用不上，只会诱导 peer 按局部口径重算整条封面带（review P1-1/P2）。"""
    hits, scope = await stale_hits_after_revision(canvas_store, canvas_id=canvas_id, turn_id=turn_id, payload=payload)
    if not kpi_band_will_be_rebuilt(scope, bounded=revise_scope_bounded(bus)):
        return []
    return sorted({v for s, vs in hits if str(getattr(s, "kind", "") or "") == "kpi_row" for v in vs})


async def stale_hits_after_revision(
    canvas_store: Any, *, canvas_id: str | None, turn_id: str | None, payload: dict[str, Any] | None = None
) -> tuple[list[tuple[Any, list[str]]], list[tuple[str, str]]]:
    """本轮改掉的旧值仍留在哪些段（正文 / KPI 带 / 建议），外加章范围。与修订收尾器的完整性提示同一判据
    （`stale_values_after_revision`，章范围修订只看该章）。best-effort：取不到任何东西都返回空（闸不因读库失败拦人）。"""
    if canvas_store is None or not canvas_id or not turn_id:
        return [], []
    try:
        secs = await canvas_store.list_sections(canvas_id)
        prev_by_id: dict[str, dict[str, Any]] = {}
        for sid, prev in await canvas_store.list_revisions_by_turn(turn_id):
            prev_by_id.setdefault(sid, prev)
    except Exception:
        logger.warning("revise_kpi.stale_lookup_failed turn=%s", turn_id, exc_info=True)
        return [], []
    revised = revised_prior_narratives(secs, touched_ids=prev_by_id, this_turn_id=turn_id)
    if not revised:
        return [], []
    block = document_block_for(secs, revised[0].id)
    if block is None:
        return [], []
    scope = chapter_scoped_revision(block, [s.id for s in revised], payload=payload, this_turn_id=turn_id)
    return stale_values_after_revision(block, revised, prev_by_id, chapter_scope=scope), scope


# ─── Batch B1-3: RC-8 covers revised sections ──────────────────────────────────


async def rc8_revised_inputs(
    canvas_store: Any, *, session_id: str | None, turn_id: str | None, revised_ids: Iterable[str]
) -> tuple[list[tuple[Any, set[str]]], list[Any]]:
    """终态 RC-8 前**重新拉取**修订段（拿最新 rev，gate 的 patch 走 CAS）并算各自的继承集
    （修订前正文引的 `[figure:id]` 集 = 本轮账本首条 prev）；`extra_kpi_sections` = 修订段所在文档块的
    kpi_row（修订轮被 patch 的 KPI 带不在 appended 里）。best-effort：任何异常 ⇒ 空（gate 退回旧行为）。"""
    ids = [i for i in dict.fromkeys(revised_ids) if i]
    if not ids or not turn_id or canvas_store is None:
        return [], []
    try:
        canvas = await canvas_store.get_canvas_by_session(session_id or "")
        if canvas is None:
            return [], []
        sections = await canvas_store.list_sections(canvas.id)
        by_id = {s.id: s for s in sections}
        prev_by_id: dict[str, dict[str, Any]] = {}
        for sid, prev in await canvas_store.list_revisions_by_turn(turn_id):
            prev_by_id.setdefault(sid, prev)
        revised: list[tuple[Any, set[str]]] = []
        for sid in ids:
            s = by_id.get(sid)
            if s is None or getattr(s, "superseded", False):
                continue
            prev_md = (prev_by_id.get(sid) or {}).get("markdown")
            inherited = set(_FIGURE_REF_RE.findall(prev_md)) if isinstance(prev_md, str) else set()
            revised.append((s, inherited))
        extra: list[Any] = []
        if revised:
            block = document_block_for(sections, revised[0][0].id)
            if block is not None and block.kpi_row is not None:
                extra.append(block.kpi_row)
            if block is not None:
                # Chapter-scoped revision writes new KPI into that chapter's own band, not the cover; include its kpi_row in the cross-face KPI view,
                # or nobody checks the band actually changed this turn (independent review found extra contained only the unchanged cover).
                chapters = {_chapter_key(s) for s, _ in revised if is_eager_chapter(s)}
                extra += [
                    s
                    for s in block.body
                    if str(getattr(s, "kind", "") or "") == "kpi_row"
                    and is_eager_chapter(s)
                    and _chapter_key(s) in chapters
                    and not getattr(s, "superseded", False)
                ]
        return revised, extra
    except Exception as e:
        logger.warning("canvas_revise.rc8_inputs_failed turn=%s err=%s", turn_id, e)
        return [], []


# ─── Batch B1-2: fold old figures only on replacement; fold companion code/sql too ───

_FOLD_FIGURES_ENV = "BOWHEAD_CANVAS_REVISE_FOLD_FIGURES"
# Single source of truth is figure_anchor_layout (core layer, no agents import). Previously this copied a same-shaped set.
_FIGURE_KINDS_LOCAL: frozenset[str] = FIGURE_KINDS


def fold_figures_enabled() -> bool:
    """`BOWHEAD_CANVAS_REVISE_FOLD_FIGURES`——B1-2 收旧图的 kill switch（默认开）。这是 B1 唯一「让内容消失」的动作。"""
    return os.environ.get(_FOLD_FIGURES_ENV, "1").strip().lower() in _TRUE_VALUES


def _figure_artifact_ids(section: Any) -> set[str]:
    p = _payload(section)
    out: set[str] = set()
    spec = p.get("spec") if isinstance(p.get("spec"), dict) else {}
    meta = p.get("meta") if isinstance(p.get("meta"), dict) else {}
    for v in (
        spec.get("data_artifact_id"),
        p.get("data_artifact_id"),
        p.get("computation_artifact_id"),
        meta.get("artifact_id"),
        p.get("artifact_id"),
    ):
        if v:
            out.add(str(v))
    return out


def _card_embedded_refs(section: Any) -> set[str]:
    out: set[str] = set()
    for ev in _payload(section).get("evidence") or []:
        if isinstance(ev, dict):
            for k in ("chart_section_id", "image_section_id", "table_section_id"):
                if ev.get(k):
                    out.add(str(ev[k]))
    return out


def figures_still_referenced(all_sections: Sequence[Any]) -> set[str]:
    """可见正文（`[figure:id]`）与洞察卡（evidence）仍在引用的图 id。收旧图（B1-2）与 `remove_chart`（B2-2）同一口径：
    被引用的图一旦收起，前端仍会在正文里按 id 渲出它（引用解析看全部段），同时又出现在「已收起的图表」里，导出则留一个悬空锚。"""
    still: set[str] = set()
    for s in all_sections:
        if getattr(s, "superseded", False):
            continue
        kind = str(getattr(s, "kind", "") or "")
        if kind == "narrative":
            still |= set(_FIGURE_REF_RE.findall(str(_payload(s).get("markdown") or "")))
        elif kind == "insight_card":
            still |= _card_embedded_refs(s)
    return still


def dropped_figure_ids(
    all_sections: Sequence[Any], revised: Sequence[Any], prev_by_id: dict[str, dict[str, Any]], *, this_turn_id: str | None
) -> list[str]:
    """本轮修订段丢弃、且全画布没有别的可见正文/卡片仍引用的**上一轮**图。纯函数。
    守卫：只有当该段的新引用里含**本轮新图**（换图）时才算丢弃——措辞改写把锚点抄丢不折。"""
    new_figures = {
        s.id
        for s in all_sections
        if str(getattr(s, "kind", "") or "") in _FIGURE_KINDS_LOCAL and (getattr(s, "turn_id", None) or None) == this_turn_id
    }
    if not new_figures:
        return []
    dropped: set[str] = set()
    for s in revised:
        prev = prev_by_id.get(s.id)
        if not prev:
            continue
        old_refs = set(_FIGURE_REF_RE.findall(str(prev.get("markdown") or "")))
        new_refs = set(_FIGURE_REF_RE.findall(str(_payload(s).get("markdown") or "")))
        if new_refs & new_figures:
            dropped |= old_refs - new_refs
    if not dropped:
        return []
    still = figures_still_referenced(all_sections)
    by_id = {s.id: s for s in all_sections if not getattr(s, "superseded", False)}
    return [
        fid
        for fid in sorted(dropped)
        if fid not in still
        and fid in by_id
        and str(getattr(by_id[fid], "kind", "") or "") in _FIGURE_KINDS_LOCAL
        and (getattr(by_id[fid], "turn_id", None) or None) != this_turn_id
    ]


def companion_code_ids(all_sections: Sequence[Any], folded_figure_ids: Iterable[str]) -> list[str]:
    """被折图的伴生 code/sql 块（拍板 §8-2）：`output_artifact_ids` 与被折图的产出物相交，且没有别的**可见**图/表还在用其产出物。"""
    folded = set(folded_figure_ids)
    by_id = {s.id: s for s in all_sections}
    folded_artifacts: set[str] = set()
    for fid in folded:
        if fid in by_id:
            folded_artifacts |= _figure_artifact_ids(by_id[fid])
    if not folded_artifacts:
        return []
    live_artifacts: set[str] = set()
    for s in all_sections:
        if getattr(s, "superseded", False) or s.id in folded:
            continue
        if str(getattr(s, "kind", "") or "") in _FIGURE_KINDS_LOCAL:
            live_artifacts |= _figure_artifact_ids(s)
    out: list[str] = []
    for s in all_sections:
        if getattr(s, "superseded", False) or str(getattr(s, "kind", "") or "") not in _APPENDIX_KINDS:
            continue
        p = _payload(s)
        outputs = {str(x) for x in (p.get("output_artifact_ids") or []) if x} | {
            str(x) for x in (p.get("output_artifacts") or []) if isinstance(x, str)
        }
        if outputs and outputs & folded_artifacts and not (outputs & live_artifacts):
            out.append(s.id)
    return out


async def fold_dropped_figures(
    canvas_store: Any, bus: Any, *, canvas_id: str, turn_id: str | None, revised: Sequence[Any], prev_by_id: dict[str, dict[str, Any]]
) -> list[str]:
    """B1-2 IO：折被丢弃的旧图 + 伴生代码块，打 `_meta.folded_by=revise`，发 `canvas.sections_superseded`。返回折叠的 id。"""
    if not fold_figures_enabled() or not turn_id:
        return []
    try:
        all_secs = await canvas_store.list_sections(canvas_id)
        figs = dropped_figure_ids(all_secs, revised, prev_by_id, this_turn_id=turn_id)
        if not figs:
            return []
        ids = figs + companion_code_ids(all_secs, figs)
        folded = await _mark_folded_by_revise(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section_ids=ids)
        logger.info("compose_report.revise_fold_figures turn=%s n=%d ids=%s", turn_id, len(folded), folded)
        return folded
    except Exception:
        logger.warning("compose_report.revise_fold_figures_failed turn=%s", turn_id, exc_info=True)
        return []


# ─── Batch B2-1: Canvas change ledger for revision turns (facts returned to Lead) ───
#
# 116 live testing (tech-design-b2 §2.3): the finisher had not run when Lead wrote its final answer; it could only repeat compute's claims.
# Claims of deletion, chart replacement, and hiding the other 30 entries all contradicted the database. The server calculates this ledger from current database state,
# taking no model claims; Lead must base descriptions of Canvas actions exclusively on it.

_LEDGER_MAX_ITEMS = 12


def _section_heading(section: Any) -> str:
    """正文段的第一个 markdown 标题；没有标题就取首行前 30 字（给 Lead 认段用，不进用户面）。"""
    p = _payload(section)
    md = str(p.get("markdown") or p.get("text") or "")
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:60]
    first = next((ln.strip() for ln in md.splitlines() if ln.strip()), "")
    return first[:30]


def canvas_changes_ledger(
    all_sections: Sequence[Any], patched_ids: Iterable[str], *, this_turn_id: str | None, pending_fold_ids: Iterable[str] = ()
) -> dict[str, Any]:
    """本轮到此刻为止画布上**实际**发生了什么。纯函数。

    - ``patched``：本轮就地改过的**上一轮**段（``patched_ids`` 来自本轮 patch 账本）；
    - ``added_charts``：本轮新画、仍可见的图；
    - ``removed_charts``：本轮收起的图（``folded_by=revise`` 且 ``superseded_by_turn`` 是本轮），
      外加 ``pending_fold_ids``——收尾器紧接着按 B1-2 规则一定会收的图（锚被本轮新图替换）；
    - ``prior_charts_visible``：上一轮的图、此刻仍可见且收尾器不会收——Lead 据此如实说「旧图仍在」。

    review P1-3：不算 pending 的话，账单说「旧图仍在」、几秒后收尾器又把它收了，是反方向的谎报。
    """
    from bowhead_core.figure_anchor_layout import figure_title

    patched_set = set(patched_ids)
    pending = set(pending_fold_ids)
    patched: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for s in all_sections:
        kind = str(getattr(s, "kind", "") or "")
        sid = str(getattr(s, "id", "") or "")
        own_turn = (getattr(s, "turn_id", None) or None) == this_turn_id
        superseded = bool(getattr(s, "superseded", False))
        if kind in _FIGURE_KINDS_LOCAL:
            item = {"section_id": sid, "title": figure_title(_payload(s), kind)[:80]}
            if superseded:
                if _meta(s).get("folded_by") == "revise" and this_turn_id and getattr(s, "superseded_by_turn", None) == this_turn_id:
                    removed.append({**item, "reason": str(_meta(s).get("fold_reason") or "replaced")})
            elif sid in pending:
                removed.append({**item, "reason": "replaced"})
            elif own_turn and not getattr(s, "hidden_for_chat_mode", False):
                added.append(item)
        elif sid in patched_set and not own_turn and not superseded:
            patched.append({"section_id": sid, "kind": kind, "heading": _section_heading(s)})
    return {
        "patched": patched[:_LEDGER_MAX_ITEMS],
        "added_charts": added[:_LEDGER_MAX_ITEMS],
        "removed_charts": removed[:_LEDGER_MAX_ITEMS],
        "prior_charts_visible": [c for c in prior_visible_charts(all_sections, this_turn_id=this_turn_id) if c["section_id"] not in pending],
    }


def _finisher_will_fold(visible: Sequence[Any], revised: Sequence[Any], *, this_turn_id: str | None, bus: Any) -> bool:
    """修订收尾器（`report_writer_loop._revise_finish_if_applicable` → `fold_dropped_figures`）真会跑到收图那一步吗。
    逐条复现它的闸（review P2）：收图开关、非 plan 执行轮、本轮有被改的上一轮正文、本轮没新开文档、被改段落在某份文档块里。
    任一不满足收尾器就不收——账单若照样记「已收起」，是往「已删除」方向的谎报。"""
    if not fold_figures_enabled() or not revised:
        return False
    if bus is not None and getattr(bus, "_d6_is_plan_execution", False):
        return False
    if turn_opened_new_document([s for s in visible if (getattr(s, "turn_id", None) or None) == this_turn_id]):
        return False
    return document_block_for(visible, revised[0].id) is not None


async def build_canvas_changes(
    canvas_store: Any, *, canvas_id: str | None, turn_id: str | None, bus: Any = None, payload: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """B2-1 IO：读库算账单。取不到（无 store / 无 turn / 读库失败）返回 None——调用方就不挂字段，
    而不是挂一份空账单（空账单会被读成「什么都没改」，那是反方向的谎报）。"""
    if canvas_store is None or not canvas_id or not turn_id:
        return None
    try:
        secs = await canvas_store.list_sections(canvas_id, include_hidden=True)
        prev_by_id: dict[str, dict[str, Any]] = {}
        for sid, prev in await canvas_store.list_revisions_by_turn(turn_id):
            prev_by_id.setdefault(sid, prev)  # First entry = before this turn's changes (same as revision finisher)
    except Exception:
        logger.warning("canvas_changes.build_failed turn=%s canvas=%s", turn_id, canvas_id, exc_info=True)
        return None
    # Reproduce the finisher's B1-2 folding exactly: default visible sections (exclude chat-hidden ones), the same revised criterion, and every gate.
    visible = [s for s in secs if not getattr(s, "hidden_for_chat_mode", False)]
    revised = revised_prior_narratives(visible, touched_ids=prev_by_id, this_turn_id=turn_id)
    pending: list[str] = []
    if _finisher_will_fold(visible, revised, this_turn_id=turn_id, bus=bus):
        pending = dropped_figure_ids(visible, revised, prev_by_id, this_turn_id=turn_id)
    ledger = canvas_changes_ledger(secs, prev_by_id, this_turn_id=turn_id, pending_fold_ids=pending)
    # Batch B2-5: where this turn's replaced values remain (out-of-scope summaries/KPI bands), so Lead can truthfully describe unchanged bases.
    # KPI band: if payload has kpis, the finisher immediately rebuilds the whole band; not stale residue.
    if revised:
        hits, ch_scope = await stale_hits_after_revision(canvas_store, canvas_id=canvas_id, turn_id=turn_id, payload=payload)
        # Exclude residue only with kpis that the finisher will actually write to that band (multiple chapters/unresolved named chapters prevent writing; review P2).
        _kpis_applied = bool((payload or {}).get("kpis")) and kpi_band_will_be_rebuilt(ch_scope, bounded=revise_scope_bounded(bus))
        stale_kpi = sorted({v for s, vs in hits if str(getattr(s, "kind", "") or "") == "kpi_row" for v in vs}) if not _kpis_applied else []
        stale_text = [
            {"section_id": str(s.id), "heading": _section_heading(s), "values": vs[:6]}
            for s, vs in hits
            if str(getattr(s, "kind", "") or "") != "kpi_row"
        ][:_LEDGER_MAX_ITEMS]
        if stale_kpi:
            ledger["stale_kpi"] = stale_kpi[:8]
        if stale_text:
            ledger["stale_elsewhere"] = stale_text
    return ledger


def canvas_changes_is_empty(cc: dict[str, Any] | None) -> bool:
    """本轮对画布什么都没做（没改段、没画图、没收图）。「仍可见的旧图」不算改动。"""
    return not cc or not (cc.get("patched") or cc.get("added_charts") or cc.get("removed_charts"))


CANVAS_CHANGES_DIRECTIVE = (
    "【画布改动以 canvas_changes 为准】本结果的 `canvas_changes` 是服务端按库统计的本轮画布实际改动，不是 peer 的自述。"
    "向用户描述画布上做了什么，只能依据它：`removed_charts` 为空时，不得说图「已替换 / 已删除 / 不再展示」；"
    "`prior_charts_visible` 里的上一轮图此刻仍在画布上——用户要求换掉或删掉的图若还在其中，要如实说它仍在。"
    "`stale_kpi` / `stale_elsewhere` 非空＝这些地方还留着改之前的数（KPI 卡、没在修订范围内的段），要如实告诉用户哪里仍是旧口径。"
)


# ─── Batch B2-2: remove_chart; the chart author decides immediately what to fold ───
#
# 116 live testing: six chart replacements after basis changes folded zero old charts (dropped_figure_ids recognizes only [figure:] anchors, rarely written in reports).
# Asked to delete the monthly-trend chart, compute had no chart-removal tool (canvas_delete_section is bound only to
# canvas-writer and hard-deletes). Folding is soft: superseded + _meta.folded_by=revise; frontend places it in a restorable folded bar.

REMOVE_REASONS: frozenset[str] = frozenset({"replaced", "removed"})


def removable_chart_ids(all_sections: Sequence[Any], requested: Iterable[str], *, this_turn_id: str | None) -> tuple[list[str], list[dict[str, str]]]:
    """把请求的 id 分成「可收」与「拒收（带原因）」。纯函数。

    只收**上一轮**、仍可见的图：本轮刚画的图不该在同一轮被收（那是画错了，重画即可）；
    正文/KPI/来源段不归这个工具管；已收起的不重复收；chat 面隐藏的图用户从没在画布上见过，按不存在处理；
    **正文或洞察卡仍在引用的图不收**（review P1-1，与 B1-2 同口径）——先用 write_report 把锚改掉再收。"""
    by_id = {str(getattr(s, "id", "") or ""): s for s in all_sections}
    # Same as B1-2: only user-visible Canvas body text counts as still referencing a figure; chat-hidden sections do not.
    still = figures_still_referenced([s for s in all_sections if not getattr(s, "hidden_for_chat_mode", False)])
    ok: list[str] = []
    rejected: list[dict[str, str]] = []
    for raw in requested:
        sid = str(raw or "").strip()
        if not sid or sid in ok or any(r["section_id"] == sid for r in rejected):
            continue
        s = by_id.get(sid)
        if s is None or getattr(s, "hidden_for_chat_mode", False):
            rejected.append({"section_id": sid, "reason": "not_found"})
        elif str(getattr(s, "kind", "") or "") not in _FIGURE_KINDS_LOCAL:
            rejected.append({"section_id": sid, "reason": "not_a_chart"})
        elif getattr(s, "superseded", False):
            rejected.append({"section_id": sid, "reason": "already_removed"})
        elif (getattr(s, "turn_id", None) or None) == this_turn_id:
            rejected.append({"section_id": sid, "reason": "drawn_this_turn"})
        elif sid in still:
            rejected.append({"section_id": sid, "reason": "still_referenced"})
        else:
            ok.append(sid)
    return ok, rejected


_REJECT_HINTS = {
    "not_found": "画布上没有这个 id（用 read_report 的 figures 或 add_chart 的提醒里给的 id）",
    "not_a_chart": "不是图表；正文用 write_report 改",
    "already_removed": "已经收起过了",
    "drawn_this_turn": "是本轮刚画的图；画错了就重画，不用收",
    "still_referenced": "正文里还有它的 [figure:id] 锚（或洞察卡在引用它）；先用 write_report 把那处改成新图 id 或删掉，再调本工具",
}


async def remove_charts(
    canvas_store: Any, bus: Any, *, canvas_id: str, turn_id: str | None, section_ids: Iterable[str], reason: str
) -> dict[str, Any]:
    """B2-2 IO：校验 → 收起（连同伴生代码块）→ 返回给模型的结果。"""
    all_secs = await canvas_store.list_sections(canvas_id, include_hidden=True)
    ok, rejected = removable_chart_ids(all_secs, section_ids, this_turn_id=turn_id)
    removed: list[str] = []
    if ok:
        ids = ok + companion_code_ids(all_secs, ok)
        folded = set(
            await _mark_folded_by_revise(canvas_store, bus, canvas_id=canvas_id, turn_id=turn_id, section_ids=ids, reason=reason, open_turn=True)
        )
        removed = [sid for sid in ok if sid in folded]
        # Report already_removed truthfully for figures others folded between validation and folding (mark_sections_superseded returns only rows actually changed here).
        rejected += [{"section_id": sid, "reason": "already_removed"} for sid in ok if sid not in folded]
    logger.info("remove_chart turn=%s reason=%s removed=%s rejected=%s", turn_id, reason, removed, rejected)
    out: dict[str, Any] = {"ok": bool(removed) or not rejected, "removed": removed, "reason": reason}
    if rejected:
        out["rejected"] = [{**r, "hint": _REJECT_HINTS.get(r["reason"], "")} for r in rejected]
    if removed:
        out["note"] = "已收起：这些图进了报告末尾的「已收起的图表」，用户展开可以查看；向用户说明时照实说收起了哪几张。"
    return out


def prior_visible_charts(all_sections: Sequence[Any], *, this_turn_id: str | None) -> list[dict[str, str]]:
    """上一轮画的、此刻仍可见的图（id + 标题）。`add_chart` 修订档的提醒与账单同一口径。"""
    from bowhead_core.figure_anchor_layout import figure_title

    return [
        {"section_id": str(s.id), "title": figure_title(_payload(s), str(s.kind))[:80]}
        for s in all_sections
        if str(getattr(s, "kind", "") or "") in _FIGURE_KINDS_LOCAL
        and not getattr(s, "superseded", False)
        and not getattr(s, "hidden_for_chat_mode", False)
        and (getattr(s, "turn_id", None) or None) != this_turn_id
    ][:_LEDGER_MAX_ITEMS]


def prior_charts_reminder(prior: Sequence[dict[str, str]]) -> str:
    """修订档 `add_chart` 返回里的一句提醒：画图那一刻最知道新图取代了谁。"""
    if not prior:
        return ""
    listed = "；".join(f"{p['title']}（{p['section_id']}）" for p in prior)
    return (
        f"这是修订轮，上一轮的这些图仍在画布上：{listed}。"
        '如果刚画的图取代了其中某张（同一主题换了口径/范围），调 remove_chart(section_ids=[旧图 id], reason="replaced") 收起旧图'
        "（正文还引用着旧图的话，先用 write_report 把那处锚改成新图再收）；没有被取代的就不要动。"
    )
