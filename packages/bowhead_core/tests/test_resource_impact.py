from __future__ import annotations

import base64

import pytest
from bowhead_core.resource_impact import (
    ResourceImpactKeyUnavailable,
    ResourceImpactTokenInvalid,
    ResourceImpactTokenStale,
    digest_json,
    issue_impact_token,
    resource_change_request_digest,
    verify_impact_token,
)

ROOT = base64.urlsafe_b64encode(b"x" * 32).decode()


def _issue(**overrides: object) -> str:
    values = {
        "team_id": "team_1",
        "actor_user_id": "usr_1",
        "resource_kind": "tool_source",
        "resource_id": "ots_1",
        "command": {"action": "delete"},
        "resource_state": {"status": "enabled", "version": 2},
        "dependency_impact": {"agents": ["a1"], "schedules": ["j1"]},
        "now": 100,
        "root_key": ROOT,
    }
    values.update(overrides)
    return issue_impact_token(**values)  # type: ignore[arg-type]


def test_impact_token_binds_command_state_dependency_and_actor() -> None:
    token = _issue()
    verified = verify_impact_token(
        token,
        team_id="team_1",
        actor_user_id="usr_1",
        resource_kind="tool_source",
        resource_id="ots_1",
        command={"action": "delete"},
        resource_state={"status": "enabled", "version": 2},
        dependency_impact={"agents": ["a1"], "schedules": ["j1"]},
        now=101,
        root_key=ROOT,
    )
    assert verified.command_digest == digest_json({"action": "delete"})

    with pytest.raises(ResourceImpactTokenStale):
        verify_impact_token(
            token,
            team_id="team_1",
            actor_user_id="usr_1",
            resource_kind="tool_source",
            resource_id="ots_1",
            command={"action": "delete"},
            resource_state={"status": "disabled", "version": 2},
            dependency_impact={"agents": ["a1"], "schedules": ["j1"]},
            now=101,
            root_key=ROOT,
        )

    with pytest.raises(ResourceImpactTokenInvalid):
        verify_impact_token(
            token,
            team_id="team_1",
            actor_user_id="usr_2",
            resource_kind="tool_source",
            resource_id="ots_1",
            command={"action": "delete"},
            resource_state={"status": "enabled", "version": 2},
            dependency_impact={"agents": ["a1"], "schedules": ["j1"]},
            now=101,
            root_key=ROOT,
        )


def test_impact_token_expires_and_rejects_tampering() -> None:
    token = _issue()
    with pytest.raises(ResourceImpactTokenStale):
        verify_impact_token(
            token,
            team_id="team_1",
            actor_user_id="usr_1",
            resource_kind="tool_source",
            resource_id="ots_1",
            command={"action": "delete"},
            resource_state={"status": "enabled", "version": 2},
            dependency_impact={"agents": ["a1"], "schedules": ["j1"]},
            now=400,
            root_key=ROOT,
        )
    with pytest.raises(ResourceImpactTokenInvalid):
        verify_impact_token(
            token[:-1] + ("A" if token[-1] != "A" else "B"),
            team_id="team_1",
            actor_user_id="usr_1",
            resource_kind="tool_source",
            resource_id="ots_1",
            command={"action": "delete"},
            resource_state={"status": "enabled", "version": 2},
            dependency_impact={"agents": ["a1"], "schedules": ["j1"]},
            now=101,
            root_key=ROOT,
        )


def test_impact_token_requires_stable_fernet_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    with pytest.raises(ResourceImpactKeyUnavailable):
        issue_impact_token(
            team_id="team_1",
            actor_user_id="usr_1",
            resource_kind="skill",
            resource_id="skill_1",
            command={},
            resource_state={},
            dependency_impact={},
        )


def test_idempotency_request_digest_binds_resource_identity() -> None:
    command = {"action": "delete", "proposed_change": {}}
    first = resource_change_request_digest(
        resource_kind="tool_source",
        resource_id="source_1",
        command=command,
    )
    second = resource_change_request_digest(
        resource_kind="tool_source",
        resource_id="source_2",
        command=command,
    )
    assert first != second
