"""Chart-style settings contracts and deterministic effective resolution.

The settings in this module deliberately stay separate from both the legacy
appearance blob and team-branding revisions.  They change presentation only;
chart data, ``ChartSpec`` and generation are outside this contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final

from bowhead_core.chart_style_registry_generated import (
    ACTIVE_CHART_STYLE_PRESET_IDS,
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
    PRESET_CONTENT_SHA256,
    PRESET_RELEASES,
)

KEY_TEAM_CHART_STYLE_POLICY: Final = "team_chart_style_policy"
KEY_USER_TEAM_CHART_STYLE_SELECTION_PREFIX: Final = "user_team_chart_style_selection"
KEY_CHART_ACCESSIBILITY_PREFERENCES: Final = "chart_accessibility_preferences"

MAX_CHART_STYLE_SETTING_BYTES: Final = 2048
#: Used only when a team never saved a chart-style policy (and as the last-resort fallback).
#: Saved policies, personal selections and custom styles keep their own preset ids.
#: Changed from "bowhead-editorial" to "clean-blue" for 1.5.0 (product decision 2026-10-01).
PRODUCT_DEFAULT_PRESET_ID: Final = "clean-blue"
ACTIVE_CHART_STYLE_PRESET_ID_SET: Final = frozenset(ACTIVE_CHART_STYLE_PRESET_IDS)

CONTRAST_VALUES: Final = frozenset({"standard", "high"})
COLOR_VISION_VALUES: Final = frozenset({"standard", "color-safe"})


class ChartStyleError(ValueError):
    """Raised when a chart-style setting fails validation."""


def user_team_chart_style_selection_key(team_id: str) -> str:
    """Return the authenticated team's user-setting key."""

    return f"{KEY_USER_TEAM_CHART_STYLE_SELECTION_PREFIX}:{team_id}"


def default_team_chart_style_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "defaultPresetId": PRODUCT_DEFAULT_PRESET_ID,
        "enabledPresetIds": list(ACTIVE_CHART_STYLE_PRESET_IDS),
    }


def _loads_object(raw_json: str, *, label: str) -> dict[str, Any]:
    if len(raw_json.encode("utf-8")) > MAX_CHART_STYLE_SETTING_BYTES:
        raise ChartStyleError(f"{label} exceeds {MAX_CHART_STYLE_SETTING_BYTES} bytes")
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ChartStyleError(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ChartStyleError(f"{label} must be a JSON object")
    return parsed


def _require_exact_fields(obj: dict[str, Any], fields: set[str], *, label: str) -> None:
    if set(obj) != fields:
        raise ChartStyleError(f"{label} must contain exactly: {', '.join(sorted(fields))}")


def _require_version(obj: dict[str, Any], expected: int, *, label: str) -> None:
    if type(obj.get("version")) is not int or obj["version"] != expected:
        raise ChartStyleError(f"{label}.version must be {expected}")


def _compact_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def chart_style_setting_version(raw_json: str) -> int:
    obj = _loads_object(raw_json, label="chart-style setting")
    version = obj.get("version")
    if type(version) is not int:
        raise ChartStyleError("chart-style setting.version must be an integer")
    return version


def _parse_team_chart_style_policy_v1(obj: dict[str, Any]) -> dict[str, Any]:
    _require_version(obj, 1, label=KEY_TEAM_CHART_STYLE_POLICY)
    _require_exact_fields(
        obj,
        {"version", "defaultPresetId", "enabledPresetIds"},
        label=KEY_TEAM_CHART_STYLE_POLICY,
    )
    default_preset_id = obj["defaultPresetId"]
    enabled = obj["enabledPresetIds"]
    if not isinstance(default_preset_id, str) or default_preset_id not in ACTIVE_CHART_STYLE_PRESET_ID_SET:
        raise ChartStyleError("defaultPresetId must reference an active chart-style preset")
    if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
        raise ChartStyleError("enabledPresetIds must be an array of preset ids")
    normalized_enabled = [preset_id for preset_id in ACTIVE_CHART_STYLE_PRESET_IDS if preset_id in set(enabled)]
    if len(normalized_enabled) != len(set(enabled)):
        raise ChartStyleError("enabledPresetIds contains an unknown chart-style preset")
    if len(normalized_enabled) < min(2, len(ACTIVE_CHART_STYLE_PRESET_IDS)):
        raise ChartStyleError("enabledPresetIds must keep at least two active presets")
    if default_preset_id not in normalized_enabled:
        raise ChartStyleError("defaultPresetId must be included in enabledPresetIds")
    return {
        "version": 1,
        "defaultPresetId": default_preset_id,
        "enabledPresetIds": normalized_enabled,
    }


def _require_style_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or (value not in ACTIVE_CHART_STYLE_PRESET_ID_SET and re.fullmatch(r"tcs_[0-9a-f]{24}", value) is None):
        raise ChartStyleError(f"{field} must reference a built-in preset or team chart style")
    return value


def _parse_team_chart_style_policy_v2(obj: dict[str, Any]) -> dict[str, Any]:
    _require_version(obj, 2, label=KEY_TEAM_CHART_STYLE_POLICY)
    _require_exact_fields(
        obj,
        {"version", "defaultStyleId", "enabledStyleIds", "fallbackBuiltInPresetId"},
        label=KEY_TEAM_CHART_STYLE_POLICY,
    )
    default_style_id = _require_style_id(obj["defaultStyleId"], field="defaultStyleId")
    enabled = obj["enabledStyleIds"]
    if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
        raise ChartStyleError("enabledStyleIds must be an array of style ids")
    normalized_enabled = [_require_style_id(item, field="enabledStyleIds") for item in enabled]
    if len(normalized_enabled) != len(set(normalized_enabled)):
        raise ChartStyleError("enabledStyleIds must not contain duplicates")
    if len(normalized_enabled) < 2:
        raise ChartStyleError("enabledStyleIds must keep at least two styles")
    if default_style_id not in normalized_enabled:
        raise ChartStyleError("defaultStyleId must be included in enabledStyleIds")
    fallback = obj["fallbackBuiltInPresetId"]
    if not isinstance(fallback, str) or fallback not in ACTIVE_CHART_STYLE_PRESET_ID_SET:
        raise ChartStyleError("fallbackBuiltInPresetId must reference an active built-in preset")
    # V1 callers cannot resolve custom revisions. Project the explicitly saved
    # built-in fallback without mutating the V2 setting.
    return {
        "version": 1,
        "defaultPresetId": fallback,
        "enabledPresetIds": list(ACTIVE_CHART_STYLE_PRESET_IDS),
    }


def parse_team_chart_style_policy(raw_json: str) -> dict[str, Any]:
    obj = _loads_object(raw_json, label=KEY_TEAM_CHART_STYLE_POLICY)
    if obj.get("version") == 2:
        return _parse_team_chart_style_policy_v2(obj)
    return _parse_team_chart_style_policy_v1(obj)


def normalize_team_chart_style_policy(raw_json: str) -> str:
    obj = _loads_object(raw_json, label=KEY_TEAM_CHART_STYLE_POLICY)
    return _compact_json(_parse_team_chart_style_policy_v1(obj))


def _parse_user_team_chart_style_selection_v1(obj: dict[str, Any]) -> dict[str, Any]:
    _require_version(obj, 1, label=KEY_USER_TEAM_CHART_STYLE_SELECTION_PREFIX)
    _require_exact_fields(
        obj,
        {"version", "presetId"},
        label=KEY_USER_TEAM_CHART_STYLE_SELECTION_PREFIX,
    )
    preset_id = obj["presetId"]
    if not isinstance(preset_id, str) or preset_id not in ACTIVE_CHART_STYLE_PRESET_ID_SET:
        raise ChartStyleError("presetId must reference an active chart-style preset")
    return {"version": 1, "presetId": preset_id}


def _parse_user_team_chart_style_selection_v2(obj: dict[str, Any]) -> dict[str, Any]:
    _require_version(obj, 2, label=KEY_USER_TEAM_CHART_STYLE_SELECTION_PREFIX)
    _require_exact_fields(
        obj,
        {"version", "styleId", "fallbackBuiltInPresetId"},
        label=KEY_USER_TEAM_CHART_STYLE_SELECTION_PREFIX,
    )
    _require_style_id(obj["styleId"], field="styleId")
    fallback = obj["fallbackBuiltInPresetId"]
    if not isinstance(fallback, str) or fallback not in ACTIVE_CHART_STYLE_PRESET_ID_SET:
        raise ChartStyleError("fallbackBuiltInPresetId must reference an active built-in preset")
    return {"version": 1, "presetId": fallback}


def parse_user_team_chart_style_selection(raw_json: str) -> dict[str, Any]:
    obj = _loads_object(raw_json, label=KEY_USER_TEAM_CHART_STYLE_SELECTION_PREFIX)
    if obj.get("version") == 2:
        return _parse_user_team_chart_style_selection_v2(obj)
    return _parse_user_team_chart_style_selection_v1(obj)


def normalize_user_team_chart_style_selection(raw_json: str) -> str:
    obj = _loads_object(raw_json, label=KEY_USER_TEAM_CHART_STYLE_SELECTION_PREFIX)
    return _compact_json(_parse_user_team_chart_style_selection_v1(obj))


def parse_chart_accessibility_preferences(raw_json: str) -> dict[str, Any]:
    obj = _loads_object(raw_json, label=KEY_CHART_ACCESSIBILITY_PREFERENCES)
    unknown = set(obj) - {"version", "contrast", "colorVision"}
    if unknown:
        raise ChartStyleError(f"{KEY_CHART_ACCESSIBILITY_PREFERENCES} has unknown field(s): {', '.join(sorted(unknown))}")
    _require_version(obj, 1, label=KEY_CHART_ACCESSIBILITY_PREFERENCES)
    normalized: dict[str, Any] = {"version": 1}
    contrast = obj.get("contrast")
    color_vision = obj.get("colorVision")
    if contrast is not None:
        if not isinstance(contrast, str) or contrast not in CONTRAST_VALUES:
            raise ChartStyleError("contrast must be one of: high, standard")
        normalized["contrast"] = contrast
    if color_vision is not None:
        if not isinstance(color_vision, str) or color_vision not in COLOR_VISION_VALUES:
            raise ChartStyleError("colorVision must be one of: color-safe, standard")
        normalized["colorVision"] = color_vision
    return normalized


def normalize_chart_accessibility_preferences(raw_json: str) -> str:
    return _compact_json(parse_chart_accessibility_preferences(raw_json))


def chart_style_policy_etag(raw_policy: str | None) -> str:
    """Version token for optimistic team-policy writes."""

    canonical = raw_policy or _compact_json(default_team_chart_style_policy())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f'W/"chart-style-policy:{digest}"'


def resolve_effective_chart_style(
    *,
    team_policy: dict[str, Any] | None,
    user_selection: dict[str, Any] | None,
    accessibility: dict[str, Any] | None,
    policy_etag: str,
) -> dict[str, Any]:
    """Resolve chart style without reading storage, network, time, or chart data."""

    policy = team_policy or default_team_chart_style_policy()
    enabled = list(policy["enabledPresetIds"])
    team_default = policy["defaultPresetId"]
    saved_preset = user_selection.get("presetId") if user_selection else None
    unavailable = bool(saved_preset and saved_preset not in enabled)
    if saved_preset and not unavailable:
        preset_id = saved_preset
        selection_source = "user"
    else:
        preset_id = team_default
        selection_source = "team" if team_policy is not None else "product"

    preferences = accessibility or {"version": 1}
    contrast = preferences.get("contrast", "standard")
    color_vision = preferences.get("colorVision", "standard")
    revision, checksum = PRESET_RELEASES[preset_id]

    return {
        "schemaVersion": 1,
        "preset": {
            "id": preset_id,
            "revision": revision,
            "checksum": checksum,
        },
        "selection": {
            "source": selection_source,
            "savedPresetId": saved_preset,
            "unavailableByTeamPolicy": unavailable,
        },
        "accessibility": {
            "contrast": contrast,
            "colorVision": color_vision,
            "source": {
                "contrast": "user" if "contrast" in preferences else "product",
                "colorVision": "user" if "colorVision" in preferences else "product",
            },
        },
        "teamPolicy": {
            **policy,
            "etag": policy_etag,
        },
    }


__all__ = [
    "ACTIVE_CHART_STYLE_PRESET_IDS",
    "BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM",
    "ChartStyleError",
    "KEY_CHART_ACCESSIBILITY_PREFERENCES",
    "KEY_TEAM_CHART_STYLE_POLICY",
    "KEY_USER_TEAM_CHART_STYLE_SELECTION_PREFIX",
    "PRESET_RELEASES",
    "PRESET_CONTENT_SHA256",
    "PRODUCT_DEFAULT_PRESET_ID",
    "chart_style_setting_version",
    "chart_style_policy_etag",
    "default_team_chart_style_policy",
    "normalize_chart_accessibility_preferences",
    "normalize_team_chart_style_policy",
    "normalize_user_team_chart_style_selection",
    "parse_chart_accessibility_preferences",
    "parse_team_chart_style_policy",
    "parse_user_team_chart_style_selection",
    "resolve_effective_chart_style",
    "user_team_chart_style_selection_key",
]
