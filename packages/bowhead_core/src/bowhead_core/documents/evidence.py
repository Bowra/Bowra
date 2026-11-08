"""Bounded evidence preparation for the isolated document worker only.

Never evaluate spreadsheet formulas or fetch external links. Original uploads
remain frozen separately; this normalized representation is also hashed into the
planning context. CSV numeric syntax is deliberately narrow to preserve IDs.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from bowhead_core.documents.package_io import MAX_EXPANDED_BYTES, MAX_PART_BYTES, MAX_PARTS, OfficePackageError, _safe_part, _xml, _XmlBudget
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, _json
from bowhead_core.read_receipts import ReadLimit, read_info

MAX_DATA_BYTES = 4 * 1024 * 1024
MAX_CELLS = 512
MAX_NORMALIZED_BYTES = 128_000
MAX_PREVIEW_RECORDS = 21
MAX_PREVIEW_BYTES = 64_000
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]{0,99})(?:\.[0-9]{1,100})?\Z")


@dataclass(frozen=True)
class InspectedEvidence(JsonEvidenceSource):
    read_info: dict


def _limited(kind: str, actual: int | None, maximum: int, unit: str, *, preview: bool = False) -> OfficePackageError:
    limit = ReadLimit(scope="model_preview" if preview else "parser", kind=kind, actual=actual, maximum=maximum, unit=unit)
    return OfficePackageError("document_data_limit", read_info=read_info(view="none", unit=unit, returned=0, limit=limit))


def _csv_error(error: csv.Error, field_limit: int) -> OfficePackageError:
    # The supported Python runtime exposes this resource refusal only as csv.Error.
    # Do not infer a field's actual length or classify other parser messages by substring.
    if str(error) == f"field larger than field limit ({field_limit})":
        limit = ReadLimit(scope="parser", kind="csv_field_characters", maximum=field_limit, unit="characters")
        return OfficePackageError("document_data_invalid", read_info=read_info(view="none", unit="csv_records", returned=0, limit=limit))
    return OfficePackageError("document_data_invalid", read_info=read_info(view="none", unit="csv_records", returned=0))


def _encode(value: object) -> str:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise OfficePackageError("document_data_invalid")
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + _encode(item) for key, item in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _xlsx(content: bytes) -> object:
    from openpyxl import load_workbook

    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_PARTS:
                raise _limited("archive_parts", len(entries), MAX_PARTS, "parts")
            expanded = sum(entry.file_size for entry in entries)
            if expanded > MAX_EXPANDED_BYTES:
                raise _limited("expanded_bytes", expanded, MAX_EXPANDED_BYTES, "bytes")
            seen = set()
            budget = _XmlBudget()
            for entry in entries:
                name = entry.filename
                if entry.is_dir():
                    if not _safe_part(name.rstrip("/")) or entry.file_size:
                        raise OfficePackageError("document_data_invalid")
                    continue
                if not _safe_part(name) or name.casefold() in seen or entry.flag_bits & 1 or entry.file_size > MAX_PART_BYTES:
                    raise OfficePackageError("document_data_invalid")
                seen.add(name.casefold())
                if any(marker in name.casefold() for marker in ("vbaproject", "externallinks/", "embeddings/", "connections.xml")):
                    raise OfficePackageError("document_data_unsupported")
                raw = archive.read(entry)
                if name.endswith((".xml", ".rels")):
                    root = _xml(raw, budget)
                    if name.endswith(".rels") and any(node.get("TargetMode") == "External" for node in root):
                        raise OfficePackageError("document_data_unsupported")
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, KeyError, ValueError, OSError) as exc:
        if isinstance(exc, OfficePackageError):
            raise
        raise OfficePackageError("document_data_invalid") from exc
    result, count = {}, 0
    try:
        if len(workbook.worksheets) > 8:
            raise _limited("worksheets", len(workbook.worksheets), 8, "worksheets")
        for sheet in workbook.worksheets:
            extent = (sheet.max_row or 0) * (sheet.max_column or 0)
            if extent > MAX_CELLS:
                raise _limited("worksheet_extent", extent, MAX_CELLS, "extent_cells", preview=True)
            rows = []
            for row in sheet.iter_rows():
                values = []
                for cell in row:
                    count += 1
                    if count > MAX_CELLS:
                        raise _limited("visited_cells", count, MAX_CELLS, "cells", preview=True)
                    if cell.data_type in {"f", "e"}:
                        raise OfficePackageError("document_data_formula_unsupported")
                    value = cell.value
                    if isinstance(value, (datetime, date, time)):
                        value = value.isoformat()
                    elif isinstance(value, float):
                        value = Decimal(str(value))
                    values.append(value)
                rows.append(values)
            result[sheet.title] = rows
    finally:
        workbook.close()
    return result


def prepare_evidence(name: str, content: bytes) -> JsonEvidenceSource:
    """Normalize a supported upload without truncating cells or inventing data."""
    if not name or len(name) > 256:
        raise _limited("filename_characters", len(name), 256, "characters")
    if len(content) > MAX_DATA_BYTES:
        raise _limited("input_bytes", len(content), MAX_DATA_BYTES, "bytes")
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == ".json":
        if len(content) > MAX_NORMALIZED_BYTES:
            raise _limited("normalized_bytes", len(content), MAX_NORMALIZED_BYTES, "bytes", preview=True)
        _json(content)
        return JsonEvidenceSource(name, content)
    if suffix == ".xlsx":
        value = _xlsx(content)
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise OfficePackageError("document_data_encoding_unsupported") from exc
        if suffix in {".txt", ".md"}:
            value = {"text": text}
        elif suffix == ".csv":
            rows, count = [], 0
            field_limit = csv.field_size_limit()
            try:
                for row in csv.reader(io.StringIO(text), strict=True):
                    count += len(row)
                    if count > MAX_CELLS:
                        raise _limited("visited_cells", count, MAX_CELLS, "cells", preview=True)
                    rows.append([Decimal(cell) if _NUMBER.fullmatch(cell) else cell for cell in row])
            except csv.Error as exc:
                raise _csv_error(exc, field_limit) from exc
            value = rows
        else:
            raise OfficePackageError("document_data_format_unsupported")
    encoded = _encode(value).encode()
    if len(encoded) > MAX_NORMALIZED_BYTES:
        raise _limited("normalized_bytes", len(encoded), MAX_NORMALIZED_BYTES, "bytes", preview=True)
    return JsonEvidenceSource(name, encoded)


def inspect_evidence(name: str, content: bytes) -> InspectedEvidence:
    """Inspect a large CSV as an explicit prefix; never substitute it for full evidence."""
    try:
        complete = prepare_evidence(name, content)
        suffix = PurePosixPath(name).suffix.lower()
        unit, count = "bytes", len(complete.content)
        if suffix == ".csv":
            unit, count = "csv_records", len(_json(complete.content))
        elif suffix in {".txt", ".md"}:
            unit, count = "characters", len(_json(complete.content)["text"])
        elif suffix == ".xlsx":
            unit, count = "cells", sum(len(row) for rows in _json(complete.content).values() for row in rows)
        return InspectedEvidence(complete.name, complete.content, read_info(view="complete", unit=unit, returned=count, total=count))
    except OfficePackageError as error:
        if (
            error.code != "document_data_limit"
            or PurePosixPath(name).suffix.lower() != ".csv"
            or not name
            or len(name) > 256
            or len(content) > MAX_DATA_BYTES
        ):
            raise
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError as error:
        raise OfficePackageError("document_data_encoding_unsupported") from error
    rows, cells, size, total = [], 0, 0, 0
    collecting = True
    limit = None
    field_limit = csv.field_size_limit()
    try:
        for row in csv.reader(io.StringIO(text), strict=True):
            total += 1
            if not collecting:
                continue
            if len(rows) >= MAX_PREVIEW_RECORDS:
                limit = ReadLimit(scope="model_preview", kind="preview_records", actual=total, maximum=MAX_PREVIEW_RECORDS, unit="csv_records")
                collecting = False
                continue
            if cells + len(row) > MAX_CELLS:
                limit = ReadLimit(scope="model_preview", kind="preview_cells", actual=cells + len(row), maximum=MAX_CELLS, unit="cells")
                collecting = False
                continue
            values = [Decimal(cell) if _NUMBER.fullmatch(cell) else cell for cell in row]
            row_size = len(_encode(values).encode())
            if size + row_size > MAX_PREVIEW_BYTES:
                limit = ReadLimit(scope="model_preview", kind="preview_record_bytes", actual=size + row_size, maximum=MAX_PREVIEW_BYTES, unit="bytes")
                collecting = False
                continue
            rows.append(values)
            cells += len(row)
            size += row_size
    except csv.Error as error:
        raise _csv_error(error, field_limit) from error
    metadata = read_info(view="partial" if rows else "none", unit="csv_records", returned=len(rows), total=total, limit=limit)
    if not rows:
        raise OfficePackageError("document_data_limit", read_info=metadata)
    # Keep coverage inside content_json so older clients cannot discard it.
    preview = {
        "format": "csv_preview",
        "records": rows,
        "coverage": {"total_records": total, "returned_records": len(rows), "complete": len(rows) == total},
        "usage": "This is a prefix of the original CSV records, including the header if present, not a complete dataset. "
        "Use the complete bound file bytes in the final edit script for filtering, joins and calculations. "
        "Repeating this inspection returns the same prefix; do not calculate totals from it.",
    }
    return InspectedEvidence(name, _encode(preview).encode(), metadata)
