from __future__ import annotations

import json

import pytest
from bowhead_core.theme_config import (
    KEY_APPEARANCE_THEME_CONFIG,
    PRODUCT_THEME_DEFAULT,
    ThemeConfigError,
    normalize_appearance_theme_config,
    normalize_team_theme_policy,
    parse_appearance_theme_config,
    resolve_effective_theme,
)


def test_appearance_config_is_sparse_and_normalized() -> None:
    raw = json.dumps(
        {
            "version": 1,
            "chartPalette": "colorblind-safe",
            "mode": "dark",
            "typeScale": "large",
        }
    )

    assert normalize_appearance_theme_config(raw) == ('{"version":1,"mode":"dark","typeScale":"large","chartPalette":"colorblind-safe"}')
    parsed = parse_appearance_theme_config(raw)
    assert parsed == {
        "version": 1,
        "mode": "dark",
        "typeScale": "large",
        "chartPalette": "colorblind-safe",
    }
    assert "colorProfile" not in parsed
    assert "typographyProfile" not in parsed


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "accent": "ocean"},
        {"version": 1, "backgroundColor": "#fff"},
        {"version": 1, "colorProfile": {"type": "preset", "id": "not-real"}},
        {"version": 1, "colorProfile": {"type": "custom", "id": "bowhead"}},
        {"version": 1, "typographyProfile": {"type": "preset", "id": "papyrus"}},
        {"version": 1, "typeScale": "18px"},
        {"version": 1, "chartPalette": "#123456"},
    ],
)
def test_appearance_config_rejects_unknown_or_raw_custom_values(payload: dict) -> None:
    with pytest.raises(ThemeConfigError):
        normalize_appearance_theme_config(json.dumps(payload))


def test_appearance_config_rejects_non_object_and_missing_version() -> None:
    with pytest.raises(ThemeConfigError):
        normalize_appearance_theme_config("[]")
    with pytest.raises(ThemeConfigError):
        normalize_appearance_theme_config("{}")
    with pytest.raises(ThemeConfigError):
        normalize_appearance_theme_config('{"version":true}')


def test_team_policy_accepts_only_m1_color_profile_defaults_and_locks() -> None:
    raw = json.dumps(
        {
            "version": 1,
            "defaults": {"colorProfile": {"type": "preset", "id": "ocean"}},
            "locks": {"colorProfile": True},
            "brand": {"name": "Acme", "colorProfileId": "ocean"},
        }
    )

    assert normalize_team_theme_policy(raw) == (
        '{"version":1,'
        '"defaults":{"colorProfile":{"type":"preset","id":"ocean"}},'
        '"locks":{"colorProfile":true},'
        '"brand":{"name":"Acme","colorProfileId":"ocean"}}'
    )

    with pytest.raises(ThemeConfigError):
        normalize_team_theme_policy(json.dumps({"version": 1, "defaults": {"typeScale": "large"}}))
    with pytest.raises(ThemeConfigError):
        normalize_team_theme_policy(json.dumps({"version": 1, "locks": {"colorProfile": True}}))


def test_resolve_effective_theme_preserves_sparse_user_config_and_lock_precedence() -> None:
    team_policy = {
        "version": 1,
        "defaults": {"colorProfile": {"type": "preset", "id": "ocean"}},
        "locks": {"colorProfile": True},
    }
    user_config = {
        "version": 1,
        "colorProfile": {"type": "preset", "id": "plum"},
        "chartPalette": "colorblind-safe",
        "typeScale": "large",
    }

    resolved = resolve_effective_theme(team_policy, user_config)

    assert resolved["effective"] == {
        **PRODUCT_THEME_DEFAULT,
        "colorProfile": {"type": "preset", "id": "ocean"},
        "chartPalette": "colorblind-safe",
        "typeScale": "large",
    }
    assert resolved["locks"] == {"colorProfile": True}
    assert resolved["source"]["colorProfile"] == "team_lock"
    assert resolved["source"]["chartPalette"] == "user"
    assert resolved["source"]["typeScale"] == "user"
    assert resolved["userConfig"] == user_config
    assert KEY_APPEARANCE_THEME_CONFIG == "appearance_theme_config"


def test_m1_resolution_golden_stays_stable_for_unmanaged_teams() -> None:
    """S0 regression lock: V2 work must not change the existing M1 resolver."""

    resolved = resolve_effective_theme(
        {
            "version": 1,
            "defaults": {"colorProfile": {"type": "preset", "id": "forest"}},
            "locks": {"colorProfile": False},
            "brand": {"name": "Example", "colorProfileId": "forest"},
        },
        {
            "version": 1,
            "mode": "dark",
            "colorProfile": {"type": "preset", "id": "plum"},
            "typographyProfile": {"type": "preset", "id": "readable"},
            "density": "compact",
            "radius": "soft",
        },
    )

    assert resolved == {
        "effective": {
            **PRODUCT_THEME_DEFAULT,
            "mode": "dark",
            "colorProfile": {"type": "preset", "id": "plum"},
            "typographyProfile": {"type": "preset", "id": "readable"},
            "density": "compact",
            "radius": "soft",
        },
        "locks": {},
        "source": {
            "mode": "user",
            "colorProfile": "user",
            "typographyProfile": "user",
            "typeScale": "product",
            "density": "user",
            "radius": "user",
            "motion": "product",
            "chartPalette": "product",
        },
        "userConfig": {
            "version": 1,
            "mode": "dark",
            "colorProfile": {"type": "preset", "id": "plum"},
            "typographyProfile": {"type": "preset", "id": "readable"},
            "density": "compact",
            "radius": "soft",
        },
    }
