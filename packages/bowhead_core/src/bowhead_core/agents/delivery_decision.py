"""交付面自决（compute-agent-optimize 批 5ⓐ，用户拍板 2026-08-20）。

「chat 还是报告」由 **agent 决定**，后端不再持有任何「报告长什么样」的定义：
不数图、不看有没有 insight_card、不按 plan 轮强制画布。后端只做两件事——
按优先级挑一个**明说的**决定、在谁都没说时才用旧的证据规则兜底。

决定权层级（高 → 低），每一层都是人或 agent 的明说：

1. ``user``   — 用户原话显式「不要报告」（`parse_user_delivery_directive`）。
2. ``plan``   — planner 的 `deliverable_kind`（answer→chat，report/deck→canvas）。
3. ``lead``   — Lead spawn 时的 `delivery=`（它是唯一看得到用户原话 + 记忆偏好的 agent）。
4. ``peer``   — peer `subagent_return.payload.delivery_surface`（全部 peer 都说 chat → chat；
                任一说 report → canvas；混合/缺失 → 不占档）。
5. ``scanner``— Lead 的 `<response_kind>` 预判（既有，89% 缺席）。
6. ``evidence``— 证据兜底（有画布写入 / 报告级 payload / 亲笔正文 → canvas）。
7. ``heuristic``— 老启发式分派。

这里只放**纯函数**（输入全部显式传入，不碰 bus / ctx），orchestrator 只调它——
`feedback_adapter_pattern_safer_than_hot_path_edit`。交付决策作为正式行为直接生效。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

Surface = Literal["chat", "canvas"]

_TIERS = (
    "user",
    "plan",
    "lead",
    "peer",
    "scanner",
    "evidence",
    "heuristic",
    "canvas_mode",
)  # canvas_mode: batch B0 revise/append turns changing prior body override chat decisions outside the user tier.


@dataclass(frozen=True)
class DeliveryDecision:
    surface: Surface
    tier: str  # one of _TIERS

    def as_bus_value(self) -> dict[str, str]:
        return {"surface": self.surface, "tier": self.tier}


def plan_kind_to_surface(deliverable_kind: str | None) -> Surface | None:
    """planner 的交付形态 → 交付面。answer→chat；report/deck/artifact→canvas；
    其余（none / actions_summary / 缺失）→ 不占档。"""
    k = str(deliverable_kind or "").strip().lower()
    if k == "answer":
        return "chat"
    if k in ("report", "deck", "artifact"):
        return "canvas"
    return None


def peer_declared_surface(peer_payloads: list[Any]) -> Surface | None:
    """peer 们在 payload 里明说的交付面。

    - 忽略 bail 信封（`error.kind`）与交付件 payload（`deliverables`——它们的面由交付件决定）。
    - 全部有效 payload 都 `delivery_surface == "chat"` → "chat"。
    - 任一 `delivery_surface == "report"` → "canvas"（报告赢：漏报告比多报告伤害大）。
    - 没有任何有效声明 → None（不占档，落到下面的档）。
    """
    saw_chat = False
    saw_report = False
    saw_unspecified = False
    for entry in peer_payloads or []:
        p = entry.get("payload") if isinstance(entry, dict) and "payload" in entry else entry
        if not isinstance(p, dict):
            continue
        err = p.get("error")
        if isinstance(err, dict) and err.get("kind"):
            continue
        if p.get("deliverables"):
            continue
        ds = str(p.get("delivery_surface") or "").strip().lower()
        if ds == "report":
            saw_report = True
        elif ds == "chat":
            saw_chat = True
        else:
            saw_unspecified = True
    if saw_report:
        return "canvas"
    if saw_chat and not saw_unspecified:
        return "chat"
    return None


def decide_delivery(
    *,
    user_directive: Surface | None,
    plan_deliverable_kind: str | None,
    lead_delivery: str | None,
    peer_payloads: list[Any],
    scanner_pre_flight: str | None,
    evidence_canvas: bool,
    heuristic_surface: Surface,
) -> DeliveryDecision:
    """按层级挑一个明说的决定；谁都没说才落证据/启发式。

    `lead_delivery` 是 `_lead_spawn_delivery` 的返回（"chat"/"report"/None）；
    `scanner_pre_flight` 是 `response_kind_pre_flight`（"chat"/"canvas"/None）。
    """
    if user_directive in ("chat", "canvas"):
        return DeliveryDecision(user_directive, "user")
    ps = plan_kind_to_surface(plan_deliverable_kind)
    if ps is not None:
        return DeliveryDecision(ps, "plan")
    if lead_delivery in ("chat", "report"):
        return DeliveryDecision("canvas" if lead_delivery == "report" else "chat", "lead")
    peer = peer_declared_surface(peer_payloads)
    if peer is not None:
        return DeliveryDecision(peer, "peer")
    if scanner_pre_flight in ("chat", "canvas"):
        return DeliveryDecision(scanner_pre_flight, "scanner")  # type: ignore[arg-type]
    if evidence_canvas:
        return DeliveryDecision("canvas", "evidence")
    return DeliveryDecision(heuristic_surface, "heuristic")


def revised_turn_override(decision: DeliveryDecision, *, revised_prior: bool) -> DeliveryDecision:
    """canvas-update-turn 批 B0：本轮就地改了上一轮正文（`canvas_revise.turn_revised_prior_narratives` 非空）
    ⇒ 这是画布轮。peer/scanner/heuristic 档判成 chat（canvas-writer 的 payload 常带 delivery_surface=chat、
    Lead 派它时顺口说 delivery=chat）一律覆盖成 canvas；用户明说 chat（user 档）不动。"""
    if revised_prior and decision.surface == "chat" and decision.tier != "user":
        return DeliveryDecision("canvas", "canvas_mode")
    return decision


# ── chat-canvas-surface 2026-08: **early decision** ──────────────
#
# Same priority as `decide_*` above, but **only first three tiers**, since only these are known
# before the peer begins plotting:
#   1 user — original user wording, available at turn start.
#   2 plan — planner deliverable_kind, available at plan-turn start.
#   3 lead — Lead spawn delivery=, available **at spawn** (charts come afterward).
# Tiers 4-7 (peer / scanner / evidence / heuristic) need output, unavailable for early decisions.
#
# Cannot reuse `decide_delivery_surface`: its compose-time call reads
# `result.tool_invocations` (`lead_orchestrator.py` `_decision`), populated only after the peer finishes.
#
# Return None = none of these tiers declares a surface ⇒ **do nothing**, byte-identical prior behavior
# (116 observation: roughly half, declared 61 / evidence fallback 62).
def decide_early_surface(
    *,
    user_directive: str | None,
    plan_deliverable_kind: str | None,
    lead_delivery: str | None,
) -> Surface | None:
    """画图之前就能下的交付面判定；拿不准一律返 None（不猜）。"""
    if user_directive in ("chat", "canvas"):
        return user_directive  # type: ignore[return-value]
    ps = plan_kind_to_surface(plan_deliverable_kind)
    if ps is not None:
        return ps
    if lead_delivery in ("chat", "report"):
        return "canvas" if lead_delivery == "report" else "chat"
    return None


def declared_surface_from_bus(bus: Any) -> Surface | None:
    """终态读 compose 期挂在 bus 上的决定；没有（老 turn / 重放 / flag 关）→ None。"""
    d = getattr(bus, "_delivery_decision", None)
    if isinstance(d, dict):
        s = d.get("surface")
        if s in ("chat", "canvas"):
            return s  # type: ignore[return-value]
    return None


_FIGURE_ANCHOR_ID_RE = re.compile(r"\[figure:([A-Za-z0-9_]+)\]")
_SECTION_ID_RE = re.compile(r"sec_[A-Za-z0-9]+")


def _insight_chart_id(insight: dict[str, Any]) -> str | None:
    """这条论点的 evidence 里第一张图的 section id；只认真 `sec_*`（翻译/校验之后的形状），别的一律不放。"""
    for ev in insight.get("evidence") or []:
        if isinstance(ev, dict):
            sid = str(ev.get("chart_section_id") or "").strip()
            if _SECTION_ID_RE.fullmatch(sid):
                return sid
    return None


def chat_figure_placement_stats(payload: dict[str, Any], answer: str, figure_ids: list[str]) -> dict[str, int]:
    """本轮画的图有几张跟着论点走、几张落在末尾（只喂日志）。

    判「模型肯不肯在 evidence 里点名图」：`insights_with_chart` 是点名了图的论点数，
    `placed` 是本轮的图里正文放了锚点的张数，`tail` 是剩下落在答案末尾的张数。
    """
    insights = [i for i in payload.get("insights") or [] if isinstance(i, dict)]
    anchored = set(_FIGURE_ANCHOR_ID_RE.findall(answer or ""))
    figures = list(dict.fromkeys(figure_ids))
    placed = sum(1 for f in figures if f in anchored)
    return {
        "figures": len(figures),
        "insights": len(insights),
        "insights_with_chart": sum(1 for i in insights if _insight_chart_id(i)),
        "placed": placed,
        "tail": len(figures) - placed,
    }


def requested_reference_formula_lines(
    payload: dict[str, Any],
    *,
    user_text: str,
    delegated_task: str,
    answer: str,
) -> list[str]:
    """Show arithmetic already supplied by the selected Agent when the user asked for it.

    A chat payload's ground_facts are a private ledger and the normal renderer
    intentionally omits them. For an exact-reference calculation, that can
    discard the only visible substitutions even though the Agent supplied them.
    Only arithmetic matching its declared result and an operand in the actual
    delegated task may cross into the answer; missing/invalid entries stay out.
    """
    if not re.search(r"公式|计算过程|代入过程|formulas?|calculations?|show your work", user_text, re.IGNORECASE):
        return []
    if re.search(
        r"(?:无需|不需要|不要|不用|不必)(?:列出|展示|提供|给出|写出|给)?(?:计算)?(?:公式|过程)"
        r"|\b(?:without|no)\s+(?:formulas?|calculations?)\b"
        r"|\b(?:do not|don't|dont)\s+(?:show|include|provide|list)\s+(?:formulas?|calculations?)\b",
        user_text,
        re.IGNORECASE,
    ):
        return []
    facts = payload.get("ground_facts")
    if not isinstance(facts, list) or len(delegated_task) > 8192:
        return []
    from bowhead_core.report_ledger import verify_formula

    literals = r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
    source_literals = re.findall(literals, delegated_task)
    if len(source_literals) > 256:
        return []
    source_numbers = {float(literal) for literal in source_literals}
    source_numbers = {value for value in source_numbers if math.isfinite(value)}
    if not source_numbers:
        return []
    lines: list[str] = []
    for fact in facts[:64]:
        if not isinstance(fact, dict):
            continue
        formula = str(fact.get("formula") or "").strip()
        label = " ".join(str(fact.get("label") or "").split())[:64]
        unit = str(fact.get("unit") or "").strip()
        value = fact.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        try:
            numeric_value = float(value)
        except (ValueError, OverflowError):
            continue
        if (
            not formula
            or formula in answer
            or not label
            or not math.isfinite(numeric_value)
            or len(formula) > 128
            or not re.fullmatch(r"[\d \t.,eE+\-*/()×÷（）]+", formula)
            or len(unit) > 16
            or not re.fullmatch(r"[\w%％/／-]*", unit)
        ):
            continue
        formula_numbers = {abs(float(literal)) for literal in re.findall(literals, formula)}
        if any(number not in source_numbers and number not in {1.0, 100.0, 1000.0, 10000.0, 1e8} for number in formula_numbers):
            continue
        if not verify_formula(formula, numeric_value, unit, source_numbers)[0]:
            continue
        lines.append(f"- {label}: `{formula}` = {numeric_value:g}{unit}")
        if len(lines) >= 8:
            break
    return lines


def render_chat_answer_markdown(payload: dict[str, Any], *, calculation_lines: list[str] | None = None) -> str:
    """chat 声明的 payload → 聊天正文（确定性渲染，不做任何取舍判断）。

    旧路只把 `exec_summary` 当最终答案；交付面自决后 peer 说 chat 的 payload 可能还带
    kpis / insights / recommendations / caveats——那是它想在聊天里说的内容，不能丢。
    这里按固定顺序拼成 markdown 交给 Lead（Lead 的 directive 是「直接使用」）。
    """
    parts: list[str] = []
    summary = str(payload.get("exec_summary") or "").strip()
    if summary:
        parts.append(summary)
    lines = []
    for k in payload.get("kpis") or []:
        if not isinstance(k, dict) or not k.get("label"):
            continue
        label = str(k.get("label")).strip()
        raw_value = k.get("value")
        value = "" if raw_value is None else str(raw_value).strip()
        # review2-R4: skip valueless KPIs; "- GMV:" is line noise, not information.
        if not value:
            continue
        # Pure deduplication, not judgment: omit a fact already fully stated in summary.
        if label and label in summary and value in summary:
            continue
        delta = str(k.get("delta") or "").strip()
        # review2-R2: ASCII punctuation; full-width "：（）" mixes scripts in English turns (`- Revenue：5.2B（+12%）`).
        lines.append(f"- {label}: {value}" + (f" ({delta})" if delta else ""))
    if lines:
        parts.append("\n".join(lines))
    if calculation_lines:
        parts.append("\n".join(calculation_lines))
    # Figures follow arguments: insight evidence names a chart (`chart_section_id`, a model-bound structured field),
    # so put `[figure:<id>]` alone after that argument; frontend renders it there. Observed on 116: chat models never
    # wrote body anchors manually (0 in 30 hours), piling charts at the end. Evidence is required in every agent result contract,
    # independent of a prompt remembering to teach it. Omit charts already named in body or cited twice; unnamed charts stay at the end.
    placed = set(_FIGURE_ANCHOR_ID_RE.findall(summary))
    for ins in payload.get("insights") or []:
        if not isinstance(ins, dict):
            continue
        title = str(ins.get("title") or "").strip()
        thesis = str(ins.get("thesis") or "").strip()
        narr = str(ins.get("narrative") or "").strip()
        body = " ".join(x for x in (thesis, narr) if x)
        if title and body:
            parts.append(f"**{title}** {body}")
        elif title or body:
            parts.append(title or body)
        placed.update(_FIGURE_ANCHOR_ID_RE.findall(narr))
        fig = _insight_chart_id(ins)
        if fig and fig not in placed and (title or body):
            parts.append(f"[figure:{fig}]")
            placed.add(fig)
    # review-R6: add no language-specific connective labels ("建议：""注意："); peer content already follows user language,
    # and adding Chinese headings contaminates an English final_answer when Lead directive says "use directly".
    recs = [str(r.get("action")).strip() for r in (payload.get("recommendations") or []) if isinstance(r, dict) and r.get("action")]
    if recs:
        parts.append("\n".join(f"- {a}" for a in recs))
    # Review item 6: machine markers must not appear in chat replies (users would see `> [plan_gap] …`).
    from bowhead_core.analysis_payload import strip_caveat_marker as _strip_cav_marker

    cavs = [_strip_cav_marker(str(c)).strip() for c in (payload.get("caveats") or []) if str(c).strip()]
    if cavs:
        # review2-R3: identical bullets for recommendations and limitations make "data not backfilled" read as an action item.
        # Distinguish with Markdown blockquotes: language-neutral, no connective labels needed.
        parts.append("\n".join(f"> {c}" for c in cavs))
    return "\n\n".join(parts).strip()
