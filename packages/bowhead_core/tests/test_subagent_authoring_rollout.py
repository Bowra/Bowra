from __future__ import annotations

from bowhead_core.subagent_authoring_rollout import (
    resolve_subagent_authoring_capabilities,
    stable_team_bucket,
)


def test_conversational_authoring_is_on_by_default(monkeypatch) -> None:
    capabilities = resolve_subagent_authoring_capabilities("team_a")

    assert capabilities.creator is True


def test_invalid_percentages_fail_closed(monkeypatch) -> None:
    flag = "FEATURE_SUBAGENT_DRAFT_CHAT_EDIT"
    monkeypatch.setenv(flag, "1")
    monkeypatch.delenv(f"{flag}_TEAM_IDS", raising=False)
    for value in ("", "-1", "101", "not-a-number"):
        monkeypatch.setenv(f"{flag}_PCT", value)
        assert resolve_subagent_authoring_capabilities("team_a").draft_chat_edit is False


def test_stable_team_bucket_is_deterministic_and_feature_scoped() -> None:
    first = stable_team_bucket(
        "FEATURE_SUBAGENT_TEXT_ONLY",
        "team_a",
    )
    assert first == stable_team_bucket(
        "FEATURE_SUBAGENT_TEXT_ONLY",
        "team_a",
    )
    assert 0 <= first < 100
    assert first != stable_team_bucket(
        "FEATURE_SUBAGENT_DRAFT_CHAT_EDIT",
        "team_a",
    )


def test_text_only_rollout_is_team_scoped(monkeypatch) -> None:
    flag = "FEATURE_SUBAGENT_TEXT_ONLY"
    monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv(f"{flag}_TEAM_IDS", "team_a")
    monkeypatch.setenv(f"{flag}_PCT", "0")
    assert resolve_subagent_authoring_capabilities("team_a").text_only is False
    monkeypatch.setenv(flag, "1")
    assert resolve_subagent_authoring_capabilities("team_a").text_only is True
    assert resolve_subagent_authoring_capabilities("team_b").text_only is False
    assert resolve_subagent_authoring_capabilities("").text_only is False


def test_draft_chat_edit_uses_its_own_team_gate(monkeypatch) -> None:
    flag = "FEATURE_SUBAGENT_DRAFT_CHAT_EDIT"
    monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv(f"{flag}_TEAM_IDS", "team_a")
    monkeypatch.setenv(f"{flag}_PCT", "0")
    assert resolve_subagent_authoring_capabilities("team_a").draft_chat_edit is False
    monkeypatch.setenv(flag, "1")
    assert resolve_subagent_authoring_capabilities("team_a").draft_chat_edit is True
    assert resolve_subagent_authoring_capabilities("team_b").draft_chat_edit is False


def test_unified_draft_is_available_without_a_team_rollout() -> None:
    assert resolve_subagent_authoring_capabilities("team_a").unified_draft
    assert resolve_subagent_authoring_capabilities("team_b").unified_draft
    assert not resolve_subagent_authoring_capabilities("").unified_draft
