"""Canvas XLSX data-workbook renderer and closed-file evidence extractor.

The workbook is created from a blank shell.  Every ExportIR block is listed in
the public Manifest sheet, typed values are written as real spreadsheet cell
types, and every materialized visual is embedded in Visual Index.  Cell
comments are private locators only: the extractor reopens the completed file
and proves parity from visible cell values and decoded image pixels.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import quote
from xml.etree import ElementTree as ET

from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]
from openpyxl.cell.cell import Cell  # type: ignore[import-untyped]
from openpyxl.comments import Comment  # type: ignore[import-untyped]
from openpyxl.drawing.image import Image as OpenpyxlImage  # type: ignore[import-untyped]
from openpyxl.styles import (  # type: ignore[import-untyped]
    Alignment,
    Border,
    Font,
    PatternFill,
    Side,
)
from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]
from openpyxl.worksheet.worksheet import Worksheet  # type: ignore[import-untyped]
from PIL import Image, UnidentifiedImageError

from bowhead_core.chart.numeric_display import currency_identity, numeric_display
from bowhead_core.error_catalog import CodedError
from bowhead_core.export_ir import (
    ExportIR,
    ExportIRBlock,
    build_capability_plan,
    iter_export_blocks,
)
from bowhead_core.export_manifest import (
    ExtractedOutputEvidence,
    ExtractedSectionEvidence,
    ManifestReasonCode,
    OutputTargetRef,
)
from bowhead_core.export_materializer import MaterializedVisual
from bowhead_core.export_visual_identity import visual_typed_data

XLSX_RENDERER_NAME = "canvas-data-workbook-v2"
XLSX_RENDERER_VERSION = "3"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_XLSX_BYTES = 150 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_MEMBER_BYTES = 100 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = 350 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100
MAX_XML_MEMBER_BYTES = 20 * 1024 * 1024
MAX_TYPED_CELLS = 500_000
MAX_WORKBOOK_SHEETS = 1_000
MAX_CELL_TEXT_CHARS = 30_000

_LOCATOR_PREFIX = "BH1:"
_TEXT_CHUNK_PREFIX = "BH-TEXT1:"
_FORBIDDEN_MEMBER_PARTS = (
    "/externallinks/",
    "/connections",
    "/activex/",
    "/oleobjects/",
    "/embeddings/",
)
_FORBIDDEN_MEMBER_SUFFIXES = (
    "vbaproject.bin",
    ".xls",
    ".xlsm",
    ".xlam",
    ".exe",
    ".dll",
)
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_INVALID_SHEET_CHARS_RE = re.compile(r"[\[\]:*?/\\]")
_DEMO_FINGERPRINTS = (
    "CMP-2604-001",
    "Microsoft_Excel_Worksheet2.xlsx",
    "整体合规率",
)

_FOREST = "173D32"
_FOREST_LIGHT = "26594B"
_CREAM = "F6F1E7"
_PAPER = "FFFCF6"
_INK = "192824"
_MUTED = "65726D"
_TERRACOTTA = "C9674B"
_SAGE = "D9E6DE"
_WHITE = "FFFFFF"
_BORDER = Side(style="thin", color="D8D3C8")

XlsxDisposition = Literal["rendered", "fallback"]


class XlsxExportError(CodedError):
    """The workbook cannot be rendered or validated without losing parity."""


@dataclass(frozen=True, slots=True)
class XlsxRenderResult:
    relative_path: str
    content_sha256: str
    size_bytes: int
    sheet_count: int
    data_sheet_count: int
    renderer_name: str = XLSX_RENDERER_NAME
    renderer_version: str = XLSX_RENDERER_VERSION


@dataclass(frozen=True, slots=True)
class _TypedEntry:
    path: str
    value_type: str
    value: Any


@dataclass(frozen=True, slots=True)
class _Locator:
    marker: str
    source_digest: str
    part: int
    role: str
    disposition: XlsxDisposition
    reason_code: ManifestReasonCode | None
    min_row: int
    max_row: int
    min_col: int
    max_col: int


@dataclass(slots=True)
class _ObservedPart:
    locator: _Locator
    sheet_name: str
    values: list[list[Any]]


def render_export_xlsx(
    *,
    export_ir: ExportIR,
    materialized_visuals: tuple[MaterializedVisual, ...],
    attempt_dir: Path,
    output_name: str = "output.xlsx",
) -> XlsxRenderResult:
    """Render the data-workbook profile from frozen IR and pinned visuals."""
    output_path = _prepare_output_path(
        attempt_dir=attempt_dir,
        output_name=output_name,
    )
    blocks = iter_export_blocks(export_ir.blocks)
    plan = build_capability_plan(
        export_ir,
        target="xlsx_data",
        include_appendix=True,
    )
    plan_by_key = {entry.block_key: entry for entry in plan}
    if len(plan_by_key) != len(plan) or len(plan_by_key) != len(blocks):
        raise XlsxExportError("duplicate_ir_block_key")
    if any(entry.action in {"appendix", "exclude", "summary_candidate"} for entry in plan):
        raise XlsxExportError("xlsx_capability_plan_invalid")

    visuals = _index_visuals(
        export_ir=export_ir,
        materialized_visuals=materialized_visuals,
        attempt_dir=attempt_dir,
    )
    typed_by_key = {block.block_key: _typed_entries(block) for block in blocks if block.typed_data_sha256 is not None}
    typed_count = sum(len(entries) for entries in typed_by_key.values())
    if typed_count > MAX_TYPED_CELLS:
        raise XlsxExportError(
            "xlsx_typed_cell_limit",
            observed=typed_count,
            maximum=MAX_TYPED_CELLS,
        )

    workbook = Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)
    workbook.properties.creator = "Bowhead"
    workbook.properties.title = export_ir.document.title
    workbook.properties.subject = "Canvas 数据工作簿"
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    workbook.calculation.calcMode = "auto"

    summary = workbook.create_sheet("Summary")
    typed_sheets: dict[str, Worksheet] = {}
    for index, block in enumerate(
        (candidate for candidate in blocks if candidate.block_key in typed_by_key),
        start=1,
    ):
        typed_sheets[block.block_key] = workbook.create_sheet(
            _unique_sheet_title(
                base=f"Data - {index}",
                existing=set(workbook.sheetnames),
            )
        )
    insights = workbook.create_sheet("Insights")
    recommendations = workbook.create_sheet("Recommendations")
    visual_index = workbook.create_sheet("Visual Index")
    sql_code = workbook.create_sheet("SQL & Code")
    sources = workbook.create_sheet("Sources")
    manifest = workbook.create_sheet("Manifest")
    for worksheet in workbook.worksheets:
        _setup_sheet(worksheet)

    family_sheets: dict[str, Worksheet] = {
        "summary": summary,
        "insights": insights,
        "recommendations": recommendations,
        "visual": visual_index,
        "code": sql_code,
        "sources": sources,
    }
    next_rows = dict.fromkeys(family_sheets, 4)
    _write_workbook_header(
        worksheet=summary,
        title=export_ir.document.title,
        subtitle="Canvas data workbook · typed data, insights, visuals, code, and sources",
    )
    for name, worksheet in family_sheets.items():
        if name == "summary":
            continue
        _write_workbook_header(
            worksheet=worksheet,
            title=_family_title(name),
            subtitle=export_ir.document.title,
        )
    next_rows["summary"] = 7

    targets_by_key: dict[str, list[str]] = defaultdict(list)
    for block in blocks:
        disposition: XlsxDisposition = "fallback" if plan_by_key[block.block_key].action == "fallback" else "rendered"
        reason_code: ManifestReasonCode | None = "interactive_not_executable" if disposition == "fallback" else None
        family = _family_for_block(block)
        worksheet = family_sheets[family]
        start_row = next_rows[family]
        end_row = _write_semantic_card(
            worksheet=worksheet,
            start_row=start_row,
            block=block,
            disposition=disposition,
            reason_code=reason_code,
            part=1,
        )
        next_rows[family] = end_row + 2
        targets_by_key[block.block_key].append(worksheet.title)

        entries = typed_by_key.get(block.block_key)
        if entries is not None:
            data_sheet = typed_sheets[block.block_key]
            _write_workbook_header(
                worksheet=data_sheet,
                title=_block_display_title(block),
                subtitle=f"{block.source.section_kind} · {block.source.section_id} · rev {block.source.section_rev}",
            )
            _write_typed_rows(
                worksheet=data_sheet,
                start_row=5,
                block=block,
                entries=entries,
                disposition=disposition,
                reason_code=reason_code,
                part=2,
            )
            targets_by_key[block.block_key].append(data_sheet.title)

        visual = visuals.get(block.block_key)
        if visual is not None:
            anchor_row = _write_visual_entry(
                worksheet=visual_index,
                start_row=next_rows["visual"],
                block=block,
                visual=visual,
                attempt_dir=attempt_dir,
                disposition=disposition,
                reason_code=reason_code,
                part=3,
            )
            next_rows["visual"] = anchor_row + 19
            if visual_index.title not in targets_by_key[block.block_key]:
                targets_by_key[block.block_key].append(visual_index.title)

    _write_manifest(
        worksheet=manifest,
        export_ir=export_ir,
        blocks=blocks,
        plan_by_key=plan_by_key,
        targets_by_key=targets_by_key,
    )
    _finalize_workbook(workbook)
    workbook.save(output_path)
    workbook.close()

    content = output_path.read_bytes()
    if len(content) <= 4 or len(content) > MAX_XLSX_BYTES:
        raise XlsxExportError("xlsx_size_invalid")
    reopened = load_workbook(output_path, read_only=True)
    sheet_count = len(reopened.sheetnames)
    reopened.close()
    return XlsxRenderResult(
        relative_path=str(output_path.relative_to(attempt_dir)),
        content_sha256=_sha256_bytes(content),
        size_bytes=len(content),
        sheet_count=sheet_count,
        data_sheet_count=len(typed_by_key),
    )


def extract_xlsx_evidence(
    *,
    xlsx_path: Path,
    export_ir: ExportIR,
    materialized_visuals: tuple[MaterializedVisual, ...],
    validation_dir: Path,
    soffice_path: Path,
) -> ExtractedOutputEvidence:
    """Reopen a closed workbook and reconstruct section evidence."""
    size_bytes = xlsx_path.stat().st_size if xlsx_path.is_file() else 0
    magic_valid = size_bytes >= 4 and xlsx_path.read_bytes()[:4] == b"PK\x03\x04"
    hard_failures = _inspect_package(xlsx_path)
    parser_opened = False
    observed_parts: dict[str, list[_ObservedPart]] = defaultdict(list)
    observed_images: dict[str, list[tuple[str, str]]] = defaultdict(list)
    unexpected_count = 0
    demo_residue_count = 0
    expected_fingerprints = {
        fingerprint
        for fingerprint in _DEMO_FINGERPRINTS
        if fingerprint
        in json.dumps(
            export_ir.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )
    }
    blocks = iter_export_blocks(export_ir.blocks)
    blocks_by_marker = {_marker_id(block.block_key): block for block in blocks}
    if len(blocks_by_marker) != len(blocks):
        hard_failures.add("xlsx_marker_collision")
    expected_sheet_names: set[str] = {
        "Summary",
        "Insights",
        "Recommendations",
        "Visual Index",
        "SQL & Code",
        "Sources",
        "Manifest",
    }
    expected_sheet_names.update(
        f"Data - {index}"
        for index, block in enumerate(
            (candidate for candidate in blocks if candidate.typed_data_sha256 is not None),
            start=1,
        )
    )

    try:
        workbook = load_workbook(
            xlsx_path,
            data_only=False,
            keep_links=True,
        )
        parser_opened = True
        _inspect_workbook_structure(
            workbook=workbook,
            expected_sheet_names=expected_sheet_names,
            hard_failures=hard_failures,
        )
        _inspect_manifest_sheet(
            workbook=workbook,
            export_ir=export_ir,
            hard_failures=hard_failures,
        )
        for worksheet in workbook.worksheets:
            demo_residue_count += _demo_residue_count(
                worksheet,
                expected_fingerprints=expected_fingerprints,
            )
            image_by_anchor = _images_by_anchor(worksheet)
            for row in worksheet.iter_rows():
                for cell in row:
                    if cell.data_type == "f":
                        hard_failures.add("xlsx_formula_forbidden")
                    if cell.data_type == "e":
                        hard_failures.add("xlsx_formula_error")
                    if cell.comment is None:
                        continue
                    locator = _parse_locator(cell.comment.text)
                    if locator is None:
                        if cell.comment.text.startswith(_LOCATOR_PREFIX):
                            hard_failures.add("xlsx_locator_invalid")
                        continue
                    block = blocks_by_marker.get(locator.marker)
                    if block is None:
                        unexpected_count += 1
                        continue
                    if locator.source_digest != _source_digest(block.source_refs) or locator.min_row != cell.row or locator.min_col != cell.column:
                        hard_failures.add("xlsx_locator_source_mismatch")
                        continue
                    if not _locator_range_is_visible(
                        worksheet=worksheet,
                        locator=locator,
                    ):
                        hard_failures.add(
                            "xlsx_locator_range_hidden",
                        )
                        continue
                    values = _visible_range_values(
                        worksheet=worksheet,
                        locator=locator,
                    )
                    observed_parts[block.block_key].append(
                        _ObservedPart(
                            locator=locator,
                            sheet_name=worksheet.title,
                            values=values,
                        )
                    )
                    if locator.role != "visual":
                        continue
                    images = image_by_anchor.get((cell.row, cell.column), [])
                    if len(images) != 1:
                        hard_failures.add("xlsx_visual_image_missing")
                        continue
                    try:
                        pixel_sha256 = _visible_pixel_sha256(images[0])
                    except (OSError, UnidentifiedImageError, ValueError):
                        hard_failures.add("xlsx_visual_image_decode_failed")
                        continue
                    observed_images[block.block_key].append((worksheet.title, pixel_sha256))
        workbook.close()
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        hard_failures.add("xlsx_parser_failed")

    recalculated_path = _libreoffice_recalculate(
        xlsx_path=xlsx_path,
        validation_dir=validation_dir,
        soffice_path=soffice_path,
        hard_failures=hard_failures,
    )
    if recalculated_path is not None:
        _inspect_recalculated_workbook(
            recalculated_path=recalculated_path,
            expected_sheet_count=len(expected_sheet_names),
            hard_failures=hard_failures,
        )

    visuals = {visual.block_key: visual for visual in materialized_visuals}
    if len(visuals) != len(materialized_visuals):
        hard_failures.add("xlsx_duplicate_materialized_visual")
    _inspect_locator_accounting(
        blocks=blocks,
        observed_parts=observed_parts,
        hard_failures=hard_failures,
    )
    sections = tuple(
        _evidence_for_block(
            block=block,
            parts=observed_parts.get(block.block_key, []),
            images=observed_images.get(block.block_key, []),
            expected_visual=visuals.get(block.block_key),
        )
        for block in blocks
    )
    return ExtractedOutputEvidence(
        mime_magic_valid=magic_valid,
        parser_opened=parser_opened,
        size_bytes=size_bytes,
        sections=sections,
        unexpected_business_section_count=unexpected_count,
        demo_residue_count=demo_residue_count,
        hard_check_failures=tuple(sorted(hard_failures)),
    )


def _write_semantic_card(
    *,
    worksheet: Worksheet,
    start_row: int,
    block: ExportIRBlock,
    disposition: XlsxDisposition,
    reason_code: ManifestReasonCode | None,
    part: int,
) -> int:
    _set_text(
        worksheet.cell(start_row, 1),
        _block_display_title(block),
    )
    worksheet.cell(start_row, 2, block.source.section_kind)
    worksheet.cell(start_row, 3, block.source.section_id)
    worksheet.cell(start_row, 4, block.source.section_rev)
    for column in range(1, 5):
        cell = worksheet.cell(start_row, column)
        cell.fill = PatternFill("solid", fgColor=_FOREST)
        cell.font = Font(color=_WHITE, bold=True)
    worksheet.cell(start_row + 1, 1, "Source references")
    _set_text(
        worksheet.cell(start_row + 1, 2),
        _source_refs_text(block.source_refs),
    )
    worksheet.cell(start_row + 2, 1, "Content")
    token_row = start_row + 2
    for token in block.semantic_tokens:
        token_row += 1
        token_row = _write_text_chunks(
            worksheet=worksheet,
            start_row=token_row,
            column=2,
            value=token,
        )
    if not block.semantic_tokens:
        token_row += 1
        _set_text(worksheet.cell(token_row, 2), "Visual or structured data entry")
    if disposition == "fallback":
        token_row += 1
        _set_text(
            worksheet.cell(token_row, 2),
            "Interactive content is represented as a safe, non-executable workbook entry.",
        )
    for row in worksheet.iter_rows(
        min_row=start_row + 1,
        max_row=token_row,
        min_col=1,
        max_col=4,
    ):
        for cell in row:
            cell.fill = PatternFill("solid", fgColor=_PAPER)
            cell.border = Border(bottom=_BORDER)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    _attach_locator(
        cell=worksheet.cell(start_row, 1),
        block=block,
        part=part,
        role="semantic",
        disposition=disposition,
        reason_code=reason_code,
        min_row=start_row,
        max_row=token_row,
        min_col=1,
        max_col=4,
    )
    return token_row


def _write_typed_rows(
    *,
    worksheet: Worksheet,
    start_row: int,
    block: ExportIRBlock,
    entries: tuple[_TypedEntry, ...],
    disposition: XlsxDisposition,
    reason_code: ManifestReasonCode | None,
    part: int,
) -> int:
    row_number = start_row
    tabular = _tabular_view(block)
    if tabular is not None:
        headers, rows = tabular
        worksheet.cell(row_number, 1, "Reusable data")
        worksheet.cell(row_number, 1).font = Font(
            color=_FOREST,
            bold=True,
            size=12,
        )
        row_number += 1
        for column, header in enumerate(headers, start=1):
            cell = worksheet.cell(row_number, column)
            _set_text(cell, header)
            cell.fill = PatternFill("solid", fgColor=_TERRACOTTA)
            cell.font = Font(color=_WHITE, bold=True)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        hints = _date_type_hints(block)
        formats = _numeric_column_hints(block)
        for raw_row in rows:
            row_number += 1
            for column, header in enumerate(headers, start=1):
                value_type, value = _normalize_typed_scalar(
                    value=raw_row.get(header),
                    hint=hints.get(header),
                )
                if isinstance(value, str) and len(value) > MAX_CELL_TEXT_CHARS:
                    raise XlsxExportError(
                        "xlsx_tabular_cell_too_long",
                    )
                _set_typed_value(
                    worksheet.cell(row_number, column),
                    entry=_TypedEntry(
                        path=f"$.{_escape_path(header)}",
                        value_type=value_type,
                        value=value,
                    ),
                )
                cell = worksheet.cell(row_number, column)
                if value_type in {"number", "integer"} and header in formats:
                    cell.number_format = _numeric_cell_format(formats[header], value)
                cell.fill = PatternFill(
                    "solid",
                    fgColor=_WHITE if row_number % 2 else _CREAM,
                )
                cell.border = Border(bottom=_BORDER)
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=True,
                )
        worksheet.auto_filter.ref = f"A{start_row + 1}:{get_column_letter(len(headers))}{row_number}"
        row_number += 2
    worksheet.cell(row_number, 1, "Typed parity ledger")
    worksheet.cell(row_number, 1).font = Font(
        color=_FOREST,
        bold=True,
        size=12,
    )
    row_number += 1
    ledger_start = row_number
    headers = ("Path", "Type", "Value")
    for column, header in enumerate(headers, start=1):
        cell = worksheet.cell(ledger_start, column, header)
        cell.fill = PatternFill("solid", fgColor=_FOREST_LIGHT)
        cell.font = Font(color=_WHITE, bold=True)
        cell.alignment = Alignment(vertical="center")
    for entry in entries:
        entry_start = row_number + 1
        row_number = _write_typed_entry(
            worksheet=worksheet,
            start_row=entry_start,
            entry=entry,
        )
        for styled_row in range(entry_start, row_number + 1):
            for column in range(1, 4):
                cell = worksheet.cell(styled_row, column)
                cell.fill = PatternFill(
                    "solid",
                    fgColor=(_WHITE if styled_row % 2 else _CREAM),
                )
                cell.border = Border(bottom=_BORDER)
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=True,
                )
    if not entries:
        row_number += 1
        _set_text(worksheet.cell(row_number, 1), "$")
        _set_text(worksheet.cell(row_number, 2), "empty")
        _set_text(worksheet.cell(row_number, 3), "—")
    _attach_locator(
        cell=worksheet.cell(ledger_start, 1),
        block=block,
        part=part,
        role="typed",
        disposition=disposition,
        reason_code=reason_code,
        min_row=ledger_start,
        max_row=row_number,
        min_col=1,
        max_col=3,
    )
    worksheet.freeze_panes = f"A{start_row + 2}"
    worksheet.column_dimensions["A"].width = 46
    worksheet.column_dimensions["B"].width = 14
    worksheet.column_dimensions["C"].width = 32
    return row_number


def _tabular_view(
    block: ExportIRBlock,
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]] | None:
    raw_rows: Any = None
    preferred_columns: list[str] = []
    if block.block_kind == "table":
        raw_rows = block.content.get("rows")
        columns = block.content.get("columns")
        if isinstance(columns, list):
            preferred_columns = [str(column["key"]) for column in columns if isinstance(column, dict) and isinstance(column.get("key"), str)]
    elif block.block_kind == "kpi_group":
        raw_rows = block.content.get("cells")
    elif block.block_kind == "visual":
        resolved_chart_rows = block.content.get(
            "resolved_chart_rows",
        )
        if isinstance(resolved_chart_rows, list):
            raw_rows = resolved_chart_rows
        payload = block.content.get("payload")
        if not isinstance(payload, dict):
            return None
        if raw_rows is not None:
            pass
        elif block.source.section_kind == "chart":
            spec = payload.get("spec")
            if isinstance(spec, dict):
                raw_rows = spec.get("rows") if isinstance(spec.get("rows"), list) else spec.get("data")
                columns = spec.get("columns")
                if isinstance(columns, list):
                    preferred_columns = [str(column["key"]) for column in columns if isinstance(column, dict) and isinstance(column.get("key"), str)]
        elif block.source.section_kind == "spec_block":
            raw_rows = payload.get("nodes")
    elif block.block_kind == "interactive_fallback":
        data = block.content.get("data")
        raw_rows = data if isinstance(data, list) else [data]
    if not isinstance(raw_rows, list):
        return None
    rows = tuple(dict(row) for row in raw_rows if isinstance(row, dict))
    if not rows:
        return None
    if any(isinstance(value, str) and len(value) > MAX_CELL_TEXT_CHARS for row in rows for value in row.values()):
        return None
    discovered = sorted({str(key) for row in rows for key in row})
    headers = tuple(
        dict.fromkeys(
            [
                *preferred_columns,
                *discovered,
            ]
        )
    )
    if not headers or len(headers) > 50:
        return None
    return headers, rows


def _write_visual_entry(
    *,
    worksheet: Worksheet,
    start_row: int,
    block: ExportIRBlock,
    visual: MaterializedVisual,
    attempt_dir: Path,
    disposition: XlsxDisposition,
    reason_code: ManifestReasonCode | None,
    part: int,
) -> int:
    image_path = attempt_dir / visual.relative_path
    if image_path.is_symlink() or not image_path.is_file() or _sha256_bytes(image_path.read_bytes()) != visual.content_sha256:
        raise XlsxExportError("xlsx_visual_staging_invalid")
    _set_text(
        worksheet.cell(start_row, 1),
        _block_display_title(block),
    )
    worksheet.cell(start_row, 2, block.source.section_kind)
    worksheet.cell(start_row + 1, 1, "Section")
    _set_text(
        worksheet.cell(start_row + 1, 2),
        f"{block.source.section_id} · rev {block.source.section_rev}",
    )
    worksheet.cell(start_row + 2, 1, "Source artifacts")
    _set_text(
        worksheet.cell(start_row + 2, 2),
        _artifact_ids_text(block),
    )
    worksheet.cell(start_row + 3, 1, "Source references")
    _set_text(
        worksheet.cell(start_row + 3, 2),
        _source_refs_text(block.source_refs),
    )
    for row in range(start_row, start_row + 4):
        for column in range(1, 4):
            cell = worksheet.cell(row, column)
            cell.fill = PatternFill(
                "solid",
                fgColor=_FOREST if row == start_row else _PAPER,
            )
            cell.font = Font(
                color=_WHITE if row == start_row else _INK,
                bold=row == start_row,
            )
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    image = OpenpyxlImage(str(image_path))
    scale = min(480 / image.width, 260 / image.height, 1.0)
    image.width = int(image.width * scale)
    image.height = int(image.height * scale)
    anchor = worksheet.cell(start_row, 5)
    worksheet.add_image(image, anchor.coordinate)
    _attach_locator(
        cell=anchor,
        block=block,
        part=part,
        role="visual",
        disposition=disposition,
        reason_code=reason_code,
        min_row=start_row,
        max_row=start_row + 15,
        min_col=5,
        max_col=10,
    )
    for row in range(start_row, start_row + 16):
        worksheet.row_dimensions[row].height = 18
    worksheet.column_dimensions["E"].width = 18
    return start_row


def _write_manifest(
    *,
    worksheet: Worksheet,
    export_ir: ExportIR,
    blocks: tuple[ExportIRBlock, ...],
    plan_by_key: dict[str, Any],
    targets_by_key: dict[str, list[str]],
) -> None:
    _write_workbook_header(
        worksheet=worksheet,
        title="Manifest",
        subtitle=(f"snapshot {export_ir.snapshot_id} · surface {export_ir.surface_token}"),
    )
    headers = (
        "Block key",
        "Section ID",
        "Revision",
        "Kind",
        "Path",
        "Placement",
        "Disposition",
        "Target sheets",
        "Source references",
    )
    header_row = 5
    for column, header in enumerate(headers, start=1):
        cell = worksheet.cell(header_row, column, header)
        cell.fill = PatternFill("solid", fgColor=_FOREST_LIGHT)
        cell.font = Font(color=_WHITE, bold=True)
    for index, block in enumerate(blocks, start=1):
        row = header_row + index
        action = plan_by_key[block.block_key].action
        values: tuple[Any, ...] = (
            block.block_key,
            block.source.section_id,
            block.source.section_rev,
            block.source.section_kind,
            ".".join(str(item) for item in block.source.section_path),
            block.source.surface_placement,
            "fallback" if action == "fallback" else "rendered",
            ", ".join(targets_by_key[block.block_key]),
            _source_refs_text(block.source_refs),
        )
        for column, value in enumerate(values, start=1):
            cell = worksheet.cell(row, column)
            if isinstance(value, str):
                _set_text(cell, value)
            else:
                cell.value = value
            cell.fill = PatternFill(
                "solid",
                fgColor=_WHITE if index % 2 else _CREAM,
            )
            cell.border = Border(bottom=_BORDER)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    worksheet.freeze_panes = "A6"
    worksheet.auto_filter.ref = f"A{header_row}:I{header_row + len(blocks)}"
    for column_letter, width in {
        "A": 30,
        "B": 22,
        "C": 10,
        "D": 22,
        "E": 12,
        "F": 18,
        "G": 14,
        "H": 35,
        "I": 35,
    }.items():
        worksheet.column_dimensions[column_letter].width = width


def _evidence_for_block(
    *,
    block: ExportIRBlock,
    parts: list[_ObservedPart],
    images: list[tuple[str, str]],
    expected_visual: MaterializedVisual | None,
) -> ExtractedSectionEvidence:
    semantic_parts = sorted(
        (part for part in parts if part.locator.role == "semantic"),
        key=lambda part: part.locator.part,
    )
    typed_parts = [part for part in parts if part.locator.role == "typed"]
    source_refs = _sources_from_semantic_part(semantic_parts[0]) if semantic_parts else ()
    semantic_values = [_cell_visible_text(value) for part in semantic_parts for row in part.values for value in row if _cell_visible_text(value)]
    semantic_match = _tokens_in_order(
        expected=block.semantic_tokens,
        observed=semantic_values,
    )
    typed_match = False
    if block.typed_data_sha256 is None:
        typed_match = True
    elif len(typed_parts) == 1:
        typed_match = _typed_entries_match(
            expected=_typed_entries(block),
            observed=_typed_entries_from_part(typed_parts[0]),
        )

    visual_match = False
    visual_targets: list[OutputTargetRef] = []
    if block.visual_input_sha256 is None:
        visual_match = True
    elif expected_visual is not None and len(images) == 1 and images[0][1] == expected_visual.pixel_sha256:
        visual_match = True
        visual_targets.append(
            OutputTargetRef(
                kind="sheet",
                id=images[0][0],
                region="thumbnail",
            )
        )

    disposition: XlsxDisposition = semantic_parts[0].locator.disposition if semantic_parts else "rendered"
    reason_code = semantic_parts[0].locator.reason_code if semantic_parts else None
    targets = [
        OutputTargetRef(
            kind=("interaction_fallback" if disposition == "fallback" else "sheet"),
            id=part.sheet_name,
            region=part.locator.role,
        )
        for part in parts
        if part.locator.role != "visual"
    ]
    targets.extend(visual_targets)
    unique_targets = tuple(dict.fromkeys(targets))
    visible = (
        bool(semantic_parts)
        and (block.typed_data_sha256 is None or bool(typed_parts))
        and (block.visual_input_sha256 is None or bool(visual_targets))
    )
    artifact_hashes = tuple(sorted(_visual_artifact_hashes(block).items())) if visual_match and block.visual_input_sha256 is not None else ()
    return ExtractedSectionEvidence(
        block_key=block.block_key,
        disposition=disposition,
        visible=visible,
        targets=unique_targets,
        semantic_content_sha256=(block.semantic_content_sha256 if semantic_match and visible else None),
        typed_data_sha256=(block.typed_data_sha256 if typed_match and visible else None),
        visual_input_sha256=(block.visual_input_sha256 if visual_match and visible else None),
        visual_artifact_hashes=artifact_hashes,
        source_refs=source_refs,
        reason_code=reason_code,
    )


def _typed_entries(block: ExportIRBlock) -> tuple[_TypedEntry, ...]:
    typed_data = _typed_data_for_block(block)
    if typed_data is None:
        return ()
    hints = _date_type_hints(block)
    entries: list[_TypedEntry] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if not value:
                entries.append(_TypedEntry(path, "object", "{}"))
                return
            for key in sorted(value):
                visit(value[key], f"{path}.{_escape_path(str(key))}")
            return
        if isinstance(value, list):
            if not value:
                entries.append(_TypedEntry(path, "array", "[]"))
                return
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
            return
        value_type, normalized = _normalize_typed_scalar(
            value=value,
            hint=hints.get(_path_leaf(path)),
        )
        entries.append(
            _TypedEntry(
                path=path,
                value_type=value_type,
                value=normalized,
            )
        )

    visit(typed_data, "$")
    return tuple(entries)


def _typed_data_for_block(block: ExportIRBlock) -> Any:
    if block.typed_data_sha256 is None:
        return None
    if block.block_kind == "table":
        return {
            "columns": block.content.get("columns"),
            "rows": block.content.get("rows"),
            "total_row": block.content.get("total_row"),
        }
    if block.block_kind == "kpi_group":
        cells = block.content.get("cells")
        if not isinstance(cells, list):
            return []
        visible_keys = (
            "lbl",
            "label",
            "val",
            "value",
            "unit",
            "sub",
            "delta",
        )
        return [{key: cell[key] for key in visible_keys if key in cell} for cell in cells if isinstance(cell, dict)]
    if block.block_kind == "visual":
        resolved_chart_rows = block.content.get(
            "resolved_chart_rows",
        )
        if isinstance(resolved_chart_rows, list):
            return resolved_chart_rows
        payload = block.content.get("payload")
        return visual_typed_data(block.source.section_kind, payload) if isinstance(payload, dict) else None
    if block.block_kind == "interactive_fallback":
        return block.content.get("data")
    raise XlsxExportError(
        "xlsx_typed_adapter_missing",
        section_kind=block.source.section_kind,
    )


def _date_type_hints(block: ExportIRBlock) -> dict[str, str]:
    hints: dict[str, str] = {}
    if block.block_kind == "table":
        raw_columns = block.content.get("columns")
    elif block.block_kind == "visual":
        payload = block.content.get("payload")
        spec = payload.get("spec") if isinstance(payload, dict) else None
        raw_columns = spec.get("columns") if isinstance(spec, dict) else None
    else:
        raw_columns = None
    if not isinstance(raw_columns, list):
        return hints
    for column in raw_columns:
        if not isinstance(column, dict):
            continue
        key = column.get("key", column.get("field"))
        value_type = column.get("value_type", column.get("type"))
        if isinstance(key, str) and value_type in {"date", "datetime"}:
            hints[key] = str(value_type)
    return hints


def _numeric_column_hints(block: ExportIRBlock) -> dict[str, dict[str, Any]]:
    payload = block.content.get("payload")
    spec = payload.get("spec") if isinstance(payload, dict) else None
    columns = block.content.get("columns") if block.block_kind == "table" else spec.get("columns") if isinstance(spec, dict) else None
    return (
        {column["key"]: column for column in columns if isinstance(column, dict) and isinstance(column.get("key"), str)}
        if isinstance(columns, list)
        else {}
    )


def _numeric_cell_format(column: dict[str, Any], value: int | float) -> str:
    """Excel keeps typed source values; number formats carry known reading precision."""
    display = numeric_display(column.get("display"))
    scale = column.get("percent_scale")
    unit = column.get("unit")
    currency = currency_identity(column.get("currency_code")) or currency_identity(unit)
    kind = column.get("format")
    places = (
        display["decimal_places"]
        if display
        else 0
        if kind == "int"
        else 4
        if kind == "float"
        else 2
        if currency or kind == "currency" or scale or unit == "%"
        else 4
    )
    shown = value * 100 if scale == "fraction" else value
    tiny = not display and kind != "int" and 0 < abs(shown) < 0.5 * 10**-places
    digits = "0.0E+00" if tiny else "#,##0" + ("." + ("0" if display and display["decimal_mode"] == "fixed" else "#") * places if places else "")
    if scale == "fraction":
        return digits + "%"
    # Quote literal unit strings: a points column must not acquire Excel's implicit x100.
    label = currency or unit
    if isinstance(label, str) and label:
        return digits + ' "' + label.replace('"', '""') + '"'
    return digits


def _normalize_typed_scalar(
    *,
    value: Any,
    hint: str | None,
) -> tuple[str, Any]:
    if value is None:
        return "null", None
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, int) and not isinstance(value, bool):
        return ("string", str(value)) if abs(value) > 2**53 - 1 else ("integer", value)
    if isinstance(value, float):
        if not (float("-inf") < value < float("inf")):
            raise XlsxExportError("xlsx_non_finite_number")
        return "number", value
    if isinstance(value, datetime):
        normalized = value.replace(tzinfo=None)
        return "datetime", normalized
    if isinstance(value, date):
        return "date", value
    if isinstance(value, str) and hint in {"date", "datetime"}:
        try:
            if hint == "date":
                return "date", date.fromisoformat(value)
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return "datetime", parsed.replace(tzinfo=None)
        except ValueError:
            return "string", value
    if isinstance(value, str):
        return "string", value
    return (
        "json",
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _set_typed_value(
    cell: Cell,
    *,
    entry: _TypedEntry,
) -> None:
    if entry.value_type in {"string", "json", "object", "array"}:
        _set_text(cell, str(entry.value))
        return
    if entry.value_type == "null":
        cell.value = None
        return
    cell.value = entry.value
    if entry.value_type == "date":
        cell.number_format = "yyyy-mm-dd"
    elif entry.value_type == "datetime":
        cell.number_format = "yyyy-mm-dd hh:mm:ss"
    elif entry.value_type == "number":
        cell.number_format = "0.00############"
    elif entry.value_type == "integer":
        cell.number_format = "0"


def _typed_entries_from_part(
    part: _ObservedPart,
) -> tuple[_TypedEntry, ...]:
    rows = part.values
    if not rows or tuple(rows[0][:3]) != ("Path", "Type", "Value"):
        return ()
    entries: list[_TypedEntry] = []
    for row in rows[1:]:
        if len(row) < 3:
            return ()
        path, value_type, value = row[:3]
        if path is None and value_type is None and value is None:
            continue
        if not isinstance(path, str) or not isinstance(value_type, str):
            return ()
        if value_type == "empty":
            continue
        normalized = value
        if value_type == "date" and isinstance(value, datetime):
            normalized = value.date()
        elif value_type == "date" and isinstance(value, date):
            normalized = value
        elif value_type == "datetime" and isinstance(value, datetime):
            normalized = value.replace(tzinfo=None)
        elif value_type == "null" and value is None:
            normalized = None
        entries.append(
            _TypedEntry(
                path=path,
                value_type=value_type,
                value=normalized,
            )
        )
    return tuple(entries)


def _typed_entries_match(
    *,
    expected: tuple[_TypedEntry, ...],
    observed: tuple[_TypedEntry, ...],
) -> bool:
    if len(expected) != len(observed):
        return False
    for expected_entry, observed_entry in zip(expected, observed, strict=True):
        if expected_entry.path != observed_entry.path or expected_entry.value_type != observed_entry.value_type:
            return False
        if expected_entry.value_type == "number":
            if (
                not isinstance(expected_entry.value, (int, float))
                or isinstance(expected_entry.value, bool)
                or not isinstance(observed_entry.value, (int, float))
                or isinstance(observed_entry.value, bool)
                or not math.isclose(
                    float(expected_entry.value),
                    float(observed_entry.value),
                    rel_tol=1e-15,
                    abs_tol=0.0,
                )
            ):
                return False
        elif expected_entry.value != observed_entry.value:
            return False
    return True


def _inspect_package(path: Path) -> set[str]:
    failures: set[str] = set()
    if not path.is_file() or path.stat().st_size > MAX_XLSX_BYTES:
        return {"xlsx_size_invalid"}
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                failures.add("xlsx_zip_member_limit")
            names: set[str] = set()
            total_uncompressed = 0
            for info in infos:
                normalized = info.filename.lower()
                pure = PurePosixPath(info.filename)
                if info.filename in names:
                    failures.add("xlsx_zip_duplicate_member")
                names.add(info.filename)
                if pure.is_absolute() or ".." in pure.parts or "\\" in info.filename:
                    failures.add("xlsx_zip_path_invalid")
                if info.file_size > MAX_ZIP_MEMBER_BYTES:
                    failures.add("xlsx_zip_member_size_limit")
                total_uncompressed += info.file_size
                if info.compress_size > 0 and info.file_size / info.compress_size > MAX_ZIP_COMPRESSION_RATIO:
                    failures.add("xlsx_zip_ratio_limit")
                if any(part in f"/{normalized}" for part in _FORBIDDEN_MEMBER_PARTS):
                    failures.add("xlsx_forbidden_package_member")
                if any(normalized.endswith(suffix) for suffix in _FORBIDDEN_MEMBER_SUFFIXES):
                    failures.add("xlsx_forbidden_package_member")
                if normalized.endswith((".xml", ".rels")):
                    if info.file_size > MAX_XML_MEMBER_BYTES:
                        failures.add("xlsx_xml_member_limit")
                        continue
                    raw = archive.read(info)
                    upper = raw.upper()
                    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                        failures.add("xlsx_xml_entity_forbidden")
                    if normalized.endswith(".rels"):
                        failures.update(_inspect_relationship_xml(raw))
            if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                failures.add("xlsx_zip_uncompressed_limit")
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                failures.add("xlsx_package_parts_missing")
    except (OSError, zipfile.BadZipFile, RuntimeError):
        failures.add("xlsx_zip_invalid")
    return failures


def _inspect_relationship_xml(raw: bytes) -> set[str]:
    failures: set[str] = set()
    try:
        root = ET.fromstring(raw)  # noqa: S314 - DTD/entity rejected before parse.
    except ET.ParseError:
        return {"xlsx_relationship_xml_invalid"}
    for relationship in root.findall(f"{_REL_NS}Relationship"):
        if relationship.attrib.get("TargetMode") == "External":
            failures.add("xlsx_external_relationship_forbidden")
    return failures


def _inspect_workbook_structure(
    *,
    workbook: Any,
    expected_sheet_names: set[str],
    hard_failures: set[str],
) -> None:
    if len(workbook.sheetnames) > MAX_WORKBOOK_SHEETS:
        hard_failures.add("xlsx_sheet_limit")
    if len(workbook.sheetnames) != len(set(workbook.sheetnames)):
        hard_failures.add("xlsx_duplicate_sheet_name")
    for name in workbook.sheetnames:
        if len(name) > 31 or _INVALID_SHEET_CHARS_RE.search(name):
            hard_failures.add("xlsx_sheet_name_invalid")
        if name not in expected_sheet_names:
            hard_failures.add("xlsx_unexpected_sheet")
        if workbook[name].sheet_state != "visible":
            hard_failures.add("xlsx_sheet_hidden")
    for required in (
        "Summary",
        "Insights",
        "Recommendations",
        "Visual Index",
        "SQL & Code",
        "Sources",
        "Manifest",
    ):
        if required not in workbook.sheetnames:
            hard_failures.add("xlsx_required_sheet_missing")
    if getattr(workbook, "_external_links", []):
        hard_failures.add("xlsx_external_link_forbidden")


def _inspect_manifest_sheet(
    *,
    workbook: Any,
    export_ir: ExportIR,
    hard_failures: set[str],
) -> None:
    if "Manifest" not in workbook.sheetnames:
        return
    worksheet = workbook["Manifest"]
    headers = tuple(worksheet.cell(5, column).value for column in range(1, 10))
    if headers != (
        "Block key",
        "Section ID",
        "Revision",
        "Kind",
        "Path",
        "Placement",
        "Disposition",
        "Target sheets",
        "Source references",
    ):
        hard_failures.add("xlsx_manifest_header_mismatch")
        return
    observed: dict[str, tuple[Any, ...]] = {}
    duplicate = False
    for row in range(6, worksheet.max_row + 1):
        values = tuple(worksheet.cell(row, column).value for column in range(1, 10))
        block_key = values[0]
        if not isinstance(block_key, str):
            if any(value is not None for value in values):
                hard_failures.add("xlsx_manifest_row_invalid")
            continue
        if block_key in observed:
            duplicate = True
        observed[block_key] = values
    if duplicate:
        hard_failures.add("xlsx_manifest_duplicate_block")
    blocks = iter_export_blocks(export_ir.blocks)
    if set(observed) != {block.block_key for block in blocks}:
        hard_failures.add("xlsx_manifest_accounting_mismatch")
        return
    plan = {
        entry.block_key: entry
        for entry in build_capability_plan(
            export_ir,
            target="xlsx_data",
            include_appendix=True,
        )
    }
    for block in blocks:
        values = observed[block.block_key]
        expected_prefix: tuple[Any, ...] = (
            block.block_key,
            block.source.section_id,
            block.source.section_rev,
            block.source.section_kind,
            ".".join(str(item) for item in block.source.section_path),
            block.source.surface_placement,
            ("fallback" if plan[block.block_key].action == "fallback" else "rendered"),
        )
        if values[:7] != expected_prefix:
            hard_failures.add("xlsx_manifest_entry_mismatch")
        if not isinstance(values[7], str) or not values[7].strip():
            hard_failures.add("xlsx_manifest_target_missing")
        if values[8] != _source_refs_text(block.source_refs):
            hard_failures.add("xlsx_manifest_source_mismatch")


def _inspect_locator_accounting(
    *,
    blocks: tuple[ExportIRBlock, ...],
    observed_parts: dict[str, list[_ObservedPart]],
    hard_failures: set[str],
) -> None:
    for block in blocks:
        counts: dict[str, int] = defaultdict(int)
        part_numbers: set[int] = set()
        for part in observed_parts.get(block.block_key, []):
            counts[part.locator.role] += 1
            if part.locator.part in part_numbers:
                hard_failures.add("xlsx_duplicate_locator_part")
            part_numbers.add(part.locator.part)
        expected = {
            "semantic": 1,
            "typed": int(block.typed_data_sha256 is not None),
            "visual": int(block.visual_input_sha256 is not None),
        }
        if any(counts.get(role, 0) != count for role, count in expected.items()):
            hard_failures.add("xlsx_locator_accounting_mismatch")


def _libreoffice_recalculate(
    *,
    xlsx_path: Path,
    validation_dir: Path,
    soffice_path: Path,
    hard_failures: set[str],
) -> Path | None:
    if not soffice_path.is_file():
        hard_failures.add("xlsx_libreoffice_unavailable")
        return None
    source_dir = validation_dir / "source"
    output_dir = validation_dir / "recalculated"
    profile_dir = validation_dir / "lo-profile"
    shutil.rmtree(validation_dir, ignore_errors=True)
    source_dir.mkdir(mode=0o700, parents=True)
    output_dir.mkdir(mode=0o700)
    profile_dir.mkdir(mode=0o700)
    source_path = source_dir / "input.xlsx"
    shutil.copyfile(xlsx_path, source_path)
    environment = os.environ.copy()
    environment["HOME"] = str(profile_dir)
    environment["XDG_CACHE_HOME"] = str(profile_dir / "cache")
    environment["XDG_CONFIG_HOME"] = str(profile_dir / "config")
    profile_url = f"file://{quote(str(profile_dir.resolve()))}"
    try:
        completed = subprocess.run(
            [
                str(soffice_path),
                "--headless",
                f"-env:UserInstallation={profile_url}",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(output_dir),
                str(source_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        hard_failures.add("xlsx_libreoffice_recalc_failed")
        return None
    recalculated = output_dir / "input.xlsx"
    if completed.returncode != 0 or not recalculated.is_file():
        hard_failures.add("xlsx_libreoffice_recalc_failed")
        return None
    return recalculated


def _inspect_recalculated_workbook(
    *,
    recalculated_path: Path,
    expected_sheet_count: int,
    hard_failures: set[str],
) -> None:
    try:
        workbook = load_workbook(
            recalculated_path,
            data_only=True,
            keep_links=True,
        )
        if len(workbook.sheetnames) != expected_sheet_count:
            hard_failures.add("xlsx_libreoffice_sheet_mismatch")
        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows():
                for cell in row:
                    if cell.data_type == "e":
                        hard_failures.add("xlsx_libreoffice_formula_error")
        workbook.close()
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        hard_failures.add("xlsx_libreoffice_reopen_failed")


def _images_by_anchor(
    worksheet: Worksheet,
) -> dict[tuple[int, int], list[bytes]]:
    indexed: dict[tuple[int, int], list[bytes]] = defaultdict(list)
    for image in cast(list[Any], getattr(worksheet, "_images", [])):
        anchor = getattr(image, "anchor", None)
        origin = getattr(anchor, "_from", None)
        if origin is None:
            continue
        try:
            content = cast(bytes, image._data())  # noqa: SLF001
        except (OSError, ValueError, AttributeError):
            continue
        indexed[(int(origin.row) + 1, int(origin.col) + 1)].append(content)
    return indexed


def _visible_range_values(
    *,
    worksheet: Worksheet,
    locator: _Locator,
) -> list[list[Any]]:
    if (
        locator.min_row <= 0
        or locator.max_row < locator.min_row
        or locator.min_col <= 0
        or locator.max_col < locator.min_col
        or locator.max_row > worksheet.max_row + 100
        or locator.max_col > worksheet.max_column + 100
    ):
        return []
    values = [
        [worksheet.cell(row, column).value for column in range(locator.min_col, locator.max_col + 1)]
        for row in range(locator.min_row, locator.max_row + 1)
    ]
    _reassemble_text_chunks(
        worksheet=worksheet,
        locator=locator,
        values=values,
    )
    return values


def _locator_range_is_visible(
    *,
    worksheet: Worksheet,
    locator: _Locator,
) -> bool:
    if worksheet.sheet_state != "visible":
        return False
    if any(worksheet.row_dimensions[row].hidden for row in range(locator.min_row, locator.max_row + 1)):
        return False
    return not any(worksheet.column_dimensions[get_column_letter(column)].hidden for column in range(locator.min_col, locator.max_col + 1))


def _reassemble_text_chunks(
    *,
    worksheet: Worksheet,
    locator: _Locator,
    values: list[list[Any]],
) -> None:
    groups: dict[
        str,
        list[tuple[int, int, int, int, str]],
    ] = defaultdict(list)
    for row_offset, row in enumerate(
        range(locator.min_row, locator.max_row + 1),
    ):
        for column_offset, column in enumerate(
            range(locator.min_col, locator.max_col + 1),
        ):
            cell = worksheet.cell(row, column)
            comment = cell.comment
            if comment is None or not comment.text.startswith(_TEXT_CHUNK_PREFIX) or not isinstance(cell.value, str):
                continue
            fields = comment.text[len(_TEXT_CHUNK_PREFIX) :].rsplit(":", maxsplit=2)
            if len(fields) != 3:
                continue
            group, index_value, count_value = fields
            try:
                index = int(index_value)
                count = int(count_value)
            except ValueError:
                continue
            if not group or index < 0 or count < 2:
                continue
            groups[group].append(
                (
                    index,
                    count,
                    row_offset,
                    column_offset,
                    cell.value,
                )
            )

    for parts in groups.values():
        ordered = sorted(parts)
        count = ordered[0][1]
        if len(ordered) != count or [part[0] for part in ordered] != list(range(count)) or any(part[1] != count for part in ordered):
            continue
        first = ordered[0]
        values[first[2]][first[3]] = "".join(part[4] for part in ordered)
        for part in ordered[1:]:
            values[part[2]][part[3]] = None


def _attach_locator(
    *,
    cell: Cell,
    block: ExportIRBlock,
    part: int,
    role: str,
    disposition: XlsxDisposition,
    reason_code: ManifestReasonCode | None,
    min_row: int,
    max_row: int,
    min_col: int,
    max_col: int,
) -> None:
    payload = {
        "d": disposition,
        "k": _marker_id(block.block_key),
        "p": part,
        "r": role,
        "reason": reason_code,
        "s": _source_digest(block.source_refs),
        "x1": min_col,
        "x2": max_col,
        "y1": min_row,
        "y2": max_row,
    }
    cell.comment = Comment(
        _LOCATOR_PREFIX
        + json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "Bowhead",
    )


def _parse_locator(value: str) -> _Locator | None:
    if not value.startswith(_LOCATOR_PREFIX):
        return None
    try:
        raw = json.loads(value[len(_LOCATOR_PREFIX) :])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    disposition = raw.get("d")
    reason = raw.get("reason")
    if disposition not in {"rendered", "fallback"}:
        return None
    if reason not in {None, "interactive_not_executable"}:
        return None
    marker = raw.get("k")
    source_digest = raw.get("s")
    role = raw.get("r")
    numbers = (
        raw.get("p"),
        raw.get("x1"),
        raw.get("x2"),
        raw.get("y1"),
        raw.get("y2"),
    )
    if (
        not isinstance(marker, str)
        or not re.fullmatch(r"[0-9a-f]{24}", marker)
        or not isinstance(source_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_digest)
        or role not in {"semantic", "typed", "visual"}
        or any(not isinstance(number, int) or isinstance(number, bool) for number in numbers)
    ):
        return None
    return _Locator(
        marker=marker,
        source_digest=source_digest,
        part=int(raw["p"]),
        role=str(role),
        disposition=cast(XlsxDisposition, disposition),
        reason_code=cast(ManifestReasonCode | None, reason),
        min_row=int(raw["y1"]),
        max_row=int(raw["y2"]),
        min_col=int(raw["x1"]),
        max_col=int(raw["x2"]),
    )


def _sources_from_semantic_part(
    part: _ObservedPart,
) -> tuple[str, ...]:
    for row in part.values:
        if len(row) >= 2 and row[0] == "Source references":
            value = row[1]
            if value == "—":
                return ()
            if not isinstance(value, str):
                return ()
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return ()
            if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                return tuple(sorted(cast(list[str], parsed)))
    return ()


def _tokens_in_order(
    *,
    expected: tuple[str, ...],
    observed: list[str],
) -> bool:
    position = 0
    for token in expected:
        try:
            position = observed.index(token, position) + 1
        except ValueError:
            return False
    return True


def _source_refs_text(source_refs: tuple[str, ...]) -> str:
    if not source_refs:
        return "—"
    return json.dumps(
        list(source_refs),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _artifact_ids_text(block: ExportIRBlock) -> str:
    raw = block.content.get("artifact_content_hashes")
    if not isinstance(raw, dict):
        return "—"
    artifact_ids = sorted(key for key, value in raw.items() if isinstance(key, str) and isinstance(value, str))
    return ", ".join(artifact_ids) if artifact_ids else "—"


def _visual_artifact_hashes(block: ExportIRBlock) -> dict[str, str]:
    raw = block.content.get("artifact_content_hashes")
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}


def _index_visuals(
    *,
    export_ir: ExportIR,
    materialized_visuals: tuple[MaterializedVisual, ...],
    attempt_dir: Path,
) -> dict[str, MaterializedVisual]:
    indexed: dict[str, MaterializedVisual] = {}
    for visual in materialized_visuals:
        if visual.block_key in indexed:
            raise XlsxExportError("duplicate_materialized_visual")
        relative = Path(visual.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise XlsxExportError("xlsx_visual_staging_invalid")
        path = attempt_dir / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(attempt_dir.resolve()):
            raise XlsxExportError("xlsx_visual_staging_invalid")
        indexed[visual.block_key] = visual
    expected = {block.block_key for block in iter_export_blocks(export_ir.blocks) if block.visual_input_sha256 is not None}
    if set(indexed) != expected:
        raise XlsxExportError("xlsx_visual_accounting_mismatch")
    return indexed


def _family_for_block(block: ExportIRBlock) -> str:
    if block.block_kind == "callout":
        return "insights"
    if block.block_kind == "recommendations":
        return "recommendations"
    if block.block_kind == "visual":
        return "visual"
    if block.block_kind == "code":
        return "code"
    if block.block_kind == "source_list":
        return "sources"
    if block.block_kind == "interactive_fallback":
        return "visual"
    return "summary"


def _family_title(name: str) -> str:
    return {
        "insights": "Insights",
        "recommendations": "Recommendations",
        "visual": "Visual Index",
        "code": "SQL & Code",
        "sources": "Sources",
    }[name]


def _block_display_title(block: ExportIRBlock) -> str:
    for value in (
        block.content.get("title"),
        block.content.get("label"),
        (block.content.get("payload", {}).get("title") if isinstance(block.content.get("payload"), dict) else None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    if block.semantic_tokens:
        return block.semantic_tokens[0][:120]
    return block.source.section_kind.replace("_", " ").title()


def _write_workbook_header(
    *,
    worksheet: Worksheet,
    title: str,
    subtitle: str,
) -> None:
    worksheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=10)
    _set_text(worksheet.cell(1, 1), title)
    worksheet.cell(1, 1).font = Font(
        name="Noto Sans CJK SC",
        size=20,
        bold=True,
        color=_WHITE,
    )
    worksheet.cell(1, 1).fill = PatternFill("solid", fgColor=_FOREST)
    worksheet.cell(1, 1).alignment = Alignment(vertical="center")
    worksheet.row_dimensions[1].height = 32
    worksheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=10)
    _set_text(worksheet.cell(2, 1), subtitle)
    worksheet.cell(2, 1).font = Font(
        name="Noto Sans CJK SC",
        size=10,
        color=_MUTED,
        italic=True,
    )
    worksheet.cell(2, 1).fill = PatternFill("solid", fgColor=_CREAM)
    worksheet.row_dimensions[2].height = 22


def _setup_sheet(worksheet: Worksheet) -> None:
    worksheet.sheet_view.showGridLines = False
    worksheet.freeze_panes = "A4"
    worksheet.sheet_properties.pageSetUpPr.fitToPage = True
    worksheet.page_setup.fitToWidth = 1
    worksheet.page_setup.fitToHeight = 0
    worksheet.sheet_properties.outlinePr.summaryBelow = True
    for column, width in {
        "A": 24,
        "B": 42,
        "C": 24,
        "D": 16,
        "E": 18,
        "F": 18,
        "G": 18,
        "H": 18,
        "I": 18,
        "J": 18,
    }.items():
        worksheet.column_dimensions[column].width = width


def _finalize_workbook(workbook: Workbook) -> None:
    for worksheet in workbook.worksheets:
        worksheet.sheet_state = "visible"
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                font = copy.copy(cell.font)
                font.name = "Noto Sans CJK SC"
                if font.sz is None:
                    font.sz = 10
                cell.font = font
                if isinstance(cell.value, str) and cell.data_type == "f":
                    raise XlsxExportError("xlsx_formula_injection")
        worksheet.auto_filter.ref = worksheet.auto_filter.ref


def _set_text(cell: Cell, value: str) -> None:
    if len(value) > MAX_CELL_TEXT_CHARS:
        raise XlsxExportError("xlsx_cell_text_too_long")
    cell.value = value
    cell.data_type = "s"


def _write_text_chunks(
    *,
    worksheet: Worksheet,
    start_row: int,
    column: int,
    value: str,
) -> int:
    chunks = tuple(value[offset : offset + MAX_CELL_TEXT_CHARS] for offset in range(0, len(value), MAX_CELL_TEXT_CHARS)) or ("",)
    group = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    for index, chunk in enumerate(chunks):
        cell = worksheet.cell(start_row + index, column)
        _set_text(cell, chunk)
        if len(chunks) > 1:
            cell.comment = Comment(
                (f"{_TEXT_CHUNK_PREFIX}{group}:{index}:{len(chunks)}"),
                "Bowhead",
            )
    return start_row + len(chunks) - 1


def _write_typed_entry(
    *,
    worksheet: Worksheet,
    start_row: int,
    entry: _TypedEntry,
) -> int:
    _set_text(worksheet.cell(start_row, 1), entry.path)
    _set_text(worksheet.cell(start_row, 2), entry.value_type)
    if entry.value_type in {"string", "json", "object", "array"} and len(str(entry.value)) > MAX_CELL_TEXT_CHARS:
        return _write_text_chunks(
            worksheet=worksheet,
            start_row=start_row,
            column=3,
            value=str(entry.value),
        )
    _set_typed_value(
        worksheet.cell(start_row, 3),
        entry=entry,
    )
    return start_row


def _prepare_output_path(
    *,
    attempt_dir: Path,
    output_name: str,
) -> Path:
    if not attempt_dir.is_absolute() or Path(output_name).name != output_name or not output_name.lower().endswith(".xlsx"):
        raise XlsxExportError("xlsx_output_path_invalid")
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir = attempt_dir / "output"
    output_dir.mkdir(mode=0o700, exist_ok=True)
    if attempt_dir.is_symlink() or output_dir.is_symlink():
        raise XlsxExportError("xlsx_output_path_invalid")
    return output_dir / output_name


def _unique_sheet_title(
    *,
    base: str,
    existing: set[str],
) -> str:
    cleaned = _INVALID_SHEET_CHARS_RE.sub(" ", base)
    cleaned = " ".join(cleaned.replace("\x00", " ").split()).strip("' ")
    cleaned = cleaned or "Sheet"
    candidate = cleaned[:31]
    suffix = 2
    while candidate.casefold() in {name.casefold() for name in existing}:
        tail = f" ({suffix})"
        candidate = f"{cleaned[: 31 - len(tail)]}{tail}"
        suffix += 1
    return candidate


def _escape_path(value: str) -> str:
    return value.replace("\\", "\\\\").replace(".", "\\.")


def _path_leaf(path: str) -> str:
    leaf = path.rsplit(".", maxsplit=1)[-1]
    leaf = re.sub(r"\[[0-9]+]$", "", leaf)
    return leaf.replace("\\.", ".").replace("\\\\", "\\")


def _cell_visible_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return " ".join(str(value).split())


def _marker_id(block_key: str) -> str:
    return hashlib.sha256(block_key.encode("utf-8")).hexdigest()[:24]


def _source_digest(source_refs: tuple[str, ...]) -> str:
    encoded = json.dumps(
        sorted(source_refs),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _demo_residue_count(
    worksheet: Worksheet,
    *,
    expected_fingerprints: set[str],
) -> int:
    count = 0
    for row in worksheet.iter_rows():
        for cell in row:
            value = cell.value
            if not isinstance(value, str):
                continue
            count += sum(value.count(item) for item in _DEMO_FINGERPRINTS if item not in expected_fingerprints)
    return count


def _visible_pixel_sha256(content: bytes) -> str:
    with Image.open(io.BytesIO(content)) as image:
        image.load()
        rgba = image.convert("RGBA")
    visible = Image.new("RGB", rgba.size, "white")
    visible.paste(rgba, mask=rgba.getchannel("A"))
    identity = visible.width.to_bytes(4, "big") + visible.height.to_bytes(4, "big") + visible.tobytes()
    return _sha256_bytes(identity)


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


__all__ = [
    "MAX_TYPED_CELLS",
    "XLSX_MIME",
    "XLSX_RENDERER_NAME",
    "XLSX_RENDERER_VERSION",
    "XlsxExportError",
    "XlsxRenderResult",
    "extract_xlsx_evidence",
    "render_export_xlsx",
]
