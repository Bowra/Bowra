from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from bowhead_core.chart_style import ChartStyleError
from bowhead_core.chart_style_materializer import (
    actual_surface_projection,
    built_in_authoring_base,
)
from bowhead_core.chart_style_v2 import (
    chart_style_policy_etag_v2,
    default_team_chart_style_policy_v2,
    normalize_user_team_chart_style_selection_v2,
    parse_team_chart_style_policy_v2,
    parse_user_team_chart_style_selection_v2,
)


def test_v2_policy_keeps_order_and_projects_v1_without_mutating_storage() -> None:
    v2 = {
        "version": 2,
        "defaultStyleId": "tcs_0123456789abcdef01234567",
        "enabledStyleIds": [
            "tcs_0123456789abcdef01234567",
            "clean-blue",
        ],
        "fallbackBuiltInPresetId": "clean-blue",
    }
    assert parse_team_chart_style_policy_v2(json.dumps(v2)) == v2
    assert parse_team_chart_style_policy_v2('{"version":1,"defaultPresetId":"clean-blue","enabledPresetIds":["clean-blue","bowhead-editorial"]}') == {
        "version": 2,
        "defaultStyleId": "clean-blue",
        "enabledStyleIds": ["clean-blue", "bowhead-editorial"],
        "fallbackBuiltInPresetId": "clean-blue",
    }


def test_v2_policy_rejects_duplicate_or_oversized_option_sets() -> None:
    duplicate = {
        "version": 2,
        "defaultStyleId": "clean-blue",
        "enabledStyleIds": ["clean-blue", "clean-blue"],
        "fallbackBuiltInPresetId": "clean-blue",
    }
    with pytest.raises(ChartStyleError):
        parse_team_chart_style_policy_v2(json.dumps(duplicate))

    oversized = {
        **default_team_chart_style_policy_v2(),
        "enabledStyleIds": [
            "bowhead-editorial",
            "clean-blue",
            "analytical-neutral",
            *[f"tcs_{index:024x}" for index in range(13)],
        ],
    }
    with pytest.raises(ChartStyleError):
        parse_team_chart_style_policy_v2(json.dumps(oversized))


def test_v2_selection_accepts_v1_and_server_owns_custom_fallback() -> None:
    assert parse_user_team_chart_style_selection_v2('{"version":1,"presetId":"clean-blue"}') == {
        "version": 2,
        "styleId": "clean-blue",
        "fallbackBuiltInPresetId": "clean-blue",
    }
    raw = normalize_user_team_chart_style_selection_v2(
        style_id="tcs_0123456789abcdef01234567",
        fallback_builtin_preset_id="analytical-neutral",
    )
    assert json.loads(raw) == {
        "version": 2,
        "styleId": "tcs_0123456789abcdef01234567",
        "fallbackBuiltInPresetId": "analytical-neutral",
    }


def test_v2_policy_etag_is_strong_and_distinguishes_raw_versions() -> None:
    unset = chart_style_policy_etag_v2(None)
    explicit = chart_style_policy_etag_v2(json.dumps(default_team_chart_style_policy_v2(), separators=(",", ":")))
    assert unset.startswith('"chart-style-policy-v2:')
    assert not unset.startswith("W/")
    assert unset != explicit


@pytest.mark.parametrize(
    ("mode", "surface"),
    [("light", "#FFFFFF"), ("dark", "#242A2E"), ("light", "#808080")],
)
def test_actual_surface_projection_is_stable_and_revalidates_full_palette(
    mode: str,
    surface: str,
) -> None:
    snapshot, _, _ = built_in_authoring_base("bowhead-editorial")
    first = actual_surface_projection(
        snapshot,
        mode=mode,
        surface=surface,
        color_scheme_checksum="scheme-v1",
    )
    second = actual_surface_projection(
        snapshot,
        mode=mode,
        surface=surface,
        color_scheme_checksum="scheme-v1",
    )
    assert first == second
    assert len(first["categoricalSeriesPalette"]) == 16
    assert len(set(first["categoricalSeriesPalette"])) == 16
    assert first["maxDistinctSeriesColors"] == 16
    assert first["paletteOverflowRecipeVersion"] == "palette-overflow/2"
    assert first["finalPaletteQA"]["hardInvariantsPass"] is True
    assert first["finalPaletteQA"]["minimumContrast"] >= 3.0
    assert first["finalPaletteQA"]["minimumPairwiseDeltaE"] >= 0.02


def test_generation_agents_cannot_import_chart_style_runtime() -> None:
    agents_dir = Path(__file__).resolve().parents[1] / "src" / "bowhead_core" / "agents"
    forbidden = (
        "bowhead_core.chart_style_materializer",
        "bowhead_core.chart_style_presentation",
        "bowhead_core.chart_style_read_service",
        "bowhead_core.chart_style_v2",
    )
    violations: list[str] = []
    for path in agents_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            if any(module == blocked or module.startswith(f"{blocked}.") for module in modules for blocked in forbidden):
                violations.append(str(path.relative_to(agents_dir)))
    assert violations == []
