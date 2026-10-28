"""Apply deterministic consistency checks after a report is composed.

Mark references to historical figures that were not recalculated in this turn.
Compare matching KPI labels across narrative, chart titles and KPI surfaces
using canonical units. These checks complement source-number verification and
payload authorization; they annotate or record mismatches without changing
numbers or rejecting the completed report.

BOWHEAD_STALE_FIGURE_NOTE defaults to 1. BOWHEAD_CROSSFACE_NUMBERS accepts
``off``, ``shadow`` and ``annotate``, defaulting to ``shadow``.
"""

from __future__ import annotations

import logging
import os
import re
from types import SimpleNamespace
from typing import Any

from bowhead_core.metrics import inc as _metric_inc
from bowhead_core.report_ledger import extract_numbers, unit_class

logger = logging.getLogger(__name__)

# ── flags ─────────────────────────────────────────────────────────────────────


def stale_note_enabled() -> bool:
    return os.environ.get("BOWHEAD_STALE_FIGURE_NOTE", "1").strip() != "0"


def crossface_mode() -> str:
    m = os.environ.get("BOWHEAD_CROSSFACE_NUMBERS", "shadow").strip().lower()
    return m if m in ("off", "shadow", "annotate") else "shadow"


# ── stale-figure note(WP-B2)────────────────────────────────────────────────

# Anchor rule matches report_writer_loop._ANCHOR_ONLY_LINE; caption rule matches its _CAPTION_LINE.
_ANCHOR_ONLY_LINE = re.compile(r"^[ \t]*\[figure:([a-zA-Z0-9_]+)\][ \t]*$")
_CAPTION_LINE = re.compile(r"^[ \t]*(?:\*\*)?图\s*\d")
_FIGURE_REF_RE = re.compile(r"\[figure:([a-zA-Z0-9_]+)\]")

# Avoid demonstrative pronoun "该图": stripping [figure:] in IM mirror leaves dangling reference (design P2-5).
STALE_NOTE = "> ⚠ 上图为本轮之前生成,数据未在本轮重算"


def insert_stale_figure_notes(md: str, stale_ids: set[str]) -> tuple[str, int]:
    """在 stale 图锚块(锚行 + 可选紧随 caption 行)之后插入独立 blockquote 警示。

    警示行**前后各补一个空行**——CommonMark lazy continuation 会把无空行紧随的
    caption/正文吸进 blockquote(design review P1-1;LLM 常态是锚行+caption 连续
    无空行)。幂等:锚块后 2 行内已有该警示 → 不重插。

    Returns (new_md, n_inserted)。
    """
    if not md or not stale_ids:
        return md, 0
    lines = md.split("\n")
    out: list[str] = []
    inserted = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _ANCHOR_ONLY_LINE.match(line)
        i += 1
        if not m or m.group(1) not in stale_ids:
            continue
        # Anchor block absorbs immediate **图N**: caption; insert warning afterward (P1-1).
        if i < len(lines) and _CAPTION_LINE.match(lines[i]):
            out.append(lines[i])
            i += 1
        # Idempotent: same warning within next 2 lines → skip.
        if any(ln.strip() == STALE_NOTE for ln in lines[i : i + 2]):
            continue
        if out and out[-1].strip() != "":
            out.append("")
        out.append(STALE_NOTE)
        inserted += 1
        if i < len(lines) and lines[i].strip() != "":
            out.append("")
    return "\n".join(out), inserted


# ── Cross-face single-metric consistency (WP-C) ──────────────────


def _unwrap_num_markers(text: str) -> str:
    """把退役标记 bake 成「值+单位」纯文本，供 extract_numbers 扫。

    citation-audit M3-5：原来这里自带一条 unwrap 正则（design review P2-1 曾特意
    警告「三代格式并存，勿自写别的正则」——正是债5 的症状）。现在改调**单一口径**
    `citation_marks.strip_legacy`：与 canvas/chat/IM/迁移/FE 同一份文法，双端 golden
    互锁。库内已无退役标记（M1 停写 + M2 洗净），此调用实为兜底；对纯 gen3 文本是
    幂等 no-op，`值 [N]` 里的 `[N]` 不受影响（strip_legacy 不碰 gen3）。
    """
    from bowhead_core.citation_marks import strip_legacy

    return strip_legacy(text or "")


_WS_RE = re.compile(r"\s+")


def _norm_label(s: str) -> str:
    """label 归一:全角→半角、去空白、lower。"""
    out: list[str] = []
    for ch in s or "":
        o = ord(ch)
        if o == 0x3000:
            o = 32
        elif 0xFF01 <= o <= 0xFF5E:
            o -= 0xFEE0
        out.append(chr(o))
    return _WS_RE.sub("", "".join(out)).lower()


def _is_year_like(v: float) -> bool:
    return float(v).is_integer() and 1900 <= v <= 2100


def _classed_numbers(text: str) -> list[tuple[str, float, float]]:
    """text → [(unit_class, canonical_value, raw_value)]。

    只留**带单位**的数字(class != none)——无单位数字(裸整数/年份/编号)噪声太大,
    不参与跨面比对(设计:比对只在同 unit_class 内)。canonical = raw × 自己声明
    单位的 scale(design review P1-4:各值按自身单位归一,绝不做类内自由 scale
    搜索——那会豁免 4万 vs 40万 的真错)。
    """
    out: list[tuple[str, float, float]] = []
    for v, unit in extract_numbers(text):
        if _is_year_like(v):
            continue
        cls, scale = unit_class(unit)
        if cls == "none":
            continue
        out.append((cls, v * scale, v))
    return out


def _kpi_metrics(kpi_cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """kpi_row cells → [{label, norm, numbers:[(cls,canon,raw)], unresolved}]。

    两条 kpi_row 生成路的 cell 形状都收:writer-loop band = {lbl,val,sub?},
    composer = {lbl,val,delta?}。delta/sub 里常是增速类指标的真身(review P2-2),
    一并纳入该 label 的授权值集。
    """
    out: list[dict[str, Any]] = []
    for cell in kpi_cells or []:
        if not isinstance(cell, dict):
            continue
        label = str(cell.get("lbl") or cell.get("label") or "").strip()
        if not label:
            continue
        texts = [str(cell.get(k) or "") for k in ("val", "value", "delta", "sub")]
        nums: list[tuple[str, float, float]] = []
        has_any_number = False
        for t in texts:
            if t and extract_numbers(t):
                has_any_number = True
            nums.extend(_classed_numbers(t))
        out.append(
            {
                "label": label,
                "norm": _norm_label(label),
                "numbers": nums,
                # Numeric val with no classifiable units → blind spot (shadow records unresolved).
                "unresolved": has_any_number and not nums,
            }
        )
    return out


def _values_match(a: tuple[str, float, float], b: tuple[str, float, float], *, rel: float = 0.02) -> bool:
    """同 unit_class 且 canonical 值在 rel 容差内。"""
    if a[0] != b[0]:
        return False
    ca, cb = a[1], b[1]
    return abs(ca - cb) <= max(rel * max(abs(ca), abs(cb)), 1e-9)


_SKIP_LINE_PREFIXES = ("|", ">", "#")


def find_crossface_mismatches(
    kpi_cells: list[dict[str, Any]],
    narrative_texts: list[str],
    chart_texts: list[str],
    chat_text: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """跨面单指标一致性扫描(纯函数,确定性)。

    以 KPI label 为锚。某面一行含 label 且行内有该 label 单位类的数字,
    **且 ∀ 行内该类数字均不匹配 KPI 值集** → mismatch(design review P1-2:
    ∃ 语义对「由2.3%升至17.2%」这类对比句全是 FP;行内任一数字吻合即视为
    corroborated 不报)。不报缺失(没提该 KPI 不算错)。

    Returns (mismatches, n_unresolved)。mismatch = {label, face, kpi_values,
    found, line}(值为 raw 值,便于日志可读)。
    """
    metrics = [m for m in _kpi_metrics(kpi_cells) if m["norm"]]
    n_unresolved = sum(1 for m in metrics if m["unresolved"])
    anchored = [m for m in metrics if m["numbers"]]
    if not anchored:
        return [], n_unresolved

    faces: list[tuple[str, list[str]]] = []
    nar_lines: list[str] = []
    for t in narrative_texts:
        for ln in _unwrap_num_markers(t).split("\n"):
            st = ln.strip()
            if not st or st.startswith(_SKIP_LINE_PREFIXES):
                continue
            nar_lines.append(st)
    faces.append(("narrative", nar_lines))
    faces.append(("chart", [t.strip() for t in chart_texts if t and t.strip()]))
    if chat_text:
        faces.append(("chat", [ln.strip() for ln in _unwrap_num_markers(chat_text).split("\n") if ln.strip()]))

    mismatches: list[dict[str, Any]] = []
    for metric in anchored:
        kpi_classes = {n[0] for n in metric["numbers"]}
        for face_name, face_lines in faces:
            for ln in face_lines:
                if metric["norm"] not in _norm_label(ln):
                    continue
                found = [n for n in _classed_numbers(ln) if n[0] in kpi_classes]
                if not found:
                    continue
                corroborated = any(_values_match(f, k) for f in found for k in metric["numbers"])
                if corroborated:
                    continue
                mismatches.append(
                    {
                        "label": metric["label"],
                        "face": face_name,
                        "kpi_values": [n[2] for n in metric["numbers"]],
                        "found": [f[2] for f in found],
                        "line": ln[:120],
                    }
                )
                break  # Once per (metric,face).
    return mismatches, n_unresolved


_ANNOTATE_CAP = 5


def build_crossface_footer(mismatches: list[dict[str, Any]]) -> str:
    """annotate 模式的 footer(照 number_verifier.annotate_with_warning 哲学:
    附注不改正文,cap 条数)。只列 narrative/chart 面(chat 面永远 shadow-only)。"""
    rows = [m for m in mismatches if m["face"] in ("narrative", "chart")][:_ANNOTATE_CAP]
    if not rows:
        return ""
    lines = ["", "> ⚠ 一致性提示:以下指标在报告不同位置出现了不一致的数值,请以数据源核对为准:"]
    for m in rows:
        face_cn = "正文" if m["face"] == "narrative" else "图表标题"
        kpi_v = "/".join(str(v) for v in m["kpi_values"][:3])
        found_v = "/".join(str(v) for v in m["found"][:3])
        lines.append(f"> - 「{m['label']}」{face_cn}({found_v})与关键指标({kpi_v})不一致")
    return "\n".join(lines)


# ── Final reconciliation pass (WP-D hook from lead_orchestrator done branch) ──


def _chart_face_text(section: Any) -> str:
    p = section.payload if isinstance(section.payload, dict) else {}
    spec_raw = p.get("spec")
    spec: dict[str, Any] = spec_raw if isinstance(spec_raw, dict) else {}
    return " ".join(str(spec.get(k) or "") for k in ("title", "subtitle")).strip()


async def run_final_consistency_pass(
    *,
    store: Any,
    turn_id: str,
    turn_sections: list[Any],
    bus: Any = None,
    revised: list[tuple[Any, set[str]]] | None = None,
    extra_kpi_sections: list[Any] | None = None,
) -> None:
    """done-branch 终稿对账(best-effort:任何异常由调用方 try 吞掉,绝不影响 done)。

    - stale 图:本轮 narrative 引用的 [figure:id] ∉ touched-by-turn 集 → 插警示。
    - cross-face:KPI vs narrative/chart(+chat shadow-only)同名指标比对。
    - narrative 变更合并为**每 section 一次** patch_section(expected_rev CAS,
      冲突跳过)+ 复用 report_tools._emit_section_patched(契约一全消费者)。

    canvas-update-turn 批 B1-3：``revised`` = 本轮修订的上一轮段 + 各自的**继承集**（修订前正文引的
    figure 集，`list_revisions_by_turn` 首条 prev）。修订段的 stale = refs − 继承 − touched：沿用旧图不标、
    新引一张上一轮旧图才标。``extra_kpi_sections`` = 文档块的 kpi_row（修订轮被 patch 的 KPI 带不在
    appended 里，没有它 cross-face 就没有 KPI 面可比）。两参缺省 ⇒ 旧行为逐字节不变。
    """
    stale_on = stale_note_enabled()
    cf_mode = crossface_mode()
    revised = [(s, set(inh)) for s, inh in (revised or []) if s is not None]
    if (not turn_sections and not revised) or (not stale_on and cf_mode == "off"):
        return

    def _is_narrative(s: Any) -> bool:
        return s.kind == "narrative" and isinstance(s.payload, dict) and isinstance(s.payload.get("markdown"), str)

    narratives = [s for s in turn_sections if _is_narrative(s)]
    revised_narratives = [(s, inh) for s, inh in revised if _is_narrative(s) and s.id not in {n.id for n in narratives}]
    inherited_by_id = {s.id: inh for s, inh in revised_narratives}
    narratives += [s for s, _ in revised_narratives]
    canvas_id = (turn_sections or [revised[0][0]])[0].canvas_id

    # Full-canvas id→Section index (include_hidden=True: referenced historical chart may be hidden by
    # chat-mode turn; default filtering would miss it, design review P1-3).
    all_sections = await store.list_sections(canvas_id, include_hidden=True)
    index = {s.id: s for s in all_sections}

    # ── Stale-figure warnings ──
    new_mds: dict[str, str] = {}  # section_id → pending Markdown.
    n_stale_total = 0
    if stale_on:
        touched = await store.section_ids_touched_by_turn(turn_id)
        for s in narratives:
            md = s.payload["markdown"]
            refs = set(_FIGURE_REF_RE.findall(md))
            inherited = inherited_by_id.get(s.id, set())
            stale_ids = {fid for fid in refs if fid in index and fid not in touched and fid not in inherited}
            if not stale_ids:
                continue
            new_md, n = insert_stale_figure_notes(md, stale_ids)
            if n:
                new_mds[s.id] = new_md
                n_stale_total += n
        if n_stale_total:
            _metric_inc("report_stale_figure_notes_total", value=float(n_stale_total))
            logger.info("final_gate.stale_figure_notes turn=%s n=%d", turn_id, n_stale_total)
    # ⚠ Outside **if stale_on:**: observes what RC-8 inspected this turn (dogfood R11);
    # disabling stale warnings must never disable observation.
    logger.info("rc8.final_gate turn=%s appended=%d revised=%d stale_new=%d", turn_id, len(turn_sections), len(revised_narratives), n_stale_total)

    # ── Cross-face consistency (independent failure domain: default-shadow observation exceptions must never
    # destroy stale warnings already computed above, impl review F3) ──
    if cf_mode != "off":
        try:
            kpi_cells: list[dict[str, Any]] = []
            _kpi_seen: set[str] = set()
            for s in list(turn_sections) + list(extra_kpi_sections or []):
                if s.id in _kpi_seen:
                    continue
                _kpi_seen.add(s.id)
                if s.kind == "kpi_row" and isinstance(s.payload, dict):
                    cells = s.payload.get("cells")
                    if isinstance(cells, list):
                        kpi_cells.extend(c for c in cells if isinstance(c, dict))
            # Chart face = this-turn charts + historical charts referenced by this-turn narrative (all visible
            # chart surfaces in this report).
            chart_ids = {s.id for s in turn_sections if s.kind == "chart"}
            for s in narratives:
                for fid in _FIGURE_REF_RE.findall(s.payload["markdown"]):
                    sec = index.get(fid)
                    if sec is not None and sec.kind == "chart":
                        chart_ids.add(fid)
            chart_secs = (index.get(sid) for sid in chart_ids)
            chart_texts = [t for cs in chart_secs if cs is not None and (t := _chart_face_text(cs))]
            nar_texts = [new_mds.get(s.id) or s.payload["markdown"] for s in narratives]
            chat_text = None
            if bus is not None:
                st = getattr(bus, "_pi_turn_state", None)
                if isinstance(st, dict):
                    chat_text = st.get("final_answer")

            mismatches, n_unresolved = find_crossface_mismatches(kpi_cells, nar_texts, chart_texts, chat_text)
            if n_unresolved:
                _metric_inc("report_crossface_unit_unresolved_total", value=float(n_unresolved))
            for m in mismatches:
                _metric_inc("report_crossface_mismatch_total", labels={"mode": cf_mode, "face": m["face"]})
            if mismatches:
                logger.info(
                    "final_gate.crossface mode=%s turn=%s mismatches=%s",
                    cf_mode,
                    turn_id,
                    [{k: m[k] for k in ("label", "face", "kpi_values", "found")} for m in mismatches[:10]],
                )
            if cf_mode == "annotate" and narratives:
                footer = build_crossface_footer(mismatches)
                if footer:
                    # Last report section (multi-writer narratives[0] is intro; footer belongs at end).
                    # —— impl review F6)
                    target = narratives[-1]
                    base = new_mds.get(target.id) or target.payload["markdown"]
                    if footer not in base:  # Idempotent.
                        new_mds[target.id] = base + "\n" + footer
        except Exception as cf_exc:
            logger.warning("final_gate.crossface_failed turn=%s err=%s", turn_id, cf_exc)

    # ── Persist + emit (contract 1) ──
    if not new_mds:
        return
    from bowhead_core.canvas_store import CanvasRevConflictError, CanvasSectionSupersededError

    for s in narratives:
        md = new_mds.get(s.id)
        if md is None:
            continue
        try:
            stored = await store.patch_section(
                section_id=s.id,
                patch={"markdown": md},
                expected_rev=s.rev,
                by_turn_id=turn_id,
                canvas_id=canvas_id,  # This canvas only (revised sections from other turns; bare ID risks cross-session).
                reject_superseded=True,  # Reject folded sections in transaction: changes invisible to user.
            )
        except CanvasRevConflictError:
            logger.info("final_gate.rev_conflict_skip turn=%s section=%s", turn_id, s.id)
            continue
        except CanvasSectionSupersededError:
            logger.info("final_gate.superseded_skip turn=%s section=%s", turn_id, s.id)
            continue
        if stored is None:
            continue
        # Reuse write_report patched emit (full section payload supports frontend late-join
        # upsert + IM mirror). Lazy import avoids agents-package cycle.
        from bowhead_core.agents.report_tools import _emit_section_patched

        await _emit_section_patched(SimpleNamespace(parent_bus=bus, turn_id=turn_id), canvas_id, stored)
