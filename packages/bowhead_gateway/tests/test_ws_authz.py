"""workspace-datarc-decouple P1 R7 — ws_authz decision + flag tests."""

from __future__ import annotations

from bowhead_gateway.ws_authz import authorize_ws_access, ws_membership_enforce_enabled


def test_enforce_off_allows_anything() -> None:
    # flag off == today: any workspace_id allowed (auto-enroll happens elsewhere)
    assert authorize_ws_access(enforce=False, existing_role=None, ws_team_id="team_b", caller_team_id="team_a") is None


def test_enforce_on_no_caller_team_denied() -> None:
    assert authorize_ws_access(enforce=True, existing_role="member", ws_team_id="team_a", caller_team_id=None) == "no_team"


def test_enforce_on_cross_team_denied() -> None:
    assert authorize_ws_access(enforce=True, existing_role="member", ws_team_id="team_b", caller_team_id="team_a") == "cross_team"


def test_enforce_on_not_a_member_denied() -> None:
    assert authorize_ws_access(enforce=True, existing_role=None, ws_team_id="team_a", caller_team_id="team_a") == "not_a_member"


def test_enforce_on_member_same_team_allowed() -> None:
    assert authorize_ws_access(enforce=True, existing_role="member", ws_team_id="team_a", caller_team_id="team_a") is None


def test_enforce_on_unknown_ws_team_still_requires_membership() -> None:
    # ws_team_id=None (e.g. pre-migration datarc id, no workspaces row) → team leg skipped,
    # but membership is still required → fail-closed without a member row.
    assert authorize_ws_access(enforce=True, existing_role=None, ws_team_id=None, caller_team_id="team_a") == "not_a_member"
    assert authorize_ws_access(enforce=True, existing_role="admin", ws_team_id=None, caller_team_id="team_a") is None


def test_flag_default_off(monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", raising=False)
    assert ws_membership_enforce_enabled() is False


def test_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    assert ws_membership_enforce_enabled() is True


def test_flag_other_values_off(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    assert ws_membership_enforce_enabled() is False
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "yes")  # only "1" enables
    assert ws_membership_enforce_enabled() is False
