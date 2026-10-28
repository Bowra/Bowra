from __future__ import annotations

import itertools
import re

import pytest
from bowhead_core.legacy_theme_registry import legacy_m1_compiled_scheme
from bowhead_core.personal_theme_compiler import (
    PERSONAL_OVERLAY_VERSION,
    PersonalThemeCompiler,
    PersonalThemeCompilerError,
)
from bowhead_core.theme_compiler import SCHEME_TOKEN_KEYS, canonical_json
from bowhead_core.theme_validation import contrast_ratio

_HEX_RE = re.compile(r"^#[0-9A-F]{6}$")


def _base_scheme(profile_id: str = "bowhead") -> dict:
    compiled = legacy_m1_compiled_scheme(profile_id)
    return {
        "tokenSchemaVersion": compiled["tokenSchemaVersion"],
        "light": compiled["light"],
        "dark": compiled["dark"],
        "checksum": compiled["checksum"],
    }


def _alphabetical_base_scheme(profile_id: str = "bowhead") -> dict:
    base = _base_scheme(profile_id)
    base["light"] = dict(sorted(base["light"].items()))
    base["dark"] = dict(sorted(base["dark"].items()))
    return base


@pytest.mark.unit
@pytest.mark.parametrize("profile_id", ["bowhead", "ocean", "forest", "plum", "graphite"])
def test_no_personal_color_override_preserves_base_scheme_byte_for_byte(profile_id: str) -> None:
    base = _base_scheme(profile_id)

    result = PersonalThemeCompiler().compile(
        base,
        {
            "mode": "dark",
            "typographyProfile": {"type": "preset", "id": "system"},
            "density": "compact",
        },
    )

    assert result.color_scheme == base
    assert canonical_json(result.color_scheme) == canonical_json(base)
    assert result.color_scheme["checksum"] == base["checksum"]
    assert result.background_source == "base"
    assert result.accent_source == "base"
    assert result.validation["status"] == "valid"


@pytest.mark.unit
def test_accent_only_overlay_does_not_recompile_unrelated_base_roles() -> None:
    base = _base_scheme()

    result = PersonalThemeCompiler().compile(
        base,
        {"accent": {"kind": "custom", "seed": "#246BCE"}},
    )

    accent_roles = {"accent", "accentHover", "accentInk", "accentSoft", "accentTint", "focus"}
    assert result.validation["blockingCount"] == 0
    assert result.background_source == "base"
    for mode in ("light", "dark"):
        assert {role: value for role, value in result.color_scheme[mode].items() if role not in accent_roles} == {
            role: value for role, value in base[mode].items() if role not in accent_roles
        }


@pytest.mark.unit
def test_overlay_accepts_legacy_alphabetical_token_order_and_canonicalizes_output() -> None:
    base = _alphabetical_base_scheme()
    assert tuple(base["light"]) != SCHEME_TOKEN_KEYS

    result = PersonalThemeCompiler().compile(
        base,
        {
            "accent": {"kind": "preset", "registryVersion": 1, "id": "forest"},
            "background": {
                "kind": "preset",
                "registryVersion": 1,
                "id": "pure-white",
            },
        },
    )

    assert result.validation["blockingCount"] == 0, result.validation
    assert tuple(result.color_scheme["light"]) == SCHEME_TOKEN_KEYS
    assert tuple(result.color_scheme["dark"]) == SCHEME_TOKEN_KEYS


_ACCENTS = (
    {"kind": "preset", "registryVersion": 1, "id": "bowhead"},
    {"kind": "preset", "registryVersion": 1, "id": "ocean"},
    {"kind": "preset", "registryVersion": 1, "id": "forest"},
    {"kind": "preset", "registryVersion": 1, "id": "plum"},
    {"kind": "preset", "registryVersion": 1, "id": "graphite"},
    {"kind": "custom", "seed": "#246BCE"},
    {"kind": "custom", "seed": "#FFCC00"},
)
_BACKGROUNDS = (
    {"kind": "preset", "registryVersion": 1, "id": "warm-paper"},
    {"kind": "preset", "registryVersion": 1, "id": "pure-white"},
    {"kind": "preset", "registryVersion": 1, "id": "cool-gray"},
    {"kind": "custom", "light": "#F8FAFC", "dark": "#111827"},
)


@pytest.mark.unit
@pytest.mark.parametrize("background", _BACKGROUNDS)
def test_background_only_overlay_revalidates_context_without_replacing_accent_source(
    background: dict,
) -> None:
    result = PersonalThemeCompiler().compile(
        _base_scheme(),
        {"background": background},
    )

    assert result.validation["blockingCount"] == 0, result.validation
    assert result.background_source == "personal"
    assert result.accent_source == "base"
    for mode in ("light", "dark"):
        scheme = result.color_scheme[mode]
        assert contrast_ratio(scheme["accentInk"], scheme["accent"]) >= 4.5
        assert contrast_ratio(scheme["ink"], scheme["background"]) >= 4.5


@pytest.mark.unit
@pytest.mark.parametrize(("accent", "background"), itertools.product(_ACCENTS, _BACKGROUNDS))
def test_personal_color_matrix_emits_safe_complete_deterministic_scheme(
    accent: dict,
    background: dict,
) -> None:
    compiler = PersonalThemeCompiler()
    overrides = {"accent": accent, "background": background}

    first = compiler.compile(_base_scheme(), overrides)
    second = compiler.compile(_base_scheme(), overrides)

    assert first == second
    assert first.personal_overlay_version == PERSONAL_OVERLAY_VERSION
    assert first.validation["blockingCount"] == 0, first.validation
    assert first.validation["warningCount"] == len(first.validation["warnings"])
    assert tuple(first.color_scheme["light"]) == SCHEME_TOKEN_KEYS
    assert tuple(first.color_scheme["dark"]) == SCHEME_TOKEN_KEYS
    assert all(_HEX_RE.fullmatch(value) for value in first.color_scheme["light"].values())
    assert all(_HEX_RE.fullmatch(value) for value in first.color_scheme["dark"].values())
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first.color_scheme["checksum"])
    assert first.background_source == "personal"
    assert first.accent_source == "personal"

    for mode in ("light", "dark"):
        scheme = first.color_scheme[mode]
        assert contrast_ratio(scheme["accentInk"], scheme["accent"]) >= 4.5
        assert contrast_ratio(scheme["accentInk"], scheme["accentHover"]) >= 4.5
        assert contrast_ratio(scheme["ink"], scheme["background"]) >= 4.5


@pytest.mark.unit
def test_custom_input_seed_is_reported_separately_from_adjusted_tokens() -> None:
    result = PersonalThemeCompiler().compile(
        _base_scheme(),
        {"accent": {"kind": "custom", "seed": "#ffcc00"}},
    )

    assert result.input_seeds == {"accent": "#FFCC00"}
    assert result.color_scheme["light"]["accent"] != "#FFCC00"
    assert _HEX_RE.fullmatch(result.color_scheme["dark"]["accent"])


@pytest.mark.unit
def test_unsafe_custom_background_returns_blocking_validation_without_mutating_base() -> None:
    base = _base_scheme()

    result = PersonalThemeCompiler().compile(
        base,
        {
            "background": {
                "kind": "custom",
                "light": "#777777",
                "dark": "#888888",
            }
        },
    )

    assert result.validation["status"] == "blocking"
    assert result.validation["blockingCount"] > 0
    assert base == _base_scheme()


@pytest.mark.unit
def test_compiler_rejects_invalid_base_scheme() -> None:
    base = _base_scheme()
    del base["light"]["focus"]

    with pytest.raises(PersonalThemeCompilerError, match="base light token keys"):
        PersonalThemeCompiler().compile(base, {})

    base = _base_scheme()
    base["light"]["unexpected"] = "#FFFFFF"
    with pytest.raises(PersonalThemeCompilerError, match="base light token keys"):
        PersonalThemeCompiler().compile(base, {})

    base = _base_scheme()
    base["checksum"] = "sha256:short"
    with pytest.raises(PersonalThemeCompilerError, match="checksum"):
        PersonalThemeCompiler().compile(base, {})
