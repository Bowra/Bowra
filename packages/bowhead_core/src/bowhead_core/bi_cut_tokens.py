"""Translate Datarc `/data/cut` token props into the model-facing `_parse.tokens` shape.

Datarc labels each word with an internal prop: `m1` (measure 1), `d54` (dimension 54),
`d50=5LiK5rW3` (a base64 value of dimension 50), `t1` (time field), `dateRange_…`, `op_count`,
`limit_10`, `unknown`, and so on. No BI tool accepts these ids as input, so they only give the
model a second, opaque spelling of names it already has, which it can copy into queries or
prose. The model sees `{word, kind, field?, value?}` instead; the raw props stay in the result's
`_debug_cut_props` for audit, which `model_view` keeps out of the model context.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from typing import Any


def _decode_value(encoded: str) -> str:
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""


def _numeric_id(text: str) -> int | None:
    return int(text) if text.isdigit() else None


def field_names(items: Iterable[Any]) -> dict[int, str]:
    """Schema items (MeasureMeta / DimensionMeta) → {id: primary name}."""
    out: dict[int, str] = {}
    for item in items:
        names = getattr(item, "names", None) or []
        item_id = getattr(item, "id", None)
        if isinstance(item_id, int) and names:
            out[item_id] = str(names[0])
    return out


def describe_token(word: str, prop: str, *, measures: Mapping[int, str], dimensions: Mapping[int, str]) -> dict[str, str]:
    """One cut token → `{word, kind, field?, value?}` without Datarc ids.

    `field` is omitted when the id is not in the schema names (unknown or not loaded).
    """
    out: dict[str, str] = {"word": word}
    head, _, encoded = prop.partition("=")
    if head[:1] == "m" and (mid := _numeric_id(head[1:])) is not None:
        out["kind"] = "measure"
        if name := measures.get(mid):
            out["field"] = name
    elif head[:1] == "d" and (did := _numeric_id(head[1:])) is not None:
        out["kind"] = "dimension_value" if encoded else "dimension"
        if name := dimensions.get(did):
            out["field"] = name
        if encoded and (value := _decode_value(encoded)):
            out["value"] = value
    elif head[:1] == "t" and _numeric_id(head[1:]) is not None:
        out["kind"] = "time_field"
    elif head.startswith(("dateRange_", "dateBefore_", "dateAfter_")):
        out["kind"] = "time_range"
    elif head.startswith("limit_"):
        out["kind"] = "limit"
    elif head.startswith(("op_", "$")):
        out["kind"] = "operator"
    elif head.startswith("value_"):
        out["kind"] = "value"
        if value := _decode_value(prop[len("value_") :]):
            out["value"] = value
    elif prop == "unknown":
        out["kind"] = "unknown"
    else:
        out["kind"] = "other"
    return out
