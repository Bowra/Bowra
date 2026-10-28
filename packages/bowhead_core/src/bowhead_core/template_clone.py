"""Parse PDF and PPTX templates into bounded raw page structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ─────────────────────────────────────────────────────────────
# Parsed template (per-page raw structure pre-LLM)
# ─────────────────────────────────────────────────────────────


@dataclass
class ParsedPage:
    page_no: int
    chapter_tag: str  # Upper-left colored tag text, e.g. "全产品" / "格华止"
    text: str  # All page text
    tables: list[list[list[str]]]  # list of tables, each is 2D cells (None → "")
    has_chart: bool  # detected via image presence


@dataclass
class ParsedMaster:
    fonts: list[str] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)


@dataclass
class ParsedTemplate:
    source_path: str
    source_format: str  # "pdf" | "pptx"
    page_count: int
    pages: list[ParsedPage]
    master: ParsedMaster


# ─────────────────────────────────────────────────────────────
# 1. Parsing (PDF / PPTX → ParsedTemplate)
# ─────────────────────────────────────────────────────────────


_KNOWN_TAGS = ("全产品", "格华止", "康忻", "甲状腺", "市场分析", "预估", "活动", "人员管理")


def _detect_chapter_tag(text: str, known_tags: tuple[str, ...] = _KNOWN_TAGS) -> str:
    """页面前 80 字 头部探测 chapter tag."""
    head = (text or "")[:80]
    for tag in known_tags:
        if tag in head:
            return tag
    return ""


def parse_pdf(path: str | Path) -> ParsedTemplate:
    """PDF 解析 (依赖 pdfplumber + pypdf)."""
    p = Path(path)
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError("pdfplumber 未装, pip install pdfplumber") from e

    pages: list[ParsedPage] = []
    fonts: set[str] = set()
    colors: set[str] = set()
    with pdfplumber.open(str(p)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            tables = page.extract_tables() or []
            # Convert None to "" so the LLM sees actual cell placeholders (None means a merged span).
            tables_normalized = [[[(c or "") for c in row] for row in tbl] for tbl in tables]
            chapter_tag = _detect_chapter_tag(text)
            # Detect chart images (page.images contains embedded images).
            has_chart = bool(page.images)
            pages.append(
                ParsedPage(
                    page_no=i,
                    chapter_tag=chapter_tag,
                    text=text,
                    tables=tables_normalized,
                    has_chart=has_chart,
                )
            )
            # Extract master fonts + colors (first 3 pages are representative enough).
            if i <= 3:
                for ch in (page.chars or [])[:300]:
                    fn = ch.get("fontname")
                    if fn:
                        fonts.add(str(fn).split("+")[-1])  # Remove BCD prefix
                    col = ch.get("non_stroking_color")
                    if col:
                        colors.add(str(col)[:30])
    return ParsedTemplate(
        source_path=str(p),
        source_format="pdf",
        page_count=len(pages),
        pages=pages,
        master=ParsedMaster(fonts=sorted(fonts), colors=sorted(colors)),
    )


def parse_pptx(path: str | Path) -> ParsedTemplate:
    """PPTX 解析 (用 python-pptx). 此实现是 v0 — 拿 slide text + table cells.
    高级 placeholder/master 抽取留 Phase A5 (用 Anthropic pptx unpack.py)."""
    p = Path(path)
    try:
        from pptx import Presentation
    except ImportError as e:
        raise RuntimeError("python-pptx 未装") from e

    prs = Presentation(str(p))
    pages: list[ParsedPage] = []
    fonts: set[str] = set()
    colors: set[str] = set()
    for i, slide in enumerate(prs.slides, start=1):
        text_parts: list[str] = []
        tables: list[list[list[str]]] = []
        has_chart = False
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text
                if t:
                    text_parts.append(t)
            if shape.has_table:
                tbl = shape.table
                cells = [[cell.text for cell in row.cells] for row in tbl.rows]
                tables.append(cells)
            if shape.has_chart:
                has_chart = True
        text = "\n".join(text_parts)
        chapter_tag = _detect_chapter_tag(text)
        pages.append(
            ParsedPage(
                page_no=i,
                chapter_tag=chapter_tag,
                text=text,
                tables=tables,
                has_chart=has_chart,
            )
        )
    return ParsedTemplate(
        source_path=str(p),
        source_format="pptx",
        page_count=len(pages),
        pages=pages,
        master=ParsedMaster(fonts=sorted(fonts), colors=sorted(colors)),
    )


def parse_template(path: str | Path) -> ParsedTemplate:
    """根据扩展名 dispatch."""
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return parse_pdf(path)
    if ext == ".pptx":
        return parse_pptx(path)
    raise ValueError(f"unsupported template format: {ext}")
