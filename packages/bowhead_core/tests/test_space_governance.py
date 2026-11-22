from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from bowhead_core.space_governance import (
    SpaceGovernanceError,
    normalize_space_name,
)
from bowhead_core.user_store import UserStore

from bowhead_core import _db, sqlite_domain_runtime


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


def _lose_lease_in_metadata_write(monkeypatch: pytest.MonkeyPatch, times: int, action: str = "workspace_metadata_updated") -> list[int]:
    """Fail the audit write inside a governance transaction, after its UPDATE, with the lease conflict."""
    attempts: list[int] = []
    original = UserStore.write_admin_audit_log

    async def write_admin_audit_log(self: UserStore, **kwargs: object) -> object:
        if kwargs.get("action") == action:
            attempts.append(len(attempts) + 1)
            if len(attempts) <= times:
                raise sqlite_domain_runtime._busy()
        return await original(self, **kwargs)

    monkeypatch.setattr(UserStore, "write_admin_audit_log", write_admin_audit_log)
    return attempts


async def test_metadata_update_repeats_after_losing_the_authority_lease(
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_id, owner_id = await _seed_owner(store)
    created = await store.create_governed_workspace(team_id=team_id, actor_user_id=owner_id, name="Original")
    attempts = _lose_lease_in_metadata_write(monkeypatch, 1)

    updated = await store.update_governed_workspace(
        team_id=team_id,
        actor_user_id=owner_id,
        workspace_id=created["workspace_id"],
        expected_revision=1,
        description="Retail data",
    )

    assert attempts == [1, 2]
    # The failed attempt rolled back its UPDATE, so the revision advanced exactly once.
    assert updated["revision"] == 2
    assert updated["description"] == "Retail data"


async def test_metadata_update_reports_busy_and_writes_nothing_once_the_wait_is_spent(
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_id, owner_id = await _seed_owner(store)
    created = await store.create_governed_workspace(team_id=team_id, actor_user_id=owner_id, name="Original")
    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 0.05)
    attempts = _lose_lease_in_metadata_write(monkeypatch, 10_000)

    with pytest.raises(_db.SQLiteStoreBusy) as busy:
        await store.update_governed_workspace(
            team_id=team_id,
            actor_user_id=owner_id,
            workspace_id=created["workspace_id"],
            expected_revision=1,
            name="Renamed",
        )

    assert busy.value.code == "store_busy"
    assert _db.is_authority_busy(busy.value.__cause__)
    assert len(attempts) > 1
    unchanged = await store.get_workspace(created["workspace_id"])
    assert unchanged is not None
    assert (unchanged["name"], unchanged["revision"]) == ("Original", 1)


async def test_archive_and_restore_repeat_after_losing_the_authority_lease(
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """116 (1.7.61): with Turns running, Space archive and restore answered 500 on a lost lease."""
    team_id, owner_id = await _seed_owner(store)
    await store.create_governed_workspace(team_id=team_id, actor_user_id=owner_id, name="Kept")
    created = await store.create_governed_workspace(team_id=team_id, actor_user_id=owner_id, name="Seasonal")

    archive_attempts = _lose_lease_in_metadata_write(monkeypatch, 1, action="workspace_archived")
    archived = await store.archive_governed_workspace(
        team_id=team_id, actor_user_id=owner_id, workspace_id=created["workspace_id"], expected_revision=1
    )
    # Each attempt rechecks status and revision; the failed one rolled back, so the revision moved once.
    assert archive_attempts == [1, 2]
    assert (archived["status"], archived["revision"]) == ("archived", 2)

    restore_attempts = _lose_lease_in_metadata_write(monkeypatch, 1, action="workspace_restored")
    restored = await store.restore_governed_workspace(
        team_id=team_id, actor_user_id=owner_id, workspace_id=created["workspace_id"], expected_revision=2
    )
    assert restore_attempts == [1, 2]
    assert (restored["status"], restored["revision"]) == ("active", 3)


async def test_archive_reports_busy_and_writes_nothing_once_the_wait_is_spent(
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_id, owner_id = await _seed_owner(store)
    await store.create_governed_workspace(team_id=team_id, actor_user_id=owner_id, name="Kept")
    created = await store.create_governed_workspace(team_id=team_id, actor_user_id=owner_id, name="Seasonal")
    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 0.05)
    attempts = _lose_lease_in_metadata_write(monkeypatch, 10_000, action="workspace_archived")

    with pytest.raises(_db.SQLiteStoreBusy):
        await store.archive_governed_workspace(team_id=team_id, actor_user_id=owner_id, workspace_id=created["workspace_id"], expected_revision=1)

    assert len(attempts) > 1
    unchanged = await store.get_workspace(created["workspace_id"])
    assert unchanged is not None
    assert (unchanged["status"], unchanged["revision"]) == ("active", 1)
