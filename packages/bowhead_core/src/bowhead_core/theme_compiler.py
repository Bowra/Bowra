"""Deterministic Bowhead team-branding color compiler.

The compiler accepts a small, versioned source document and produces complete
Light and Dark semantic token maps.  Published revisions persist the compiled
snapshot, so changing this module never mutates an existing team's visuals.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from functools import cache
from typing import Any, Final

GENERATOR_ID: Final = "bowhead-oklch"
GENERATOR_VERSION: Final = "1"
TOKEN_SCHEMA_VERSION: Final = 2

NEUTRAL_LIGHT: Final = (0.995, 0.975, 0.950, 0.920, 0.860, 0.780, 0.620, 0.480, 0.340, 0.180)
NEUTRAL_DARK: Final = (0.060, 0.100, 0.140, 0.180, 0.220, 0.300, 0.420, 0.580, 0.740, 0.920)
ACCENT: Final = (0.980, 0.940, 0.880, 0.780, 0.680, 0.580, 0.500, 0.420, 0.340, 0.240, 0.160)

SCHEME_TOKEN_KEYS: Final = (
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
    "focus",
    "positive",
    "positiveSoft",
    "warning",
    "warningSoft",
    "error",
    "errorSoft",
    "info",
    "infoSoft",
)

_HEX_COLOR_RE: Final = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CONFIGURATION_TYPES: Final = frozenset({"bowhead", "preset", "custom"})
_BACKGROUND_STYLES: Final = frozenset({"warm", "cool", "neutral", "custom"})
_OVERRIDE_FIELDS: Final = frozenset({"background", "surface"})
_SOURCE_FIELDS: Final = frozenset(
    {
        "version",
        "configurationType",
        "presetId",
        "primarySeed",
        "neutralSeed",
        "backgroundStyle",
        "overrides",
    }
)

_PRESETS: Final[dict[str, dict[str, str]]] = {
    "bowhead": {
        "primarySeed": "#B04A2A",
        "neutralSeed": "#8A8170",
        "backgroundStyle": "warm",
    },
    "ocean": {
        "primarySeed": "#2F6F84",
        "neutralSeed": "#667780",
        "backgroundStyle": "cool",
    },
    "forest": {
        "primarySeed": "#4B6F44",
        "neutralSeed": "#6C7568",
        "backgroundStyle": "neutral",
    },
    "plum": {
        "primarySeed": "#7A527C",
        "neutralSeed": "#756B76",
        "backgroundStyle": "neutral",
    },
    "graphite": {
        "primarySeed": "#4F5B5C",
        "neutralSeed": "#62696A",
        "backgroundStyle": "cool",
    },
}

# The canonical product look that ``configurationType: "bowhead"`` compiles to and that every
# team without an explicit color choice renders.  Since 1.5.0 it is the SaaS Blue palette
# (``team_theme_library`` built-in ``saas-blue``); the terracotta seeds above stay available
# as the legacy-compatible ``preset``/``bowhead`` entry.  Published revisions keep their
# stored snapshot, so changing these seeds only reaches drafts compiled afterwards.
PRODUCT_DEFAULT_THEME_SEEDS: Final[dict[str, Any]] = {
    "primarySeed": "#2563EB",
    "neutralSeed": "#64748B",
    "backgroundStyle": "custom",
    "overrides": {
        "light": {"background": "#F4F7FB", "surface": "#FFFFFF"},
        "dark": {"background": "#0F172A", "surface": "#172033"},
    },
}
PRODUCT_DEFAULT_THEME_SOURCE: Final[dict[str, Any]] = {
    "version": 2,
    "configurationType": "bowhead",
    "presetId": None,
}
# Hue used when a custom primary seed is nearly achromatic.  Pinned to the historical
# terracotta seed so the product default switch never changes custom compilations.
_ACHROMATIC_FALLBACK_HUE_SEED: Final = "#B04A2A"

_BACKGROUND_SEEDS: Final = {
    "warm": "#8A8170",
    "cool": "#667780",
    "neutral": "#707070",
}

_STATUS_TOKENS: Final = {
    "light": {
        "positive": "#16794A",
        "positiveSoft": "#DDF5E8",
        "warning": "#8A5A00",
        "warningSoft": "#FFF0C2",
        "error": "#B42318",
        "errorSoft": "#FEE4E2",
        "info": "#175CD3",
        "infoSoft": "#E5EFFF",
    },
    "dark": {
        "positive": "#62C491",
        "positiveSoft": "#10291F",
        "warning": "#E5B65A",
        "warningSoft": "#30240D",
        "error": "#F58B82",
        "errorSoft": "#351714",
        "info": "#8BB7FF",
        "infoSoft": "#14243E",
    },
}


class ThemeCompilerError(ValueError):
    """Raised when a V2 theme source cannot be normalized or compiled."""


def canonical_json(value: Any) -> str:
    """Serialize a JSON value using the checksum contract."""

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def normalize_theme_source(source: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize an administrator-authored V2 color source."""

    if not isinstance(source, dict):
        raise ThemeCompilerError("theme source must be an object")
    unknown = sorted(set(source) - _SOURCE_FIELDS)
    if unknown:
        raise ThemeCompilerError(f"theme source has unknown field(s): {', '.join(unknown)}")
    if type(source.get("version")) is not int or source["version"] != 2:
        raise ThemeCompilerError("theme source.version must be 2")

    configuration_type = source.get("configurationType")
    if configuration_type not in _CONFIGURATION_TYPES:
        raise ThemeCompilerError("configurationType must be bowhead, preset, or custom")

    if configuration_type == "bowhead":
        _require_absent_or_null(source, "presetId")
        _require_absent(source, "primarySeed")
        _require_absent(source, "neutralSeed")
        _require_absent(source, "backgroundStyle")
        _require_absent(source, "overrides")
        return {
            "version": 2,
            "configurationType": "bowhead",
            "presetId": None,
        }

    if configuration_type == "preset":
        preset_id = source.get("presetId")
        if not isinstance(preset_id, str) or preset_id not in _PRESETS:
            raise ThemeCompilerError(f"presetId must be one of: {', '.join(sorted(_PRESETS))}")
        _require_absent(source, "primarySeed")
        _require_absent(source, "neutralSeed")
        _require_absent(source, "backgroundStyle")
        _require_absent(source, "overrides")
        return {
            "version": 2,
            "configurationType": "preset",
            "presetId": preset_id,
        }

    _require_absent_or_null(source, "presetId")
    primary_seed = _normalize_hex(source.get("primarySeed"), "primarySeed")
    neutral_seed = _normalize_hex(source.get("neutralSeed"), "neutralSeed")
    background_style = source.get("backgroundStyle")
    if background_style not in _BACKGROUND_STYLES:
        raise ThemeCompilerError("backgroundStyle must be warm, cool, neutral, or custom")
    overrides = _normalize_overrides(source.get("overrides", {}))
    if background_style == "custom" and not any(overrides[mode] for mode in ("light", "dark")):
        raise ThemeCompilerError("backgroundStyle=custom requires at least one override")

    return {
        "version": 2,
        "configurationType": "custom",
        "presetId": None,
        "primarySeed": primary_seed,
        "neutralSeed": neutral_seed,
        "backgroundStyle": background_style,
        "overrides": overrides,
    }


def compile_theme_scheme(source: dict[str, Any]) -> dict[str, Any]:
    """Compile a normalized V2 source into complete Light and Dark token maps."""

    normalized = normalize_theme_source(source)
    resolved = _resolve_source(normalized)
    primary = hex_to_oklch(resolved["primarySeed"])
    neutral = hex_to_oklch(resolved["neutralSeed"])
    warnings: list[dict[str, str]] = []

    accent_hue = primary[2]
    accent_chroma = min(0.24, max(0.06, primary[1]))
    if primary[1] < 0.03:
        accent_hue = neutral[2] if neutral[1] >= 0.01 else hex_to_oklch(_ACHROMATIC_FALLBACK_HUE_SEED)[2]
        warnings.append(
            {
                "code": "unstable_primary_hue",
                "path": "primarySeed",
                "message": "Primary seed is nearly achromatic; a stable fallback hue was used.",
            }
        )

    background_seed = resolved["neutralSeed"] if resolved["backgroundStyle"] == "custom" else _BACKGROUND_SEEDS[resolved["backgroundStyle"]]
    background_oklch = hex_to_oklch(background_seed)
    neutral_hue = neutral[2] if neutral[1] >= 0.01 else background_oklch[2]
    neutral_chroma = min(0.04, max(neutral[1], background_oklch[1] * 0.5))

    light_neutral = [_oklch_hex(lightness, neutral_chroma, neutral_hue) for lightness in NEUTRAL_LIGHT]
    dark_neutral = [_oklch_hex(lightness, neutral_chroma, neutral_hue) for lightness in NEUTRAL_DARK]
    accent = [_oklch_hex(lightness, accent_chroma, accent_hue) for lightness in ACCENT]

    light = _light_scheme(light_neutral, accent)
    dark = _dark_scheme(dark_neutral, accent)
    for mode, scheme in (("light", light), ("dark", dark)):
        scheme.update(_STATUS_TOKENS[mode])
        for field, value in resolved["overrides"][mode].items():
            scheme[field] = value
        _assert_complete_scheme(scheme, mode=mode)

    compiled: dict[str, Any] = {
        "tokenSchemaVersion": TOKEN_SCHEMA_VERSION,
        "generator": {"id": GENERATOR_ID, "version": GENERATOR_VERSION},
        "light": light,
        "dark": dark,
        "validation": {
            "blocking": [],
            "warnings": warnings,
        },
    }
    from bowhead_core.theme_validation import repair_and_validate_theme

    compiled = repair_and_validate_theme(normalized, compiled)
    compiled["checksum"] = checksum_compiled_scheme(compiled)
    return compiled


def product_default_compiled_scheme() -> dict[str, Any]:
    """Return the compiled scheme every team without an explicit color choice renders."""

    return deepcopy(_product_default_compiled_scheme())


@cache
def _product_default_compiled_scheme() -> dict[str, Any]:
    return compile_theme_scheme(PRODUCT_DEFAULT_THEME_SOURCE)


def checksum_compiled_scheme(compiled: dict[str, Any]) -> str:
    """Return the canonical digest, excluding the checksum field itself."""

    payload = {key: value for key, value in compiled.items() if key != "checksum"}
    encoded = canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def hex_to_oklch(value: str) -> tuple[float, float, float]:
    """Convert an ``#RRGGBB`` sRGB color to OKLCH."""

    normalized = _normalize_hex(value, "color")
    red = _srgb_to_linear(int(normalized[1:3], 16) / 255)
    green = _srgb_to_linear(int(normalized[3:5], 16) / 255)
    blue = _srgb_to_linear(int(normalized[5:7], 16) / 255)
    lightness, axis_a, axis_b = _linear_srgb_to_oklab(red, green, blue)
    chroma = math.hypot(axis_a, axis_b)
    hue = math.degrees(math.atan2(axis_b, axis_a)) % 360 if chroma > 1e-12 else 0.0
    return (lightness, chroma, hue)


def oklch_to_hex(lightness: float, chroma: float, hue: float) -> str:
    """Convert OKLCH to a gamut-mapped uppercase ``#RRGGBB`` value."""

    return _oklch_hex(lightness, chroma, hue)


def _normalize_overrides(raw: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        raise ThemeCompilerError("overrides must be an object")
    unknown_modes = sorted(set(raw) - {"light", "dark"})
    if unknown_modes:
        raise ThemeCompilerError(f"overrides has unknown mode(s): {', '.join(unknown_modes)}")

    normalized = {"light": {}, "dark": {}}
    for mode in ("light", "dark"):
        mode_value = raw.get(mode, {})
        if not isinstance(mode_value, dict):
            raise ThemeCompilerError(f"overrides.{mode} must be an object")
        unknown_fields = sorted(set(mode_value) - _OVERRIDE_FIELDS)
        if unknown_fields:
            raise ThemeCompilerError(f"overrides.{mode} has unknown field(s): {', '.join(unknown_fields)}")
        for field in ("background", "surface"):
            if field in mode_value:
                normalized[mode][field] = _normalize_hex(mode_value[field], f"overrides.{mode}.{field}")
    return normalized


def _resolve_source(source: dict[str, Any]) -> dict[str, Any]:
    configuration_type = source["configurationType"]
    if configuration_type == "bowhead":
        return deepcopy(PRODUCT_DEFAULT_THEME_SEEDS)
    if configuration_type == "preset":
        preset = _PRESETS[source["presetId"]]
        return {**preset, "overrides": {"light": {}, "dark": {}}}
    return {
        "primarySeed": source["primarySeed"],
        "neutralSeed": source["neutralSeed"],
        "backgroundStyle": source["backgroundStyle"],
        "overrides": source["overrides"],
    }


def _light_scheme(neutral: list[str], accent: list[str]) -> dict[str, str]:
    return {
        "background": neutral[1],
        "surface": neutral[0],
        "surface2": neutral[2],
        "surface3": neutral[3],
        "line": neutral[6],
        "line2": neutral[5],
        "ink": neutral[9],
        "ink2": neutral[8],
        "ink3": neutral[7],
        "ink4": neutral[6],
        "accent": accent[6],
        "accentHover": accent[7],
        "accentInk": "#FFFFFF",
        "accentSoft": accent[1],
        "accentTint": accent[0],
        "focus": accent[6],
    }


def _dark_scheme(neutral: list[str], accent: list[str]) -> dict[str, str]:
    return {
        "background": neutral[0],
        "surface": neutral[1],
        "surface2": neutral[2],
        "surface3": neutral[3],
        "line": neutral[5],
        "line2": neutral[6],
        "ink": neutral[9],
        "ink2": neutral[8],
        "ink3": neutral[7],
        "ink4": neutral[6],
        "accent": accent[4],
        "accentHover": accent[3],
        "accentInk": neutral[0],
        "accentSoft": _oklch_hex(NEUTRAL_DARK[4], min(0.06, hex_to_oklch(accent[4])[1] * 0.45), hex_to_oklch(accent[4])[2]),
        "accentTint": _oklch_hex(NEUTRAL_DARK[3], min(0.04, hex_to_oklch(accent[4])[1] * 0.35), hex_to_oklch(accent[4])[2]),
        "focus": accent[4],
    }


def _assert_complete_scheme(scheme: dict[str, str], *, mode: str) -> None:
    actual = set(scheme)
    expected = set(SCHEME_TOKEN_KEYS)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ThemeCompilerError(f"{mode} scheme key mismatch; missing={missing}, extra={extra}")
    for key, value in scheme.items():
        if not _HEX_COLOR_RE.fullmatch(value):
            raise ThemeCompilerError(f"{mode}.{key} must compile to #RRGGBB")


def _normalize_hex(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX_COLOR_RE.fullmatch(value):
        raise ThemeCompilerError(f"{label} must be an opaque #RRGGBB color")
    return value.upper()


def _require_absent(source: dict[str, Any], field: str) -> None:
    if field in source:
        raise ThemeCompilerError(f"{field} is not allowed for {source['configurationType']} configuration")


def _require_absent_or_null(source: dict[str, Any], field: str) -> None:
    if source.get(field) is not None:
        raise ThemeCompilerError(f"{field} must be null for {source['configurationType']} configuration")


def _srgb_to_linear(value: float) -> float:
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(value: float) -> float:
    if value <= 0.0031308:
        return 12.92 * value
    return 1.055 * (value ** (1 / 2.4)) - 0.055


def _linear_srgb_to_oklab(red: float, green: float, blue: float) -> tuple[float, float, float]:
    long = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    medium = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    short = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    long_root = math.copysign(abs(long) ** (1 / 3), long)
    medium_root = math.copysign(abs(medium) ** (1 / 3), medium)
    short_root = math.copysign(abs(short) ** (1 / 3), short)
    return (
        0.2104542553 * long_root + 0.7936177850 * medium_root - 0.0040720468 * short_root,
        1.9779984951 * long_root - 2.4285922050 * medium_root + 0.4505937099 * short_root,
        0.0259040371 * long_root + 0.7827717662 * medium_root - 0.8086757660 * short_root,
    )


def _oklab_to_linear_srgb(lightness: float, axis_a: float, axis_b: float) -> tuple[float, float, float]:
    long_root = lightness + 0.3963377774 * axis_a + 0.2158037573 * axis_b
    medium_root = lightness - 0.1055613458 * axis_a - 0.0638541728 * axis_b
    short_root = lightness - 0.0894841775 * axis_a - 1.2914855480 * axis_b
    long = long_root**3
    medium = medium_root**3
    short = short_root**3
    return (
        4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
        -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
        -0.0041960863 * long - 0.7034186147 * medium + 1.7076147010 * short,
    )


def _oklch_linear_srgb(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    radians = math.radians(hue)
    return _oklab_to_linear_srgb(lightness, chroma * math.cos(radians), chroma * math.sin(radians))


def _in_gamut(rgb: tuple[float, float, float]) -> bool:
    epsilon = 1e-9
    return all(-epsilon <= channel <= 1 + epsilon for channel in rgb)


def _oklch_hex(lightness: float, chroma: float, hue: float) -> str:
    lightness = min(1.0, max(0.0, lightness))
    chroma = max(0.0, chroma)
    rgb = _oklch_linear_srgb(lightness, chroma, hue)
    if not _in_gamut(rgb):
        low = 0.0
        high = chroma
        while high - low > 0.0001:
            candidate = (low + high) / 2
            if _in_gamut(_oklch_linear_srgb(lightness, candidate, hue)):
                low = candidate
            else:
                high = candidate
        rgb = _oklch_linear_srgb(lightness, low, hue)
    channels = [round(min(1.0, max(0.0, _linear_to_srgb(channel))) * 255) for channel in rgb]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


__all__ = [
    "ACCENT",
    "GENERATOR_ID",
    "GENERATOR_VERSION",
    "NEUTRAL_DARK",
    "NEUTRAL_LIGHT",
    "PRODUCT_DEFAULT_THEME_SEEDS",
    "PRODUCT_DEFAULT_THEME_SOURCE",
    "SCHEME_TOKEN_KEYS",
    "TOKEN_SCHEMA_VERSION",
    "ThemeCompilerError",
    "canonical_json",
    "checksum_compiled_scheme",
    "compile_theme_scheme",
    "hex_to_oklch",
    "normalize_theme_source",
    "oklch_to_hex",
    "product_default_compiled_scheme",
]
