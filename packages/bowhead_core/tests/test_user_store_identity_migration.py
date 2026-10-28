"""Legacy identity and invitation migration compatibility."""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore, _now


async def _seed_user(
    store: UserStore,
    *,
    user_id: str,
    status: str,
    days_inactive: int = 0,
) -> None:
    """Seed a user row; set last_active_at to N days ago."""
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, "
            "created_at, last_active_at) "
            "VALUES (?, 'team_a', ?, 1, '', '[]', 'ultra', 0, 0, "
            "?, 'zh-CN', 'UTC', '{}', ?, datetime('now', ?), datetime('now', ?))",
            (
                user_id,
                f"{user_id}@test.com",
                _now(),
                status,
                f"-{days_inactive} days",
                f"-{days_inactive} days",
            ),
        )
        await db.commit()


@pytest.fixture
async def store(tmp_path):
    s = UserStore(str(tmp_path / "users.db"))
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_initialize_migrates_ambiguous_deleted_identity_fail_closed(store):
    await _seed_user(
        store,
        user_id="u_legacy_deleted",
        status="deleted",
        days_inactive=60,
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "DELETE FROM user_store_migrations WHERE name='identity_membership_split_v1'",
        )
        await db.commit()

    await store.initialize()

    migrated = await store.get_user("u_legacy_deleted")
    assert migrated is not None
    assert migrated.status == "legacy_deleted"


@pytest.mark.asyncio
async def test_initialize_deduplicates_active_invites_before_unique_index(store):
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("DROP INDEX uq_team_invite_active")
        for invite_id, created_at in (
            ("oinv_old", "2026-07-01T00:00:00+00:00"),
            ("oinv_new", "2026-07-02T00:00:00+00:00"),
        ):
            await db.execute(
                "INSERT INTO team_invite_tokens("
                "id,team_id,team_role,invited_email,token_hash,status,"
                "created_at,expires_at,token_version) "
                "VALUES (?, 'team_a', 'member', 'duplicate@example.com', ?, "
                "'active', ?, '2999-01-01T00:00:00+00:00', 1)",
                (invite_id, f"hash_{invite_id}", created_at),
            )
        await db.commit()

    await store.initialize()

    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute(
            "SELECT id,status FROM team_invite_tokens WHERE invited_email='duplicate@example.com' ORDER BY id",
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute(
            "SELECT 1 FROM pragma_index_list('team_invite_tokens') WHERE name='uq_team_invite_active' AND \"unique\"=1",
        ) as cur:
            unique_index = await cur.fetchone()
    assert rows == [("oinv_new", "active"), ("oinv_old", "revoked")]
    assert unique_index is not None
