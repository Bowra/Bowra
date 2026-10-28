"""Canvas composition, chart placement and heading-anchor matching.

Call these shared functions directly; orchestration lives in TurnRunner and Lead.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from bowhead_core.canvas_doc_projection import project_document
from bowhead_core.canvas_rank import canonical_order
from bowhead_core.canvas_store import MACHINE_REORDER_TURN_ID
from bowhead_core.chart.units_enrich import lookup_from_source
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.published_diagnostics import build_chat_canvas_failure
from bowhead_core.report_trust import apply_unverified
from bowhead_core.terminal_llm_invocation import reject_unclassified_direct_mutation
from bowhead_core.types import (
    IntentType,
    ToolInvocation,
    TurnRequest,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Shell sections provide framing without content for users to read.
# A payload containing only these kinds is empty (see the end of `_build_sections_from_payload`).
_SHELL_ONLY_KINDS = frozenset({"h1", "eyebrow", "meta"})

# citation-audit (implementation review C-P2-4): untranslated purely numeric [ref:K] remnants bypassing ν1.5
# must not reach resolve (it would synthesize a garbage source for "3"); strip them first in the Phase-F walk.
_NUMERIC_REF_RE = re.compile(r"\s?\[ref:\d{1,3}\]")


def _heading_norm(s: str) -> str:
    """Normalize heading text for cross-match (lowercase + strip md emphasis / punct / space)."""
    s = str(s or "").lower()
    return re.sub(r"[\*_`~#\s（）()，。、；：:;,.]", "", s)


def _event(turn_id: str, event_type: str, payload: dict[str, Any]) -> OrchestratorEvent:
    return OrchestratorEvent(
        event_type=event_type,  # type: ignore[arg-type]
        turn_id=turn_id,
        payload=payload,
    )


async def _emit_canvas_done(bus: Any, turn_id: str, payload: dict[str, Any]) -> None:
    """ξ2-F3: single helper for all canvas.done emissions. Sets
    bus._canvas_done_emitted flag so lead_orchestrator done-chokepoint fallback
    doesn't double-emit. Idempotent at bus level (flag check) + composer level
    (the 6 historic emit sites; first wins, later no-op).

    Trust (teams trust-fix): every canvas.done carries authoritative report trust so IM channels
    don't re-derive it from the lossy/number-stripped event stream. Computed HERE — the single choke
    for all composer emit sites — so no path (streaming / document / payload / legacy) can ship a
    trust-less card. Best-effort; caller may pre-supply `trust` (then we don't recompute)."""
    try:
        if getattr(bus, "_canvas_done_emitted", False):
            return  # idempotent — composer paths may multi-call
        bus._canvas_done_emitted = True  # type: ignore[attr-defined]
    except Exception:  # noqa: S110
        pass
    if "trust" not in payload:
        store = getattr(bus, "_d6_canvas_store", None)
        if store is not None:
            trust = await _canvas_trust_payload(store, turn_id)
            if trust:
                payload = {**payload, "trust": trust}
    # canvas-comprehensive WP-3 (TD-3): carry turn_id in the payload so the FE
    # onCanvasDone can reveal buffered sections BY TURN (multi-path reveal, F1).
    # Additive; IM mirror trust logic reads other fields, unaffected.
    if "turn_id" not in payload:
        payload = {**payload, "turn_id": turn_id}
    await bus.emit(_event(turn_id, "canvas.done", payload))


async def _canvas_trust_payload(canvas_store: Any, turn_id: str) -> dict[str, Any] | None:
    """Authoritative report-trust for a turn, computed over the sections THIS turn appended (raw
    markers intact, pre number-render strip) — turn-scoped so it matches what the card renders and
    survives the i18n strip AND any dropped/reordered section_added event. IM channels prefer this
    canvas.done value over re-deriving from the lossy event stream. Best-effort: None on failure or
    when the turn appended no sections (→ consumers fall back to their own fold)."""
    try:
        from bowhead_core.report_trust import trust_payload

        sections = await canvas_store.list_sections_appended_by_turn(turn_id)
        if not sections:
            return None
        dtos = [s.to_report_section() for s in sections]
        # citation-audit M1-4 (r3 architecture P1-5): [N] validity requires sources in the same
        # scanned set (gen2-src is self-contained; [N] is not). A cross-turn continuation writes only narrative,
        # without new sources; missing sources in the turn batch excludes every [N], falsely reporting 0%. In that case,
        # include current, nonsuperseded canvas sources in the count.
        if not any(d.get("kind") == "sources" for d in dtos):
            _cv_id = getattr(sections[0], "canvas_id", None)
            if _cv_id:
                for _s in await canvas_store.list_sections(_cv_id):
                    if getattr(_s, "kind", None) == "sources" and not getattr(_s, "superseded", False):
                        dtos.append(_s.to_report_section())
                        break
        return trust_payload(dtos)
    except Exception as exc:
        logger.warning("compose.trust_compute_failed turn=%s err=%s", turn_id, exc)
        return None


async def build_api_chart_refs(
    *,
    canvas_store: Any,
    canvas_id: str,
    composed_sections: list[dict[str, Any]],
    appended_ids: list[str],
) -> list[tuple[int, str]]:
    """#112: 把 add_chart 里传的 `anchor_heading` 翻译成 chart_ref_order 项.

    1. 读 canvas 所有 chart sections，提取 payload.anchor_heading
    2. 扫 composed_sections 找 kind in {h1, section} 的 heading 文本
    3. 归一化文本匹配（去空格 / 小写 / 去 markdown 强调）
    4. 命中 → `(heading_idx + 1, chart_section_id)` —— chart 放到 heading 正下方
    """
    all_sections = await canvas_store.list_sections(canvas_id)
    chart_sections = [s for s in all_sections if s.kind == "chart"]
    if not chart_sections:
        return []

    heading_by_norm: dict[str, str] = {}
    for sec in composed_sections:
        kind = sec.get("kind")
        text = ""
        if kind == "h1":
            text = str(sec.get("text") or "")
        elif kind == "section":
            text = str(sec.get("h2") or "")
        if not text:
            continue
        key = _heading_norm(text)
        if key and key not in heading_by_norm:
            heading_by_norm[key] = str(sec.get("id") or "")

    idx_by_sid = {sid: i for i, sid in enumerate(appended_ids)}

    refs: list[tuple[int, str]] = []
    for chart in chart_sections:
        anchor = (getattr(chart, "payload", None) or {}).get("anchor_heading")
        if not isinstance(anchor, str) or not anchor.strip():
            continue
        key = _heading_norm(anchor)
        composed_sid = heading_by_norm.get(key)
        if not composed_sid:
            continue
        idx = idx_by_sid.get(composed_sid)
        if idx is None:
            continue
        refs.append((idx + 1, chart.id))
    return refs


async def reorder_for_chart_refs(
    *,
    canvas_store: Any,
    canvas_id: str,
    appended_ids: list[str],
    chart_ref_order: list[tuple[int, str]],
    turn_id: str,
    bus: TurnEventBus,
) -> None:
    """#92: 按 placeholder/API 位置重排已 stream 的 chart sections. 未引用的 chart 保到末尾."""
    all_sections = await canvas_store.list_sections(canvas_id)
    chart_sections = [s for s in all_sections if s.kind == "chart"]
    non_chart_ids = [s.id for s in all_sections if s.kind != "chart"]

    referenced = {sid for _, sid in chart_ref_order}
    chart_by_id = {s.id: s for s in chart_sections}

    new_order: list[str] = []
    # Put non-appended non-chart sections from historical turns first, preserving order.
    appended_set = set(appended_ids)
    for sid in non_chart_ids:
        if sid not in appended_set:
            new_order.append(sid)

    inserts_at: dict[int, list[str]] = {}
    for idx, ref_sid in chart_ref_order:
        if ref_sid in chart_by_id:
            inserts_at.setdefault(idx, []).append(ref_sid)

    for i, sid in enumerate(appended_ids):
        if i in inserts_at:
            new_order.extend(inserts_at[i])
        new_order.append(sid)
    new_order.extend(inserts_at.get(len(appended_ids), []))

    trailing_charts = [s.id for s in chart_sections if s.id not in referenced]
    new_order.extend(trailing_charts)

    # A3 (report-edit-audit, authoritative seq): after weaving, apply **canonical presentation order** again.
    # writer-loop emits headers (eyebrow/h1/meta) only during compose, with seq later than peer-loop-persisted
    # code_block / bare charts. Without reordering, Python code leads the report. Preserve relative weave order
    # within each rank (stable sort), retaining [figure:] interleaving and multiwriter chapter order.
    # Use the single canvas_rank source, exactly aligned with frontend preprocessSections.
    section_by_id = {s.id: s for s in all_sections}
    ordered_sections = [section_by_id[sid] for sid in new_order if sid in section_by_id]
    new_order = canonical_order(ordered_sections)

    if new_order == [s.id for s in all_sections]:
        return  # no-op

    await canvas_store.reorder_sections(
        canvas_id=canvas_id,
        ordered_ids=new_order,
        by_turn_id=turn_id or MACHINE_REORDER_TURN_ID,
    )
    await bus.emit(
        _event(
            turn_id,
            "canvas.reordered",
            {"canvas_id": canvas_id, "order": new_order},
        )
    )


# ─────────────────────────────────────────────────────────────
# M137 #142 · compose_canvas_from_payload: structured path.
# ─────────────────────────────────────────────────────────────


def payload_prose_for_language(payload: dict[str, Any]) -> str:
    """从 payload 里挑出**能当语言证据的散文**（多语言批3）。

    只取 h1 / exec_summary / 洞察标题与正文。刻意**不取**：
    - `kpis[].value` / 表格 / 图表规格 —— 数字与列名不是语言证据；
    - 任何代码（沙箱代码块里的英文标识符会把中文报告判成英文）；
    - `sources` / `caveats` 这类 chrome —— 它们**正是要跟随判定结果的东西**，
      拿它们当证据就成了自证（chrome 是英文 ⇒ 判英文 ⇒ chrome 继续英文）。
    """
    # ⚠ Accept **both title keys**: composer uses `report_title`; the writer (peer-authored) path uses `title`.
    #   116 G5 measured payload `report_title` as None with the real value in `title`; accepting only one silently omits input.
    parts: list[str] = [
        str(payload.get("report_title") or payload.get("title") or ""),
        str(payload.get("exec_summary") or ""),
    ]
    for insight in payload.get("insights") or []:
        if isinstance(insight, dict):
            parts.append(str(insight.get("title") or ""))
            parts.append(str(insight.get("thesis") or insight.get("narrative") or ""))
    return " ".join(part for part in parts if part.strip())


# M137 #145: feature flag + routing decision.
import contextlib
import os as _os


def _read_force_payload_flag() -> str:
    """读 env. 三态：off / canary / on.

    WP6 清理: 默认一律 'on'（prod 已稳定 6+ 周；旧 dev 默认 'off' 造成
    dev/prod 行为分叉, legacy narrative 路径在 dev 静默存活）. off/canary
    仅留作显式 env 应急逃生.
    """
    v = (_os.environ.get("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", "") or "").strip().lower()
    if v in ("off", "canary", "on"):
        return v
    return "on"


def _canary_pick(turn_id: str) -> bool:
    """canary 模式按 turn_id hash 分流 50% —— 不需要持久化 / sticky session."""
    import hashlib

    h = hashlib.md5(turn_id.encode("utf-8")).hexdigest()
    return int(h[0], 16) % 2 == 0


def payload_required_for(intent: IntentType, complexity: float, turn_id: str = "") -> bool:
    """M137 #145: 决策是否走 payload 强制路径.

    - off 永远 False
    - on 按 intent + complexity 表决策
    - canary 走表决策 但只对 50% turn 生效（按 turn_id hash 分流）
    """
    flag = _read_force_payload_flag()
    if flag == "off":
        return False
    decision = _decide_by_intent(intent, complexity)
    if not decision:
        return False
    if flag == "canary":
        return _canary_pick(turn_id)
    return True  # on


def _decide_by_intent(intent: IntentType, complexity: float) -> bool:
    """UX-3 (2026-04-29) 收紧"灵敏度":
    - REPORT / CAUSAL: 永远触发 (产报告 / 归因必走 payload-path)
    - COMPARISON: ≥ 0.5 (跨维度对比通常需要 task_plan 拆解)
    - COMPUTE: ≥ 0.5 (从 0.7 降, 中等复杂计算也值得 HITL 校 plan)
    - TREND: ≥ 0.7 (多周期多维度趋势)
    - LOOKUP / MODELING / AMBIGUOUS: 不触发
    """
    if intent == IntentType.REPORT:
        return True
    if intent == IntentType.CAUSAL:
        return True
    if intent == IntentType.COMPUTE and complexity >= 0.5:
        return True
    if intent == IntentType.COMPARISON and complexity >= 0.5:
        return True
    return bool(intent == IntentType.TREND and complexity >= 0.7)


# ν2 v2: intent-based canvas routing removed (intent_wants_canvas shim deleted in
# WP6 cleanup, with zero callers repository-wide). Surface (chat vs canvas) is decided post-hoc
# in lead_orchestrator._compute_response_kind() based on observed tool_invocations.


async def compose_payload_missing_guard(
    *,
    canvas_store: Any,
    request: TurnRequest,
    bus: TurnEventBus,
    intent: IntentType,
    invocations: list[ToolInvocation],
) -> list[str]:
    """M137 #145: 强制 payload 路径但 subagent 没交 → 写一个 guard narrative section.

    不再产 KPI / insight_card / recommendations —— 避免给用户看到一半结构化一半文字流的混合.
    用户看到清晰的"系统未生成结构化报告"提示 + 已经做的 tool 列表，可以重问 / 看 Data tab.
    """
    reject_unclassified_direct_mutation("canvas.compose_payload_missing_guard")
    assert canvas_store is not None
    try:
        canvas = await canvas_store.ensure_canvas(
            session_id=request.session_id,
            workspace_id=request.workspace_id,
            user_id=request.user_id,
        )
        # `TurnEventBus.emit` supplies the whole-turn lock on seeing canvas.opened below (see events.py).
        await bus.emit(
            _event(
                request.turn_id,
                "canvas.opened",
                {"canvas_id": canvas.id, "session_id": canvas.session_id, "title": canvas.title},
            )
        )
        await bus.emit(
            _event(
                request.turn_id,
                "payload_missing_guard",
                {"intent": intent.value, "tool_count": len(invocations), "reason": "force_payload_required_but_subagent_did_not_return"},
            )
        )

        # Prod fix C: user-friendly guard, no longer exposing internal terms like "subagent_return".
        tool_count = len(invocations)
        if tool_count > 0:
            body = (
                "我跑了一些数据查询，但没能把它们组装成一份完整的报告。\n\n"
                "你可以试试：\n"
                "- **换个更聚焦的问法**, 比如指定时间范围 / 指标 / 维度\n"
                "- 在 **Data 标签页** 直接看刚才查到的原始数据\n"
                "- 把问题拆成两到三个小问，一步步来"
            )
        else:
            body = (
                "我没能把这个问题转成可以跑的查询。\n\n"
                "你可以试试：\n"
                '- **换个更具体的问法**, 比如"看 X 月 Y 产品的销售额趋势"\n'
                '- 先问"有哪些维度/指标可以用"摸清数据范围\n'
                "- 把目标说清楚: 是要表、图、还是结论"
            )
        sections = [
            {"kind": "eyebrow", "text": "需要更具体的问法"},
            {"kind": "narrative", "markdown": body, "style": "guard"},
        ]
        composed_kinds: list[str] = []
        for sec in sections:
            kind = sec["kind"]
            payload_for_section = {k: v for k, v in sec.items() if k != "kind"}
            stored = await canvas_store.append_section(
                canvas_id=canvas.id,
                kind=kind,
                payload=payload_for_section,
                by_turn_id=request.turn_id,
            )
            composed_kinds.append(kind)
            await bus.emit(
                _event(
                    request.turn_id,
                    "canvas.section_added",
                    {"canvas_id": canvas.id, "section": stored.to_report_section(), "seq": stored.seq},
                )
            )

        await _emit_canvas_done(bus, request.turn_id, {"canvas_id": canvas.id, "section_count": len(composed_kinds), "errors": [], "source": "guard"})
        return composed_kinds
    except Exception as e:
        logger.exception("compose_payload_missing_guard.failed turn=%s err=%s", request.turn_id, e)
        await _emit_canvas_done(bus, request.turn_id, {"canvas_id": None, "section_count": 0, "errors": [f"guard_failed: {e}"], "source": "guard"})
        return []


# ψ-E r2: per-canvas async lock + dedup state (in-memory; per-process).
# WeakValueDictionary auto-evicts unreferenced locks (SRE P0-5 leak fix).
# Dedup keyed by (turn_id, peer_id, section_kind_ordinal) — guards against
# double-compose of same payload across eager + Lead end-of-turn calls.
# canvas-comprehensive WP-5: env-overridable (ops flexibility) + monkeypatchable
# (read as a module global at call time → tests set it to ~0 to force the timeout path).
_PSI_COMPOSE_LOCK_TIMEOUT_SEC = float(_os.environ.get("BOWHEAD_PSI_COMPOSE_LOCK_TIMEOUT_SEC", "15.0"))


def _get_canvas_lock(canvas_id: str) -> asyncio.Lock:
    """Lazy lock per canvas_id, weak-ref backed for GC."""
    global _PSI_COMPOSE_LOCKS
    try:
        return _PSI_COMPOSE_LOCKS[canvas_id]
    except KeyError:
        lock = asyncio.Lock()
        _PSI_COMPOSE_LOCKS[canvas_id] = lock
        return lock


def _composed_set_for(canvas_id: str) -> set[tuple[str, str, str]]:
    """Per-canvas set of (turn_id, peer_id, kind_ordinal) keys already written.
    Cleared on canvas.done."""
    if canvas_id not in _PSI_COMPOSED_KEYS:
        _PSI_COMPOSED_KEYS[canvas_id] = set()
    return _PSI_COMPOSED_KEYS[canvas_id]


# Module-level state. WeakValueDictionary auto-cleans Locks that no caller
# holds. _PSI_COMPOSED_KEYS retained until canvas.done emits.
import asyncio  # noqa: E402
import weakref  # noqa: E402

_PSI_COMPOSE_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_PSI_COMPOSED_KEYS: dict[str, set[tuple[str, str, str]]] = {}


# ───────────────────────────────────────────────────────────────
# chat-truth B-5.5: composer chart auto-build fallback
# ───────────────────────────────────────────────────────────────
# Compute peers no longer require add_chart (old ξ B5 gate removed). When a peer
# skips charting but payload evidence provides data_artifact_id, composer falls back to
# data-shape heuristics to build a chart section, avoiding caption-only prose for users.

_HINT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "trend": (
        # Chinese
        "趋势",
        "走势",
        "月",
        "季",
        "年",
        "周",
        "日",
        "同比",
        "环比",
        # English
        "trend",
        "monthly",
        "weekly",
        "daily",
        "quarterly",
        "yearly",
        "over time",
        "yoy",
        "mom",
        "qoq",
    ),
    "rank": (
        "排名",
        "前",
        "top",
        "大到小",
        "倒序",
        "榜",
        "ranking",
        "leaderboard",
        "highest",
        "lowest",
    ),
    "share": (
        "占比",
        "份额",
        "构成",
        "分布",
        "share",
        "proportion",
        "breakdown",
        "composition",
        "distribution",
    ),
    "compare": (
        "对比",
        "vs",
        "与",
        "compare",
        "versus",
        "comparison",
    ),
}


def _hint_from_caption(caption: str) -> str | None:
    """Heuristic: pick chart hint from evidence.caption text. None → 让 infer_chart_spec 自动."""
    if not caption:
        return None
    low = caption.lower()
    for hint, kws in _HINT_KEYWORDS.items():
        for kw in kws:
            if kw in low or kw in caption:
                return hint
    return None


def _emit_auto_chart_metric(result: str) -> None:
    """chat-truth B-5.5 metric. result ∈ {built, failed, skipped}.

    Wrapped in try to avoid breaking compose path if metrics infra unavailable.
    """
    try:
        from bowhead_core import metrics as core_metrics

        core_metrics.inc(
            "bowhead_composer_auto_chart_total",
            labels={"result": result},
        )
    except Exception:  # noqa: S110
        pass


def _supports_computation_auto_chart(rows: list[dict[str, Any]], cols: list[Any]) -> bool:
    """Computation evidence is only promoted when the artifact is chart-shaped."""
    if len(rows) < 2:
        return False
    has_measure = any(getattr(col, "type", None) == "numeric" for col in cols)
    has_dimension = any(getattr(col, "type", None) in {"date", "categorical", "geo"} for col in cols)
    return has_measure and has_dimension


async def _try_auto_build_chart_section(
    *,
    canvas_id: str,
    artifact_store: Any,
    canvas_store: Any,
    data_artifact_id: str,
    caption: str,
    user_id: str | None,
    turn_id: str,
    bus: Any,
    require_chart_shape: bool = False,
    units_lookup: Any = None,
    units_locale: str = "",
) -> str | None:
    """Try to build a chart section from a data artifact when peer skipped add_chart.

    Returns new section_id on success, None on any failure (artifact missing, parse
    error, infer fails, store error). Failures degrade to caption-only — never raise.
    """
    if not (artifact_store and canvas_store and data_artifact_id):
        _emit_auto_chart_metric("skipped")
        return None
    try:
        # Ownership check prevents unauthorized access (same #54 pattern as _add_chart).
        art = await artifact_store.get(data_artifact_id, check_created_by=user_id)
        if art is None:
            logger.info(
                "composer.auto_chart_skip turn=%s aid=%s reason=artifact_not_found",
                turn_id,
                data_artifact_id,
            )
            _emit_auto_chart_metric("skipped")
            return None
        raw_bytes = await artifact_store.backend.read(art.path)
        from bowhead_core.canvas_tools import _parse_csv_with_types
        from bowhead_core.chart import infer_chart_spec, spec_to_dict

        rows, cols = _parse_csv_with_types(raw_bytes, type_overrides={})
        if not rows or not cols:
            logger.info(
                "composer.auto_chart_skip turn=%s aid=%s reason=empty_data",
                turn_id,
                data_artifact_id,
            )
            _emit_auto_chart_metric("skipped")
            return None
        if require_chart_shape and not _supports_computation_auto_chart(rows, cols):
            logger.info(
                "composer.auto_chart_skip turn=%s aid=%s reason=insufficient_chart_shape",
                turn_id,
                data_artifact_id,
            )
            _emit_auto_chart_metric("skipped")
            return None
        hint = _hint_from_caption(caption)
        title = str(caption or "数据图表").strip()[:60] or "数据图表"
        # Unit enrichment: this **automatic chart** path and add_chart are separate producers;
        # covering only one still gives incorrect percentage shapes when the model omits add_chart (the original 1% bug).
        from bowhead_core.chart.units_enrich import (
            can_enrich_from_current_datarc_turn,
            enrich_columns_with_units,
            mark_unknown_percent_shape,
            relabel_columns_for_locale,
        )

        if units_lookup is not None and can_enrich_from_current_datarc_turn(art, turn_id):
            enrich_columns_with_units(cols, units_lookup, rows)
            relabel_columns_for_locale(cols, units_lookup, units_locale or "en-US")
        else:
            # ⚠ Missing source metadata is **silent** (`lookup_from_source` returns None if any of three prerequisites fails),
            # and no model is present here to declare shape, leaving all chart columns without metadata.
            # 116 measured zero unit/format/percent_scale declarations across 80 numeric columns on this path; nobody noticed
            # because this code stayed silent. One log line makes zero hits observable.
            logger.info(
                "composer.auto_chart_no_units turn=%s aid=%s — 无源元数据句柄，列将只带裸类型",
                turn_id,
                data_artifact_id,
            )
        # Ratio shape: **whoever changed values declares it; without declarations, do not guess**.
        #
        # This is **system-generated automatic charting**, without a model to declare `column_types`. The step above can enrich
        # only source measures with **exact names** (`利润率` matches; sandbox-created `利润率_环比变化`
        # necessarily misses). With both producers absent, the frontend has only two fallbacks: a narrow name family
        # (`达成率|完成率|增长率|…`, none matching `利润率_环比变化`) and classifying a column with max ≤1
        # as fraction. The latter always misclassifies **percentage-point changes**, interpreting 0.55 points as 55%.
        # 116 `s_a0093acfa9f3`: `利润率_环比变化` -0.2479 rendered as **-24.8%** (actual -0.25pp),
        # while same-family `退款率_环比变化` in the same table (2.95, >1) was classified as points, correctly rendering 3.0%;
        # the same quantity type rendered in two different modes in one table must include an error.
        #
        # Explicitly mark every undeclared numeric column here; frontend then avoids percentage formatting and displays raw values.
        # The cost is raw values even for columns previously **guessed correctly** (`利润率` 0.1744 no longer displays 17.4%).
        # This is intentional: correctness came from coincidentally falling within ≤1, not from an informed declaration.
        mark_unknown_percent_shape(cols)
        spec = infer_chart_spec(
            rows=rows,
            columns=cols,
            hint=hint,
            title=title,
            data_artifact_id=data_artifact_id,
        )
        # Surface the auto-built hint to frontend (subtle UI indicator, aligned with ι sprint auto_charted).
        spec.auto_charted = True
        spec.source_meta = {
            "id": data_artifact_id,
            "tool": "composer_auto_chart",
            "artifact_id": data_artifact_id,
            "turn_id": turn_id,
        }
        stored = await canvas_store.append_section(
            canvas_id=canvas_id,
            kind="chart",
            payload={"spec": spec_to_dict(spec)},
            by_turn_id=turn_id,
        )
        # SSE emission (same synchronous path as _add_chart: streaming + done + legacy section_added).
        try:
            from bowhead_core.canvas_streaming import (
                emit_section_done,
                emit_section_streaming,
            )

            await emit_section_streaming(
                bus,
                turn_id=turn_id,
                section_id=stored.id,
                kind="chart",
            )
            await emit_section_done(
                bus,
                turn_id=turn_id,
                section_id=stored.id,
                payload=stored.to_report_section(),
            )
        except Exception:  # noqa: S110
            pass
        with contextlib.suppress(Exception):
            await bus.emit(
                _event(
                    turn_id,
                    "canvas.section_added",
                    {"canvas_id": canvas_id, "section": stored.to_report_section(), "seq": stored.seq},
                )
            )
        logger.info(
            "composer.auto_chart_built turn=%s aid=%s section_id=%s kind=%s",
            turn_id,
            data_artifact_id,
            stored.id,
            spec.kind,
        )
        _emit_auto_chart_metric("built")
        return stored.id
    except Exception as exc:
        logger.info(
            "composer.auto_chart_failed turn=%s aid=%s err=%s",
            turn_id,
            data_artifact_id,
            exc,
        )
        _emit_auto_chart_metric("failed")
        return None


async def promote_document_chrome(
    *,
    canvas_store: Any,
    bus: Any,
    turn_id: str,
    canvas_id: str,
) -> list[str]:
    """把本轮的**文档级 header chrome** 提到画布最前（plan-report-assembly 批 2-c0）。

    chrome = 本轮写的、**无主**（owner 为空）的 header 段：eyebrow / h1 / meta / kpi_row。
    带主的段是「章」，由槽位序决定位置，不动。

    为什么需要：亲笔路的收尾器（`report_writer_loop._maybe_finish_peer_authored_report`）
    **一次排序都不做**，纯 append ⇒ 封面落在画布最末。
    生产 t_a06cfbe66ec8 的 seq 76-80 就是它的 append 序：eyebrow→h1→meta→kpi_row→caveats。
    （`_apply_canonical_order` 全仓只有一个调用点，在**代笔路** `compose_canvas_via_writer_loop`，
    亲笔路够不着它。）

    只提 header，不动 `sources`/`caveats` —— 它们在文档里本来就该殿后。
    提前后 chrome 的 h1 成为**第一个** h1，于是 `canonical_order` 的分段逻辑自然把它当封面段、
    各章各自成段，两边口径不打架。

    返回新序（空 = 没动）。无变化时不写库不发事件。
    """
    sections = await canvas_store.list_sections(canvas_id)
    if not sections:
        return []
    # Under document projection, writer bylines are style=byline narrative and also chrome; place them after kpi_row
    # (same KPI band -> byline + introduction order as the composer path).
    order = {k: i for i, k in enumerate(("eyebrow", "h1", "meta", "kpi_row", "narrative:byline"))}

    def _chrome_key(sec: Any) -> str:
        kind = str(getattr(sec, "kind", ""))
        if kind == "narrative" and (getattr(sec, "payload", None) or {}).get("style") == "byline":
            return "narrative:byline"
        return kind

    def _is_chrome(sec: Any) -> bool:
        return (
            _chrome_key(sec) in order
            and not str(getattr(sec, "owner", "") or "")
            and str(getattr(sec, "turn_id", "") or "") == turn_id
            and not getattr(sec, "superseded", False)
        )

    chrome = [s for s in sections if _is_chrome(s)]
    if not chrome:
        return []
    chrome.sort(key=lambda s: (order[_chrome_key(s)], s.seq))
    chrome_ids = [s.id for s in chrome]
    rest = [s.id for s in sections if s.id not in set(chrome_ids)]
    new_order = chrome_ids + rest
    if new_order == [s.id for s in sections]:
        return new_order
    await canvas_store.reorder_sections(canvas_id=canvas_id, ordered_ids=new_order, by_turn_id=turn_id or MACHINE_REORDER_TURN_ID)
    await bus.emit(_event(turn_id, "canvas.reordered", {"canvas_id": canvas_id, "order": new_order, "reason": "document_chrome_lead"}))
    return new_order


def build_recommendation_items(recs: list[Any]) -> list[dict[str, Any]]:
    """recommendations[] → section items（**单源**：确定性 composer 与亲笔路收尾器共用）。

    排序 = P0/P1/P2 主序、同优先级内保持传入序（稳定排序）——plan-report-assembly
    决议 #1 的口径：确定性、零 LLM、可解释（「为什么这条在前」＝它是 P0 / 它来自第几章）。

    批 2-b0 抽出来的原因：亲笔路收尾器（`report_writer_loop`）此前**根本不渲**
    recommendations，，而只要有一条腿调过 `write_report` 末轮就走那条路 ⇒
    「把建议收进文档级池」会一条都渲不出来（self-review R3 的 P1）。
    两处各写一份排序逻辑迟早分叉，所以收敛成这一个函数。
    """
    prio_order = {"P0": 0, "P1": 1, "P2": 2}
    items: list[dict[str, Any]] = []
    for r in sorted(recs, key=lambda r: prio_order.get((r or {}).get("priority", "P2"), 99)):
        entry: dict[str, Any] = {
            "priority": r.get("priority"),
            "action": str(r.get("action") or ""),
        }
        for k in ("owner", "timeline", "expected_impact", "rationale"):
            v = r.get(k)
            if v not in (None, ""):
                entry[k] = str(v)
        items.append(entry)
    return items


def spawn_order_slots(bus: Any) -> dict[str, int]:
    """非 plan 多腿轮的槽位号 = **spawn 序**（plan-report-assembly 批 1-e / tech-design §2.7）。

    Lead 的派发顺序是它自己定的、有意义的顺序，比「谁先交稿」强得多。
    `bus._spawn_order` 由 `subagent.py` 在铸出 subagent_id 时按序追加（turn 作用域，
    与 `_psi_compose_done` / `_d6_session_id` 同款 bus 属性惯例）。

    ⚠ `agent_spawned` 是**多态**事件（同一 subagent_id 完成时会再发一次），所以记序与
    本函数都按**首次出现**去重。

    只有 >=2 条腿才返回非空 —— 单腿轮不需要槽位序, 调用方据此整段跳过（生产 533 个
    非 plan 多 peer 轮里 89% 只有 2 条腿, h1>=2 的仅 3 个, 这条闸让绝大多数轮次零影响）。
    """
    raw = getattr(bus, "_spawn_order", None)
    if not isinstance(raw, list) or len(raw) < 2:
        return {}
    slots: dict[str, int] = {}
    for sid in raw:
        key = str(sid or "")
        if key and key not in slots:
            slots[key] = len(slots)
    return slots if len(slots) >= 2 else {}


async def supersede_stale_owner_sections(
    *,
    canvas_store: Any,
    canvas_id: str,
    owner: str,
    keep_turn_id: str,
) -> list[str]:
    """重跑某一步时，**只**折这一步上一轮留下的段（plan-report-assembly 批 1-c2 / 决议 #3）。

    为什么不能靠现有折叠：α 折叠（`lead_orchestrator._alpha_fold_prior_blocks`）的粒度是
    **整个前序 turn**（canvas-update-turn 批 A1 后按 turn_id 判，且对 plan 执行轮直接跳过）。
    对 plan 重试来说粒度是错的：重跑第 4 步不能把另外 7 章一并折掉，只能按 owner 折本步。

    按 owner 折才等于「重跑这一步」的语义。★ 这个修法**只有在 stamp 了 owner 之后
    才表达得出来**（批 1-a），所以它是批 1 的副产品而不是额外成本。

    调用时机 = 新段已经落库之后（compose 完），于是不存在「新旧都没有」的窗口。
    返回被折的 section id 列表；无旧段则不写库。
    """
    sections = await canvas_store.list_sections(canvas_id)
    stale = [
        str(getattr(s, "id", ""))
        for s in sections
        if str(getattr(s, "owner", "") or "") == owner
        and str(getattr(s, "turn_id", "") or "") != keep_turn_id
        and not getattr(s, "superseded", False)
    ]
    if not stale:
        return []
    await canvas_store.mark_sections_superseded(section_ids=stale, superseded_by_turn=keep_turn_id)
    return stale


async def reorder_canvas_by_slots(
    *,
    canvas_store: Any,
    bus: Any,
    turn_id: str,
    canvas_id: str,
    slot_of_owner: dict[str, int],
) -> list[str]:
    """按**槽位序**重排画布上带主的段（plan-report-assembly 批 1-c 原语）。

    `slot_of_owner` = {section.owner: 槽位号}。两条路共用同一个原语，只是槽位号来源不同：

    - plan 执行轮：槽位号 = `plan_steps.seq`（真章序），owner = ``"sub_" + step.id``
    - 非 plan 多腿轮：槽位号 = spawn 序（Lead 派发顺序）

    为什么需要它：`compose_canvas_from_payload` 的 `promote_to_top` 是「谁后交谁在前」，
    多腿轮下等于**完成倒序**（discovery C3）。而 composer 结构上不知道 plan，
    所以排序的权威放在知道顺序的地方，composer 只提供原语（tech-design §2.1 岔路 A）。

    只列出**带主**的段；`reorder_sections` 的语义是「最终序 = 传入且存在的 id 按给定序
    + 未列出的按原相对序」，于是流式 chart / code_block / 别轮的段自动尾随，不必枚举。

    返回排好序的 owned section id 列表（空 = 没排，调用方据此判断）。
    已是该序时不写库也不发事件（no-op 守卫，同 A3 口径）。
    """
    if not slot_of_owner:
        return []
    sections = await canvas_store.list_sections(canvas_id)
    owned = [s for s in sections if str(getattr(s, "owner", "") or "") in slot_of_owner]
    if not owned:
        return []
    # Folded old chapters do not participate in slot order, avoiding duplicate slots after retries (batch 1-c2).
    owned = [s for s in owned if not getattr(s, "superseded", False)]
    if not owned:
        return []
    # Within a slot, preserve persisted relative order by seq (composer determines chapter order; do not disrupt it).
    ordered = [s.id for s in sorted(owned, key=lambda s: (slot_of_owner[str(s.owner)], s.seq))]
    # ★G1 (116 device t_c9853b490565): the no-op gate **cannot compare only the relative order of owned sections**.
    # `reorder_sections` means listed sections first + unlisted sections trailing in original relative order, making unowned sections'
    # absolute positions part of the result. Production shape: after early chapters, canvas = [ch0..ch5][charts/code…],
    # then ch6 appends after the charts. Owned sections still appear as ch0..ch6 in slot order,
    # so the old `ordered == [s.id for s in owned]` gate short-circuits, trapping charts
    # between ch5 and ch6 forever. Measurement: eight steps produced only four reorder writes; steps 3/6/7 were swallowed.
    # Correct criterion: after applying this intended order, would the **visible arrangement** remain identical?
    # ⚠ Use visible order (`list_sections` filters `hidden_for_chat_mode` by default), not all sections:
    # `reorder_sections` rebuilds seq over all sections; hidden-row positions cannot be predicted here.
    # Hidden rows are absent from the canvas anyway, so skipping writes affecting only them changes nothing visible.
    visible_ids = [s.id for s in sections]
    _owned_ids = set(ordered)
    predicted = ordered + [sid for sid in visible_ids if sid not in _owned_ids]
    if predicted == visible_ids:
        return ordered  # Visible arrangement already matches the final state -> no database write or event.
    await canvas_store.reorder_sections(canvas_id=canvas_id, ordered_ids=ordered, by_turn_id=turn_id or MACHINE_REORDER_TURN_ID)
    # canvas.reordered is an existing event type with a changed reason. Only new **event types** require updating
    # the EventSource allowlist; adding/changing payload keys does not.
    await bus.emit(_event(turn_id, "canvas.reordered", {"canvas_id": canvas_id, "order": ordered, "reason": "slot_order"}))
    return ordered


async def compose_canvas_from_payload(
    *,
    canvas_store: Any,
    request: TurnRequest,
    bus: TurnEventBus,
    payload: dict[str, Any],
    intent: IntentType | None = None,
    tool_invocations: list[Any] | None = None,
    peer_id: str | None = None,  # ψ-E r2: peer scope for idempotency key
    emit_lifecycle: bool = True,  # ψ-E r2: False for eager peer call (skip opened/done)
    artifact_store: Any = None,  # chat-truth B-5.5: chart auto-build fallback
    chapter_mode: bool = False,  # Batch 2-a: this payload is one document chapter; leg-level chrome belongs in the document-level pool.
) -> list[str]:
    """从 ``AnalysisPayload`` dict 直接铺 canvas（不调 LLM narrator）.

    与老 ``compose_canvas`` 的差异：
      - **不**消费 ``ToolInvocation`` 列表 —— payload 自带 KPIs / insights / recs
      - **不**生成 ``data_table`` / ``sql_block`` —— raw 数据归 Data tab（M2 解耦后彻底走开）
      - chart sections 由 subagent 在 ``add_chart`` 时已写入；本函数**不重建**，
        而是 ``insight_card`` section 通过 ``chart_section_id`` 字段引用，前端按 id 嵌入
      - 段顺序固定：``eyebrow → h1 → meta → kpi_row → insight_card×N → recommendations →
        narrative(caveats) → sources``

    返新写入 section 的 kinds 列表（不含已存在的 chart sections）.
    """
    reject_unclassified_direct_mutation("canvas.compose_payload")
    assert canvas_store is not None

    # chat-v4 W4-fix CTO P0-1 — _psi_skip_final_compose_only reader.
    # When Lead has emitted `lead.final.payload` (V4 path), atomic V4-or-V1
    # invariant requires the narrative-style final compose to NOT also run
    # (would duplicate the V4 payload content as legacy narrative sections).
    # Scope: ONLY the narrative compose path is skipped here. Canvas section
    # emit via `add_chart` / `canvas_add_insight` from peer subagents
    # is independent (those are direct canvas_store writes, not composer).
    # The W4.1 emit-site sets this flag immediately after lead.final.payload.
    try:
        if getattr(bus, "_psi_skip_final_compose_only", False):
            logger.info(
                "psi.compose_skip_v4_committed turn=%s — _psi_skip_final_compose_only set, "
                "skip narrative compose (v4 path), canvas section emit unaffected",
                getattr(request, "turn_id", "?"),
            )
            return []
    except Exception:  # noqa: S110
        pass
    # Only successful eager rendering sets the completion marker. A mid-render
    # exception can still leave partial sections, so the final entry checks for eager
    # nonchapter sections from this turn to avoid duplicating them under Lead's key.
    # Chapters alone must not block the final pass: plan turns may need final document
    # chrome even when their steps failed.
    if emit_lifecycle and not getattr(bus, "_psi_compose_done", False):
        try:
            from bowhead_core.canvas_revise import is_eager_chapter, is_eager_composed

            _prev = await canvas_store.list_sections_appended_by_turn(request.turn_id)
            _partial = [s for s in _prev if is_eager_composed(s) and not is_eager_chapter(s)]
            if _partial:
                logger.info(
                    "psi.compose_skip_partial_eager turn=%s — eager 已落 %d 段（可能中途失败），末轮不再渲整套",
                    getattr(request, "turn_id", "?"),
                    len(_partial),
                )
                return []
        except Exception:  # noqa: S110 — Probe failure continues composition, falling back to previous behavior.
            pass

    # ν2 v2: composer soft-guard removed. response_kind is decided post-hoc at
    # done event emit time in lead_orchestrator; if compute peer payload made it
    # here, it IS a canvas turn — compose normally.

    try:
        canvas = await canvas_store.ensure_canvas(
            session_id=request.session_id,
            workspace_id=request.workspace_id,
            user_id=request.user_id,
        )

        # ψ-E r2: serialize concurrent compose calls on same canvas (SRE P0-2).
        # asyncio.wait_for guards against compose hangs (e.g. DB stall) that
        # would otherwise block all subsequent peer returns on this canvas.
        _psi_lock = _get_canvas_lock(canvas.id)
        try:
            await asyncio.wait_for(_psi_lock.acquire(), timeout=_PSI_COMPOSE_LOCK_TIMEOUT_SEC)
        except TimeoutError:
            logger.warning(
                "psi.compose_lock_timeout canvas=%s turn=%s — skip this compose",
                canvas.id,
                request.turn_id,
            )
            # canvas-comprehensive WP-5 (FR-1.6 / B7): surface an explicit failure
            # instead of a silent empty return that leaves "narrative says report,
            # canvas blank, no explanation". ONLY on the Lead-end compose
            # (emit_lifecycle=True) — an eager-peer timeout (emit_lifecycle=False) is
            # retried by the subsequent Lead compose, so emitting there = false alarm.
            # FE canvas_op_failed handler (canvas-sse) → inline pill; terminal reconcile
            # clears it if the report actually lands (WP-2 banner self-heal).
            if emit_lifecycle:
                with contextlib.suppress(Exception):
                    canvas_failure = build_chat_canvas_failure(
                        reason="lock_timeout",
                        turn_id=request.turn_id,
                    )
                    await bus.emit(
                        _event(
                            request.turn_id,
                            "canvas_op_failed",
                            {
                                "op": "compose",
                                "reason_code": canvas_failure.reason_code,
                                "turn_id": request.turn_id,
                                "diagnostics": canvas_failure.diagnostics,
                            },
                        )
                    )
                    logger.warning(
                        "canvas.compose_public_failure turn=%s occurrence_id=%s reason_code=%s",
                        request.turn_id,
                        canvas_failure.occurrence_id,
                        canvas_failure.reason_code,
                    )
            return []
        _psi_lock_held = True
        # NOTE: lock release happens via the outer try's `finally` block at
        # function end. All early-return paths below are inside the outer try
        # and will hit that finally.

        # P3.5 + ψ-E r2: lifecycle events only on first (Lead end) call.
        # Eager peer calls (emit_lifecycle=False) skip canvas.opened / canvas.done
        # — those fire exactly once when Lead's end-of-turn compose wraps up.
        #
        # ⚠ `canvas.turn_streaming` **no longer** depends on this gate: it signals this turn producing canvas content,
        # which eager peers also do (frontend currentTurnId should already be set then). Depending on emit_lifecycle
        # was one cause of D2/D3 doing no useful work. `TurnEventBus.emit` now supplies it
        # before this turn's first canvas-content event, exactly once.
        if emit_lifecycle:
            await bus.emit(
                _event(
                    request.turn_id,
                    "canvas.opened",
                    {"canvas_id": canvas.id, "session_id": canvas.session_id, "title": canvas.title},
                )
            )

        # Multilingual batch 3 (C2 consistency): **before** constructing chrome, pin this turn's language from actual model-authored prose.
        #
        # 116 G2: English setting + Chinese question yielded Chinese h1/KPI/insights/recommendations but English meta pills,
        # source labels and caveat headings: model content followed the question while deterministic content followed settings.
        # Strong model anchoring (batch 2) can still be ignored, so add a deterministic fallback: **chrome follows prose**.
        # The criterion merely selects strings from a closed Chinese/English locale table; it draws no semantic conclusions.
        # Misclassification makes chrome follow the prose, without numeric or measure-definition errors.
        # ⚠ Feed only **prose** (h1 / exec_summary / insight text): English code-block identifiers are not language evidence,
        # nor are BI entity values (G4 English prose validly includes `北京市`). The underlying criterion already weights
        # mixed content; excluding code provides a second safeguard.
        from bowhead_core.report_chrome_text import chrome_lang as _chrome_lang

        with contextlib.suppress(Exception):
            from bowhead_core.effective_language import (
                carrier_language,
                language_decision_v2_enabled,
                pin_turn_output_language,
            )

            if language_decision_v2_enabled():
                _observed = carrier_language(payload_prose_for_language(payload))
                if _observed:
                    pin_turn_output_language(bus, _observed)

        sections_to_emit = _build_sections_from_payload(
            payload,
            intent,
            tool_invocations=tool_invocations,
            lang=_chrome_lang(getattr(request, "language_context", None)),
            chapter_mode=chapter_mode,
        )
        # chat-truth L-BE: data-hash dedup of data_table / chart / kpi_row
        # sections to fix walkthrough Session 7 (4 identical 36-row tables).
        sections_to_emit = dedup_sections_by_data_hash(sections_to_emit)

        # Sprint µ + chat-truth B-5.5 — chart_section_id validation + auto-build fallback.
        # Flow per evidence with kind="chart":
        #   1. chart_section_id valid in canvas → keep as-is (peer called add_chart)
        #   2. chart_section_id missing/invalid BUT data_artifact_id present + artifact_store
        #      available → composer auto-builds chart section from data shape (B-5.5)
        #   3. else -> drop chart_section_id (caption-only fallback, silently hidden by frontend µ-N1b).
        existing_section_ids: set[str] = set()
        try:
            try:
                _existing = await canvas_store.list_sections(canvas.id)
                existing_section_ids = {getattr(s, "id", "") for s in _existing}
            except Exception:  # noqa: S110
                pass
            # B-5.5 P1-4: idempotency cache; repeated refs to the same data_artifact_id do not rebuild charts.
            _auto_built_cache: dict[str, str] = {}
            # Unit-enrichment data-source handle (turn-scoped, fetched once). Missing sources yield None,
            # and automatic charts proceed without units; enrichment must never introduce a failure point.
            _units_locale = str(getattr(getattr(request, "language_context", None), "output_language", "") or "en-US")
            _units_lookup = lookup_from_source(
                None if getattr(request, "datarc_unavailable", False) else getattr(request, "datarc_client", None),
                str(getattr(request, "active_datarc_project", "") or ""),
            )
            for sec in sections_to_emit:
                if sec.get("kind") != "insight_card":
                    continue
                evidence = sec.get("evidence")
                if not isinstance(evidence, list):
                    continue
                kept = []
                for ev in evidence:
                    if not isinstance(ev, dict):
                        kept.append(ev)
                        continue
                    if ev.get("kind") == "chart":
                        cs_id = ev.get("chart_section_id")
                        cs_valid = bool(cs_id) and cs_id in existing_section_ids
                        if not cs_valid:
                            # B-5.5: try auto-build from data_artifact_id before giving up
                            data_aid = ev.get("data_artifact_id") or ev.get("computation_artifact_id")
                            caption = str(ev.get("caption") or "").strip()
                            if data_aid and artifact_store is not None:
                                # P1-4: cache lookup; reuse when several insights reference the same artifact.
                                data_artifact_id = str(data_aid)
                                cache_key = f"chart:{data_artifact_id}"
                                new_sid = _auto_built_cache.get(cache_key)
                                if new_sid is None:
                                    new_sid = await _try_auto_build_chart_section(
                                        canvas_id=canvas.id,
                                        artifact_store=artifact_store,
                                        canvas_store=canvas_store,
                                        data_artifact_id=data_artifact_id,
                                        caption=caption,
                                        user_id=request.user_id,
                                        turn_id=request.turn_id,
                                        bus=bus,
                                        units_lookup=_units_lookup,
                                        units_locale=_units_locale,
                                    )
                                    if new_sid:
                                        _auto_built_cache[cache_key] = new_sid
                                if new_sid:
                                    ev = {**ev, "chart_section_id": new_sid}
                                    existing_section_ids.add(new_sid)
                                else:
                                    if cs_id:
                                        logger.warning(
                                            "compose_payload.chart_section_id_invalid turn=%s ref=%s aid=%s — auto-build failed, caption-only",
                                            request.turn_id,
                                            cs_id,
                                            data_aid,
                                        )
                                    ev = {**ev, "chart_section_id": None}
                            else:
                                if cs_id:
                                    logger.warning(
                                        "compose_payload.chart_section_id_invalid turn=%s ref=%s — no data_artifact_id for auto-build, caption-only",
                                        request.turn_id,
                                        cs_id,
                                    )
                                ev = {**ev, "chart_section_id": None}
                    kept.append(ev)
                    if ev.get("kind") == "computation":
                        data_aid = ev.get("computation_artifact_id")
                        caption = str(ev.get("caption") or "").strip()
                        if data_aid and artifact_store is not None:
                            data_artifact_id = str(data_aid)
                            cache_key = f"computation:{data_artifact_id}:{caption}"
                            new_sid = _auto_built_cache.get(cache_key)
                            if new_sid is None:
                                new_sid = await _try_auto_build_chart_section(
                                    canvas_id=canvas.id,
                                    artifact_store=artifact_store,
                                    canvas_store=canvas_store,
                                    data_artifact_id=data_artifact_id,
                                    caption=caption,
                                    user_id=request.user_id,
                                    turn_id=request.turn_id,
                                    bus=bus,
                                    require_chart_shape=True,
                                    units_lookup=_units_lookup,
                                    units_locale=_units_locale,
                                )
                                if new_sid:
                                    _auto_built_cache[cache_key] = new_sid
                            if new_sid:
                                kept[-1] = {**ev, "kind": "chart", "chart_section_id": new_sid}
                                existing_section_ids.add(new_sid)
                sec["evidence"] = kept
        except Exception as _vex:
            logger.warning(
                "compose_payload.chart_validate_failed turn=%s err=%s — sections emit unfiltered",
                request.turn_id,
                _vex,
            )

        # Card skeleton -> document. Deliberately place it here: chart validation/automatic charting above has converted
        # evidence chart_section_id into real IDs usable as [figure:] anchors, while Phase-F below
        # binds [N] by the `markdown` key, equally covering document-shaped narrative.
        # All three finalization paths pass through this function, so attach it only here. Design and side-by-side comparison:
        # docs/sprints/canvas-elements-2026-09-04/doc-projection-spike.md
        _before_kinds = [str(x.get("kind")) for x in sections_to_emit if isinstance(x, dict)]
        _doc_lang = _chrome_lang(getattr(request, "language_context", None))
        # Writer header meta (updated time / N data systems / Bowhead-generated) uses the same logic,
        # folded into an italic introduction first line: both paths share report attribution without pills.
        try:
            from bowhead_core.report_writer_loop import _report_meta_pills

            _meta_pills = _report_meta_pills(payload, tool_invocations, _doc_lang)
        except Exception:  # noqa: S110 — The byline is supplementary and must never fail composition.
            _meta_pills = None
        sections_to_emit = project_document(
            sections_to_emit,
            lang=_doc_lang,
            valid_chart_ids=existing_section_ids,
            meta_pills=_meta_pills,
            chapter_mode=chapter_mode,
        )
        logger.info(
            "compose_payload.doc_projection turn=%s kinds=%s -> %s",
            request.turn_id,
            _before_kinds,
            [str(x.get("kind")) for x in sections_to_emit if isinstance(x, dict)],
        )

        # Sprint θ Phase γ: inline citation binding. Walk text-bearing fields
        # (markdown / narrative / thesis / action / etc.) and upgrade
        # citation-audit M1/M3-2: Phase-γ gen2 upgrade (`[num:V:U]` ->
        # `[num:V:U:src:S]` + `#highlight=`) was **entirely removed**. Binding now directly produces `[N]`
        # inside Phase-F below (value -> single content-addressed source, numbered by the same registry).

        # ─── Sprint agent-redesign-1 Phase F r2 — sources[] + [N] resolver ───
        # In-place mutate text fields in already-built sections_to_emit + the
        # `sources` section (if present). Runs AFTER citation bind (which adds
        # src: suffix to legacy [num:V:U]). Flag-gated.
        #
        # Review P0-2 fix: previously this block REBUILT sections_to_emit via
        # `_build_sections_from_payload(...)`, which silently lost the
        # chart-auto-build + data-hash dedup done upstream (L744 / L752-816).
        # Now we walk sections_to_emit in-place using `resolve_in_text` on each
        # text field, and refresh the `sources` section's entries from the
        # turn-scope sources list built incrementally.
        # citation-audit M1/M3-2: Phase-F is the sole peer-text binding point and applies unconditionally
        # (legacy `BOWHEAD_SOURCES_REF_V2` flag retired together with the kill switch).
        if sections_to_emit:
            try:
                from bowhead_core.sources_resolver import (
                    enrich_source_labels,
                    resolve_bare_artifact_ids,
                    resolve_in_text,
                )

                # citation-audit M1-1: turn-scoped registry on the bus, sharing its numbering space with
                # thin-path / writer-loop and continuing [N] already allocated by thin-path
                # (sources/id_to_index are live registry references, updated directly by lazy registration
                # below). M3-2: removed the old per-payload list branch.
                from bowhead_core.sources_resolver import (
                    get_turn_sources_registry as _get_reg,
                )

                # Canvas-elements source merge: table construction and writeback share one entry. This is the **production default path**
                # (ψ-E eager / default ghostwritten cards), which previously built tables without writing back, thereby
                # overwriting canvas-scoped sources with a current-turn-only list.
                _f_reg = await _get_reg(bus, canvas_store=canvas_store, canvas_id=canvas.id, turn_id=request.turn_id)
                _f_reg.ingest_payload_sources(payload.get("sources"), tool_invocations)
                _f_reg.ingest_tool_invocations(tool_invocations)
                _f_sources, _f_id2idx = _f_reg.sources, _f_reg.id_to_index
                # Walk every text-bearing field in every section
                # Sprint chat-stream-fix D-1 (2026-05-16): added `rationale`,
                # `expected_impact`, `owner`, `timeline` to cover the new
                # recommendations item shape (canvas_composer:1356-1364).
                # Previously only `action` was in _TEXT_KEYS → rationale +
                # expected_impact still had raw `[num:V:U:src:tl_xxx]` markers.
                _TEXT_KEYS = (
                    "markdown",
                    "text",
                    "thesis",
                    "narrative",
                    "exec_summary",
                    "title",
                    "callout_text",
                    "caption",
                    "subcaption",
                    "action",
                    "rationale",
                    "expected_impact",
                    "owner",
                    "timeline",
                )

                # citation-audit M1-2 (composer integration): in the new mode Phase-F is
                # the sole peer-text binding point. Explicit :src: attribution uses resolve_in_text
                # to connect content-addressed sources directly to numbers, without value-inference fallback. Residual plain gen2
                # bakes to plain values (units retained), then bind_to_numbered adds [N] to bare values.
                _v2s_f: dict[float, str] = {}
                if tool_invocations:
                    try:
                        from bowhead_core.canvas_composers.answer_decomposer import (
                            _build_value_to_source_map as _bv2s,
                        )

                        _v2s_f = _bv2s(tool_invocations)
                    except Exception:  # noqa: S110
                        _v2s_f = {}

                def _walk_text_fields(obj: Any) -> None:
                    """Rewrite [num:V:U] / [ref:S] in any string field in obj
                    matching _TEXT_KEYS. Recurses into list values (items[]).
                    """
                    nonlocal _f_sources, _f_id2idx  # type: ignore[misc]
                    if not isinstance(obj, dict):
                        return
                    for _k in _TEXT_KEYS:
                        _v = obj.get(_k)
                        if not isinstance(_v, str):
                            continue
                        _new = _v
                        # SEC-P0 (implementation review): strip peer-authored bare [N], even in-range numbers
                        # before minting any legitimate [N], otherwise frontend decoration makes hallucinated conclusions clickable
                        # as trusted citations and inflates verified counts.
                        # Strip untranslated purely numeric [ref:K] remnants too (C-P2-4: they synthesize
                        # garbage sources); resolve handles actual chunk IDs normally.
                        from bowhead_core.citation_marks import (
                            strip_cite_markers as _strip_cite,
                        )
                        from bowhead_core.citation_marks import (
                            strip_untrusted_numbered as _strip_untrusted,
                        )

                        # Composer half of source-accessibility presentation e1: `_auto_append_citations` adds paragraph-end
                        # `[cite:N]` to exec_summary / facts in the **turn registry numbering space**.
                        # That is chat-surface semantics; `[N]` minted for the same prose below uses the **sources numbering space**.
                        # The 08-28 decision assigns canvas-prose attribution to gen3 [N]. e1 stripped markers only at the writer path's
                        # evidence-block entry (report_writer_loop `_strip_turn_cite_markers`),
                        # missing this direct-payload path. 116 found `112.39 亿元 [1]` in one paragraph referencing
                        # sources #1, while c2 rendered paragraph-end `[cite:1]` as [1] referencing registry #1: identical appearance,
                        # different meaning. Strip before minting any [N]; leave `[figure:sec_x]` anchors unchanged, as in e1.
                        if "[cite:" in _new:
                            _new = _strip_cite(_new)
                        if "[" in _new:
                            _new = _strip_untrusted(_new)
                            _new = _NUMERIC_REF_RE.sub("", _new)
                        if "[num:" in _new or "[ref:" in _new:
                            _new, _f_sources, _f_id2idx = resolve_in_text(
                                _new,
                                sources=_f_sources,
                                id_to_index=_f_id2idx,
                                tool_invocations=tool_invocations,
                            )
                        # Follow-up #12: bare artifact IDs in prose -> [N] in the same numbering space.
                        if "art_" in _new:
                            _new, _f_sources, _f_id2idx = resolve_bare_artifact_ids(
                                _new,
                                sources=_f_sources,
                                id_to_index=_f_id2idx,
                                tool_invocations=tool_invocations,
                            )
                        from bowhead_core.citation_marks import (
                            bind_to_numbered as _bind_n,
                        )
                        from bowhead_core.citation_marks import (
                            strip_legacy as _strip_lg,
                        )

                        # Residual plain gen2 / [chart:] / [fact:] -> bake.
                        if "[num:" in _new or "[ref:" in _new or "[chart:" in _new or "[fact:" in _new or "[bi:" in _new or "[memory:" in _new:
                            _new = _strip_lg(_new)
                        # Bare values -> [N] (skip adjacent markers to prevent duplicates; lazy registration uses the same numbering space).
                        if _v2s_f:
                            _new = _bind_n(
                                _new,
                                value_to_source=_v2s_f,
                                sources=_f_sources,
                                id_to_index=_f_id2idx,
                                tool_invocations=tool_invocations,
                            )
                        if _new != _v:
                            obj[_k] = _new

                for _sec in sections_to_emit:
                    if not isinstance(_sec, dict):
                        continue
                    _walk_text_fields(_sec)
                    # Nested: items[] (recommendations + kpi_row) — each
                    # item dict has the same text fields as a section.
                    _items = _sec.get("items")
                    if isinstance(_items, list):
                        for _item in _items:
                            _walk_text_fields(_item)
                    # Nested: evidence[].callout_text/caption + insight subfields
                    # Review F8: caption/callout_text both belong to _TEXT_KEYS;
                    # evidence items reuse _walk_text_fields directly instead of a second manual implementation.
                    _ev = _sec.get("evidence")
                    if isinstance(_ev, list):
                        for _e in _ev:
                            _walk_text_fields(_e)
                # citation-audit M1-3 (composer path): unverified numbers -> structured fields.
                # Previously amber rendering of peer plain-gen2 markers conveyed this meaning (the sole
                # producer wire was legacy decompose). New mode bakes plain markers, so compute it
                # deterministically for narrative sections instead, using the same frontend banner channel.
                if tool_invocations:
                    try:
                        from bowhead_core.canvas_composers.answer_decomposer import (
                            _unverified_numbers_in_text,
                        )
                        from bowhead_core.number_verifier import (
                            build_authorized_numbers,
                        )

                        _auth_f = build_authorized_numbers(tool_invocations)
                        if _auth_f:
                            for _sec in sections_to_emit:
                                if not isinstance(_sec, dict):
                                    continue
                                _kind_f = _sec.get("kind")
                                if _kind_f == "narrative":
                                    # Document projection renders recommendations as narrative (style=recommendations).
                                    # These are action instructions, whose numbers are naturally targets rather than data assertions
                                    # (same reason FU-1 excludes recommendations from grounding), so do not scan them.
                                    # The byline's data-system count (style=byline) likewise is not a data assertion.
                                    if _sec.get("style") in ("recommendations", "byline"):
                                        continue
                                    _texts_f = [_sec.get("markdown") or _sec.get("text")]
                                elif _kind_f == "insight_card":
                                    # SEC-P1 (implementation review): insight-card unverified numbers previously relied on
                                    # amber peer plain-gen2 rendering. After baking, add structured
                                    # computation or fabricated numbers lose all warnings (and bare
                                    # [N] can incorrectly inflate verified counts).
                                    _texts_f = [
                                        _sec.get("thesis"),
                                        _sec.get("narrative"),
                                    ] + [_e.get("callout_text") for _e in (_sec.get("evidence") or []) if isinstance(_e, dict)]
                                else:
                                    continue
                                _joined_f = "\n".join(t for t in _texts_f if isinstance(t, str) and t.strip())
                                if not _joined_f:
                                    continue
                                _unv_f = _unverified_numbers_in_text(_joined_f, _auth_f, trace=str(getattr(request, "turn_id", "") or ""))
                                if _unv_f:
                                    # Write the list and total together (see `report_trust.apply_unverified`):
                                    # separate writes eventually omit one, making the trust percentage falsely optimistic.
                                    apply_unverified(_sec, list(_unv_f), cap=10)
                    except Exception as _unv_exc:  # noqa: S110
                        logger.warning(
                            "compose_payload.unverified_flag_failed turn=%s err=%s",
                            request.turn_id,
                            _unv_exc,
                        )
                # Update or insert `sources` section with the final list.
                # Sprint chat-stream-fix D-1 (2026-05-16): filter dropped empty-
                # string fields, but if ALL fields were empty the entry
                # collapsed to `{}` and broke [N] popover lookup. Now require
                # at least `label` OR `artifact_id` to keep entry; otherwise
                # drop. sources_resolver Pass 1 already synthesizes label from
                # tool+query when empty.
                if _f_sources:
                    _src_entries = []
                    for s in _f_sources:
                        cleaned = {k: v for k, v in s.items() if v not in (None, "")}
                        if cleaned.get("label") or cleaned.get("artifact_id"):
                            _src_entries.append(cleaned)
                    # Follow-up #14: make fallback label artifact: art_x readable using the filename.
                    # Review F1: ownership checks prevent leaking others' artifact filenames.
                    _src_entries = await enrich_source_labels(
                        _src_entries,
                        artifact_store,
                        user_id=getattr(request, "user_id", None),
                    )
                    # The third sources-building path (Phase-F rebuild) also needs distinguishing suffixes.
                    from bowhead_core.sources_resolver import number_duplicate_source_labels

                    number_duplicate_source_labels(
                        _src_entries,
                        lang=_chrome_lang(getattr(request, "language_context", None)),
                    )
                    _existing_sources_sec = next(
                        (s for s in sections_to_emit if isinstance(s, dict) and s.get("kind") == "sources"),
                        None,
                    )
                    if _existing_sources_sec is not None:
                        _existing_sources_sec["entries"] = _src_entries
                    else:
                        from bowhead_core.report_chrome_text import chrome_text as _chrome_text

                        sections_to_emit.append(
                            {
                                "kind": "sources",
                                "label": _chrome_text(
                                    "sources_label",
                                    _chrome_lang(getattr(request, "language_context", None)),
                                ),
                                "entries": _src_entries,
                            }
                        )
            except Exception as _f_exc:
                logger.warning(
                    "compose_payload.phase_f_resolver_failed turn=%s err=%s — sections still emitted with legacy ν1 markers",
                    request.turn_id,
                    _f_exc,
                )

        if emit_lifecycle:
            await bus.emit(
                _event(
                    request.turn_id,
                    "canvas.outline_ready",
                    {
                        "sections": [{"id": f"sec_pre_{i}", "kind": s["kind"]} for i, s in enumerate(sections_to_emit)],
                        "meta_intent": intent.value if intent else None,
                        "source": "payload",
                    },
                )
            )

        # ψ-E r2: dedupe per (turn_id, peer_id, kind_ordinal). Eager + Lead
        # end-of-turn calls share the same canvas; without dedupe, Lead's
        # final compose would re-write peer's already-written sections.
        _composed = _composed_set_for(canvas.id)
        _peer_key = peer_id or "lead"

        # citation-audit M3-3 (§4.5): same as above; removed i18n rendering before emit,
        # so emitted DTO == persisted payload.
        composed_kinds: list[str] = []
        new_payload_ids: list[str] = []
        _kind_ordinal: dict[str, int] = {}
        for sec in sections_to_emit:
            kind = sec["kind"]
            # ψ-E r2 dedupe: ordinal counts how many of this kind this peer has
            # already produced. (turn_id, peer_id, kind_ord) uniquely identifies.
            _ord = _kind_ordinal.get(kind, 0)
            _kind_ordinal[kind] = _ord + 1
            _idem_key = (request.turn_id, _peer_key, f"{kind}#{_ord}")
            if _idem_key in _composed:
                # ar1 P1-2 (CTO review f8ed5ba): for kind="sources" specifically,
                # Phase F resolver may have updated entries on the second compose
                # (Lead end-of-turn) with new artifact_ids referenced. Skip-only
                # would leave frontend `numberedSources` stale. Patch the existing
                # section's payload instead so [N] lookups resolve.
                if kind == "sources":
                    try:
                        # citation-audit (implementation review B-P2-6): consolidate in the upsert
                        # primitive. The old next(kind=="sources") branch lacked turn/superseded
                        # filtering and CAS, writing this turn's entries into another turn's
                        # section, even a superseded one, on cross-turn canvases.
                        from bowhead_core.report_chrome_text import chrome_lang as _chrome_lang2
                        from bowhead_core.sources_resolver import (
                            upsert_turn_sources_section as _upsert_src2,
                        )

                        await _upsert_src2(
                            canvas_store=canvas_store,
                            bus=bus,
                            canvas_id=canvas.id,
                            turn_id=request.turn_id,
                            entries=sec.get("entries") or [],
                            lang=_chrome_lang2(getattr(request, "language_context", None)),
                        )
                        continue
                    except Exception as _src_patch_exc:
                        logger.warning(
                            "compose.sources_repatch_failed turn=%s err=%s",
                            request.turn_id,
                            _src_patch_exc,
                        )
                # Lead end-of-turn re-compose hitting same peer payload —
                # skip silently (peer already wrote this section).
                continue
            _composed.add(_idem_key)

            payload_for_section = {k: v for k, v in sec.items() if k != "kind"}
            # canvas-update-turn batch A2: stamp eager cards with renderer identity (server-written, patch-protected `_meta`).
            # Finalization excludes eager **nonchapter** narrative from handwritten content because full chrome already rendered; eager **chapters**
            # in plan turns remain handwritten. Batch B1-0: Lead default-ghostwriter / remainder-card paths (peer_id=None) also stamp composed_by="lead".
            # `is_document_h1` uses `_meta.chapter` for document boundaries; unmarked h1 is treated as document-level.
            _m = dict(payload_for_section.get("_meta") or {})
            _m["composed_by"] = "eager" if peer_id else "lead"
            _m["chapter"] = bool(chapter_mode)
            payload_for_section["_meta"] = _m
            if kind == "sources":
                # citation-audit M1-1 general rule 2: sources use the upsert primitive. Thin-path
                # (peer-stage canvas_add_insight) may already have created this turn's section; converge by patching
                # rather than appending a duplicate that mixes [N] numbering. Upsert emits events internally.
                from bowhead_core.sources_resolver import (
                    upsert_turn_sources_section as _upsert_src,
                )

                _sid_src = await _upsert_src(
                    canvas_store=canvas_store,
                    bus=bus,
                    canvas_id=canvas.id,
                    turn_id=request.turn_id,
                    entries=payload_for_section.get("entries") or [],
                    lang=_chrome_lang(getattr(request, "language_context", None)),
                )
                if _sid_src:
                    composed_kinds.append(kind)
                    new_payload_ids.append(_sid_src)
                continue
            stored = await canvas_store.append_section(
                canvas_id=canvas.id,
                kind=kind,
                payload=payload_for_section,
                by_turn_id=request.turn_id,
                # Batch 1-a (plan-report-assembly technical design §2.1): persist peer_id directly as owner,
                # the binding-slot key. Plan looks up sections produced by this step using `owner == "sub_" + step.id`
                # (agent_runner_adapter:510 derives subagent_id from step ID), so determining which step
                # produced which sections needs no outputs threaded across layers (self-review R2).
                # peer_id=None (final Lead / ordinary turns) -> owner=None, byte-identical to previous behavior.
                owner=peer_id or None,
            )
            composed_kinds.append(kind)
            new_payload_ids.append(stored.id)
            section_dto = stored.to_report_section()
            await bus.emit(
                _event(
                    request.turn_id,
                    "canvas.section_added",
                    {"canvas_id": canvas.id, "section": section_dto, "seq": stored.seq},
                )
            )

        # M137 follow-up: move payload sections to the canvas top, trailing preexisting streaming sections
        # (subagent add_chart charts / realtime_stream sql_block, etc.)
        # afterward. Measurement t_97a57d365e04: six charts + 11 sql_block sections appeared above the title,
        # forcing users through tables/SQL before the structured report, a poor experience.
        # ψ-E r2: reorder skip if no new sections this call (Eng P0 #5).
        # Without skip, eager call's `target_order = []` would push everything
        # to tail, then Lead's compose pushes things back — infinite shuffle.
        # Batch 1-b: once this turn has a second **producing leg**, promote_to_top must yield to slot order;
        # otherwise each submission moves itself first, reversing completion order (discovery C3), fighting plan
        # reordering and making blocks visibly jump.
        # ★ Self-review R1: the gate counts producing legs, not owner presence. Every ψ-E compose
        # supplies peer_id, so using it as the gate unconditionally short-circuits all multipeer turns.
        # Preserve behavior for N=1: promote_to_top places payload above streaming charts;
        # for N>=2, reorder_sections' unlisted-sections-tail-in-original-order rule provides the equivalent behavior.
        _slot_ordered_elsewhere = False
        if new_payload_ids:
            try:
                _owners = {
                    str(getattr(_s, "owner", "") or "")
                    for _s in await canvas_store.list_sections(canvas.id)
                    if getattr(_s, "turn_id", None) == request.turn_id and getattr(_s, "owner", None)
                }
                _slot_ordered_elsewhere = len(_owners) >= 2
            except Exception as _oe:
                # Probe failures fall back to legacy promotion; inability to count legs must not prevent ordering.
                logger.warning("compose_payload.owner_probe_failed turn=%s err=%s", request.turn_id, _oe)
        # canvas-update-turn batch B0 (review fix): Lead revise/append creates no new document this turn; cards add
        # a chapter/evidence group after the old report. promote_to_top moves them under its title but above old prose (N=1
        # applies canonical_order to `new + tail`, placing new same-rank sections first). Skip promotion and retain append order at the end.
        _mode_no_promote = False
        if new_payload_ids and not _slot_ordered_elsewhere:
            with contextlib.suppress(Exception):
                from bowhead_core.canvas_revise import no_document_chrome as _no_chrome_promote

                _mode_no_promote = _no_chrome_promote(bus)
            if _mode_no_promote:
                logger.info("compose_payload.promote_skip turn=%s reason=canvas_mode", request.turn_id)
        if new_payload_ids and not _slot_ordered_elsewhere and not _mode_no_promote:
            try:
                all_sections = await canvas_store.list_sections(canvas.id)
                all_ids = [s.id for s in all_sections]
                new_id_set = set(new_payload_ids)
                tail_ids = [sid for sid in all_ids if sid not in new_id_set]
                target_order = new_payload_ids + tail_ids
                # A3: like reorder_for_chart_refs, apply canonical presentation order before persistence (headers first /
                # code·SQL appendices last), using one canvas_rank source and preserving computed same-rank order.
                _by_id = {s.id: s for s in all_sections}
                target_order = canonical_order([_by_id[sid] for sid in target_order if sid in _by_id])
                if target_order != all_ids:
                    await canvas_store.reorder_sections(
                        canvas_id=canvas.id,
                        ordered_ids=target_order,
                        by_turn_id=request.turn_id or MACHINE_REORDER_TURN_ID,
                    )
                    # chat-polish-r3 Phase 4 (B8/B9) — drop the
                    # `if emit_lifecycle:` gate. Reorder is a content
                    # event (state mutation FE must see), NOT a lifecycle
                    # signal like canvas.opened/done. Pre-r3 the gate
                    # meant eager compose (emit_lifecycle=False, called
                    # from subagent_return) reordered DB silently, then
                    # Lead's end-of-turn compose hit dedup → new_payload_ids
                    # empty → never reached this branch → FE stuck on
                    # arrival-order rendering. F5 fetches the sorted DB
                    # state so it always looked correct (complete only at F5). Now
                    # FE gets the reorder signal whichever path ran the
                    # actual DB reorder.
                    await bus.emit(
                        _event(
                            request.turn_id,
                            "canvas.reordered",
                            {"canvas_id": canvas.id, "order": target_order, "reason": "payload_path_promote_to_top"},
                        )
                    )
            except Exception as e:
                logger.warning(
                    "compose_payload.reorder_failed turn=%s err=%s",
                    request.turn_id,
                    e,
                )

        if emit_lifecycle:
            # trust is attached centrally in _emit_canvas_done (covers every emit path).
            await _emit_canvas_done(
                bus, request.turn_id, {"canvas_id": canvas.id, "section_count": len(composed_kinds), "errors": [], "source": "payload"}
            )
            # ψ-E r2: lifecycle done → drop dedup set for this canvas
            _PSI_COMPOSED_KEYS.pop(canvas.id, None)
        return composed_kinds
    except Exception as e:
        logger.exception("compose_canvas_from_payload.failed turn=%s err=%s", request.turn_id, e)
        if emit_lifecycle:
            await _emit_canvas_done(
                bus, request.turn_id, {"canvas_id": None, "section_count": 0, "errors": [f"compose_payload_failed: {e}"], "source": "payload"}
            )
        return []
    finally:
        # ψ-E r2: always release lock if held
        if "_psi_lock_held" in locals() and _psi_lock_held:
            try:  # noqa: SIM105
                _psi_lock.release()
            except RuntimeError:  # lock not held / already released
                pass


_CITATION_TEXT_FIELDS = ("markdown", "narrative", "thesis", "action", "rationale", "callout_text", "subcaption", "expected_impact")


def _data_hash_for_section(sec: dict[str, Any]) -> str | None:
    """chat-truth L-BE: compute deterministic hash of a section's data payload
    for cross-section dedup (walkthrough Session 7: 4 identical 36-row tables
    under different titles).

    Returns hex digest str for `data_table` / `chart` / `kpi_row` kinds when
    payload carries rows+columns; None for sections without dedupe-able data
    (narrative / sources / etc — content is prose, dedup-by-text would
    over-aggressively drop near-duplicates).
    """
    import hashlib
    import json as _json

    kind = sec.get("kind")
    if kind not in ("data_table", "chart", "kpi_row"):
        return None
    try:
        # Collect data signature: prefer rows+columns; fall back to chart spec
        # data or kpi cells. Title intentionally excluded — Session 7 evidence:
        # same data, different titles, still want dedup.
        sig: dict[str, Any] = {}
        if "rows" in sec:
            sig["rows"] = sec.get("rows")
        if "columns" in sec:
            sig["columns"] = sec.get("columns")
        if "cells" in sec:
            sig["cells"] = sec.get("cells")
        if "spec" in sec and isinstance(sec.get("spec"), dict):
            _spec = sec["spec"]
            # Use chart series + categories (the data, not labels) as signature
            sig["spec_data"] = {k: _spec.get(k) for k in ("series", "categories", "data", "values") if k in _spec}
        if not sig:
            return None
        raw = _json.dumps(sig, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return None


def dedup_sections_by_data_hash(
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop duplicate data-bearing sections (data_table / chart / kpi_row)
    sharing identical rows+columns/spec data. Preserves order — keeps first
    occurrence. Non-data sections (narrative, sources, etc) pass through
    untouched.

    Used by `compose_canvas_from_payload` before append_section calls. Fixes
    walkthrough Session 7 (4 identical 36-row 品类 tables in same turn).
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for sec in sections:
        h = _data_hash_for_section(sec)
        if h is not None:
            if h in seen:
                logger.info(
                    "canvas_compose.dedup_skip kind=%s data_hash=%s (walkthrough Session 7 fix)",
                    sec.get("kind"),
                    h,
                )
                continue
            seen.add(h)
        out.append(sec)
    return out


def _extract_chart_titles_from_invocations(
    tool_invocations: list[Any] | None,
) -> list[str]:
    """chat-truth N: pull `add_chart` titles from tool_invocations
    for F4-A2 narrative fallback (when exec_summary + insights both empty).

    Returns ordered list of non-empty title strings (dedup preserving order).
    """
    if not tool_invocations:
        return []
    titles: list[str] = []
    seen: set[str] = set()
    for inv in tool_invocations:
        try:
            name = getattr(inv, "name", None) or getattr(inv, "tool_name", None)
            if name != "add_chart":
                continue
            args = getattr(inv, "arguments", None) or getattr(inv, "args", None) or {}
            if not isinstance(args, dict):
                continue
            t = str(args.get("title") or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            titles.append(t)
        except Exception:  # noqa: S112
            continue
    return titles


def _build_bi_fallback_sections(
    tool_invocations: list[Any] | None,
) -> list[dict[str, Any]]:
    """D-fix 2026-05-25 (canvas-empty-bug) — Lead 偷懒 self-narrate 不 spawn compute
    时, AnalysisPayload 是空的, 但 tool_invocations 里有 bi_query / bi_fetch_rows
    的 sample_rows. 这个 helper 直接从 sample_rows 提取 KPI 和 data_table section,
    保证 canvas 至少有数据 section.

    返 [{kind: 'kpi_row', cells: [...]}] + 可选 [{kind: 'data_table', ...}].
    每个 single-row result → kpi cell; multi-row → data_table.
    """
    if not tool_invocations:
        return []
    sections: list[dict[str, Any]] = []
    kpi_cells: list[dict[str, Any]] = []
    table_rows_per_query: list[tuple[str, list[dict[str, Any]]]] = []

    for inv in tool_invocations:
        name = getattr(inv, "tool_name", "") or ""
        if name not in ("bi_query", "bi_fetch_rows"):
            continue
        result = getattr(inv, "result", None)
        if not isinstance(result, dict):
            continue
        results_list = result.get("results") or []
        if not isinstance(results_list, list):
            continue
        for r in results_list:
            if not isinstance(r, dict) or not r.get("ok"):
                continue
            sample_rows = r.get("sample_rows") or []
            query_text = str(r.get("query_text") or "").strip() or "查询"
            if not isinstance(sample_rows, list) or not sample_rows:
                continue
            if len(sample_rows) == 1 and isinstance(sample_rows[0], dict):
                row = sample_rows[0]
                non_empty = [(k, v) for k, v in row.items() if v not in (None, "")]
                if not non_empty:
                    continue
                for k, v in non_empty:
                    label = f"{query_text}" if len(non_empty) == 1 else f"{query_text} · {k}"
                    if len(non_empty) == 1:
                        label = str(k)  # column name is the metric name
                    kpi_cells.append(
                        {
                            "lbl": label[:60],
                            "val": str(v)[:40],
                        }
                    )
            elif len(sample_rows) > 1:
                table_rows_per_query.append((query_text, sample_rows[:50]))

    if kpi_cells:
        sections.append({"kind": "kpi_row", "cells": kpi_cells[:8]})

    for query_text, rows in table_rows_per_query[:3]:
        if not rows:
            continue
        cols = list(rows[0].keys())

        # 0.39.8 #4 — emit FE-shaped TableCol[]/TableRow[] (not bare strings
        # and not array-of-arrays). FE `data-table.tsx` does
        # `<th>{c.header}</th>` + `<td>{row[col.key]}</td>` — without
        # objectified columns / keyed rows, every cell renders as "—".
        # Numeric column detection: peek first non-null value per col.
        def _is_numeric_col(col_name: str) -> bool:
            for r in rows:  # noqa: B023
                v = r.get(col_name)
                if v is None or v == "":
                    continue
                return isinstance(v, (int, float)) and not isinstance(v, bool)
            return False

        sections.append(
            {
                "kind": "data_table",
                "caption": query_text[:80],
                "columns": [{"key": c, "header": c, "numeric": _is_numeric_col(c)} for c in cols],
                "rows": [{c: row.get(c, "") for c in cols} for row in rows],
            }
        )

    return sections


def _build_sections_from_payload(
    payload: dict[str, Any],
    intent: IntentType | None,
    tool_invocations: list[Any] | None = None,
    lang: str = "en-US",
    *,
    chapter_mode: bool = False,
) -> list[dict[str, Any]]:
    """payload (dict) → 顺序的 section list，每个含 kind + 字段.

    section kinds:
      - ``eyebrow``         (新增)：标签段（"合规监控"）
      - ``h1``              ：报告标题
      - ``meta``            ：intent pill (REPORT / CAUSAL / ...)
      - ``narrative``       ：exec_summary（带 ``style: 'executive'`` 让前端突出样式）
      - ``kpi_row``         ：4-5 个 KPI 卡
      - ``insight_card``    (新)：每个 insight 一段，含 thesis + evidence_refs + narrative + impact
      - ``recommendations`` (新)：行动建议列表（priority 排序）
      - ``narrative``       ：global caveats（带 ``style: 'caveats'``）
      - ``sources``         ：数据源
    """
    out: list[dict[str, Any]] = []

    # eyebrow
    # Batch 2-a (plan-report-assembly §2.3): chapter_mode means this payload delivers
    # **one chapter**, not a standalone report. Defer leg-level chrome to document-level final Lead:
    # discard eyebrow (only one per document); pool recommendations/caveats and render each once for the document.
    # Retain chapter content: title(h1) / exec_summary / kpis / insights / sources.
    # Eight legs rendering whole reports produced four eyebrows, five recommendation sets and six caveat blocks (discovery C4).
    eyebrow = payload.get("eyebrow")
    if eyebrow and not chapter_mode:
        out.append({"kind": "eyebrow", "text": str(eyebrow)})

    # h1
    title = str(payload.get("title") or "").strip()
    if title:
        out.append({"kind": "h1", "text": title})

    # meta pill (intent)
    # Batch 2-a (review B2-4): meta pills (updated time / N data systems / Bowhead-generated)
    # are **document-level** identification, not chapter content; one per chapter gives eight rows. Handle like eyebrow.
    if intent is not None and not chapter_mode:
        out.append({"kind": "meta", "pills": [intent.value.upper()]})

    # exec summary as narrative
    exec_summary = str(payload.get("exec_summary") or "").strip()
    # canvas-pipeline-fix F4 backstop: payload lacks exec_summary but has insights
    # -> concatenate insight theses into stub narrative (no new LLM-authored prose, just
    # listing payload.insights[].thesis), ensuring the canvas always has a narrative
    # section (compute peer / strip-and-pass / direct Lead calls may all lack exec_summary).
    if not exec_summary:
        _ins_list = payload.get("insights") or []
        _theses = [str(ins.get("thesis") or "").strip() for ins in _ins_list if isinstance(ins, dict) and (ins.get("thesis") or "").strip()]
        if _theses:
            exec_summary = "**关键发现**\n\n" + "\n\n".join(f"- {t}" for t in _theses)
        else:
            # chat-truth N (Case A fallback): both exec_summary AND insight theses
            # empty. Peer did chart-heavy turn (add_chart × N) but returned
            # subagent_return(payload={}). Synthesize stub narrative from chart
            # titles so canvas guarantees ≥ 1 narrative section. Better than 0/7.
            # See docs/sprints/chat-truth/n-spike-findings.md.
            _chart_titles = _extract_chart_titles_from_invocations(tool_invocations)
            if _chart_titles:
                _items = "\n".join(f"- {t}" for t in _chart_titles[:10])
                exec_summary = f"**关键发现**\n\n本次分析生成了 {len(_chart_titles)} 张图表:\n\n{_items}"
    if exec_summary:
        out.append(
            {
                "kind": "narrative",
                "markdown": exec_summary,
                "style": "executive",
            }
        )

    # kpi_row
    kpis = payload.get("kpis") or []
    if kpis:
        cells = []
        for k in kpis:
            cell: dict[str, Any] = {
                "lbl": str(k.get("label") or ""),
                "val": str(k.get("value") or ""),
            }
            if k.get("delta"):
                cell["delta"] = str(k["delta"])
            sentiment = k.get("sentiment")
            # chat-v4 CHANGE-S.5: +warning (neutral-negative, yellow on frontend).
            if sentiment in ("good", "bad", "neutral", "warning"):
                cell["sentiment"] = sentiment
            cells.append(cell)
        out.append({"kind": "kpi_row", "cells": cells})

    # insights → insight_card per insight
    for ins in payload.get("insights") or []:
        evidence_blocks: list[dict[str, Any]] = []
        for ev in ins.get("evidence") or []:
            block = {
                "kind": ev.get("kind", "callout"),
                "caption": ev.get("caption", ""),
                "display_hint": ev.get("display_hint", "full_width"),
            }
            for ref_field in (
                "chart_section_id",
                "computation_artifact_id",
                "table_artifact_id",
                "data_artifact_id",
                "callout_text",
                "subcaption",
            ):
                v = ev.get(ref_field)
                if v not in (None, ""):
                    block[ref_field] = v
            evidence_blocks.append(block)

        card: dict[str, Any] = {
            "kind": "insight_card",
            "title": str(ins.get("title") or ""),
            "thesis": str(ins.get("thesis") or ""),
            "narrative": str(ins.get("narrative") or ""),
            "evidence": evidence_blocks,
            "confidence": ins.get("confidence") or "med",
        }
        impact = ins.get("impact")
        if isinstance(impact, dict) and any(impact.get(k) not in (None, "") for k in impact):
            card["impact"] = {k: v for k, v in impact.items() if v not in (None, "")}
        sources = ins.get("source_refs")
        if sources:
            card["source_refs"] = list(sources)
        out.append(card)

    # Recommendations are **no longer rendered** (canvas-elements tech-design-document-minimalism §2.2;
    # user chose prose on 2026-09-07). Recommendations are **content** owned by the author; prompts teach inclusion under prose `## 建议`.
    # Half of 312 handwritten reports already did so; system rendering duplicates them (12.8% measured with both sets).
    # Keep `payload.recommendations`: it remains structured peer evidence for Lead's chat answer,
    # but no longer automatically becomes canvas sections.

    # Render machine-generated caveats as styled narrative.
    # This deterministic composer has no separate provenance section, unlike the
    # handwritten writer path. Filtering guard entries here would hide unsupported-
    # number warnings. Any future separate section must move the filter and renderer
    # together. Model-authored caveats remain optional prose; provenance warnings and
    # missing-plan-leg notices are system-generated honesty information.
    from bowhead_core.analysis_payload import plan_gap_caveats as _gap_cav
    from bowhead_core.analysis_payload import strip_caveat_marker as _strip_marker

    _gap_caveats = [_strip_marker(str(c)) for c in _gap_cav(payload) if str(c).strip()]
    if _gap_caveats and not chapter_mode:
        from bowhead_core.report_chrome_text import chrome_text as _gap_text

        out.append(
            {
                "kind": "narrative",
                "markdown": _gap_text("caveats_heading", lang) + "\n\n" + "\n".join(f"- {c}" for c in _gap_caveats),
                "style": "caveats",
                # Batch B1-1: system-generated marker prevents later author-caveat rewrites from overwriting it (same as report_writer_loop missing-leg section).
                "_meta": {"gap": True},
            }
        )

    from bowhead_core.analysis_payload import provenance_caveats as _prov_cav

    _guard_caveats = [c for c in _prov_cav(payload) if str(c).strip()]
    if _guard_caveats and not chapter_mode:
        from bowhead_core.report_chrome_text import chrome_text as _chrome_text

        body = _chrome_text("provenance_heading", lang) + "\n\n" + "\n".join(f"- {c}" for c in _guard_caveats)
        out.append(
            {
                "kind": "narrative",
                "markdown": body,
                "style": "caveats",
            }
        )

    # sources
    sources = payload.get("sources") or []
    if sources:
        entries = []
        for s in sources:
            entry: dict[str, Any] = {}
            for k in ("label", "tool", "artifact_id", "query"):
                v = s.get(k)
                if v not in (None, ""):
                    entry[k] = str(v)
            # citation-hover shows source text: carry the inline-data preview (bi_query
            # sample_rows, folded by TurnSourcesRegistry) through verbatim so the
            # popover can render original rows instead of a dead tl_ link. These are
            # list/int — must NOT be str()-flattened like the fields above.
            pv_cols = s.get("preview_columns")
            pv_rows = s.get("preview_rows")
            if isinstance(pv_cols, list) and isinstance(pv_rows, list) and pv_rows:
                entry["preview_columns"] = pv_cols
                entry["preview_rows"] = pv_rows
                tr = s.get("total_rows")
                if isinstance(tr, int) and not isinstance(tr, bool):
                    entry["total_rows"] = tr
            if entry:
                entries.append(entry)
        if entries:
            from bowhead_core.report_chrome_text import chrome_text as _chrome_text
            from bowhead_core.sources_resolver import number_duplicate_source_labels

            # This legacy path builds sections itself, bypassing upsert, so add distinguishing suffixes here too
            # (one concept implemented N times: changing only upsert misses this path). The function is idempotent,
            # so passing both paths does not stack suffixes.
            number_duplicate_source_labels(entries, lang=lang)
            out.append({"kind": "sources", "label": _chrome_text("sources_label", lang), "entries": entries})

    # D-fix 2026-05-25: empty payload with only BI tool_invocations falls back to sample_rows
    # to build kpi_row / data_table. Trigger: out has no narrative / kpi / insight
    # (Lead skipped spawning compute). Existing paths remain intact: actual AnalysisPayload
    # fields already make out nonempty, bypassing this branch.
    if not any(s.get("kind") in ("kpi_row", "insight_card", "data_table") for s in out):
        bi_sections = _build_bi_fallback_sections(tool_invocations)
        if bi_sections:
            # Add h1 + narrative notice at the top to provide canvas context.
            if not any(s.get("kind") == "h1" for s in out):
                out.insert(0, {"kind": "h1", "text": "数据查询结果"})
            out.extend(bi_sections)

    # dev 1.0.77 dogfood P2-e: a section with only framing and no content must not enter the canvas.
    #
    # Observed shape: user asked to export this canvas report to Word. That turn did no analysis, only invoking
    # the export skill, yet the peer returned a payload `title` equal to the user's instruction, creating
    # an empty report titled with the request to export a Word document (.docx). It looked poor itself,
    # and worse, **polluted every subsequent export**: export reads the entire canvas projection, baking the instruction
    # into xlsx/docx/pdf as a prose title (`art_f2dd91d8ea22` contained a second Title).
    #
    # Check only for substantive content, not whether this is an export turn: without any sections beyond shell kinds
    # (title/eyebrow/meta), the payload has no readable content and emitting it adds only noise.
    # Even the shortest actual report includes narrative / KPI / chart content and cannot hit this guard.
    if out and all(s.get("kind") in _SHELL_ONLY_KINDS for s in out):
        logger.info(
            "composer.skip_shell_only_payload kinds=%s title=%r",
            sorted({str(s.get("kind")) for s in out}),
            str(payload.get("title") or "")[:60],
        )
        return []

    return out
