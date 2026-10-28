"""V2 chart-style policy, selection, and safe built-in projections.

This module is deliberately additive.  The V1 contract in ``chart_style``
continues to serve clients that do not opt in to contract 2.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Final

from bowhead_core.chart_style import (
    ACTIVE_CHART_STYLE_PRESET_ID_SET,
    ACTIVE_CHART_STYLE_PRESET_IDS,
    MAX_CHART_STYLE_SETTING_BYTES,
    PRODUCT_DEFAULT_PRESET_ID,
    ChartStyleError,
)
from bowhead_core.chart_style_materializer import built_in_authoring_base
from bowhead_core.chart_style_models import sha256_json
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
    CHART_STYLE_PRESETS,
)
from bowhead_core.theme_compiler import canonical_json

CHART_STYLE_CONTRACT_HEADER: Final = "X-Bowhead-Chart-Style-Contract"
CHART_STYLE_CONTRACT_V2: Final = "2"
MAX_CHART_STYLE_OPTIONS: Final = 15

_CUSTOM_STYLE_ID_RE: Final = re.compile(r"^tcs_[0-9a-f]{24}$")
_POLICY_FIELDS: Final = frozenset({"version", "defaultStyleId", "enabledStyleIds", "fallbackBuiltInPresetId"})
_SELECTION_FIELDS: Final = frozenset({"version", "styleId", "fallbackBuiltInPresetId"})
_BUILT_IN_NAMES: Final = {
    "bowhead-editorial": (
        "Editorial report",
        "Warm, restrained hierarchy for evidence-led reports.",
    ),
    "clean-blue": (
        "Clean blue",
        "Bright and clear for operating and everyday analysis.",
    ),
    "analytical-neutral": (
        "Analytical neutral",
        "Low saturation and high information density for serious analysis.",
    ),
}


def default_team_chart_style_policy_v2() -> dict[str, Any]:
    return {
        "version": 2,
        "defaultStyleId": PRODUCT_DEFAULT_PRESET_ID,
        "enabledStyleIds": list(ACTIVE_CHART_STYLE_PRESET_IDS),
        "fallbackBuiltInPresetId": PRODUCT_DEFAULT_PRESET_ID,
    }


def is_custom_chart_style_id(value: str) -> bool:
    return _CUSTOM_STYLE_ID_RE.fullmatch(value) is not None


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


def _style_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or (value not in ACTIVE_CHART_STYLE_PRESET_ID_SET and not is_custom_chart_style_id(value)):
        raise ChartStyleError(f"{field} must reference an active built-in preset or team chart style")
    return value


def parse_team_chart_style_policy_v2(raw_json: str) -> dict[str, Any]:
    """Parse either stored policy version into the lossless V2 projection."""

    obj = _loads_object(raw_json, label="team_chart_style_policy")
    version = obj.get("version")
    if version == 1:
        if set(obj) != {"version", "defaultPresetId", "enabledPresetIds"}:
            raise ChartStyleError("team_chart_style_policy V1 fields are invalid")
        default_id = obj.get("defaultPresetId")
        enabled = obj.get("enabledPresetIds")
        if (
            not isinstance(default_id, str)
            or default_id not in ACTIVE_CHART_STYLE_PRESET_ID_SET
            or not isinstance(enabled, list)
            or any(not isinstance(item, str) or item not in ACTIVE_CHART_STYLE_PRESET_ID_SET for item in enabled)
            or len(enabled) != len(set(enabled))
            or len(enabled) < 2
            or default_id not in enabled
        ):
            raise ChartStyleError("team_chart_style_policy V1 is invalid")
        return {
            "version": 2,
            "defaultStyleId": default_id,
            "enabledStyleIds": list(enabled),
            "fallbackBuiltInPresetId": default_id,
        }
    if type(version) is not int or version != 2:
        raise ChartStyleError("team_chart_style_policy.version must be 1 or 2")
    if set(obj) != _POLICY_FIELDS:
        raise ChartStyleError("team_chart_style_policy must contain exactly: " + ", ".join(sorted(_POLICY_FIELDS)))
    default_id = _style_id(obj.get("defaultStyleId"), field="defaultStyleId")
    enabled = obj.get("enabledStyleIds")
    if not isinstance(enabled, list):
        raise ChartStyleError("enabledStyleIds must be an array of style ids")
    normalized_enabled = [_style_id(item, field="enabledStyleIds") for item in enabled]
    if len(normalized_enabled) != len(set(normalized_enabled)):
        raise ChartStyleError("enabledStyleIds must not contain duplicates")
    if len(normalized_enabled) < 2:
        raise ChartStyleError("enabledStyleIds must keep at least two styles")
    if len(normalized_enabled) > MAX_CHART_STYLE_OPTIONS:
        raise ChartStyleError(f"enabledStyleIds must contain at most {MAX_CHART_STYLE_OPTIONS} styles")
    if default_id not in normalized_enabled:
        raise ChartStyleError("defaultStyleId must be included in enabledStyleIds")
    fallback = obj.get("fallbackBuiltInPresetId")
    if not isinstance(fallback, str) or fallback not in ACTIVE_CHART_STYLE_PRESET_ID_SET:
        raise ChartStyleError("fallbackBuiltInPresetId must reference an active built-in preset")
    return {
        "version": 2,
        "defaultStyleId": default_id,
        "enabledStyleIds": normalized_enabled,
        "fallbackBuiltInPresetId": fallback,
    }


def normalize_team_chart_style_policy_v2(raw_json: str) -> str:
    return canonical_json(parse_team_chart_style_policy_v2(raw_json))


def parse_user_team_chart_style_selection_v2(raw_json: str) -> dict[str, Any]:
    """Accept V1 built-in selections and the server-owned V2 envelope."""

    obj = _loads_object(raw_json, label="user_team_chart_style_selection")
    version = obj.get("version")
    if version == 1:
        if set(obj) != {"version", "presetId"}:
            raise ChartStyleError("user_team_chart_style_selection V1 fields are invalid")
        preset_id = obj.get("presetId")
        if not isinstance(preset_id, str) or preset_id not in ACTIVE_CHART_STYLE_PRESET_ID_SET:
            raise ChartStyleError("presetId must reference an active built-in preset")
        return {
            "version": 2,
            "styleId": preset_id,
            "fallbackBuiltInPresetId": preset_id,
        }
    if type(version) is not int or version != 2:
        raise ChartStyleError("user_team_chart_style_selection.version must be 1 or 2")
    if set(obj) != _SELECTION_FIELDS:
        raise ChartStyleError("user_team_chart_style_selection must contain exactly: " + ", ".join(sorted(_SELECTION_FIELDS)))
    selected = _style_id(obj.get("styleId"), field="styleId")
    fallback = obj.get("fallbackBuiltInPresetId")
    if not isinstance(fallback, str) or fallback not in ACTIVE_CHART_STYLE_PRESET_ID_SET:
        raise ChartStyleError("fallbackBuiltInPresetId must reference an active built-in preset")
    return {
        "version": 2,
        "styleId": selected,
        "fallbackBuiltInPresetId": fallback,
    }


def normalize_user_team_chart_style_selection_v2(*, style_id: str, fallback_builtin_preset_id: str) -> str:
    return canonical_json(
        parse_user_team_chart_style_selection_v2(
            canonical_json(
                {
                    "version": 2,
                    "styleId": style_id,
                    "fallbackBuiltInPresetId": fallback_builtin_preset_id,
                }
            )
        )
    )


def chart_style_policy_etag_v2(raw_policy: str | None) -> str:
    canonical = raw_policy or canonical_json(default_team_chart_style_policy_v2())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f'"chart-style-policy-v2:{digest}"'


def strong_chart_style_etag(label: str, value: object) -> str:
    digest = sha256_json(value).removeprefix("sha256:")
    return f'"{label}:{digest}"'


def built_in_style_projection(preset_id: str) -> dict[str, Any]:
    """Return one renderer-independent, safe built-in style projection."""

    snapshot, revision, _ = built_in_authoring_base(preset_id)
    package = CHART_STYLE_PRESETS[preset_id]
    name, description = _BUILT_IN_NAMES[preset_id]
    return {
        "kind": "builtIn",
        "id": preset_id,
        "name": name,
        "description": description,
        "nameKey": package["labelKey"],
        "descriptionKey": package["descriptionKey"],
        "revision": revision,
        "snapshotChecksum": sha256_json(snapshot),
        "fallbackBuiltInPresetId": preset_id,
        "resolvedPresetSnapshot": copy.deepcopy(snapshot),
    }


def built_in_style_catalog() -> dict[str, dict[str, Any]]:
    return {preset_id: built_in_style_projection(preset_id) for preset_id in ACTIVE_CHART_STYLE_PRESET_IDS}


def chart_style_cache_identity(
    *,
    team_id: str,
    registry_version: int,
    raw_policy: str | None,
    raw_selection: str | None,
    selection_updated_at: str | None,
    raw_accessibility: str | None,
    accessibility_updated_at: str | None,
) -> str:
    return canonical_json(
        {
            "teamId": team_id,
            "registryVersion": registry_version,
            "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
            "policyRaw": raw_policy,
            "policyEtag": chart_style_policy_etag_v2(raw_policy),
            "selectionRaw": raw_selection,
            "selectionUpdatedAt": selection_updated_at,
            "accessibilityRaw": raw_accessibility,
            "accessibilityUpdatedAt": accessibility_updated_at,
            "contractVersion": 2,
        }
    )


__all__ = [
    "CHART_STYLE_CONTRACT_HEADER",
    "CHART_STYLE_CONTRACT_V2",
    "MAX_CHART_STYLE_OPTIONS",
    "built_in_style_catalog",
    "built_in_style_projection",
    "chart_style_cache_identity",
    "chart_style_policy_etag_v2",
    "default_team_chart_style_policy_v2",
    "is_custom_chart_style_id",
    "normalize_team_chart_style_policy_v2",
    "normalize_user_team_chart_style_selection_v2",
    "parse_team_chart_style_policy_v2",
    "parse_user_team_chart_style_selection_v2",
    "strong_chart_style_etag",
]
