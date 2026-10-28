"""Theme configuration contracts for user appearance and team brand policy."""

from __future__ import annotations

import copy
import json
from typing import Any, Final

KEY_APPEARANCE_THEME_CONFIG: Final = "appearance_theme_config"
KEY_TEAM_THEME_POLICY: Final = "theme_policy"

MAX_THEME_CONFIG_BYTES: Final = 4096

THEME_MODE_VALUES: Final = frozenset({"system", "light", "dark"})
COLOR_PROFILE_IDS: Final = frozenset({"bowhead", "ocean", "forest", "plum", "graphite"})
TYPOGRAPHY_PROFILE_IDS: Final = frozenset({"bowhead", "system", "readable", "serif-display"})
TYPE_SCALE_VALUES: Final = frozenset({"compact", "standard", "large", "extra-large"})
DENSITY_VALUES: Final = frozenset({"comfortable", "compact", "spacious"})
RADIUS_VALUES: Final = frozenset({"balanced", "sharp", "soft"})
MOTION_VALUES: Final = frozenset({"standard", "reduced"})
CHART_PALETTE_VALUES: Final = frozenset({"default", "high-contrast", "colorblind-safe"})

THEME_FIELDS: Final = (
    "mode",
    "colorProfile",
    "typographyProfile",
    "typeScale",
    "density",
    "radius",
    "motion",
    "chartPalette",
)

PRODUCT_THEME_DEFAULT: Final[dict[str, Any]] = {
    "version": 1,
    "mode": "system",
    "colorProfile": {"type": "preset", "id": "bowhead"},
    "typographyProfile": {"type": "preset", "id": "bowhead"},
    "typeScale": "standard",
    "density": "comfortable",
    "radius": "balanced",
    "motion": "standard",
    "chartPalette": "default",
}

_APPEARANCE_FIELDS: Final = ("version", *THEME_FIELDS)
_TEAM_POLICY_FIELDS: Final = ("version", "defaults", "locks", "brand")


class ThemeConfigError(ValueError):
    """Raised when a theme config payload fails validation."""


def _loads_object(raw_json: str, *, label: str) -> dict[str, Any]:
    if len(raw_json.encode("utf-8")) > MAX_THEME_CONFIG_BYTES:
        raise ThemeConfigError(f"{label} exceeds {MAX_THEME_CONFIG_BYTES} bytes")
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ThemeConfigError(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ThemeConfigError(f"{label} must be a JSON object")
    return parsed


def _require_version(obj: dict[str, Any], *, label: str) -> None:
    if type(obj.get("version")) is not int or obj["version"] != 1:
        raise ThemeConfigError(f"{label}.version must be 1")


def _reject_unknown(obj: dict[str, Any], allowed: tuple[str, ...], *, label: str) -> None:
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        raise ThemeConfigError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _validate_enum(value: Any, allowed: frozenset[str], *, label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ThemeConfigError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return value


def _validate_profile(value: Any, allowed_ids: frozenset[str], *, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ThemeConfigError(f"{label} must be an object")
    _reject_unknown(value, ("type", "id"), label=label)
    if value.get("type") != "preset":
        raise ThemeConfigError(f"{label}.type must be preset")
    profile_id = _validate_enum(value.get("id"), allowed_ids, label=f"{label}.id")
    return {"type": "preset", "id": profile_id}


def _compact_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def _normalize_appearance_obj(obj: dict[str, Any]) -> dict[str, Any]:
    _require_version(obj, label="appearance_theme_config")
    _reject_unknown(obj, _APPEARANCE_FIELDS, label="appearance_theme_config")

    normalized: dict[str, Any] = {"version": 1}
    if "mode" in obj:
        normalized["mode"] = _validate_enum(obj["mode"], THEME_MODE_VALUES, label="mode")
    if "colorProfile" in obj:
        normalized["colorProfile"] = _validate_profile(
            obj["colorProfile"],
            COLOR_PROFILE_IDS,
            label="colorProfile",
        )
    if "typographyProfile" in obj:
        normalized["typographyProfile"] = _validate_profile(
            obj["typographyProfile"],
            TYPOGRAPHY_PROFILE_IDS,
            label="typographyProfile",
        )
    if "typeScale" in obj:
        normalized["typeScale"] = _validate_enum(obj["typeScale"], TYPE_SCALE_VALUES, label="typeScale")
    if "density" in obj:
        normalized["density"] = _validate_enum(obj["density"], DENSITY_VALUES, label="density")
    if "radius" in obj:
        normalized["radius"] = _validate_enum(obj["radius"], RADIUS_VALUES, label="radius")
    if "motion" in obj:
        normalized["motion"] = _validate_enum(obj["motion"], MOTION_VALUES, label="motion")
    if "chartPalette" in obj:
        normalized["chartPalette"] = _validate_enum(
            obj["chartPalette"],
            CHART_PALETTE_VALUES,
            label="chartPalette",
        )
    return normalized


def parse_appearance_theme_config(raw_json: str) -> dict[str, Any]:
    """Parse and validate sparse user appearance overrides."""

    return _normalize_appearance_obj(_loads_object(raw_json, label=KEY_APPEARANCE_THEME_CONFIG))


def normalize_appearance_theme_config(raw_json: str) -> str:
    """Return compact, stable JSON for sparse user appearance overrides."""

    return _compact_json(parse_appearance_theme_config(raw_json))


def _normalize_team_defaults(obj: dict[str, Any]) -> dict[str, Any]:
    if "defaults" in obj:
        defaults = obj["defaults"]
        if not isinstance(defaults, dict):
            raise ThemeConfigError("theme_policy.defaults must be an object")
        _reject_unknown(defaults, ("colorProfile",), label="theme_policy.defaults")
        if "colorProfile" in defaults:
            return {
                "colorProfile": _validate_profile(
                    defaults["colorProfile"],
                    COLOR_PROFILE_IDS,
                    label="defaults.colorProfile",
                ),
            }
    return {}


def _normalize_team_locks(obj: dict[str, Any]) -> dict[str, bool]:
    if "locks" in obj:
        locks = obj["locks"]
        if not isinstance(locks, dict):
            raise ThemeConfigError("theme_policy.locks must be an object")
        _reject_unknown(locks, ("colorProfile",), label="theme_policy.locks")
        if "colorProfile" in locks:
            if not isinstance(locks["colorProfile"], bool):
                raise ThemeConfigError("locks.colorProfile must be boolean")
            return {"colorProfile": locks["colorProfile"]}
    return {}


def _normalize_team_brand(obj: dict[str, Any]) -> dict[str, Any]:
    if "brand" in obj:
        brand = obj["brand"]
        if not isinstance(brand, dict):
            raise ThemeConfigError("theme_policy.brand must be an object")
        _reject_unknown(brand, ("name", "colorProfileId"), label="theme_policy.brand")
        normalized_brand: dict[str, Any] = {}
        if "name" in brand:
            if not isinstance(brand["name"], str) or len(brand["name"]) > 128:
                raise ThemeConfigError("brand.name must be a string up to 128 chars")
            normalized_brand["name"] = brand["name"]
        if "colorProfileId" in brand:
            normalized_brand["colorProfileId"] = _validate_enum(
                brand["colorProfileId"],
                COLOR_PROFILE_IDS,
                label="brand.colorProfileId",
            )
        return normalized_brand
    return {}


def _normalize_team_policy_obj(obj: dict[str, Any]) -> dict[str, Any]:
    _require_version(obj, label="theme_policy")
    _reject_unknown(obj, _TEAM_POLICY_FIELDS, label="theme_policy")

    defaults = _normalize_team_defaults(obj)
    locks = _normalize_team_locks(obj)
    brand = _normalize_team_brand(obj)

    if locks.get("colorProfile") and not defaults.get("colorProfile"):
        raise ThemeConfigError("locks.colorProfile=true requires defaults.colorProfile")

    normalized: dict[str, Any] = {"version": 1}
    if defaults:
        normalized["defaults"] = defaults
    if locks:
        normalized["locks"] = locks
    if brand:
        normalized["brand"] = brand

    return normalized


def parse_team_theme_policy(raw_json: str) -> dict[str, Any]:
    """Parse and validate an M1 team theme policy."""

    return _normalize_team_policy_obj(_loads_object(raw_json, label=KEY_TEAM_THEME_POLICY))


def normalize_team_theme_policy(raw_json: str) -> str:
    """Return compact, stable JSON for an M1 team theme policy."""

    return _compact_json(parse_team_theme_policy(raw_json))


def resolve_effective_theme(
    team_policy: dict[str, Any] | None = None,
    user_config: dict[str, Any] | None = None,
    *,
    product_default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve product defaults, team policy, and sparse user overrides."""

    effective = copy.deepcopy(product_default or PRODUCT_THEME_DEFAULT)
    source = dict.fromkeys(THEME_FIELDS, "product")
    locks: dict[str, bool] = {}

    team_policy = team_policy or {}
    user_config = user_config or {"version": 1}

    team_color = team_policy.get("defaults", {}).get("colorProfile")
    if team_color:
        effective["colorProfile"] = copy.deepcopy(team_color)
        source["colorProfile"] = "team_default"

    for field in THEME_FIELDS:
        if field in user_config:
            effective[field] = copy.deepcopy(user_config[field])
            source[field] = "user"

    if team_policy.get("locks", {}).get("colorProfile"):
        locked_color = team_policy.get("defaults", {}).get("colorProfile")
        if locked_color:
            effective["colorProfile"] = copy.deepcopy(locked_color)
            locks["colorProfile"] = True
            source["colorProfile"] = "team_lock"

    return {
        "effective": effective,
        "locks": locks,
        "source": source,
        "userConfig": copy.deepcopy(user_config),
    }
