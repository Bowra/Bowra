"""i18n_flag 单测：feature flag (per-workspace + 全局 kill)."""

from __future__ import annotations

import pytest
from bowhead_core.i18n_flag import (
    ENV_KILL_SWITCH,
    WORKSPACE_FLAG_KEY,
    is_i18n_enabled_for_workspace,
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


class TestWorkspaceFlag:
    """2026-08-18：由 opt-in 翻成 **opt-out**（user 拍板）。

    界面语言不是 A/B 实验；关着时后端退回 zh-CN 而前端只看 cookie /
    `navigator.language`，两边判据不同会让「英文浏览器 + 没点过切换器」的用户看到混排。
    """

    def test_missing_settings_defaults_enabled(self) -> None:
        # Before the default changed, these two asserted False. Change the criterion, not remove assertions: the default is the tested behavior.
        assert is_i18n_enabled_for_workspace(None) is True
        assert is_i18n_enabled_for_workspace({}) is True

    def test_flag_true_enables(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ENV_KILL_SWITCH, raising=False)
        assert is_i18n_enabled_for_workspace({WORKSPACE_FLAG_KEY: "true"}) is True

    def test_flag_false_disables(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """★ 正对照：单个 workspace 仍关得掉 —— 别把「默认开」做成「关不掉」。"""
        monkeypatch.delenv(ENV_KILL_SWITCH, raising=False)
        assert is_i18n_enabled_for_workspace({WORKSPACE_FLAG_KEY: "false"}) is False

    @pytest.mark.parametrize("val", ["maybe", "", "TRUE", "0"])
    def test_only_explicit_false_disables(
        self,
        monkeypatch: pytest.MonkeyPatch,
        val: str,
    ) -> None:
        """除了显式 "false"，其余取值一律开（含空串与拼错的值）。"""
        monkeypatch.delenv(ENV_KILL_SWITCH, raising=False)
        assert is_i18n_enabled_for_workspace({WORKSPACE_FLAG_KEY: val}) is True

    def test_kill_switch_overrides_default_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """★ 正对照：翻默认之后，全局 kill 仍必须一键压倒一切（那是唯一的退路）。"""
        monkeypatch.setenv(ENV_KILL_SWITCH, "1")
        assert is_i18n_enabled_for_workspace(None) is False
        assert is_i18n_enabled_for_workspace({}) is False

    def test_kill_switch_overrides_workspace_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENV_KILL_SWITCH, "1")
        assert is_i18n_enabled_for_workspace({WORKSPACE_FLAG_KEY: "true"}) is False

    def test_case_insensitive(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ENV_KILL_SWITCH, raising=False)
        assert is_i18n_enabled_for_workspace({WORKSPACE_FLAG_KEY: "TRUE"}) is True
        assert is_i18n_enabled_for_workspace({WORKSPACE_FLAG_KEY: "True"}) is True
