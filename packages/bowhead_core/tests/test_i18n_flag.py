"""Tests for the deployment-level language rollback."""

from __future__ import annotations

import pytest
from bowhead_core.i18n_flag import (
    ENV_KILL_SWITCH,
    is_kill_switch_active,
)


class TestKillSwitch:
    def test_unset_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_KILL_SWITCH, raising=False)
        assert is_kill_switch_active() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        val: str,
    ) -> None:
        monkeypatch.setenv(ENV_KILL_SWITCH, val)
        assert is_kill_switch_active() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        val: str,
    ) -> None:
        monkeypatch.setenv(ENV_KILL_SWITCH, val)
        assert is_kill_switch_active() is False
