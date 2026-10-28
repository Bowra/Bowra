"""Pure personal color overlay compiler for the canonical 24-token scheme."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Final

from bowhead_core.personal_theme_models import (
    PERSONAL_ACCENT_REGISTRY_V1,
    PERSONAL_BACKGROUND_REGISTRY_V1,
    PersonalThemeModelError,
    normalize_personal_theme_overrides,
)
from bowhead_core.theme_compiler import (
    SCHEME_TOKEN_KEYS,
    canonical_json,
    hex_to_oklch,
    oklch_to_hex,
)
from bowhead_core.theme_validation import (
    contrast_ratio,
    delta_e_ok,
    repair_and_validate_theme,
)

PERSONAL_OVERLAY_VERSION: Final = 1
PERSONAL_OVERLAY_GENERATOR_ID: Final = "bowhead-personal-overlay"

_SEMANTIC_ROLES: Final = ("positive", "warning", "error", "info")
_TEXT_CONTRAST: Final = 4.5
_HEX_LENGTH: Final = 7
_CHECKSUM_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


class PersonalThemeCompilerError(ValueError):
    """Raised when the trusted base scheme cannot be overlaid."""


class PersonalThemeBaseSchemeError(PersonalThemeCompilerError):
    """Raised when the trusted base scheme is structurally incompatible."""


@dataclass(frozen=True, slots=True)
class PersonalThemeCompileResult:
    color_scheme: dict[str, Any]
    validation: dict[str, Any]
    accent_source: str
    background_source: str
    input_seeds: dict[str, Any]
    personal_overlay_version: int = PERSONAL_OVERLAY_VERSION


class PersonalThemeCompiler:
    """Apply normalized personal background then accent without performing I/O."""

    def compile(
        self,
        base_scheme: dict[str, Any],
        overrides: dict[str, Any],
    ) -> PersonalThemeCompileResult:
        _validate_base_scheme(base_scheme)
        try:
            normalized = normalize_personal_theme_overrides(overrides)
        except PersonalThemeModelError as exc:
            raise PersonalThemeCompilerError(str(exc)) from exc

        background = normalized.get("background")
        accent = normalized.get("accent")
        if background is None and accent is None:
            return PersonalThemeCompileResult(
                color_scheme=copy.deepcopy(base_scheme),
                validation=_valid_report(),
                accent_source="base",
                background_source="base",
                input_seeds={},
            )

        light = {key: base_scheme["light"][key] for key in SCHEME_TOKEN_KEYS}
        dark = {key: base_scheme["dark"][key] for key in SCHEME_TOKEN_KEYS}
        input_seeds: dict[str, Any] = {}
        warnings: list[dict[str, Any]] = []
        semantic_blocking: list[dict[str, Any]] = []

        if background is not None:
            background_seeds = _resolve_background_seeds(background)
            input_seeds["background"] = copy.deepcopy(background_seeds)
            light.update(_derive_background_mode(background_seeds["light"], mode="light"))
            dark.update(_derive_background_mode(background_seeds["dark"], mode="dark"))
            for mode, scheme in (("light", light), ("dark", dark)):
                mode_warnings, mode_blocking = _adjust_semantic_foregrounds(scheme, mode=mode)
                warnings.extend(mode_warnings)
                semantic_blocking.extend(mode_blocking)

        if accent is not None:
            accent_seed = _resolve_accent_seed(accent)
            input_seeds["accent"] = accent_seed
            light.update(_derive_accent_mode(accent_seed, light, mode="light"))
            dark.update(_derive_accent_mode(accent_seed, dark, mode="dark"))

        compiled: dict[str, Any] = {
            "tokenSchemaVersion": 2,
            "generator": {
                "id": PERSONAL_OVERLAY_GENERATOR_ID,
                "version": str(PERSONAL_OVERLAY_VERSION),
            },
            "light": light,
            "dark": dark,
            "validation": {"blocking": [], "warnings": warnings},
        }
        if background is None:
            repaired = compiled
            validation = _validate_accent_overlay(light, dark)
        else:
            validation_source: dict[str, Any] = {"overrides": {"light": {}, "dark": {}}}
            if accent is not None:
                validation_source["primarySeed"] = input_seeds["accent"]
            repaired = repair_and_validate_theme(validation_source, compiled)
            validation = repaired["validation"]
            if semantic_blocking:
                validation["blocking"].extend(semantic_blocking)
                validation["blockingCount"] = len(validation["blocking"])
                validation["status"] = "blocking"
            validation["warningCount"] = len(validation["warnings"])
            if not validation["blocking"] and validation["warnings"]:
                validation["status"] = "warning"

        color_scheme: dict[str, Any] = {
            "tokenSchemaVersion": 2,
            "light": repaired["light"],
            "dark": repaired["dark"],
        }
        color_scheme["checksum"] = _overlay_checksum(color_scheme)
        return PersonalThemeCompileResult(
            color_scheme=color_scheme,
            validation=validation,
            accent_source="personal" if accent is not None else "base",
            background_source="personal" if background is not None else "base",
            input_seeds=input_seeds,
        )


def _validate_base_scheme(base_scheme: dict[str, Any]) -> None:
    if not isinstance(base_scheme, dict):
        raise PersonalThemeBaseSchemeError("base color scheme must be an object")
    expected_top = {"tokenSchemaVersion", "light", "dark", "checksum"}
    if set(base_scheme) != expected_top or base_scheme.get("tokenSchemaVersion") != 2:
        raise PersonalThemeBaseSchemeError("base color scheme shape is invalid")
    checksum = base_scheme.get("checksum")
    if not isinstance(checksum, str) or not _CHECKSUM_RE.fullmatch(checksum):
        raise PersonalThemeBaseSchemeError("base color scheme checksum is invalid")
    for mode in ("light", "dark"):
        scheme = base_scheme.get(mode)
        if not isinstance(scheme, dict) or set(scheme) != set(SCHEME_TOKEN_KEYS):
            raise PersonalThemeBaseSchemeError(f"base {mode} token keys are invalid")
        for value in scheme.values():
            if not isinstance(value, str) or len(value) != _HEX_LENGTH:
                raise PersonalThemeBaseSchemeError(f"base {mode} token is invalid")
            try:
                hex_to_oklch(value)
            except (TypeError, ValueError) as exc:
                raise PersonalThemeBaseSchemeError(f"base {mode} token is invalid") from exc


def _resolve_background_seeds(background: dict[str, Any]) -> dict[str, str]:
    if background["kind"] == "preset":
        return copy.deepcopy(PERSONAL_BACKGROUND_REGISTRY_V1[background["id"]])
    return {"light": background["light"], "dark": background["dark"]}


def _resolve_accent_seed(accent: dict[str, Any]) -> str:
    if accent["kind"] == "preset":
        return PERSONAL_ACCENT_REGISTRY_V1[accent["id"]]
    return accent["seed"]


def _derive_background_mode(seed: str, *, mode: str) -> dict[str, str]:
    page_lightness, page_chroma, page_hue = hex_to_oklch(seed)
    neutral_chroma = min(0.025, page_chroma)
    if mode == "light":
        if page_lightness >= 0.975:
            surfaces = (page_lightness - 0.028, page_lightness - 0.054, page_lightness - 0.080)
        else:
            surfaces = (
                min(1.0, page_lightness + 0.028),
                page_lightness - 0.028,
                page_lightness - 0.054,
            )
        ink_lightness = (0.18, 0.34, 0.46, 0.60)
        line_lightness = (0.58, 0.72)
    else:
        available = max(0.0, 0.295 - page_lightness)
        step = min(0.040, available / 3)
        surfaces = (
            page_lightness + step,
            page_lightness + step * 2,
            page_lightness + step * 3,
        )
        ink_lightness = (0.92, 0.74, 0.58, 0.42)
        line_lightness = (0.42, 0.30)

    def neutral(lightness: float, chroma: float = neutral_chroma) -> str:
        return oklch_to_hex(lightness, chroma, page_hue)

    return {
        "background": seed,
        "surface": neutral(surfaces[0]),
        "surface2": neutral(surfaces[1]),
        "surface3": neutral(surfaces[2]),
        "line": neutral(line_lightness[0]),
        "line2": neutral(line_lightness[1]),
        "ink": neutral(ink_lightness[0]),
        "ink2": neutral(ink_lightness[1]),
        "ink3": neutral(ink_lightness[2]),
        "ink4": neutral(ink_lightness[3]),
    }


def _derive_accent_mode(
    seed: str,
    scheme: dict[str, str],
    *,
    mode: str,
) -> dict[str, str]:
    seed_lightness, seed_chroma, seed_hue = hex_to_oklch(seed)
    backgrounds = (scheme["background"], scheme["surface"])
    accent, accent_ink = _select_accent(
        seed_lightness,
        seed_chroma,
        seed_hue,
        backgrounds=backgrounds,
        mode=mode,
    )
    accent_lightness = hex_to_oklch(accent)[0]
    hover_target = accent_lightness - 0.07 if mode == "light" else accent_lightness + 0.07
    accent_hover = _select_accent_with_ink(
        hover_target,
        seed_chroma,
        seed_hue,
        backgrounds=backgrounds,
        accent_ink=accent_ink,
        mode=mode,
        distinct_from=accent,
    )

    page_lightness = hex_to_oklch(scheme["background"])[0]
    if mode == "light":
        soft_lightness = page_lightness - 0.065
        tint_lightness = page_lightness - 0.025
    else:
        soft_lightness = page_lightness + 0.080
        tint_lightness = page_lightness + 0.035
    accent_soft = oklch_to_hex(soft_lightness, min(0.045, seed_chroma * 0.35), seed_hue)
    accent_tint = oklch_to_hex(tint_lightness, min(0.025, seed_chroma * 0.20), seed_hue)
    return {
        "accent": accent,
        "accentHover": accent_hover,
        "accentInk": accent_ink,
        "accentSoft": accent_soft,
        "accentTint": accent_tint,
        "focus": accent,
    }


def _select_accent(
    lightness: float,
    chroma: float,
    hue: float,
    *,
    backgrounds: tuple[str, ...],
    mode: str,
) -> tuple[str, str]:
    for candidate_lightness in _ordered_lightness(lightness, mode=mode):
        candidate = oklch_to_hex(candidate_lightness, chroma, hue)
        ink = _best_ink(candidate)
        if _has_contrast(candidate, backgrounds, _TEXT_CONTRAST) and contrast_ratio(ink, candidate) >= _TEXT_CONTRAST:
            return candidate, ink
    raise PersonalThemeCompilerError("accent cannot meet contrast requirements")


def _select_accent_with_ink(
    lightness: float,
    chroma: float,
    hue: float,
    *,
    backgrounds: tuple[str, ...],
    accent_ink: str,
    mode: str,
    distinct_from: str,
) -> str:
    for candidate_lightness in _ordered_lightness(lightness, mode=mode):
        candidate = oklch_to_hex(candidate_lightness, chroma, hue)
        if (
            _has_contrast(candidate, backgrounds, _TEXT_CONTRAST)
            and contrast_ratio(accent_ink, candidate) >= _TEXT_CONTRAST
            and delta_e_ok(candidate, distinct_from) >= 0.012
        ):
            return candidate
    raise PersonalThemeCompilerError("accent hover cannot meet contrast requirements")


def _ordered_lightness(origin: float, *, mode: str) -> list[float]:
    values = (index / 1000 for index in range(1001))
    direction = 1 if mode == "light" else -1
    return sorted(
        values,
        key=lambda candidate: (
            abs(candidate - origin),
            direction * candidate,
        ),
    )


def _best_ink(background: str) -> str:
    candidates = ("#000000", "#FFFFFF")
    return max(candidates, key=lambda candidate: contrast_ratio(candidate, background))


def _has_contrast(
    foreground: str,
    backgrounds: tuple[str, ...],
    minimum: float,
) -> bool:
    return all(contrast_ratio(foreground, background) >= minimum for background in backgrounds)


def _adjust_semantic_foregrounds(
    scheme: dict[str, str],
    *,
    mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    for role in _SEMANTIC_ROLES:
        original = scheme[role]
        backgrounds = (
            scheme["background"],
            scheme["surface"],
            scheme[f"{role}Soft"],
        )
        if _has_contrast(original, backgrounds, _TEXT_CONTRAST):
            continue
        lightness, chroma, hue = hex_to_oklch(original)
        adjusted = next(
            (
                oklch_to_hex(candidate_lightness, chroma, hue)
                for candidate_lightness in _ordered_lightness(lightness, mode=mode)
                if _has_contrast(
                    oklch_to_hex(candidate_lightness, chroma, hue),
                    backgrounds,
                    _TEXT_CONTRAST,
                )
            ),
            None,
        )
        if adjusted is None:
            blocking.append(
                {
                    "code": "semantic_contrast_unresolvable",
                    "severity": "blocking",
                    "mode": mode,
                    "path": f"{mode}.{role}",
                    "message": f"{role} cannot meet contrast on the personal background.",
                    "actual": original,
                    "required": _TEXT_CONTRAST,
                    "suggestion": "Choose a different personal background.",
                }
            )
            continue
        scheme[role] = adjusted
        warnings.append(
            {
                "code": "semantic_adjustment",
                "severity": "warning",
                "mode": mode,
                "path": f"{mode}.{role}",
                "message": f"{role} was adjusted for the personal background.",
                "input": original,
                "actual": adjusted,
            }
        )
    return warnings, blocking


def _overlay_checksum(color_scheme: dict[str, Any]) -> str:
    payload = {
        "personalOverlayVersion": PERSONAL_OVERLAY_VERSION,
        "tokenSchemaVersion": color_scheme["tokenSchemaVersion"],
        "light": color_scheme["light"],
        "dark": color_scheme["dark"],
    }
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


def _validate_accent_overlay(
    light: dict[str, str],
    dark: dict[str, str],
) -> dict[str, Any]:
    blocking: list[dict[str, Any]] = []
    contrast_pairs = (
        ("accentInk", "accent", 4.5),
        ("accentInk", "accentHover", 4.5),
        ("accent", "background", 4.5),
        ("accent", "surface", 4.5),
        ("focus", "surface", 3.0),
        ("accent", "accentTint", 3.0),
    )
    for mode, scheme in (("light", light), ("dark", dark)):
        for foreground, background, minimum in contrast_pairs:
            actual = contrast_ratio(scheme[foreground], scheme[background])
            if actual < minimum:
                blocking.append(
                    {
                        "code": "contrast_below_threshold",
                        "severity": "blocking",
                        "mode": mode,
                        "path": f"{mode}.{foreground}",
                        "message": f"{foreground} does not have enough contrast against {background}.",
                        "actual": round(actual, 6),
                        "required": minimum,
                        "suggestion": "Choose a different accent color.",
                        "foreground": foreground,
                        "background": background,
                    }
                )
        actual_delta = delta_e_ok(scheme["accent"], scheme["accentHover"])
        if actual_delta < 0.012:
            blocking.append(
                {
                    "code": "state_or_surface_not_distinct",
                    "severity": "blocking",
                    "mode": mode,
                    "path": f"{mode}.accent,{mode}.accentHover",
                    "message": "accent and accentHover are not visually distinct enough.",
                    "actual": round(actual_delta, 6),
                    "required": 0.012,
                    "suggestion": "Choose a different accent color.",
                    "foreground": "accent",
                    "background": "accentHover",
                }
            )
    return {
        "status": "blocking" if blocking else "valid",
        "blockingCount": len(blocking),
        "warningCount": 0,
        "blocking": blocking,
        "warnings": [],
    }


def _valid_report() -> dict[str, Any]:
    return {
        "status": "valid",
        "blockingCount": 0,
        "warningCount": 0,
        "blocking": [],
        "warnings": [],
    }


__all__ = [
    "PERSONAL_OVERLAY_GENERATOR_ID",
    "PERSONAL_OVERLAY_VERSION",
    "PersonalThemeCompileResult",
    "PersonalThemeBaseSchemeError",
    "PersonalThemeCompiler",
    "PersonalThemeCompilerError",
]
