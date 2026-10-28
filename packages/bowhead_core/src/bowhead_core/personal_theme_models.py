"""Strict persisted models for personal appearance V3."""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from typing import Any, Final

from bowhead_core.theme_config import (
    DENSITY_VALUES,
    MOTION_VALUES,
    RADIUS_VALUES,
    THEME_MODE_VALUES,
    TYPE_SCALE_VALUES,
    TYPOGRAPHY_PROFILE_IDS,
)

KEY_APPEARANCE_THEME_CONFIG_V3: Final = "appearance_theme_config_v3"
KEY_APPEARANCE_THEME_UNDO_V3: Final = "appearance_theme_undo_v3"
MAX_PERSONAL_THEME_CONFIG_BYTES: Final = 4096
MAX_PERSONAL_THEME_UNDO_BYTES: Final = 8192
PERSONAL_THEME_SCHEMA_VERSION: Final = 3
PERSONAL_THEME_REGISTRY_VERSION: Final = 1

RESERVED_USER_SETTING_KEYS: Final = frozenset({KEY_APPEARANCE_THEME_CONFIG_V3, KEY_APPEARANCE_THEME_UNDO_V3})

PERSONAL_ACCENT_REGISTRY_V1: Final[dict[str, str]] = {
    "bowhead": "#B04A2A",
    "ocean": "#2F6F84",
    "forest": "#4B6F44",
    "plum": "#7A527C",
    "graphite": "#4F5B5C",
}
PERSONAL_BACKGROUND_REGISTRY_V1: Final[dict[str, dict[str, str]]] = {
    "warm-paper": {"light": "#F7F2E8", "dark": "#17130F"},
    "pure-white": {"light": "#FFFFFF", "dark": "#121212"},
    "cool-gray": {"light": "#F4F7F8", "dark": "#11161A"},
}
PERSONAL_PALETTE_REGISTRY_V1: Final[dict[str, dict[str, Any]]] = {
    "saas-blue": {
        "accent": "#2563EB",
        "background": {"light": "#F8FAFC", "dark": "#0F172A"},
    },
    "bowhead-editorial": {
        "accent": "#B04A2A",
        "background": {"light": "#F7F2E8", "dark": "#17130F"},
    },
    "product-indigo": {
        "accent": "#635BFF",
        "background": {"light": "#F6F9FC", "dark": "#101323"},
    },
    "collaboration-teal": {
        "accent": "#0F766E",
        "background": {"light": "#F0FDFA", "dark": "#0B1F1D"},
    },
    "focus-violet": {
        "accent": "#7C3AED",
        "background": {"light": "#FAF5FF", "dark": "#171126"},
    },
    "neutral-graphite": {
        "accent": "#4F5B5C",
        "background": {"light": "#F7F7F5", "dark": "#141617"},
    },
}

_TOP_LEVEL_FIELDS: Final = ("version", "rowVersion", "overrides", "origin")
_UNDO_TOP_LEVEL_FIELDS: Final = (
    "version",
    "resetId",
    "idempotencyDigest",
    "restorable",
    "before",
    "beforeChecksum",
    "expectedAfter",
    "expiresAt",
)
_OVERRIDE_FIELDS: Final = (
    "mode",
    "accent",
    "background",
    "typographyProfile",
    "typeScale",
    "density",
    "radius",
    "motion",
)
PERSONAL_THEME_OVERRIDE_FIELDS: Final = frozenset(_OVERRIDE_FIELDS)
_HEX_RE: Final = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CHECKSUM_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_RESET_ID_RE: Final = re.compile(r"^[0-9a-f]{32}$")


class PersonalThemeModelError(ValueError):
    """Raised when a personal V3 document is not canonicalizable."""


def parse_personal_theme_config(raw_json: str) -> dict[str, Any]:
    """Parse a materialized V3 row and return its normalized object."""

    if len(raw_json.encode("utf-8")) > MAX_PERSONAL_THEME_CONFIG_BYTES:
        raise PersonalThemeModelError(f"{KEY_APPEARANCE_THEME_CONFIG_V3} exceeds {MAX_PERSONAL_THEME_CONFIG_BYTES} bytes")
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise PersonalThemeModelError(f"{KEY_APPEARANCE_THEME_CONFIG_V3} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise PersonalThemeModelError(f"{KEY_APPEARANCE_THEME_CONFIG_V3} must be a JSON object")

    _reject_unknown(parsed, _TOP_LEVEL_FIELDS, label=KEY_APPEARANCE_THEME_CONFIG_V3)
    if type(parsed.get("version")) is not int or parsed["version"] != PERSONAL_THEME_SCHEMA_VERSION:
        raise PersonalThemeModelError("personal theme version must be 3")
    if type(parsed.get("rowVersion")) is not int or parsed["rowVersion"] < 1:
        raise PersonalThemeModelError("personal theme rowVersion must be a positive integer")
    if "overrides" not in parsed:
        raise PersonalThemeModelError("personal theme overrides is required")

    normalized: dict[str, Any] = {
        "version": PERSONAL_THEME_SCHEMA_VERSION,
        "rowVersion": parsed["rowVersion"],
        "overrides": normalize_personal_theme_overrides(parsed["overrides"]),
    }
    if "origin" in parsed:
        normalized["origin"] = _normalize_origin(parsed["origin"])
    _assert_canonical_size(normalized)
    return normalized


def normalize_personal_theme_config(raw_json: str) -> str:
    """Return compact stable JSON for a materialized V3 row."""

    return _canonical_json(parse_personal_theme_config(raw_json))


def parse_personal_theme_undo(raw_json: str) -> dict[str, Any]:
    """Parse the short-lived single-use reset receipt."""

    if len(raw_json.encode("utf-8")) > MAX_PERSONAL_THEME_UNDO_BYTES:
        raise PersonalThemeModelError(f"{KEY_APPEARANCE_THEME_UNDO_V3} exceeds {MAX_PERSONAL_THEME_UNDO_BYTES} bytes")
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise PersonalThemeModelError(f"{KEY_APPEARANCE_THEME_UNDO_V3} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise PersonalThemeModelError(f"{KEY_APPEARANCE_THEME_UNDO_V3} must be a JSON object")

    _reject_unknown(parsed, _UNDO_TOP_LEVEL_FIELDS, label=KEY_APPEARANCE_THEME_UNDO_V3)
    if type(parsed.get("version")) is not int or parsed["version"] != 1:
        raise PersonalThemeModelError("personal theme undo version must be 1")
    reset_id = parsed.get("resetId")
    if not isinstance(reset_id, str) or not _RESET_ID_RE.fullmatch(reset_id):
        raise PersonalThemeModelError("personal theme undo resetId is invalid")
    idempotency_digest = _normalize_checksum(
        parsed.get("idempotencyDigest"),
        label="personal theme undo idempotencyDigest",
    )
    restorable = parsed.get("restorable")
    if type(restorable) is not bool:
        raise PersonalThemeModelError("personal theme undo restorable must be boolean")

    before_raw = parsed.get("before")
    before_checksum_raw = parsed.get("beforeChecksum")
    if restorable:
        if not isinstance(before_raw, dict) or before_checksum_raw is None:
            raise PersonalThemeModelError("restorable personal theme undo requires before state")
        before = _normalize_undo_before(before_raw)
        before_checksum: str | None = _normalize_checksum(
            before_checksum_raw,
            label="personal theme undo beforeChecksum",
        )
    else:
        if before_raw is not None or before_checksum_raw is not None:
            raise PersonalThemeModelError("non-restorable personal theme undo cannot contain before state")
        before = None
        before_checksum = None

    expected_after_raw = parsed.get("expectedAfter")
    if not isinstance(expected_after_raw, dict):
        raise PersonalThemeModelError("personal theme undo expectedAfter must be an object")
    _reject_unknown(expected_after_raw, ("rowVersion", "checksum"), label="expectedAfter")
    row_version = expected_after_raw.get("rowVersion")
    if type(row_version) is not int or row_version < 1:
        raise PersonalThemeModelError("personal theme undo expectedAfter.rowVersion must be a positive integer")
    expected_checksum = _normalize_checksum(
        expected_after_raw.get("checksum"),
        label="personal theme undo expectedAfter.checksum",
    )

    normalized: dict[str, Any] = {
        "version": 1,
        "resetId": reset_id,
        "idempotencyDigest": idempotency_digest,
        "restorable": restorable,
        "before": before,
        "beforeChecksum": before_checksum,
        "expectedAfter": {
            "rowVersion": row_version,
            "checksum": expected_checksum,
        },
        "expiresAt": _normalize_utc_timestamp(parsed.get("expiresAt")),
    }
    if len(_canonical_json(normalized).encode("utf-8")) > MAX_PERSONAL_THEME_UNDO_BYTES:
        raise PersonalThemeModelError(f"{KEY_APPEARANCE_THEME_UNDO_V3} exceeds {MAX_PERSONAL_THEME_UNDO_BYTES} bytes")
    return normalized


def normalize_personal_theme_undo(raw_json: str) -> str:
    """Return compact stable JSON for a reset receipt."""

    return _canonical_json(parse_personal_theme_undo(raw_json))


def normalize_personal_theme_overrides(raw: Any) -> dict[str, Any]:
    """Validate the sparse editable portion shared by persistence and compile."""

    if not isinstance(raw, dict):
        raise PersonalThemeModelError("personal theme overrides must be an object")
    _reject_unknown(raw, _OVERRIDE_FIELDS, label="overrides")

    normalized: dict[str, Any] = {}
    if "mode" in raw:
        normalized["mode"] = _normalize_enum(raw["mode"], THEME_MODE_VALUES, label="overrides.mode")
    if "accent" in raw:
        normalized["accent"] = _normalize_accent(raw["accent"])
    if "background" in raw:
        normalized["background"] = _normalize_background(raw["background"])
    if "typographyProfile" in raw:
        normalized["typographyProfile"] = _normalize_typography(raw["typographyProfile"])
    if "typeScale" in raw:
        normalized["typeScale"] = _normalize_enum(
            raw["typeScale"],
            TYPE_SCALE_VALUES,
            label="overrides.typeScale",
        )
    if "density" in raw:
        normalized["density"] = _normalize_enum(
            raw["density"],
            DENSITY_VALUES,
            label="overrides.density",
        )
    if "radius" in raw:
        normalized["radius"] = _normalize_enum(
            raw["radius"],
            RADIUS_VALUES,
            label="overrides.radius",
        )
    if "motion" in raw:
        normalized["motion"] = _normalize_enum(
            raw["motion"],
            MOTION_VALUES,
            label="overrides.motion",
        )
    return normalized


def project_legacy_appearance_config(
    legacy_config: dict[str, Any] | None,
    *,
    legacy_checksum: str,
) -> dict[str, Any]:
    """Project validated V1 appearance into an unmaterialized V3 snapshot."""

    if not _CHECKSUM_RE.fullmatch(legacy_checksum):
        raise PersonalThemeModelError("legacy checksum must be a sha256 digest")
    legacy_config = legacy_config or {"version": 1}
    if type(legacy_config.get("version")) is not int or legacy_config["version"] != 1:
        raise PersonalThemeModelError("legacy appearance version must be 1")
    overrides: dict[str, Any] = {}

    if "mode" in legacy_config:
        overrides["mode"] = _normalize_enum(
            legacy_config["mode"],
            THEME_MODE_VALUES,
            label="legacy.mode",
        )
    color_profile = legacy_config.get("colorProfile")
    if color_profile is not None:
        if not isinstance(color_profile, dict) or color_profile.get("type") != "preset":
            raise PersonalThemeModelError("legacy colorProfile must be a preset")
        profile_id = color_profile.get("id")
        if profile_id not in PERSONAL_ACCENT_REGISTRY_V1:
            raise PersonalThemeModelError("legacy colorProfile id is unsupported")
        overrides["accent"] = {
            "kind": "preset",
            "registryVersion": PERSONAL_THEME_REGISTRY_VERSION,
            "id": profile_id,
        }
    for field in ("typographyProfile", "typeScale", "density", "radius", "motion"):
        if field in legacy_config:
            overrides[field] = copy.deepcopy(legacy_config[field])

    normalized_overrides = normalize_personal_theme_overrides(overrides)
    return {
        "version": PERSONAL_THEME_SCHEMA_VERSION,
        "rowVersion": 0,
        "overrides": normalized_overrides,
        "origin": {
            "kind": "legacy_projection",
            "legacyChecksum": legacy_checksum,
        },
    }


def _normalize_accent(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PersonalThemeModelError("overrides.accent must be an object")
    kind = raw.get("kind")
    if kind == "preset":
        _reject_unknown(raw, ("kind", "registryVersion", "id"), label="overrides.accent")
        _require_registry_version(raw.get("registryVersion"), label="overrides.accent")
        accent_id = raw.get("id")
        if not isinstance(accent_id, str) or accent_id not in PERSONAL_ACCENT_REGISTRY_V1:
            raise PersonalThemeModelError("overrides.accent.id is not in registry version 1")
        return {"kind": "preset", "registryVersion": 1, "id": accent_id}
    if kind == "custom":
        _reject_unknown(raw, ("kind", "seed"), label="overrides.accent")
        return {"kind": "custom", "seed": _normalize_hex(raw.get("seed"), label="overrides.accent.seed")}
    raise PersonalThemeModelError("overrides.accent.kind must be preset or custom")


def _normalize_background(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PersonalThemeModelError("overrides.background must be an object")
    kind = raw.get("kind")
    if kind == "preset":
        _reject_unknown(raw, ("kind", "registryVersion", "id"), label="overrides.background")
        _require_registry_version(raw.get("registryVersion"), label="overrides.background")
        background_id = raw.get("id")
        if not isinstance(background_id, str) or background_id not in PERSONAL_BACKGROUND_REGISTRY_V1:
            raise PersonalThemeModelError("overrides.background.id is not in registry version 1")
        return {"kind": "preset", "registryVersion": 1, "id": background_id}
    if kind == "custom":
        _reject_unknown(raw, ("kind", "light", "dark"), label="overrides.background")
        return {
            "kind": "custom",
            "light": _normalize_hex(raw.get("light"), label="overrides.background.light"),
            "dark": _normalize_hex(raw.get("dark"), label="overrides.background.dark"),
        }
    raise PersonalThemeModelError("overrides.background.kind must be preset or custom")


def _normalize_typography(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise PersonalThemeModelError("overrides.typographyProfile must be an object")
    _reject_unknown(raw, ("type", "id"), label="overrides.typographyProfile")
    if raw.get("type") != "preset":
        raise PersonalThemeModelError("overrides.typographyProfile.type must be preset")
    profile_id = _normalize_enum(
        raw.get("id"),
        TYPOGRAPHY_PROFILE_IDS,
        label="overrides.typographyProfile.id",
    )
    return {"type": "preset", "id": profile_id}


def _normalize_origin(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise PersonalThemeModelError("personal theme origin must be an object")
    _reject_unknown(raw, ("kind", "legacyChecksum"), label="origin")
    if raw.get("kind") != "legacy_projection":
        raise PersonalThemeModelError("personal theme origin.kind must be legacy_projection")
    checksum = raw.get("legacyChecksum")
    if not isinstance(checksum, str) or not _CHECKSUM_RE.fullmatch(checksum):
        raise PersonalThemeModelError("personal theme origin.legacyChecksum must be a sha256 digest")
    return {"kind": "legacy_projection", "legacyChecksum": checksum}


def _normalize_undo_before(raw: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(raw, ("wasMaterialized", "overrides", "origin"), label="before")
    was_materialized = raw.get("wasMaterialized")
    if type(was_materialized) is not bool:
        raise PersonalThemeModelError("personal theme undo before.wasMaterialized must be boolean")
    if "overrides" not in raw:
        raise PersonalThemeModelError("personal theme undo before.overrides is required")
    normalized: dict[str, Any] = {
        "wasMaterialized": was_materialized,
        "overrides": normalize_personal_theme_overrides(raw["overrides"]),
    }
    if "origin" in raw:
        normalized["origin"] = _normalize_origin(raw["origin"])
    if not was_materialized and "origin" not in normalized:
        raise PersonalThemeModelError("unmaterialized personal theme undo requires origin")
    return normalized


def _normalize_checksum(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _CHECKSUM_RE.fullmatch(value):
        raise PersonalThemeModelError(f"{label} must be a sha256 digest")
    return value


def _normalize_utc_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise PersonalThemeModelError("personal theme undo expiresAt must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersonalThemeModelError("personal theme undo expiresAt must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PersonalThemeModelError("personal theme undo expiresAt must be UTC")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_enum(value: Any, allowed: frozenset[str], *, label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise PersonalThemeModelError(f"{label} is invalid")
    return value


def _normalize_hex(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        raise PersonalThemeModelError(f"{label} must be an opaque #RRGGBB color")
    return value.upper()


def _require_registry_version(value: Any, *, label: str) -> None:
    if type(value) is not int or value != PERSONAL_THEME_REGISTRY_VERSION:
        raise PersonalThemeModelError(f"{label}.registryVersion is unsupported")


def _reject_unknown(raw: dict[str, Any], allowed: tuple[str, ...], *, label: str) -> None:
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise PersonalThemeModelError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _assert_canonical_size(value: dict[str, Any]) -> None:
    if len(_canonical_json(value).encode("utf-8")) > MAX_PERSONAL_THEME_CONFIG_BYTES:
        raise PersonalThemeModelError(f"{KEY_APPEARANCE_THEME_CONFIG_V3} exceeds {MAX_PERSONAL_THEME_CONFIG_BYTES} bytes")


__all__ = [
    "KEY_APPEARANCE_THEME_CONFIG_V3",
    "KEY_APPEARANCE_THEME_UNDO_V3",
    "MAX_PERSONAL_THEME_CONFIG_BYTES",
    "MAX_PERSONAL_THEME_UNDO_BYTES",
    "PERSONAL_ACCENT_REGISTRY_V1",
    "PERSONAL_BACKGROUND_REGISTRY_V1",
    "PERSONAL_PALETTE_REGISTRY_V1",
    "PERSONAL_THEME_OVERRIDE_FIELDS",
    "PERSONAL_THEME_REGISTRY_VERSION",
    "PERSONAL_THEME_SCHEMA_VERSION",
    "RESERVED_USER_SETTING_KEYS",
    "PersonalThemeModelError",
    "normalize_personal_theme_config",
    "normalize_personal_theme_overrides",
    "normalize_personal_theme_undo",
    "parse_personal_theme_config",
    "parse_personal_theme_undo",
    "project_legacy_appearance_config",
]
