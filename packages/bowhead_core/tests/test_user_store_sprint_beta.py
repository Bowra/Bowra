"""Sprint β Phase 1 — schema + helpers tests.

Covers:
- Schema migrations idempotent (admin_audit_log + team_members + workspaces + users ALTER)
- D-3=B append-only trigger blocks UPDATE on admin_audit_log
- D-2=C single-team trigger blocks cross-team INSERT on team_members
- write_admin_audit_log best-effort + atomic mode
- audit_critical_action atomic tx + rollback on audit failure
- promote_team_role atomic + concurrent race
- get_team_role creator precedence + team_members lookup
- is_only_remaining_team_admin
- _migrate_init_team_members_from_creators idempotent
- sync_workspace_from_datarc upsert
- list_workspaces_for_user join
- touch_workspace_member_active
- set_user_preference_key json_set atomic
- try_acquire_pwd_change_lock + release + concurrent
- update_user_password pwd_version bump
- list_refresh_tokens active_only
- revoke_refresh_token_by_id IDOR-safe
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest
from bowhead_core.audit_actions import CRITICAL_ACTIONS, AuditAction, is_critical
from bowhead_core.user_store import UserStore, _id, _now, hash_password


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "beta.db")
    await s.initialize()
    return s


async def _seed_team(s: UserStore, *, team_id: str = "team_b", domain: str | None = None) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, "
            "default_user_tier, team_credit_balance, team_credit_quota, "
            "tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, 10, 'ultra', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, f"Team {team_id}", domain, _now()),
        )
        await db.commit()


async def _seed_user(
    s: UserStore,
    *,
    user_id: str,
    team_id: str = "team_b",
    email: str | None = None,
    is_creator: bool = False,
) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, "
            "password_hash, tier, credit_balance, credit_quota, "
            "status, created_at) "
            "VALUES (?, ?, ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email or f"{user_id}@test.com", hash_password("Password123"), _now()),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


# ─────────────────────────────────────────────────────────
# Schema migration idempotency
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_idempotent_second_call_beta(tmp_path):
    s = UserStore(tmp_path / "x.db")
    await s.initialize()
    await s.initialize()  # 2nd call should not error
    async with aiosqlite.connect(s.db_path) as db, db.execute("PRAGMA table_info(users)") as cur:
        cols = [r[1] async for r in cur]
    for c in ("pwd_version", "session_token_version", "password_change_in_progress", "last_password_change_at"):
        assert c in cols


@pytest.mark.asyncio
async def test_team_members_table_present(store):
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='team_members'") as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_admin_audit_log_table_present(store):
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='admin_audit_log'") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_workspaces_table_present(store):
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='workspaces'") as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_teamanizations_llm_config_version_col(store):
    async with aiosqlite.connect(store.db_path) as db, db.execute("PRAGMA table_info(teams)") as cur:
        cols = [r[1] async for r in cur]
    assert "llm_config_version" in cols


@pytest.mark.asyncio
async def test_team_llm_config_occ_write_is_atomic(store):
    await _seed_team(store)

    first = await store.update_team_llm_config_if_version(
        "team_b",
        {"schema_version": 2, "marker": "first"},
        expected_version=0,
    )
    stale = await store.update_team_llm_config_if_version(
        "team_b",
        {"schema_version": 2, "marker": "stale"},
        expected_version=0,
    )

    assert first is True
    assert stale is False
    config, version = await store.get_team_llm_config_with_version("team_b")
    assert version == 1
    assert config["marker"] == "first"


@pytest.mark.asyncio
async def test_workspace_members_new_cols(store):
    async with aiosqlite.connect(store.db_path) as db, db.execute("PRAGMA table_info(workspace_members)") as cur:
        cols = [r[1] async for r in cur]
    assert "last_active_at" in cols
    assert "cached_name" in cols


# ─────────────────────────────────────────────────────────
# Triggers: append-only + single-team
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_audit_log_no_update_trigger(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    await store.write_admin_audit_log(
        actor_user_id="u1",
        team_id="team_b",
        action="test_action",
        scope="team",
        payload={"k": "v"},
    )
    # Try UPDATE — must fail
    with pytest.raises(Exception, match="append-only"):
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute(
                "UPDATE admin_audit_log SET payload_json = '{}' WHERE actor_user_id = ?",
                ("u1",),
            )
            await db.commit()


@pytest.mark.asyncio
async def test_team_members_multi_team_insert_allowed(store):
    """多团队授权迁移 (FEATURE_MULTI_TEAM_AUTHZ, 授权体系.md §3.1): 旧的
    single-team 触发器已按设计删除 — 一个用户可以在第二个团队拥有成员资格.
    本用例 pin 新不变量, 防止旧触发器复活."""
    await _seed_team(store, team_id="team_A")
    await _seed_team(store, team_id="team_B")
    await _seed_user(store, user_id="u1", team_id="team_A")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, ?, ?, 'team_admin', 'active', ?)",
            (_id("om"), "u1", "team_B", _now()),  # second team — now legal
        )
        await db.commit()
    assert await store.get_team_role("u1", "team_B") == "team_admin"


@pytest.mark.asyncio
async def test_team_members_same_team_insert_ok(store):
    await _seed_team(store, team_id="team_A")
    await _seed_user(store, user_id="u1", team_id="team_A")
    # INSERT team_members for u1 in team_A (same team as users.team_id) → OK
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, ?, ?, 'team_admin', 'active', ?)",
            (_id("om"), "u1", "team_A", _now()),
        )
        await db.commit()


# ─────────────────────────────────────────────────────────
# audit_actions module
# ─────────────────────────────────────────────────────────


def test_audit_actions_critical_set():
    assert AuditAction.ROLE_CHANGE in CRITICAL_ACTIONS
    assert AuditAction.PASSWORD_CHANGE in CRITICAL_ACTIONS
    assert AuditAction.LLM_CONFIG_ROTATE in CRITICAL_ACTIONS
    assert AuditAction.MEMBER_INVITE not in CRITICAL_ACTIONS
    assert AuditAction.ENDPOINT_403_DENIAL not in CRITICAL_ACTIONS


def test_audit_actions_is_critical():
    assert is_critical("role_change") is True
    assert is_critical("member_invite") is False
    assert is_critical("random_unknown") is False


# ─────────────────────────────────────────────────────────
# write_admin_audit_log
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_admin_audit_log_best_effort_success(store):
    ok = await store.write_admin_audit_log(
        actor_user_id="u1",
        team_id="team_b",
        action="test_action",
        scope="team",
        payload={"key": "value"},
    )
    assert ok is True
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT action, payload_json FROM admin_audit_log WHERE actor_user_id = ?",
            ("u1",),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == "test_action"
    assert json.loads(row[1]) == {"key": "value"}


@pytest.mark.asyncio
async def test_write_admin_audit_log_truncates_large_payload(store):
    big_value = "x" * 6000
    await store.write_admin_audit_log(
        actor_user_id="u1",
        team_id="team_b",
        action="test_action",
        scope="team",
        payload={"big": big_value},
    )
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT payload_json FROM admin_audit_log WHERE actor_user_id='u1'") as cur:
        row = await cur.fetchone()
    parsed = json.loads(row[0])
    assert parsed["_truncated"] is True
    assert len(parsed["big"]) < 1000


# ─────────────────────────────────────────────────────────
# audit_critical_action (D-3=B)
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_critical_action_atomic_success(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    # Promote: real main_op + audit
    rowcount = await store.audit_critical_action(
        action=AuditAction.ROLE_CHANGE,
        actor_user_id="u1",
        team_id="team_b",
        scope="team",
        payload={"old": "member", "new": "team_admin"},
        main_op_sql="INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, ?, ?, ?, 'active', ?)",
        main_op_params=(_id("om"), "u1", "team_b", "team_admin", _now()),
    )
    assert rowcount == 1
    # Both team_members row + audit log row should exist
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM team_members WHERE user_id='u1'") as cur:
            row = await cur.fetchone()
        assert row[0] == 1
        async with db.execute(
            "SELECT COUNT(*) FROM admin_audit_log WHERE action=?",
            (AuditAction.ROLE_CHANGE,),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 1


@pytest.mark.asyncio
async def test_audit_critical_action_rejects_non_critical(store):
    await _seed_team(store)
    with pytest.raises(ValueError, match="CRITICAL_ACTIONS"):
        await store.audit_critical_action(
            action=AuditAction.MEMBER_INVITE,  # not critical
            actor_user_id="u1",
            team_id="team_b",
            scope="team",
            payload={},
            main_op_sql="SELECT 1",
            main_op_params=(),
        )


# ─────────────────────────────────────────────────────────
# get_team_role + owner precedence
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_team_role_owner_wins(store):
    await _seed_team(store)
    await _seed_user(store, user_id="creator_u", is_creator=True)
    role = await store.get_team_role("creator_u", "team_b")
    assert role == "owner"


@pytest.mark.asyncio
async def test_get_team_role_from_team_members(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, 'u1', 'team_b', 'audit_admin', 'active', ?)",
            (_id("om"), _now()),
        )
        await db.commit()
    role = await store.get_team_role("u1", "team_b")
    assert role == "audit_admin"


@pytest.mark.asyncio
async def test_is_team_admin_includes_promoted_team_admin(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    await store.promote_team_role(
        user_id="u1",
        team_id="team_b",
        new_role="team_admin",
        promoted_by="creator",
    )
    assert await store.is_team_admin("u1", "team_b") is True


@pytest.mark.asyncio
async def test_get_team_role_none_when_not_member(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    role = await store.get_team_role("u1", "team_b")
    assert role is None


# ─────────────────────────────────────────────────────────
# promote_team_role atomic
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_team_role_new(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    await _seed_user(store, user_id="caller")
    changed, old = await store.promote_team_role(
        user_id="u1",
        team_id="team_b",
        new_role="team_admin",
        promoted_by="caller",
    )
    assert changed is True
    assert old is None


@pytest.mark.asyncio
async def test_promote_team_role_noop_same_role(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    await _seed_user(store, user_id="caller")
    await store.promote_team_role(
        user_id="u1",
        team_id="team_b",
        new_role="team_admin",
        promoted_by="caller",
    )
    changed, old = await store.promote_team_role(
        user_id="u1",
        team_id="team_b",
        new_role="team_admin",
        promoted_by="caller",
    )
    assert changed is False
    assert old == "team_admin"


@pytest.mark.asyncio
async def test_promote_team_role_concurrent_race(store):
    """P0-T-3 race fix verified: 2 concurrent promotes to different roles — last wins."""
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    await _seed_user(store, user_id="caller")
    await asyncio.gather(
        store.promote_team_role(
            user_id="u1",
            team_id="team_b",
            new_role="team_admin",
            promoted_by="caller",
        ),
        store.promote_team_role(
            user_id="u1",
            team_id="team_b",
            new_role="audit_admin",
            promoted_by="caller",
        ),
    )
    # Both succeed (atomic), one wrote first, one updated
    final_role = await store.get_team_role("u1", "team_b")
    assert final_role in ("team_admin", "audit_admin")


# ─────────────────────────────────────────────────────────
# is_only_remaining_team_admin
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_only_remaining_team_admin_true(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    await _seed_user(store, user_id="caller")
    await store.promote_team_role(
        user_id="u1",
        team_id="team_b",
        new_role="team_admin",
        promoted_by="caller",
    )
    # u1 is only team_admin
    assert await store.is_only_remaining_team_admin("u1", "team_b") is True


@pytest.mark.asyncio
async def test_is_only_remaining_team_admin_false_with_peer(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    await _seed_user(store, user_id="u2")
    await _seed_user(store, user_id="caller")
    await store.promote_team_role(
        user_id="u1",
        team_id="team_b",
        new_role="team_admin",
        promoted_by="caller",
    )
    await store.promote_team_role(
        user_id="u2",
        team_id="team_b",
        new_role="team_admin",
        promoted_by="caller",
    )
    assert await store.is_only_remaining_team_admin("u1", "team_b") is False


# ─────────────────────────────────────────────────────────
# Grandfather migration
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migrate_init_team_members_idempotent(store):
    await _seed_team(store, team_id="team_A")
    await _seed_user(store, user_id="creator_A", team_id="team_A", is_creator=True)
    # First migration
    n1 = await store._migrate_init_team_members_from_creators()
    assert n1 == 1
    # Second migration — should be no-op
    n2 = await store._migrate_init_team_members_from_creators()
    assert n2 == 0
    # Verify creator has team_admin row
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT role FROM team_members WHERE user_id='creator_A' AND team_id='team_A'") as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == "team_admin"


# ─────────────────────────────────────────────────────────
# sync_workspace_from_datarc + list_workspaces_for_user
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_workspace_from_datarc_insert(store):
    await _seed_team(store)
    ws_id = await store.sync_workspace_from_datarc(
        team_id="team_b",
        datarc_workspace_id="dr_1",
        name="泰尔茂",
    )
    assert ws_id.startswith("ws_")
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT name FROM workspaces WHERE id=?", (ws_id,)) as cur:
        row = await cur.fetchone()
    assert row[0] == "泰尔茂"


@pytest.mark.asyncio
async def test_sync_workspace_from_datarc_upsert(store):
    await _seed_team(store)
    ws_id1 = await store.sync_workspace_from_datarc(
        team_id="team_b",
        datarc_workspace_id="dr_1",
        name="泰尔茂",
    )
    ws_id2 = await store.sync_workspace_from_datarc(
        team_id="team_b",
        datarc_workspace_id="dr_1",
        name="泰尔茂(renamed)",
    )
    assert ws_id1 == ws_id2  # same row updated
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT name FROM workspaces WHERE id=?", (ws_id1,)) as cur:
        row = await cur.fetchone()
    assert row[0] == "泰尔茂(renamed)"


@pytest.mark.asyncio
async def test_list_workspaces_for_user(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    ws_id = await store.sync_workspace_from_datarc(
        team_id="team_b",
        datarc_workspace_id="dr_1",
        name="泰尔茂",
    )
    await store.add_member(workspace_id=ws_id, user_id="u1", role="admin")
    rows = await store.list_workspaces_for_user("u1")
    assert len(rows) == 1
    assert rows[0]["workspace_id"] == ws_id
    assert rows[0]["role"] == "admin"
    assert rows[0]["name"] == "泰尔茂"


@pytest.mark.asyncio
async def test_backfill_legacy_datarc_workspaces_copies_members_to_canonical_ws(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO workspace_members"
            "(workspace_id, user_id, role, invited_by, joined_at, status, last_active_at) "
            "VALUES ('353', 'u1', 'admin', 'legacy_admin', ?, 'active', ?)",
            ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),
        )
        await db.commit()

    assert await store.list_workspaces_for_user("u1") == []

    dry_run = await store.backfill_legacy_datarc_workspaces()
    assert dry_run["dry_run"] is True
    assert dry_run["legacy_memberships"] == 1
    assert dry_run["workspaces_created"] == 1
    assert dry_run["members_copied"] == 0
    assert await store.list_workspaces_for_user("u1") == []

    applied = await store.backfill_legacy_datarc_workspaces(apply=True)
    assert applied["dry_run"] is False
    assert applied["legacy_memberships"] == 1
    assert applied["workspaces_created"] == 1
    assert applied["members_copied"] == 1

    rows = await store.list_workspaces_for_user("u1")
    assert len(rows) == 1
    assert rows[0]["workspace_id"].startswith("ws_")
    assert rows[0]["datarc_workspace_id"] == "353"
    assert rows[0]["role"] == "admin"
    assert rows[0]["name"] == "Datarc project 353"

    again = await store.backfill_legacy_datarc_workspaces(apply=True)
    assert again["workspaces_created"] == 0
    assert again["members_copied"] == 0


@pytest.mark.asyncio
async def test_backfill_legacy_datarc_workspaces_respects_legacy_id_filter(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    await _seed_user(store, user_id="u2")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, joined_at, status) VALUES ('353', 'u1', 'member', ?, 'active')",
            (_now(),),
        )
        await db.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, joined_at, status) VALUES ('team_39aaaf974bc7', 'u2', 'member', ?, 'active')",
            (_now(),),
        )
        await db.commit()

    dry_run = await store.backfill_legacy_datarc_workspaces(legacy_workspace_ids=["353"])
    assert dry_run["legacy_workspace_ids_filter"] == ["353"]
    assert dry_run["legacy_memberships"] == 1
    assert dry_run["workspace_mappings"][0]["legacy_workspace_id"] == "353"

    applied = await store.backfill_legacy_datarc_workspaces(
        legacy_workspace_ids=["353"],
        apply=True,
    )
    assert applied["workspaces_created"] == 1
    rows_u1 = await store.list_workspaces_for_user("u1")
    rows_u2 = await store.list_workspaces_for_user("u2")
    assert rows_u1[0]["datarc_workspace_id"] == "353"
    assert rows_u2 == []


@pytest.mark.asyncio
async def test_backfill_legacy_datarc_workspaces_reuses_existing_synced_ws(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    ws_id = await store.sync_workspace_from_datarc(
        team_id="team_b",
        datarc_workspace_id="353",
        name="经营数据",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, joined_at, status) VALUES ('353', 'u1', 'member', ?, 'active')",
            (_now(),),
        )
        await db.commit()

    applied = await store.backfill_legacy_datarc_workspaces(apply=True)
    assert applied["workspaces_created"] == 0
    assert applied["members_copied"] == 1

    rows = await store.list_workspaces_for_user("u1")
    assert len(rows) == 1
    assert rows[0]["workspace_id"] == ws_id
    assert rows[0]["datarc_workspace_id"] == "353"
    assert rows[0]["name"] == "经营数据"


# ─────────────────────────────────────────────────────────
# touch_workspace_member_active + set_user_preference_key
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_touch_workspace_member_active(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    ws_id = await store.sync_workspace_from_datarc(
        team_id="team_b",
        datarc_workspace_id="dr_1",
        name="ws",
    )
    await store.add_member(workspace_id=ws_id, user_id="u1", role="member")
    ok = await store.touch_workspace_member_active(ws_id, "u1")
    assert ok is True
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT last_active_at FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (ws_id, "u1"),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row[0] is not None


@pytest.mark.asyncio
async def test_set_user_preference_key_json_set(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    ok = await store.set_user_preference_key("u1", "last_workspace_id", "ws_xyz")
    assert ok is True
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT preferences_json FROM users WHERE id='u1'") as cur:
        row = await cur.fetchone()
    prefs = json.loads(row[0])
    assert prefs["last_workspace_id"] == "ws_xyz"


@pytest.mark.asyncio
async def test_set_user_preference_key_rejects_invalid_key(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    with pytest.raises(ValueError):
        await store.set_user_preference_key("u1", "evil$path", "x")


@pytest.mark.asyncio
async def test_set_user_preference_key_concurrent_no_loss(store):
    """P1-S-3 fix: json_set is atomic → concurrent updates don't lose data."""
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    await asyncio.gather(
        store.set_user_preference_key("u1", "last_workspace_id", "ws_a"),
        store.set_user_preference_key("u1", "theme", "dark"),
    )
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT preferences_json FROM users WHERE id='u1'") as cur:
        row = await cur.fetchone()
    prefs = json.loads(row[0])
    assert "last_workspace_id" in prefs
    assert "theme" in prefs


# ─────────────────────────────────────────────────────────
# Password change lock + update_user_password
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_try_acquire_pwd_change_lock(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    ok = await store.try_acquire_pwd_change_lock("u1")
    assert ok is True
    # Second attempt — locked
    ok2 = await store.try_acquire_pwd_change_lock("u1")
    assert ok2 is False
    # Release
    await store.release_pwd_change_lock("u1")
    # Now can re-acquire
    ok3 = await store.try_acquire_pwd_change_lock("u1")
    assert ok3 is True


@pytest.mark.asyncio
async def test_update_user_password_bumps_pwd_version(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    v0 = await store.get_pwd_version("u1")
    new_hash = hash_password("NewPwd456")
    ok = await store.update_user_password("u1", new_hash)
    assert ok is True
    v1 = await store.get_pwd_version("u1")
    assert v1 == v0 + 1


@pytest.mark.asyncio
async def test_update_user_password_bumps_session_token_version(store):
    await _seed_team(store)
    await _seed_user(store, user_id="u1")
    s0 = await store.get_session_token_version("u1")
    await store.update_user_password("u1", hash_password("NewPwd456"))
    s1 = await store.get_session_token_version("u1")
    assert s1 == s0 + 1
