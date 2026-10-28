from __future__ import annotations

import copy

import pytest
from bowhead_core.theme_compiler import (
    SCHEME_TOKEN_KEYS,
    checksum_compiled_scheme,
    compile_theme_scheme,
)
from bowhead_core.theme_validation import (
    contrast_ratio,
    delta_e_ok,
    validate_compiled_theme,
)


@pytest.mark.unit
def test_wcag_reference_contrast_values_are_unrounded() -> None:
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("#777777", "#FFFFFF") == pytest.approx(4.478089)
    assert contrast_ratio("#767676", "#FFFFFF") >= 4.5
    assert contrast_ratio("#777777", "#FFFFFF") < 4.5


@pytest.mark.unit
@pytest.mark.parametrize("preset_id", ["bowhead", "ocean", "forest", "plum", "graphite"])
def test_presets_pass_structural_and_contrast_gates(preset_id: str) -> None:
    compiled = compile_theme_scheme(
        {
            "version": 2,
            "configurationType": "preset",
            "presetId": preset_id,
        }
    )

    assert compiled["validation"]["blockingCount"] == 0
    assert compiled["validation"]["status"] in {"valid", "warning"}
    assert compiled["checksum"] == checksum_compiled_scheme(compiled)


@pytest.mark.unit
def test_derived_accent_is_repaired_but_admin_override_is_not_rewritten() -> None:
    compiled = compile_theme_scheme(
        {
            "version": 2,
            "configurationType": "custom",
            "primarySeed": "#FFFF00",
            "neutralSeed": "#66717B",
            "backgroundStyle": "cool",
            "overrides": {
                "light": {"background": "#777777", "surface": "#777777"},
                "dark": {},
            },
        }
    )

    assert compiled["light"]["background"] == "#777777"
    assert compiled["light"]["surface"] == "#777777"
    assert compiled["validation"]["blockingCount"] > 0
    assert any(issue["code"] == "override_lightness_out_of_range" for issue in compiled["validation"]["blocking"])
    assert any(issue["code"] == "state_or_surface_not_distinct" for issue in compiled["validation"]["blocking"])
    assert any(issue["code"] == "derived_role_adjusted" for issue in compiled["validation"]["warnings"])


@pytest.mark.unit
def test_exact_token_key_gate_rejects_missing_and_unknown_roles() -> None:
    source = {"version": 2, "configurationType": "bowhead", "presetId": None}
    compiled = compile_theme_scheme(source)
    broken = copy.deepcopy(compiled)
    del broken["light"]["focus"]
    broken["light"]["surprise"] = "#FFFFFF"
    broken.pop("checksum")

    report = validate_compiled_theme(source, broken)

    assert report["status"] == "blocking"
    assert report["blocking"][0]["code"] == "token_keys_invalid"
    assert tuple(compiled["dark"]) == SCHEME_TOKEN_KEYS


@pytest.mark.unit
def test_surface_and_hover_delta_use_oklab_distance() -> None:
    assert delta_e_ok("#FFFFFF", "#FFFFFF") == 0
    assert delta_e_ok("#FFFFFF", "#F0F0F0") > 0.012
