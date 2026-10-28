"""Fail-closed PDF renderer and closed-file evidence extractor.

The renderer consumes only frozen ExportIR and already-materialized visual
pixels.  It never rebuilds a chart from rows.  The extractor reopens the
closed PDF, proves visible text and embedded pixels, rasterizes every page
with Poppler, and returns content-free evidence for ``export_validation``.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import shutil
import subprocess
import unicodedata
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops
from pypdf import PdfReader
from pypdf.generic import DictionaryObject, IndirectObject
from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.enums import TA_CENTER, TA_LEFT  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
from reportlab.lib.units import mm  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # type: ignore[import-untyped]
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    Flowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import (
    Image as ReportLabImage,
)

from bowhead_core.error_catalog import CodedError
from bowhead_core.export_ir import ExportIR, ExportIRBlock, iter_export_blocks
from bowhead_core.export_manifest import (
    ExtractedOutputEvidence,
    ExtractedSectionEvidence,
    ManifestDisposition,
    ManifestReasonCode,
    OutputTargetKind,
    OutputTargetRef,
)
from bowhead_core.export_materializer import MaterializedVisual
from bowhead_core.export_native_palette import NativeExportPalette, native_export_palette
from bowhead_core.print_theme import PrintThemeError

PDF_RENDERER_NAME = "canvas-reportlab-pdf-v2"
# "4": keep figure captions and stale-data qualifications with the last visual
# in their figure, while allowing large compositions to span pages.
PDF_RENDERER_VERSION = "4"
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_PAGES = 250
MAX_TABLE_ROWS = 100_000
_A4_WIDTH, _A4_HEIGHT = A4
_MARKER_RE = re.compile(r"BH1S_([0-9a-f]{24})_([0-9a-f]{64})")
_DEFAULT_DEMO_FINGERPRINTS = (
    "CMP-2604-001",
    "Microsoft_Excel_Worksheet2.xlsx",
)
_LOGGER = logging.getLogger(__name__)


class PdfExportError(CodedError):
    """The PDF cannot be rendered or validated without losing parity."""


@dataclass(frozen=True, slots=True)
class PdfRenderResult:
    relative_path: str
    content_sha256: str
    size_bytes: int
    page_count: int
    renderer_name: str = PDF_RENDERER_NAME
    renderer_version: str = PDF_RENDERER_VERSION


@dataclass(frozen=True, slots=True)
class _PdfStyles:
    body: ParagraphStyle
    heading_1: ParagraphStyle
    heading_2: ParagraphStyle
    eyebrow: ParagraphStyle
    code: ParagraphStyle
    callout: ParagraphStyle
    fallback: ParagraphStyle
    table_header: ParagraphStyle
    table_cell: ParagraphStyle
    title: ParagraphStyle
    palette: NativeExportPalette


class _ReceiptMarker(Flowable):  # type: ignore[misc]
    """Tiny locator painted in the page color; never accepted as semantic or visual proof."""

    def __init__(self, value: str, *, page_color: str) -> None:
        super().__init__()
        self.value = value
        self.page_color = page_color
        self.width = 1
        # Invisible evidence locators must not consume page space or create a
        # trailing page containing only the final block's receipt marker.
        self.height = 0

    def draw(self) -> None:
        canvas = self.canv
        canvas.saveState()
        # Deliberately invisible: the page color, not a contrast pair.
        canvas.setFillColor(colors.HexColor(self.page_color))
        canvas.setFont("Helvetica", 0.5)
        canvas.drawString(0, 0, self.value)
        canvas.restoreState()


def render_export_pdf(
    *,
    export_ir: ExportIR,
    materialized_visuals: tuple[MaterializedVisual, ...],
    attempt_dir: Path,
    print_theme: dict[str, Any],
    output_name: str = "output.pdf",
) -> PdfRenderResult:
    """Render an immutable ExportIR to a staged PDF and close the writer.

    ``print_theme`` is the job's frozen print-theme snapshot; every page color is resolved
    from it (``export_native_palette``), so a different theme needs no renderer change.
    """
    try:
        palette = native_export_palette(print_theme)
    except PrintThemeError as exc:
        raise PdfExportError("pdf_print_theme_invalid") from exc
    output_path = _prepare_output_path(
        attempt_dir=attempt_dir,
        output_name=output_name,
    )
    visuals = _index_visuals(
        export_ir=export_ir,
        materialized_visuals=materialized_visuals,
        attempt_dir=attempt_dir,
    )
    normal_font, bold_font = _register_pdf_fonts()
    styles = _build_styles(
        normal_font=normal_font,
        bold_font=bold_font,
        palette=palette,
    )
    story: list[Flowable] = [
        Paragraph(_paragraph_markup(export_ir.document.title), styles.title),
        Spacer(1, 5 * mm),
    ]
    in_appendix = False
    for block in export_ir.blocks:
        is_appendix = block.source.surface_placement in {
            "data_tab",
            "code_tab",
        }
        if is_appendix and not in_appendix:
            story.extend(
                [
                    PageBreak(),
                    Paragraph(
                        "附录" if export_ir.document.locale.startswith("zh") else "Appendix",
                        styles.heading_1,
                    ),
                ]
            )
            in_appendix = True
        story.extend(
            _render_block_tree(
                block=block,
                visuals=visuals,
                attempt_dir=attempt_dir,
                styles=styles,
            )
        )
    if not story:
        raise PdfExportError("pdf_story_empty")

    part_path = output_path.with_suffix(".pdf.part")
    if part_path.exists() or part_path.is_symlink():
        raise PdfExportError("pdf_staging_conflict")
    document = SimpleDocTemplate(
        str(part_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title=export_ir.document.title,
        author="Bowhead",
        creator=f"{PDF_RENDERER_NAME}/{PDF_RENDERER_VERSION}",
        pageCompression=1,
    )
    try:
        document.build(
            story,
            onFirstPage=lambda canvas, doc: _draw_page_chrome(canvas, doc, palette=palette),
            onLaterPages=lambda canvas, doc: _draw_page_chrome(canvas, doc, palette=palette),
        )
    except Exception as exc:
        part_path.unlink(missing_ok=True)
        raise PdfExportError("pdf_render_failed") from exc
    size_bytes = part_path.stat().st_size
    if size_bytes <= 0 or size_bytes > MAX_PDF_BYTES:
        part_path.unlink(missing_ok=True)
        raise PdfExportError("pdf_size_invalid")
    try:
        reader = PdfReader(str(part_path))
        page_count = len(reader.pages)
    except Exception as exc:
        part_path.unlink(missing_ok=True)
        raise PdfExportError("pdf_parser_failed") from exc
    if page_count <= 0 or page_count > MAX_PDF_PAGES:
        part_path.unlink(missing_ok=True)
        raise PdfExportError("pdf_page_count_invalid")
    part_path.replace(output_path)
    content = output_path.read_bytes()
    return PdfRenderResult(
        relative_path=output_path.relative_to(attempt_dir).as_posix(),
        content_sha256=_sha256_bytes(content),
        size_bytes=len(content),
        page_count=page_count,
    )


def extract_pdf_evidence(
    *,
    pdf_path: Path,
    export_ir: ExportIR,
    materialized_visuals: tuple[MaterializedVisual, ...],
    validation_dir: Path,
    pdftoppm_path: Path | None = None,
    demo_fingerprints: tuple[str, ...] = _DEFAULT_DEMO_FINGERPRINTS,
) -> ExtractedOutputEvidence:
    """Reopen a closed PDF and independently prove its visible output."""
    size_bytes = pdf_path.stat().st_size if pdf_path.is_file() else 0
    mime_magic_valid = size_bytes > 0 and pdf_path.read_bytes()[:5] == b"%PDF-"
    if not mime_magic_valid:
        return ExtractedOutputEvidence(
            mime_magic_valid=False,
            parser_opened=False,
            size_bytes=size_bytes,
            sections=(),
            hard_check_failures=("pdf_mime_magic_invalid",),
        )
    try:
        reader = PdfReader(str(pdf_path))
        if reader.is_encrypted:
            return ExtractedOutputEvidence(
                mime_magic_valid=True,
                parser_opened=False,
                size_bytes=size_bytes,
                sections=(),
                hard_check_failures=("pdf_encrypted",),
            )
        page_texts = tuple(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ExtractedOutputEvidence(
            mime_magic_valid=True,
            parser_opened=False,
            size_bytes=size_bytes,
            sections=(),
            hard_check_failures=("pdf_parser_failed",),
        )

    hard_failures: set[str] = set()
    if not 0 < len(reader.pages) <= MAX_PDF_PAGES:
        hard_failures.add("pdf_page_count_invalid")
    hard_failures.update(_page_size_failures(reader))
    if any("\ufffd" in text or "\x00" in text for text in page_texts):
        hard_failures.add("pdf_missing_glyph")
    full_text = "\n".join(page_texts)
    expected_text = _expected_visible_text(export_ir)
    demo_residue_count = _demo_residue_count(
        full_text=full_text,
        expected_text=expected_text,
        fingerprints=demo_fingerprints,
    )

    raster_failures = _rasterize_and_check_pages(
        pdf_path=pdf_path,
        validation_dir=validation_dir,
        expected_pages=len(reader.pages),
        pdftoppm_path=pdftoppm_path,
    )
    hard_failures.update(raster_failures)
    if _contains_non_ascii(expected_text) and not _has_embedded_font(reader):
        hard_failures.add("pdf_font_not_embedded")

    extracted_images, image_failures = _extract_pdf_images(reader)
    hard_failures.update(image_failures)
    image_pages: dict[str, deque[int]] = defaultdict(deque)
    for pixel_sha256, page_number in extracted_images:
        image_pages[pixel_sha256].append(page_number)
    visuals = {visual.block_key: visual for visual in materialized_visuals}
    expected_visual_keys = {block.block_key for block in iter_export_blocks(export_ir.blocks) if block.visual_input_sha256 is not None}
    if set(visuals) != expected_visual_keys:
        hard_failures.add("pdf_visual_materialization_mismatch")

    blocks = iter_export_blocks(export_ir.blocks)
    known_marker_ids = {_marker_id(block.block_key) for block in blocks}
    unexpected_markers = [marker for marker, _source_digest in _MARKER_RE.findall(full_text) if marker not in known_marker_ids]
    sections: list[ExtractedSectionEvidence] = []
    for block in blocks:
        evidence, block_failures = _extract_block_evidence(
            block=block,
            page_texts=page_texts,
            visual=visuals.get(block.block_key),
            image_pages=image_pages,
        )
        hard_failures.update(block_failures)
        if evidence is not None:
            sections.append(evidence)

    leftover_images = sum(len(pages) for pages in image_pages.values())
    if leftover_images:
        hard_failures.add("unexpected_pdf_image")
    if unexpected_markers:
        hard_failures.add("unexpected_pdf_section_marker")
    if len(sections) != len(blocks):
        hard_failures.add("pdf_section_accounting_mismatch")
    warnings = tuple(sorted({warning for visual in materialized_visuals for warning in visual.warnings}))
    return ExtractedOutputEvidence(
        mime_magic_valid=True,
        parser_opened=True,
        size_bytes=size_bytes,
        sections=tuple(sections),
        unexpected_business_section_count=len(unexpected_markers),
        demo_residue_count=demo_residue_count,
        hard_check_failures=tuple(sorted(hard_failures)),
        warnings=warnings,
    )


def _prepare_output_path(
    *,
    attempt_dir: Path,
    output_name: str,
) -> Path:
    if not attempt_dir.is_absolute() or Path(output_name).name != output_name or not output_name.endswith(".pdf"):
        raise PdfExportError("pdf_staging_invalid")
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if attempt_dir.is_symlink() or not attempt_dir.is_dir():
        raise PdfExportError("pdf_staging_invalid")
    output_dir = attempt_dir / "output"
    output_dir.mkdir(mode=0o700, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise PdfExportError("pdf_staging_invalid")
    output_path = output_dir / output_name
    if output_path.exists() or output_path.is_symlink():
        raise PdfExportError("pdf_staging_conflict")
    return output_path


def _index_visuals(
    *,
    export_ir: ExportIR,
    materialized_visuals: tuple[MaterializedVisual, ...],
    attempt_dir: Path,
) -> dict[str, MaterializedVisual]:
    visuals = {visual.block_key: visual for visual in materialized_visuals}
    if len(visuals) != len(materialized_visuals):
        raise PdfExportError("duplicate_materialized_visual")
    expected = {block.block_key for block in iter_export_blocks(export_ir.blocks) if block.visual_input_sha256 is not None}
    if set(visuals) != expected:
        raise PdfExportError("visual_materialization_accounting_mismatch")
    root = attempt_dir.resolve()
    for visual in materialized_visuals:
        relative = Path(visual.relative_path)
        path = attempt_dir / relative
        if relative.is_absolute() or ".." in relative.parts or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise PdfExportError("visual_staging_invalid")
        content = path.read_bytes()
        if _sha256_bytes(content) != visual.content_sha256:
            raise PdfExportError("visual_content_changed")
    return visuals


def _render_block_tree(
    *,
    block: ExportIRBlock,
    visuals: dict[str, MaterializedVisual],
    attempt_dir: Path,
    styles: _PdfStyles,
) -> list[Flowable]:
    start = _ReceiptMarker(_start_marker(block), page_color=styles.palette.page)
    end = _ReceiptMarker(_end_marker(block), page_color=styles.palette.page)
    body = _render_block_body(
        block=block,
        visual=visuals.get(block.block_key),
        attempt_dir=attempt_dir,
        styles=styles,
    )
    children: list[Flowable] = []
    figure_start = 0
    figure_key: str | None = None
    for child in block.children:
        rendered = _render_block_tree(
            block=child,
            visuals=visuals,
            attempt_dir=attempt_dir,
            styles=styles,
        )
        if child.content.get("role") == "figure_caption" and child.content.get("caption_for") == figure_key:
            # Keep the final plot and its source notes with the caption, rather
            # than forcing an entire multi-plot composition onto a single page.
            last_visual = next((i for i in range(len(children) - 1, figure_start - 1, -1) if isinstance(children[i], KeepTogether)), None)
            if last_visual is not None:
                # Flatten our existing group: nested KeepTogether reports a
                # sentinel height and forces a page break even when it fits.
                grouped = children[last_visual]._content  # noqa: SLF001
                children[last_visual:] = [KeepTogether([*grouped, *children[last_visual + 1 :], *rendered])]
                continue
        else:
            figure_start, figure_key = len(children), child.block_key
        children.extend(rendered)
    if block.block_kind == "visual":
        return [KeepTogether([start, *body, *children, end])]
    return [start, *body, *children, end]


def _render_block_body(
    *,
    block: ExportIRBlock,
    visual: MaterializedVisual | None,
    attempt_dir: Path,
    styles: _PdfStyles,
) -> list[Flowable]:
    if block.block_kind == "visual":
        if visual is None:
            raise PdfExportError("visual_materialization_missing")
        return _visual_flowables(
            block=block,
            visual=visual,
            attempt_dir=attempt_dir,
            styles=styles,
        )
    if block.block_kind == "table":
        return _table_flowables(block=block, styles=styles)
    if block.block_kind == "kpi_group":
        return _kpi_flowables(block=block, styles=styles)
    if block.block_kind == "interactive_fallback":
        return _fallback_flowables(block=block, styles=styles)
    if block.block_kind == "code":
        return [
            Paragraph(
                _paragraph_markup(token),
                styles.code,
            )
            for token in block.semantic_tokens
        ] or [Paragraph("—", styles.code)]
    if block.block_kind == "heading":
        level = block.content.get("level")
        style = styles.heading_1 if level == 1 else styles.heading_2
        return [Paragraph(_paragraph_markup(token), style) for token in block.semantic_tokens]
    if block.source.section_kind == "eyebrow":
        style = styles.eyebrow
    elif block.block_kind == "callout":
        style = styles.callout
    else:
        style = styles.body
    return [Paragraph(_paragraph_markup(token), style) for token in block.semantic_tokens] or [Paragraph("—", style)]


def _visual_flowables(
    *,
    block: ExportIRBlock,
    visual: MaterializedVisual,
    attempt_dir: Path,
    styles: _PdfStyles,
) -> list[Flowable]:
    path = attempt_dir / visual.relative_path
    max_width = 170 * mm
    max_height = 96 * mm
    scale = min(
        max_width / visual.width,
        max_height / visual.height,
        1.0,
    )
    image = ReportLabImage(
        str(path),
        width=visual.width * scale,
        height=visual.height * scale,
    )
    image.hAlign = "CENTER"
    flowables: list[Flowable] = [
        Paragraph(
            _paragraph_markup(token),
            styles.body,
        )
        for token in block.semantic_tokens
    ]
    flowables.extend([image, Spacer(1, 3 * mm)])
    return flowables


def _table_flowables(
    *,
    block: ExportIRBlock,
    styles: _PdfStyles,
) -> list[Flowable]:
    columns = block.content.get("columns")
    rows = block.content.get("rows")
    total_row = block.content.get("total_row")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise PdfExportError("pdf_table_invalid")
    if len(rows) > MAX_TABLE_ROWS:
        raise PdfExportError("pdf_table_row_limit")
    flowables: list[Flowable] = []
    for token in block.semantic_tokens[: -len(columns) if columns else None]:
        flowables.append(Paragraph(_paragraph_markup(token), styles.body))
    keys: list[str] = []
    headers: list[Flowable] = []
    for index, column in enumerate(columns):
        if not isinstance(column, dict):
            raise PdfExportError("pdf_table_invalid")
        key = column.get("key")
        label = column.get("label")
        if not isinstance(key, str):
            key = f"column_{index + 1}"
        keys.append(key)
        headers.append(
            Paragraph(
                _paragraph_markup(label if isinstance(label, str) else key),
                styles.table_header,
            )
        )
    if not headers:
        return flowables or [Paragraph("—", styles.body)]
    table_rows: list[list[Flowable]] = [headers]
    raw_rows = [row for row in rows if isinstance(row, dict)]
    if isinstance(total_row, dict):
        raw_rows.append(total_row)
    for row in raw_rows:
        table_rows.append(
            [
                Paragraph(
                    _paragraph_markup(_scalar_text(row.get(key))),
                    styles.table_cell,
                )
                for key in keys
            ]
        )
    if len(table_rows) == 1:
        table_rows.append([Paragraph("—", styles.table_cell) for _key in keys])
    table = Table(
        table_rows,
        repeatRows=1,
        hAlign="LEFT",
        colWidths=[170 * mm / len(keys)] * len(keys),
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(styles.palette.soft_header_fill)),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(styles.palette.soft_header_ink)),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor(styles.palette.rule)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    flowables.extend([table, Spacer(1, 4 * mm)])
    return flowables


def _kpi_flowables(
    *,
    block: ExportIRBlock,
    styles: _PdfStyles,
) -> list[Flowable]:
    cells = block.content.get("cells")
    if not isinstance(cells, list) or not cells:
        return [Paragraph("—", styles.body)]
    rendered: list[list[Flowable]] = []
    for cell in cells:
        tokens = _kpi_visible_tokens(cell)
        rendered.append(
            [
                Paragraph(
                    "<br/>".join(_paragraph_markup(token) for token in tokens) or "—",
                    styles.table_cell,
                )
            ]
        )
    table = Table(
        [sum(rendered, [])],
        colWidths=[170 * mm / len(rendered)] * len(rendered),
    )
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(styles.palette.rule)),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(styles.palette.card_alt)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return [table, Spacer(1, 4 * mm)]


def _fallback_flowables(
    *,
    block: ExportIRBlock,
    styles: _PdfStyles,
) -> list[Flowable]:
    tokens = list(block.semantic_tokens)
    safe_url = block.content.get("safe_url")
    if isinstance(safe_url, str) and safe_url:
        tokens.append(safe_url)
    if not tokens:
        tokens.append("请在在线报告中查看交互内容")
    return [Paragraph(_paragraph_markup(token), styles.fallback) for token in tokens]


def _extract_block_evidence(
    *,
    block: ExportIRBlock,
    page_texts: tuple[str, ...],
    visual: MaterializedVisual | None,
    image_pages: dict[str, deque[int]],
) -> tuple[ExtractedSectionEvidence | None, set[str]]:
    failures: set[str] = set()
    start = _start_marker(block)
    end = _end_marker(block)
    start_hits = _marker_hits(page_texts, start)
    end_hits = _marker_hits(page_texts, end)
    if len(start_hits) != 1 or len(end_hits) != 1:
        return None, {"pdf_section_marker_mismatch"}
    start_page, start_offset = start_hits[0]
    end_page, end_offset = end_hits[0]
    if (end_page, end_offset) <= (start_page, start_offset):
        return None, {"pdf_section_marker_order_invalid"}
    segment = _segment_text(
        page_texts=page_texts,
        start_page=start_page,
        start_offset=start_offset + len(start),
        end_page=end_page,
        end_offset=end_offset,
    )
    visible_segment = _strip_private_markers(segment)
    semantic_match = _contains_tokens_in_order(
        visible_segment,
        block.semantic_tokens,
    )
    typed_tokens = _typed_visible_tokens(block)
    typed_match = _contains_tokens_in_order(
        visible_segment,
        typed_tokens,
    )
    visual_match = False
    visual_page: int | None = None
    if block.visual_input_sha256 is not None:
        if visual is None:
            failures.add("pdf_visual_missing")
        else:
            pages = image_pages.get(visual.pixel_sha256)
            if pages:
                visual_page = pages.popleft()
                visual_match = start_page <= visual_page - 1 <= end_page
            if not visual_match:
                failures.add("pdf_visual_pixel_mismatch")
    visible = bool(_normalize_visible(visible_segment)) or visual_match
    if not visible:
        failures.add("pdf_section_not_visible")
    source_match = _source_digest(block.source_refs) in start
    if not source_match:
        failures.add("pdf_source_receipt_mismatch")

    semantic_digest = block.semantic_content_sha256 if semantic_match and visible else None
    if block.typed_data_sha256 is None:
        typed_digest = None
    elif block.visual_input_sha256 is not None:
        typed_digest = block.typed_data_sha256 if visual_match else None
    else:
        typed_digest = block.typed_data_sha256 if typed_match and visible else None
    visual_digest = block.visual_input_sha256 if visual_match else None
    artifact_hashes = visual.source_artifact_hashes if visual_match and visual is not None else ()
    target_pages = range(start_page + 1, end_page + 2)
    if block.block_kind == "interactive_fallback":
        target_kind: OutputTargetKind = "interaction_fallback"
        disposition: ManifestDisposition = "fallback"
        reason: ManifestReasonCode | None = "interactive_not_executable"
    elif block.source.surface_placement in {"data_tab", "code_tab"}:
        target_kind = "appendix"
        disposition = "rendered"
        reason = None
    else:
        target_kind = "page"
        disposition = "rendered"
        reason = None
    targets = tuple(
        OutputTargetRef(
            kind=target_kind,
            id=f"page-{page_number}",
            region=("appendix" if target_kind == "appendix" else "body"),
        )
        for page_number in target_pages
    )
    return (
        ExtractedSectionEvidence(
            block_key=block.block_key,
            disposition=disposition,
            visible=visible,
            targets=targets,
            semantic_content_sha256=semantic_digest,
            typed_data_sha256=typed_digest,
            visual_input_sha256=visual_digest,
            visual_artifact_hashes=artifact_hashes,
            source_refs=block.source_refs if source_match else (),
            reason_code=reason,
        ),
        failures,
    )


def _extract_pdf_images(
    reader: PdfReader,
) -> tuple[list[tuple[str, int]], set[str]]:
    extracted: list[tuple[str, int]] = []
    failures: set[str] = set()
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            images = list(page.images)
        except Exception:
            failures.add("pdf_image_decode_failed")
            continue
        for image_file in images:
            try:
                source = image_file.image
                if source is None:
                    failures.add("pdf_image_decode_failed")
                    continue
                with source:
                    normalized = source.convert("RGBA")
                    extracted.append(
                        (
                            _visible_pixel_sha256(normalized),
                            page_number,
                        )
                    )
            except Exception:
                failures.add("pdf_image_decode_failed")
    return extracted, failures


def _rasterize_and_check_pages(
    *,
    pdf_path: Path,
    validation_dir: Path,
    expected_pages: int,
    pdftoppm_path: Path | None,
) -> set[str]:
    failures: set[str] = set()
    binary = pdftoppm_path
    if binary is None:
        discovered = shutil.which("pdftoppm")
        binary = Path(discovered) if discovered else None
    if binary is None or not binary.is_file():
        return {"pdf_poppler_unavailable"}
    validation_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if validation_dir.is_symlink() or not validation_dir.is_dir():
        return {"pdf_validation_staging_invalid"}
    prefix = validation_dir / "page"
    try:
        completed = subprocess.run(
            [
                str(binary),
                "-png",
                "-r",
                "96",
                str(pdf_path),
                str(prefix),
            ],
            check=False,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"pdf_poppler_failed"}
    if completed.returncode != 0:
        return {"pdf_poppler_failed"}
    pages = sorted(validation_dir.glob("page-*.png"))
    if len(pages) != expected_pages:
        failures.add("pdf_poppler_page_count_mismatch")
    for page in pages:
        try:
            with Image.open(page) as image:
                rgb = image.convert("RGB")
                if _pdf_body_is_blank(rgb):
                    failures.add("pdf_blank_page")
        except OSError:
            failures.add("pdf_poppler_page_decode_failed")
    return failures


def _pdf_body_is_blank(image: Image.Image) -> bool:
    footer_height = max(1, round(image.height * (20 / 297)))
    content = image.crop(
        (0, 0, image.width, image.height - footer_height),
    )
    white = Image.new("RGB", content.size, "white")
    return ImageChops.difference(content, white).getbbox() is None


def _page_size_failures(reader: PdfReader) -> set[str]:
    failures: set[str] = set()
    for page in reader.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if abs(width - _A4_WIDTH) > 2 or abs(height - _A4_HEIGHT) > 2:
            failures.add("pdf_page_size_invalid")
    return failures


def _has_embedded_font(reader: PdfReader) -> bool:
    for page in reader.pages:
        resources = page.get("/Resources")
        resources = _resolve_pdf_object(resources)
        if not isinstance(resources, DictionaryObject):
            continue
        fonts = _resolve_pdf_object(resources.get("/Font"))
        if not isinstance(fonts, DictionaryObject):
            continue
        for font in fonts.values():
            if _font_object_embedded(_resolve_pdf_object(font)):
                return True
    return False


def _font_object_embedded(value: Any) -> bool:
    if not isinstance(value, DictionaryObject):
        return False
    descriptor = _resolve_pdf_object(value.get("/FontDescriptor"))
    if isinstance(descriptor, DictionaryObject) and any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
        return True
    descendants = _resolve_pdf_object(value.get("/DescendantFonts"))
    if isinstance(descendants, list):
        return any(_font_object_embedded(_resolve_pdf_object(item)) for item in descendants)
    return False


def _resolve_pdf_object(value: Any) -> Any:
    return value.get_object() if isinstance(value, IndirectObject) else value


def _marker_hits(
    page_texts: tuple[str, ...],
    marker: str,
) -> list[tuple[int, int]]:
    hits: list[tuple[int, int]] = []
    for page_number, text in enumerate(page_texts):
        offset = 0
        while True:
            found = text.find(marker, offset)
            if found < 0:
                break
            hits.append((page_number, found))
            offset = found + len(marker)
    return hits


def _segment_text(
    *,
    page_texts: tuple[str, ...],
    start_page: int,
    start_offset: int,
    end_page: int,
    end_offset: int,
) -> str:
    if start_page == end_page:
        return page_texts[start_page][start_offset:end_offset]
    parts = [page_texts[start_page][start_offset:]]
    parts.extend(page_texts[start_page + 1 : end_page])
    parts.append(page_texts[end_page][:end_offset])
    return "\n".join(parts)


def _strip_private_markers(value: str) -> str:
    value = _MARKER_RE.sub(" ", value)
    value = re.sub(r"BH1E_[0-9a-f]{24}", " ", value)
    return value


def _contains_tokens_in_order(
    haystack: str,
    tokens: Iterable[str],
) -> bool:
    normalized = _normalize_visible(haystack)
    offset = 0
    for token in tokens:
        needle = _normalize_visible(token)
        if not needle:
            continue
        found = normalized.find(needle, offset)
        if found < 0:
            return False
        offset = found + len(needle)
    return True


def _typed_visible_tokens(block: ExportIRBlock) -> tuple[str, ...]:
    if block.typed_data_sha256 is None:
        return ()
    if block.block_kind == "visual":
        return ()
    if block.block_kind == "table":
        columns = block.content.get("columns")
        rows = block.content.get("rows")
        total_row = block.content.get("total_row")
        if not isinstance(columns, list) or not isinstance(rows, list):
            return ()
        keys = [column.get("key") for column in columns if isinstance(column, dict) and isinstance(column.get("key"), str)]
        values: list[str] = []
        for row in rows:
            if isinstance(row, dict):
                values.extend(_scalar_text(row.get(key)) for key in keys)
        if isinstance(total_row, dict):
            values.extend(_scalar_text(total_row.get(key)) for key in keys)
        return tuple(values)
    if block.block_kind == "kpi_group":
        return block.semantic_tokens
    if block.block_kind == "interactive_fallback":
        return tuple(_flatten_visible_scalars(block.content.get("data")))
    return ()


def _kpi_visible_tokens(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return _flatten_visible_scalars(
        {
            key: value[key]
            for key in (
                "lbl",
                "label",
                "val",
                "value",
                "unit",
                "sub",
                "delta",
            )
            if key in value
        }
    )


def _flatten_visible_scalars(value: Any) -> list[str]:
    values: list[str] = []
    if value is None:
        return values
    if isinstance(value, dict):
        for key in sorted(value):
            values.extend(_flatten_visible_scalars(value[key]))
        return values
    if isinstance(value, list | tuple):
        for item in value:
            values.extend(_flatten_visible_scalars(item))
        return values
    values.append(_scalar_text(value))
    return values


def _scalar_text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    return _normalize_visible(str(value))


def _expected_visible_text(export_ir: ExportIR) -> str:
    values: list[str] = [export_ir.document.title]
    for block in iter_export_blocks(export_ir.blocks):
        values.extend(block.semantic_tokens)
        values.extend(_typed_visible_tokens(block))
    return "\n".join(values)


def _demo_residue_count(
    *,
    full_text: str,
    expected_text: str,
    fingerprints: tuple[str, ...],
) -> int:
    normalized_output = _normalize_visible(full_text)
    normalized_expected = _normalize_visible(expected_text)
    return sum(
        normalized_output.count(normalized)
        for fingerprint in fingerprints
        if (normalized := _normalize_visible(fingerprint)) and normalized not in normalized_expected
    )


def _start_marker(block: ExportIRBlock) -> str:
    return f"BH1S_{_marker_id(block.block_key)}_{_source_digest(block.source_refs)}"


def _end_marker(block: ExportIRBlock) -> str:
    return f"BH1E_{_marker_id(block.block_key)}"


def _marker_id(block_key: str) -> str:
    return hashlib.sha256(block_key.encode("utf-8")).hexdigest()[:24]


def _source_digest(source_refs: tuple[str, ...]) -> str:
    encoded = json.dumps(
        list(source_refs),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _visible_pixel_sha256(image: Image.Image) -> str:
    rgba = image.convert("RGBA")
    visible = Image.new("RGB", rgba.size, "white")
    visible.paste(rgba, mask=rgba.getchannel("A"))
    identity = visible.width.to_bytes(4, "big") + visible.height.to_bytes(4, "big") + visible.tobytes()
    return _sha256_bytes(identity)


_SYMBOL_FONT_NAME = "BowheadSymbol"
_SYMBOL_FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/Library/Fonts/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
)
_markup_coverage: tuple[frozenset[int], frozenset[int]] | None = None


def _markup_font_coverage() -> tuple[frozenset[int], frozenset[int]]:
    """(primary_codepoints, symbol_codepoints) for glyph-fallback markup, cached.

    The embeddable CJK font (wqy-zenhei) lacks Miscellaneous-Symbols glyphs such
    as ⚠ (U+26A0); reportlab renders those as .notdef, which pypdf extracts as a
    replacement/NUL char and the closed-file check rejects as pptx-style
    `pdf_missing_glyph`. DejaVu Sans (already on the image, TrueType/embeddable)
    covers them, so characters outside the primary font are wrapped in it. Both
    sets are empty when the primary font's coverage can't be read (e.g. the
    non-embedding STSong-Light fallback), which disables wrapping entirely.
    """
    global _markup_coverage
    if _markup_coverage is not None:
        return _markup_coverage
    normal_name, _ = _register_pdf_fonts()
    try:
        primary = frozenset(int(codepoint) for codepoint in pdfmetrics.getFont(normal_name).face.charToGlyph)
    except (KeyError, AttributeError):
        primary = frozenset()
    symbol: frozenset[int] = frozenset()
    if primary:
        symbol_path = next(
            (path for path in _SYMBOL_FONT_CANDIDATES if path.is_file() and _register_ttf(_SYMBOL_FONT_NAME, path)),
            None,
        )
        if symbol_path is not None:
            try:
                symbol = frozenset(int(codepoint) for codepoint in pdfmetrics.getFont(_SYMBOL_FONT_NAME).face.charToGlyph)
            except (KeyError, AttributeError):
                symbol = frozenset()
    _markup_coverage = (primary, symbol)
    return _markup_coverage


def _render_markup(value: str, *, primary: frozenset[int], symbol: frozenset[int]) -> str:
    parts: list[str] = []
    for character in value:
        if character == "\n":
            parts.append("<br/>")
            continue
        if character != "\t" and ord(character) < 32:
            continue
        escaped = html.escape(character)
        codepoint = ord(character)
        if codepoint not in primary and codepoint in symbol:
            parts.append(f'<font name="{_SYMBOL_FONT_NAME}">{escaped}</font>')
        else:
            parts.append(escaped)
    return "".join(parts)


def _paragraph_markup(value: str) -> str:
    primary, symbol = _markup_font_coverage()
    return _render_markup(value, primary=primary, symbol=symbol)


def _normalize_visible(value: str) -> str:
    # NFKC-fold, then drop ALL whitespace. pypdf's extract_text() inserts a
    # spurious space at every soft line-wrap it infers from a glyph
    # Y-coordinate jump; in a CJK-dominant report a wrap can split anything --
    # two ideographs, a number ("29.6%" -> "2 9.6%"), a citation ("[10]" ->
    # "[1 0]"), an ASCII acronym ("SOP" -> "S OP"), or any punctuation
    # boundary. None of those spaces are semantic. Whitespace only ever
    # separates English words, and dropping it there still preserves every
    # non-space character in order -- so, applied symmetrically to the
    # expected token and the observed PDF text, it can only let a true match
    # succeed, never fabricate a false one. Two texts differing solely in
    # spacing become equal, acceptable for a text-presence check; structural
    # fidelity is enforced by the visual and typed-data checks.
    return "".join(unicodedata.normalize("NFKC", value).split())


def _contains_non_ascii(value: str) -> bool:
    return any(ord(character) > 127 for character in value)


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _register_pdf_fonts() -> tuple[str, str]:
    normal_name = "BowheadCJK"
    bold_name = "BowheadCJK-Bold"
    if normal_name in pdfmetrics.getRegisteredFontNames():
        registered_bold = bold_name if bold_name in pdfmetrics.getRegisteredFontNames() else normal_name
        return normal_name, registered_bold

    # Debian's fonts-noto-cjk ships PostScript/CFF-outline OpenType fonts;
    # reportlab's TTFont embedder rejects those outright ("postscript
    # outlines are not supported"), so they never actually embed on the
    # Linux image despite the file existing. fonts-wqy-zenhei is a real
    # TrueType (glyf) CJK font and is what the Linux image can embed; it
    # must be tried first. The Noto/macOS paths stay as candidates for other
    # environments, but selection must try registering each one in order
    # rather than stopping at the first *existing* path — otherwise an
    # existing-but-unembeddable file (Noto) silently blocks every later
    # candidate and the renderer falls straight to the non-embedded
    # STSong-Light fallback.
    normal_candidates = (
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    )
    bold_candidates = (
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf"),
    )
    normal_path = next(
        (path for path in normal_candidates if path.is_file() and _register_ttf(normal_name, path)),
        None,
    )
    if normal_path is not None:
        bold_path = next(
            (path for path in bold_candidates if path.is_file() and _register_ttf(bold_name, path)),
            None,
        )
        registered_bold = bold_name if bold_path is not None else normal_name
        pdfmetrics.registerFontFamily(
            normal_name,
            normal=normal_name,
            bold=registered_bold,
            italic=normal_name,
            boldItalic=registered_bold,
        )
        return normal_name, registered_bold

    fallback = "STSong-Light"
    if fallback not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(fallback))
    pdfmetrics.registerFontFamily(
        fallback,
        normal=fallback,
        bold=fallback,
        italic=fallback,
        boldItalic=fallback,
    )
    return fallback, fallback


def _register_ttf(name: str, path: Path) -> bool:
    indexes = (2, 0, 1, 3, 4) if path.suffix.lower() == ".ttc" else (0,)
    for index in indexes:
        try:
            options = {"subfontIndex": index} if path.suffix.lower() == ".ttc" else {}
            pdfmetrics.registerFont(TTFont(name, str(path), **options))
            return True
        except Exception:
            _LOGGER.debug(
                "Unable to register PDF font %s at subfont index %d",
                path,
                index,
                exc_info=True,
            )
            continue
    return False


def _build_styles(
    *,
    normal_font: str,
    bold_font: str,
    palette: NativeExportPalette,
) -> _PdfStyles:
    common = {
        "fontName": normal_font,
        "textColor": colors.HexColor(palette.ink),
        "alignment": TA_LEFT,
    }
    return _PdfStyles(
        body=ParagraphStyle(
            "ExportBody",
            fontSize=10,
            leading=15,
            spaceAfter=6,
            **common,
        ),
        heading_1=ParagraphStyle(
            "ExportH1",
            fontName=bold_font,
            fontSize=21,
            leading=27,
            textColor=colors.HexColor(palette.heading),
            spaceBefore=4,
            spaceAfter=9,
        ),
        heading_2=ParagraphStyle(
            "ExportH2",
            fontName=bold_font,
            fontSize=14,
            leading=19,
            textColor=colors.HexColor(palette.heading),
            spaceBefore=8,
            spaceAfter=6,
        ),
        eyebrow=ParagraphStyle(
            "ExportEyebrow",
            fontName=bold_font,
            fontSize=8,
            leading=11,
            textColor=colors.HexColor(palette.muted),
            spaceAfter=4,
        ),
        code=ParagraphStyle(
            "ExportCode",
            fontName=normal_font,
            fontSize=8.5,
            leading=12,
            textColor=colors.HexColor(palette.code_ink),
            backColor=colors.HexColor(palette.card_alt),
            borderPadding=6,
            spaceAfter=5,
        ),
        callout=ParagraphStyle(
            "ExportCallout",
            fontName=normal_font,
            fontSize=10,
            leading=15,
            textColor=colors.HexColor(palette.callout_ink),
            backColor=colors.HexColor(palette.callout_fill),
            borderColor=colors.HexColor(palette.callout_border),
            borderWidth=0.5,
            borderPadding=7,
            spaceBefore=3,
            spaceAfter=7,
        ),
        fallback=ParagraphStyle(
            "ExportFallback",
            fontName=normal_font,
            fontSize=9,
            leading=13,
            textColor=colors.HexColor(palette.notice_ink),
            backColor=colors.HexColor(palette.notice_fill),
            borderColor=colors.HexColor(palette.notice_border),
            borderWidth=0.5,
            borderPadding=7,
            spaceBefore=3,
            spaceAfter=7,
        ),
        table_header=ParagraphStyle(
            "ExportTableHeader",
            fontName=bold_font,
            fontSize=8,
            leading=10,
            textColor=colors.HexColor(palette.soft_header_ink),
        ),
        table_cell=ParagraphStyle(
            "ExportTableCell",
            fontName=normal_font,
            fontSize=8,
            leading=10,
            textColor=colors.HexColor(palette.ink),
        ),
        title=ParagraphStyle(
            "ExportTitle",
            fontName=bold_font,
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            textColor=colors.HexColor(palette.muted),
            spaceAfter=3,
        ),
        palette=palette,
    )


def _draw_page_chrome(canvas: Canvas, document: Any, *, palette: NativeExportPalette) -> None:
    # Only a decorative footer rule — deliberately no drawn page number.
    #
    # SimpleDocTemplate fires this onPage callback *before* the page's body
    # flowables are written to the content stream (reportlab
    # BaseDocTemplate.handle_pageBegin -> onPage precedes handle_frameBegin).
    # pypdf's extract_text() reads text in content-stream order, so any drawn
    # page number ends up at the very start of that page's extracted text.
    # When a narrative block spans a page break, _segment_text() slurps the
    # whole page text between markers and the leaked page-number digit lands
    # inside the reconstructed segment, breaking the exact semantic-token
    # match (real 116 export: a page-4 "4" injected mid-narrative ->
    # section_semantic_mismatch). Moving the draw to onPageEnd does not help:
    # the digit merely relocates to the page seam, still inside the segment.
    # The only robust fix is to keep no extractable chrome text at all. The
    # rule below is a graphics operator, not text, so it never leaks. Viewers
    # still show page position from their own navigation UI.
    canvas.saveState()
    # Paint the paper so a tinted print surface reaches the page, not only the boxes on it.
    canvas.setFillColor(colors.HexColor(palette.page))
    canvas.rect(0, 0, _A4_WIDTH, _A4_HEIGHT, stroke=0, fill=1)
    canvas.setStrokeColor(colors.HexColor(palette.rule))
    canvas.setLineWidth(0.4)
    canvas.line(18 * mm, 14 * mm, _A4_WIDTH - 18 * mm, 14 * mm)
    canvas.restoreState()


__all__ = [
    "PDF_RENDERER_NAME",
    "PDF_RENDERER_VERSION",
    "PdfExportError",
    "PdfRenderResult",
    "extract_pdf_evidence",
    "render_export_pdf",
]
