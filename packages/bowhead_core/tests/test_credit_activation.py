from __future__ import annotations

from bowhead_core.credit_activation import (
    CreditTeamContext,
    CreditUsageCandidate,
    credit_capabilities_for_role,
    decorate_credit_attention_actions,
    evaluate_credit_attention,
)


def test_evaluate_low_balance_power_user_recommends_grant() -> None:
    team = CreditTeamContext("team_a", team_credit_balance=50_000, team_credit_quota=100_000)
    rows = [
        CreditUsageCandidate(
            user_id="u_power",
            credit_balance=100,
            credit_quota=1_000,
            spend_7d=240,
            spend_30d=900,
            meaningful_turns_7d=4,
            meaningful_turns_30d=12,
        ),
    ]

    out = evaluate_credit_attention(team, rows, now="2026-06-07T00:00:00+00:00")

    item = next(i for i in out if i.user_id == "u_power")
    assert item.policy_key == "low_balance_power_user"
    assert item.severity == "warning"
    assert item.recommended_action_key == "grant_credit"
    assert "low_balance_power_user" in item.segment_flags


def test_team_pool_critical_disables_user_grant_and_prefers_team_quota() -> None:
    team = CreditTeamContext("team_a", team_credit_balance=100, team_credit_quota=100_000)
    rows = [
        CreditUsageCandidate(
            user_id="u_blocked",
            credit_balance=0,
            credit_quota=1_000,
            spend_7d=500,
            spend_30d=1_500,
            meaningful_turns_7d=5,
            meaningful_turns_30d=12,
            exhausted_blocks_7d=1,
        ),
    ]

    out = evaluate_credit_attention(team, rows, now="2026-06-07T00:00:00+00:00")

    user_item = next(i for i in out if i.user_id == "u_blocked")
    assert user_item.policy_key == "blocked_valuable_user"
    assert user_item.recommended_action_key == "increase_team_quota"

    decorated = decorate_credit_attention_actions(
        user_item,
        credit_capabilities_for_role("team_admin"),
        team,
    )
    assert decorated["recommended_action"]["key"] == "increase_team_quota"
    assert decorated["recommended_action"]["allowed"] is True
    grant = next(a for a in decorated["available_actions"] if a["key"] == "grant_credit")
    assert grant["allowed"] is False
    assert grant["disabled_reason"] == "team_pool_critical"


def test_team_pool_critical_fallbacks_growth_spike_and_disables_user_quota_actions() -> None:
    team = CreditTeamContext("team_a", team_credit_balance=100, team_credit_quota=100_000)
    rows = [
        CreditUsageCandidate(
            user_id="u_growth",
            credit_balance=800,
            credit_quota=1_000,
            spend_7d=500,
            prior_30d_spend=100,
            meaningful_turns_7d=5,
        ),
    ]

    out = evaluate_credit_attention(team, rows, now="2026-06-07T00:00:00+00:00")

    user_item = next(i for i in out if i.user_id == "u_growth")
    assert user_item.policy_key == "growth_spike"
    assert user_item.recommended_action_key == "increase_team_quota"

    decorated = decorate_credit_attention_actions(
        user_item,
        credit_capabilities_for_role("team_admin"),
        team,
    )
    assert decorated["recommended_action"]["key"] == "increase_team_quota"
    quota = next(a for a in decorated["available_actions"] if a["key"] == "edit_user_quota")
    tier = next(a for a in decorated["available_actions"] if a["key"] == "edit_user_tier")
    assert quota["allowed"] is False
    assert quota["disabled_reason"] == "team_pool_critical"
    assert tier["allowed"] is False
    assert tier["disabled_reason"] == "team_pool_critical"

    stale_item = user_item.to_dict()
    stale_item["recommended_action_key"] = "edit_user_quota"
    stale_item["action_intent"] = {"key": "edit_user_quota", "label": "Edit quota"}
    stale_decorated = decorate_credit_attention_actions(
        stale_item,
        credit_capabilities_for_role("team_admin"),
        team,
    )
    assert stale_decorated["recommended_action"]["key"] == "increase_team_quota"


def test_team_pool_warning_recommends_open_billing_action() -> None:
    team = CreditTeamContext("team_a", team_credit_balance=10_000, team_credit_quota=100_000)

    out = evaluate_credit_attention(team, [], now="2026-06-07T00:00:00+00:00")

    item = next(i for i in out if i.policy_key == "team_pool_warning")
    assert item.recommended_action_key == "open_billing"

    decorated = decorate_credit_attention_actions(
        item,
        credit_capabilities_for_role("team_admin"),
        team,
    )
    assert decorated["recommended_action"] == {
        "key": "open_billing",
        "label": "Open billing",
        "allowed": True,
        "disabled_reason": None,
        "capability_required": "credits:read",
    }
    assert any(a["key"] == "open_billing" for a in decorated["available_actions"])
