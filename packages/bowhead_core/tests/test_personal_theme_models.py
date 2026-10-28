from __future__ import annotations

import json

import pytest
from bowhead_core.personal_theme_models import (
    KEY_APPEARANCE_THEME_CONFIG_V3,
    KEY_APPEARANCE_THEME_UNDO_V3,
    MAX_PERSONAL_THEME_CONFIG_BYTES,
    RESERVED_USER_SETTING_KEYS,
    PersonalThemeModelError,
    normalize_personal_theme_config,
    normalize_personal_theme_undo,
    parse_personal_theme_config,
    parse_personal_theme_undo,
    project_legacy_appearance_config,
)


@pytest.mark.unit
def test_personal_v3_config_normalizes_sparse_overrides() -> None:
    raw = json.dumps(
        {
            "version": 3,
            "rowVersion": 4,
            "overrides": {
                "mode": "system",
                "accent": {"kind": "custom", "seed": "#2f6f84"},
                "background": {
                    "kind": "custom",
                    "light": "#ffffff",
                    "dark": "#12161a",
                },
                "typographyProfile": {"type": "preset", "id": "system"},
                "typeScale": "large",
                "density": "comfortable",
                "radius": "balanced",
                "motion": "reduced",
            },
            "origin": {
                "kind": "legacy_projection",
                "legacyChecksum": "sha256:" + "a" * 64,
            },
        }
    )

    normalized = normalize_personal_theme_config(raw)

    assert normalized == (
        '{"version":3,"rowVersion":4,"overrides":{"mode":"system",'
        '"accent":{"kind":"custom","seed":"#2F6F84"},'
        '"background":{"kind":"custom","light":"#FFFFFF","dark":"#12161A"},'
        '"typographyProfile":{"type":"preset","id":"system"},'
        '"typeScale":"large","density":"comfortable","radius":"balanced",'
        '"motion":"reduced"},"origin":{"kind":"legacy_projection",'
        '"legacyChecksum":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}'
    )
    assert parse_personal_theme_config(normalized)["rowVersion"] == 4


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"version": 3, "rowVersion": 0, "overrides": {}},
        {"version": 3, "rowVersion": True, "overrides": {}},
        {"version": 3, "rowVersion": 1, "overrides": {}, "unknown": True},
        {"version": 3, "rowVersion": 1, "overrides": {"chartPalette": "default"}},
        {"version": 3, "rowVersion": 1, "overrides": {"accent": None}},
        {
            "version": 3,
            "rowVersion": 1,
            "overrides": {"accent": {"kind": "custom", "seed": "#FFF"}},
        },
        {
            "version": 3,
            "rowVersion": 1,
            "overrides": {"accent": {"kind": "custom", "seed": "#11223344"}},
        },
        {
            "version": 3,
            "rowVersion": 1,
            "overrides": {"accent": {"kind": "preset", "registryVersion": 2, "id": "ocean"}},
        },
        {
            "version": 3,
            "rowVersion": 1,
            "overrides": {"background": {"kind": "custom", "light": "#FFFFFF"}},
        },
        {
            "version": 3,
            "rowVersion": 1,
            "overrides": {"typographyProfile": {"type": "custom", "id": "system"}},
        },
    ],
)
def test_personal_v3_config_rejects_ambiguous_or_unknown_input(payload: dict) -> None:
    with pytest.raises(PersonalThemeModelError):
        normalize_personal_theme_config(json.dumps(payload))


@pytest.mark.unit
def test_personal_v3_config_enforces_transport_and_canonical_size_limit() -> None:
    valid = '{"version":3,"rowVersion":1,"overrides":{}}'
    oversized = valid + " " * MAX_PERSONAL_THEME_CONFIG_BYTES

    with pytest.raises(PersonalThemeModelError, match="exceeds"):
        normalize_personal_theme_config(oversized)


@pytest.mark.unit
def test_legacy_projection_excludes_chart_and_is_deterministic() -> None:
    legacy = {
        "version": 1,
        "mode": "dark",
        "colorProfile": {"type": "preset", "id": "forest"},
        "typographyProfile": {"type": "preset", "id": "readable"},
        "typeScale": "extra-large",
        "density": "compact",
        "radius": "soft",
        "motion": "reduced",
        "chartPalette": "colorblind-safe",
    }

    projected = project_legacy_appearance_config(
        legacy,
        legacy_checksum="sha256:" + "b" * 64,
    )

    assert projected == {
        "version": 3,
        "rowVersion": 0,
        "overrides": {
            "mode": "dark",
            "accent": {"kind": "preset", "registryVersion": 1, "id": "forest"},
            "typographyProfile": {"type": "preset", "id": "readable"},
            "typeScale": "extra-large",
            "density": "compact",
            "radius": "soft",
            "motion": "reduced",
        },
        "origin": {
            "kind": "legacy_projection",
            "legacyChecksum": "sha256:" + "b" * 64,
        },
    }


@pytest.mark.unit
@pytest.mark.parametrize("profile_id", ["bowhead", "ocean", "forest", "plum", "graphite"])
def test_legacy_projection_supports_every_frozen_profile(profile_id: str) -> None:
    projected = project_legacy_appearance_config(
        {
            "version": 1,
            "colorProfile": {"type": "preset", "id": profile_id},
        },
        legacy_checksum="sha256:" + "c" * 64,
    )

    assert projected["overrides"]["accent"] == {
        "kind": "preset",
        "registryVersion": 1,
        "id": profile_id,
    }


@pytest.mark.unit
def test_legacy_projection_rejects_non_v1_input() -> None:
    with pytest.raises(PersonalThemeModelError, match="version must be 1"):
        project_legacy_appearance_config(
            {"version": 3},
            legacy_checksum="sha256:" + "d" * 64,
        )


@pytest.mark.unit
def test_personal_internal_keys_are_explicitly_reserved() -> None:
    assert KEY_APPEARANCE_THEME_CONFIG_V3 == "appearance_theme_config_v3"
    assert KEY_APPEARANCE_THEME_UNDO_V3 == "appearance_theme_undo_v3"
    assert frozenset({KEY_APPEARANCE_THEME_CONFIG_V3, KEY_APPEARANCE_THEME_UNDO_V3}) == RESERVED_USER_SETTING_KEYS


@pytest.mark.unit
def test_personal_undo_receipt_is_strict_and_canonical() -> None:
    raw = json.dumps(
        {
            "version": 1,
            "resetId": "0123456789abcdef0123456789abcdef",
            "idempotencyDigest": "sha256:" + "1" * 64,
            "restorable": True,
            "before": {
                "wasMaterialized": False,
                "overrides": {"accent": {"kind": "preset", "registryVersion": 1, "id": "ocean"}},
                "origin": {
                    "kind": "legacy_projection",
                    "legacyChecksum": "sha256:" + "2" * 64,
                },
            },
            "beforeChecksum": "sha256:" + "3" * 64,
            "expectedAfter": {
                "rowVersion": 5,
                "checksum": "sha256:" + "4" * 64,
            },
            "expiresAt": "2026-09-05T14:42:00Z",
        }
    )

    normalized = normalize_personal_theme_undo(raw)
    parsed = parse_personal_theme_undo(normalized)

    assert parsed["resetId"] == "0123456789abcdef0123456789abcdef"
    assert parsed["before"]["wasMaterialized"] is False
    assert parsed["expiresAt"] == "2026-09-05T14:42:00Z"


@pytest.mark.unit
@pytest.mark.parametrize(
    "updates",
    [
        {"unknown": True},
        {"resetId": "short"},
        {"restorable": True, "before": None, "beforeChecksum": None},
        {"restorable": False, "before": {"wasMaterialized": True, "overrides": {}}, "beforeChecksum": None},
        {"expiresAt": "not-a-time"},
        {"expectedAfter": {"rowVersion": 0, "checksum": "sha256:" + "4" * 64}},
    ],
)
def test_personal_undo_receipt_rejects_invalid_shapes(updates: dict) -> None:
    payload = {
        "version": 1,
        "resetId": "0123456789abcdef0123456789abcdef",
        "idempotencyDigest": "sha256:" + "1" * 64,
        "restorable": False,
        "before": None,
        "beforeChecksum": None,
        "expectedAfter": {
            "rowVersion": 1,
            "checksum": "sha256:" + "4" * 64,
        },
        "expiresAt": "2026-09-05T14:42:00Z",
    }
    payload.update(updates)

    with pytest.raises(PersonalThemeModelError):
        normalize_personal_theme_undo(json.dumps(payload))
