"""Trusted chart-style contracts for durable visual exports.

V1 packages remain readable so already-created jobs can finish. V2 writers do
not accept a resolved package from the browser: the browser submits freshness,
then Gateway freezes the exact server-owned style and per-chart presentation.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Final

from bowhead_core.chart_style import PRESET_CONTENT_SHA256, PRESET_RELEASES
from bowhead_core.chart_style_materializer import validate_resolved_snapshot
from bowhead_core.chart_style_models import RESOLVER_POLICY_VERSION, sha256_json
from bowhead_core.chart_style_presentation import (
    normalize_chart_for_rendering_v1,
    resolve_chart_presentation_v2,
)
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
)
from bowhead_core.export_visual_identity import sha256_canonical
from bowhead_core.print_theme import parse_print_theme_snapshot

CHART_STYLE_RESOLVER_POLICY_VERSION: Final = "1"
CHART_RENDER_CONTEXT_RECIPE_VERSION: Final = "1"
CHART_RENDER_CONTEXT_RECIPE_VERSION_V2: Final = "2"
CHART_RENDER_SHAPE_VERSION: Final = "chart-shape/1"
CHART_BAR_RADIUS_RECIPE_VERSION: Final = "bar-radius/1"
CHART_PALETTE_OVERFLOW_RECIPE_VERSION: Final = "palette-overflow/2"
CHART_EXPORT_RENDERER_CONTRACT_VERSION: Final = "2"
CHART_EXPORT_SURFACE_CONTRACT_VERSION: Final = "canvas-export-visual/2"
MAX_FROZEN_CHART_PRESENTATION_BYTES: Final = 64 * 1024
_EXPORT_CONTENT_BOUNDS: Final = {
    "widthPx": 1456,
    "heightPx": 772,
    "devicePixelRatio": 1,
}

_V1_THEME_FIELDS: Final = frozenset(
    {
        "kind",
        "schemaVersion",
        "mode",
        "preset",
        "presetChecksum",
        "selectionSource",
        "accessibility",
        "teamPolicyEtag",
        "resolverPolicyVersion",
        "contextRecipeVersion",
    }
)
_V2_THEME_FIELDS: Final = frozenset(
    {
        "kind",
        "schemaVersion",
        "style",
        "selectionSource",
        "accessibility",
        "teamPolicyEtag",
        "registryVersion",
        "builtInRegistryChecksum",
        "structuralThemeChecksum",
        "modeColors",
        "resolverPolicyVersion",
        "contextRecipeVersion",
        "renderShapeVersion",
        "rendererContractVersion",
    }
)
_V2_FRESHNESS_FIELDS: Final = frozenset(
    {
        "kind",
        "schemaVersion",
        "styleId",
        "revision",
        "snapshotChecksum",
        "teamPolicyEtag",
        "registryVersion",
        "builtInRegistryChecksum",
    }
)
_FROZEN_PRESENTATION_FIELDS: Final = frozenset(
    {
        "normalizedRenderShape",
        "normalizedRenderShapeChecksum",
        "renderContext",
        "requested",
        "effective",
        "adjustments",
        "finalPaletteQA",
        "presentationChecksum",
        "contextRecipeVersion",
        "renderShapeVersion",
        "barRadiusRecipeVersion",
        "paletteOverflowRecipeVersion",
    }
)


class ChartStyleExportError(ValueError):
    """Raised when an export chart-style value is not a trusted contract."""


class ChartPresentationTooLargeError(ChartStyleExportError):
    """这张图的冻结样式放不下——**只是这一张画不进导出**，不是主题或契约坏了。

    单独一个类型是为了让调用方分得开：主题/契约错必须大声失败（整份导出停下来），
    而「这张图太大」应当只把**这一张**降级成表格，不该让整份 PDF/PPTX/XLSX 失败
    （116 实测：编辑态把一张 2000 行的折线切成柱状，冻结样式就超 64KB，
    用户看到的是「导出服务暂不可用，请稍后重试」——而重试永远不会成功）。
    """


def _package_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _json_detach(value: Any, *, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ChartStyleExportError(f"{label} is not JSON-safe") from exc


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def parse_chart_style_export_theme(value: Any) -> dict[str, Any]:
    """Validate and detach a complete V1 or V2 server-frozen theme."""

    if not isinstance(value, dict):
        raise ChartStyleExportError("chart-style export theme is invalid")
    if value.get("schemaVersion") == 2:
        return _parse_v2_theme(value)
    return _parse_v1_theme(value)


def _parse_v1_theme(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _V1_THEME_FIELDS:
        raise ChartStyleExportError("chart-style export theme fields are invalid")
    preset = value.get("preset")
    accessibility = value.get("accessibility")
    preset_id = preset.get("id") if isinstance(preset, dict) else None
    release = PRESET_RELEASES.get(preset_id) if isinstance(preset_id, str) else None
    if (
        value.get("kind") != "chart-style"
        or value.get("schemaVersion") != 1
        or value.get("mode") != "light"
        or value.get("selectionSource") not in {"product", "team", "user"}
        or value.get("resolverPolicyVersion") != CHART_STYLE_RESOLVER_POLICY_VERSION
        or value.get("contextRecipeVersion") != CHART_RENDER_CONTEXT_RECIPE_VERSION
        or not isinstance(value.get("teamPolicyEtag"), str)
        or not isinstance(accessibility, dict)
        or set(accessibility) != {"contrast", "colorVision"}
        or accessibility.get("contrast") not in {"standard", "high"}
        or accessibility.get("colorVision") not in {"standard", "color-safe"}
        or release is None
        or preset.get("presetRevision") != release[0]
        or value.get("presetChecksum") != release[1]
        or _package_sha256(preset) != PRESET_CONTENT_SHA256[preset_id]
    ):
        raise ChartStyleExportError("chart-style export theme is invalid")
    return deepcopy(_json_detach(value, label="chart-style export theme"))


def _parse_v2_theme(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _V2_THEME_FIELDS:
        raise ChartStyleExportError("chart-style export theme fields are invalid")
    style = value.get("style")
    accessibility = value.get("accessibility")
    mode_colors = value.get("modeColors")
    if not isinstance(style, dict) or set(style) not in (
        {"kind", "id", "revision", "snapshotChecksum", "resolvedPresetSnapshot"},
        {
            "kind",
            "id",
            "revision",
            "snapshotChecksum",
            "basePreset",
            "resolvedPresetSnapshot",
        },
    ):
        raise ChartStyleExportError("chart-style export style is invalid")
    snapshot = style.get("resolvedPresetSnapshot")
    if not isinstance(snapshot, dict):
        raise ChartStyleExportError("chart-style export snapshot is invalid")
    try:
        validation = validate_resolved_snapshot(snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        raise ChartStyleExportError("chart-style export snapshot is invalid") from exc
    source = accessibility.get("source") if isinstance(accessibility, dict) else None
    light = mode_colors.get("light") if isinstance(mode_colors, dict) else None
    final_qa = light.get("finalPaletteQA") if isinstance(light, dict) else None
    if (
        value.get("kind") != "chart-style"
        or style.get("kind") not in {"builtIn", "teamCustom"}
        or not isinstance(style.get("id"), str)
        or not style["id"]
        or not isinstance(style.get("revision"), str)
        or not style["revision"]
        or not _is_sha256(style.get("snapshotChecksum"))
        or sha256_json(snapshot) != style.get("snapshotChecksum")
        or snapshot.get("resolverPolicyVersion") != RESOLVER_POLICY_VERSION
        or validation.get("blockers")
        or value.get("selectionSource") not in {"product", "team", "user"}
        or not isinstance(accessibility, dict)
        or set(accessibility) != {"contrast", "colorVision", "source"}
        or accessibility.get("contrast") not in {"standard", "high"}
        or accessibility.get("colorVision") not in {"standard", "color-safe"}
        or not isinstance(source, dict)
        or set(source) != {"contrast", "colorVision"}
        or any(item not in {"product", "user"} for item in source.values())
        or not isinstance(value.get("teamPolicyEtag"), str)
        or not value["teamPolicyEtag"]
        or not isinstance(value.get("registryVersion"), int)
        or isinstance(value.get("registryVersion"), bool)
        or value["registryVersion"] < 0
        or value.get("builtInRegistryChecksum") != BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM
        or not _is_sha256(value.get("structuralThemeChecksum"))
        or not isinstance(mode_colors, dict)
        or set(mode_colors) != {"light"}
        or not isinstance(light, dict)
        or light.get("paletteOverflowRecipeVersion") != CHART_PALETTE_OVERFLOW_RECIPE_VERSION
        or light.get("maxDistinctSeriesColors") != 16
        or not isinstance(light.get("categoricalSeriesPalette"), list)
        or not 6 <= len(light["categoricalSeriesPalette"]) <= 16
        or not isinstance(final_qa, dict)
        or final_qa.get("hardInvariantsPass") is not True
        or value.get("resolverPolicyVersion") != RESOLVER_POLICY_VERSION
        or value.get("contextRecipeVersion") != CHART_RENDER_CONTEXT_RECIPE_VERSION_V2
        or value.get("renderShapeVersion") != CHART_RENDER_SHAPE_VERSION
        or value.get("rendererContractVersion") != CHART_EXPORT_RENDERER_CONTRACT_VERSION
    ):
        raise ChartStyleExportError("chart-style export theme is invalid")
    base = style.get("basePreset")
    if (
        (style.get("kind") == "teamCustom" and base is None)
        or (style.get("kind") == "builtIn" and base is not None)
        or (
            base is not None
            and (
                not isinstance(base, dict)
                or set(base) != {"id", "revision", "checksum"}
                or not all(isinstance(base.get(key), str) and base[key] for key in ("id", "revision"))
                or not _is_sha256(base.get("checksum"))
            )
        )
    ):
        raise ChartStyleExportError("chart-style export base preset is invalid")
    return deepcopy(_json_detach(value, label="chart-style export theme"))


def parse_chart_style_export_freshness_v2(value: Any) -> dict[str, Any]:
    """Validate the compact client freshness receipt; it contains no tokens."""

    if not isinstance(value, dict) or set(value) != _V2_FRESHNESS_FIELDS:
        raise ChartStyleExportError("chart-style freshness fields are invalid")
    if (
        value.get("kind") != "chart-style-freshness"
        or value.get("schemaVersion") != 2
        or not isinstance(value.get("styleId"), str)
        or not value["styleId"]
        or not isinstance(value.get("revision"), str)
        or not value["revision"]
        or not _is_sha256(value.get("snapshotChecksum"))
        or not isinstance(value.get("teamPolicyEtag"), str)
        or not value["teamPolicyEtag"]
        or not isinstance(value.get("registryVersion"), int)
        or isinstance(value.get("registryVersion"), bool)
        or value["registryVersion"] < 0
        or value.get("builtInRegistryChecksum") != BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM
    ):
        raise ChartStyleExportError("chart-style freshness is invalid")
    return deepcopy(_json_detach(value, label="chart-style freshness"))


def build_chart_style_export_theme_v2(
    effective: dict[str, Any],
    *,
    print_theme: dict[str, Any],
) -> dict[str, Any]:
    """Freeze an export-only V2 theme from a server-resolved effective value."""

    parsed_print = parse_print_theme_snapshot(print_theme)
    style = effective.get("style")
    if effective.get("schemaVersion") != 2 or not isinstance(style, dict):
        raise ChartStyleExportError("effective chart style V2 is invalid")
    frozen_style = {
        key: deepcopy(style[key])
        for key in (
            "kind",
            "id",
            "revision",
            "snapshotChecksum",
            "basePreset",
            "resolvedPresetSnapshot",
        )
        if key in style
    }
    theme = {
        "kind": "chart-style",
        "schemaVersion": 2,
        "style": frozen_style,
        "selectionSource": effective.get("selection", {}).get("source"),
        "accessibility": deepcopy(effective.get("accessibility")),
        "teamPolicyEtag": effective.get("teamPolicy", {}).get("etag"),
        "registryVersion": effective.get("registryVersion"),
        "builtInRegistryChecksum": effective.get("builtInRegistryChecksum"),
        "structuralThemeChecksum": parsed_print["checksum"],
        "modeColors": {"light": deepcopy(effective.get("modeColors", {}).get("light"))},
        "resolverPolicyVersion": RESOLVER_POLICY_VERSION,
        "contextRecipeVersion": CHART_RENDER_CONTEXT_RECIPE_VERSION_V2,
        "renderShapeVersion": CHART_RENDER_SHAPE_VERSION,
        "rendererContractVersion": CHART_EXPORT_RENDERER_CONTRACT_VERSION,
    }
    return _parse_v2_theme(theme)


def require_current_chart_style_v2(
    freshness: dict[str, Any],
    effective: dict[str, Any],
) -> None:
    """Reject stale/forged V2 references before constructing a snapshot."""

    style = effective.get("style")
    policy = effective.get("teamPolicy")
    if (
        not isinstance(style, dict)
        or not isinstance(policy, dict)
        or (
            freshness["styleId"] != style.get("id")
            or freshness["revision"] != style.get("revision")
            or freshness["snapshotChecksum"] != style.get("snapshotChecksum")
            or freshness["teamPolicyEtag"] != policy.get("etag")
            or freshness["registryVersion"] != effective.get("registryVersion")
            or freshness["builtInRegistryChecksum"] != effective.get("builtInRegistryChecksum")
        )
    ):
        raise ChartStyleExportError("chart-style export freshness is stale")


def require_current_chart_style(
    theme: dict[str, Any],
    effective: dict[str, Any],
) -> None:
    """Prevent V1 clients from forging or exporting a stale selection."""

    preset = effective["preset"]
    accessibility = effective["accessibility"]
    if (
        theme["preset"]["id"] != preset["id"]
        or theme["preset"]["presetRevision"] != preset["revision"]
        or theme["presetChecksum"] != preset["checksum"]
        or theme["selectionSource"] != effective["selection"]["source"]
        or theme["accessibility"]
        != {
            "contrast": accessibility["contrast"],
            "colorVision": accessibility["colorVision"],
        }
        or theme["teamPolicyEtag"] != effective["teamPolicy"]["etag"]
    ):
        raise ChartStyleExportError("chart-style export theme is stale")


def export_chart_render_context_v2(*, locale: str) -> dict[str, Any]:
    if locale not in {"zh-CN", "en-US", "de-DE", "fr-FR"}:
        raise ChartStyleExportError("chart export locale is invalid")
    return {
        "placement": "embed",
        "bounds": dict(_EXPORT_CONTENT_BOUNDS),
        "input": {"hover": False, "pointer": "none", "keyboard": False},
        "output": {"medium": "raster", "static": True},
        "mode": "light",
        "locale": locale,
        "reducedMotion": True,
        "forcedColors": False,
        "exactDataAlternative": {"kind": "none"},
    }


def _color_only_series_capacity(surface: dict[str, Any]) -> int | None:
    """The gateway-measured color-only prefix; ``None`` (whole palette) for older projections."""

    value = surface.get("finalPaletteQA", {}).get("colorOnlySeriesCapacity")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def freeze_chart_presentation_v2(
    raw_spec: dict[str, Any],
    *,
    theme: dict[str, Any],
    locale: str,
) -> dict[str, Any]:
    """Freeze the only V2 presentation consumed by the public export page."""

    parsed_theme = _parse_v2_theme(theme)
    _, shape = normalize_chart_for_rendering_v1(raw_spec)
    render_context = export_chart_render_context_v2(locale=locale)
    surface = parsed_theme["modeColors"]["light"]
    resolved = resolve_chart_presentation_v2(
        shape,
        kind_tokens=parsed_theme["style"]["resolvedPresetSnapshot"]["kindTokens"],
        categorical_series_palette=surface["categoricalSeriesPalette"],
        accessibility=parsed_theme["accessibility"],
        render_context=render_context,
        color_only_series_capacity=_color_only_series_capacity(surface),
    )
    frozen = {
        "normalizedRenderShape": shape,
        "normalizedRenderShapeChecksum": sha256_canonical(shape),
        "renderContext": render_context,
        "requested": resolved["requested"],
        "effective": resolved["effective"],
        "adjustments": resolved["adjustments"],
        "finalPaletteQA": deepcopy(surface["finalPaletteQA"]),
        "contextRecipeVersion": CHART_RENDER_CONTEXT_RECIPE_VERSION_V2,
        "renderShapeVersion": CHART_RENDER_SHAPE_VERSION,
        "barRadiusRecipeVersion": CHART_BAR_RADIUS_RECIPE_VERSION,
        "paletteOverflowRecipeVersion": CHART_PALETTE_OVERFLOW_RECIPE_VERSION,
    }
    frozen["presentationChecksum"] = sha256_canonical(frozen)
    parsed = parse_frozen_chart_presentation_v2(frozen, locale=locale)
    encoded = json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_FROZEN_CHART_PRESENTATION_BYTES:
        raise ChartPresentationTooLargeError("frozen chart presentation is too large")
    return parsed


def parse_frozen_chart_presentation_v2(
    value: Any,
    *,
    locale: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FROZEN_PRESENTATION_FIELDS:
        raise ChartStyleExportError("frozen chart presentation fields are invalid")
    shape = value.get("normalizedRenderShape")
    checksum_payload = {key: child for key, child in value.items() if key != "presentationChecksum"}
    final_qa = value.get("finalPaletteQA")
    if (
        not isinstance(shape, dict)
        or shape.get("version") != CHART_RENDER_SHAPE_VERSION
        or value.get("normalizedRenderShapeChecksum") != sha256_canonical(shape)
        or value.get("renderContext") != export_chart_render_context_v2(locale=locale)
        or not isinstance(value.get("requested"), dict)
        or not isinstance(value.get("effective"), dict)
        or not isinstance(value.get("adjustments"), list)
        or not isinstance(final_qa, dict)
        or final_qa.get("hardInvariantsPass") is not True
        or value.get("presentationChecksum") != sha256_canonical(checksum_payload)
        or value.get("contextRecipeVersion") != CHART_RENDER_CONTEXT_RECIPE_VERSION_V2
        or value.get("renderShapeVersion") != CHART_RENDER_SHAPE_VERSION
        or value.get("barRadiusRecipeVersion") != CHART_BAR_RADIUS_RECIPE_VERSION
        or value.get("paletteOverflowRecipeVersion") != CHART_PALETTE_OVERFLOW_RECIPE_VERSION
    ):
        raise ChartStyleExportError("frozen chart presentation is invalid")
    return deepcopy(_json_detach(value, label="frozen chart presentation"))


__all__ = [
    "CHART_BAR_RADIUS_RECIPE_VERSION",
    "CHART_EXPORT_RENDERER_CONTRACT_VERSION",
    "CHART_EXPORT_SURFACE_CONTRACT_VERSION",
    "CHART_PALETTE_OVERFLOW_RECIPE_VERSION",
    "CHART_RENDER_CONTEXT_RECIPE_VERSION",
    "CHART_RENDER_CONTEXT_RECIPE_VERSION_V2",
    "CHART_RENDER_SHAPE_VERSION",
    "CHART_STYLE_RESOLVER_POLICY_VERSION",
    "ChartPresentationTooLargeError",
    "MAX_FROZEN_CHART_PRESENTATION_BYTES",
    "ChartStyleExportError",
    "build_chart_style_export_theme_v2",
    "export_chart_render_context_v2",
    "freeze_chart_presentation_v2",
    "parse_chart_style_export_freshness_v2",
    "parse_chart_style_export_theme",
    "parse_frozen_chart_presentation_v2",
    "require_current_chart_style",
    "require_current_chart_style_v2",
]
