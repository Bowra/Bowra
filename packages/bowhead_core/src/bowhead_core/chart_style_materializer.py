"""Pure validation and materialization for team-authored chart styles."""

from __future__ import annotations

import copy
import re
import unicodedata
from functools import lru_cache
from itertools import combinations
from typing import Any, Final

from bowhead_core.chart_palette_distinctness import (
    BASE_CVD_TARGET,
    BASE_NORMAL_TARGET,
    CVD_MATRICES,
    distinct_prefix_length,
    labs_distinctness,
    simulate_vision,
    vision_labs,
)
from bowhead_core.chart_style_models import (
    RESOLVER_POLICY_VERSION,
    STYLE_SCHEMA_VERSION,
    TOKEN_SCHEMA_VERSION,
    VALIDATION_POLICY_VERSION,
    ChartStyleDomainError,
    sha256_json,
)
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
    CHART_STYLE_PRESENTATION_POLICY,
    CHART_STYLE_PRESETS,
    PRESET_RELEASES,
)
from bowhead_core.theme_compiler import canonical_json, hex_to_oklch, oklch_to_hex
from bowhead_core.theme_validation import contrast_ratio, delta_e_ok, relative_luminance

CHART_STYLE_KINDS: Final = (
    "line",
    "area_stacked",
    "bar",
    "hbar",
    "stacked_bar",
    "pie",
    "dual_axis",
    "combo",
    "scatter",
    "heatmap",
    "waterfall",
    "sankey",
)
LINE_KINDS: Final = frozenset({"line", "area_stacked", "dual_axis", "combo"})
BAR_KINDS: Final = frozenset({"bar", "hbar", "stacked_bar", "waterfall"})

MAX_AUTHORING_SOURCE_BYTES: Final = 16 * 1024
MAX_RESOLVED_SNAPSHOT_BYTES: Final = 32 * 1024
LIGHT_STANDARD_SURFACE: Final = "#FFFFFF"
DARK_STANDARD_SURFACE: Final = "#1B1A17"
SURFACE_CONTRACT_VERSION: Final = "chart-surface/1"
DARK_PALETTE_RECIPE_VERSION: Final = "bowhead-chart-dark-palette/1"
COLOR_QA_POLICY_VERSION: Final = "chart-color-qa/2"

_HEX_RE: Final = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SOURCE_FIELDS: Final = frozenset({"schemaVersion", "name", "description", "overrides"})
_OVERRIDE_FIELDS: Final = frozenset({"schemaVersion", "categoricalPalette", "line", "bar", "components"})
_LINE_VALUES: Final = {
    "curve": frozenset({"straight", "monotone"}),
    "width": frozenset({"thin", "standard", "strong"}),
    "points": frozenset({"auto", "show", "hide"}),
}
_BAR_VALUES: Final = {"radius": frozenset({"square", "soft", "round"})}
_COMPONENT_VALUES: Final = {
    "tooltip": frozenset({"enabled", "disabled"}),
    "grid": frozenset({"hidden", "quiet", "standard"}),
}
_WIDTH_PX: Final = CHART_STYLE_PRESENTATION_POLICY["line"]["widthPx"]
_RADIUS_PX: Final = CHART_STYLE_PRESENTATION_POLICY["bar"]["radiusPx"]

_CVD_DEFICIENCIES: Final = tuple(CVD_MATRICES)


def _normalized_text(value: Any, *, field: str, minimum: int, maximum: int, collapse: bool) -> str:
    if not isinstance(value, str):
        raise ChartStyleDomainError(
            "chart_style_invalid_field",
            status_code=400,
            details={"path": field, "reason": "string_required"},
        )
    normalized = unicodedata.normalize("NFKC", value).strip()
    if collapse:
        normalized = " ".join(normalized.split())
    if not minimum <= len(normalized) <= maximum:
        raise ChartStyleDomainError(
            "chart_style_invalid_field",
            status_code=400,
            details={"path": field, "reason": "length", "minimum": minimum, "maximum": maximum},
        )
    return normalized


def normalize_style_name(value: Any) -> tuple[str, str]:
    name = _normalized_text(value, field="name", minimum=2, maximum=40, collapse=True)
    return name, name.casefold()


def _object(value: Any, *, path: str, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChartStyleDomainError(
            "chart_style_invalid_field",
            status_code=400,
            details={"path": path, "reason": "object_required"},
        )
    unknown = sorted(set(value) - fields)
    if unknown:
        raise ChartStyleDomainError(
            "chart_style_unknown_field",
            status_code=400,
            details={"path": path, "fields": unknown},
        )
    return value


def _enum_object(value: Any, *, path: str, values: dict[str, frozenset[str]]) -> dict[str, str]:
    obj = _object(value, path=path, fields=frozenset(values))
    normalized: dict[str, str] = {}
    for field in values:
        if field not in obj:
            continue
        item = obj[field]
        if not isinstance(item, str) or item not in values[field]:
            raise ChartStyleDomainError(
                "chart_style_invalid_field",
                status_code=400,
                details={"path": f"{path}.{field}", "reason": "enum", "allowed": sorted(values[field])},
            )
        normalized[field] = item
    return normalized


def _palette(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ChartStyleDomainError(
            "chart_style_invalid_field",
            status_code=400,
            details={"path": path, "reason": "array_length", "minimum": 1, "maximum": 8},
        )
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or _HEX_RE.fullmatch(item) is None:
            raise ChartStyleDomainError(
                "chart_style_invalid_field",
                status_code=400,
                details={"path": f"{path}.{index}", "reason": "hex_color"},
            )
        normalized.append(item.upper())
    return normalized


def normalize_authoring_source(source: Any) -> dict[str, Any]:
    """Validate storage safety and return the canonical authoring source."""

    obj = _object(source, path="source", fields=_SOURCE_FIELDS)
    if set(obj) != _SOURCE_FIELDS:
        raise ChartStyleDomainError(
            "chart_style_invalid_field",
            status_code=400,
            details={"path": "source", "reason": "required_fields", "fields": sorted(_SOURCE_FIELDS)},
        )
    if type(obj.get("schemaVersion")) is not int or obj["schemaVersion"] != STYLE_SCHEMA_VERSION:
        raise ChartStyleDomainError(
            "chart_style_invalid_field",
            status_code=400,
            details={"path": "source.schemaVersion", "reason": "unsupported_version"},
        )
    name, _ = normalize_style_name(obj["name"])
    description = _normalized_text(obj["description"], field="description", minimum=0, maximum=120, collapse=False)
    raw_overrides = _object(obj["overrides"], path="overrides", fields=_OVERRIDE_FIELDS)
    if "schemaVersion" not in raw_overrides:
        raise ChartStyleDomainError(
            "chart_style_invalid_field",
            status_code=400,
            details={"path": "overrides.schemaVersion", "reason": "required"},
        )
    if type(raw_overrides["schemaVersion"]) is not int or raw_overrides["schemaVersion"] != STYLE_SCHEMA_VERSION:
        raise ChartStyleDomainError(
            "chart_style_invalid_field",
            status_code=400,
            details={"path": "overrides.schemaVersion", "reason": "unsupported_version"},
        )
    overrides: dict[str, Any] = {"schemaVersion": STYLE_SCHEMA_VERSION}
    if "categoricalPalette" in raw_overrides:
        raw_palette = _object(
            raw_overrides["categoricalPalette"],
            path="overrides.categoricalPalette",
            fields=frozenset({"light", "dark"}),
        )
        if not raw_palette:
            raise ChartStyleDomainError(
                "chart_style_invalid_field",
                status_code=400,
                details={"path": "overrides.categoricalPalette", "reason": "empty_object"},
            )
        palette: dict[str, list[str]] = {}
        for mode in ("light", "dark"):
            if mode in raw_palette:
                palette[mode] = _palette(raw_palette[mode], path=f"overrides.categoricalPalette.{mode}")
        overrides["categoricalPalette"] = palette
    for field, values in (("line", _LINE_VALUES), ("bar", _BAR_VALUES), ("components", _COMPONENT_VALUES)):
        if field in raw_overrides:
            normalized = _enum_object(raw_overrides[field], path=f"overrides.{field}", values=values)
            if not normalized:
                raise ChartStyleDomainError(
                    "chart_style_invalid_field",
                    status_code=400,
                    details={"path": f"overrides.{field}", "reason": "empty_object"},
                )
            overrides[field] = normalized
    normalized_source = {
        "schemaVersion": STYLE_SCHEMA_VERSION,
        "name": name,
        "description": description,
        "overrides": overrides,
    }
    if len(canonical_json(normalized_source).encode("utf-8")) > MAX_AUTHORING_SOURCE_BYTES:
        raise ChartStyleDomainError(
            "chart_style_source_too_large",
            status_code=400,
            details={"maximumBytes": MAX_AUTHORING_SOURCE_BYTES},
        )
    return normalized_source


def built_in_authoring_base(preset_id: str) -> tuple[dict[str, Any], str, str]:
    """Compile the current built-in package into the frozen V2 base shape."""

    package = CHART_STYLE_PRESETS.get(preset_id)
    release = PRESET_RELEASES.get(preset_id)
    if package is None or release is None or package.get("status") != "active":
        raise ChartStyleDomainError(
            "chart_style_base_preset_unavailable",
            status_code=400,
            details={"basePresetId": preset_id},
        )
    geometry = package["tokens"]["geometry"]
    grid = geometry["gridDensity"]
    if grid == "strong":
        grid = "standard"
    kind_tokens: dict[str, Any] = {}
    for kind in CHART_STYLE_KINDS:
        tokens: dict[str, Any] = {"tooltip": "enabled", "grid": grid}
        if kind in LINE_KINDS:
            tokens["line"] = {
                "curve": "monotone",
                "widthPx": geometry["lineWidth"],
                "points": "auto",
                "pointRadiusPx": geometry["pointRadius"],
            }
        if kind in BAR_KINDS:
            tokens["bar"] = {"radiusPx": geometry["barRadius"]}
        kind_tokens[kind] = tokens
    base = {
        "schemaVersion": STYLE_SCHEMA_VERSION,
        "tokenSchemaVersion": TOKEN_SCHEMA_VERSION,
        "resolverPolicyVersion": RESOLVER_POLICY_VERSION,
        "capabilities": {
            "supportedChartKinds": list(CHART_STYLE_KINDS),
            "requiredAdapterCapabilities": list(package["capabilities"]["requiredRendererFeatures"]),
        },
        "colors": {
            mode: {
                "categorical": list(package["tokens"][mode]["palette"]),
                "sequential": dict(package["tokens"][mode]["sequential"]),
                "diverging": dict(package["tokens"][mode]["diverging"]),
            }
            for mode in ("light", "dark")
        },
        "kindTokens": kind_tokens,
    }
    return base, release[0], release[1]


def _generate_dark_color(light_color: str) -> str:
    lightness, chroma, hue = hex_to_oklch(light_color)
    dark_lightness = min(0.82, max(0.68, 1.0 - 0.45 * lightness))
    dark_chroma = min(0.16, chroma * 0.9)
    candidate = oklch_to_hex(dark_lightness, dark_chroma, hue)
    while contrast_ratio(candidate, DARK_STANDARD_SURFACE) < 3.0 and dark_lightness < 0.95:
        dark_lightness = min(0.95, round(dark_lightness + 0.001, 3))
        candidate = oklch_to_hex(dark_lightness, dark_chroma, hue)
    return candidate


def materialize_chart_style(authoring_base_snapshot: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply typed team overrides to a frozen renderer-independent base."""

    snapshot = copy.deepcopy(authoring_base_snapshot)
    palette = overrides.get("categoricalPalette")
    if palette:
        if "light" in palette:
            snapshot["colors"]["light"]["categorical"] = list(palette["light"])
            if "dark" not in palette:
                snapshot["colors"]["dark"]["categorical"] = [_generate_dark_color(color) for color in palette["light"]]
        if "dark" in palette:
            snapshot["colors"]["dark"]["categorical"] = list(palette["dark"])
    line = overrides.get("line", {})
    bar = overrides.get("bar", {})
    components = overrides.get("components", {})
    for kind in snapshot["capabilities"]["supportedChartKinds"]:
        tokens = snapshot["kindTokens"][kind]
        if "tooltip" in components:
            tokens["tooltip"] = components["tooltip"]
        if "grid" in components:
            tokens["grid"] = components["grid"]
        if "line" in tokens:
            if "curve" in line:
                tokens["line"]["curve"] = line["curve"]
            if "width" in line:
                tokens["line"]["widthPx"] = _WIDTH_PX[line["width"]]
            if "points" in line:
                tokens["line"]["points"] = line["points"]
        if "bar" in tokens and "radius" in bar:
            tokens["bar"]["radiusPx"] = _RADIUS_PX[bar["radius"]]
    if len(canonical_json(snapshot).encode("utf-8")) > MAX_RESOLVED_SNAPSHOT_BYTES:
        raise ChartStyleDomainError(
            "chart_style_snapshot_too_large",
            status_code=400,
            details={"maximumBytes": MAX_RESOLVED_SNAPSHOT_BYTES},
        )
    return snapshot


def _issue(path: str, severity: str, code: str, **details: Any) -> dict[str, Any]:
    return {"path": path, "severity": severity, "code": code, "details": details}


def _palette_validation(mode: str, palette: list[str], surface: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    path = f"colors.{mode}.categorical"
    if not 6 <= len(palette) <= 8:
        blockers.append(_issue(path, "blocker", "palette.count", minimum=6, maximum=8, actual=len(palette)))
    if len(set(palette)) != len(palette):
        blockers.append(_issue(path, "blocker", "palette.duplicate_color"))
    # This 3:1 floor governs the palette itself — the colours used when a chart has no
    # protagonist. The renderer additionally fades non-lead series toward the surface
    # (`lib/chart/presentation.ts` `mutedOf`, 30% of the original), landing at 1.4–1.7:1,
    # and that is deliberately NOT held to this floor: a faded series is decorative
    # context. Decided 2026-09-10; see chart-style-customization-2026-09-06/prd.md §8.2.
    # Reporting "the gate rejects 2.24 but the product paints 1.39" as a defect has
    # already happened once — the two numbers measure different things.
    for index, color in enumerate(palette):
        actual = contrast_ratio(color, surface)
        if actual < 3.0:
            blockers.append(_issue(f"{path}.{index}", "blocker", "palette.surface_contrast", minimum=3.0, actual=round(actual, 6)))
    for (first_index, first), (second_index, second) in combinations(enumerate(palette), 2):
        delta = delta_e_ok(first, second)
        if delta < 0.02:
            blockers.append(
                _issue(
                    path,
                    "blocker",
                    "palette.pairwise_delta",
                    firstIndex=first_index,
                    secondIndex=second_index,
                    minimum=0.02,
                    actual=round(delta, 6),
                )
            )
    for index, (first, second) in enumerate(zip(palette, palette[1:], strict=False)):
        delta = delta_e_ok(first, second)
        if 0.02 <= delta <= 0.06:
            warnings.append(_issue(path, "warning", "palette.adjacent_delta", firstIndex=index, secondIndex=index + 1, actual=round(delta, 6)))
        grayscale_delta = abs(relative_luminance(first) - relative_luminance(second))
        if grayscale_delta < 0.08:
            warnings.append(
                _issue(
                    path,
                    "warning",
                    "palette.adjacent_grayscale",
                    firstIndex=index,
                    secondIndex=index + 1,
                    actual=round(grayscale_delta, 6),
                )
            )
    for deficiency in _CVD_DEFICIENCIES:
        simulated = [simulate_vision(color, deficiency) for color in palette]
        minimum = min((delta_e_ok(first, second) for first, second in combinations(simulated, 2)), default=1.0)
        if minimum < 0.045:
            warnings.append(_issue(path, "warning", "palette.cvd_separation", deficiency=deficiency, actual=round(minimum, 6)))
    return blockers, warnings


def _distinctness_info(mode: str, palette: list[str]) -> list[dict[str, Any]]:
    """Report color pairs a reader cannot reliably tell apart (CIEDE2000, worst vision).

    The 3:1 floor above only asks whether each color stands out from the surface; two
    colors can both pass it and still look identical to each other. Audit 2026-09-28
    measured exactly that on a built-in preset (``analytical-neutral/dark`` slots 0/1:
    ΔE2000 4.2 under normal vision), so every two-series dark chart in that preset was
    ambiguous while the gate stayed green.

    Severity ``info`` on purpose: a warning forces an acknowledgement on publish / restore
    (``chart_style_warning_ack_required``), and every team style frozen on an older preset
    would hit it. Nothing breaks either way: charts with colliding colors get a fill pattern
    / dash from the presentation resolver (``colorOnlySeriesCapacity``).
    """
    path = f"colors.{mode}.categorical"
    issues: list[dict[str, Any]] = []
    normal = [vision_labs(color, ("normal",)) for color in palette]
    cvd = [vision_labs(color, ("protan", "deutan")) for color in palette]
    for first, second in combinations(range(len(palette)), 2):
        normal_delta = labs_distinctness(normal[first], normal[second])
        cvd_delta = labs_distinctness(cvd[first], cvd[second])
        if normal_delta < BASE_NORMAL_TARGET or cvd_delta < BASE_CVD_TARGET:
            issues.append(
                _issue(
                    path,
                    "info",
                    "palette.distinctness",
                    firstIndex=first,
                    secondIndex=second,
                    normal=round(normal_delta, 2),
                    cvd=round(cvd_delta, 2),
                    normalMinimum=BASE_NORMAL_TARGET,
                    cvdMinimum=BASE_CVD_TARGET,
                )
            )
    return issues


def _stable_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {"blocker": 0, "warning": 1, "info": 2}
    return sorted(
        issues,
        key=lambda issue: (
            issue["path"],
            rank[issue["severity"]],
            issue["code"],
            canonical_json(issue["details"]),
        ),
    )


def validate_resolved_snapshot(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    light = snapshot["colors"]["light"]["categorical"]
    dark = snapshot["colors"]["dark"]["categorical"]
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []
    for mode, palette, surface in (("light", light, LIGHT_STANDARD_SURFACE), ("dark", dark, DARK_STANDARD_SURFACE)):
        mode_blockers, mode_warnings = _palette_validation(mode, palette, surface)
        blockers.extend(mode_blockers)
        warnings.extend(mode_warnings)
        info.extend(_distinctness_info(mode, palette))
    if len(light) != len(dark):
        blockers.append(
            _issue(
                "colors",
                "blocker",
                "palette.mode_length_mismatch",
                lightCount=len(light),
                darkCount=len(dark),
            )
        )
    return {"blockers": _stable_issues(blockers), "warnings": _stable_issues(warnings), "info": _stable_issues(info)}


def validation_fingerprint(
    *,
    source: dict[str, Any],
    authoring_base_snapshot_checksum: str,
    validation: dict[str, list[dict[str, Any]]],
) -> str:
    return sha256_json(
        {
            "source": source,
            "authoringBaseSnapshotChecksum": authoring_base_snapshot_checksum,
            "validationPolicyVersion": VALIDATION_POLICY_VERSION,
            "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
            "warnings": validation["warnings"],
        }
    )


def evaluate_chart_style_candidate(
    *,
    source: Any,
    authoring_base_snapshot: dict[str, Any],
    authoring_base_snapshot_checksum: str,
) -> dict[str, Any]:
    normalized = normalize_authoring_source(source)
    snapshot = materialize_chart_style(authoring_base_snapshot, normalized["overrides"])
    validation = validate_resolved_snapshot(snapshot)
    fingerprint = validation_fingerprint(
        source=normalized,
        authoring_base_snapshot_checksum=authoring_base_snapshot_checksum,
        validation=validation,
    )
    publishable = not validation["blockers"]
    return {
        "normalizedAuthoringSource": normalized,
        "candidateSnapshot": snapshot if publishable else None,
        "candidateSnapshotChecksum": sha256_json(snapshot) if publishable else None,
        "validationFingerprint": fingerprint,
        "validation": validation,
        "surfaceEvidence": standard_surface_evidence(snapshot) if publishable else None,
    }


def _minimum_pair_delta(palette: list[str]) -> float:
    return min((delta_e_ok(first, second) for first, second in combinations(palette, 2)), default=1.0)


def _minimum_adjacent_grayscale(palette: list[str]) -> float:
    return min(
        (abs(relative_luminance(first) - relative_luminance(second)) for first, second in zip(palette, palette[1:], strict=False)),
        default=1.0,
    )


def _qa_evidence(palette: list[str], surface: str, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    cvd_minimums: dict[str, float] = {}
    for deficiency in _CVD_DEFICIENCIES:
        simulated = [simulate_vision(color, deficiency) for color in palette]
        cvd_minimums[deficiency] = round(_minimum_pair_delta(simulated), 6)
    return {
        "policyVersion": COLOR_QA_POLICY_VERSION,
        "surfaceChecksum": sha256_json({"surface": surface}),
        "distinctColorCount": len(set(palette)),
        "minimumContrast": round(min(contrast_ratio(color, surface) for color in palette), 6),
        "minimumPairwiseDeltaE": round(_minimum_pair_delta(palette), 6),
        "minimumAdjacentGrayscaleDelta": round(_minimum_adjacent_grayscale(palette), 6),
        "cvdMinimumDeltaE": cvd_minimums,
        "colorOnlySeriesCapacity": color_only_series_capacity(palette),
        "hardInvariantsPass": len(set(palette)) == len(palette)
        and min(contrast_ratio(color, surface) for color in palette) >= 3.0
        and _minimum_pair_delta(palette) >= 0.02,
        "warningCodes": sorted({issue["code"] for issue in warnings}),
    }


MAX_DISTINCT_SERIES_COLORS: Final = 16
PALETTE_OVERFLOW_RECIPE_VERSION: Final = "palette-overflow/2"
_OVERFLOW_LIGHTNESS_STEPS: Final = 9
_OVERFLOW_CHROMA_STEPS: Final = 4
_OVERFLOW_HUE_STEP_DEGREES: Final = 10


def _overflow_candidates(palette: tuple[str, ...], surface: str) -> list[str]:
    """Colors inside the base palette's own lightness / chroma band that pass 3:1 on ``surface``.

    Staying inside the band keeps the preset's character: a muted preset stays muted.
    Widening it (measured 2026-10-01: lightness ±0.06, chroma ×1.15) bought almost no
    extra distinctness and produced saturated "generic categorical" hues such as
    ``#00F3FD`` in ``bowhead-editorial/dark``; the second channel (fill pattern / line
    dash) is the answer once the band runs out, not louder colors.
    """

    coordinates = [hex_to_oklch(color) for color in palette]
    lightness_low = max(0.18, min(item[0] for item in coordinates))
    lightness_high = min(0.92, max(item[0] for item in coordinates))
    chroma_low = max(0.02, min(item[1] for item in coordinates))
    chroma_high = min(0.2, max(chroma_low, max(item[1] for item in coordinates)))
    candidates: set[str] = set()
    for lightness_step in range(_OVERFLOW_LIGHTNESS_STEPS):
        lightness = lightness_low + (lightness_high - lightness_low) * lightness_step / (_OVERFLOW_LIGHTNESS_STEPS - 1)
        for chroma_step in range(_OVERFLOW_CHROMA_STEPS):
            chroma = chroma_low + (chroma_high - chroma_low) * chroma_step / (_OVERFLOW_CHROMA_STEPS - 1)
            for hue in range(0, 360, _OVERFLOW_HUE_STEP_DEGREES):
                candidate = oklch_to_hex(lightness, chroma, float(hue))
                if contrast_ratio(candidate, surface) >= 3.0:
                    candidates.add(candidate)
    return sorted(candidates)


def _normalized_distinctness(first: tuple[Any, ...], second: tuple[Any, ...]) -> float:
    """Distance as a fraction of the targets (normal vision first, then protan / deutan): 1.0 just meets both."""

    return min(
        labs_distinctness(first[:1], second[:1]) / BASE_NORMAL_TARGET,
        labs_distinctness(first[1:], second[1:]) / BASE_CVD_TARGET,
    )


@lru_cache(maxsize=128)
def _overflow_palette_cached(palette: tuple[str, ...], surface: str) -> tuple[str, ...]:
    pool = [candidate for candidate in _overflow_candidates(palette, surface) if candidate not in palette]
    pool_labs = [vision_labs(candidate) for candidate in pool]
    expanded = list(palette)
    # Running minimum distance from every candidate to every color selected so far.
    nearest = [min(_normalized_distinctness(labs, vision_labs(other)) for other in palette) for labs in pool_labs]
    while len(expanded) < MAX_DISTINCT_SERIES_COLORS and pool:
        # Farthest-point step; the hex breaks exact ties so the result is deterministic.
        chosen = max(range(len(pool)), key=lambda index: (nearest[index], pool[index]))
        chosen_labs = pool_labs[chosen]
        expanded.append(pool[chosen])
        del pool[chosen], pool_labs[chosen], nearest[chosen]
        nearest = [min(current, _normalized_distinctness(labs, chosen_labs)) for current, labs in zip(nearest, pool_labs, strict=True)]
    return tuple(expanded)


def _overflow_palette(palette: list[str], *, surface: str) -> list[str]:
    """Expand a reviewed base palette to 16 runtime colors, choosing each by distinctness.

    ``palette-overflow/1`` walked OKLCH in fixed steps and never looked at the colors it
    produced: on 116 the 16-color ``clean-blue/light`` projection fell to ΔE2000 5.4
    (normal) / 2.3 (with color-vision deficiency) from the 11th color on. Version 2 adds
    each color by farthest-point selection (CIEDE2000, normal vision and simulated
    protan / deutan, relative to ``BASE_NORMAL_TARGET`` / ``BASE_CVD_TARGET``) from the
    base palette's lightness / chroma band, so every prefix stays as separated as the
    band allows. How many of them actually meet the targets is reported as
    ``colorOnlySeriesCapacity``; series past it get a second channel.
    """

    return list(_overflow_palette_cached(tuple(color.upper() for color in palette), surface.upper()))


def color_only_series_capacity(palette: list[str]) -> int:
    """How many leading palette colors a chart may use with color as the only channel.

    The measured distinguishable prefix, clamped by the product-wide cap
    ``nonColor.colorOnlySeriesCap`` in ``config/chart_style/presentation_policy.json``
    (``null`` = no cap; ``6`` = "texture from the 7th series"). Both resolvers apply the
    same cap again, so changing that one value switches screen and export together.
    """

    measured = distinct_prefix_length(palette, normal_target=BASE_NORMAL_TARGET, cvd_target=BASE_CVD_TARGET)
    cap = CHART_STYLE_PRESENTATION_POLICY["nonColor"]["colorOnlySeriesCap"]
    return measured if cap is None else max(1, min(measured, int(cap)))


def _repair_candidates(requested: str) -> list[str]:
    lightness, chroma, hue = hex_to_oklch(requested)
    candidates = [requested]
    # Preserve hue and chroma first; the ordering is the deterministic
    # approximation of "minimum requested delta" used by chart-surface/1.
    for step in range(1, 101):
        distance = step / 100
        for candidate_lightness in (lightness - distance, lightness + distance):
            if 0.08 <= candidate_lightness <= 0.94:
                candidates.append(oklch_to_hex(candidate_lightness, min(chroma, 0.22), hue))
    return sorted(
        set(candidates),
        key=lambda candidate: (delta_e_ok(requested, candidate), candidate),
    )


def _color_safe_candidates(requested: str) -> list[str]:
    requested_l, requested_c, requested_h = hex_to_oklch(requested)
    candidates: list[tuple[float, str]] = []
    for lightness in (0.2, 0.28, 0.36, 0.44, 0.56, 0.64, 0.72, 0.8, 0.88):
        for chroma in (0.08, 0.14, 0.2):
            for hue in range(0, 360, 15):
                value = oklch_to_hex(lightness, chroma, float(hue))
                candidates.append(
                    (
                        abs(lightness - requested_l)
                        + abs(chroma - requested_c) * 0.35
                        + min(abs(hue - requested_h), 360 - abs(hue - requested_h)) / 3600,
                        value,
                    )
                )
    return list(dict.fromkeys(value for _, value in sorted(candidates)))


def _fits_runtime_palette(
    candidate: str,
    *,
    surface: str,
    selected: list[str],
) -> bool:
    return contrast_ratio(candidate, surface) >= 3.0 and candidate not in selected and all(delta_e_ok(candidate, other) >= 0.02 for other in selected)


def _repair_runtime_palette(
    requested_palette: list[str],
    *,
    surface: str,
    mode: str,
    color_scheme_checksum: str,
    base_color_count: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    selected: list[str] = []
    adjustments: list[dict[str, Any]] = []
    fallback_base = CHART_STYLE_PRESETS["analytical-neutral"]["tokens"][mode]["palette"]
    fallback_palette = _overflow_palette(fallback_base, surface=surface)
    for index, requested in enumerate(requested_palette):
        candidate = next(
            (value for value in _repair_candidates(requested) if _fits_runtime_palette(value, surface=surface, selected=selected)),
            None,
        )
        fallback_used = False
        if candidate is None:
            fallback_requested = fallback_palette[index]
            candidate = next(
                (
                    value
                    for value in [
                        *_repair_candidates(fallback_requested),
                        *_color_safe_candidates(fallback_requested),
                    ]
                    if _fits_runtime_palette(
                        value,
                        surface=surface,
                        selected=selected,
                    )
                ),
                None,
            )
            fallback_used = True
        if candidate is None:
            raise ChartStyleDomainError(
                "chart_style_surface_projection_unavailable",
                details={"mode": mode},
            )
        selected.append(candidate)
        if candidate == requested:
            continue
        if fallback_used:
            reason = "accessibility.palette_color_safe_fallback"
        elif contrast_ratio(requested, surface) < 3.0:
            reason = "accessibility.palette_surface_contrast_adjusted"
        else:
            reason = "accessibility.palette_separation_repaired"
        adjustments.append(
            {
                "path": (f"colors.{mode}.categorical.{index}" if index < base_color_count else f"categoricalSeriesPalette.{index}"),
                "requested": requested,
                "effective": candidate,
                "reason": reason,
                "effectiveColorSchemeChecksum": color_scheme_checksum,
            }
        )
    return selected, adjustments


def actual_surface_projection(
    snapshot: dict[str, Any],
    *,
    mode: str,
    surface: str,
    color_scheme_checksum: str,
) -> dict[str, Any]:
    """Project one immutable snapshot onto the actual structural surface."""

    if mode not in {"light", "dark"} or _HEX_RE.fullmatch(surface) is None:
        raise ChartStyleDomainError(
            "chart_style_surface_projection_unavailable",
            details={"mode": mode},
        )
    normalized_surface = surface.upper()
    colors = copy.deepcopy(snapshot["colors"][mode])
    requested_base = list(colors["categorical"])
    requested_expanded = _overflow_palette(requested_base, surface=normalized_surface)
    effective, adjustments = _repair_runtime_palette(
        requested_expanded,
        surface=normalized_surface,
        mode=mode,
        color_scheme_checksum=color_scheme_checksum,
        base_color_count=len(requested_base),
    )
    colors["categorical"] = effective[: len(requested_base)]
    _, warnings = _palette_validation(mode, effective, normalized_surface)
    evidence = _qa_evidence(effective, normalized_surface, warnings)
    if not evidence["hardInvariantsPass"]:
        raise ChartStyleDomainError(
            "chart_style_surface_projection_unavailable",
            details={"mode": mode},
        )
    return {
        "colors": colors,
        "categoricalSeriesPalette": effective,
        "maxDistinctSeriesColors": MAX_DISTINCT_SERIES_COLORS,
        "paletteOverflowRecipeVersion": PALETTE_OVERFLOW_RECIPE_VERSION,
        "finalPaletteQA": evidence,
        "adjustments": adjustments,
    }


def standard_surface_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    validation = validate_resolved_snapshot(snapshot)
    modes: dict[str, Any] = {}
    surfaces = {"light": LIGHT_STANDARD_SURFACE, "dark": DARK_STANDARD_SURFACE}
    for mode, surface in surfaces.items():
        colors = snapshot["colors"][mode]
        palette = colors["categorical"]
        mode_warnings = [issue for issue in validation["warnings"] if issue["path"].startswith(f"colors.{mode}.")]
        modes[mode] = {
            "colors": colors,
            "categoricalSeriesPalette": palette,
            "maxDistinctSeriesColors": MAX_DISTINCT_SERIES_COLORS,
            "paletteOverflowRecipeVersion": PALETTE_OVERFLOW_RECIPE_VERSION,
            "finalPaletteQA": _qa_evidence(palette, surface, mode_warnings),
            "adjustments": [],
        }
    return {
        "contractVersion": SURFACE_CONTRACT_VERSION,
        "surfaceChecksums": {mode: sha256_json({"surface": surface}) for mode, surface in surfaces.items()},
        "modes": modes,
    }


def revision_checksum(
    *,
    source: dict[str, Any],
    base_preset_id: str,
    base_preset_revision: str,
    base_preset_checksum: str,
    authoring_base_snapshot: dict[str, Any],
    authoring_base_snapshot_checksum: str,
    resolved_snapshot: dict[str, Any],
    snapshot_checksum: str,
    validation: dict[str, Any],
) -> str:
    return sha256_json(
        {
            "schemaVersion": STYLE_SCHEMA_VERSION,
            "tokenSchemaVersion": TOKEN_SCHEMA_VERSION,
            "resolverPolicyVersion": RESOLVER_POLICY_VERSION,
            "validationPolicyVersion": VALIDATION_POLICY_VERSION,
            "name": source["name"],
            "description": source["description"],
            "basePresetId": base_preset_id,
            "basePresetRevision": base_preset_revision,
            "basePresetChecksum": base_preset_checksum,
            "authoringBaseSnapshot": authoring_base_snapshot,
            "authoringBaseSnapshotChecksum": authoring_base_snapshot_checksum,
            "overrides": source["overrides"],
            "resolvedSnapshot": resolved_snapshot,
            "snapshotChecksum": snapshot_checksum,
            "validation": validation,
        }
    )


__all__ = [
    "CHART_STYLE_KINDS",
    "COLOR_QA_POLICY_VERSION",
    "DARK_PALETTE_RECIPE_VERSION",
    "DARK_STANDARD_SURFACE",
    "LIGHT_STANDARD_SURFACE",
    "MAX_AUTHORING_SOURCE_BYTES",
    "MAX_DISTINCT_SERIES_COLORS",
    "MAX_RESOLVED_SNAPSHOT_BYTES",
    "PALETTE_OVERFLOW_RECIPE_VERSION",
    "SURFACE_CONTRACT_VERSION",
    "actual_surface_projection",
    "built_in_authoring_base",
    "color_only_series_capacity",
    "evaluate_chart_style_candidate",
    "materialize_chart_style",
    "normalize_authoring_source",
    "normalize_style_name",
    "revision_checksum",
    "standard_surface_evidence",
    "validate_resolved_snapshot",
    "validation_fingerprint",
]
