from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.space_governance import SpaceGovernanceError
from bowhead_core.user_store import UserStore


async def test_authorization_reads_observe_external_revocation_and_space_archive(tmp_path) -> None:
    store = UserStore(tmp_path / "authorization-freshness.db")
    await store.initialize()
    team = await store.create_team(name="Team", max_seats=10)
    owner = await store.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    member = await store.create_user(team_id=team.id, email="member@example.com", password="Passw0rd!")
    workspace = await store.create_local_workspace(team_id=team.id, name="Knowledge", creator_user_id=owner.id)
    await store.add_member(workspace_id=workspace, user_id=member.id, role="member")
    assert await store.get_team_role(member.id, team.id) == "member"
    assert await store.get_member_role(workspace, member.id) == "member"
    assert await store.get_workspace_team_id(workspace) == team.id
    assert await store.get_user_team_id(member.id) == team.id
    assert (await store.get_workspace(workspace))["status"] == "active"

    async with aiosqlite.connect(store.db_path) as external:
        await external.execute("UPDATE team_members SET status='removed' WHERE user_id=? AND team_id=?", (member.id, team.id))
        await external.execute("UPDATE workspace_members SET status='removed' WHERE user_id=? AND workspace_id=?", (member.id, workspace))
        await external.execute("UPDATE users SET status='disabled' WHERE id=?", (member.id,))
        await external.execute("UPDATE workspaces SET status='archived',revision=revision+1 WHERE id=?", (workspace,))
        await external.commit()

    assert await store.get_team_role(member.id, team.id) is None
    assert await store.get_member_role(workspace, member.id) is None
    assert await store.get_user_team_id(member.id) is None
    assert (await store.get_workspace(workspace))["status"] == "archived"
    assert await store.get_workspace_team_id(workspace) == team.id


async def test_add_member_rejects_cross_team_canonical_workspace(tmp_path) -> None:
    store = UserStore(tmp_path / "membership.db")
    await store.initialize()
    team_a = await store.create_team(name="Team A", max_seats=10)
    team_b = await store.create_team(name="Team B", max_seats=10)
    user_a = await store.create_user(
        team_id=team_a.id,
        email="a@example.com",
        password="Passw0rd!",
    )
    user_b = await store.create_user(
        team_id=team_b.id,
        email="b@example.com",
        password="Passw0rd!",
    )
    ws_a = await store.create_local_workspace(
        team_id=team_a.id,
        name="Sales",
        creator_user_id=user_a.id,
    )

    with pytest.raises(ValueError, match="active team membership"):
        await store.add_member(workspace_id=ws_a, user_id=user_b.id, role="member")

    async with aiosqlite.connect(store.db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError, match="active team membership"):
            await db.execute(
                "INSERT INTO workspace_members"
                "(workspace_id, user_id, role, joined_at, status) "
                "VALUES (?, ?, 'member', '2026-01-01T00:00:00Z', 'active')",
                (ws_a, user_b.id),
            )


async def test_user_can_join_second_team_and_its_space(tmp_path) -> None:
    store = UserStore(tmp_path / "multi-team.db")
    await store.initialize()
    team_a = await store.create_team(name="Team A", max_seats=10)
    team_b = await store.create_team(name="Team B", max_seats=10)
    user = await store.create_user(team_id=team_a.id, email="member@example.com", password="Passw0rd!")
    owner_b = await store.create_user(team_id=team_b.id, email="owner@example.com", password="Passw0rd!")
    ws_b = await store.create_local_workspace(team_id=team_b.id, name="Finance", creator_user_id=owner_b.id)

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES ('om_second', ?, ?, 'member', 'active', '2026-01-01T00:00:00Z')",
            (user.id, team_b.id),
        )
        await db.commit()

    await store.add_member(workspace_id=ws_b, user_id=user.id)
    assert await store.user_has_active_team_membership(user.id, team_b.id)
    assert [team["id"] for team in await store.list_active_teams_for_user(user.id)] == [team_a.id, team_b.id]
    assert [row["workspace_id"] for row in await store.list_workspaces_for_user(user.id, team_id=team_b.id)] == [ws_b]


async def test_add_member_rejects_archived_workspace(tmp_path) -> None:
    store = UserStore(tmp_path / "archived-membership.db")
    await store.initialize()
    team = await store.create_team(name="Team", max_seats=10)
    owner = await store.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    member = await store.create_user(team_id=team.id, email="member@example.com", password="Passw0rd!")
    workspace_id = await store.create_local_workspace(
        team_id=team.id,
        name="Archived",
        creator_user_id=owner.id,
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.commit()

    with pytest.raises(SpaceGovernanceError, match="workspace_archived"):
        await store.add_member(
            workspace_id=workspace_id,
            user_id=member.id,
        )

    assert await store.get_member_role(workspace_id, member.id) is None
