"""Bounded optional display metadata; values and analytical meaning stay untouched."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

from babel.core import get_global


class NumericDisplay(TypedDict):
    decimal_places: int
    decimal_mode: Literal["max", "fixed"]


def currency_identity(value: Any) -> str | None:
    """Only a known ISO code establishes currency identity, never a symbol or locale."""
    code = value.strip().upper() if isinstance(value, str) else ""
    return code if code in get_global("all_currencies") and code not in {"XXX", "XTS"} else None


def numeric_display(value: Any) -> NumericDisplay | None:
    if not isinstance(value, dict) or set(value) != {"decimal_places", "decimal_mode"}:
        return None
    places = value["decimal_places"]
    mode = value["decimal_mode"]
    if type(places) is not int or not 0 <= places <= 12 or mode not in ("max", "fixed"):
        return None
    return {"decimal_places": places, "decimal_mode": mode}


def numeric_format_kinds(column: Mapping[str, Any]) -> list[str]:
    """Expose precision edits without guessing a source unit or ratio shape."""
    if column.get("type") != "numeric":
        return []
    kinds = ["number"]
    percent = column.get("percent_scale") in ("fraction", "points") or column.get("unit") == "%"
    currency_code = currency_identity(column.get("currency_code"))
    unit_code = currency_identity(column.get("unit"))
    currency = bool(currency_code or unit_code or column.get("format") == "currency")
    if (percent and currency) or (currency_code and unit_code and currency_code != unit_code):
        return kinds
    if percent and column.get("format") not in ("currency", "datetime"):
        kinds.append("percent")
    if currency:
        kinds.append("currency")
    return kinds


def apply_column_display(columns: list[Any], requested: Any) -> list[dict[str, str]]:
    """Drop only unsupported optional entries and return actionable Tool receipts."""
    if requested is None:
        return []
    entries = requested if isinstance(requested, dict) else {"": requested}
    numeric = {column.key: column for column in columns if column.type == "numeric"}
    fallbacks = []
    for key, value in entries.items():
        display = numeric_display(value)
        if key not in numeric or display is None:
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            fallbacks.append(
                {
                    "code": "invalid_numeric_display",
                    "path": "/column_display" + ("/" + escaped if escaped else ""),
                    "message": "Use an exact numeric column key with decimal_places (integer 0–12) and decimal_mode (max or fixed); default formatting was retained.",
                }
            )
        else:
            numeric[key].display = display
    return fallbacks


def source_numeric_format(value: Any) -> dict[str, Any]:
    """Support only an unambiguous numeric format subset, not a format language."""
    token = value.strip().lower() if isinstance(value, str) else ""
    if token in {"integer", "int"}:
        return {"format": "int"}
    if token in {"decimal", "float", "number"}:
        return {"format": "float"}
    match = re.fullmatch(r"(?:#,##)?0(?:\.(0{1,12}))?", token)
    return {"display": {"decimal_places": len(match[1] or ""), "decimal_mode": "fixed"}} if match else {}
