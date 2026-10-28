"""Frozen M1 color-profile snapshots used by V2 lazy ownership transfer.

These values mirror the canonical ``--c-*`` variables in frontend
``app/globals.css``.  They are intentionally independent from the V2
generator: creating V2 state must not change an existing team's visuals.
"""

from __future__ import annotations

import copy
from typing import Any, Final

from bowhead_core.theme_compiler import checksum_compiled_scheme

LEGACY_M1_PROFILE_IDS: Final = frozenset({"bowhead", "ocean", "forest", "plum", "graphite"})

_LIGHT_BASE: Final = {
    "background": "#FAF9F6",
    "surface": "#FFFFFF",
    "surface2": "#F3EEE2",
    "surface3": "#E8E5DD",
    "line": "#E8E2D4",
    "line2": "#F0EADD",
    "ink": "#161310",
    "ink2": "#3A342C",
    "ink3": "#56503F",
    # UI redesign P0-1: ink4 carries actual text, requiring body contrast 4.5:1 (old #8A8170 = 3.66:1);
    # warning also carries text (old #A98028 = 3.44:1).
    "ink4": "#7B7262",
    "positive": "#3A724A",
    "positiveSoft": "#E7EFE0",
    "warning": "#956C06",
    "warningSoft": "#F6EBCF",
    "error": "#9F2438",
    "errorSoft": "#F5DADF",
    "info": "#3F6F8C",
    "infoSoft": "#DEEAF1",
}
_DARK_BASE: Final = {
    "background": "#131210",
    "surface": "#1B1A17",
    "surface2": "#211F1B",
    "surface3": "#2B2925",
    "line": "#2A2824",
    "line2": "#393631",
    "ink": "#ECEAE3",
    "ink2": "#A8A49B",
    # feedback-triage-2026-08-24 batch 5: these two levels and `app/globals.css` `[data-theme="dark"]`
    # are **two copies of the same handwritten ladder** and must match exactly (`test_theme_ink_ladder_contrast.py`
    # guards this). Old ink3 #7E7A71 = 4.38:1, ink4 #4F4C46 = 2.19:1,
    # both below their thresholds; legacy palettes **skip repair**, so correct them directly here.
    # UI redesign P0-1: raise ink4 to body contrast 4.5:1 and brighten ink3 too, preserving ≥1.15× separation between adjacent levels.
    "ink3": "#959188",
    "ink4": "#86827B",
    "positive": "#62C491",
    "positiveSoft": "#10291F",
    "warning": "#E5B65A",
    "warningSoft": "#30240D",
    "error": "#F58B82",
    "errorSoft": "#351714",
    "info": "#8BB7FF",
    "infoSoft": "#14243E",
}
_PRE_FIX_DARK_STATUS: Final = {
    "positive": "#3A724A",
    "positiveSoft": "#E7EFE0",
    "warning": "#A98028",
    "warningSoft": "#F6EBCF",
    "error": "#9F2438",
    "errorSoft": "#F5DADF",
    "info": "#3F6F8C",
    "infoSoft": "#DEEAF1",
}
_ACCENTS: Final = {
    "bowhead": {
        "light": ("#B04A2A", "#8F3A1F", "#FBFBF7", "#F5E6DC", "#FAF0E8"),
        "dark": ("#62B89C", "#7AC9AD", "#131210", "#1F2D27", "#1A221E"),
    },
    "ocean": {
        "light": ("#2F6F84", "#245769", "#FBFBF7", "#E1EEF2", "#F0F7F8"),
        "dark": ("#75B9CB", "#90CEDE", "#101719", "#19313A", "#15242A"),
    },
    "forest": {
        "light": ("#4B6F44", "#385633", "#FBFBF7", "#E5EFDF", "#F2F8ED"),
        "dark": ("#91C27F", "#ACD995", "#101710", "#21311F", "#1A2419"),
    },
    "plum": {
        "light": ("#7A527C", "#5F3E62", "#FBFBF7", "#EFE2F0", "#F8EFF8"),
        "dark": ("#C197C6", "#D8AFD9", "#181119", "#302236", "#251B2A"),
    },
    "graphite": {
        "light": ("#4F5B5C", "#3D4748", "#FBFBF7", "#E4E8E6", "#F3F5F3"),
        "dark": ("#AEBBBB", "#C4D0D0", "#111314", "#2A3030", "#202525"),
    },
}


def legacy_m1_compiled_scheme(profile_id: str) -> dict[str, Any]:
    """Return a deep-copied compiled snapshot for an existing M1 profile."""

    if profile_id not in LEGACY_M1_PROFILE_IDS:
        raise ValueError(f"unknown legacy M1 profile: {profile_id}")
    light = _legacy_mode(_LIGHT_BASE, _ACCENTS[profile_id]["light"])
    dark = _legacy_mode(_DARK_BASE, _ACCENTS[profile_id]["dark"])
    compiled: dict[str, Any] = {
        "tokenSchemaVersion": 2,
        "generator": {"id": "legacy-m1-css", "version": "1"},
        "light": light,
        "dark": dark,
        "validation": {
            "status": "legacy",
            "blockingCount": 0,
            "warningCount": 0,
            "blocking": [],
            "warnings": [],
        },
    }
    compiled["checksum"] = checksum_compiled_scheme(compiled)
    return copy.deepcopy(compiled)


def is_pre_fix_legacy_m1_compiled_scheme(compiled: dict[str, Any]) -> bool:
    """Return whether a retained M1 snapshot has the pre-fix Dark status pair."""

    generator = compiled.get("generator")
    dark = compiled.get("dark")
    return (
        generator == {"id": "legacy-m1-css", "version": "1"}
        and isinstance(dark, dict)
        and all(dark.get(token) == value for token, value in _PRE_FIX_DARK_STATUS.items())
    )


def _legacy_mode(
    base: dict[str, str],
    accent_values: tuple[str, str, str, str, str],
) -> dict[str, str]:
    accent, accent_hover, accent_ink, accent_soft, accent_tint = accent_values
    return {
        **{key: base[key] for key in ("background", "surface", "surface2", "surface3", "line", "line2", "ink", "ink2", "ink3", "ink4")},
        "accent": accent,
        "accentHover": accent_hover,
        "accentInk": accent_ink,
        "accentSoft": accent_soft,
        "accentTint": accent_tint,
        "focus": accent,
        **{key: base[key] for key in ("positive", "positiveSoft", "warning", "warningSoft", "error", "errorSoft", "info", "infoSoft")},
    }


__all__ = [
    "LEGACY_M1_PROFILE_IDS",
    "is_pre_fix_legacy_m1_compiled_scheme",
    "legacy_m1_compiled_scheme",
]
