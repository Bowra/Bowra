"""Shared availability contract and Community policy.

Build composition supplies the Business purchase policy from the private package.
Availability never substitutes for authentication, resource permissions or budgets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from bowhead_core.user_store import UserStore

Mode = Literal["saas", "local"]
Health = Literal["active", "grace", "over_grace", "suspended", "needs_license", "tamper"]


def deployment_mode_from_env() -> Mode:
    """``BOWHEAD_DEPLOYMENT_MODE`` ∈ {saas, local}; default ``local`` for standard private deployment."""
    val = os.environ.get("BOWHEAD_DEPLOYMENT_MODE", "local").strip().lower()
    return "local" if val == "local" else "saas"


@dataclass(frozen=True)
class EntitlementSnapshot:
    """Availability consumed by shared gates; ``None`` means no seat ceiling."""

    team_id: str
    health: Health
    expires_at: str | None = None
    grace_until: str | None = None
    max_seats: int | None = 0
    features: frozenset[str] = field(default_factory=frozenset)
    default_user_tier: str = "free"
    tier_credits: dict[str, int] = field(default_factory=dict)
    deployment_mode: Mode = "saas"
    degraded: bool = False  # The adapter is using its last verified state.
    source: Literal["db", "license", "license_lkg", "community", "none"] = "db"


class EntitlementDenied(Exception):
    """Pure-python denial (no FastAPI in core). The gateway maps it to HTTPException / a scheduler tuple.

    ``reason`` ∈ {over_grace, suspended, seat_exceeded, feature_unlicensed, needs_license, tamper, clock_tamper}.
    """

    def __init__(self, reason: str, http_status: int, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.http_status = http_status
        self.message = message


class EntitlementResolver(Protocol):
    """Availability operations consumed by shared gates and workflows."""

    @property
    def mode(self) -> Mode: ...
    @property
    def enforce(self) -> bool: ...
    async def get(self, team_id: str) -> EntitlementSnapshot: ...
    async def assert_can_run_turn(self, team_id: str | None) -> EntitlementSnapshot | None: ...
    async def assert_can_add_seat(self, team_id: str | None, *, active_count: int) -> None: ...
    async def is_feature_licensed(self, team_id: str | None, feature: str) -> bool: ...
    def is_feature_licensed_from_snapshot(self, snapshot: EntitlementSnapshot, feature: str) -> bool: ...
    async def resolve_team_for_user(self, user_id: str) -> str | None: ...


class CommunityResolver:
    """Installed core capabilities without commercial activation or a seat ceiling."""

    def __init__(self, *, mode: Mode, user_store: UserStore) -> None:
        self._mode = mode
        self._user_store = user_store

    async def get(self, team_id: str) -> EntitlementSnapshot:
        team = await self._user_store.get_team(team_id) if team_id and self._user_store else None
        return EntitlementSnapshot(
            team_id=team_id,
            health="active" if team is not None and team.status == "active" else "suspended",
            max_seats=None,
            features=frozenset({"kb", "mcp", "scheduling"}),
            default_user_tier=team.default_user_tier if team else "free",
            tier_credits=dict(team.tier_credits) if team else {},
            deployment_mode=self.mode,
            source="community",
        )

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def enforce(self) -> bool:
        return True

    async def assert_can_run_turn(self, team_id: str | None) -> EntitlementSnapshot:
        snapshot = await self.get(team_id or "")
        if snapshot.health != "active":
            raise EntitlementDenied("suspended", 403, "The Team is unavailable.")
        return snapshot

    async def assert_can_add_seat(self, team_id: str | None, *, active_count: int) -> None:
        await self.assert_can_run_turn(team_id)

    async def resolve_team_for_user(self, user_id: str) -> str | None:
        user = await self._user_store.get_user(user_id) if user_id else None
        return user.team_id if user else None

    async def is_feature_licensed(self, team_id: str | None, feature: str) -> bool:
        return self.is_feature_licensed_from_snapshot(await self.get(team_id or ""), feature)

    def is_feature_licensed_from_snapshot(self, snapshot: EntitlementSnapshot, feature: str) -> bool:
        return snapshot.health == "active" and feature in snapshot.features
