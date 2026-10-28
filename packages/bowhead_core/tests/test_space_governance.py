from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from bowhead_core.space_governance import (
    SpaceGovernanceError,
    normalize_space_name,
)
from bowhead_core.user_store import UserStore


@pytest.fixture
async def store(tmp_path) -> UserStore:
    value = UserStore(tmp_path / "space-governance.db")
    await value.initialize()
    return value


async def _seed_owner(store: UserStore) -> tuple[str, str]:
    team = await store.create_team(name="Governance Team", max_seats=20)
    owner = await store.create_user(
        team_id=team.id,
        email="owner@example.com",
        password="Passw0rd!",
    )
    await store.set_team_creator(team.id, owner.id)
    return team.id, owner.id


async def test_normalize_space_name_uses_nfkc_strip_and_casefold() -> None:
    assert normalize_space_name("  ＳＡＬＥＳ  ") == ("SALES", "sales")
    with pytest.raises(SpaceGovernanceError, match="workspace_name_required"):
        normalize_space_name("　")


async def test_create_requires_team_governance_and_rejects_normalized_duplicate(
    store: UserStore,
) -> None:
    team_id, owner_id = await _seed_owner(store)
    member = await store.create_user(
        team_id=team_id,
        email="member@example.com",
        password="Passw0rd!",
    )

    with pytest.raises(SpaceGovernanceError) as denied:
        await store.create_governed_workspace(
            team_id=team_id,
            actor_user_id=member.id,
            name="Sales",
        )
    assert denied.value.code == "not_team_admin"

    created = await store.create_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        name="  ＳＡＬＥＳ ",
    )
    assert created["name"] == "SALES"
    assert created["caller_membership"] == "admin"
    assert created["revision"] == 1

    with pytest.raises(SpaceGovernanceError) as duplicate:
        await store.create_governed_workspace(
            team_id=team_id,
            actor_user_id=owner_id,
            name="sales",
        )
    assert duplicate.value.code == "duplicate_workspace_name"


async def test_concurrent_archive_preserves_one_active_space(
    store: UserStore,
) -> None:
    team_id, owner_id = await _seed_owner(store)
    first = await store.create_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        name="First",
    )
    second = await store.create_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        name="Second",
    )

    results = await asyncio.gather(
        store.archive_governed_workspace(
            team_id=team_id,
            actor_user_id=owner_id,
            workspace_id=first["workspace_id"],
            expected_revision=first["revision"],
        ),
        store.archive_governed_workspace(
            team_id=team_id,
            actor_user_id=owner_id,
            workspace_id=second["workspace_id"],
            expected_revision=second["revision"],
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    errors = [result for result in results if isinstance(result, SpaceGovernanceError)]
    assert len(errors) == 1
    assert errors[0].code == "last_active_space"
    assert await store.count_active_workspaces(team_id) == 1


async def test_archive_clears_current_preference_and_restore_repairs_admin(
    store: UserStore,
) -> None:
    team_id, owner_id = await _seed_owner(store)
    first = await store.create_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        name="First",
        set_as_current=True,
    )
    await store.create_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        name="Second",
    )
    archived = await store.archive_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        workspace_id=first["workspace_id"],
        expected_revision=first["revision"],
    )

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?",
            (first["workspace_id"], owner_id),
        )
        await db.commit()
        async with db.execute(
            "SELECT json_extract(preferences_json,'$.last_workspace_id') FROM users WHERE id=?",
            (owner_id,),
        ) as cur:
            preference = await cur.fetchone()
    assert preference == (None,)

    with pytest.raises(SpaceGovernanceError) as required:
        await store.restore_governed_workspace(
            team_id=team_id,
            actor_user_id=owner_id,
            workspace_id=first["workspace_id"],
            expected_revision=archived["revision"],
        )
    assert required.value.code == "restore_admin_required"

    restored = await store.restore_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        workspace_id=first["workspace_id"],
        expected_revision=archived["revision"],
        recovery_admin_user_id=owner_id,
    )
    assert restored["status"] == "active"
    assert restored["admin_count"] == 1
    assert await store.get_member_role(first["workspace_id"], owner_id) == "admin"


async def test_openapi_restore_requires_or_repairs_an_active_admin(
    store: UserStore,
) -> None:
    team_id, owner_id = await _seed_owner(store)
    first = await store.create_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        name="First",
    )
    await store.create_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        name="Second",
    )
    await store.archive_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        workspace_id=first["workspace_id"],
        expected_revision=first["revision"],
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?",
            (first["workspace_id"], owner_id),
        )
        await db.commit()

    with pytest.raises(SpaceGovernanceError, match="restore_admin_required"):
        await store.restore_openapi_workspace(
            team_id=team_id,
            workspace_id=first["workspace_id"],
        )

    assert await store.restore_openapi_workspace(
        team_id=team_id,
        workspace_id=first["workspace_id"],
        recovery_admin_user_id=owner_id,
    )
    assert await store.get_member_role(first["workspace_id"], owner_id) == "admin"


async def test_stale_metadata_update_does_not_overwrite(
    store: UserStore,
) -> None:
    team_id, owner_id = await _seed_owner(store)
    created = await store.create_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        name="Original",
    )
    updated = await store.update_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        workspace_id=created["workspace_id"],
        expected_revision=1,
        name="Current",
    )
    assert updated["revision"] == 2

    with pytest.raises(SpaceGovernanceError) as stale:
        await store.update_governed_workspace(
            team_id=team_id,
            actor_user_id=owner_id,
            workspace_id=created["workspace_id"],
            expected_revision=1,
            name="Stale",
        )
    assert stale.value.code == "stale_workspace_update"
    assert stale.value.details == {"current_revision": 2}
