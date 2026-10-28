"""Stable print-theme snapshot contract for durable Canvas exports."""

from __future__ import annotations

import copy

import pytest
from bowhead_core.print_theme import (
    PRINT_THEME_SNAPSHOT,
    PRINT_THEME_TOKEN_FIELDS,
    PrintThemeError,
    build_print_theme_snapshot,
    current_print_theme_snapshot,
    parse_print_theme_snapshot,
)
from bowhead_core.theme_compiler import compile_theme_scheme, product_default_compiled_scheme
from bowhead_core.theme_validation import contrast_ratio


def test_current_print_theme_is_detached_and_checksum_pinned() -> None:
    first = current_print_theme_snapshot()
    second = current_print_theme_snapshot()

    assert first == PRINT_THEME_SNAPSHOT
    assert first is not second
    assert first["tokens"] is not second["tokens"]
    assert first["recipeVersion"] == "bowhead-print-theme-v3"
    assert first["checksum"] == "sha256:f1b2e4faa632f9ebe955cfd0fe6617cb0750529c48e913b159232fc97f736738"


def test_every_print_token_is_copied_from_the_compiled_product_default() -> None:
    product_light = product_default_compiled_scheme()["light"]
    tokens = current_print_theme_snapshot()["tokens"]

    # No hand-picked value: every token is the compiler's light-scheme value of the same name,
    # so the export frame (SaaS Blue accent, cool neutrals) cannot drift from the product look.
    assert tuple(tokens) == PRINT_THEME_TOKEN_FIELDS
    assert tokens == {field: product_light[field] for field in PRINT_THEME_TOKEN_FIELDS}
    assert tokens["accent"] == "#1754DB"
    assert tokens["ink"] == "#061223"


def test_build_print_theme_snapshot_is_deterministic_per_scheme() -> None:
    product_light = product_default_compiled_scheme()["light"]
    rebuilt = build_print_theme_snapshot(product_light)
    other = build_print_theme_snapshot({**product_light, "accent": "#0F766E"})

    assert rebuilt == PRINT_THEME_SNAPSHOT
    assert other["checksum"] != rebuilt["checksum"]


def test_parser_accepts_a_team_brand_snapshot_bound_by_its_checksum() -> None:
    brand = compile_theme_scheme(
        {
            "version": 2,
            "configurationType": "custom",
            "primarySeed": "#0F766E",
            "neutralSeed": "#64748B",
            "backgroundStyle": "cool",
        }
    )
    snapshot = build_print_theme_snapshot(brand["light"])

    assert parse_print_theme_snapshot(snapshot) == snapshot
    assert snapshot["checksum"] != PRINT_THEME_SNAPSHOT["checksum"]
    tampered = copy.deepcopy(snapshot)
    tampered["tokens"]["accent"] = PRINT_THEME_SNAPSHOT["tokens"]["accent"]
    with pytest.raises(PrintThemeError, match="print theme snapshot"):
        parse_print_theme_snapshot(tampered)


def test_build_print_theme_snapshot_rejects_an_incomplete_scheme() -> None:
    product_light = product_default_compiled_scheme()["light"]
    incomplete = {key: value for key, value in product_light.items() if key != "warningSoft"}

    with pytest.raises(PrintThemeError, match="warningSoft"):
        build_print_theme_snapshot(incomplete)
    with pytest.raises(PrintThemeError, match="accent"):
        build_print_theme_snapshot({**product_light, "accent": "blue"})


def test_print_accent_stays_readable_on_paper() -> None:
    tokens = current_print_theme_snapshot()["tokens"]

    # Text on an accent fill and accent text/rules on the white paper both need body-text
    # contrast (WCAG AA, 4.5:1) because print has no hover or zoom.
    assert contrast_ratio(tokens["accentInk"], tokens["accent"]) >= 4.5
    assert contrast_ratio(tokens["accent"], tokens["surface"]) >= 4.5


def _legacy_snapshot(recipe: str, tokens: dict[str, str], checksum: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "recipeVersion": recipe,
        "mode": "light",
        "tokens": tokens,
        "fontBundleVersion": "bowhead-system-fonts-v1",
        "checksum": checksum,
    }


_V1_TOKENS = {
    "page": "#FFFFFF",
    "surface": "#FFFFFF",
    "ink": "#161310",
    "mutedInk": "#56503F",
    "line": "#D9D4CB",
    "accent": "#8E3F27",
    "accentInk": "#FFFFFF",
}


@pytest.mark.parametrize(
    "legacy",
    [
        _legacy_snapshot(
            "bowhead-print-theme-v1",
            _V1_TOKENS,
            "sha256:c1ab9fd688b2d8eff0f4cb27a8c80d19dc5da9cc77b6bfdf1d3164e0c3729cb8",
        ),
        _legacy_snapshot(
            "bowhead-print-theme-v2",
            {**_V1_TOKENS, "accent": "#1754DB"},
            "sha256:80caed8907cc61f778aebe53bebdd0d1ce149a4a9f9c6c57ec9c9bd56417db43",
        ),
    ],
    ids=["v1_terracotta", "v2_warm_neutrals"],
)
def test_print_theme_rejects_snapshots_frozen_under_older_recipes(legacy: dict[str, object]) -> None:
    # Export jobs snapshotted before the v3 recipe carry exactly these values. They must fail
    # closed (the worker reports the ordinary "export again" failure) rather than render with a
    # recipe the current renderers no longer own.
    with pytest.raises(PrintThemeError, match="print theme snapshot"):
        parse_print_theme_snapshot(legacy)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value.update({"recipeVersion": "bowhead-print-theme-v2"}),
        lambda value: value["tokens"].update({"accent": "#8E3F27"}),
        lambda value: value["tokens"].update({"surface": "#FFFDF8"}),
        lambda value: value["tokens"].update({"accent": "#1754db"}),
        lambda value: value["tokens"].pop("warningSoft"),
        lambda value: value.update({"checksum": f"sha256:{'0' * 64}"}),
    ],
)
def test_print_theme_rejects_unknown_version_content_and_checksum(mutate) -> None:  # type: ignore[no-untyped-def]
    value = copy.deepcopy(PRINT_THEME_SNAPSHOT)
    mutate(value)

    with pytest.raises(PrintThemeError, match="print theme snapshot"):
        parse_print_theme_snapshot(value)


def test_print_theme_parser_returns_a_detached_snapshot() -> None:
    value = current_print_theme_snapshot()

    parsed = parse_print_theme_snapshot(value)
    parsed["tokens"]["surface"] = "#000000"

    assert value["tokens"]["surface"] == "#FFFFFF"


def test_print_theme_parser_accepts_canonical_json_key_order() -> None:
    value = current_print_theme_snapshot()
    value["tokens"] = dict(sorted(value["tokens"].items()))

    assert parse_print_theme_snapshot(value) == value
