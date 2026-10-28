"""Versioned, trusted print-chrome theme for durable Canvas exports."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Final

from bowhead_core.theme_compiler import product_default_compiled_scheme

# "v2" (post-1.5.0): the accent follows the compiled product default color (SaaS Blue since
# 1.5.0) instead of the hand-picked terracotta "#8E3F27" of "v1".
# "v3": every token is copied from the compiled light scheme (cool neutrals, the full accent
# family and the warning pair), so the native PDF/PPTX layouts take all their colors from the
# snapshot instead of their own fixed palettes. Snapshots frozen under an older recipe no
# longer validate, so export jobs created before the upgrade fail closed and the user exports
# again.
PRINT_THEME_RECIPE_VERSION: Final = "bowhead-print-theme-v3"
PRINT_THEME_FONT_BUNDLE_VERSION: Final = "bowhead-system-fonts-v1"
# Exported paper is always white, whatever the team theme: the PDF page, the export-only visual
# frame and the chart surface its PNGs are projected onto. Team colors reach cards, table
# headers, callouts and accents only. Mirrored by PRINT_PAPER in lib/print-theme.ts.
PRINT_PAPER: Final = "#FFFFFF"
# Copied verbatim from the compiled light scheme; the names are the compiler's token keys.
PRINT_THEME_TOKEN_FIELDS: Final = (
    "background",
    "surface",
    "surface2",
    "surface3",
    "line",
    "line2",
    "ink",
    "ink2",
    "ink3",
    "ink4",
    "accent",
    "accentHover",
    "accentInk",
    "accentSoft",
    "accentTint",
    "warning",
    "warningSoft",
)
_HEX_RE: Final = re.compile(r"#[0-9A-F]{6}")
_SNAPSHOT_FIELDS: Final = frozenset(
    {
        "schemaVersion",
        "recipeVersion",
        "mode",
        "tokens",
        "fontBundleVersion",
        "checksum",
    }
)


class PrintThemeError(ValueError):
    """Raised when an export print-theme snapshot is incomplete or untrusted."""


def print_theme_tokens_from_compiled_light(light_scheme: Mapping[str, Any]) -> dict[str, str]:
    """Copy the print tokens from a compiled light scheme without picking any value by hand."""

    tokens: dict[str, str] = {}
    for field in PRINT_THEME_TOKEN_FIELDS:
        value = light_scheme.get(field)
        if not isinstance(value, str) or _HEX_RE.fullmatch(value.upper()) is None:
            raise PrintThemeError(f"compiled light scheme token {field} is invalid")
        tokens[field] = value.upper()
    return tokens


def build_print_theme_snapshot(light_scheme: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze a checksummed print-theme snapshot from a compiled light scheme."""

    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "recipeVersion": PRINT_THEME_RECIPE_VERSION,
        "mode": "light",
        "tokens": print_theme_tokens_from_compiled_light(light_scheme),
        "fontBundleVersion": PRINT_THEME_FONT_BUNDLE_VERSION,
    }
    return {**payload, "checksum": _checksum(payload)}


def _checksum(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


# Print chrome is always light. Every token comes from the same compiled product default that
# every team without an explicit color choice renders, so the export frame cannot drift from
# the product look. Contrast between tokens is not assumed here: the native renderers resolve
# every text/background pair at render time (``export_native_palette``).
PRINT_THEME_SNAPSHOT: Final = build_print_theme_snapshot(product_default_compiled_scheme()["light"])


def current_print_theme_snapshot() -> dict[str, Any]:
    """Return a detached snapshot suitable for immutable job persistence."""

    return deepcopy(PRINT_THEME_SNAPSHOT)


def parse_print_theme_snapshot(value: Any) -> dict[str, Any]:
    """Validate a frozen snapshot of the recipe supported by this renderer generation.

    Any well-formed v3 snapshot whose checksum matches its own content is accepted: the
    product default and a team-brand snapshot built by ``build_print_theme_snapshot`` alike.
    Trust comes from the server alone freezing snapshots into export jobs. The checksum is an
    unkeyed content digest, not a signature: anyone can recompute it, so it does not prove
    origin. It keys the visual identity hash (cached receipts never mix themes) and catches a
    snapshot whose tokens no longer match its own digest. Older recipes, unknown fields and
    malformed tokens also fail closed.
    """

    if not isinstance(value, dict) or set(value) != _SNAPSHOT_FIELDS:
        raise PrintThemeError("print theme snapshot fields are invalid")
    tokens = value.get("tokens")
    payload = {key: child for key, child in value.items() if key != "checksum"}
    if (
        value.get("schemaVersion") != 1
        or value.get("recipeVersion") != PRINT_THEME_RECIPE_VERSION
        or value.get("mode") != "light"
        or value.get("fontBundleVersion") != PRINT_THEME_FONT_BUNDLE_VERSION
        or not isinstance(tokens, dict)
        or set(tokens) != set(PRINT_THEME_TOKEN_FIELDS)
        or any(not isinstance(tokens[key], str) or _HEX_RE.fullmatch(tokens[key]) is None for key in PRINT_THEME_TOKEN_FIELDS)
        or value.get("checksum") != _checksum(payload)
    ):
        raise PrintThemeError("print theme snapshot is invalid")
    return deepcopy(value)


__all__ = [
    "PRINT_PAPER",
    "PRINT_THEME_FONT_BUNDLE_VERSION",
    "PRINT_THEME_RECIPE_VERSION",
    "PRINT_THEME_SNAPSHOT",
    "PRINT_THEME_TOKEN_FIELDS",
    "PrintThemeError",
    "build_print_theme_snapshot",
    "current_print_theme_snapshot",
    "parse_print_theme_snapshot",
    "print_theme_tokens_from_compiled_light",
]
