"""Validate non-executable calendar display patterns for native charts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_TOKEN = re.compile(r"yyyy|yy|MM|dd|HH|mm|ss|XXX|[\s/.:\-]")
_CALENDAR = re.compile(r"^(\d{4})[-年](\d{1,2})(?:月|-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:?\d{2})?)?)?$")
DATE_PRESETS = {
    "iso_date": "yyyy-MM-dd",
    "compact_date": "yyyyMMdd",
    "year_month": "yyyy-MM",
    "month_day": "MM-dd",
    "year": "yyyy",
    "date_time": "yyyy-MM-dd HH:mm",
    "time": "HH:mm",
}


def date_pattern(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    # Common date-only spellings are unambiguous; elsewhere `mm` means minutes.
    if value in {"yyyymmdd", "yyyy-mm-dd", "yyyy/mm/dd", "yyyy.mm.dd"}:
        value = value.replace("mm", "MM")
    elif re.search(r"(?:yyyy|yy)[/.-]?mm[/.-]?dd", value):
        return None
    tokens = list(_TOKEN.finditer(value))
    if not tokens or "".join(token.group() for token in tokens) != value:
        return None
    if not any(token.group()[0].isalpha() for token in tokens):
        return None
    return value


def date_pattern_issue(pattern: str, rows: list[dict[str, Any]], key: str) -> str | None:
    """Check source precision and distinguishability without changing observations."""
    tokens = [match.group() for match in _TOKEN.finditer(pattern)]
    seen: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        value = row.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            return "A date value is not an inspectable calendar string."
        year_only = re.fullmatch(r"(\d{4})年?", value)
        compact_date = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", value)
        match = _CALENDAR.fullmatch(value)
        if year_only:
            year, month, day, hour, minute, second, fraction, zone = int(year_only[1]), None, None, None, None, None, None, ""
        elif compact_date:
            year, month, day = (int(part) for part in compact_date.groups())
            hour, minute, second, fraction, zone = None, None, None, None, ""
            if year < 100:
                return "A date value is outside the supported calendar range."
        elif match:
            year, month, day, hour, minute, second = (int(part) if part is not None else None for part in match.group(1, 2, 3, 4, 5, 6))
            fraction, zone = match[7], match[8] or ""
            if year < 100:
                return "A date value is outside the supported calendar range."
        else:
            return "A date value is invalid or its calendar meaning is unavailable."
        try:
            datetime(year, month if month is not None else 1, day if day is not None else 1, hour or 0, minute or 0, second or 0)
        except ValueError:
            return "A date value is invalid or its calendar meaning is unavailable."
        if zone and zone != "Z":
            digits = zone[1:].replace(":", "")
            if int(digits[:2]) > 23 or int(digits[2:]) > 59:
                return "A date value has an invalid UTC offset."
            zone = f"{zone[0]}{digits[:2]}:{digits[2:]}"
        parts = {
            "yyyy": f"{year:04d}",
            "yy": f"{year % 100:02d}",
            "MM": f"{month:02d}" if month is not None else None,
            "dd": f"{day:02d}" if day is not None else None,
            "HH": f"{hour:02d}" if hour is not None else None,
            "mm": f"{minute:02d}" if minute is not None else None,
            "ss": f"{second:02d}" if second is not None else None,
            "XXX": zone or None,
        }
        if any(token in parts and parts[token] is None for token in tokens):
            return "The requested pattern needs date, time or offset details absent from the source."
        label = "".join(parts.get(token, token) for token in tokens)
        identity = (year, month, day, hour, minute, second, fraction, zone)
        if label in seen and seen[label] != identity:
            return "The requested pattern makes distinct source dates indistinguishable."
        seen[label] = identity
    return None


def apply_date_patterns(columns: list[Any], requested: Any, rows: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """Apply valid declarations independently, leaving source values untouched."""
    if requested is None:
        return []
    entries = requested if isinstance(requested, dict) else {"": requested}
    dates = {column.key: column for column in columns if column.type == "date"}
    fallbacks = []
    for key, value in entries.items():
        if value == "auto" and key in dates:
            dates[key].date_pattern = None
            continue
        pattern = date_pattern(DATE_PRESETS.get(value, value) if isinstance(value, str) else value)
        issue = date_pattern_issue(pattern, rows or [], key) if key in dates and pattern is not None else None
        if key not in dates or pattern is None or issue:
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            fallbacks.append(
                {
                    "code": "invalid_date_pattern",
                    "path": "/column_date_formats" + ("/" + escaped if escaped else ""),
                    "message": (issue or "Use an exact date column key with a supported preset or pattern.") + " Default formatting was retained.",
                }
            )
        else:
            dates[key].date_pattern = pattern
    return fallbacks
