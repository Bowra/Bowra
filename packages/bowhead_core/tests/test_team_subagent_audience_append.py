"""TeamSubagentStore.add_team_subagent_audience_user — access-request audience append."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.subagent_write_boundary import TeamSubagentWriteBoundaryError
from bowhead_core.team_subagent_store import (
    TeamSubagentAudienceForbiddenError,
    TeamSubagentAudienceInvalidError,
    TeamSubagentRevisionWriterRequiredError,
    TeamSubagentStore,
)
from bowhead_core.user_store import TeamSubagent, UserStore

pytestmark = pytest.mark.integration

_TEAM = "team_append"
_TS = "2026-09-01T00:00:00Z"


async def _seed_member(db_path, user_id: str, *, membership_status: str = "active") -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR IGNORE INTO teams(id,name,max_seats,status,created_at) VALUES (?,?,20,'active',?)", (_TEAM, _TEAM, _TS))
        await db.execute(
            "INSERT INTO users(id,team_id,email,status,created_at) VALUES (?,?,?,'active',?)", (user_id, _TEAM, f"{user_id}@x.test", _TS)
        )
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,'member',?,?)",
            (f"tm_{user_id}", user_id, _TEAM, membership_status, _TS),
        )
        await db.commit()


async def _setup(tmp_path):
    db_path = tmp_path / "bowhead.db"
    await UserStore(db_path).initialize()
    access = AccessControlStore(db_path)
    await access.initialize()
    store = TeamSubagentStore(db_path)
    await store.initialize()
    for user in ("u_owner", "u_viewer", "u_bob"):
        await _seed_member(db_path, user)
    await _seed_member(db_path, "u_gone", membership_status="removed")
    await store.create_team_subagent(
        TeamSubagent(
            id="osa_a",
            team_id=_TEAM,
            name="append-agent",
            tool_refs=["bi_query"],
            visibility="creator_only",
            created_by="u_owner",
            created_at=_TS,
            updated_at=_TS,
        )
    )
    return db_path, access, store


async def _allow_rows(access):
    grants = await access.list_resource_grants(_TEAM, resource_kind="team_subagent", resource_id="osa_a")
    return {(g.id, g.principal_type, g.principal_id, g.permission, g.expires_at) for g in grants if g.effect == "allow"}


def _add(store, access, user_id="u_bob", actor="u_owner"):
    return store.add_team_subagent_audience_user(
        _TEAM, "osa_a", user_id=user_id, access_store=access, actor_user_id=actor, audit_action="subagent_audience_changed"
    )


async def test_private_audience_becomes_custom_with_the_new_member(tmp_path) -> None:
    _, access, store = await _setup(tmp_path)
    epoch = await access.get_policy_epoch(_TEAM)
    result = await _add(store, access)
    assert result.changed and result.mode == "custom" and result.user_ids == ("u_bob",)
    assert {(t, p, perm, exp) for _, t, p, perm, exp in await _allow_rows(access)} == {("user", "u_bob", "use", None)}
    assert await access.get_policy_epoch(_TEAM) == epoch + 1


async def test_append_preserves_existing_grant_rows_and_is_idempotent(tmp_path) -> None:
    _, access, store = await _setup(tmp_path)
    await store.replace_team_subagent_audience(
        _TEAM, "osa_a", mode="custom", user_ids=["u_viewer"], access_store=access, actor_user_id="u_owner", audit_action="subagent_audience_changed"
    )
    before = await _allow_rows(access)
    await _add(store, access)
    after = await _allow_rows(access)
    assert before < after and len(after) == 2
    again = await _add(store, access)
    assert again.changed is False and await _allow_rows(access) == after


async def test_teamwide_audience_is_a_no_op(tmp_path) -> None:
    _, access, store = await _setup(tmp_path)
    await store.replace_team_subagent_audience(
        _TEAM, "osa_a", mode="teamwide", access_store=access, actor_user_id="u_owner", audit_action="subagent_audience_changed"
    )
    before = await _allow_rows(access)
    assert (await _add(store, access)).changed is False
    assert await _allow_rows(access) == before


async def test_only_the_current_owner_may_append_and_only_active_members(tmp_path) -> None:
    _, access, store = await _setup(tmp_path)
    with pytest.raises(TeamSubagentAudienceForbiddenError):
        await _add(store, access, actor="u_viewer")
    with pytest.raises(TeamSubagentAudienceInvalidError):
        await _add(store, access, user_id="u_gone")
    assert await _allow_rows(access) == set()


async def test_release_cutover_refuses_and_leaves_releases_and_grants_untouched(tmp_path) -> None:
    db_path, access, store = await _setup(tmp_path)

    async def counts():
        # Every Audience fact a release snapshot is derived from: grant rows plus the
        # aggregate's audience projection and version.
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT id, principal_id, revoked_at FROM resource_grants ORDER BY id") as cur:
                grants = await cur.fetchall()
            async with db.execute("SELECT audience_mode, visibility, row_version FROM team_subagent WHERE id='osa_a'") as cur:
                aggregate = await cur.fetchone()
            return grants, aggregate

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE team_subagent_schema_meta SET release_semantics_activated_at=? WHERE singleton=1",
            (datetime.now(UTC).isoformat(),),
        )
        await db.commit()
    before = await counts()
    with pytest.raises(TeamSubagentRevisionWriterRequiredError):
        await _add(store, access)
    assert await counts() == before


async def test_generic_grant_path_still_rejects_subagent_writes(tmp_path) -> None:
    _, access, _ = await _setup(tmp_path)
    with pytest.raises(TeamSubagentWriteBoundaryError):
        await access.create_or_update_resource_grant(
            team_id=_TEAM, resource_kind="team_subagent", resource_id="osa_a", principal_type="user", principal_id="u_bob", permission="use"
        )
