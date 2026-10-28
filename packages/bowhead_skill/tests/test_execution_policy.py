"""Deployment defaults leave room for execution and transport cleanup."""

import pytest
from bowhead_skill.execution_policy import sandbox_default_timeout_s, sandbox_max_timeout_s, skill_wall_timeout_s


def test_default_timeout_and_outer_margin(monkeypatch):
    for name in ("BOWHEAD_SANDBOX_MAX_TIMEOUT", "BOWHEAD_SANDBOX_DEFAULT_TIMEOUT_S", "BOWHEAD_SKILL_WALL_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    assert sandbox_default_timeout_s() == 120
    assert sandbox_max_timeout_s() == 600
    assert skill_wall_timeout_s() == 660
    monkeypatch.setenv("BOWHEAD_SANDBOX_MAX_TIMEOUT", "900")
    assert skill_wall_timeout_s() == 960
    monkeypatch.setenv("BOWHEAD_SANDBOX_DEFAULT_TIMEOUT_S", "1200")
    assert sandbox_default_timeout_s() == 900
    monkeypatch.setenv("BOWHEAD_SKILL_WALL_TIMEOUT_S", "1500")
    assert skill_wall_timeout_s() == 1500


@pytest.mark.parametrize("value", ["garbage", "0", "-10"])
def test_invalid_settings_retain_positive_defaults(monkeypatch, value):
    for name in ("BOWHEAD_SANDBOX_MAX_TIMEOUT", "BOWHEAD_SANDBOX_DEFAULT_TIMEOUT_S", "BOWHEAD_SKILL_WALL_TIMEOUT_S"):
        monkeypatch.setenv(name, value)
    assert (sandbox_default_timeout_s(), sandbox_max_timeout_s(), skill_wall_timeout_s()) == (120, 600, 660)
