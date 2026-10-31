from __future__ import annotations

import copy
import hashlib
import json
import statistics
import time
from pathlib import Path

import pytest
from bowhead_core.chart_style_presentation import (
    assign_series_encodings,
    normalize_chart_for_rendering_v1,
    resolve_chart_presentation_v2,
)
from bowhead_core.chart_style_registry_generated import CHART_STYLE_PRESENTATION_POLICY

ROOT = Path(__file__).resolve().parents[3]


def _fixture(name: str) -> dict:
    payload = json.loads((ROOT / "config/chart_style" / name).read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 1
    return payload


@pytest.mark.parametrize("fixture", _fixture("normalization_fixtures.json")["fixtures"], ids=lambda value: value["name"])
def test_raw_spec_normalization_matches_canonical_fixture(fixture: dict) -> None:
    original = copy.deepcopy(fixture["rawSpec"])
    _, shape = normalize_chart_for_rendering_v1(fixture["rawSpec"])
    assert shape == fixture["shape"]
    assert fixture["rawSpec"] == original


def test_equivalent_date_dimensions_do_not_create_false_line_series() -> None:
    spec = {
        "kind": "line",
        "title": "Revenue",
        "row_count": 3,
        "columns": [
            {"key": "date", "header": "Date", "type": "date"},
            {"key": "raw", "header": "Original date", "type": "categorical"},
            {"key": "revenue", "header": "Revenue", "type": "numeric", "unit": "亿元"},
        ],
        "rows": [
            {"date": "2026-01-02", "raw": "20260102", "revenue": 0.0009},
            {"date": "2026-01-28", "raw": "20260128", "revenue": 0.0012},
            {"date": "2026-03-03", "raw": "20260303", "revenue": -0.00004},
        ],
    }
    original = copy.deepcopy(spec)
    normalized, shape = normalize_chart_for_rendering_v1(spec)
    assert normalized["rows"] == spec["rows"] and shape["seriesCount"] == 1
    assert spec == original
    explicit = copy.deepcopy(spec)
    explicit["columns"][1]["role"] = "color"
    assert normalize_chart_for_rendering_v1(explicit)[1]["seriesCount"] == 3
    mismatched = copy.deepcopy(spec)
    mismatched["rows"][1]["raw"] = "20260129"
    assert normalize_chart_for_rendering_v1(mismatched)[1]["seriesCount"] == 3
    invalid = copy.deepcopy(spec)
    invalid["rows"][1].update(date="2026-02-30", raw="20260230")
    assert normalize_chart_for_rendering_v1(invalid)[1]["seriesCount"] == 3
    repeated = copy.deepcopy(spec)
    repeated["rows"][1].update(date="2026-01-02", raw="20260102")
    assert normalize_chart_for_rendering_v1(repeated)[1]["seriesCount"] == 2


@pytest.mark.parametrize("fixture", _fixture("presentation_fixtures.json")["fixtures"], ids=lambda value: value["name"])
def test_presentation_matches_canonical_fixture(fixture: dict) -> None:
    resolved = resolve_chart_presentation_v2(
        fixture["shape"],
        kind_tokens=fixture["kindTokens"],
        categorical_series_palette=fixture["categoricalSeriesPalette"],
        accessibility=fixture["accessibility"],
        render_context=fixture["renderContext"],
        color_only_series_capacity=fixture.get("colorOnlySeriesCapacity"),
    )
    assert resolved == fixture["expected"]


def test_shape_and_presentation_stay_inside_reviewed_gateway_budget() -> None:
    spec = {
        "kind": "line",
        "title": "performance",
        "row_count": 200,
        "columns": [
            {"key": "x", "header": "X", "type": "date"},
            *[{"key": f"s{index}", "header": f"S{index}", "type": "numeric"} for index in range(12)],
        ],
        "rows": [{"x": f"M{row}", **{f"s{index}": row - index for index in range(12)}} for row in range(200)],
    }
    kind_tokens = {
        "line": {
            "tooltip": "enabled",
            "grid": "quiet",
            "line": {"curve": "monotone", "widthPx": 2, "points": "auto", "pointRadiusPx": 3},
        }
    }
    render_context = {
        "placement": "canvas",
        "bounds": {"widthPx": 720, "heightPx": 360, "devicePixelRatio": 2},
        "input": {"hover": True, "pointer": "fine", "keyboard": True},
        "output": {"medium": "screen", "static": False},
        "mode": "light",
        "locale": "zh-CN",
        "reducedMotion": False,
        "forcedColors": False,
        "exactDataAlternative": {"kind": "inspect-table"},
    }
    samples: list[float] = []
    for _ in range(30):
        started = time.perf_counter()
        _, shape = normalize_chart_for_rendering_v1(spec)
        resolve_chart_presentation_v2(
            shape,
            kind_tokens=kind_tokens,
            categorical_series_palette=[f"#{index:06X}" for index in range(16)],
            accessibility={"contrast": "standard", "colorVision": "standard"},
            render_context=render_context,
            color_only_series_capacity=None,
        )
        samples.append((time.perf_counter() - started) * 1000)
    p95 = statistics.quantiles(samples, n=20)[18]
    assert p95 < 25


@pytest.mark.unit
def test_series_within_color_capacity_keep_color_only() -> None:
    # Product cap 6: up to six series stay color-only whatever the measured capacity.
    assert assign_series_encodings(["bar"] * 6, palette_length=16, capacity=11, color_safe=False, stacked=True) is None
    assert assign_series_encodings(["bar"] * 2, palette_length=16, capacity=16, color_safe=False, stacked=False) is None
    assert assign_series_encodings(["bar"] * 7, palette_length=16, capacity=16, color_safe=False, stacked=False) is not None


@pytest.mark.unit
def test_series_past_capacity_keep_their_colors_and_gain_the_next_texture() -> None:
    encodings = assign_series_encodings(["bar"] * 13, palette_length=16, capacity=11, color_safe=False, stacked=True)
    assert encodings is not None
    assert [item["paletteIndex"] for item in encodings] == list(range(13))
    assert [item["fillPattern"] for item in encodings] == ["solid"] * 6 + ["diagonal"] * 6 + ["dots"]
    assert encodings[6]["lineDash"] == [7, 4]
    assert encodings[0]["lineDash"] == []
    assert {item["boundaryWidthPx"] for item in encodings} == {1}


@pytest.mark.unit
def test_color_safe_gives_every_series_its_own_texture_cycle() -> None:
    encodings = assign_series_encodings(["bar"] * 5, palette_length=6, capacity=6, color_safe=True, stacked=False)
    assert encodings is not None
    assert [item["fillPattern"] for item in encodings] == ["solid", "diagonal", "dots", "crosshatch", "solid"]
    assert [item["paletteIndex"] for item in encodings] == [0, 1, 2, 3, 4]
    # Grouped bars do not touch: no separator eating into thin bars.
    assert {item["boundaryWidthPx"] for item in encodings} == {0}


@pytest.mark.unit
def test_only_filled_marks_carry_a_texture() -> None:
    encodings = assign_series_encodings(["bar", "line", "area", "point", "segment"], palette_length=6, capacity=6, color_safe=True, stacked=True)
    assert encodings is not None
    assert [item["fillPattern"] for item in encodings] == ["solid", "solid", "dots", "solid", "solid"]
    assert [item["boundaryWidthPx"] for item in encodings] == [1, 0, 0, 0, 0]


@pytest.mark.unit
@pytest.mark.parametrize("palette_length", [6, 16])
@pytest.mark.parametrize("capacity", [1, 2, 6, 11, 16])
@pytest.mark.parametrize("color_safe", [False, True])
def test_color_and_texture_pairs_stay_unique_until_every_pair_is_used(palette_length: int, capacity: int, color_safe: bool) -> None:
    patterns = len(CHART_STYLE_PRESENTATION_POLICY["nonColor"]["fillPatterns"])
    effective = min(capacity, palette_length)
    # Every pair: palette_length × patterns. Beyond capacity × patterns is where block-cycling alone repeats.
    for count in (min(effective * patterns + 1, palette_length * patterns), palette_length * patterns):
        encodings = assign_series_encodings(["bar"] * count, palette_length=palette_length, capacity=capacity, color_safe=color_safe, stacked=False)
        if encodings is None:
            continue
        pairs = [(item["paletteIndex"], item["fillPattern"]) for item in encodings]
        assert len(set(pairs)) == len(pairs), (count, pairs)


@pytest.mark.unit
@pytest.mark.parametrize("capacity", [1, 2])
@pytest.mark.parametrize("count", [3, 5])
def test_low_capacity_team_palettes_never_lose_colors(capacity: int, count: int) -> None:
    """A team style frozen on an older preset can measure capacity 1 or 2; series must keep their own colors."""

    encodings = assign_series_encodings(["bar"] * count, palette_length=6, capacity=capacity, color_safe=False, stacked=False)
    assert encodings is not None
    assert [item["paletteIndex"] for item in encodings] == list(range(count))
    assert encodings[0]["fillPattern"] == "solid"
    assert encodings[capacity]["fillPattern"] == "diagonal"


@pytest.mark.unit
def test_lines_and_points_never_lose_colors_past_capacity() -> None:
    for role in ("line", "point"):
        encodings = assign_series_encodings([role] * 7, palette_length=16, capacity=1, color_safe=False, stacked=False)
        assert encodings is not None
        assert [item["paletteIndex"] for item in encodings] == list(range(7))
        assert {item["fillPattern"] for item in encodings} == {"solid"}
        assert len({(item["paletteIndex"], item["pointStyle"]) for item in encodings}) == 7


@pytest.mark.unit
def test_export_freeze_reads_the_capacity_from_the_projection_qa() -> None:
    from bowhead_core.chart_style_export import _color_only_series_capacity

    assert _color_only_series_capacity({"finalPaletteQA": {"colorOnlySeriesCapacity": 11}}) == 11
    assert _color_only_series_capacity({"finalPaletteQA": {}}) is None
    assert _color_only_series_capacity({"finalPaletteQA": {"colorOnlySeriesCapacity": True}}) is None


def series_encoding_grid_lines(grid: dict) -> list[str]:
    """Canonical serialization shared with `fill-pattern.test.ts`."""

    lines: list[str] = []
    for role_set in grid["roleSets"]:
        for count in range(grid["seriesCounts"]["from"], grid["seriesCounts"]["to"] + 1):
            roles = [role_set[index % len(role_set)] for index in range(count)]
            for capacity in grid["capacities"]:
                for palette_length in grid["paletteLengths"]:
                    for color_safe in grid["colorSafe"]:
                        for stacked in grid["stacked"]:
                            encodings = assign_series_encodings(
                                roles, palette_length=palette_length, capacity=capacity, color_safe=color_safe, stacked=stacked
                            )
                            if encodings is not None:
                                # Contract the browser relies on: color is always positional.
                                assert [item["paletteIndex"] for item in encodings] == [i % palette_length for i in range(count)]
                            body = (
                                "none"
                                if encodings is None
                                else ";".join(
                                    f"{item['paletteIndex']},{item['fillPattern']},{'-'.join(map(str, item['lineDash']))},"
                                    f"{item['pointStyle']},{item['boundaryWidthPx']}"
                                    for item in encodings
                                )
                            )
                            lines.append(f"{'+'.join(role_set)}|{count}|{capacity}|{palette_length}|{int(color_safe)}|{int(stacked)}|{body}")
    return lines


@pytest.mark.unit
def test_series_encoding_grid_matches_the_cross_runtime_digest() -> None:
    grid = _fixture("series_encoding_grid.json")
    lines = series_encoding_grid_lines(grid)
    assert len(lines) == grid["caseCount"] == 19600
    assert hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest() == grid["sha256"]


@pytest.mark.unit
def test_product_cap_switches_both_capacity_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nonColor.colorOnlySeriesCap` is the one switch between "measured" and "texture from the 7th series"."""

    from bowhead_core.chart_style_materializer import color_only_series_capacity

    palette = ["#2563A7", "#B06B33", "#3EA586", "#8E4000", "#592C76", "#1F86C1", "#6D2C39", "#BB8500"]
    # Shipped choice (user decision 2026-10-01): texture from the 7th series.
    assert CHART_STYLE_PRESENTATION_POLICY["nonColor"]["colorOnlySeriesCap"] == 6
    assert color_only_series_capacity(palette) <= 6
    encodings = assign_series_encodings(["bar"] * 7, palette_length=16, capacity=16, color_safe=False, stacked=False)
    assert encodings is not None
    assert [item["fillPattern"] for item in encodings] == ["solid"] * 6 + ["diagonal"]
    # `null` falls back to the measured capacity on both sources.
    monkeypatch.setitem(CHART_STYLE_PRESENTATION_POLICY["nonColor"], "colorOnlySeriesCap", None)
    assert assign_series_encodings(["bar"] * 7, palette_length=16, capacity=16, color_safe=False, stacked=False) is None
    measured = color_only_series_capacity(palette)
    monkeypatch.setitem(CHART_STYLE_PRESENTATION_POLICY["nonColor"], "colorOnlySeriesCap", 2)
    assert color_only_series_capacity(palette) == min(measured, 2)
