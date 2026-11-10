"""Bounded palette or exact HEX selection for directly bound Cartesian measures."""

import re
from colorsys import rgb_to_hls
from typing import Any


def editable_color_columns(spec: dict[str, Any]) -> list[dict[str, Any]]:
    if spec.get("kind") not in {"line", "bar", "hbar", "stacked_bar", "area_stacked", "combo", "dual_axis"}:
        return []
    if spec.get("views") or spec.get("presentation_plan") or spec.get("_export_analytical"):
        return []
    columns = [c for c in spec.get("columns", []) if isinstance(c, dict)]
    explicit = spec.get("explicit_presentation")
    if explicit:
        if explicit.get("mode") != "plot" or explicit.get("group_by"):
            return []
        keys = explicit.get("axes", {}).get("y", [])
        columns = [c for c in columns if c.get("key") in keys]
    elif len([c for c in columns if c.get("type") in {"date", "categorical", "geo"}]) > 1:
        return []
    return [c for c in columns if c.get("type") == "numeric" and isinstance(c.get("key"), str)]


def color_index(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value < 16 else None


def color_hex(value: Any) -> str | None:
    """Canonical opaque HEX only; never accept CSS expressions or discard alpha."""
    if not isinstance(value, str) or re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value) is None:
        return None
    digits = value[1:]
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    return "#" + digits.upper()


def palette_color_family(hex_color: str) -> str:
    """Describe a validated slot for model selection; never generate or adjust paint."""
    hue, _, saturation = rgb_to_hls(*(int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)))
    if saturation < 0.12:
        return "neutral"
    degrees = hue * 360
    for ceiling, name in [(20, "red"), (45, "orange"), (75, "yellow"), (165, "green"), (195, "teal"), (260, "blue"), (320, "purple"), (345, "pink")]:
        if degrees < ceiling:
            return name
    return "red"
