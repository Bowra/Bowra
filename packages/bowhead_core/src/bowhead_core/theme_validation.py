"""Validation and bounded repair for compiled team-branding color schemes."""

from __future__ import annotations

import copy
import math
from typing import Any, Final

from bowhead_core.theme_compiler import (
    SCHEME_TOKEN_KEYS,
    ThemeCompilerError,
    checksum_compiled_scheme,
    hex_to_oklch,
    oklch_to_hex,
)

_TEXT_CONTRAST: Final = 4.5
_UI_CONTRAST: Final = 3.0
_SURFACE_DELTA: Final = 0.012
_PAGE_SURFACE_DELTA: Final = 0.020
# Minimum ratio between ink3/ink4 contrast on background: promoting ink4 to body text must not collapse both into one gray.
_INK_LADDER_STEP: Final = 1.15

# feedback-triage-2026-08-24 batch 5: the matrix originally stopped at ink2.
#
# A customer reported poor visibility in dark mode; measured compiled team palettes on live 116:
#   dark ink3 #827968 on background #11100F = 4.42:1 (just below body-text AA)
#   dark ink4 #544C3C on background #11100F = 2.24:1 (below even the 3.0 UI threshold)
# ink3/ink4 are the two levels used for 10–11px meta rows, placeholders, table footers and attribution
# (`--c-ink-3`/`--c-ink-4`, with aliases `--text-muted`/`--text-subtle`).
#
# ★ Why the gate missed it: the gate worked and `_repair_role` already supported automatic brightening,
#   but **these two levels never entered the matrix**, leaving every compiled result unchecked for them.
#   Having a gate does not mean it covers this issue.
#
# Both ink3/ink4 require the 4.5 body-text threshold. ink4 formerly used UI/AA-large 3.0 as a subdued layer,
# but carries about 550 instances of actual 10–12px text (times, counts, footers, explanations); 3.3–3.9:1 is unreadable
# (UI redesign P0-1, docs/design/界面改版-Apple设计方向.md). Pure decoration uses only ink5, outside this matrix.
_TEXT_PAIRS: Final = (
    ("ink", "background"),
    ("ink", "surface"),
    ("ink2", "background"),
    ("ink2", "surface"),
    ("ink3", "background"),
    ("ink3", "surface"),
    ("ink4", "background"),
    ("ink4", "surface"),
    ("accentInk", "accent"),
    ("accentInk", "accentHover"),
    ("accent", "background"),
    ("positive", "positiveSoft"),
    ("warning", "warningSoft"),
    ("error", "errorSoft"),
    ("info", "infoSoft"),
)
_UI_PAIRS: Final = (
    ("line", "background"),
    ("line", "surface"),
    ("focus", "surface"),
    ("accent", "accentTint"),
)


def repair_and_validate_theme(
    source: dict[str, Any],
    compiled: dict[str, Any],
) -> dict[str, Any]:
    """Repair allowed derived roles, then return a complete validation report."""

    repaired = copy.deepcopy(compiled)
    repair_warnings: list[dict[str, Any]] = []
    for mode in ("light", "dark"):
        scheme = repaired[mode]
        _repair_text_roles(scheme, mode=mode, warnings=repair_warnings)
        _repair_line_and_focus(scheme, mode=mode, warnings=repair_warnings)

    validation = validate_compiled_theme(source, repaired)
    existing_warnings = list(compiled.get("validation", {}).get("warnings", []))
    validation["warnings"] = existing_warnings + repair_warnings + validation["warnings"]
    repaired["validation"] = validation
    return repaired


def apply_text_contrast_floor(compiled: dict[str, Any]) -> dict[str, Any] | None:
    """Return a read-time copy whose ink4 and warning meet text contrast, or None if compliant.

    Published revisions keep their compiled snapshot, so raising the ink4 floor in the
    compiler only reaches themes published afterwards.  The resolver applies this floor
    when it serves any snapshot; stored revisions are never rewritten.
    """

    upgraded = copy.deepcopy(compiled)
    changed = False
    for mode, direction in (("light", "darker"), ("dark", "lighter")):
        scheme = upgraded.get(mode)
        if not isinstance(scheme, dict):
            return None
        before = {role: scheme.get(role) for role in ("ink3", "ink4", "warning")}
        for role in ("ink4", "warning"):
            _repair_role(
                scheme,
                role,
                against=("background", "surface"),
                minimum=_TEXT_CONTRAST,
                direction=direction,
                warnings=[],
                mode=mode,
            )
        _keep_ink_ladder_step(scheme, direction=direction, warnings=[], mode=mode)
        changed = changed or any(scheme.get(role) != value for role, value in before.items())
    if not changed:
        return None
    upgraded["checksum"] = checksum_compiled_scheme(upgraded)
    return upgraded


def validate_compiled_theme(
    source: dict[str, Any],
    compiled: dict[str, Any],
) -> dict[str, Any]:
    """Run structural, semantic, and WCAG gates against a compiled scheme."""

    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    _validate_compiled_shape(compiled, blocking)
    if blocking:
        return _report(blocking, warnings)

    for mode in ("light", "dark"):
        scheme = compiled[mode]
        _validate_surface_semantics(mode, scheme, blocking)
        _validate_contrast_matrix(mode, scheme, blocking)
        _validate_overrides(mode, source, scheme, blocking)
        _validate_state_distinction(mode, scheme, blocking)

    light_average = _average_surface_lightness(compiled["light"])
    dark_average = _average_surface_lightness(compiled["dark"])
    if light_average < dark_average:
        blocking.append(
            _issue(
                code="light_dark_semantics_reversed",
                mode=None,
                path="light,dark",
                message="Light surfaces must remain lighter than Dark surfaces.",
                actual=light_average - dark_average,
                required=0.0,
                suggestion="Use light page/container colors for Light and dark colors for Dark.",
            )
        )

    _validate_accent_fidelity(source, compiled, warnings)
    return _report(blocking, warnings)


def contrast_ratio(foreground: str, background: str) -> float:
    """Return the unrounded WCAG 2 relative-luminance contrast ratio."""

    foreground_luminance = relative_luminance(foreground)
    background_luminance = relative_luminance(background)
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def relative_luminance(value: str) -> float:
    """Return WCAG relative luminance for an opaque sRGB hex color."""

    channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def delta_e_ok(first: str, second: str) -> float:
    """Return Euclidean ΔE in Oklab."""

    first_l, first_c, first_h = hex_to_oklch(first)
    second_l, second_c, second_h = hex_to_oklch(second)
    first_a = first_c * math.cos(math.radians(first_h))
    first_b = first_c * math.sin(math.radians(first_h))
    second_a = second_c * math.cos(math.radians(second_h))
    second_b = second_c * math.sin(math.radians(second_h))
    return math.sqrt((first_l - second_l) ** 2 + (first_a - second_a) ** 2 + (first_b - second_b) ** 2)


def _repair_text_roles(
    scheme: dict[str, str],
    *,
    mode: str,
    warnings: list[dict[str, Any]],
) -> None:
    text_direction = "darker" if mode == "light" else "lighter"
    # ⚠ Every matrix level must **also** enter repair; adding it only to the matrix makes existing team palettes
    # go from savable to blocked, trading readability problems for a more severe failure.
    for role in ("ink", "ink2", "ink3", "ink4"):
        _repair_role(
            scheme,
            role,
            against=("background", "surface"),
            minimum=_TEXT_CONTRAST,
            direction=text_direction,
            warnings=warnings,
            mode=mode,
        )
    _keep_ink_ladder_step(scheme, direction=text_direction, warnings=warnings, mode=mode)

    accent_ink = scheme["accentInk"]
    accent_direction = "darker" if mode == "light" else "lighter"
    for role in ("accent", "accentHover"):
        _repair_role(
            scheme,
            role,
            against=("background",),
            minimum=_TEXT_CONTRAST,
            direction=accent_direction,
            extra_foreground=accent_ink,
            warnings=warnings,
            mode=mode,
        )


def _repair_line_and_focus(
    scheme: dict[str, str],
    *,
    mode: str,
    warnings: list[dict[str, Any]],
) -> None:
    direction = "darker" if mode == "light" else "lighter"
    _repair_role(
        scheme,
        "line",
        against=("background", "surface"),
        minimum=_UI_CONTRAST,
        direction=direction,
        warnings=warnings,
        mode=mode,
    )
    _repair_role(
        scheme,
        "focus",
        against=("surface",),
        minimum=_UI_CONTRAST,
        direction=direction,
        warnings=warnings,
        mode=mode,
    )


def _keep_ink_ladder_step(
    scheme: dict[str, str],
    *,
    direction: str,
    warnings: list[dict[str, Any]],
    mode: str,
) -> None:
    """Push ink3 away from ink4 so the two muted tiers stay distinguishable."""

    if not all(isinstance(scheme.get(role), str) for role in ("ink3", "ink4", "background")):
        return
    target = contrast_ratio(scheme["ink4"], scheme["background"]) * _INK_LADDER_STEP
    _repair_role(
        scheme,
        "ink3",
        against=("background",),
        minimum=target,
        direction=direction,
        warnings=warnings,
        mode=mode,
    )


def _repair_role(
    scheme: dict[str, str],
    role: str,
    *,
    against: tuple[str, ...],
    minimum: float,
    direction: str,
    warnings: list[dict[str, Any]],
    mode: str,
    extra_foreground: str | None = None,
) -> None:
    original = scheme[role]
    if _meets_contrast_constraints(
        original,
        scheme,
        against=against,
        minimum=minimum,
        extra_foreground=extra_foreground,
    ):
        return

    lightness, chroma, hue = hex_to_oklch(original)
    candidates = range(1001)
    ordered = sorted(
        (candidate / 1000 for candidate in candidates),
        key=lambda candidate: (abs(candidate - lightness), candidate if direction == "darker" else -candidate),
    )
    for candidate_lightness in ordered:
        if direction == "darker" and candidate_lightness > lightness:
            continue
        if direction == "lighter" and candidate_lightness < lightness:
            continue
        candidate = oklch_to_hex(candidate_lightness, chroma, hue)
        if _meets_contrast_constraints(
            candidate,
            scheme,
            against=against,
            minimum=minimum,
            extra_foreground=extra_foreground,
        ):
            scheme[role] = candidate
            warnings.append(
                {
                    "code": "derived_role_adjusted",
                    "severity": "warning",
                    "mode": mode,
                    "path": f"{mode}.{role}",
                    "message": f"{role} was adjusted to meet contrast requirements.",
                    "input": original,
                    "actual": candidate,
                }
            )
            return


def _meets_contrast_constraints(
    candidate: str,
    scheme: dict[str, str],
    *,
    against: tuple[str, ...],
    minimum: float,
    extra_foreground: str | None,
) -> bool:
    if any(contrast_ratio(candidate, scheme[background]) < minimum for background in against):
        return False
    return extra_foreground is None or contrast_ratio(extra_foreground, candidate) >= minimum


def _validate_compiled_shape(
    compiled: dict[str, Any],
    blocking: list[dict[str, Any]],
) -> None:
    expected_top = {"tokenSchemaVersion", "generator", "light", "dark", "validation"}
    actual_top = set(compiled) - {"checksum"}
    if actual_top != expected_top:
        blocking.append(
            _issue(
                code="compiled_shape_invalid",
                mode=None,
                path="$",
                message="Compiled theme has an invalid top-level shape.",
                actual=sorted(actual_top),
                required=sorted(expected_top),
                suggestion="Recompile the source with the current server generator.",
            )
        )
        return
    if compiled.get("tokenSchemaVersion") != 2:
        blocking.append(
            _issue(
                code="token_schema_version_invalid",
                mode=None,
                path="tokenSchemaVersion",
                message="Unsupported token schema version.",
                actual=compiled.get("tokenSchemaVersion"),
                required=2,
                suggestion="Recompile the source with the current server generator.",
            )
        )

    for mode in ("light", "dark"):
        scheme = compiled.get(mode)
        if not isinstance(scheme, dict) or tuple(scheme) != SCHEME_TOKEN_KEYS:
            blocking.append(
                _issue(
                    code="token_keys_invalid",
                    mode=mode,
                    path=mode,
                    message=f"{mode.title()} must contain the exact canonical token key set.",
                    actual=list(scheme) if isinstance(scheme, dict) else type(scheme).__name__,
                    required=list(SCHEME_TOKEN_KEYS),
                    suggestion="Recompile the source; do not edit compiled tokens.",
                )
            )
            continue
        for role, value in scheme.items():
            try:
                lightness, _, _ = hex_to_oklch(value)
            except (ThemeCompilerError, TypeError):
                lightness = -1
            if lightness < 0:
                blocking.append(
                    _issue(
                        code="compiled_color_invalid",
                        mode=mode,
                        path=f"{mode}.{role}",
                        message="Compiled token must be an opaque #RRGGBB color.",
                        actual=value,
                        required="#RRGGBB",
                        suggestion="Recompile the source; do not edit compiled tokens.",
                    )
                )


def _validate_surface_semantics(
    mode: str,
    scheme: dict[str, str],
    blocking: list[dict[str, Any]],
) -> None:
    maximum = 1.0 if mode == "light" else 0.30
    minimum = 0.86 if mode == "light" else 0.04
    for role in ("background", "surface", "surface2", "surface3"):
        lightness = hex_to_oklch(scheme[role])[0]
        valid = minimum <= lightness <= maximum
        if not valid:
            blocking.append(
                _issue(
                    code="surface_lightness_out_of_range",
                    mode=mode,
                    path=f"{mode}.{role}",
                    message=f"{role} violates {mode.title()} surface semantics.",
                    actual=lightness,
                    required={"minimum": minimum, "maximum": maximum},
                    suggestion=f"Choose a {'lighter' if mode == 'light' else 'darker'} {role} color.",
                )
            )

    _require_delta(mode, scheme, "background", "surface", _PAGE_SURFACE_DELTA, blocking)
    _require_delta(mode, scheme, "surface", "surface2", _SURFACE_DELTA, blocking)
    _require_delta(mode, scheme, "surface2", "surface3", _SURFACE_DELTA, blocking)


def _validate_contrast_matrix(
    mode: str,
    scheme: dict[str, str],
    blocking: list[dict[str, Any]],
) -> None:
    for foreground, background in _TEXT_PAIRS:
        _require_contrast(mode, scheme, foreground, background, _TEXT_CONTRAST, blocking)
    for foreground, background in _UI_PAIRS:
        _require_contrast(mode, scheme, foreground, background, _UI_CONTRAST, blocking)


def _validate_overrides(
    mode: str,
    source: dict[str, Any],
    scheme: dict[str, str],
    blocking: list[dict[str, Any]],
) -> None:
    overrides = source.get("overrides", {}).get(mode, {})
    minimum, maximum = (0.86, 1.0) if mode == "light" else (0.04, 0.30)
    for role in overrides:
        lightness = hex_to_oklch(scheme[role])[0]
        if not minimum <= lightness <= maximum:
            blocking.append(
                _issue(
                    code="override_lightness_out_of_range",
                    mode=mode,
                    path=f"overrides.{mode}.{role}",
                    message=f"{mode.title()} {role} override is outside the allowed lightness range.",
                    actual=lightness,
                    required={"minimum": minimum, "maximum": maximum},
                    suggestion=f"Choose a {'lighter' if mode == 'light' else 'darker'} override.",
                )
            )


def _validate_state_distinction(
    mode: str,
    scheme: dict[str, str],
    blocking: list[dict[str, Any]],
) -> None:
    _require_delta(mode, scheme, "accent", "accentHover", _SURFACE_DELTA, blocking)


def _validate_accent_fidelity(
    source: dict[str, Any],
    compiled: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> None:
    primary_seed = source.get("primarySeed")
    if not isinstance(primary_seed, str):
        return
    _, seed_chroma, seed_hue = hex_to_oklch(primary_seed)
    if seed_chroma < 0.03:
        return
    for mode in ("light", "dark"):
        _, actual_chroma, actual_hue = hex_to_oklch(compiled[mode]["accent"])
        hue_difference = abs((actual_hue - seed_hue + 180) % 360 - 180)
        chroma_retention = actual_chroma / seed_chroma if seed_chroma else 1.0
        if hue_difference > 12 or chroma_retention < 0.60:
            warnings.append(
                {
                    "code": "accent_fidelity_adjusted",
                    "severity": "warning",
                    "mode": mode,
                    "path": f"{mode}.accent",
                    "message": "Actual accent differs materially from the input seed.",
                    "hueDifference": round(hue_difference, 3),
                    "chromaRetention": round(chroma_retention, 3),
                    "suggestion": "Review the actual Light and Dark accent previews before publishing.",
                }
            )


def _require_contrast(
    mode: str,
    scheme: dict[str, str],
    foreground: str,
    background: str,
    minimum: float,
    blocking: list[dict[str, Any]],
) -> None:
    actual = contrast_ratio(scheme[foreground], scheme[background])
    if actual >= minimum:
        return
    blocking.append(
        _issue(
            code="contrast_below_threshold",
            mode=mode,
            path=f"{mode}.{foreground}",
            message=f"{foreground} does not have enough contrast against {background}.",
            actual=actual,
            required=minimum,
            suggestion=f"Adjust {foreground} or {background} until contrast is at least {minimum}:1.",
            foreground=foreground,
            background=background,
        )
    )


def _require_delta(
    mode: str,
    scheme: dict[str, str],
    first: str,
    second: str,
    minimum: float,
    blocking: list[dict[str, Any]],
) -> None:
    actual = delta_e_ok(scheme[first], scheme[second])
    if actual >= minimum:
        return
    blocking.append(
        _issue(
            code="state_or_surface_not_distinct",
            mode=mode,
            path=f"{mode}.{first},{mode}.{second}",
            message=f"{first} and {second} are not visually distinct enough.",
            actual=actual,
            required=minimum,
            suggestion=f"Increase the visual difference between {first} and {second}.",
            foreground=first,
            background=second,
        )
    )


def _average_surface_lightness(scheme: dict[str, str]) -> float:
    return sum(hex_to_oklch(scheme[role])[0] for role in ("background", "surface", "surface2", "surface3")) / 4


def _report(
    blocking: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "blocking" if blocking else ("warning" if warnings else "valid"),
        "blockingCount": len(blocking),
        "warningCount": len(warnings),
        "blocking": blocking,
        "warnings": warnings,
    }


def _issue(
    *,
    code: str,
    mode: str | None,
    path: str,
    message: str,
    actual: Any,
    required: Any,
    suggestion: str,
    foreground: str | None = None,
    background: str | None = None,
) -> dict[str, Any]:
    issue = {
        "code": code,
        "severity": "blocking",
        "mode": mode,
        "path": path,
        "message": message,
        "actual": round(actual, 6) if isinstance(actual, float) else actual,
        "required": required,
        "suggestion": suggestion,
    }
    if foreground is not None:
        issue["foreground"] = foreground
    if background is not None:
        issue["background"] = background
    return issue


__all__ = [
    "apply_text_contrast_floor",
    "contrast_ratio",
    "delta_e_ok",
    "relative_luminance",
    "repair_and_validate_theme",
    "validate_compiled_theme",
]
