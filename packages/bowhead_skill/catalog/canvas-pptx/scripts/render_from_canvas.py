"""Canvas PPTX entrypoint: content-driven standard export or explicit template.

Without an allowed, existing template_path, render_content paginates all supplied
sections in order. The historical role-based functions below serve explicit
compatible templates only; their slot limits do not apply to standard downloads.

加载用户 baseline template (canvas-default.pptx, web claude.ai 一次性产的精致 7-slide),
按 slide index + shape index 找具体 textbox 角色, 替换为 canvas section 真数据.
保留 template 的 master/theme/layouts/排版/字体/色板/编号系统/双横线 motif.

Template slide 角色:
  1 cover:    [0]eyebrow [1]DOC# [2]title [3]subtitle [4]brand [5]date
  2 exec:     [1]section_label [2]hero_italic [3]narrative [5,8,11]FINDING# [6,9,12]finding text [14]RECOMMENDATION [15]rec_text
  3 kpi:      [1]section [2]title [3]deck [5,9,13,17]kpi_label [6,10,14,18]kpi_value [7,11,15,19]kpi_sub [20]summary
  4 chart:    [1]section [2]title [3]eng_subtitle [4]CHART [6]I N S I G H T [7]insight_thesis [8]insight_narrative
  5 trend:    same as 4
  6 recs:     [1]section [2]title [3]deck + 3 blocks ([7,17,27]priority [8,18,28]action_title [9,19,29]desc [11,21,31]owner [13,23,33]timeline)
  7 closing:  [0]FIN [1]Thank you [2]deck [3]email [4]brand [5]date

默认模板最多 7-slide。缺少真实图表/图片时删除对应视觉页；少 finding 时清空多余。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt
except ImportError:
    sys.exit(2)


# Palette (aligned with the template)
C_INK = RGBColor(0x1A, 0x19, 0x16)
C_INK2 = RGBColor(0x57, 0x54, 0x4D)
C_INK3 = RGBColor(0x8B, 0x87, 0x7E)
C_ACCENT = RGBColor(0x2E, 0x5D, 0x4F)
C_POS = RGBColor(0x3F, 0x7A, 0x65)
C_ERR = RGBColor(0xB5, 0x44, 0x3B)


SCRIPT_DIR = Path(__file__).resolve().parent


def _packaged_template() -> Path:
    """Read the fixed edition manifest without importing application packages."""
    root = SCRIPT_DIR.parent / "templates"
    manifest = json.loads((root / "default.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "template"} or manifest["schema_version"] != 1:
        raise ValueError("Invalid packaged template manifest")
    name = manifest["template"]
    if name not in {"canvas-default.pptx", "community-neutral.pptx"}:
        raise ValueError("Unknown packaged template")
    template = root / name
    if not template.is_file() or template.resolve().parent != root.resolve():
        raise ValueError("Packaged template is missing or outside its directory")
    return template


TEMPLATE = _packaged_template()

# Report exports must not inherit template vendor branding, marketing text, or
# contact details. Clear these fields rather than attributing user reports to
# the template vendor. The current export payload supplies no team branding.
_VENDOR_BRAND_TOKENS = ("bowhead", "北极九章")


def _is_vendor_brand_text(value: object) -> bool:
    """判定一段模板文案是不是厂商品牌串（页脚署名 / 收尾页营销文案 / 联系方式）。

    按 normalize 后的**子串**判，不做等值比较 —— 模板里同一个品牌串有
    "B O W H E A D · 北极九章" 和 "B O W H E A D  A N A L Y T I C S · 北极九章"
    两种字距写法，等值判据只能逮住其中一种。
    """
    normalized = _normalize_residue_text(value)
    if not normalized:
        return False
    return any(token in normalized for token in _VENDOR_BRAND_TOKENS)


# ── A-1 continued: document properties (docProps) ───────────────────────────────────────────
#
# Found on 2026-08-25 while reviewing a real deck exported from 116 production (1.1.53, including the fixes above):
# **Slides / layouts / masters were clean, but all brand and demo residue remained in document properties**.
#
#   docProps/app.xml   <Company>北极九章 · Bowhead</Company>
#   docProps/core.xml  <dc:creator>Bowhead Analytics</dc:creator>
#                      <cp:lastModifiedBy>Bowhead Analytics</cp:lastModifiedBy>
#                      <dc:title>EA 结算单据合规监控看板</dc:title>   ← **template demo title**
#                      <dc:subject>PptxGenJS Presentation</dc:subject>
#                      <dcterms:created>2026-04-27T04:50:20Z</dcterms:created>
#
# These fields are directly visible in PowerPoint "File → Info", Explorer file properties, and any document system indexing OOXML
# metadata. Every customer report sent out still carried our
# company name and attribution, plus a demo title unrelated to its content.
#
# ★ Why the previous review missed this: acceptance checked that "the package XML (slide + layout + master)
# contained no brand strings", while `_find_default_template_residue` scanned only shape text in prs.slides and
# prs.slide_layouts. docProps was outside both checks.
# (The same lesson as the units incident: **the acceptance scope we define creates our own blind spots**.)
_APP_XML_PARTNAME = "/docProps/app.xml"
_COMPANY_RE = re.compile(r"(<Company>)(.*?)(</Company>)", re.DOTALL)


def _scrub_document_properties(prs: Any, *, deck_title: str, stamped: Any) -> None:
    """把文档属性从模板的厂商 / demo 值改写成这份报告自己的值。

    只写「本报告为真」的东西：标题取 canvas 的 h1，时间戳取本次导出时间。
    署名类字段（creator / lastModifiedBy / company）**一律清空**而不是填我们的名字
    —— 与页脚同一个决定：私有化部署里 deck 属于客户。要署名只能走团队品牌配置。
    """
    core = prs.core_properties
    core.title = deck_title or ""
    core.subject = ""
    core.author = ""
    core.last_modified_by = ""
    core.keywords = ""
    core.category = ""
    core.comments = ""
    core.content_status = ""
    core.created = stamped
    core.modified = stamped
    core.revision = 1

    # <Company> lives in docProps/app.xml; python-pptx has no corresponding API, so edit the part blob directly.
    # ⚠ Rewrite the element **only if it already exists**; do not add it. Inserting an empty Company element into a template
    # that lacks one would only cause problems for other parsers.
    for part in prs.part.package.iter_parts():
        if str(part.partname) != _APP_XML_PARTNAME:
            continue
        try:
            text = part.blob.decode("utf-8")
        except Exception:  # noqa: S110 — Property cleanup failure must not ruin the entire export.
            return
        if not _COMPANY_RE.search(text):
            return
        part._blob = _COMPANY_RE.sub(r"\1\3", text).encode("utf-8")
        return


def _document_property_residue(prs: Any, *, deck_title: str) -> list[str]:
    """文档属性里的厂商品牌 / 模板 demo 残留（fail-closed 扫描的一部分）。

    与 slide 侧同一条规矩：**没被本次 canvas 数据证成的模板文案，一律算残留**。
    这里额外多一条厂商品牌判据 —— 品牌串不会出现在 canvas 数据里，所以「有就是错」。
    """
    core = prs.core_properties
    fields = {
        "core.title": core.title,
        "core.subject": core.subject,
        "core.author": core.author,
        "core.last_modified_by": core.last_modified_by,
        "core.keywords": core.keywords,
        "core.category": core.category,
        "core.comments": core.comments,
    }
    for part in prs.part.package.iter_parts():
        if str(part.partname) == _APP_XML_PARTNAME:
            try:
                found = _COMPANY_RE.search(part.blob.decode("utf-8"))
            except Exception:  # noqa: S110
                found = None
            if found:
                fields["app.company"] = found.group(2)
            break

    demo_texts = _default_template_demo_texts()
    allowed = _normalize_residue_text(deck_title)
    residue: list[str] = []
    for name, value in fields.items():
        normalized = _normalize_residue_text(value)
        if not normalized:
            continue
        if _is_vendor_brand_text(value):
            residue.append(f"docprop_brand:{name}={str(value)[:60]}")
            continue
        # Template demo copy: it is absent from this report's title but present in properties, so it came from the template.
        if any(marker in normalized for marker in demo_texts) and normalized not in allowed:
            residue.append(f"docprop_demo:{name}={str(value)[:60]}")
    return residue


# Internal canvas image anchors (same criteria as canvas-docx / canvas-pdf).
_FIG_ANCHOR = re.compile(r"\[figure:[A-Za-z0-9_]+\]")


def _md_text(md: object) -> str:
    """narrative/deck 的 markdown → 可直接进 slide 的纯文本。

    ⚠ 本文件读 markdown 的地方有**三处**（deck 摘要 / exec slide 正文 / KPI 小结），
    第一版只改了中间那处，端到端渲一份 deck 立刻看见锚点仍从另一处漏出来。
    统一收口在这里，下一处新增读点自然继承。
    """
    return _FIG_ANCHOR.sub("", str(md or "")).replace("**", "").replace("\n", " ")


def _set_text(shape, text: str) -> None:
    """替换 shape text 但保留 run-level font props (用第一段第一 run 的字体)."""
    if not shape.has_text_frame:
        return
    tf = shape.text_frame
    # Preserve the first run's font properties.
    first_run = None
    if tf.paragraphs and tf.paragraphs[0].runs:
        first_run = tf.paragraphs[0].runs[0]
        font_face = first_run.font.name
        font_size = first_run.font.size
        font_bold = first_run.font.bold
        font_italic = first_run.font.italic
        try:
            font_color = first_run.font.color.rgb
        except (AttributeError, TypeError):
            font_color = None
    else:
        font_face = font_size = font_bold = font_italic = font_color = None

    # Remove all paragraphs except the first.
    p_list = list(tf.paragraphs)
    for p in p_list[1:]:
        p._element.getparent().remove(p._element)
    p0 = tf.paragraphs[0]
    # Clear everything except the first run in the first paragraph.
    for run in list(p0.runs)[1:]:
        run._r.getparent().remove(run._r)
    if p0.runs:
        run = p0.runs[0]
        run.text = str(text)
        if font_face:
            run.font.name = font_face
        if font_size:
            run.font.size = font_size
        if font_bold is not None:
            run.font.bold = font_bold
        if font_italic is not None:
            run.font.italic = font_italic
        if font_color:
            run.font.color.rgb = font_color
    else:
        p0.text = str(text)


def _fmt_eyebrow(text: str) -> str:
    """eyebrow 中文/英文混合时给空格分隔感. ASCII 字符自动 spaced."""
    if not text:
        return ""
    return " ".join(text.upper())


def render_cover(slide, h1, eyebrow, deck, date_str):
    s = slide.shapes
    _set_text(s[0], _fmt_eyebrow(eyebrow.get("text") or ""))
    _set_text(s[1], "")  # The template DOC# is a sample number and must not enter real reports.
    _set_text(s[2], h1.get("text") or "")
    # Strip Markdown markers; clear template demo copy even when there is no deck.
    clean = _md_text(deck.get("markdown"))
    _set_text(s[3], clean[:120])
    # A-1: s[4] is the template's vendor-branded footer; previously untouched, it entered customer reports unchanged.
    _set_text(s[4], "")
    if date_str:
        # The template uses the spaced style "2 0 2 6  ·  0 4  ·  2 7".
        spaced = "  ·  ".join(part for part in date_str.split("-"))
        _set_text(s[5], "  ".join(spaced))


def render_exec(slide, narrative, findings, recommendation, section_idx: int):
    s = slide.shapes
    _set_text(s[1], f"E X E C U T I V E  S U M M A R Y  ·  {section_idx:02d}")
    _set_text(s[2], narrative.get("hero") or "")
    _set_text(s[3], narrative.get("body") or "")
    # Three findings (5/6, 8/9, 11/12).
    finding_label_idxs = [5, 8, 11]
    finding_text_idxs = [6, 9, 12]
    for i in range(3):
        if i < len(findings):
            _set_text(s[finding_label_idxs[i]], f"F I N D I N G  ·  {i + 1:02d}")
            _set_text(s[finding_text_idxs[i]], findings[i])
        else:
            _set_text(s[finding_label_idxs[i]], "")
            _set_text(s[finding_text_idxs[i]], "")
    _set_text(s[15], recommendation or "")


def _kpi_value_font_size(text: str) -> int:
    """Scale KPI text by display width within the template's value box.

    CJK glyphs and ASCII characters have different widths; character count alone
    underestimates long mixed text. Keep width thresholds 9/14/18 aligned with
    Web kpiValueSizeTier and scale the baseline 44-point size proportionally."""
    width = sum(2 if ord(ch) > 127 else 1 for ch in text)
    if width <= 9:
        return 44  # Template default
    if width <= 14:
        return 33
    if width <= 18:
        return 25
    return 21  # Fallback for very long values (Chinese units + percent sign).


def _kpi_label_font_size(text: str) -> int:
    """V2.2.1: KPI label 也按长度调 (label 用 Geist Mono spaced, 中文 7+ 字会换行).

    template demo "TOTAL DOCS" / "COMPLIANCE" 都是英文 ≤ 10 字, 用 10pt OK.
    真 canvas "受监控总单据数" 7 个中文字 + spaced 渲染会溢出 box 宽 2.4".
    """
    # Approximate monospace character count (Chinese characters count as width 2, ASCII as 1).
    n = sum(2 if ord(c) > 127 else 1 for c in text)
    if n <= 12:
        return 10  # Template default
    if n <= 16:
        return 9
    if n <= 20:
        return 8
    return 7


def _resize_run_font(shape, size_pt: int) -> None:
    """重设 shape 第一段所有 run 的字号."""
    if not shape.has_text_frame:
        return
    for p in shape.text_frame.paragraphs:
        for r in p.runs:
            r.font.size = Pt(size_pt)


def render_kpi(slide, kpis, title, deck, summary, section_idx: int):
    s = slide.shapes
    _set_text(s[1], f"K E Y  M E T R I C S  ·  {section_idx:02d}")
    _set_text(s[2], title or "总览指标")
    _set_text(s[3], deck or "")
    # Four cards: label, value, sub.
    cards = [(5, 6, 7), (9, 10, 11), (13, 14, 15), (17, 18, 19)]
    for i, (lbl_i, val_i, sub_i) in enumerate(cards):
        if i < len(kpis):
            k = kpis[i]
            lbl_text = str(k.get("lbl", ""))
            _set_text(s[lbl_i], _fmt_eyebrow(lbl_text))
            # V2.2.1: adapt label size to length too (7+ Chinese characters overflow).
            _resize_run_font(s[lbl_i], _kpi_label_font_size(lbl_text))
            val_text = str(k.get("val", ""))
            _set_text(s[val_i], val_text)
            # V2.2: adapt font size to length.
            _resize_run_font(s[val_i], _kpi_value_font_size(val_text))
            sub = str(k.get("sub", ""))
            _set_text(s[sub_i], sub)
            # Color the sub text.
            tone = k.get("subTone", "")
            if tone == "pos":
                _color_run(s[sub_i], C_POS)
            elif tone == "neg":
                _color_run(s[sub_i], C_ERR)
        else:
            _set_text(s[lbl_i], "")
            _set_text(s[val_i], "")
            _set_text(s[sub_i], "")
    _set_text(s[20], summary or "")


def _color_run(shape, color: RGBColor):
    if not shape.has_text_frame:
        return
    for p in shape.text_frame.paragraphs:
        for r in p.runs:
            r.font.color.rgb = color


def _apply_chart_number_format(chart: Any, number_format: str) -> None:
    """把 numFmt 写到数据标签与值轴上（A-2）。

    python-pptx 只在**存在**对应元素时才有属性；模板里三处都在（plot.dLbls、
    series.dLbls、valAx.numFmt），但仍逐个 try —— 任何一处缺失都不该毁掉整张图。
    `number_format_is_linked = False` 是关键：留 True 的话 PPT 会忽略我们写的值，
    回去读 embedded workbook 的单元格格式（那份 workbook 来自模板）。
    """
    for plot in getattr(chart, "plots", []) or []:
        try:
            labels = plot.data_labels
            labels.number_format = number_format
            labels.number_format_is_linked = False
        except Exception:  # noqa: S110
            pass
        for series in getattr(plot, "series", []) or []:
            try:
                series_labels = series.data_labels
                series_labels.number_format = number_format
                series_labels.number_format_is_linked = False
            except Exception:  # noqa: S110
                pass
    try:
        ticks = chart.value_axis.tick_labels
        ticks.number_format = number_format
        ticks.number_format_is_linked = False
    except Exception:  # noqa: S110
        pass


def render_chart_slide(slide, sec_label_text, title, eng_subtitle, insight_thesis, insight_narrative, chart_data):
    s = slide.shapes
    _set_text(s[1], sec_label_text)
    _set_text(s[2], title)
    _set_text(s[3], eng_subtitle or "")
    # s[4] = chart placeholder; replace_data leaves valAxis unchanged (PowerPoint auto-scales):
    # bar chart data naturally starts near 0 (compliance rates / document counts);
    # for line-chart time series far from 0, fitting the axis to the data makes trends clearer (e.g. 8%-12% monthly changes).
    if chart_data and len(s) > 4 and s[4].has_chart:
        chart = s[4].chart
        try:
            new_data = CategoryChartData()
            new_data.categories = chart_data["labels"]
            new_data.add_series(
                chart_data.get("series_name", "Series"),
                chart_data["values"],
                number_format=chart_data.get("number_format") or "#,##0",
            )
            chart.replace_data(new_data)
        except Exception:  # noqa: S110
            pass
        # A-2: replace_data changes data, not formatting; data labels and the value axis still use the template's hardcoded
        # percentage numFmt. Rewrite it explicitly and disable sourceLinked, or PowerPoint falls back to template values.
        _apply_chart_number_format(chart, chart_data.get("number_format") or "#,##0")
    # Insight thesis (s[7]): separate multiple lines with |.
    # Template thesis uses | for line breaks: "信息技术体系 | 51.6% | 远超均值。"
    _set_text(s[7], (insight_thesis or "").replace("\n", " | "))
    _set_text(s[8], insight_narrative or "")


def render_recommendations(slide, recs, section_idx: int, *, title: str = "行动建议"):
    s = slide.shapes
    _set_text(s[1], f"R E C O M M E N D A T I O N S  ·  {section_idx:02d}")
    _set_text(s[2], title)
    _set_text(s[3], "")
    # Three recommendation blocks.
    blocks = [
        (7, 8, 9, 11, 13),  # P0: priority(7), action_title(8), desc(9), owner(11), timeline(13)
        (17, 18, 19, 21, 23),
        (27, 28, 29, 31, 33),
    ]
    for i, (p_i, t_i, d_i, o_i, tm_i) in enumerate(blocks):
        if i < len(recs):
            r = recs[i]
            prio = r.get("priority", "P1")
            _set_text(s[p_i], " ".join(prio))
            # action: truncate to ≤ 30 characters to prevent overflow.
            action = r.get("action") or ""
            for sep in ("，", ",", "；", "(", "（"):
                if sep in action:
                    action = action.split(sep)[0]
                    break
            _set_text(s[t_i], action[:24])
            # description: rationale or the remaining action text.
            rationale = r.get("rationale") or (r.get("action") or "")[len(action) :].strip("，,；。 ")[:50]
            _set_text(s[d_i], rationale[:50])
            _set_text(s[o_i], (r.get("owner") or "")[:20])
            _set_text(s[tm_i], (r.get("timeline") or "")[:20])
        else:
            for idx in (p_i, t_i, d_i, o_i, tm_i):
                _set_text(s[idx], "")


def render_closing(slide, deck, email_url, date_str):
    s = slide.shapes
    if date_str:
        _set_text(s[0], f"F I N  ·  {' '.join(date_str[:4])}")
    _set_text(s[2], deck or "")
    _set_text(s[3], email_url or "")
    # A-1: s[4] is the vendor-branded footer, as on the cover.
    _set_text(s[4], "")
    if date_str:
        spaced = "  ·  ".join(p for p in date_str.split("-"))
        _set_text(s[5], "  ".join(spaced))


def render_design_summary(prs, design_sections) -> None:
    if not design_sections:
        return
    blank_layout = prs.slide_layouts[-1]
    for idx, design in enumerate(design_sections, start=1):
        slide = prs.slides.add_slide(blank_layout)
        title = design.get("title") or "Editable design"
        width = design.get("canvas_width") or "?"
        height = design.get("canvas_height") or "?"
        artifact_id = design.get("flattened_artifact_id") or ""
        layers = design.get("layers") or []
        layer_text = " / ".join(str(layer.get("text") or "")[:24] for layer in layers if isinstance(layer, dict) and layer.get("type") == "text")

        label = slide.shapes.add_textbox(Inches(0.7), Inches(0.45), Inches(8.4), Inches(0.35))
        label.text = f"E D I T A B L E  D E S I G N  ·  {idx:02d}"
        _resize_run_font(label, 11)
        _color_run(label, C_ACCENT)

        heading = slide.shapes.add_textbox(Inches(0.7), Inches(0.9), Inches(8.4), Inches(0.7))
        heading.text = str(title)
        _resize_run_font(heading, 30)
        _color_run(heading, C_INK)

        body = slide.shapes.add_textbox(Inches(0.7), Inches(1.85), Inches(8.4), Inches(2.2))
        lines = [
            f"Canvas: {width} × {height} px",
            f"Text layers: {layer_text or 'None'}",
        ]
        if artifact_id:
            lines.append(f"Flattened PNG artifact: {artifact_id}")
        else:
            lines.append("Flatten this design from the canvas to export the PNG deliverable.")
        body.text = "\n".join(lines)
        _resize_run_font(body, 16)
        _color_run(body, C_INK2)


def _duplicate_slide(prs, slide):
    """复制一张 slide 添加到末尾, 返回新 slide. python-pptx 没原生 copy_slide."""
    blank_layout = slide.slide_layout
    new_slide = prs.slides.add_slide(blank_layout)
    # Copy shapes XML.
    for shape in slide.shapes:
        el = shape.element
        new_slide.shapes._spTree.append(copy.deepcopy(el))
    return new_slide


def _move_slide_to(prs, slide, target_idx):
    """重排 slide 到 target_idx (0-based)."""
    sld_id_lst = prs.slides._sldIdLst
    slides = list(sld_id_lst)
    # Find the slide's corresponding sldId.
    target_sld_id = None
    for sld_id in slides:
        rId = sld_id.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        if prs.slides._part.related_part(rId) is slide.part:
            target_sld_id = sld_id
            break
    if target_sld_id is None:
        return
    sld_id_lst.remove(target_sld_id)
    sld_id_lst.insert(target_idx, target_sld_id)


def _delete_slide(prs: Any, slide: Any) -> None:
    """删除 slide. python-pptx 不直接支持, XML 级移除."""
    sld_id_lst = prs.slides._sldIdLst
    for sld_id in list(sld_id_lst):
        rId = sld_id.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        if rId and prs.part.related_part(rId) is slide.part:
            sld_id_lst.remove(sld_id)
            prs.part.drop_rel(rId)
            return


def _find_chart_for_insight(
    ic: dict[str, Any],
    visual_sections: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """V2.6 helper: 找 insight 第一个视觉 evidence 对应的 section.

    feedback-0713 F5 (B4 债): chart_section_id 是视觉引用双桶 (chart/image 同
    字段, B4) —— evidence kind 收 chart 与 image 两种, 传入的 visual_sections
    可以是 charts+images 合集。
    """
    for ev in ic.get("evidence") or []:
        if ev.get("kind") not in ("chart", "image"):
            continue
        cid = ev.get("chart_section_id")
        if not cid:
            continue
        return next((s for s in visual_sections if s.get("id") == cid), None)
    return None


def _render_chart_role(
    slide: Any,
    ic: dict[str, Any] | None,
    chart_sec: dict[str, Any] | None,
    sec_label: str,
    section_idx: int,
    *,
    english: bool = False,
) -> bool:
    """V2.6 helper: chart slide role 渲染统一封装 (insight + chart 数据).

    feedback-0713 F5: chart_sec 也可以是 image section (视觉引用双桶) —— 图片
    路径走 _render_image_role (文本同模板 + 图片盖 chart placeholder 框位)。
    """
    if (chart_sec or {}).get("kind") == "image":
        return _render_image_role(slide, ic, chart_sec, sec_label, section_idx, english=english)
    if ic is None:
        # Standalone chart, without an enclosing insight.
        title = (chart_sec or {}).get("spec", {}).get("title") or ("Data chart" if english else "数据图表")
        thesis = ""
        narrative = ""
    else:
        title = ic.get("title") or ("Analysis" if english else "维度分析")
        thesis = ic.get("thesis") or ""
        narrative = ic.get("narrative") or ""
    chart_data = _spec_to_chart_data((chart_sec or {}).get("spec") or {})
    if chart_data is None:
        return False
    render_chart_slide(
        slide,
        sec_label_text=f"{sec_label}  ·  {section_idx:02d}",
        title=title[:18],
        eng_subtitle=(chart_sec or {}).get("title") or "",
        insight_thesis=thesis[:50],
        insight_narrative=narrative[:160],
        chart_data=chart_data,
    )
    return True


def _render_image_role(
    slide: Any,
    ic: dict[str, Any] | None,
    img_sec: dict[str, Any] | None,
    sec_label: str,
    section_idx: int,
    *,
    english: bool = False,
) -> bool:
    """feedback-0713 F5: image section 上 chart slide —— 文本占位照 chart slide
    模板填, 图片 aspect-fit 盖在 chart placeholder 框位上 (先铺白底盖 demo chart)。
    无可读 _local_path (预取失败) 时返回 False，由调用方删除整页。
    """
    img_sec = img_sec or {}
    local = img_sec.get("_local_path")
    if not local or not Path(local).is_file():
        return False
    if ic is None:
        title = img_sec.get("caption") or img_sec.get("alt") or ("Illustration" if english else "图示")
        thesis = ""
        narrative = img_sec.get("caption") or ""
    else:
        title = ic.get("title") or ("Analysis" if english else "维度分析")
        thesis = ic.get("thesis") or ""
        narrative = ic.get("narrative") or ""
    render_chart_slide(
        slide,
        sec_label_text=f"{sec_label}  ·  {section_idx:02d}",
        title=title[:18],
        eng_subtitle=(img_sec.get("caption") or img_sec.get("alt") or "")[:60],
        insight_thesis=thesis[:50],
        insight_narrative=narrative[:160],
        # The image covers the chart shape, but the embedded workbook remains packaged; replace its data with empty values first
        # so users unpacking/editing the chart do not see the default template's sample business values.
        chart_data={"labels": [""], "values": [0], "series_name": ""},
    )
    s = slide.shapes
    if len(s) > 4:
        left, top, width, height = s[4].left, s[4].top, s[4].width, s[4].height
    else:
        left, top, width, height = Inches(0.8), Inches(1.8), Inches(8.4), Inches(4.2)
    _add_picture_fitted(slide, local, left, top, width, height)
    return True


def _add_picture_fitted(slide, path: str, left, top, width, height):
    """图片 aspect-fit 居中放进给定框位; 先铺白底矩形盖掉框位内的 template demo
    chart (python-pptx 删不掉 chart part, 盖底是最稳妥的做法)。"""
    try:
        from pptx.enum.shapes import MSO_SHAPE

        rect = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
        rect.fill.solid()
        rect.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        rect.line.fill.background()
        rect.shadow.inherit = False
    except Exception:  # noqa: S110 — A white-background failure must not block the image.
        pass
    try:
        from PIL import Image as _PILImage  # python-pptx depends on Pillow, so it is always available in the sandbox.

        with _PILImage.open(path) as im:
            iw, ih = im.size
        if iw <= 0 or ih <= 0:
            raise ValueError("bad image size")
        scale = min(int(width) / iw, int(height) / ih)
        w = int(iw * scale)
        h = int(ih * scale)
        slide.shapes.add_picture(path, int(left) + (int(width) - w) // 2, int(top) + (int(height) - h) // 2, width=w, height=h)
    except Exception:  # noqa: S110 — Fallback: stretch to the frame width (scale height proportionally).
        import contextlib

        with contextlib.suppress(Exception):  # Broken image: preserve the text slide.
            slide.shapes.add_picture(path, left, top, width=width)


def _excel_number_format(column: dict) -> str:
    """Map column metadata to Excel/OOXML number formats.

    Replacing chart data does not replace a template's number formats. Set them
    from current column semantics so ordinary quantities do not inherit percent
    formatting. Never infer percent scale from the values: fraction and points
    ranges overlap. Explicit percent_scale wins; otherwise format=percent uses
    Excel scaling, and unit=% without that format uses a literal percent suffix.
    Fall back to #,##0.## when the metadata does not establish a percentage."""
    fmt = (column.get("format") or "").strip().lower()
    scale = (column.get("percent_scale") or "").strip().lower()
    unit = (column.get("unit") or "").strip()
    if scale == "points":
        return '0.0"%"'
    if fmt == "percent" or scale == "fraction":
        return "0.0%"
    if unit == "%" and not fmt:
        return '0.0"%"'
    if fmt == "float":
        return "#,##0.00"
    if fmt in ("currency", "int"):
        return "#,##0"
    # Columns without format (common in LLM-generated specs). **Do not guess** semantics: neither infer percentages from column names
    # (sharing patterns across surfaces requires measuring false positives, not done on this path) nor force integers (which renders 51.6 as 52).
    # `#,##0.##` adds thousands separators and up to two decimals without zero-padding, accurately representing integers and decimals.
    return "#,##0.##"


def _spec_to_chart_data(spec: dict) -> dict | None:
    """ChartSpec → {labels, values, series_name, number_format} 单 series."""
    if not spec:
        return None
    rows = (spec.get("rows") or [])[:50]
    cols = spec.get("columns") or []
    if not rows or len(cols) < 2:
        return None
    cat_key = next((c["key"] for c in cols if c.get("type") != "numeric"), cols[0]["key"])
    num_cols = [c for c in cols if c.get("type") == "numeric"]
    if not num_cols:
        return None
    # Take the first numeric column.
    val_key = num_cols[0]["key"]
    values = [float(r.get(val_key) or 0) for r in rows]
    return {
        "labels": [str(r.get(cat_key, "")) for r in rows],
        "values": values,
        "series_name": num_cols[0].get("header", val_key),
        "number_format": _excel_number_format(num_cols[0]),
    }


def _normalize_residue_text(value: object) -> str:
    """Normalize text for exact template-residue comparison."""
    return "".join(str(value).split()).casefold()


def _clear_template_layout_demo_fields(prs: Any) -> None:
    """Clear sample identifiers and vendor branding inherited from slide layouts.

    A-1: layout 上的页脚（TITLE_ONLY / KPI_GRID / CHART_SLIDE 的 shape[0]）写着
    "B O W H E A D · 北极九章"。它挂在 layout 上而不是 slide 上，所以解包 slide XML
    看不见它，**但每一页都会渲出来** —— 客户截图里正是从第 4 页页脚拍到的。
    """
    demo_fields = {_normalize_residue_text(value) for value in ("CMP-2604-001", "1003", "1004", "1005")}
    for layout in prs.slide_layouts:
        for shape in layout.shapes:
            if not shape.has_text_frame:
                continue
            text = shape.text_frame.text
            if _normalize_residue_text(text) in demo_fields or _is_vendor_brand_text(text):
                _set_text(shape, "")


def _chart_fingerprints(prs: Any) -> set[tuple[tuple[str, ...], tuple[float, ...]]]:
    """Return native-chart data fingerprints from a presentation."""
    fingerprints = set()
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_chart or not shape.chart.plots:
                continue
            plot = shape.chart.plots[0]
            if not plot.series:
                continue
            categories = tuple(str(category) for category in plot.categories)
            values = tuple(round(float(value or 0), 8) for value in plot.series[0].values)
            fingerprints.add((categories, values))
    return fingerprints


def _source_chart_fingerprints(
    sections: list[dict[str, Any]],
) -> set[tuple[tuple[str, ...], tuple[float, ...]]]:
    fingerprints = set()
    for section in sections:
        if section.get("kind") != "chart":
            continue
        chart_data = _spec_to_chart_data(section.get("spec") or {})
        if chart_data is None:
            continue
        fingerprints.add(
            (
                tuple(chart_data["labels"]),
                tuple(round(float(value or 0), 8) for value in chart_data["values"]),
            )
        )
    return fingerprints


def _default_template_demo_texts() -> set[str]:
    """Read business-specific sample fields from the bundled baseline."""
    baseline = Presentation(str(TEMPLATE))
    # Static section labels and page numbers are excluded.
    #
    # Brand and closing copy are dynamic template content and must be checked too.
    # Now scan the cover brand(0/4), closing deck/email/brand(6/2,3,4), and layout brand footers
    # together. Rendering already clears them; this is a **fallback gate against regressions**:
    # if someone restores brand strings, export fails closed (return 1) instead of silently sending them to customers.
    dynamic_shape_indexes = {
        0: (0, 1, 2, 3, 4),
        1: (2, 3, 6, 9, 12, 15),
        2: (2, 3, 5, 6, 7, 9, 10, 11, 13, 14, 15, 17, 18, 19, 20),
        3: (2, 3, 7, 8),
        4: (2, 3, 7, 8),
        5: (8, 9, 11, 13, 18, 19, 21, 23, 28, 29, 31, 33),
        6: (2, 3, 4),
    }
    markers = set()
    for slide_idx, shape_indexes in dynamic_shape_indexes.items():
        if slide_idx >= len(baseline.slides):
            continue
        slide = baseline.slides[slide_idx]
        for shape_idx in shape_indexes:
            if shape_idx >= len(slide.shapes):
                continue
            shape = slide.shapes[shape_idx]
            if shape.has_text_frame:
                marker = _normalize_residue_text(shape.text_frame.text)
                if marker:
                    markers.add(marker)
    demo_layout_fields = {_normalize_residue_text(value) for value in ("CMP-2604-001", "1003", "1004", "1005")}
    for layout in baseline.slide_layouts:
        for shape in layout.shapes:
            text_frame = getattr(shape, "text_frame", None)
            if text_frame is not None:
                marker = _normalize_residue_text(text_frame.text)
                if marker and (marker in demo_layout_fields or _is_vendor_brand_text(marker)):
                    markers.add(marker)
    return markers


def _find_default_template_residue(
    prs: Any,
    sections: list[dict[str, Any]],
) -> list[str]:
    """Find bundled-template business facts not justified by canvas source data."""
    source_text = _normalize_residue_text(json.dumps(sections, ensure_ascii=False, sort_keys=True))
    output_texts: set[str] = set()
    for container in (*prs.slides, *prs.slide_layouts):
        output_texts.update(
            _normalize_residue_text(shape.text_frame.text) for shape in container.shapes if shape.has_text_frame and shape.text_frame.text
        )
    residue = [
        f"text:{marker[:80]}"
        for marker in _default_template_demo_texts()
        if marker not in source_text and any(marker in output_text for output_text in output_texts)
    ]

    baseline_fingerprints = _chart_fingerprints(Presentation(str(TEMPLATE)))
    source_fingerprints = _source_chart_fingerprints(sections)
    output_fingerprints = _chart_fingerprints(prs)
    for fingerprint in baseline_fingerprints & output_fingerprints:
        if fingerprint not in source_fingerprints:
            residue.append(f"chart:{fingerprint!r}")

    # docProps was outside this scan: real 116 production exports had clean slides but
    # retained all vendor/demo values in <Company>/<dc:creator>/<dc:title>.
    _h1 = next((s for s in sections if s.get("kind") == "h1"), {})
    residue.extend(_document_property_residue(prs, deck_title=_h1.get("text") or ""))
    return sorted(residue)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--workdir", required=True)
    args, _ = ap.parse_known_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    community = TEMPLATE.name == "community-neutral.pptx"
    if community and not data.get("locale"):
        data["locale"] = "en-US"
    english = community and not str(data.get("locale", "en-US")).lower().startswith("zh")
    sections = data.get("sections") or []
    # SEC (skill-multiruntime S3-R): this renderer runs as a plain host subprocess with
    # backend FS read access. canvas-pptx is a catalog skill (audience:all), so besides
    # the trusted canvas_export pipeline the LLM can skill_invoke it directly with an
    # attacker-influenced template_path — an unconfined value would let it parse an
    # arbitrary backend-readable .pptx/.zip and bleed its text into slides. Confine a
    # caller-supplied template_path to the OS temp root (where canvas_export materializes
    # uploaded templates via tempfile.mkdtemp) or this workdir; open the RESOLVED path
    # (closes the resolve→open gap); on reject/missing → bundled baseline (trusted, ships
    # with the skill). Mirrors render_deck.py SEC-2.
    _caller_tpl = data.get("template_path")
    if _caller_tpl:
        _rp = Path(_caller_tpl).resolve()
        _roots = (Path(tempfile.gettempdir()).resolve(), Path(args.workdir).resolve())
        if any(str(_rp) == str(r) or str(_rp).startswith(str(r) + "/") for r in _roots):
            _caller_tpl = str(_rp)
        else:
            sys.stderr.write(f"template_path outside allowed roots (temp/workdir); using standard export: {_caller_tpl!r}\n")
            _caller_tpl = None
    if not _caller_tpl or not Path(_caller_tpl).is_file():
        from render_content import render_content

        result = render_content(data, Path(args.workdir))
        Path(args.workdir, "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return 0

    template_path = _caller_tpl
    out_path = Path(args.workdir) / "output.pptx"

    # CTO review hotfix: invalid template (missing / fewer than 7 slides) → fall back to the default baseline.
    using_fallback = False
    if not Path(template_path).is_file():
        if not TEMPLATE.is_file():
            return 1
        template_path = str(TEMPLATE)
        using_fallback = True

    prs = Presentation(template_path)
    # V2.6: no longer require 7 slides; the template determines the count, with roles routed by slide_layout.name.
    # The template needs at least one slide (a cover suffices) to render.
    if len(prs.slides) < 1:
        if str(TEMPLATE) != template_path and TEMPLATE.is_file():
            prs = Presentation(str(TEMPLATE))
            template_path = str(TEMPLATE)
            using_fallback = True
        else:
            return 1
    if using_fallback:
        pass
    _clear_template_layout_demo_fields(prs)

    # Extract canvas fields.
    h1 = next((s for s in sections if s.get("kind") == "h1"), {})
    eyebrow = next((s for s in sections if s.get("kind") == "eyebrow"), {})
    deck = next((s for s in sections if s.get("kind") == "deck"), {})
    kpi_row = next((s for s in sections if s.get("kind") == "kpi_row"), None)
    insight_cards = [s for s in sections if s.get("kind") == "insight_card"]
    rec_section = next((s for s in sections if s.get("kind") == "recommendations"), None)
    chart_sections = [s for s in sections if s.get("kind") == "chart"]
    # feedback-0713 F5 (B4 debt): image sections (matplotlib PNGs, etc.) previously never rendered;
    # queue them for chart slides (two visual-reference types: insight evidence chart_section_id
    # may reference a chart or an image).
    image_sections = [s for s in sections if s.get("kind") == "image"]
    visual_sections = chart_sections + image_sections
    design_sections = [s for s in sections if s.get("kind") == "design"]

    # Narratives: first (deck) → exec hero, second → caveats (skip).
    narrative_secs = [s for s in sections if s.get("kind") == "narrative"]
    exec_narr = narrative_secs[0] if narrative_secs else {}

    from datetime import datetime

    date_str = datetime.now().strftime("%Y-%m-%d")

    # === Pre-compute canvas-driven content ===
    # Used by the exec slide (hero/body/findings/rec).
    hero = ""
    if insight_cards:
        hero = (insight_cards[0].get("thesis") or insight_cards[0].get("title") or "")[:40]
    # `[figure:sec_xxx]` is an internal canvas image anchor, not reader-facing text. docx/pdf strip it;
    # this had **no stripping rule**, leaking inline anchors into the exec slide body (observed:
    # "如下图 [figure:sec_abc123] 所示，…" → cut at the first full stop, leaving the anchor unchanged in the slide).
    # Standalone anchors happened to be cut off at the full stop and did not leak; do not rely on that accident.
    body_raw = _md_text(exec_narr.get("markdown"))
    for sep in ("。", "；"):
        if sep in body_raw:
            body_raw = body_raw.split(sep)[0]
            break
    body = body_raw[:60]
    findings = []
    for ic in insight_cards[:3]:
        t = ic.get("thesis") or ""
        for sep in ("。", "；", "，", ","):
            if sep in t:
                t = t.split(sep)[0]
                break
        findings.append(t[:35])
    rec_text = ""
    if rec_section and rec_section.get("items"):
        top = rec_section["items"][0]
        action_short = (top.get("action") or "")[:18]
        priority = top.get("priority", "P0")
        rec_text = f"Start {action_short} ({priority})." if english else f"建议立即启动「{action_short}」({priority})。"

    # KPI slide data
    kpis = (kpi_row or {}).get("cells") or []
    kpi_summary = ""
    if narrative_secs:
        kpi_summary = _md_text(narrative_secs[0].get("markdown"))[:140]

    # Queue chart insights in two groups by chart type (bar / line).
    referenced_chart_ids = set()
    for ic in insight_cards:
        for ev in ic.get("evidence") or []:
            cid = ev.get("chart_section_id")
            if cid:
                referenced_chart_ids.add(cid)

    def _chart_kind_of(ic):
        for ev in ic.get("evidence") or []:
            cid = ev.get("chart_section_id")
            cs = next((s for s in visual_sections if s.get("id") == cid), None)
            if cs:
                if cs.get("kind") == "image":
                    return "image"  # F5: image visuals enter the image pool.
                return (cs.get("spec") or {}).get("kind") or "bar"
        return None

    bar_chart_insights = [
        (ic, _find_chart_for_insight(ic, chart_sections)) for ic in insight_cards if _chart_kind_of(ic) in ("bar", "hbar", "rank", "share", "compare")
    ]
    line_chart_insights = [(ic, _find_chart_for_insight(ic, chart_sections)) for ic in insight_cards if _chart_kind_of(ic) in ("line", "trend")]
    # Standalone line/trend charts (no enclosing insight).
    standalone_line = [
        (None, s) for s in chart_sections if (s.get("spec") or {}).get("kind") in ("line", "trend") and s.get("id") not in referenced_chart_ids
    ]
    line_chart_insights.extend(standalone_line)
    # Handle standalone bar charts the same way.
    standalone_bar = [
        (None, s)
        for s in chart_sections
        if (s.get("spec") or {}).get("kind") in ("bar", "hbar", "rank", "share") and s.get("id") not in referenced_chart_ids
    ]
    bar_chart_insights.extend(standalone_bar)
    # F5: a third visual pool for images, both insight-referenced and standalone. Preserve bar/line priority:
    # cursor0/1 borrow images when bar/line is absent; the final pool takes the remainder.
    # F5 review P2: **require _local_path to enter the pool**; otherwise _render_image_role fills only
    # text and returns, leaving fake template demo bars in the chart placeholder. A genuine user thesis paired with
    # fake chart data misrepresents fiction as "Verified Intelligence". Omit images without bytes from slides instead.
    image_visuals = [
        (ic, sec)
        for ic in insight_cards
        if _chart_kind_of(ic) == "image" and (sec := _find_chart_for_insight(ic, visual_sections)) is not None and sec.get("_local_path")
    ]
    image_visuals.extend((None, s) for s in image_sections if s.get("id") not in referenced_chart_ids and s.get("_local_path"))

    rec_items = (rec_section or {}).get("items") or []

    # === V2.6: route roles by slide_layout.name; the template determines the slide count ===
    # Maintain cursors to fill multiple slides sharing a layout with content of the same type.
    title_only_cursor = 0  # 0 = exec summary, 1+ = recommendations / extra notes
    chart_slide_cursor = 0  # 0 = bar/system, 1 = line/trend, 2+ = additional insights.
    image_cursor = 0  # F5: image pool cursor (the final pool continues here after cursor0/1 borrow images).
    cover_done = False
    rendered_count = 0
    slides_to_delete = []

    # V3 stub: detect whether the template has at least one standard layout name. If none, map roles by slide
    # order on a best-effort basis (slide 1 = cover, 2 = exec, 3 = kpi, 4+ = chart, last = closing).
    # This handles uploaded .pptx files with arbitrary structures as a fallback, without LLM slide-role classification (the full V3+
    # implementation has low marginal value and is deferred).
    _STD_LAYOUTS = {"COVER", "TITLE_ONLY", "KPI_GRID", "CHART_SLIDE", "CLOSING"}
    template_has_std_layout = any((s.slide_layout.name or "").upper().replace(" ", "_") in _STD_LAYOUTS for s in prs.slides)
    if not template_has_std_layout:
        pass

    n_slides = len(prs.slides)

    def _heuristic_role(idx: int) -> str:
        """无标准 layout 时, 按 slide 顺序 best-effort 推 role."""
        if idx == 0:
            return "COVER"
        if idx == n_slides - 1:
            return "CLOSING"
        # Middle slides: first → exec, second → kpi, others → chart_slide, second-to-last → recs.
        if idx == 1:
            return "TITLE_ONLY"  # exec
        if idx == 2:
            return "KPI_GRID"
        if idx == n_slides - 2:
            return "TITLE_ONLY"  # recs
        return "CHART_SLIDE"

    for slide in prs.slides:
        layout_name = (slide.slide_layout.name or "").upper().replace(" ", "_")
        # V3 stub: infer nonstandard layouts with heuristic_role.
        if layout_name not in _STD_LAYOUTS and not template_has_std_layout:
            layout_name = _heuristic_role(rendered_count)
        section_idx = rendered_count + 1

        if layout_name == "COVER" or (not cover_done and rendered_count == 0):
            render_cover(slide, h1, eyebrow, deck, date_str)
            cover_done = True

        elif layout_name == "KPI_GRID" or "KPI" in layout_name:
            render_kpi(slide, kpis, "Key metrics" if english else "总览指标", "", kpi_summary, section_idx=section_idx)

        elif layout_name == "CHART_SLIDE" or "CHART" in layout_name:
            # Bar first, line second; continue filling insights by cursor when there are more than two chart_slide slides.
            # F5: cursor0/1 borrow from the image pool when bar/line is absent (reports with charts generated entirely by Python
            # previously left template demo data on every chart slide); the final pool takes remaining images
            # (image_cursor tracks borrowing progress).
            chart_rendered = False
            if chart_slide_cursor == 0 and (bar_chart_insights or image_visuals):
                if bar_chart_insights:
                    ic, chart_sec = bar_chart_insights[0]
                else:
                    ic, chart_sec = image_visuals[image_cursor]
                    image_cursor += 1
                chart_rendered = _render_chart_role(
                    slide,
                    ic,
                    chart_sec,
                    sec_label="S Y S T E M  B R E A K D O W N",
                    section_idx=section_idx,
                    english=english,
                )
            elif chart_slide_cursor == 1 and (line_chart_insights or image_cursor < len(image_visuals)):
                if line_chart_insights:
                    ic, chart_sec = line_chart_insights[0]
                else:
                    ic, chart_sec = image_visuals[image_cursor]
                    image_cursor += 1
                chart_rendered = _render_chart_role(
                    slide,
                    ic,
                    chart_sec,
                    sec_label="T E M P O R A L  T R E N D",
                    section_idx=section_idx,
                    english=english,
                )
            elif chart_slide_cursor >= 2:
                # Additional chart slides: prefer unused bar insights, then line insights, then remaining images.
                idx = chart_slide_cursor - 2
                pool = bar_chart_insights[1:] + line_chart_insights[1:] + image_visuals[image_cursor:]
                if idx < len(pool):
                    ic, chart_sec = pool[idx]
                    chart_rendered = _render_chart_role(
                        slide,
                        ic,
                        chart_sec,
                        sec_label=f"I N S I G H T  ·  {section_idx:02d}",
                        section_idx=section_idx,
                        english=english,
                    )
            if not chart_rendered:
                slides_to_delete.append(slide)
            chart_slide_cursor += 1

        elif layout_name == "TITLE_ONLY":
            # First TITLE_ONLY = exec, second = recommendations; delete later slides without a role.
            if title_only_cursor == 0:
                render_exec(slide, {"hero": hero, "body": body}, findings, rec_text, section_idx=section_idx)
            elif title_only_cursor == 1:
                render_recommendations(slide, rec_items, section_idx=section_idx, title="Recommendations" if english else "行动建议")
            else:
                slides_to_delete.append(slide)
            title_only_cursor += 1

        elif layout_name == "CLOSING":
            # A-1: the closing page previously hardcoded vendor marketing copy/contact email, putting them directly into customer reports.
            # This deck is the customer's deliverable, not our promotional page; retain only "Thank you.".
            render_closing(slide, deck="", email_url="", date_str=date_str)
        elif template_has_std_layout:
            # Unknown layouts in standard templates have no reliable data mapping; keeping the entire slide leaks sample content.
            slides_to_delete.append(slide)

        rendered_count += 1  # noqa: SIM113

    for slide in slides_to_delete:
        _delete_slide(prs, slide)

    render_design_summary(prs, design_sections)

    # A-1 continued: replace document properties with the customer's own values too (see `_scrub_document_properties` comments).
    # Do this **before** residue scanning: the scan is a safety net, not a test, catching deletion/breakage of this step,
    # which is exactly what the positive control below must demonstrate.
    _scrub_document_properties(prs, deck_title=h1.get("text") or "", stamped=datetime.now())

    demo_residue = []
    if Path(template_path).resolve() == TEMPLATE.resolve():
        demo_residue = _find_default_template_residue(prs, sections)
        if demo_residue:
            sys.stderr.write(f"default template demo residue detected: {demo_residue!r}\n")
            return 1

    prs.save(str(out_path))

    result = {
        "path": str(out_path),
        "slides": len(prs.slides),
        "sections": len(sections),
        "demo_residue_count": len(demo_residue),
    }
    Path(args.workdir, "result.json").write_text(
        json.dumps(result, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
