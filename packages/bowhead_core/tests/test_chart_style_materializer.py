from __future__ import annotations

import copy

import pytest
from bowhead_core.chart_style_materializer import (
    DARK_STANDARD_SURFACE,
    built_in_authoring_base,
    evaluate_chart_style_candidate,
    materialize_chart_style,
    normalize_authoring_source,
)
from bowhead_core.chart_style_models import ChartStyleDomainError, sha256_json
from bowhead_core.theme_validation import contrast_ratio


def _source(*, palette: list[str] | None = None) -> dict:
    overrides: dict = {"schemaVersion": 2}
    if palette is not None:
        overrides["categoricalPalette"] = {"light": palette}
    return {
        "schemaVersion": 2,
        "name": "  Sales   Editorial  ",
        "description": "  presentation only  ",
        "overrides": overrides,
    }


def test_storage_normalization_is_strict_and_canonical() -> None:
    normalized = normalize_authoring_source(
        {
            **_source(palette=["#aa0011"]),
            "overrides": {
                "schemaVersion": 2,
                "categoricalPalette": {"light": ["#aa0011"]},
                "line": {"curve": "straight", "width": "strong", "points": "hide"},
                "bar": {"radius": "round"},
                "components": {"tooltip": "disabled", "grid": "hidden"},
            },
        }
    )

    assert normalized["name"] == "Sales Editorial"
    assert normalized["description"] == "presentation only"
    assert normalized["overrides"]["categoricalPalette"]["light"] == ["#AA0011"]

    with pytest.raises(ChartStyleDomainError) as unknown:
        normalize_authoring_source({**_source(), "chartJs": {"animation": False}})
    assert unknown.value.code == "chart_style_unknown_field"

    with pytest.raises(ChartStyleDomainError) as null_value:
        normalize_authoring_source({**_source(), "overrides": {"schemaVersion": 2, "line": {"curve": None}}})
    assert null_value.value.details["path"] == "overrides.line.curve"


def test_materializer_applies_only_owned_fields_and_does_not_mutate_base() -> None:
    base, _, _ = built_in_authoring_base("clean-blue")
    original = copy.deepcopy(base)
    resolved = materialize_chart_style(
        base,
        {
            "schemaVersion": 2,
            "line": {"curve": "straight", "width": "strong", "points": "hide"},
            "bar": {"radius": "round"},
            "components": {"tooltip": "disabled", "grid": "hidden"},
        },
    )

    assert base == original
    assert resolved["kindTokens"]["line"]["line"] == {
        "curve": "straight",
        "widthPx": 2.5,
        "points": "hide",
        "pointRadiusPx": 3,
    }
    assert resolved["kindTokens"]["bar"]["bar"]["radiusPx"] == 6
    assert resolved["kindTokens"]["pie"] == {"tooltip": "disabled", "grid": "hidden"}
    assert "line" not in resolved["kindTokens"]["scatter"]


def test_light_override_generates_deterministic_dark_palette() -> None:
    base, _, _ = built_in_authoring_base("bowhead-editorial")
    palette = ["#2563A7", "#2F7F87", "#5B6B8C", "#B06B33", "#75834A", "#3B4654"]
    first = materialize_chart_style(base, normalize_authoring_source(_source(palette=palette))["overrides"])
    second = materialize_chart_style(base, normalize_authoring_source(_source(palette=palette))["overrides"])

    assert first == second
    dark = first["colors"]["dark"]["categorical"]
    assert len(dark) == len(palette)
    assert all(contrast_ratio(color, DARK_STANDARD_SURFACE) >= 3.0 for color in dark)


def test_preview_keeps_storage_safe_partial_palette_but_blocks_publish() -> None:
    base, _, _ = built_in_authoring_base("bowhead-editorial")
    base_checksum = sha256_json(base)
    preview = evaluate_chart_style_candidate(
        source=_source(palette=["#2563A7", "#2F7F87", "#5B6B8C"]),
        authoring_base_snapshot=base,
        authoring_base_snapshot_checksum=base_checksum,
    )

    assert preview["normalizedAuthoringSource"]["overrides"]["categoricalPalette"]["light"] == [
        "#2563A7",
        "#2F7F87",
        "#5B6B8C",
    ]
    assert preview["candidateSnapshot"] is None
    assert preview["surfaceEvidence"] is None
    assert {issue["code"] for issue in preview["validation"]["blockers"]} == {"palette.count"}


def test_unchanged_builtin_is_publishable_with_stable_identity() -> None:
    base, _, _ = built_in_authoring_base("analytical-neutral")
    kwargs = {
        "source": _source(),
        "authoring_base_snapshot": base,
        "authoring_base_snapshot_checksum": sha256_json(base),
    }
    first = evaluate_chart_style_candidate(**kwargs)
    second = evaluate_chart_style_candidate(**kwargs)

    assert first == second
    assert first["candidateSnapshot"] == base
    assert first["candidateSnapshotChecksum"] == sha256_json(base)
    assert first["surfaceEvidence"]["contractVersion"] == "chart-surface/1"
    assert first["surfaceEvidence"]["modes"]["light"]["finalPaletteQA"]["hardInvariantsPass"] is True
