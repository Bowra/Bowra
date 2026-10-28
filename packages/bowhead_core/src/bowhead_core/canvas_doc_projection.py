"""画布「文档形投影」——composer 固定骨架 → 文档（canvas-elements sprint，discovery §5 第一步）。

`_build_sections_from_payload` 把 `AnalysisPayload` 装订成固定骨架：
eyebrow / h1 / meta / narrative(导语) / kpi_row / insight_card×N / recommendations /
narrative(caveats) / sources。其中 insight_card / recommendations / eyebrow / meta 四个 kind
天生是卡片形——色条、INSIGHT 标、置信度药丸、P0 药丸——「AI 感」的主体就是它们。

本模块是同一份段的另一种投影，**只用既有 kind**（h1 / narrative / kpi_row / chart /
code_block / sources），一行前端不改：

- eyebrow + meta（更新于 / N 个数据系统 / Bowhead 生成）→ h1 之后一段 `style=byline` 的斜体署名；intent 枚举药丸丢
- kpi_row 不动（它已经是研报的数字带；做成 markdown 表更差，spike 试过）
- insight_card → `## 标题` + 引用块论点（带影响量 / 非 high 的置信度）+ 正文 + 证据：
  图 = 块级 `[figure:id]` 锚（FE 在锚点内联该图并在顶层跳过它）+ 「图N：」图注；
  callout = 加粗结论 + 数据行；computation / table 没有同源图 → 斜体标题
- recommendations → `## 建议` 编号列表（`style: "recommendations"`，数字守卫按 FU-1 同口径跳过）
- caveats / sources / chart / code_block 不动

挂在 `compose_canvas_from_payload` 里 **chart 校验/自动出图之后、Phase-F 绑 [N] 之前**：
前者保证 `chart_section_id` 已是真 id（自动出图的段也已落库拿到 id），后者让 `[N]`
照常绑进文档形的 narrative（Phase-F 走 `markdown` 键）。三条收尾路（peer 返回时的 ψ-E
eager 卡片路 / writer 余量路 / Lead 计划执行路）都汇到那一个函数，所以只挂一处。

功能已正式发布；权限、作用域和存储就绪校验仍须执行。"""

from __future__ import annotations

from typing import Any

from bowhead_core.report_chrome_text import chrome_text

# Card fields with no document destination, verified dispensable (116 corpus, 1586 cards):
# display_hint empty in 98%; source_refs present in 6% (Phase-F [N] already covers the same semantics).
_IMPACT_DIR = {"+": "+", "-": "−", "up": "+", "down": "−"}


def _impact_phrase(impact: Any, lang: str) -> str:
    if not isinstance(impact, dict):
        return ""
    q = impact.get("quantity")
    if q in (None, ""):
        return ""
    unit = str(impact.get("unit") or "")
    direction = _IMPACT_DIR.get(str(impact.get("direction") or ""), "")
    return chrome_text("doc_impact", lang, value=f"{direction}{q}{unit}")


def _confidence_phrase(confidence: Any, lang: str) -> str:
    c = str(confidence or "").lower()
    if c == "med":
        return chrome_text("doc_confidence_med", lang)
    if c == "low":
        return chrome_text("doc_confidence_low", lang)
    return ""  # high / default: omit; 83% of corpus is high, adding only noise


def _insight_markdown(
    card: dict[str, Any],
    *,
    fig_counter: list[int],
    valid_chart_ids: set[str] | None,
    lang: str,
) -> str:
    parts: list[str] = []
    title = str(card.get("title") or "").strip()
    thesis = str(card.get("thesis") or "").strip()
    narrative = str(card.get("narrative") or "").strip()
    if title:
        parts.append(f"## {title}")
    quote_bits = [b for b in (thesis, _impact_phrase(card.get("impact"), lang), _confidence_phrase(card.get("confidence"), lang)) if b]
    if quote_bits:
        parts.append("> " + " — ".join(quote_bits))
    if narrative:
        parts.append(narrative)
    for ev in card.get("evidence") or []:
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "callout")
        caption = str(ev.get("caption") or "").strip()
        sub = str(ev.get("subcaption") or "").strip()
        if kind == "chart":
            cid = str(ev.get("chart_section_id") or "")
            if cid and (valid_chart_ids is None or cid in valid_chart_ids):
                fig_counter[0] += 1
                parts.append(f"[figure:{cid}]")
                cap = caption or sub
                if cap:
                    parts.append(chrome_text("doc_figure_caption", lang, n=fig_counter[0], caption=cap))
            elif caption:
                parts.append(f"*{caption}*")
        elif kind == "callout":
            text = str(ev.get("callout_text") or "").strip()
            if text and caption:
                parts.append(f"**{text}**\n{caption}" + (f"（{sub}）" if sub else ""))
            elif text:
                parts.append(f"**{text}**")
            elif caption:
                parts.append(caption)
        else:  # computation without a matching figure / table: cards show collapse+open; documents keep titles
            label = caption or sub or chrome_text("doc_computation" if kind == "computation" else "doc_table", lang)
            parts.append(f"*{label}*")
    return "\n\n".join(parts)


BYLINE_STYLE = "byline"
RECOMMENDATIONS_STYLE = "recommendations"


def byline_markdown(eyebrow: str | None, meta_pills: list[str] | None) -> str:
    """署名行：`*eyebrow · 更新于 … · N 个数据系统 · Bowhead 生成*`；没内容返空串。

    两条路（composer / 写手路 `_emit_report_header`）都落成一段 `style=byline` 的 narrative。
    """
    bits = [str(eyebrow or "").strip()] + [str(x).strip() for x in (meta_pills or [])]
    bits = [b for b in bits if b]
    return "*" + " · ".join(bits) + "*" if bits else ""


def recommendations_markdown(items: list[Any], lang: str = "en-US") -> str:
    """建议 → `## 建议` 编号列表（写手路收尾器与 composer 路共用）。"""
    return _recommendations_markdown(items, lang)


def _recommendations_markdown(items: list[Any], lang: str) -> str:
    lines = [chrome_text("doc_recommendations_heading", lang)]
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        action = str(it.get("action") or "").strip()
        if not action:
            continue
        n += 1
        head = f"{n}. **{action}**"
        tail = [str(x) for x in (str(it.get("priority") or "").upper(), it.get("owner"), it.get("timeline")) if x]
        if tail:
            # Brackets/clause delimiters use chrome table; 116 A6 found fullwidth "（P1 · …）"/"；" in English reports
            head += chrome_text("doc_meta_wrap", lang, value=" · ".join(tail))
        lines.append(head)
        sub = [s for s in (str(it.get("rationale") or "").strip(),) if s]
        expected = str(it.get("expected_impact") or "").strip()
        if expected:
            sub.append(chrome_text("doc_expected", lang, value=expected))
        if sub:
            lines.append("   " + chrome_text("doc_clause_sep", lang).join(sub))
    return "\n".join(lines)


def _split_lead(lead: dict[str, Any]) -> list[dict[str, Any]]:
    from bowhead_core.report_chapters import split_chapters

    chapters = split_chapters(str(lead.get("markdown") or ""))
    if len(chapters) <= 1:
        return [dict(lead)]
    out: list[dict[str, Any]] = []
    for ch in chapters:
        # ⚠ Strip `unverified_total` with `unverified_numbers`; stripping only the list gives each subchapter
        # the whole chapter's denominator, counting the trust denominator N times.
        sec = {k: v for k, v in lead.items() if k not in ("markdown", "style", "unverified_numbers", "unverified_total")}
        sec["markdown"] = ch.markdown
        sec["style"] = "executive" if ch.is_lead else "insight"
        if ch.title:
            sec["_meta"] = {**(sec.get("_meta") or {}), "chapter_title": ch.title}
        out.append(sec)
    return out


def document_chrome_sections(
    payload: dict[str, Any],
    *,
    lang: str = "en-US",
    meta_pills: list[str] | None = None,
    chapter_mode: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """外壳（不含正文）走 composer 同一条骨架 + 投影（tech-design-chapter-split §2.6）。

    亲笔路收尾器此前自己手拼 eyebrow / meta / 建议 / 局限性——第二份实现。这里只从 payload 取
    title / eyebrow / recommendations / caveats（caveats 调用方先过 `non_provenance_caveats`，
    带前缀的可信度提示另有专属段），过 `_build_sections_from_payload` → `project_document`。
    返回 (head, tail)：head = h1 + 署名段（提到最前）；tail = 建议列表 + 局限性（殿后）。
    KPI 带（`_emit_kpi_band`，设计语义 ▲▼/tone）与 sources（registry upsert）不在这里。
    """
    from bowhead_core.canvas_composer import _build_sections_from_payload

    chrome = {k: payload.get(k) for k in ("title", "eyebrow", "recommendations", "caveats") if payload.get(k)}
    if not chrome:
        return [], []
    secs = _build_sections_from_payload(chrome, None, tool_invocations=None, lang=lang, chapter_mode=chapter_mode)
    if not secs and chrome.get("title"):
        # `_SHELL_ONLY_KINDS` drops title-only skeletons (preventing instructions as titles); here title is
        # a real payload title, so standalone h1 matches today's finalizer behavior.
        secs = [{"kind": "h1", "text": str(chrome["title"])}]
        if chrome.get("eyebrow") and not chapter_mode:
            secs.insert(0, {"kind": "eyebrow", "text": str(chrome["eyebrow"])})
    projected = project_document(secs, lang=lang, meta_pills=meta_pills, chapter_mode=chapter_mode)
    # After §2.4 retirement, eyebrow / meta / byline are no longer produced. Still select these as head
    # for compatibility with historical persisted sections, without claiming new ones are generated.
    head = [x for x in projected if x.get("kind") in ("h1", "eyebrow", "meta") or x.get("style") == BYLINE_STYLE]
    tail = [x for x in projected if x not in head and x.get("kind") not in ("kpi_row", "sources")]
    return head, tail


def project_document(
    sections: list[dict[str, Any]],
    *,
    lang: str = "en-US",
    valid_chart_ids: set[str] | None = None,
    meta_pills: list[str] | None = None,
    chapter_mode: bool = False,
) -> list[dict[str, Any]]:
    """固定骨架的段列表 → 文档形段列表。纯函数，不改入参。

    `valid_chart_ids`：能当图锚目标的 chart section id 集合；None = 信任 evidence 里的
    `chart_section_id`（调用方已做过校验）。`meta_pills`：写手路 header 那排 meta
    （更新于 / N 个数据系统 / Bowhead 生成），折进导语首行斜体署名——信息留、药丸不留；
    骨架里 meta 段自带的 intent 枚举（REPORT / CAUSAL…）是内部名，丢。署名行只看
    `chapter_mode`（章不立文档级 chrome，与骨架同口径），不看骨架里有没有 meta 段——
    eager 路的 intent 常为 None、骨架没 meta 段，但那也是一份完整报告。
    多报告轮（同 turn 两套 h1/kpi_row）只折叠第一套 chrome，第二套原位保留。
    """
    out: list[dict[str, Any]] = []
    fig_counter = [0]

    h1 = next((s for s in sections if s.get("kind") == "h1"), None)
    eyebrow = next((s for s in sections if s.get("kind") == "eyebrow"), None)
    lead = next((s for s in sections if s.get("kind") == "narrative" and s.get("style") == "executive"), None)
    kpi = next((s for s in sections if s.get("kind") == "kpi_row"), None)
    consumed = {id(x) for x in (h1, eyebrow, lead, kpi) if x is not None}

    if h1 is not None:
        out.append(dict(h1))
    # Byline retired (canvas-elements tech-design-document-minimalism §2.4, user decision on 2026-09-07):
    # updated-at X / N data systems / Bowhead-generated text is system-assembled metadata, not payload-driven.
    # Metadata belongs to document chrome, not prose; document tools do not print last-edited timestamps inside content.
    # Retain `byline_markdown` / `BYLINE_STYLE` / `_report_meta_pills`: exports/chat may still use them,
    # and readers still recognize historical byline sections (`_chrome_piece_of`, etc.), but no new ones are produced.
    if lead is not None and str(lead.get("markdown") or "").strip():
        # canvas-elements step 2 (tech-design-chapter-split open question 3, user approved): peers sometimes write chapters into
        # exec_summary; split intro by `##` with the authored path's pure function and shape (executive intro + insight chapters).
        out.extend(_split_lead(lead))
    if kpi is not None:
        out.append(dict(kpi))

    for s in sections:
        if id(s) in consumed:
            continue
        kind = s.get("kind")
        if kind == "meta":
            continue
        if kind == "eyebrow":
            # Per §2.4, remove eyebrow→byline projection too (eyebrow itself retired in batch A;
            # only historical payloads can reach this). Discard rather than render in prose: it is a label, not content.
            continue
        if kind == "insight_card":
            out.append(
                {
                    "kind": "narrative",
                    "style": "insight",
                    "markdown": _insight_markdown(s, fig_counter=fig_counter, valid_chart_ids=valid_chart_ids, lang=lang),
                }
            )
            continue
        if kind == "recommendations":
            out.append(
                {
                    "kind": "narrative",
                    "style": RECOMMENDATIONS_STYLE,
                    "markdown": _recommendations_markdown(list(s.get("items") or []), lang),
                }
            )
            continue
        out.append(dict(s))
    return out
