"""Render ReportSection[] → PDF using reportlab.

Called by Bowhead SkillRuntime:
    python3 render_from_canvas.py --input <dir>/input.json --workdir <dir>

Output: workdir/output.pdf
Writes result.json with {"path", "pages", "sections"}.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from pathlib import Path

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except ImportError:
    sys.exit(3)


# Feishu bug list follow-up #18: ReportLab's default Helvetica lacks Chinese glyphs, rendering Chinese as black squares.
# Register the built-in CID Chinese font STSong-Light (bundled with ReportLab, requires no external font files, most reliable),
# and register its font family so inline <b>/<i> tags also use it instead of falling back to Helvetica.
def _register_cjk_fonts() -> tuple[str, str]:
    """注册中文字体, 返回 (normal, bold) 字体名。

    优先 Noto CJK TTF —— 它有**真粗体**权重, 表头/`<b>`/**bold** 才有视觉粗细,
    且 ASCII/英文报告也保留 (review[5]: 原先全指 STSong 使英文报告丢了 Helvetica-Bold
    的加粗)。沙箱镜像装了 fonts-noto-cjk。若找不到 → 回退 reportlab 内置 CID
    STSong-Light (SC 字形齐全, 无粗体权重, 但不再黑方块)。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    def _find(*pats: str) -> str | None:
        for pat in pats:
            cut = next((i for i, c in enumerate(pat) if c in "*?["), len(pat))
            if cut == len(pat):
                hits = [pat] if Path(pat).exists() else []
            else:
                base = Path(pat[:cut].rpartition("/")[0] or "/")
                rel = pat[cut:].lstrip("/")
                hits = [str(p) for p in base.glob(rel)] if base.is_dir() else []
            hits = sorted(hits)
            if hits:
                return hits[0]
        return None

    def _reg_ttf(name: str, path: str) -> bool:
        # .ttc is a font collection; SC subfont positions vary across distributions. Try each index until one succeeds
        # (Noto CJK subfonts for every locale contain Chinese glyphs and can render Chinese). Ignore index for .otf.
        idxs = (2, 0, 1, 3, 4) if path.endswith(".ttc") else (0,)
        for idx in idxs:
            try:
                kw = {"subfontIndex": idx} if path.endswith(".ttc") else {}
                pdfmetrics.registerFont(TTFont(name, path, **kw))
                return True
            except Exception:  # noqa: S112
                continue
        return False

    reg = _find(
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/**/NotoSansCJK*-Regular.*",
        "/usr/share/fonts/**/NotoSansCJKsc*.*",
    )
    boldp = _find(
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/**/NotoSansCJK*-Bold.*",
    )
    if reg and _reg_ttf("CJK", reg):
        bold_name = "CJK"
        if boldp and _reg_ttf("CJK-Bold", boldp):
            bold_name = "CJK-Bold"
        pdfmetrics.registerFontFamily(
            "CJK",
            normal="CJK",
            bold=bold_name,
            italic="CJK",
            boldItalic=bold_name,
        )
        return "CJK", bold_name

    # Fallback: built-in CID (no external font file dependency).
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdfmetrics.registerFontFamily(
        "STSong-Light",
        normal="STSong-Light",
        bold="STSong-Light",
        italic="STSong-Light",
        boldItalic="STSong-Light",
    )
    return "STSong-Light", "STSong-Light"


_CJK_FONT = "Helvetica"
_CJK_BOLD = "Helvetica-Bold"
with contextlib.suppress(Exception):
    _CJK_FONT, _CJK_BOLD = _register_cjk_fonts()


FORBIDDEN_CHARS = set("₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹")


def _sanitize(s: object) -> str:
    text = str(s or "")
    return "".join(c for c in text if c not in FORBIDDEN_CHARS)


def _fmt_num(v: object) -> str | None:
    """浮点数 → 人看的写法；不是浮点就返回 None（与 canvas-docx 同口径，见那边注释）。

    只管 float 不碰 int —— 年份/排名/ID 是 int，加千分位反成新 bug。
    """
    if isinstance(v, bool) or not isinstance(v, float):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    if v == 0:
        return "0"
    if abs(v) < 0.01:
        return f"{v:.4g}"
    if abs(v - round(v)) < 1e-9:
        return f"{round(v):,}"
    return f"{v:,.2f}"


def _cell_text(v: object) -> str:
    """表格格子的统一出口：先试数字格式化，再退到 `_sanitize`。"""
    return _fmt_num(v) or _sanitize(v)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    _styles_dict = {
        "eyebrow": ParagraphStyle(
            "eyebrow",
            parent=base["Normal"],
            fontSize=9,
            textColor=colors.HexColor("#6B7A6F"),
            spaceAfter=4,
            leading=11,
            textTransform="uppercase",
        ),
        "h1": ParagraphStyle(
            "h1",
            parent=base["Title"],
            fontSize=20,
            leading=24,
            spaceAfter=8,
            textColor=colors.HexColor("#1B1A19"),
        ),
        "deck": ParagraphStyle(
            "deck",
            parent=base["Normal"],
            fontSize=12,
            leading=16,
            spaceAfter=10,
            textColor=colors.HexColor("#35342F"),
        ),
        "meta": ParagraphStyle(
            "meta",
            parent=base["Normal"],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#6B7A6F"),
            spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontSize=14,
            leading=18,
            spaceBefore=12,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base["BodyText"],
            fontSize=10.5,
            leading=15,
            spaceAfter=8,
        ),
        "callout": ParagraphStyle(
            "callout",
            parent=base["BodyText"],
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#2E5D4F"),
            borderColor=colors.HexColor("#C8DDD4"),
            borderWidth=0.5,
            borderPadding=6,
            backColor=colors.HexColor("#E8F0EC"),
            spaceBefore=6,
            spaceAfter=8,
        ),
        "src_line": ParagraphStyle(
            "src_line",
            parent=base["Normal"],
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#56503f"),
            spaceAfter=2,
        ),
        "kpi_label": ParagraphStyle(
            "kpi_label",
            parent=base["Normal"],
            fontSize=8,
            textColor=colors.HexColor("#6B7A6F"),
            leading=10,
        ),
        "kpi_val": ParagraphStyle(
            "kpi_val",
            parent=base["Normal"],
            fontSize=14,
            textColor=colors.HexColor("#1B1A19"),
            leading=18,
        ),
    }
    # Feishu follow-up #18: point every paragraph style at a Chinese font (the default inherited Helvetica has no Chinese glyphs).
    for _st in _styles_dict.values():
        _st.fontName = _CJK_FONT
    return _styles_dict


# A pipe not escaped with a backslash is a real table column separator.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
# CommonMark backslash escape = `\` + ASCII punctuation.
_MD_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
# Unescaped angle brackets / & are interpreted as XML markup by ReportLab Paragraph,
# potentially losing content or failing the whole paragraph. Convert them to entities on the Paragraph path.
_XML_ENTITY = {"<": "&lt;", ">": "&gt;", "&": "&amp;"}

# ⚠ The two regexes above, `_unescape_md` / `_row_cells`, and the separator-line regex are duplicated in **four places**:
#   - `bowhead_core/report_writer_loop.py` (`_TABLE_SEP_RE` / `_UNESCAPED_PIPE_RE` / `_split_row`, canonical)
#   - `bowhead_skill/catalog/canvas-docx/scripts/render_from_canvas.py`
#   - `bowhead_skill/catalog/canvas-pdf/scripts/render_from_canvas.py` (this file)
#   - `bowhead_gateway/feishu/renderer.py`
# This is deliberate: skill scripts run independently in the sandbox and **cannot import `bowhead_core`**.
# When changing any parsing criterion, update all four copies; `report_writer_loop` is canonical.


def _unescape_md(text: str, *, xml: bool = False) -> str:
    """还原 markdown 的反斜杠转义 —— 导出器是**渲染终点**，不能把 `\\-` 印给用户。

    上游 `markdown-pm.ts::_serializeTableCell` 把每个单元格当独立文档序列化，行首的
    `-` / `N.` / `>` 会被 prosemirror-markdown 的 `esc(startOfLine)` 加反斜杠
    （`-37.5%` → `\\-37.5%`）；竖线在单元格里则**必须**保持 `\\|` 才不会劈成两格。
    写路径已不再产出行首那批，但**存量 markdown 里还有**，且 `\\|` 是有意保留的。

    `xml=True` 用于 reportlab 的 Paragraph：`\\<` 还原成裸 `<` 会被当标签，换成实体。
    """
    if xml:
        return _MD_ESCAPE.sub(lambda m: _XML_ENTITY.get(m.group(1), m.group(1)), text)
    return _MD_ESCAPE.sub(r"\1", text)


def _md_to_reportlab(s: str) -> str:
    """最小 markdown → reportlab XML 标签（**bold**, *italic*, `code`）."""
    import re

    s = _sanitize(s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", s)
    s = re.sub(r"`([^`]+)`", rf"<font name='{_CJK_FONT}'>\1</font>", s)
    # Unescape **last**: otherwise `\*` becomes `*` first and the regexes above then treat it as emphasis.
    return _unescape_md(s, xml=True)


# Batch C (report-edit-audit P2-2 legacy fallback): narrative/deck Markdown was previously stuffed into one
# Paragraph, leaving raw `##` from `## 标题` in the body, printing internal `[figure:sec_x]` anchors
# verbatim into PDF, and collapsing `- [P0] …` recommendation lists into a paragraph (three symptoms in real PDFs). Render in **blocks**:
# heading lines use h2 style, list items become separate paragraphs with •, and standalone figure anchors are stripped (charts render through
# the chart section branch; anchors are internal syntax). A full Markdown engine is out of scope (the V2 path is
# authoritative; this function only ensures the legacy fallback does not expose internal syntax).
def _md_table_flowable(header: list[str], body: list[list[str]], S: dict):
    """narrative 里的 markdown 表格 → 真 Table flowable（样式与 data_table 那条一致）。"""
    width = max(len(header), max((len(r) for r in body), default=0))
    # Cells contain **plain text** (Table does not parse XML), so unescape without XML conversion:
    # `\-37.5%` must print as `-37.5%`, and `a\|b` as `a|b`.
    data = [[_unescape_md(_sanitize(c)) for c in (header + [""] * (width - len(header)))]]
    for r in body:
        data.append([_unescape_md(_sanitize(c)) for c in (r + [""] * (width - len(r)))])
    tbl = Table(data, hAlign="LEFT")
    tbl.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), _CJK_FONT),
                ("FONTNAME", (0, 0), (-1, 0), _CJK_BOLD),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#1B1A19")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return tbl


def _narrative_flowables(md: str, S: dict, body_style: str = "body") -> list:
    import re

    _anchor_line = re.compile(r"^\s*\[figure:[a-zA-Z0-9_]+\]\s*$")
    _inline_anchor = re.compile(r"\[figure:[a-zA-Z0-9_]+\]")
    _heading = re.compile(r"^(#{1,4})\s+(.*)$")
    _list_item = re.compile(r"^\s*[-*]\s+(.*)$")
    # Markdown table: `| a | b |` followed by a `|---|:--:|` separator line.
    # 116 production 1.0.79 dogfood: writer rule 6 requires "use Markdown tables for comparisons". Frontend remark-gfm
    # renders them correctly, but the exporter did not recognize them. PDF was worse than Word: `_flush` below joins buffered lines
    # **with spaces into one paragraph**, flattening the table into an endless line of pipe-delimited text.
    # **One or more** hyphens per cell is valid (GFM spec); `-{2,}` fails to recognize valid tables such as `|-|-|`,
    # sending them to `_flush()` below as an endless line of pipe-delimited text.
    _table_sep = re.compile(r"^\s*\|\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")

    def _row_cells(ln: str) -> list[str]:
        """只在**未转义**的竖线处切：`\\|` 是单元格内的字面竖线，裸 split 会多切一列。"""
        s = ln.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|") and not s.endswith("\\|"):
            s = s[:-1]
        return [c.strip() for c in _UNESCAPED_PIPE.split(s)]

    flow: list = []
    buf: list[str] = []

    def _flush() -> None:
        if not buf:
            return
        text = _inline_anchor.sub("", " ".join(buf)).strip()
        buf.clear()
        if text:
            flow.append(Paragraph(_md_to_reportlab(text), S[body_style]))

    _lines = [ln.rstrip() for ln in (md or "").split("\n")]
    _i = -1
    while _i + 1 < len(_lines):
        _i += 1
        line = _lines[_i]
        if not line.strip():
            _flush()
            continue
        if _anchor_line.match(line):
            _flush()
            continue
        if line.strip().startswith("|") and _i + 1 < len(_lines) and _table_sep.match(_lines[_i + 1]):
            _flush()
            _hdr = _row_cells(line)
            _body: list[list[str]] = []
            _j = _i + 2
            while _j < len(_lines) and _lines[_j].strip().startswith("|"):
                _body.append(_row_cells(_lines[_j]))
                _j += 1
            _i = _j - 1
            if _hdr and _body:
                flow.append(_md_table_flowable(_hdr, _body, S))
            continue
        m = _heading.match(line)
        if m:
            _flush()
            heading_text = _inline_anchor.sub("", m.group(2)).strip()
            if heading_text:
                flow.append(Paragraph(_md_to_reportlab(heading_text), S["h2"]))
            continue
        m = _list_item.match(line)
        if m:
            _flush()
            item_text = _inline_anchor.sub("", m.group(1)).strip()
            if item_text:
                flow.append(Paragraph("• " + _md_to_reportlab(item_text), S[body_style]))
            continue
        buf.append(line)
    _flush()
    return flow


# ─────────────────────────────────────────────────────────────
# WP1 complete export path: chart → static matplotlib image (design D8 palette).
# ─────────────────────────────────────────────────────────────

# terracotta primary / slate secondary / dijon anomaly / earth / sand / ink (waterfall total)
_SERIES_COLORS = ["#b04a2a", "#5e7d8a", "#a98028", "#7a5a3f", "#9a8a6a", "#3a342c"]
_CHART_DIR_NAME = "_charts"
_chart_seq = 0


def _spec_axes(spec: dict) -> tuple[str | None, list[dict]]:
    """x 列 key + 数值系列列. role 优先, 回退类型推断."""
    cols = spec.get("columns") or []
    x_col = next((c for c in cols if c.get("role") == "x"), None)
    if x_col is None:
        x_col = next((c for c in cols if c.get("type") in ("categorical", "date")), None)
    num_cols = [c for c in cols if c.get("type") == "numeric" and c is not x_col and c.get("role") != "size"]
    return (x_col or {}).get("key"), num_cols


def _render_chart_png(spec: dict, workdir: Path) -> Path | None:
    """ChartSpec → PNG. matplotlib 缺失/数据不足/不支持 kind → None (调用方降级表格)."""
    global _chart_seq  # noqa: PLW0603
    try:
        import matplotlib as mpl

        mpl.use("Agg")
        # Without configuration, Chinese axis labels become missing-glyph squares. ⚠ This script runs in the **app container**
        # (all non-python_sandbox skill subprocesses run there; see bowhead_skill/runtime.py), rather than the sandbox image.
        # The app image also includes Noto Sans CJK (fc-list verified 10 Sans CJK faces;
        # `Noto Sans CJK SC` is the first choice below).
        mpl.rcParams["font.sans-serif"] = [
            "Noto Sans CJK SC",
            "Source Han Sans SC",
            "PingFang SC",
            "DejaVu Sans",
        ]
        mpl.rcParams["axes.unicode_minus"] = False
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    rows = spec.get("rows") or []
    kind = spec.get("kind") or "bar"
    x_key, num_cols = _spec_axes(spec)
    if not rows or kind in ("table", "kpi", "geo", "sankey") or (x_key is None and kind != "scatter"):
        return None  # sankey: matplotlib has no adequate rendering; fall back to a data table.
    if not num_cols:
        return None

    fig, ax = plt.subplots(figsize=(7.0, 3.0), dpi=150)
    try:
        xs = [str(r.get(x_key, ""))[:18] for r in rows] if x_key else list(range(len(rows)))

        def col_vals(c: dict) -> list[float]:
            out = []
            for r in rows:
                v = r.get(c["key"])
                try:
                    out.append(float(v))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    out.append(0.0)
            return out

        if kind == "pie":
            vals = col_vals(num_cols[0])
            ax.pie(vals, labels=xs, colors=_SERIES_COLORS[: len(xs)] * 8, autopct="%1.0f%%", textprops={"fontsize": 7})
        elif kind == "scatter":
            sx = col_vals(num_cols[0])
            sy = col_vals(num_cols[1]) if len(num_cols) > 1 else list(range(len(rows)))
            ax.scatter(sx, sy, c=_SERIES_COLORS[0], alpha=0.8, edgecolors="white")
        elif kind == "hbar":
            ax.barh(xs, col_vals(num_cols[0]), color=_SERIES_COLORS[0])
            ax.invert_yaxis()
        elif kind in ("line", "area_stacked"):
            for i, c in enumerate(num_cols[:6]):
                vals = col_vals(c)
                ax.plot(xs, vals, color=_SERIES_COLORS[i % 6], linewidth=1.8, marker="o", markersize=3, label=c.get("header", c["key"]))
                if kind == "area_stacked":
                    ax.fill_between(range(len(xs)), vals, alpha=0.15, color=_SERIES_COLORS[i % 6])
        elif kind == "waterfall":
            # Waterfall: cumulative floating bars, positive=terracotta, negative=slate, and a final ink total.
            cum = 0.0
            vals = col_vals(num_cols[0])
            for i, v in enumerate(vals):
                bottom = cum if v >= 0 else cum + v
                ax.bar(i, abs(v), bottom=bottom, width=0.6, color=_SERIES_COLORS[0] if v >= 0 else _SERIES_COLORS[1])
                cum += v
                if i < len(vals) - 1:
                    ax.plot([i + 0.3, i + 0.7], [cum, cum], color="#a09786", linestyle=":", linewidth=1)
            ax.bar(len(vals), abs(cum), bottom=min(0, cum), width=0.6, color=_SERIES_COLORS[5])
            ax.set_xticks(range(len(vals) + 1))
            ax.set_xticklabels([*xs, "Σ"], fontsize=7, rotation=30 if len(vals) > 6 else 0, ha="right" if len(vals) > 6 else "center")
        elif kind == "heatmap":
            mat = [[r_.get(c["key"]) or 0 for c in num_cols] for r_ in rows]
            im = ax.imshow(mat, cmap="Oranges", aspect="auto")
            ax.set_xticks(range(len(num_cols)))
            ax.set_xticklabels([c.get("header", c["key"]) for c in num_cols], fontsize=6)
            ax.set_yticks(range(len(xs)))
            ax.set_yticklabels(xs, fontsize=6)
            fig.colorbar(im, ax=ax, shrink=0.7)
        else:
            # bar / stacked_bar / combo / dual_axis → grouped/stacked bars (combo/dual draws the second column as a line).
            import numpy as np

            idx = np.arange(len(xs))
            n = max(1, len(num_cols[:4]))
            if kind == "stacked_bar":
                bottom = np.zeros(len(xs))
                for i, c in enumerate(num_cols[:6]):
                    vals = np.array(col_vals(c))
                    ax.bar(idx, vals, bottom=bottom, color=_SERIES_COLORS[i % 6], label=c.get("header", c["key"]), width=0.6)
                    bottom += vals
            elif kind in ("combo", "dual_axis") and len(num_cols) > 1:
                ax.bar(idx, col_vals(num_cols[0]), color=_SERIES_COLORS[0], label=num_cols[0].get("header"), width=0.55)
                ax2 = ax.twinx() if kind == "dual_axis" else ax
                ax2.plot(
                    idx, col_vals(num_cols[1]), color=_SERIES_COLORS[1], linewidth=1.8, marker="o", markersize=3, label=num_cols[1].get("header")
                )
            else:
                w = 0.8 / n
                for i, c in enumerate(num_cols[:4]):
                    ax.bar(idx + i * w - 0.4 + w / 2, col_vals(c), width=w, color=_SERIES_COLORS[i % 6], label=c.get("header", c["key"]))
            ax.set_xticks(range(len(xs)))
            ax.set_xticklabels(xs, fontsize=7, rotation=30 if len(xs) > 6 else 0, ha="right" if len(xs) > 6 else "center")

        if kind not in ("pie", "heatmap"):
            ax.tick_params(labelsize=7)
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", color="#f0eadd", linewidth=0.6)
            ax.set_axisbelow(True)
            if len(num_cols) > 1:
                ax.legend(fontsize=6.5, frameon=False)

        out_dir = workdir / _CHART_DIR_NAME
        out_dir.mkdir(exist_ok=True)
        _chart_seq += 1
        out = out_dir / f"chart_{_chart_seq}.png"
        fig.tight_layout()
        fig.savefig(out, bbox_inches="tight")
        return out
    except Exception:
        return None
    finally:
        plt.close(fig)


def _chart_flowables(spec: dict, S: dict, workdir: Path) -> list:
    """chart section → [标题, 图 PNG | 数据表降级, caption]. 永不返回空 (导出契约: 不蒸发)."""
    from reportlab.platypus import Image as RLImage

    out: list = []
    if spec.get("title"):
        out.append(Paragraph(f"<b>{_sanitize(spec['title'])}</b>", S["body"]))
    if spec.get("subtitle"):
        out.append(Paragraph(_sanitize(spec["subtitle"]), S["eyebrow"]))
    png = _render_chart_png(spec, workdir)
    if png is not None:
        out.append(RLImage(str(png), width=6.2 * inch, height=2.65 * inch, kind="proportional"))
    else:
        # Fallback: the first 12 data-table rows (provide data even without a chart; do not print placeholders).
        #
        # ⚠ This fallback **is the production path**: the app container lacks matplotlib, so `_render_chart_png` always returns None
        # (116 production 1.1.18: exported PDFs contained 0 embedded images). Two consequences had gone unnoticed:
        #   1. Using `_sanitize` instead of `_cell_text` leaves numbers completely unformatted. The same canvas
        #      exported to PDF had **50 raw floats** (`157216967.4831249`), while Word showed `157,216,967.48`.
        #      (The previous docx/pdf consistency batch fixed only data_table / attribution, missing this path.)
        #   2. The 12-row limit **silently** drops data, violating export contract ③: "make degradation visible, never let content disappear".
        cols = spec.get("columns") or []
        all_rows = spec.get("rows") or []
        rows = all_rows[:12]
        if cols and rows:
            data = [[_sanitize(c.get("header", c.get("key"))) for c in cols]]
            for r in rows:
                data.append([_cell_text(r.get(c["key"], "")) for c in cols])
            tbl = Table(data, hAlign="LEFT")
            tbl.setStyle(
                TableStyle(
                    [
                        ("FONTNAME", (0, 0), (-1, -1), _CJK_FONT),
                        ("FONTNAME", (0, 0), (-1, 0), _CJK_BOLD),
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#1B1A19")),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            out.append(tbl)
            if len(all_rows) > len(rows):
                out.append(
                    Paragraph(
                        f"<i>(另有 {len(all_rows) - len(rows)} 行未显示。完整数据见画布或 Excel 导出。)</i>",
                        S["eyebrow"],
                    )
                )
        else:
            out.append(Paragraph("<i>(chart data unavailable)</i>", S["eyebrow"]))
    out.append(Spacer(1, 8))
    return out


def _image_flowables(sec: dict, S: dict) -> list:
    """image section → [图 | 占位文案] (feedback-0713 F5, B4 债).

    顶层 image 与 insight_card 内嵌 image 共用 (卡内内嵌镜像 FE 视觉引用双桶)。
    _local_path 由 canvas_export._inject_image_paths 预取注入 (沙箱无网络)。
    """
    out: list = []
    local = sec.get("_local_path")
    if local and Path(local).exists():
        from reportlab.platypus import Image as RLImage

        try:
            out.append(RLImage(local, width=6.0 * inch, height=3.2 * inch, kind="proportional"))
            return out
        except Exception:  # noqa: S110 — Fall back to placeholder text for a broken image.
            pass
    out.append(
        Paragraph(
            f"<i>(image: {_sanitize(sec.get('caption') or sec.get('alt') or sec.get('artifact_id') or 'available in the live report')})</i>",
            S["eyebrow"],
        )
    )
    return out


def build_story(sections: list[dict], workdir: Path) -> list:
    S = _styles()
    story: list = []

    # WP1: charts referenced by insight_card evidence render inside the card and are skipped at the top level (mirroring frontend
    # preprocessSections deduplication).
    # feedback-0713 F5 (B4 debt): chart_section_id supports two visual types (chart/image use the same
    # field, B4). Images also render inside cards and are omitted at the top level (only explicitly referenced images;
    # unreferenced images still render at the top level, matching the frontend).
    charts_by_id = {s.get("id"): s for s in sections if s.get("kind") == "chart"}
    images_by_id = {s.get("id"): s for s in sections if s.get("kind") == "image"}
    consumed_chart_ids: set = set()
    for s in sections:
        if s.get("kind") == "insight_card":
            for ev in s.get("evidence") or []:
                cid = (ev or {}).get("chart_section_id")
                if cid:
                    consumed_chart_ids.add(cid)

    for sec in sections:
        kind = sec.get("kind")
        if kind == "eyebrow":
            story.append(Paragraph(_sanitize(sec.get("text", "")), S["eyebrow"]))
        elif kind == "h1":
            story.append(Paragraph(_sanitize(sec.get("text", "")), S["h1"]))
        elif kind == "deck":
            # review F-C1: keep deck introductions in the 12pt deck style (block rendering must not degrade the appearance).
            story.extend(_narrative_flowables(sec.get("markdown", ""), S, body_style="deck") or [Paragraph("", S["deck"])])
        elif kind == "meta":
            pills = sec.get("pills") or []
            story.append(Paragraph(" · ".join(_sanitize(p) for p in pills), S["meta"]))
        elif kind == "kpi_row":
            cells = sec.get("cells") or []
            if cells:
                row_top = [Paragraph(_sanitize(c.get("lbl", "")), S["kpi_label"]) for c in cells]
                row_bot = [Paragraph(_sanitize(c.get("val", "")), S["kpi_val"]) for c in cells]
                tbl = Table([row_top, row_bot], hAlign="LEFT")
                tbl.setStyle(
                    TableStyle(
                        [
                            ("FONTNAME", (0, 0), (-1, -1), _CJK_FONT),
                            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0E0DD")),
                            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#F0F0EC")),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                            ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ]
                    )
                )
                story.append(tbl)
                story.append(Spacer(1, 8))
        elif kind == "section":
            if sec.get("label"):
                story.append(Paragraph(_sanitize(sec["label"]), S["eyebrow"]))
            if sec.get("h2"):
                story.append(Paragraph(_sanitize(sec["h2"]), S["h2"]))
            if sec.get("body"):
                story.extend(build_story(sec["body"], workdir))
        elif kind == "narrative":
            story.extend(_narrative_flowables(sec.get("markdown", ""), S))
        elif kind == "data_table":
            cols = sec.get("columns") or []
            rows = sec.get("rows") or []
            if cols and rows:
                header = [_sanitize(c.get("header", c.get("key"))) for c in cols]
                data = [header]
                for r in rows:
                    line = []
                    for c in cols:
                        v = r.get(c["key"])
                        if isinstance(v, dict) and "delta" in v:
                            line.append(_cell_text(v["delta"]))
                        elif isinstance(v, dict) and "bar" in v:
                            line.append(f"{v['bar']:.0f}%")
                        else:
                            line.append(_cell_text(v))
                    data.append(line)
                tbl = Table(data, hAlign="LEFT")
                tbl.setStyle(
                    TableStyle(
                        [
                            ("FONTNAME", (0, 0), (-1, -1), _CJK_FONT),
                            ("FONTNAME", (0, 0), (-1, 0), _CJK_BOLD),
                            ("FONTSIZE", (0, 0), (-1, -1), 9),
                            ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#1B1A19")),
                            ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#E0E0DD")),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                            ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                if sec.get("caption"):
                    story.append(Paragraph(_sanitize(sec["caption"]), S["eyebrow"]))
                story.append(tbl)
                story.append(Spacer(1, 8))
        elif kind == "attribution":
            summary = sec.get("summary") or {}
            story.append(
                Paragraph(
                    f"{_sanitize(summary.get('label', ''))}: {_sanitize(summary.get('value', ''))} ({_sanitize(summary.get('pct', ''))})",
                    S["body"],
                )
            )
            # Reuse data_table rendering.
            rows = sec.get("rows") or []
            if rows:
                header = ["#", "维度 · 属性", "去期", "本期", "占比"]
                data = [header]
                for r in rows:
                    data.append(
                        [
                            r.get("rank", ""),
                            f"{_sanitize(r.get('dim', ''))} · {_sanitize(r.get('name', ''))}",
                            _cell_text(r.get("from", "")),
                            _cell_text(r.get("to", "")),
                            _cell_text(r.get("pct", "")),
                        ]
                    )
                tbl = Table(data, hAlign="LEFT")
                tbl.setStyle(
                    TableStyle(
                        [
                            ("FONTNAME", (0, 0), (-1, -1), _CJK_FONT),
                            ("FONTNAME", (0, 0), (-1, 0), _CJK_BOLD),
                            ("FONTSIZE", (0, 0), (-1, -1), 9),
                            ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#1B1A19")),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                            ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                story.append(tbl)
                story.append(Spacer(1, 8))
            if sec.get("insight"):
                story.append(Paragraph(_md_to_reportlab(sec["insight"]), S["body"]))
        elif kind == "callout":
            label = sec.get("label")
            body = _md_to_reportlab(sec.get("markdown", ""))
            if label:
                body = f"<b>{_sanitize(label)}</b> — {body}"
            story.append(Paragraph(body, S["callout"]))
        elif kind == "next_list":
            for it in sec.get("items") or []:
                body = f"• <b>{_sanitize(it.get('title', ''))}</b>"
                if it.get("detail"):
                    body += f" — {_sanitize(it['detail'])}"
                story.append(Paragraph(body, S["body"]))
        elif kind == "sql_block":
            sql = str(sec.get("sql") or "").strip()
            if sql:
                story.append(
                    Paragraph(
                        f"<font name='{_CJK_FONT}' size='9'>{_sanitize(sql)}</font>",
                        S["body"],
                    )
                )
        elif kind == "code_block":
            # Match canvas-docx: intermediate data-fetching/charting code is not deliverable content; skip the whole block.
            # Without this branch, it reaches the fallback and prints English placeholders, violating export contract ③.
            pass
        elif kind == "trend_chart":
            pts = sec.get("points") or []
            if pts:
                line = ", ".join(f"{_sanitize(p.get('label', ''))}={p.get('value', 0)}" for p in pts)
                story.append(Paragraph(f"<i>{_sanitize(line)}</i>", S["body"]))
            if sec.get("annotation"):
                story.append(Paragraph(_sanitize(sec["annotation"]), S["deck"]))
        elif kind == "datarc_iframe":
            # An iframe cannot be embedded in PDF; fall back to a link.
            url = sec.get("cardUrl")
            title = sec.get("title") or "Datarc card"
            if url:
                story.append(
                    Paragraph(
                        f"{_sanitize(title)}: <link href='{url}'>{_sanitize(url)}</link>",
                        S["body"],
                    )
                )
        # ── WP1 complete export path: these kinds previously reached else and printed "[unsupported kind]" ──
        elif kind == "chart":
            if sec.get("id") in consumed_chart_ids:
                continue  # Already rendered inside insight_card.
            spec = sec.get("spec") or {}
            story.extend(_chart_flowables(spec, S, workdir))
        elif kind == "insight_card":
            if sec.get("title"):
                story.append(Paragraph(_sanitize(sec["title"]), S["h2"]))
            conf = sec.get("confidence")
            if conf:
                story.append(Paragraph(f"<i>confidence: {_sanitize(conf)}</i>", S["eyebrow"]))
            if sec.get("thesis"):
                story.append(Paragraph(f"<b>{_md_to_reportlab(sec['thesis'])}</b>", S["body"]))
            if sec.get("narrative"):
                story.append(Paragraph(_md_to_reportlab(sec["narrative"]), S["body"]))
            for ev in sec.get("evidence") or []:
                cid = (ev or {}).get("chart_section_id")
                chart_sec = charts_by_id.get(cid) if cid else None
                if chart_sec:
                    story.extend(_chart_flowables(chart_sec.get("spec") or {}, S, workdir))
                else:
                    # F5 (B4 debt): two visual-reference types; image evidence also renders inside cards.
                    image_sec = images_by_id.get(cid) if cid else None
                    if image_sec:
                        story.extend(_image_flowables(image_sec, S))
                if (ev or {}).get("caption"):
                    story.append(Paragraph(_sanitize(ev["caption"]), S["eyebrow"]))
            story.append(Spacer(1, 6))
        elif kind == "image":
            if sec.get("id") in consumed_chart_ids:
                continue  # F5: images referenced by insight_card already render inside the card (same omission rule as charts).
            story.extend(_image_flowables(sec, S))
            if sec.get("caption"):
                story.append(Paragraph(_sanitize(sec["caption"]), S["eyebrow"]))
            story.append(Spacer(1, 6))
        elif kind == "design":
            title = sec.get("title") or "Editable design"
            width = sec.get("canvas_width") or "?"
            height = sec.get("canvas_height") or "?"
            artifact_id = sec.get("flattened_artifact_id")
            story.append(Paragraph(f"<b>{_sanitize(title)}</b>", S["h2"]))
            story.append(
                Paragraph(
                    f"Editable design canvas · {_sanitize(width)} × {_sanitize(height)} px",
                    S["body"],
                )
            )
            if artifact_id:
                story.append(
                    Paragraph(
                        f"Flattened PNG artifact: {_sanitize(artifact_id)}",
                        S["body"],
                    )
                )
            else:
                story.append(
                    Paragraph(
                        "Flatten this design from the canvas to export the PNG deliverable.",
                        S["body"],
                    )
                )
            story.append(Spacer(1, 6))
        elif kind == "recommendations":
            for it in sec.get("items") or []:
                pri = _sanitize(it.get("priority", ""))
                line = f"<b>[{pri}]</b> {_md_to_reportlab(it.get('action', ''))}"
                story.append(Paragraph(line, S["body"]))
                meta_bits = [_sanitize(x) for x in (it.get("owner"), it.get("timeline"), it.get("expected_impact")) if x]
                if meta_bits:
                    story.append(Paragraph(f"<i>{' · '.join(meta_bits)}</i>", S["eyebrow"]))
            story.append(Spacer(1, 6))
        elif kind == "sources":
            if sec.get("label"):
                story.append(Paragraph(_sanitize(sec["label"]), S["h2"]))
            for i, e in enumerate(sec.get("entries") or [], start=1):
                e = e or {}
                label = e.get("label") or e.get("tool") or e.get("artifact_id") or ""
                bits = [_sanitize(label)]
                if e.get("query"):
                    bits.append(_sanitize(e["query"])[:120])
                story.append(Paragraph(f"[{i}] {' — '.join(b for b in bits if b)}", S["src_line"]))
            story.append(Spacer(1, 6))
        elif kind in ("html_artifact", "external_iframe"):
            # Interactive content cannot enter PDF; use a placeholder card (design D4 export contract ③: visible degradation, no disappearance).
            # Include the title, description, and normalized embedded-data provenance in the export.
            title = sec.get("title") or "Interactive block"
            story.append(Paragraph("INTERACTIVE BLOCK", S["eyebrow"]))
            story.append(Paragraph(f"<b>{_sanitize(title)}</b>", S["body"]))
            story.append(
                Paragraph(
                    "<i>This block is interactive in the live report. Open the live report to explore the scenario yourself.</i>",
                    S["body"],
                )
            )
            if kind == "html_artifact":
                provenance_copy = sec.get("embedded_data_copy")
                if isinstance(provenance_copy, str) and provenance_copy:
                    story.append(Paragraph(f"<i>{_sanitize(provenance_copy)}</i>", S["eyebrow"]))
            story.append(Spacer(1, 6))
        else:
            # Export contract ③: never print "[unsupported kind]" placeholders. Silently degrade unknown kinds.
            #
            # ⚠ This branch previously **violated that rule itself** (like canvas-docx): with no body, it printed
            # `(<kind> — see the live report)`, observed at the start of user deliverables on 116 dogfood.
            # Adding a branch for one kind does not solve the issue; the next new kind will trigger it again. Now, print
            # nothing into the deliverable; report each omitted kind to stderr (captured by the runner).
            body_text = sec.get("markdown") or sec.get("text") or ""
            if body_text:
                story.append(Paragraph(_md_to_reportlab(str(body_text)), S["body"]))
            else:
                sys.stderr.write(f"[canvas-pdf] skipped unrenderable section kind={kind!r}\n")
    return story


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--workdir", required=True)
    args, _ = ap.parse_known_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    sections = data.get("sections") or []
    title = data.get("title") or "Bowhead Canvas"
    out_path = Path(args.workdir) / "output.pdf"

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=1 * inch,
        rightMargin=1 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=title,
        author="Bowhead",
    )
    story = build_story(sections, Path(args.workdir))
    if not story:
        story = [Paragraph("(empty canvas)", _styles()["body"])]
    doc.build(story)

    result = {
        "path": str(out_path),
        "sections": len(sections),
        "pages": getattr(doc, "page", None) or 1,
    }
    (Path(args.workdir) / "result.json").write_text(
        json.dumps(result, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
