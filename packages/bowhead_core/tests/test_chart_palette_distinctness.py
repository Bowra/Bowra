"""Categorical palette distinctness: the CIEDE2000 metric, the built-in presets, the overflow recipe.

The 3:1 surface gate only asks "can I see this mark?". Audit 2026-09-28 found built-in
colors that pass it and still look identical to each other (``analytical-neutral/dark``
slots 0/1, ΔE2000 4.2), and a 16-color overflow that fell to 5.4 / 2.3 from the 11th
color on. These tests pin the second question: "can I tell these two apart?".
"""

from __future__ import annotations

from itertools import combinations

import pytest
from bowhead_core.chart_style_materializer import (
    DARK_STANDARD_SURFACE,
    LIGHT_STANDARD_SURFACE,
    PALETTE_OVERFLOW_RECIPE_VERSION,
    _distinctness_info,
    _overflow_palette,
    _palette_validation,
    actual_surface_projection,
    color_only_series_capacity,
    validate_resolved_snapshot,
)
from bowhead_core.chart_style_registry_generated import CHART_STYLE_PRESETS
from bowhead_core.theme_validation import contrast_ratio

from bowhead_core import chart_palette_distinctness as distinct

SURFACES = {"light": LIGHT_STANDARD_SURFACE, "dark": DARK_STANDARD_SURFACE}
PRESET_MODES = [(preset_id, mode) for preset_id in sorted(CHART_STYLE_PRESETS) for mode in ("light", "dark")]

# Base palettes as shipped before 2026-10-01 (presetRevision 2026.09.1).
LEGACY_ANALYTICAL_NEUTRAL_DARK = ["#93A9B8", "#A1B0B7", "#B9A58E", "#93A48E", "#B39AA3", "#D5D8DA"]


def _base(preset_id: str, mode: str) -> list[str]:
    return list(CHART_STYLE_PRESETS[preset_id]["tokens"][mode]["palette"])


def _projection(palette: list[str], mode: str) -> dict:
    snapshot = {"colors": {mode: {"categorical": palette, "sequential": {}, "diverging": {}}}}
    return actual_surface_projection(snapshot, mode=mode, surface=SURFACES[mode], color_scheme_checksum="sha256:test")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        # Sharma, Wu & Dalal (2005) CIEDE2000 test data, pairs 1, 7, 17 and 25.
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
        ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ],
)
def test_ciede2000_matches_published_reference_pairs(first: tuple, second: tuple, expected: float) -> None:
    assert distinct._ciede2000(first, second) == pytest.approx(expected, abs=1e-4)


@pytest.mark.unit
def test_simulation_is_quantized_and_identity_for_normal_vision() -> None:
    assert distinct.simulate_vision("#2563a7", "normal") == "#2563A7"
    simulated = distinct.simulate_vision("#B04A2A", "deutan")
    assert simulated.startswith("#") and len(simulated) == 7 and simulated != "#B04A2A"


@pytest.mark.unit
@pytest.mark.parametrize(("preset_id", "mode"), PRESET_MODES)
def test_every_built_in_base_palette_meets_the_distinctness_targets(preset_id: str, mode: str) -> None:
    palette = _base(preset_id, mode)
    for first, second in combinations(palette, 2):
        assert distinct.distinctness(first, second, ("normal",)) >= distinct.BASE_NORMAL_TARGET, (preset_id, mode, first, second)
        assert distinct.distinctness(first, second, ("protan", "deutan")) >= distinct.BASE_CVD_TARGET, (preset_id, mode, first, second)
    assert _distinctness_info(mode, palette) == []


@pytest.mark.unit
def test_validation_flags_the_colliding_pair_the_audit_found() -> None:
    info = _distinctness_info("dark", LEGACY_ANALYTICAL_NEUTRAL_DARK)
    flagged = {(issue["details"]["firstIndex"], issue["details"]["secondIndex"]) for issue in info if issue["code"] == "palette.distinctness"}
    assert (0, 1) in flagged
    pair = next(issue for issue in info if issue["code"] == "palette.distinctness" and issue["details"]["secondIndex"] == 1)
    assert pair["severity"] == "info"
    assert pair["path"] == "colors.dark.categorical"
    assert pair["details"]["normal"] < distinct.BASE_NORMAL_TARGET
    assert pair["details"]["normalMinimum"] == distinct.BASE_NORMAL_TARGET
    assert pair["details"]["cvdMinimum"] == distinct.BASE_CVD_TARGET


@pytest.mark.unit
def test_distinctness_is_informational_and_needs_no_acknowledgement() -> None:
    """A blocker would stop exports of published team styles; a warning would force an ack on every
    publish / restore of a style frozen on an older preset. Only ``info`` carries it."""

    snapshot = {
        "colors": {
            "light": {"categorical": _base("analytical-neutral", "light")},
            "dark": {"categorical": LEGACY_ANALYTICAL_NEUTRAL_DARK},
        }
    }
    validation = validate_resolved_snapshot(snapshot)
    assert [issue for issue in validation["blockers"] + validation["warnings"] if issue["code"] == "palette.distinctness"] == []
    assert {issue["code"] for issue in validation["info"]} == {"palette.distinctness"}


@pytest.mark.unit
def test_distinct_prefix_length_stops_at_the_first_failing_color() -> None:
    palette = ["#2563A7", "#B06B33", "#2564A8", "#3EA586"]
    assert distinct.distinct_prefix_length(palette, normal_target=10, cvd_target=7) == 2
    assert distinct.distinct_prefix_length(palette[:2], normal_target=10, cvd_target=7) == 2
    assert distinct.distinct_prefix_length(["#2563A7"], normal_target=10, cvd_target=7) == 1


@pytest.mark.unit
@pytest.mark.parametrize(("preset_id", "mode"), PRESET_MODES)
def test_overflow_expands_to_sixteen_readable_colors_deterministically(preset_id: str, mode: str) -> None:
    base = _base(preset_id, mode)
    expanded = _overflow_palette(base, surface=SURFACES[mode])
    assert expanded[:6] == base
    assert len(expanded) == len(set(expanded)) == 16
    assert all(contrast_ratio(color, SURFACES[mode]) >= 3.0 for color in expanded)
    assert _overflow_palette(base, surface=SURFACES[mode]) == expanded


@pytest.mark.unit
@pytest.mark.parametrize(("preset_id", "mode"), PRESET_MODES)
def test_projection_reports_a_capacity_that_really_meets_the_targets(preset_id: str, mode: str) -> None:
    projection = _projection(_base(preset_id, mode), mode)
    assert projection["paletteOverflowRecipeVersion"] == PALETTE_OVERFLOW_RECIPE_VERSION == "palette-overflow/2"
    palette = projection["categoricalSeriesPalette"]
    capacity = projection["finalPaletteQA"]["colorOnlySeriesCapacity"]
    assert capacity == color_only_series_capacity(palette)
    # The product cap (6) governs the shipped capacity; the measured prefix stays the evidence for it.
    assert capacity == 6
    measured = distinct.distinct_prefix_length(palette, normal_target=distinct.BASE_NORMAL_TARGET, cvd_target=distinct.BASE_CVD_TARGET)
    # Measured 2026-10-01: 11 for analytical-neutral (both modes) and clean-blue/dark, 16 otherwise.
    assert measured >= 11
    for first, second in combinations(palette[:measured], 2):
        assert distinct.distinctness(first, second, ("normal",)) >= distinct.BASE_NORMAL_TARGET
        assert distinct.distinctness(first, second, ("protan", "deutan")) >= distinct.BASE_CVD_TARGET
    if measured < len(palette):
        nxt = palette[measured]
        assert any(
            distinct.distinctness(nxt, other, ("normal",)) < distinct.BASE_NORMAL_TARGET
            or distinct.distinctness(nxt, other, ("protan", "deutan")) < distinct.BASE_CVD_TARGET
            for other in palette[:measured]
        )


@pytest.mark.unit
def test_projection_only_holds_reviewed_slots_to_color_only_targets() -> None:
    """Overflow colors are painted with a texture past the capacity; they must not flood the warnings."""

    projection = _projection(_base("analytical-neutral", "dark"), "dark")
    assert "palette.distinctness" not in projection["finalPaletteQA"]["warningCodes"]
    _, warnings = _palette_validation("dark", projection["categoricalSeriesPalette"], DARK_STANDARD_SURFACE)
    assert [issue for issue in warnings if issue["code"] == "palette.distinctness"] == []


@pytest.mark.unit
def test_capacity_shrinks_when_a_team_palette_collides() -> None:
    projection = _projection(LEGACY_ANALYTICAL_NEUTRAL_DARK, "dark")
    assert projection["finalPaletteQA"]["colorOnlySeriesCapacity"] == 1
