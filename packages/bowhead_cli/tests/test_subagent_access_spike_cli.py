"""Stage 0A Subagent canonical access spike contract tests."""

from __future__ import annotations

import json

import pytest
from bowhead_cli.main import app
from bowhead_cli.subagent_access_spike import (
    AccessFacts,
    AccessResolution,
    resolve_access_policy,
    run_subagent_access_spike,
)
from typer.testing import CliRunner

runner = CliRunner()


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (
            AccessFacts(
                actor_active=True,
                is_owner=True,
                owner_active=True,
                is_team_governor=False,
                governance_action_allowed=False,
                audience_mode="private",
                has_user_allow=False,
                has_group_allow=False,
                has_security_deny=True,
                lifecycle_state="active",
                status="enabled",
            ),
            AccessResolution(
                can_manage=True,
                can_consume=False,
                can_govern=False,
                access_reason=None,
                denial_reason="blocked_by_security_policy",
            ),
        ),
        (
            AccessFacts(
                actor_active=True,
                is_owner=False,
                owner_active=True,
                is_team_governor=False,
                governance_action_allowed=False,
                audience_mode="custom",
                has_user_allow=True,
                has_group_allow=True,
                has_security_deny=False,
                lifecycle_state="active",
                status="enabled",
            ),
            AccessResolution(
                can_manage=False,
                can_consume=True,
                can_govern=False,
                access_reason="user_grant",
                denial_reason=None,
            ),
        ),
        (
            AccessFacts(
                actor_active=True,
                is_owner=False,
                owner_active=True,
                is_team_governor=True,
                governance_action_allowed=True,
                audience_mode="private",
                has_user_allow=True,
                has_group_allow=False,
                has_security_deny=False,
                lifecycle_state="active",
                status="enabled",
            ),
            AccessResolution(
                can_manage=False,
                can_consume=False,
                can_govern=True,
                access_reason=None,
                denial_reason="audience_not_allowed",
            ),
        ),
    ],
)
def test_pure_policy_freezes_manage_consume_govern_precedence(
    facts: AccessFacts,
    expected: AccessResolution,
) -> None:
    assert resolve_access_policy(facts) == expected


def test_spike_proves_sql_policy_and_surface_parity() -> None:
    report = run_subagent_access_spike()

    assert report.scope == "synthetic_isolated_acl_sql_spike"
    assert all(report.checks.values())
    assert report.checks["sql_policy_parity"]
    assert report.checks["surface_parity"]
    assert report.checks["query_plan_indexed"]

    by_name = {scenario["name"]: scenario for scenario in report.scenarios}
    assert by_name["teamwide_user_deny"]["resolution"] == {
        "can_manage": False,
        "can_consume": False,
        "can_govern": False,
        "access_reason": None,
        "denial_reason": "blocked_by_security_policy",
    }
    assert by_name["private_owner_deny"]["resolution"]["can_manage"] is True
    assert by_name["private_owner_deny"]["resolution"]["can_consume"] is False
    assert by_name["inactive_owner_self"]["resolution"]["can_manage"] is False
    assert by_name["inactive_owner_consumer"]["resolution"]["denial_reason"] == ("owner_inactive")
    assert by_name["admin_governance_only"]["resolution"]["can_govern"] is True
    assert by_name["admin_governance_only"]["resolution"]["can_consume"] is False
    assert by_name["cross_team"]["row_found"] is False


def test_spike_records_known_legacy_visibility_drift_without_changing_it() -> None:
    report = run_subagent_access_spike()
    by_name = {scenario["name"]: scenario for scenario in report.scenarios}

    for name in (
        "teamwide_user_deny",
        "teamwide_group_deny_user_allow",
        "private_owner_deny",
        "inactive_owner_self",
    ):
        assert by_name[name]["legacy_visible"] is True
        assert by_name[name]["legacy_drift"] is True

    assert report.methodology["legacy"] == ("current is_visible shortcut is measured, not changed")
    assert report.methodology["production_activation"] == "none"


def test_spike_is_deterministic_and_query_plan_uses_indexes() -> None:
    first = run_subagent_access_spike()
    second = run_subagent_access_spike()

    assert first.to_dict() == second.to_dict()
    assert any("idx_resource_grants_lookup" in step for step in first.query_plan)
    assert any("idx_group_memberships_principal" in step for step in first.query_plan)
    assert not any("SCAN security_deny" in step for step in first.query_plan)


def test_cli_json_emits_safe_structured_spike_evidence() -> None:
    result = runner.invoke(
        app,
        ["admin", "spike-subagent-access", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scope"] == "synthetic_isolated_acl_sql_spike"
    assert all(payload["checks"].values())
    assert payload["methodology"]["production_activation"] == "none"
    assert "access-spike.db" not in result.output
