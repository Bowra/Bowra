"""Unit semantics shared by persisted marker baking and current chart metadata."""

from __future__ import annotations

import pytest
from bowhead_core.citation_marks import _ASCII_UNIT_MAP, _CURRENCY_SCALE_UNITS, format_marker_value

from bowhead_core import units

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("code", sorted(units.KNOWN_CURRENCIES))
def test_currency_baking_and_chart_units_agree(code: str) -> None:
    spec = units.UnitSpec(kind="currency", code=code)
    assert units.to_unit_type(spec) == code
    assert code in _CURRENCY_SCALE_UNITS
    assert code in _ASCII_UNIT_MAP
    baked = format_marker_value("7016893124", code)
    assert "7016893124" not in baked
    assert "70.17" in baked
    assert units.to_chart_column(spec).get("unit")


@pytest.mark.parametrize(
    ("spec", "raw", "expected"),
    [
        (units.UnitSpec(kind="percent", percent_scale="fraction"), "0.1711", "17.11%"),
        (units.UnitSpec(kind="percent", percent_scale="points", label="%"), "2.89", "2.89%"),
        (units.UnitSpec(kind="count"), "12", "12"),
        (units.UnitSpec(kind="duration", label="days"), "12", "12天"),
        (units.UnitSpec(kind="duration", label="hours"), "12", "12小时"),
    ],
)
def test_marker_baking_preserves_unit_semantics(spec, raw: str, expected: str) -> None:
    unit = units.to_unit_type(spec)
    assert unit in _ASCII_UNIT_MAP
    assert format_marker_value(raw, unit) == expected
    if spec.kind == "percent":
        assert units.to_chart_column(spec)["percent_scale"] == spec.percent_scale


def test_currency_tables_cover_the_canonical_vocabulary() -> None:
    assert set(_ASCII_UNIT_MAP) >= _CURRENCY_SCALE_UNITS
    assert set(_CURRENCY_SCALE_UNITS) == units.KNOWN_CURRENCIES
