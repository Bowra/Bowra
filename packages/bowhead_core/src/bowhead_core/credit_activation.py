"""Credit usage activation policy helpers.

This module is intentionally pure: no database access, no env reads, no
telemetry writes. UserStore materializes policy/state; gateway routes decorate
actions for the current actor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Severity = Literal["info", "warning", "critical"]
ActionKey = Literal[
    "grant_credit",
    "edit_user_quota",
    "edit_user_tier",
    "period_reset",
    "increase_team_quota",
    "open_billing",
    "review_seat",
    "dismiss_attention_item",
    "view_ledger",
]


@dataclass(frozen=True)
class CreditActorCapabilities:
    credits_read: bool = False
    credit_grant: bool = False
    credit_user_quota_write: bool = False
    credit_user_tier_write: bool = False
    credit_period_reset: bool = False
    credit_team_quota_write: bool = False
    credit_attention_dismiss: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class CreditTeamContext:
    team_id: str
    team_credit_balance: int
    team_credit_quota: int

    @property
    def team_pool_pct(self) -> float:
        if self.team_credit_quota <= 0:
            return 0.0
        return max(0.0, self.team_credit_balance / self.team_credit_quota)


@dataclass(frozen=True)
class CreditUsageCandidate:
    user_id: str | None
    workspace_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    tier: str | None = None
    status: str | None = "active"
    credit_balance: int | None = None
    credit_quota: int | None = None
    spend_7d: int = 0
    spend_30d: int = 0
    prior_30d_spend: int = 0
    meaningful_turns_7d: int = 0
    meaningful_turns_30d: int = 0
    last_activity_at: str | None = None
    exhausted_blocks_7d: int = 0
    top_workspace_id: str | None = None
    is_new_user: bool = False

    @property
    def balance_pct(self) -> float | None:
        if self.credit_balance is None or not self.credit_quota:
            return None
        return max(0.0, self.credit_balance / self.credit_quota)


@dataclass(frozen=True)
class CreditAttentionCandidate:
    policy_key: str
    dedupe_key: str
    subject_type: Literal["user", "team", "workspace"]
    user_id: str | None
    workspace_id: str | None
    primary_segment: str
    segment_flags: list[str]
    severity: Severity
    severity_rank: int
    rank_score: float
    recommended_action_key: ActionKey
    action_intent: dict[str, Any]
    evidence: dict[str, Any]
    attention_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ROLE_CAPABILITIES: dict[str, CreditActorCapabilities] = {
    "owner": CreditActorCapabilities(
        credits_read=True,
        credit_grant=True,
        credit_user_quota_write=True,
        credit_user_tier_write=True,
        credit_period_reset=True,
        credit_team_quota_write=True,
        credit_attention_dismiss=True,
    ),
    "team_admin": CreditActorCapabilities(
        credits_read=True,
        credit_grant=True,
        credit_user_quota_write=True,
        credit_user_tier_write=True,
        credit_period_reset=True,
        credit_team_quota_write=True,
        credit_attention_dismiss=True,
    ),
    "audit_admin": CreditActorCapabilities(),
    "member": CreditActorCapabilities(),
}


def credit_capabilities_for_role(role: str | None) -> CreditActorCapabilities:
    """Return credit capabilities for an team role.

    Unknown or missing roles are intentionally treated as member-equivalent.
    """

    return _ROLE_CAPABILITIES.get(role or "member", _ROLE_CAPABILITIES["member"])


def severity_rank(severity: str) -> int:
    return {"info": 1, "warning": 2, "critical": 3}.get(severity, 0)


def _dedupe_key(
    team_id: str,
    *,
    subject_id: str,
    workspace_id: str | None,
    policy_key: str,
) -> str:
    return f"{team_id}:{subject_id}:{workspace_id or '_'}:{policy_key}"


def _action_intent(key: ActionKey, label: str, reason: str) -> dict[str, Any]:
    return {"key": key, "label": label, "reason": reason}


def _candidate(
    *,
    team_id: str,
    policy_key: str,
    subject_type: Literal["user", "team", "workspace"],
    user_id: str | None,
    workspace_id: str | None = None,
    primary_segment: str,
    segment_flags: list[str],
    severity: Severity,
    rank_score: float,
    recommended_action_key: ActionKey,
    action_label: str,
    attention_reason: str,
    evidence: dict[str, Any],
) -> CreditAttentionCandidate:
    if subject_type == "team":
        subject_id = "team"
    elif subject_type == "workspace":
        subject_id = f"workspace:{workspace_id or '_'}"
    else:
        subject_id = user_id or "user:_"
    return CreditAttentionCandidate(
        policy_key=policy_key,
        dedupe_key=_dedupe_key(
            team_id,
            subject_id=subject_id,
            workspace_id=workspace_id,
            policy_key=policy_key,
        ),
        subject_type=subject_type,
        user_id=user_id,
        workspace_id=workspace_id,
        primary_segment=primary_segment,
        segment_flags=segment_flags,
        severity=severity,
        severity_rank=severity_rank(severity),
        rank_score=rank_score,
        recommended_action_key=recommended_action_key,
        action_intent=_action_intent(recommended_action_key, action_label, attention_reason),
        evidence=evidence,
        attention_reason=attention_reason,
    )


def evaluate_credit_attention(
    team_context: CreditTeamContext,
    rows: list[CreditUsageCandidate],
    *,
    now: str,
) -> list[CreditAttentionCandidate]:
    """Evaluate MVP credit attention candidates.

    ``now`` is accepted for deterministic tests and future time-window policies;
    the current MVP rules use pre-aggregated candidate fields.
    """

    del now
    out = _team_pool_candidates(team_context)
    out.extend(item for row in rows if (item := _user_candidate(team_context, row)) is not None)

    return sorted(out, key=lambda c: (-c.severity_rank, -c.rank_score, c.policy_key))


def _team_pool_candidates(team_context: CreditTeamContext) -> list[CreditAttentionCandidate]:
    if team_context.team_credit_quota <= 0:
        return []
    pool_pct = team_context.team_pool_pct
    evidence = {
        "team_credit_balance": team_context.team_credit_balance,
        "team_credit_quota": team_context.team_credit_quota,
        "team_pool_pct": pool_pct,
    }
    if pool_pct < 0.05:
        return [
            _candidate(
                team_id=team_context.team_id,
                policy_key="team_pool_critical",
                subject_type="team",
                user_id=None,
                primary_segment="team_pool_critical",
                segment_flags=["team_pool_critical"],
                severity="critical",
                rank_score=3_500 + max(0, 5 - pool_pct * 100),
                recommended_action_key="increase_team_quota",
                action_label="Increase team quota",
                attention_reason="Team credit pool is critically low",
                evidence=evidence,
            ),
        ]
    if pool_pct < 0.20:
        return [
            _candidate(
                team_id=team_context.team_id,
                policy_key="team_pool_warning",
                subject_type="team",
                user_id=None,
                primary_segment="team_pool_warning",
                segment_flags=["team_pool_warning"],
                severity="warning",
                rank_score=2_200 + max(0, 20 - pool_pct * 100),
                recommended_action_key="open_billing",
                action_label="Open billing",
                attention_reason="Team credit pool is running low",
                evidence=evidence,
            ),
        ]
    return []


def _user_candidate(
    team_context: CreditTeamContext,
    row: CreditUsageCandidate,
) -> CreditAttentionCandidate | None:
    if row.status == "removed":
        return None
    flags = _user_segment_flags(row)
    if not flags:
        return None
    policy = _primary_user_policy(flags, team_context.team_pool_pct)
    balance_pct = row.balance_pct
    return _candidate(
        team_id=team_context.team_id,
        policy_key=policy["policy_key"],
        subject_type="user",
        user_id=row.user_id,
        workspace_id=row.top_workspace_id or row.workspace_id,
        primary_segment=policy["policy_key"],
        segment_flags=flags,
        severity=policy["severity"],
        rank_score=(policy["base_rank"] + row.exhausted_blocks_7d * 100 + row.meaningful_turns_7d * 10 + row.spend_7d / 100),
        recommended_action_key=policy["action"],
        action_label=policy["label"],
        attention_reason=policy["reason"],
        evidence={
            "email": row.email,
            "display_name": row.display_name,
            "tier": row.tier,
            "balance_pct": balance_pct,
            "credit_balance": row.credit_balance,
            "credit_quota": row.credit_quota,
            "spend_7d": row.spend_7d,
            "spend_30d": row.spend_30d,
            "meaningful_turns_7d": row.meaningful_turns_7d,
            "meaningful_turns_30d": row.meaningful_turns_30d,
            "exhausted_blocks_7d": row.exhausted_blocks_7d,
            "top_workspace_id": row.top_workspace_id,
            "team_pool_pct": team_context.team_pool_pct,
        },
    )


def _user_segment_flags(row: CreditUsageCandidate) -> list[str]:
    flags: list[str] = []
    balance_pct = row.balance_pct
    if row.exhausted_blocks_7d > 0 and row.meaningful_turns_30d > 0:
        flags.append("blocked_valuable_user")
    if balance_pct is not None and balance_pct < 0.05 and row.meaningful_turns_7d >= 1:
        flags.append("critical_balance_active_user")
    if _is_low_balance_power_user(row, balance_pct):
        flags.append("low_balance_power_user")
    if _is_growth_spike(row):
        flags.append("growth_spike")
    if row.is_new_user and row.meaningful_turns_7d >= 3 and (balance_pct is None or balance_pct < 0.50):
        flags.append("new_user_activated")
    return flags


def _is_low_balance_power_user(row: CreditUsageCandidate, balance_pct: float | None) -> bool:
    if balance_pct is None or balance_pct >= 0.20:
        return False
    return row.meaningful_turns_7d >= 3 or row.spend_7d >= max(100, int((row.credit_quota or 0) * 0.15))


def _is_growth_spike(row: CreditUsageCandidate) -> bool:
    projected_30d = row.spend_7d * 4
    growth_threshold = max(
        int(row.prior_30d_spend * 1.5),
        int((row.credit_quota or 0) * 0.30),
    )
    return row.meaningful_turns_7d >= 3 and projected_30d > growth_threshold > 0


def _primary_user_policy(
    flags: list[str],
    team_pool_pct: float,
) -> dict[str, Any]:
    team_pool_critical = team_pool_pct < 0.05
    grant_or_team_action: ActionKey = "increase_team_quota" if team_pool_pct < 0.05 else "grant_credit"
    grant_or_team_label = "Increase team quota" if grant_or_team_action == "increase_team_quota" else "Grant credits"
    if "blocked_valuable_user" in flags:
        return {
            "policy_key": "blocked_valuable_user",
            "severity": "critical",
            "action": grant_or_team_action,
            "label": grant_or_team_label,
            "reason": "Valuable user is blocked by exhausted credits",
            "base_rank": 3_300,
        }
    if "critical_balance_active_user" in flags:
        return {
            "policy_key": "critical_balance_active_user",
            "severity": "critical",
            "action": grant_or_team_action,
            "label": grant_or_team_label,
            "reason": "Active user has critically low credit balance",
            "base_rank": 3_000,
        }
    if "low_balance_power_user" in flags:
        return {
            "policy_key": "low_balance_power_user",
            "severity": "warning",
            "action": grant_or_team_action,
            "label": grant_or_team_label,
            "reason": "Power user is running low on credits",
            "base_rank": 2_400,
        }
    if "growth_spike" in flags:
        return {
            "policy_key": "growth_spike",
            "severity": "warning",
            "action": "increase_team_quota" if team_pool_critical else "edit_user_quota",
            "label": "Increase team quota" if team_pool_critical else "Edit quota",
            "reason": "User spend is growing faster than baseline",
            "base_rank": 2_000,
        }
    return {
        "policy_key": "new_user_activated",
        "severity": "info",
        "action": "increase_team_quota" if team_pool_critical else "grant_credit",
        "label": "Increase team quota" if team_pool_critical else "Grant credits",
        "reason": "New user is showing meaningful activation",
        "base_rank": 1_200,
    }


def decorate_credit_attention_actions(
    item: CreditAttentionCandidate | dict[str, Any],
    actor_capabilities: CreditActorCapabilities,
    team_context: CreditTeamContext,
) -> dict[str, Any]:
    """Decorate a materialized item with actor-specific action availability."""

    base = item.to_dict() if isinstance(item, CreditAttentionCandidate) else dict(item)
    action_key = base.get("recommended_action_key") or base.get("recommended_action", {}).get("key")
    action_intent = base.get("action_intent") or {}
    if team_context.team_pool_pct < 0.05 and action_key in {
        "grant_credit",
        "edit_user_quota",
        "edit_user_tier",
    }:
        action_key = "increase_team_quota"
        action_intent = {**action_intent, "key": action_key, "label": "Increase team quota"}
        base["recommended_action_key"] = action_key
        base["action_intent"] = action_intent
    available = [
        _decorate_action("grant_credit", "Grant credits", actor_capabilities, team_context),
        _decorate_action("edit_user_quota", "Edit quota", actor_capabilities, team_context),
        _decorate_action("edit_user_tier", "Edit tier", actor_capabilities, team_context),
        _decorate_action("increase_team_quota", "Increase team quota", actor_capabilities, team_context),
        _decorate_action("open_billing", "Open billing", actor_capabilities, team_context),
        _decorate_action("dismiss_attention_item", "Dismiss", actor_capabilities, team_context),
        _decorate_action("view_ledger", "View ledger", actor_capabilities, team_context),
    ]
    action_by_key = {a["key"]: a for a in available}
    recommended = action_by_key.get(action_key)
    if recommended is None:
        recommended = _decorate_action(
            action_key or "view_ledger",
            str(action_intent.get("label") or action_key or "View ledger"),
            actor_capabilities,
            team_context,
        )
    elif action_intent.get("label"):
        recommended = {**recommended, "label": action_intent["label"]}
    base["recommended_action"] = recommended
    base["available_actions"] = available
    return base


def _decorate_action(
    key: str,
    label: str,
    caps: CreditActorCapabilities,
    team_context: CreditTeamContext,
) -> dict[str, Any]:
    capability_required: str | None = None
    allowed = True
    disabled_reason: str | None = None

    if key == "grant_credit":
        capability_required = "credits:grant"
        allowed = caps.credit_grant
        if allowed and team_context.team_pool_pct < 0.05:
            allowed = False
            disabled_reason = "team_pool_critical"
    elif key == "edit_user_quota":
        capability_required = "credits:user_quota:write"
        allowed = caps.credit_user_quota_write
        if allowed and team_context.team_pool_pct < 0.05:
            allowed = False
            disabled_reason = "team_pool_critical"
    elif key == "edit_user_tier":
        capability_required = "credits:user_tier:write"
        allowed = caps.credit_user_tier_write
        if allowed and team_context.team_pool_pct < 0.05:
            allowed = False
            disabled_reason = "team_pool_critical"
    elif key == "period_reset":
        capability_required = "credits:period_reset"
        allowed = caps.credit_period_reset
    elif key == "increase_team_quota":
        capability_required = "credits:team_quota:write"
        allowed = caps.credit_team_quota_write
    elif key == "open_billing":
        capability_required = "credits:read"
        allowed = caps.credits_read
    elif key == "dismiss_attention_item":
        capability_required = "credits:attention:dismiss"
        allowed = caps.credit_attention_dismiss
    elif key == "view_ledger":
        capability_required = "credits:read"
        allowed = caps.credits_read
    else:
        allowed = False
        disabled_reason = "unknown_action"

    if not allowed and disabled_reason is None:
        disabled_reason = "insufficient_capability"
    return {
        "key": key,
        "label": label,
        "allowed": allowed,
        "disabled_reason": disabled_reason,
        "capability_required": capability_required,
    }
