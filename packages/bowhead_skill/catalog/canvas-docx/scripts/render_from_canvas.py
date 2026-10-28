"""Render ReportSection[] → .docx using python-docx.

Called by Bowhead SkillRuntime:
    python3 render_from_canvas.py --input <dir>/input.json --workdir <dir>

Output: workdir/output.docx
Writes result.json with {"path", "sections"}.

与 canvas-pdf 的差异（都是**有意**的，不是偷懒）：

- **不注册字体**。reportlab 要自己画字形，默认 Helvetica 没有中文字形，中文会变黑方块
  （飞书 bug 追#18）；docx 只写 OOXML，字形由 Word/WPS 端解析，不存在同款问题。
  但仍需显式设 `w:eastAsia`，否则部分 Word 版本会用西文默认字体去套中文，字距很难看。
- **不依赖 matplotlib**：chart 在 Word 里渲成**数据表**比渲成一张静态图更有用
  （可复制、可再算），这是有意选择，不是环境限制。
  ⚠ 本条原文曾是「app 容器里没有它（canvas-pdf 的 `_render_chart_png` 在 prod 实际一直走
  None 分支降级成表格）」——那个环境事实**已于 2026-08-22 修掉**（matplotlib 补进
  `bowhead_skill` 依赖），canvas-pdf 现在真能出图了。此处保留渲表是产品选择。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
except ImportError:
    sys.exit(3)


# Use the same Chinese font family as canvas-pdf; Word finds fonts by name and falls back automatically if missing, avoiding black squares.
_CJK = "Microsoft YaHei"
_MUTED = RGBColor(0x6B, 0x6A, 0x68)


def _sanitize(s: object) -> str:
    """OOXML 不接受 C0 控制字符（除 \\t\\n\\r）——混进去 Word 会报"文件已损坏"。"""
    text = "" if s is None else str(s)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _cjk(run) -> None:
    """让这个 run 的中文也走 `_CJK`（w:eastAsia 不设的话中文会套西文字体）。"""
    run.font.name = _CJK
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), _CJK)


# Underscore emphasis requires **word boundaries** (the same CommonMark rule); otherwise snake_case such as `pure_chrome_kind` and
# `province_str` is interpreted as italics and its underscores are swallowed
# (observed: `pure_chrome_kind` rendered as `purechromekind`).
_INLINE = re.compile(r"(\*\*.+?\*\*|(?<![0-9A-Za-z])__.+?__(?![0-9A-Za-z])|\*[^*]+?\*|(?<![0-9A-Za-z])_[^_]+?_(?![0-9A-Za-z])|`[^`]+?`)")


def _add_runs(para, md: str, *, size: int | None = None, bold: bool = False, italic: bool = False, color=None) -> None:
    """把一小段 markdown 内联标记铺成 runs（**粗** / *斜* / `代码`）。"""
    for part in _INLINE.split(_sanitize(md)):
        if not part:
            continue
        b, i, mono = bold, italic, False
        text = part
        if (part.startswith("**") and part.endswith("**")) or (part.startswith("__") and part.endswith("__")):
            text, b = part[2:-2], True
        elif (part.startswith("*") and part.endswith("*")) or (part.startswith("_") and part.endswith("_")):
            text, i = part[1:-1], True
        elif part.startswith("`") and part.endswith("`"):
            text, mono = part[1:-1], True
        # Unescape **last**: otherwise `\*` becomes `*` first and `_INLINE` then treats it as emphasis.
        run = para.add_run(_unescape_md(text))
        run.bold, run.italic = b, i
        if size:
            run.font.size = Pt(size)
        if color is not None:
            run.font.color.rgb = color
        if mono:
            run.font.name = "Consolas"
        else:
            _cjk(run)


def _para(doc, md: str, *, size: int | None = None, bold: bool = False, italic: bool = False, color=None, space_after: int = 6):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    _add_runs(p, md, size=size, bold=bold, italic=italic, color=color)
    return p


# `[figure:sec_xxx]` is an **internal canvas image anchor**; the frontend uses it to render the image after this paragraph.
# The anchor itself is not reader-facing text. canvas-pdf already had two regexes to strip it; docx had **none**,
# leaving two `[figure:sec_86744592bdcb]` lines in the user's Word document (observed on 116 production 1.1.18).
# This is the same issue as the earlier English placeholders: internal markers leak into the deliverable.
_FIG_ANCHOR_LINE = re.compile(r"^\s*\[figure:[A-Za-z0-9_]+\]\s*$")
_FIG_ANCHOR_INLINE = re.compile(r"\[figure:[A-Za-z0-9_]+\]")

# Markdown table: `| a | b |` followed by a `|---|:--:|` separator line.
# **One or more** hyphens per cell is valid (GFM spec); `-{2,}` fails to recognize valid tables such as `|-|-|`,
# sending them to `_para()` as a line of raw pipe-delimited text.
_MD_TABLE_SEP = re.compile(r"^\s*\|\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
# A pipe not escaped with a backslash is a real table column separator.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
# CommonMark backslash escape = `\` + ASCII punctuation.
_MD_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")

# ⚠ The four constants above and `_unescape_md` / `_md_row_cells` are duplicated in **four places**:
#   - `bowhead_core/report_writer_loop.py` (`_TABLE_SEP_RE` / `_UNESCAPED_PIPE_RE` / `_split_row`, canonical)
#   - `bowhead_skill/catalog/canvas-docx/scripts/render_from_canvas.py` (this file)
#   - `bowhead_skill/catalog/canvas-pdf/scripts/render_from_canvas.py`
#   - `bowhead_gateway/feishu/renderer.py`
# This is deliberate: skill scripts run independently in the sandbox and **cannot import `bowhead_core`**.
# When changing any parsing criterion, update all four copies; `report_writer_loop` is canonical.


def _unescape_md(text: str) -> str:
    """还原 markdown 的反斜杠转义 —— 导出器是**渲染终点**，不能把 `\\-` 印给用户。

    上游 `markdown-pm.ts::_serializeTableCell` 把每个单元格当独立文档序列化，
    行首的 `-` / `N.` / `>` 会被 prosemirror-markdown 的 `esc(startOfLine)` 加上反斜杠
    （`-37.5%` → `\\-37.5%`）；竖线在单元格里则**必须**保持 `\\|` 才不会劈成两格。
    写路径已经不再产出行首那批（见 `_unescapeBlockStart`），但**存量 markdown 里还有**，
    而且 `\\|` 是有意保留的 —— 所以渲染侧必须自己认。
    """
    return _MD_ESCAPE.sub(r"\1", text)


def _md_row_cells(line: str) -> list[str]:
    """`| 上海 | 3.09 |` → ['上海', '3.09']（去掉首尾空管道）。

    只在**未转义**的竖线处切：`\\|` 是单元格内的字面竖线，裸 `split("|")` 会把它当分隔符，
    该行凭空多一列 ⇒ 整行右移错位。
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [p.strip() for p in _UNESCAPED_PIPE.split(s)]


def _take_md_table(lines: list[str], i: int) -> tuple[list[str], list[list[str]], int] | None:
    """从 `lines[i]` 起尝试吃下一整块 markdown 表格 → (表头, 数据行, 下一行下标)。"""
    if i + 1 >= len(lines):
        return None
    if not lines[i].strip().startswith("|") or not _MD_TABLE_SEP.match(lines[i + 1]):
        return None
    header = _md_row_cells(lines[i])
    body: list[list[str]] = []
    j = i + 2
    while j < len(lines) and lines[j].strip().startswith("|"):
        body.append(_md_row_cells(lines[j]))
        j += 1
    return header, body, j


def _narrative(doc, md: str) -> None:
    """markdown 正文：支持 #/## 标题、- 无序、1. 有序、> 引用、**表格**，其余当段落。

    表格这一支是 116 prod 1.0.79 dogfood 补的：writer 的铁律 6 明写「对比用 markdown
    表格」，画布上前端有 remark-gfm 渲得好好的，**只有导出器不认**——于是用户的 Word 里
    躺着 14 行 `| 省份 | 营业额（亿元） |` 和 `|------|------|` 分隔线。
    系统自己要求写 markdown 表，渲染器就必须认。
    """
    lines = [raw.rstrip() for raw in (md or "").splitlines()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        tbl = _take_md_table(lines, i)
        if tbl is not None:
            header, body, i = tbl
            _table(doc, header, body)
            continue
        i += 1
        if _FIG_ANCHOR_LINE.match(line):
            continue  # A standalone figure anchor: discard the whole line (chart/image sections render the image themselves).
        line = _FIG_ANCHOR_INLINE.sub("", line).strip()
        if not line:
            continue
        if line.startswith("### "):
            _para(doc, line[4:], size=12, bold=True)
        elif line.startswith("## "):
            _para(doc, line[3:], size=13, bold=True)
        elif line.startswith("# "):
            _para(doc, line[2:], size=15, bold=True)
        elif re.match(r"^\s*[-*+]\s+", line):
            p = doc.add_paragraph(style="List Bullet")
            _add_runs(p, re.sub(r"^\s*[-*+]\s+", "", line))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            p = doc.add_paragraph(style="List Number")
            _add_runs(p, re.sub(r"^\s*\d+[.)]\s+", "", line))
        elif line.startswith("> "):
            _para(doc, line[2:], italic=True, color=_MUTED)
        else:
            _para(doc, line)


def _table(doc, header: list[str], rows: list[list[str]]) -> None:
    if not header or not rows:
        return
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    for cell, text in zip(t.rows[0].cells, header, strict=False):
        cell.text = ""
        _add_runs(cell.paragraphs[0], str(text), bold=True, size=9)
    for r in rows:
        cells = t.add_row().cells
        for cell, text in zip(cells, r, strict=False):
            cell.text = ""
            _add_runs(cell.paragraphs[0], str(text), size=9)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def _fmt_num(v: object) -> str | None:
    """浮点数 → 人看的写法；不是浮点就返回 None（交给 `_sanitize`）。

    dev 1.0.77 dogfood P2-c：表格里直接躺着 `21778172.57949218`、
    `18.070109577367795`——而同一份文件的 KPI 区是 `¥210.94M / 17.23%`，
    格式化能力就在隔壁，只是数据表这条路没接。

    **只管 float，不碰 int**：年份 / 排名 / ID / 计数在 JSON 里都是 int，
    给它们加千分位（`2026` → `2,026`）反而是新 bug。真正难看的那批全是 float。
    """
    if isinstance(v, bool) or not isinstance(v, float):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # Leave NaN / inf unchanged.
        return None
    if v == 0:
        return "0"
    if abs(v) < 0.01:
        return f"{v:.4g}"  # Use significant digits for small magnitudes; two decimal places would round them to 0.00.
    if abs(v - round(v)) < 1e-9:
        return f"{round(v):,}"
    return f"{v:,.2f}"


def _cell_text(v: object) -> str:
    """data_table 的格子可能是 {delta: ...} 这种富结构（与 canvas-pdf 同口径）。"""
    if isinstance(v, dict):
        v = v.get("delta", v.get("value", ""))
    return _fmt_num(v) or _sanitize(v)


# Display limits imposed by Word page width (preserve the meaning of omitted content with notes below the table; see comments below).
_MAX_TABLE_COLS = 8
_MAX_TABLE_ROWS = 50


def _chart_as_table(doc, spec: dict) -> None:
    """chart 在 Word 里降级成数据表——比降级成一句文字有用得多。"""
    rows = spec.get("rows") or spec.get("data") or []
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return
    # Word page width cannot accommodate too many columns/rows, so impose limits, but **never drop content silently** (export contract ③:
    # make degradation visible, never let content disappear). On 116 production 1.1.18, an 11-column period-over-period table exported to Word
    # had only 8 columns, silently losing 3, while the same Excel export retained all 11 columns.
    all_keys = list(rows[0].keys())
    keys = all_keys[:_MAX_TABLE_COLS]
    _table(doc, keys, [[_cell_text(r.get(k)) for k in keys] for r in rows[:_MAX_TABLE_ROWS]])
    dropped_cols = all_keys[_MAX_TABLE_COLS:]
    dropped_rows = max(0, len(rows) - _MAX_TABLE_ROWS)
    notes = []
    if dropped_cols:
        notes.append("另有 " + str(len(dropped_cols)) + " 列未显示：" + "、".join(_sanitize(k) for k in dropped_cols))
    if dropped_rows:
        notes.append("另有 " + str(dropped_rows) + " 行未显示")
    if notes:
        _para(doc, "（" + "；".join(notes) + "。完整数据见画布或 Excel 导出。）", size=9, italic=True, color=_MUTED)


def _image(doc, sec: dict, workdir: Path) -> None:
    """嵌图；文件不可用时降级成 alt 文本（绝不静默蒸发）。"""
    src = sec.get("path") or sec.get("src") or ""
    local = workdir / Path(str(src)).name if src else None
    if local and local.is_file() and not local.is_symlink():
        try:
            doc.add_picture(str(local))
        except Exception:  # noqa: BLE001 — An image failure must not break the whole document; fall back to alt text.
            _para(doc, f"({_sanitize(sec.get('alt') or 'image')} — see the live report)", italic=True, color=_MUTED)
        return
    _para(doc, f"({_sanitize(sec.get('alt') or 'image')} — see the live report)", italic=True, color=_MUTED)


def render(doc, sections: list, workdir: Path) -> None:
    # Match canvas-pdf/frontend preprocessSections: chart/image sections explicitly referenced by insight_card
    # render inside the card and are omitted at the top level to avoid duplicating the same image.
    # ⚠ Once omitted there, insight_card itself **must** render, or the image disappears (see the branch below).
    by_id: dict[str, dict] = {s["id"]: s for s in sections if isinstance(s, dict) and s.get("id")}
    consumed: set = set()
    for s in sections:
        if isinstance(s, dict) and s.get("kind") == "insight_card":
            for ev in s.get("evidence") or []:
                cid = (ev or {}).get("chart_section_id")
                if cid:
                    consumed.add(cid)

    for sec in sections:
        if not isinstance(sec, dict):
            continue
        kind = sec.get("kind")
        if sec.get("id") and sec["id"] in consumed and kind in ("chart", "image"):
            continue

        if kind == "eyebrow":
            _para(doc, sec.get("text", ""), size=9, color=_MUTED)
        elif kind == "h1":
            _para(doc, sec.get("text", ""), size=20, bold=True, space_after=10)
        elif kind == "deck":
            _para(doc, sec.get("markdown", ""), size=12, color=_MUTED)
        elif kind == "meta":
            _para(doc, " · ".join(_sanitize(p) for p in sec.get("pills") or []), size=9, color=_MUTED)
        elif kind == "kpi_row":
            cells = sec.get("cells") or []
            if cells:
                _table(
                    doc,
                    [_sanitize(c.get("lbl", "")) for c in cells],
                    [[_sanitize(c.get("val", "")) for c in cells]],
                )
        elif kind == "section":
            if sec.get("label"):
                _para(doc, sec["label"], size=9, color=_MUTED)
            if sec.get("h2"):
                _para(doc, sec["h2"], size=15, bold=True, space_after=8)
            if sec.get("body"):
                render(doc, sec["body"], workdir)
        elif kind == "narrative":
            _narrative(doc, sec.get("markdown", ""))
        elif kind == "data_table":
            cols = sec.get("columns") or []
            rows = sec.get("rows") or []
            if cols and rows:
                if sec.get("title"):
                    _para(doc, sec["title"], bold=True)
                header = [_sanitize(c.get("header", c.get("key"))) for c in cols]
                _table(doc, header, [[_cell_text(r.get(c["key"])) for c in cols] for r in rows])
        elif kind == "attribution":
            summary = sec.get("summary") or {}
            if summary:
                _para(doc, f"{_sanitize(summary.get('label', ''))}: {_sanitize(summary.get('value', ''))} ({_sanitize(summary.get('pct', ''))})")
            rows = sec.get("rows") or []
            if rows:
                _table(
                    doc,
                    ["#", "维度 · 属性", "去期", "本期", "占比"],
                    [
                        [
                            _sanitize(r.get("rank", "")),
                            f"{_sanitize(r.get('dim', ''))} · {_sanitize(r.get('name', ''))}",
                            _cell_text(r.get("from", "")),
                            _cell_text(r.get("to", "")),
                            _cell_text(r.get("pct", "")),
                        ]
                        for r in rows
                    ],
                )
            if sec.get("insight"):
                _para(doc, sec["insight"])
        elif kind == "callout":
            label = sec.get("label")
            body = sec.get("markdown", "")
            _para(doc, f"**{_sanitize(label)}** — {body}" if label else body, italic=True)
        elif kind == "next_list":
            for it in sec.get("items") or []:
                p = doc.add_paragraph(style="List Bullet")
                _add_runs(p, f"**{_sanitize(it.get('title', ''))}**" + (f" — {_sanitize(it['detail'])}" if it.get("detail") else ""))
        elif kind == "sql_block":
            sql = str(sec.get("sql") or "").strip()
            if sql:
                p = _para(doc, "", size=9)
                run = p.add_run(_sanitize(sql))
                run.font.name = "Consolas"
                run.font.size = Pt(9)
        elif kind == "code_block":
            # Intermediate data-fetching/charting code is not deliverable content; skip the whole block, as required by this file's final export contract ③:
            # do not print placeholders; silently degrade unknown kinds.
            # Without this branch, code_block reached the fallback, leaving the **first 4 lines** of the user's Word document
            # as English `(code_block — see the live report)` placeholders (observed on 116 dogfood 1.0.74,
            # English noise in a Chinese UI). Either skip explicitly or render actual code; skipping is appropriate for a business summary.
            pass
        elif kind == "trend_chart":
            pts = sec.get("points") or []
            if pts:
                _table(doc, ["label", "value"], [[_sanitize(p.get("label", "")), _sanitize(p.get("value", ""))] for p in pts])
            if sec.get("annotation"):
                _para(doc, sec["annotation"], italic=True, color=_MUTED)
        elif kind == "insight_card":
            # ★ Must render: deduplication above skips referenced chart/image sections at the top level;
            # if the card itself does not render here, the image **disappears completely from the document**
            # (on 116, four consecutive placeholder notes at the start of another user's export indicated this missing rendering).
            if sec.get("title"):
                _para(doc, sec["title"], size=15, bold=True, space_after=8)
            if sec.get("confidence"):
                _para(doc, f"confidence: {_sanitize(sec['confidence'])}", size=9, color=_MUTED)
            if sec.get("thesis"):
                _para(doc, f"**{_sanitize(sec['thesis'])}**")
            if sec.get("narrative"):
                _narrative(doc, sec["narrative"])
            for ev in sec.get("evidence") or []:
                ev = ev or {}
                cid = ev.get("chart_section_id")
                ref = (by_id.get(cid) if cid else None) or {}
                if ref.get("kind") == "chart":
                    if ref.get("title"):
                        _para(doc, ref["title"], bold=True)
                    _chart_as_table(doc, ref.get("spec") or ref)
                elif ref.get("kind") == "image":
                    _image(doc, ref, workdir)
                if ev.get("caption"):
                    _para(doc, ev["caption"], size=9, color=_MUTED)
        elif kind == "recommendations":
            for it in sec.get("items") or []:
                pri = _sanitize(it.get("priority", ""))
                p = doc.add_paragraph(style="List Bullet")
                _add_runs(p, (f"**[{pri}]** " if pri else "") + _sanitize(it.get("action", "")))
                bits = [_sanitize(x) for x in (it.get("owner"), it.get("timeline"), it.get("expected_impact")) if x]
                if bits:
                    _para(doc, " · ".join(bits), size=9, italic=True, color=_MUTED)
        elif kind == "sources":
            if sec.get("label"):
                _para(doc, sec["label"], size=15, bold=True, space_after=8)
            for i, e in enumerate(sec.get("entries") or [], start=1):
                e = e or {}
                label = e.get("label") or e.get("tool") or e.get("artifact_id") or ""
                bits = [_sanitize(label)]
                if e.get("query"):
                    bits.append(_sanitize(e["query"])[:120])
                _para(doc, f"[{i}] " + " — ".join(b for b in bits if b), size=9, color=_MUTED, space_after=2)
        elif kind in ("html_artifact", "external_iframe", "design"):
            # Interactive/editable content cannot enter Word; fall back to a visible placeholder (export contract ③: visible degradation, no disappearance).
            title = sec.get("title") or ("Editable design" if kind == "design" else "Interactive block")
            _para(doc, title, bold=True)
            if sec.get("description"):
                _para(doc, sec["description"], size=9, color=_MUTED)
            _para(doc, f"（{kind} — 交互内容请见在线报告）", size=9, italic=True, color=_MUTED)
        elif kind == "chart":
            # ⚠ A chart section payload has shape `{"spec": {...}}`, with its title in `spec.title`.
            # This previously read `sec["title"]`, always empty for real charts, so every chart became an **unnamed table** in Word.
            # 116 1.2.71 dogfood: six charts became six anonymous tables near the beginning; the narrative's `图N：` references
            # pointed elsewhere, making it impossible to match them. canvas-pdf reads spec at the equivalent site (`_chart_flowables`).
            spec = sec.get("spec") or sec
            if spec.get("title"):
                _para(doc, spec["title"], bold=True)
            if spec.get("subtitle"):
                _para(doc, spec["subtitle"], size=9, color=_MUTED)
            _chart_as_table(doc, spec)
        elif kind == "image":
            _image(doc, sec, workdir)
            if sec.get("caption"):
                _para(doc, sec["caption"], size=9, color=_MUTED)
        elif kind == "datarc_iframe":
            url = sec.get("cardUrl")
            title = sec.get("title") or "Datarc card"
            if url:
                _para(doc, f"{_sanitize(title)}: {_sanitize(url)}")
        else:
            # Export contract ③: never print "[unsupported kind]" placeholders. Silently degrade unknown kinds.
            #
            # ⚠ This branch previously **violated that rule itself**: with no body, it printed
            # `(<kind> — see the live report)`. On 116 dogfood 1.0.74, the user's Word document
            # began with four such English notes (the missing kind was code_block, now handled separately).
            # Adding one kind-specific branch does not solve the issue: **the next new kind will trigger it again**.
            # Now, print nothing into the deliverable; report each omitted kind to stderr (captured by the runner,
            # visible to the model and ops), keeping the file clean without silently losing content.
            body_text = sec.get("markdown") or sec.get("text") or ""
            if body_text:
                _narrative(doc, str(body_text))
            else:
                sys.stderr.write(f"[canvas-docx] skipped unrenderable section kind={kind!r}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--workdir", required=True)
    args, _ = ap.parse_known_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    sections = data.get("sections") or []
    title = data.get("title") or "Bowhead Canvas"
    workdir = Path(args.workdir)
    out_path = workdir / "output.docx"

    doc = Document()
    doc.core_properties.title = title
    doc.core_properties.author = "Bowhead"
    normal = doc.styles["Normal"]
    normal.font.size = Pt(10.5)
    normal.font.name = _CJK
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), _CJK)

    render(doc, sections, workdir)
    if not doc.paragraphs and not doc.tables:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_runs(p, "(empty canvas)", italic=True, color=_MUTED)

    doc.save(str(out_path))

    result = {"path": str(out_path), "sections": len(sections)}
    (workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
