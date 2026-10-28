from __future__ import annotations

import itertools
import re

import pytest
from bowhead_core.theme_compiler import (
    GENERATOR_ID,
    GENERATOR_VERSION,
    SCHEME_TOKEN_KEYS,
    ThemeCompilerError,
    checksum_compiled_scheme,
    compile_theme_scheme,
    hex_to_oklch,
    normalize_theme_source,
    oklch_to_hex,
)

HEX_RE = re.compile(r"^#[0-9A-F]{6}$")


@pytest.mark.unit
def test_compiler_is_deterministic_and_emits_exact_complete_key_sets() -> None:
    source = {
        "version": 2,
        "configurationType": "custom",
        "presetId": None,
        "primarySeed": "#0067b1",
        "neutralSeed": "#66717b",
        "backgroundStyle": "cool",
        "overrides": {
            "light": {"background": "#f5f8fa", "surface": "#ffffff"},
            "dark": {"background": "#0b1117", "surface": "#111a23"},
        },
    }

    first = compile_theme_scheme(source)
    second = compile_theme_scheme(source)

    assert first == second
    assert first["generator"] == {"id": GENERATOR_ID, "version": GENERATOR_VERSION}
    assert tuple(first["light"]) == SCHEME_TOKEN_KEYS
    assert tuple(first["dark"]) == SCHEME_TOKEN_KEYS
    assert all(HEX_RE.fullmatch(value) for value in first["light"].values())
    assert all(HEX_RE.fullmatch(value) for value in first["dark"].values())
    assert first["light"]["background"] == "#F5F8FA"
    assert first["dark"]["surface"] == "#111A23"
    assert first["checksum"] == checksum_compiled_scheme(first)


@pytest.mark.unit
@pytest.mark.parametrize("preset_id", ["bowhead", "ocean", "forest", "plum", "graphite"])
def test_every_v2_preset_compiles(preset_id: str) -> None:
    compiled = compile_theme_scheme(
        {
            "version": 2,
            "configurationType": "preset",
            "presetId": preset_id,
        }
    )

    assert set(compiled["light"]) == set(compiled["dark"]) == set(SCHEME_TOKEN_KEYS)


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        {"version": 1, "configurationType": "bowhead", "presetId": None},
        {"version": 2, "configurationType": "unknown", "presetId": None},
        {"version": 2, "configurationType": "preset", "presetId": "unknown"},
        {
            "version": 2,
            "configurationType": "custom",
            "primarySeed": "#FFF",
            "neutralSeed": "#66717B",
            "backgroundStyle": "cool",
        },
        {
            "version": 2,
            "configurationType": "custom",
            "primarySeed": "#0067B1AA",
            "neutralSeed": "#66717B",
            "backgroundStyle": "cool",
        },
        {
            "version": 2,
            "configurationType": "custom",
            "primarySeed": "#0067B1",
            "neutralSeed": "#66717B",
            "backgroundStyle": "custom",
            "overrides": {},
        },
        {
            "version": 2,
            "configurationType": "custom",
            "primarySeed": "#0067B1",
            "neutralSeed": "#66717B",
            "backgroundStyle": "cool",
            "compiled": {},
        },
    ],
)
def test_source_schema_rejects_unknown_or_ambiguous_input(source: dict) -> None:
    with pytest.raises(ThemeCompilerError):
        normalize_theme_source(source)


@pytest.mark.unit
def test_nearly_achromatic_primary_seed_gets_stable_warning() -> None:
    compiled = compile_theme_scheme(
        {
            "version": 2,
            "configurationType": "custom",
            "primarySeed": "#777777",
            "neutralSeed": "#66717B",
            "backgroundStyle": "neutral",
        }
    )

    assert compiled["validation"]["warnings"][0]["code"] == "unstable_primary_hue"


@pytest.mark.unit
def test_oklch_conversion_round_trips_representative_srgb_colors() -> None:
    for color in ("#000000", "#FFFFFF", "#0067B1", "#B04A2A", "#62C491"):
        lightness, chroma, hue = hex_to_oklch(color)
        assert oklch_to_hex(lightness, chroma, hue) == color


@pytest.mark.unit
def test_two_hundred_seed_corpus_compiles_without_shape_or_checksum_drift() -> None:
    channels = range(0, 256, 37)
    seeds = list(itertools.islice(itertools.product(channels, repeat=3), 200))
    checksums: set[str] = set()

    for red, green, blue in seeds:
        seed = f"#{red:02X}{green:02X}{blue:02X}"
        compiled = compile_theme_scheme(
            {
                "version": 2,
                "configurationType": "custom",
                "primarySeed": seed,
                "neutralSeed": "#66717B",
                "backgroundStyle": "cool",
            }
        )
        assert set(compiled["light"]) == set(SCHEME_TOKEN_KEYS)
        assert set(compiled["dark"]) == set(SCHEME_TOKEN_KEYS)
        assert compiled["checksum"] == checksum_compiled_scheme(compiled)
        checksums.add(compiled["checksum"])

    assert len(checksums) > 150
