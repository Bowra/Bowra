from __future__ import annotations

import pytest
from bowhead_core.theme_branding_flags import (
    ENV_TEAM_BRANDING_RENDER_SAFE_MODE,
    is_team_branding_render_safe_mode,
)


@pytest.mark.unit
def test_team_branding_render_safe_mode_defaults_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_TEAM_BRANDING_RENDER_SAFE_MODE, raising=False)

    assert is_team_branding_render_safe_mode() is False


@pytest.mark.unit
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_team_branding_render_safe_mode_accepts_explicit_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(ENV_TEAM_BRANDING_RENDER_SAFE_MODE, value)

    assert is_team_branding_render_safe_mode() is True


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "unexpected"])
def test_team_branding_render_safe_mode_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(ENV_TEAM_BRANDING_RENDER_SAFE_MODE, value)

    assert is_team_branding_render_safe_mode() is False
