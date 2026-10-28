"""Structural candidates from one already prepared result, never business joins."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from bowhead_core.chart.types import ChartColumn

MAX_CATALOG_BYTES = 4_000
MAX_CANDIDATES = 12
MAX_KEY_COLUMNS = 6
MAX_ARTIFACT_CATALOG_BYTES = 2_000_000


async def artifact_binding_catalog(artifact: Any, store: Any) -> dict[str, Any] | None:
    """Describe an already authorized CSV using the native chart's exact parser.

    This bounded model hint grants no access; add_chart still rereads and verifies
    the selected source and candidate at admission.
    """
    from bowhead_core.canvas_tools import _parse_csv_with_types, _retype_time_index_columns

    filename = str(getattr(artifact, "filename", "")).lower()
    size = getattr(artifact, "size", None)
    if not filename.endswith(".csv") or not isinstance(size, int) or not 0 < size <= MAX_ARTIFACT_CATALOG_BYTES:
        return None
    raw = await store.backend.read_bounded(artifact.path, limit=MAX_ARTIFACT_CATALOG_BYTES + 1)
    if len(raw) > MAX_ARTIFACT_CATALOG_BYTES or len(raw) != size:
        return None
    rows, columns = _parse_csv_with_types(raw)
    _retype_time_index_columns(rows, columns, skip=set())
    return build_binding_catalog(columns, rows, raw=raw)


def source_binding_columns(columns: Sequence[ChartColumn], rows: Sequence[Mapping[str, Any]], raw: bytes) -> list[ChartColumn]:
    """Use source types for binding grants; display declarations may preserve numeric-looking text."""
    from bowhead_core.canvas_tools import _infer_col_type, _parse_csv_with_types, _retype_time_index_columns

    original_rows, original = _parse_csv_with_types(raw)
    _retype_time_index_columns(original_rows, original, skip=set())
    by_key = {column.key: column for column in original}
    resolved = [
        by_key.get(column.key)
        or ChartColumn(key=column.key, header=column.key, type=_infer_col_type(column.key, [row.get(column.key) for row in rows]))
        for column in columns
    ]
    _retype_time_index_columns(list(rows), resolved, skip=set(by_key))
    return resolved


def typed_tuple(row: Mapping[str, Any], keys: Sequence[str]) -> str:
    """Canonical typed cells keep strings, numbers, null and empty distinct."""
    values = [row.get(key) for key in keys]
    return json.dumps(
        [int(value) if type(value) is float and math.isfinite(value) and value.is_integer() else value for value in values],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _cell(value: Any) -> bool:
    return value is None or isinstance(value, str) or (type(value) in (int, float) and math.isfinite(value))


def _time_order(column: ChartColumn, rows: Sequence[Mapping[str, Any]]) -> str | None:
    if column.type != "date":
        return None
    values = [row.get(column.key) for row in rows]
    if all(type(value) in (int, float) and math.isfinite(value) for value in values):
        # Month/week-only indices have no chronology across years.
        if re.fullmatch(r"year|年份|年", column.key, re.I) and all(type(value) is int and 1000 <= value <= 9999 for value in values):
            return "numeric"
        return None
    try:
        if len({len(value) for value in values if isinstance(value, str)}) != 1:
            return None
        for value in values:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
                return None
            date.fromisoformat(value + "-01" if len(value) == 7 else value)
    except ValueError:
        return None
    return "date"


def build_binding_catalog(columns: Sequence[ChartColumn], rows: Sequence[Mapping[str, Any]], *, raw: bytes) -> dict[str, Any]:
    """Offer only unique retained observation tuples; callers revalidate at admission."""
    dimensions = [column.key for column in columns if column.type in ("categorical", "date", "geo")]
    measures = [column.key for column in columns if column.type == "numeric"]
    catalog: dict[str, Any] = {
        "version": 1,
        "content_digest": hashlib.sha256(raw).hexdigest(),
        "artifact_rows": len(rows),
        "candidates": [],
        "omitted": 0,
        "scope": "Prepared artifact observations; business population completeness and additivity are unknown.",
    }
    if not rows or not dimensions or len(dimensions) > MAX_KEY_COLUMNS or not measures or len(measures) > 12:
        catalog["omission_reason"] = "No bounded dimension/measure grain is available. Ordinary charts remain supported."
        return catalog
    if any(not all(key in row and _cell(row[key]) for key in dimensions) for row in rows):
        catalog["omission_reason"] = "Observation keys must be finite typed cells."
        return catalog
    tuples = {typed_tuple(row, dimensions) for row in rows}
    if len(tuples) != len(rows):
        catalog["omission_reason"] = "Duplicate observation tuples require upstream preparation before linkage."
        return catalog

    def offer(kind: str, keys: list[str], **extra: Any) -> None:
        shape = {"kind": kind, "dimension_keys": keys, "observation_keys": dimensions, "measure_keys": measures, **extra}
        identity = {key: value for key, value in shape.items() if key != "measure_keys"}
        identifier = kind + "." + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
        candidate = {"id": identifier, **shape}
        trial = {**catalog, "candidates": [*catalog["candidates"], candidate]}
        if len(catalog["candidates"]) >= MAX_CANDIDATES or len(json.dumps(trial, ensure_ascii=False).encode()) > MAX_CATALOG_BYTES - 100:
            catalog["omitted"] += 1
        else:
            catalog["candidates"].append(candidate)

    offer("row", dimensions)
    for column in columns:
        if column.type in ("categorical", "geo") and any(row.get(column.key) is not None for row in rows):
            offer("entity", [column.key])
    for column in columns:
        order = _time_order(column, rows)
        if order is None:
            continue
        offer("time", [column.key], time_key=column.key, order=order)
        entity_keys = [key for key in dimensions if key != column.key]
        frames = sorted({row[column.key] for row in rows})
        if entity_keys and len(frames) <= 120:
            # Values stay out of the model catalog; the compiler resolves exact prepared frames.
            offer("frame", entity_keys, time_key=column.key, entity_keys=entity_keys, frame_count=len(frames))
    return catalog


def bounded_catalog(value: Any) -> dict[str, Any] | None:
    """Keep context refresh bounded without granting descriptor or dataset access."""
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("candidates"), list):
        return None
    try:
        if len(value["candidates"]) > MAX_CANDIDATES or len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_CATALOG_BYTES:
            return None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    return value
