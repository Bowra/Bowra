"""Canonical Subagent access policy and shared-database resolver contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.team_subagent_access import (
    TeamSubagentAccessFacts,
    TeamSubagentAccessResolution,
    TeamSubagentAccessResolver,
    resolve_team_subagent_access,
)
from bowhead_core.team_subagent_store import TeamSubagentStore
from bowhead_core.user_store import TeamSubagent, User, UserStore

_NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)


def _run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True, slots=True)
class _Fixture:
    db_path: str
    team_id: str
    other_team_id: str
    owner: User
    member: User
    admin: User


def _facts(**overrides: object) -> TeamSubagentAccessFacts:
    values: dict[str, object] = {
        "actor_active": True,
        "is_owner": False,
        "owner_active": True,
        "is_team_governor": False,
        "governance_action_allowed": False,
        "audience_mode": "teamwide",
        "has_user_allow": False,
        "has_group_allow": False,
        "has_security_deny": False,
        "lifecycle_state": "active",
        "status": "enabled",
    }
    values.update(overrides)
    return TeamSubagentAccessFacts(**values)


async def _setup_fixture(db_path: str) -> _Fixture:
    users = UserStore(db_path)
    await users.initialize()
    access = AccessControlStore(db_path)
    await access.initialize()
    subagents = TeamSubagentStore(db_path)
    await subagents.initialize()

    team = await users.create_team(name="Canonical access", max_seats=10)
    owner = await users.create_user(
        team_id=team.id,
        email="owner@example.com",
    )
    member = await users.create_user(
        team_id=team.id,
        email="member@example.com",
    )
    admin = await users.create_user(
        team_id=team.id,
        email="admin@example.com",
    )
    await users.set_team_creator(team.id, owner.id)
    await users.promote_team_role(
        user_id=admin.id,
        team_id=team.id,
        new_role="team_admin",
        promoted_by=owner.id,
    )
    other_team = await users.create_team(name="Other team", max_seats=2)
    other_owner = await users.create_user(
        team_id=other_team.id,
        email="other@example.com",
    )

    for subagent_id, name, visibility in (
        ("osa_teamwide", "teamwide-agent", "team_wide"),
        ("osa_denied", "denied-agent", "team_wide"),
        ("osa_private", "private-agent", "creator_only"),
        ("osa_custom_user", "custom-user-agent", "creator_only"),
        ("osa_custom_group", "custom-group-agent", "creator_only"),
    ):
        await subagents.create_team_subagent(
            TeamSubagent(
                id=subagent_id,
                team_id=team.id,
                name=name,
                tool_refs=["bi_query"],
                visibility=visibility,
                created_by=owner.id,
                created_at=_NOW.isoformat(),
                updated_at=_NOW.isoformat(),
            ),
        )
    await subagents.create_team_subagent(
        TeamSubagent(
            id="osa_other_team",
            team_id=other_team.id,
            name="other-agent",
            tool_refs=["bi_query"],
            visibility="team_wide",
            created_by=other_owner.id,
            created_at=_NOW.isoformat(),
            updated_at=_NOW.isoformat(),
        ),
    )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO groups(
              id, team_id, name, status, created_at, updated_at
            ) VALUES ('grp_member', ?, 'Member group', 'active', ?, ?)
            """,
            (team.id, _NOW.isoformat(), _NOW.isoformat()),
        )
        await db.execute(
            """
            INSERT INTO group_memberships(
              team_id, group_id, principal_type, principal_id,
              role, status, created_at
            ) VALUES (?, 'grp_member', 'user', ?, 'member', 'active', ?)
            """,
            (team.id, member.id, _NOW.isoformat()),
        )
        await _insert_grant(
            db,
            grant_id="grant_deny",
            team_id=team.id,
            resource_id="osa_denied",
            principal_type="user",
            principal_id=member.id,
            effect="deny",
        )
        await _insert_grant(
            db,
            grant_id="grant_custom_user",
            team_id=team.id,
            resource_id="osa_custom_user",
            principal_type="user",
            principal_id=member.id,
            effect="allow",
        )
        await _insert_grant(
            db,
            grant_id="grant_custom_group",
            team_id=team.id,
            resource_id="osa_custom_group",
            principal_type="group",
            principal_id="grp_member",
            effect="allow",
        )
        await db.commit()
    return _Fixture(
        db_path=db_path,
        team_id=team.id,
        other_team_id=other_team.id,
        owner=owner,
        member=member,
        admin=admin,
    )


async def _insert_grant(
    db: aiosqlite.Connection,
    *,
    grant_id: str,
    team_id: str,
    resource_id: str,
    principal_type: str,
    principal_id: str,
    effect: str,
) -> None:
    await db.execute(
        """
        INSERT INTO resource_grants(
          id, team_id, resource_kind, resource_id,
          principal_type, principal_id, permission, effect,
          created_at, updated_at
        ) VALUES (?, ?, 'team_subagent', ?, ?, ?, 'use', ?, ?, ?)
        """,
        (
            grant_id,
            team_id,
            resource_id,
            principal_type,
            principal_id,
            effect,
            _NOW.isoformat(),
            _NOW.isoformat(),
        ),
    )


def test_pure_policy_separates_manage_consume_and_govern() -> None:
    consumer = resolve_team_subagent_access(_facts())
    assert consumer == TeamSubagentAccessResolution(
        can_manage=False,
        can_consume=True,
        can_govern=False,
        access_reason="teamwide",
        denial_reason=None,
    )
    assert consumer.detail_level("consumer_detail") == "consumer"
    assert consumer.detail_level("manager") == "hidden"

    denied_owner = resolve_team_subagent_access(
        _facts(
            is_owner=True,
            audience_mode="private",
            has_security_deny=True,
        ),
    )
    assert denied_owner.can_manage is True
    assert denied_owner.can_consume is False
    assert denied_owner.denial_reason == "blocked_by_security_policy"
    assert denied_owner.detail_level("manager") == "owner_manager"
    assert denied_owner.detail_level("runtime") == "hidden"

    disabled_owner = resolve_team_subagent_access(
        _facts(is_owner=True, status="disabled"),
    )
    assert disabled_owner.can_manage is True
    assert disabled_owner.can_consume is False
    assert disabled_owner.denial_reason == "status_not_enabled"

    governor = resolve_team_subagent_access(
        _facts(
            is_team_governor=True,
            governance_action_allowed=True,
            audience_mode="private",
        ),
    )
    assert governor.can_govern is True
    assert governor.can_manage is False
    assert governor.can_consume is False
    assert governor.detail_level("governance") == "governance_metadata"
    assert governor.catalog_visible is False


def test_release_a_projection_resolves_legacy_owner_audience_and_deny(
    tmp_path,
) -> None:
    async def go() -> None:
        fixture = await _setup_fixture(str(tmp_path / "access.db"))
        resolver = TeamSubagentAccessResolver(fixture.db_path)

        member = await resolver.resolve_many(
            team_id=fixture.team_id,
            actor_user_id=fixture.member.id,
            now=_NOW,
        )
        assert member["osa_teamwide"].resolution.access_reason == "teamwide"
        assert member["osa_denied"].resolution.can_consume is False
        assert member["osa_denied"].resolution.denial_reason == "blocked_by_security_policy"
        assert member["osa_private"].resolution.catalog_visible is False
        assert member["osa_custom_user"].resolution.access_reason == "user_grant"
        assert member["osa_custom_group"].resolution.access_reason == "group_grant"

        owner = await resolver.resolve(
            team_id=fixture.team_id,
            actor_user_id=fixture.owner.id,
            subagent_id="osa_private",
            now=_NOW,
        )
        assert owner is not None
        assert owner.owner_user_id == fixture.owner.id
        assert owner.facts.audience_mode == "private"
        assert owner.resolution.can_manage is True
        assert owner.resolution.can_consume is True
        assert owner.resolution.access_reason == "owner"

        admin = await resolver.resolve(
            team_id=fixture.team_id,
            actor_user_id=fixture.admin.id,
            subagent_id="osa_private",
            governance_action_allowed=True,
            now=_NOW,
        )
        assert admin is not None
        assert admin.resolution.can_govern is True
        assert admin.resolution.can_manage is False
        assert admin.resolution.can_consume is False

        assert (
            await resolver.resolve(
                team_id=fixture.team_id,
                actor_user_id=fixture.member.id,
                subagent_id="osa_other_team",
                now=_NOW,
            )
            is None
        )

    _run(go())


def test_release_b_activation_stops_legacy_column_fallback(tmp_path) -> None:
    async def go() -> None:
        fixture = await _setup_fixture(str(tmp_path / "activated.db"))
        async with aiosqlite.connect(fixture.db_path) as db:
            await db.execute(
                """
                UPDATE team_subagent
                SET owner_user_id = created_by,
                    audience_mode = CASE
                      WHEN id = 'osa_teamwide' THEN 'private'
                      ELSE 'teamwide'
                    END
                WHERE team_id = ?
                """,
                (fixture.team_id,),
            )
            await db.execute(
                """
                UPDATE team_subagent_schema_meta
                SET owner_semantics_activated_at = ?,
                    audience_semantics_activated_at = ?
                WHERE singleton = 1
                """,
                (_NOW.isoformat(), _NOW.isoformat()),
            )
            await db.commit()

        decision = await TeamSubagentAccessResolver(
            fixture.db_path,
        ).resolve(
            team_id=fixture.team_id,
            actor_user_id=fixture.member.id,
            subagent_id="osa_teamwide",
            now=_NOW,
        )
        assert decision is not None
        assert decision.facts.audience_mode == "private"
        assert decision.resolution.can_consume is False
        assert decision.resolution.denial_reason == "audience_not_allowed"

    _run(go())


def test_resolver_rejects_naive_time(tmp_path) -> None:
    async def go() -> None:
        fixture = await _setup_fixture(str(tmp_path / "naive.db"))
        with pytest.raises(
            ValueError,
            match="team_subagent_access_now_must_be_timezone_aware",
        ):
            await TeamSubagentAccessResolver(fixture.db_path).resolve_many(
                team_id=fixture.team_id,
                actor_user_id=fixture.member.id,
                now=datetime(2026, 7, 29, 10, 0),
            )

    _run(go())
