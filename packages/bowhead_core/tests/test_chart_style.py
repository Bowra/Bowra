from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from bowhead_core.chart_style import (
    PRESET_CONTENT_SHA256,
    PRESET_RELEASES,
    PRODUCT_DEFAULT_PRESET_ID,
    ChartStyleError,
    chart_style_policy_etag,
    default_team_chart_style_policy,
    normalize_chart_accessibility_preferences,
    normalize_team_chart_style_policy,
    parse_team_chart_style_policy,
    parse_user_team_chart_style_selection,
    resolve_effective_chart_style,
)
from bowhead_core.chart_style_export import (
    ChartStyleExportError,
    parse_chart_style_export_theme,
    require_current_chart_style,
)
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
    CHART_STYLE_PRESETS,
)
from bowhead_core.export_visual_identity import sha256_canonical

_REPO_ROOT = Path(__file__).parents[3]
_PRESET_DIRECTORY = _REPO_ROOT / "config/chart_style/presets"


@pytest.mark.unit
def test_generated_release_identity_matches_canonical_packages() -> None:
    for preset_id, (expected_revision, expected_checksum) in PRESET_RELEASES.items():
        package_path = _PRESET_DIRECTORY / f"{preset_id}.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))

        assert package["id"] == preset_id
        assert package["presetRevision"] == expected_revision
        assert CHART_STYLE_PRESETS[preset_id] == package
        assert f"sha256:{hashlib.sha256(package_path.read_bytes()).hexdigest()}" == expected_checksum
        canonical = json.dumps(package, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        assert f"sha256:{hashlib.sha256(canonical).hexdigest()}" == PRESET_CONTENT_SHA256[preset_id]
    assert BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM.startswith("sha256:")


@pytest.mark.unit
def test_team_policy_is_canonical_and_keeps_two_presets() -> None:
    raw = json.dumps(
        {
            "version": 1,
            "defaultPresetId": "clean-blue",
            "enabledPresetIds": ["clean-blue", "bowhead-editorial"],
        }
    )

    assert normalize_team_chart_style_policy(raw) == (
        '{"version":1,"defaultPresetId":"clean-blue","enabledPresetIds":["bowhead-editorial","clean-blue"]}'
    )

    with pytest.raises(ChartStyleError, match="at least two"):
        normalize_team_chart_style_policy('{"version":1,"defaultPresetId":"clean-blue","enabledPresetIds":["clean-blue"]}')


@pytest.mark.unit
def test_v1_readers_project_v2_settings_to_saved_builtin_fallback() -> None:
    policy = parse_team_chart_style_policy(
        '{"version":2,"defaultStyleId":"tcs_0123456789abcdef01234567",'
        '"enabledStyleIds":["tcs_0123456789abcdef01234567","clean-blue"],'
        '"fallbackBuiltInPresetId":"clean-blue"}'
    )
    selection = parse_user_team_chart_style_selection(
        '{"version":2,"styleId":"tcs_0123456789abcdef01234567","fallbackBuiltInPresetId":"analytical-neutral"}'
    )

    assert policy == {
        "version": 1,
        "defaultPresetId": "clean-blue",
        "enabledPresetIds": ["bowhead-editorial", "clean-blue", "analytical-neutral"],
    }
    assert selection == {"version": 1, "presetId": "analytical-neutral"}
    with pytest.raises(ChartStyleError, match="version must be 1"):
        normalize_team_chart_style_policy(
            '{"version":2,"defaultStyleId":"clean-blue","enabledStyleIds":["clean-blue","bowhead-editorial"],"fallbackBuiltInPresetId":"clean-blue"}'
        )


@pytest.mark.unit
def test_accessibility_is_sparse_and_strict() -> None:
    assert normalize_chart_accessibility_preferences('{"version":1,"contrast":"high"}') == '{"version":1,"contrast":"high"}'
    with pytest.raises(ChartStyleError, match="unknown field"):
        normalize_chart_accessibility_preferences('{"version":1,"palette":"blue"}')


@pytest.mark.unit
def test_resolver_preserves_disabled_selection_without_legacy_fallback() -> None:
    policy = {
        "version": 1,
        "defaultPresetId": "clean-blue",
        "enabledPresetIds": ["bowhead-editorial", "clean-blue"],
    }
    resolved = resolve_effective_chart_style(
        team_policy=policy,
        user_selection={"version": 1, "presetId": "analytical-neutral"},
        accessibility={"version": 1, "contrast": "standard"},
        policy_etag=chart_style_policy_etag(json.dumps(policy)),
    )

    assert resolved["preset"]["id"] == "clean-blue"
    assert resolved["selection"] == {
        "source": "team",
        "savedPresetId": "analytical-neutral",
        "unavailableByTeamPolicy": True,
    }
    assert resolved["accessibility"]["contrast"] == "standard"
    assert resolved["accessibility"]["source"]["contrast"] == "user"
    assert resolved["accessibility"]["colorVision"] == "standard"
    assert resolved["accessibility"]["source"]["colorVision"] == "product"


@pytest.mark.unit
def test_missing_policy_uses_product_default_without_storage_write() -> None:
    resolved = resolve_effective_chart_style(
        team_policy=None,
        user_selection=None,
        accessibility=None,
        policy_etag=chart_style_policy_etag(None),
    )

    assert resolved["preset"]["id"] == default_team_chart_style_policy()["defaultPresetId"]
    assert resolved["selection"]["source"] == "product"
    assert resolved["accessibility"]["source"] == {
        "contrast": "product",
        "colorVision": "product",
    }


@pytest.mark.unit
def test_export_theme_accepts_only_reviewed_package_and_current_resolution() -> None:
    package = json.loads((_PRESET_DIRECTORY / f"{PRODUCT_DEFAULT_PRESET_ID}.json").read_text(encoding="utf-8"))
    effective = resolve_effective_chart_style(
        team_policy=None,
        user_selection=None,
        accessibility=None,
        policy_etag=chart_style_policy_etag(None),
    )
    theme = {
        "kind": "chart-style",
        "schemaVersion": 1,
        "mode": "light",
        "preset": package,
        "presetChecksum": effective["preset"]["checksum"],
        "selectionSource": "product",
        "accessibility": {"contrast": "standard", "colorVision": "standard"},
        "teamPolicyEtag": effective["teamPolicy"]["etag"],
        "resolverPolicyVersion": "1",
        "contextRecipeVersion": "1",
    }

    parsed = parse_chart_style_export_theme(theme)
    require_current_chart_style(parsed, effective)
    assert sha256_canonical(parsed).startswith("sha256:")

    forged = json.loads(json.dumps(theme))
    forged["preset"]["tokens"]["light"]["palette"][0] = "#000000"
    with pytest.raises(ChartStyleExportError):
        parse_chart_style_export_theme(forged)
