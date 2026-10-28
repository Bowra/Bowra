"""Export request option ``chart_hidden_series`` (user decision 2026-10-01, option b).

An export follows the exporting user's legend-hidden series. The option is per-viewer screen state, so it must be
normalized (same screen state -> same request hash and visual identity), limited to the sections this export renders,
and bounded far below the other request limits.
"""

from __future__ import annotations

import pytest
from bowhead_gateway.routes.canvas import _export_v2_options, _validate_chart_hidden_series
from fastapi import HTTPException

EXPORTED = {"sec_a", "sec_b", "sec_c"}


@pytest.mark.unit
def test_hidden_series_are_normalized() -> None:
    got = _validate_chart_hidden_series({"sec_b": ["y", "x", "y"], "sec_a": ["z"], "sec_c": []}, exported_section_ids=EXPORTED)
    assert got == {"sec_a": ["z"], "sec_b": ["x", "y"]}
    assert list(got) == ["sec_a", "sec_b"], "sorted section order keeps the request hash stable"


@pytest.mark.unit
def test_only_sections_this_export_renders_are_kept() -> None:
    got = _validate_chart_hidden_series({"sec_a": ["x"], "sec_unrendered": ["y"]}, exported_section_ids={"sec_a"})
    assert got == {"sec_a": ["x"]}


@pytest.mark.unit
def test_absent_option_means_nothing_hidden() -> None:
    assert _validate_chart_hidden_series(None, exported_section_ids=EXPORTED) == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        ["sec_a"],
        {"sec_a": "x"},
        {"sec_a": [1]},
        {"sec_a": [""]},
        {"": ["x"]},
        {"sec_a": ["k" * 129]},
        {"sec_a": [f"k{i}" for i in range(33)]},
        {f"sec_{i}": ["x"] for i in range(51)},
        # Total entries above the cap even though each chart is within its own.
        {f"sec_{i}": [f"k{j}" for j in range(30)] for i in range(7)},
        # Byte cap: 40 sections x 20 keys of 100 characters.
        {f"sec_{i}": [f"{j:03d}" + "k" * 97 for j in range(5)] for i in range(40)},
    ],
)
def test_malformed_or_oversized_values_are_rejected(value: object) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_chart_hidden_series(value, exported_section_ids=EXPORTED)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_options"


@pytest.mark.unit
def test_options_carry_hidden_series_only_when_something_is_hidden() -> None:
    assert _export_v2_options(True, {"sec_a": ["x"]}, exported_section_ids=EXPORTED) == {
        "include_appendix": True,
        "chart_hidden_series": {"sec_a": ["x"]},
    }
    # Nothing hidden -> the options (and so the request hash) are exactly what they were before the option existed.
    assert _export_v2_options(True, None, exported_section_ids=EXPORTED) == {"include_appendix": True}
    assert _export_v2_options(False, {"sec_a": []}, exported_section_ids=EXPORTED) == {"include_appendix": False}
    assert _export_v2_options(True, {"sec_x": ["y"]}, exported_section_ids=EXPORTED) == {"include_appendix": True}
