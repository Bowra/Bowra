"""Tests for UserStore admin credits mutations: grant / set / period_reset / ledger / audit.

V3 admin credits 数据正确性 + transactional + audit + cooldown + 跨 team 隔离.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.user_store import UserStore, _id, _now


async def _seed_team(
    store: UserStore,
    team_id: str = "team_a",
    quota: int = 100_000,
    tier_credits: dict | None = None,
) -> None:
    tier_credits = tier_credits or {"pro": 5000, "ultra": 30000}
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, "Test Team", "test.com", 10, "ultra", quota, quota, json.dumps(tier_credits), "active", _now()),
        )
        await db.commit()


async def _seed_user(
    store: UserStore,
    *,
    user_id: str,
    team_id: str = "team_a",
    email: str | None = None,
    tier: str = "ultra",
    balance: int = 30_000,
    quota: int = 30_000,
    status: str = "active",
) -> None:
    email = email or f"{user_id}@test.com"
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES (?, ?, ?, 1, '', '[]', ?, ?, ?, ?, 'zh-CN', 'UTC', '{}', ?, ?)",
            (user_id, team_id, email, tier, balance, quota, _now(), status, _now()),
        )
        await db.commit()


@pytest.fixture
async def store(tmp_path: Any) -> UserStore:
    db = tmp_path / TARGET_FILES["core"]
    # Current Stores run at the migration head; the user ledger reads the migrated
    # credit_model_usage table (0173, 5bca471f7).
    upgrade(f"sqlite+aiosqlite:///{db}", target="core")
    s = UserStore(str(db))
    await s.initialize()
    return s


# ─── admin_grant two-layer atomicity ─────────────────────────────────────────


async def test_grant_increments_user_decrements_team(store: UserStore) -> None:
    """grant 5000 → user.balance +5000, team.balance -5000."""
    await _seed_team(store, quota=100_000)
    await _seed_user(store, user_id="u_a", balance=10_000)
    new_bal = await store.admin_grant(
        user_id="u_a",
        team_id="team_a",
        amount=5000,
        by_user_id="admin_x",
    )
    assert new_bal == 15_000

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT credit_balance FROM users WHERE id=?", ("u_a",)) as cur:
            assert (await cur.fetchone())["credit_balance"] == 15_000
        async with db.execute("SELECT team_credit_balance FROM teams WHERE id=?", ("team_a",)) as cur:
            assert (await cur.fetchone())["team_credit_balance"] == 95_000


async def test_grant_team_pool_insufficient_raises(store: UserStore) -> None:
    """team pool 仅 1000, grant 5000 → ValueError, 状态不变."""
    await _seed_team(store, quota=1_000)
    await _seed_user(store, user_id="u_a", balance=0)
    with pytest.raises(ValueError, match="team_pool_insufficient"):
        await store.admin_grant(
            user_id="u_a",
            team_id="team_a",
            amount=5000,
            by_user_id="admin_x",
        )
    # rollback verify
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT credit_balance FROM users WHERE id=?", ("u_a",)) as cur:
            assert (await cur.fetchone())["credit_balance"] == 0
        async with db.execute("SELECT team_credit_balance FROM teams WHERE id=?", ("team_a",)) as cur:
            assert (await cur.fetchone())["team_credit_balance"] == 1000


async def test_grant_removed_user_raises_without_mutating_team_pool(store: UserStore) -> None:
    await _seed_team(store, quota=10_000)
    await _seed_user(store, user_id="u_removed", balance=0, status="removed")

    with pytest.raises(ValueError, match="user_not_active"):
        await store.admin_grant(
            user_id="u_removed",
            team_id="team_a",
            amount=1000,
            by_user_id="admin_x",
        )

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT credit_balance FROM users WHERE id = 'u_removed'") as cur:
            user_row = await cur.fetchone()
        async with db.execute("SELECT team_credit_balance FROM teams WHERE id = 'team_a'") as cur:
            team_row = await cur.fetchone()
        async with db.execute("SELECT COUNT(*) AS n FROM credit_ledger WHERE user_id = 'u_removed'") as cur:
            ledger_row = await cur.fetchone()

    assert user_row["credit_balance"] == 0
    assert team_row["team_credit_balance"] == 10_000
    assert ledger_row["n"] == 0


async def test_grant_writes_ledger_and_audit(store: UserStore) -> None:
    await _seed_team(store, quota=100_000)
    await _seed_user(store, user_id="u_a", balance=0)
    await store.admin_grant(
        user_id="u_a",
        team_id="team_a",
        amount=2000,
        by_user_id="admin_x",
        reason_note="test_grant",
    )
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT delta, reason FROM credit_ledger WHERE user_id=? AND reason LIKE 'admin_grant%'",
            ("u_a",),
        ) as cur:
            row = await cur.fetchone()
        assert row["delta"] == 2000
        assert row["reason"] == "admin_grant:admin_x"

        async with db.execute(
            "SELECT event_type, payload_json FROM user_audit_log WHERE event_type='credit_admin_grant'",
        ) as cur:
            audit = await cur.fetchone()
        payload = json.loads(audit["payload_json"])
        assert payload["target_user_id"] == "u_a"
        assert payload["amount"] == 2000
        assert payload["reason"] == "test_grant"


# ─── admin_set_user_credit ────────────────────────


async def test_set_tier_resets_quota_when_flag_true(store: UserStore) -> None:
    """alice tier=pro quota=5000 → set tier=ultra → quota=30000 (auto reset)."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_a", tier="pro", balance=5000, quota=5000)
    after = await store.admin_set_user_credit(
        user_id="u_a",
        team_id="team_a",
        by_user_id="admin",
        tier="ultra",
        reset_quota_with_tier=True,
    )
    assert after["tier"] == "ultra"
    assert after["credit_quota"] == 30_000


async def test_set_tier_keeps_quota_when_flag_false(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_a", tier="pro", balance=5000, quota=5000)
    after = await store.admin_set_user_credit(
        user_id="u_a",
        team_id="team_a",
        by_user_id="admin",
        tier="ultra",
        reset_quota_with_tier=False,
    )
    assert after["tier"] == "ultra"
    assert after["credit_quota"] == 5_000


async def test_set_quota_pulls_balance_when_flag_true(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_a", balance=2000, quota=5000)
    after = await store.admin_set_user_credit(
        user_id="u_a",
        team_id="team_a",
        by_user_id="admin",
        credit_quota=10_000,
        pull_balance_to_quota=True,
    )
    assert after["credit_quota"] == 10_000
    assert after["credit_balance"] == 10_000


async def test_set_balance_writes_ledger(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_a", balance=5000)
    await store.admin_set_user_credit(
        user_id="u_a",
        team_id="team_a",
        by_user_id="admin",
        credit_balance=8000,
    )
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT delta, reason FROM credit_ledger WHERE user_id='u_a' AND reason LIKE 'admin_set_balance%'",
        ) as cur:
            row = await cur.fetchone()
        assert row["delta"] == 3000


async def test_set_cross_team_raises(store: UserStore) -> None:
    """admin (team_a) set u_b (team_b) → ValueError."""
    await _seed_team(store, "team_a")
    await _seed_team(store, "team_b")
    await _seed_user(store, user_id="u_b", team_id="team_b")
    with pytest.raises(ValueError, match="user_not_found_or_cross_team"):
        await store.admin_set_user_credit(
            user_id="u_b",
            team_id="team_a",
            by_user_id="admin_in_a",
            tier="pro",
        )


async def test_set_removed_user_raises(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_dead", status="removed")
    with pytest.raises(ValueError, match="user_removed_cannot_edit"):
        await store.admin_set_user_credit(
            user_id="u_dead",
            team_id="team_a",
            by_user_id="admin",
            tier="pro",
        )


async def test_set_audit_payload_has_before_after(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_a", tier="pro", quota=5000, balance=5000)
    await store.admin_set_user_credit(
        user_id="u_a",
        team_id="team_a",
        by_user_id="admin",
        tier="ultra",
        reset_quota_with_tier=True,
    )
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT payload_json FROM user_audit_log WHERE event_type='credit_admin_set_user'",
        ) as cur:
            row = await cur.fetchone()
        p = json.loads(row["payload_json"])
        assert p["before"]["tier"] == "pro"
        assert p["after"]["tier"] == "ultra"
        assert p["after"]["quota"] == 30_000


# ─── admin_period_reset ────────────────────────────


async def test_period_reset_preview_only(store: UserStore) -> None:
    """confirm=False → 不真改, 返 plan."""
    await _seed_team(store, quota=100_000)
    await _seed_user(store, user_id="u_a", balance=10_000, quota=30_000)
    out = await store.admin_period_reset(
        team_id="team_a",
        by_user_id="admin",
        mode="balance_only",
        confirm=False,
    )
    assert out["ok"] is False
    assert out["affected_users"] == 1
    assert len(out["preview"]["users"]) == 1
    # No actual change
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT credit_balance FROM users WHERE id='u_a'") as cur:
            assert (await cur.fetchone())["credit_balance"] == 10_000


async def test_period_reset_balance_only_confirms(store: UserStore) -> None:
    """confirm=True balance_only → balance 拉回 quota."""
    await _seed_team(store, quota=100_000)
    await _seed_user(store, user_id="u_a", balance=10_000, quota=30_000)
    out = await store.admin_period_reset(
        team_id="team_a",
        by_user_id="admin",
        mode="balance_only",
        confirm=True,
    )
    assert out["ok"] is True
    assert out["affected_users"] == 1
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT credit_balance, credit_quota FROM users WHERE id='u_a'") as cur:
            row = await cur.fetchone()
        assert row["credit_balance"] == 30_000
        assert row["credit_quota"] == 30_000  # quota unchanged
        async with db.execute("SELECT team_credit_balance FROM teams WHERE id='team_a'") as cur:
            assert (await cur.fetchone())["team_credit_balance"] == 100_000


async def test_period_reset_balance_and_quota_uses_tier_credits(store: UserStore) -> None:
    """balance_and_quota 按 tier_credits 重写."""
    await _seed_team(store, tier_credits={"pro": 5000, "ultra": 50000})
    # alice's custom quota=99999 (will be overwritten)
    await _seed_user(store, user_id="u_alice", tier="ultra", balance=10_000, quota=99_999)
    await _seed_user(store, user_id="u_bob", tier="pro", balance=2_000, quota=5_000)
    out = await store.admin_period_reset(
        team_id="team_a",
        by_user_id="admin",
        mode="balance_and_quota",
        confirm=True,
    )
    assert out["ok"] is True
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT credit_quota, credit_balance FROM users WHERE id='u_alice'") as cur:
            row = await cur.fetchone()
        assert row["credit_quota"] == 50_000  # Overwrite using tier_credits ultra
        assert row["credit_balance"] == 50_000


async def test_period_reset_preview_lists_custom_overrides(store: UserStore) -> None:
    """preview 必显 custom quota override."""
    await _seed_team(store, tier_credits={"pro": 5000, "ultra": 30000})
    await _seed_user(store, user_id="u_alice", tier="ultra", quota=100_000)  # Custom
    await _seed_user(store, user_id="u_bob", tier="ultra", quota=30_000)  # Standard
    out = await store.admin_period_reset(
        team_id="team_a",
        by_user_id="admin",
        mode="balance_and_quota",
        confirm=False,
    )
    overrides = out["preview"]["custom_quota_overrides"]
    assert len(overrides) == 1
    assert overrides[0]["user_id"] == "u_alice"
    assert overrides[0]["new_quota"] == 30_000


async def test_period_reset_cooldown(store: UserStore) -> None:
    """连续两次 reset → 第二次抛 RuntimeError cooldown."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    await store.admin_period_reset(
        team_id="team_a",
        by_user_id="admin",
        mode="balance_only",
        confirm=True,
    )
    with pytest.raises(RuntimeError, match=r"cooldown:\d+"):
        await store.admin_period_reset(
            team_id="team_a",
            by_user_id="admin",
            mode="balance_only",
            confirm=True,
        )


async def test_period_reset_audit_has_full_snapshot(store: UserStore) -> None:
    """audit payload 含每用户 before/after, 可人工 rollback."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_a", balance=15_000, quota=30_000)
    await store.admin_period_reset(
        team_id="team_a",
        by_user_id="admin",
        mode="balance_only",
        confirm=True,
    )
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT payload_json FROM user_audit_log WHERE event_type='credit_admin_period_reset'",
        ) as cur:
            row = await cur.fetchone()
        p = json.loads(row["payload_json"])
        assert p["mode"] == "balance_only"
        assert len(p["snapshot"]) == 1
        assert p["snapshot"][0]["before"]["balance"] == 15_000
        assert p["snapshot"][0]["after"]["balance"] == 30_000


async def test_period_reset_only_affects_own_team(store: UserStore) -> None:
    await _seed_team(store, "team_a")
    await _seed_team(store, "team_b")
    await _seed_user(store, user_id="u_a", team_id="team_a", balance=10_000, quota=30_000)
    await _seed_user(store, user_id="u_b", team_id="team_b", balance=5_000, quota=10_000)
    await store.admin_period_reset(
        team_id="team_a",
        by_user_id="admin",
        mode="balance_only",
        confirm=True,
    )
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT credit_balance FROM users WHERE id='u_a'") as cur:
            assert (await cur.fetchone())["credit_balance"] == 30_000  # reset
        async with db.execute("SELECT credit_balance FROM users WHERE id='u_b'") as cur:
            assert (await cur.fetchone())["credit_balance"] == 5_000  # untouched


async def test_period_reset_invalid_mode(store: UserStore) -> None:
    await _seed_team(store)
    with pytest.raises(ValueError, match="invalid mode"):
        await store.admin_period_reset(
            team_id="team_a",
            by_user_id="admin",
            mode="dont_know",
            confirm=True,
        )


# ─── credits_user_ledger ───────────────────────────


async def test_user_ledger_returns_user_entries(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    # Directly insert 3 ledger rows.
    async with aiosqlite.connect(store.db_path) as db:
        for i, (delta, reason) in enumerate([(-100, "turn"), (-200, "turn"), (500, "admin_grant:x")]):
            await db.execute(
                "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
                "delta, reason, tokens_input, tokens_output, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_id("led"), "u_a", "team_a", None, f"t_{i}", delta, reason, 100, 50, _now()),
            )
        await db.commit()
    items, total = await store.credits_user_ledger("u_a", team_id="team_a")
    assert total == 3
    types = sorted([i["type"] for i in items])
    assert types == ["admin_grant", "turn", "turn"]


async def test_user_ledger_type_filter(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    async with aiosqlite.connect(store.db_path) as db:
        for delta, reason in [(-100, "turn"), (500, "admin_grant:x")]:
            await db.execute(
                "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
                "delta, reason, tokens_input, tokens_output, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_id("led"), "u_a", "team_a", None, _id("t"), delta, reason, 0, 0, _now()),
            )
        await db.commit()
    items, total = await store.credits_user_ledger(
        "u_a",
        team_id="team_a",
        type_filter="admin_grant",
    )
    assert total == 1
    assert items[0]["type"] == "admin_grant"


async def test_user_ledger_cross_team_isolated(store: UserStore) -> None:
    await _seed_team(store, "team_a")
    await _seed_team(store, "team_b")
    await _seed_user(store, user_id="u_a", team_id="team_a")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
            "delta, reason, tokens_input, tokens_output, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_id("led"), "u_a", "team_a", None, _id("t"), -100, "turn", 0, 0, _now()),
        )
        await db.commit()
    # query as if from team_b
    _items, total = await store.credits_user_ledger("u_a", team_id="team_b")
    assert total == 0


async def test_user_ledger_pagination(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    async with aiosqlite.connect(store.db_path) as db:
        for i in range(50):
            await db.execute(
                "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
                "delta, reason, tokens_input, tokens_output, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_id(f"led{i:03d}"), "u_a", "team_a", None, f"t_{i}", -10, "turn", 0, 0, _now()),
            )
        await db.commit()
    items, total = await store.credits_user_ledger(
        "u_a",
        team_id="team_a",
        page=2,
        size=20,
    )
    assert total == 50
    assert len(items) == 20


# ─── credits_audit_log ─────────────────────────────


async def test_audit_log_returns_credit_admin_events(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    # Trigger one credit_admin_grant and one credit_admin_set_user.
    await store.admin_grant(
        user_id="u_a",
        team_id="team_a",
        amount=1000,
        by_user_id="admin",
    )
    await store.admin_set_user_credit(
        user_id="u_a",
        team_id="team_a",
        by_user_id="admin",
        credit_balance=2000,
    )
    items, total = await store.credits_audit_log("team_a")
    assert total == 2
    types = sorted([i["event_type"] for i in items])
    assert types == ["credit_admin_grant", "credit_admin_set_user"]


async def test_audit_log_filter_event_type(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    await store.admin_grant(
        user_id="u_a",
        team_id="team_a",
        amount=1000,
        by_user_id="admin",
    )
    await store.admin_set_user_credit(
        user_id="u_a",
        team_id="team_a",
        by_user_id="admin",
        tier="pro",
    )
    items, total = await store.credits_audit_log(
        "team_a",
        event_type="credit_admin_grant",
    )
    assert total == 1
    assert items[0]["event_type"] == "credit_admin_grant"


async def test_audit_log_excludes_non_credit_events(store: UserStore) -> None:
    """register_success 等非 credit_admin_* 事件不返."""
    await _seed_team(store)
    await store.log_audit(
        user_id="u_x",
        team_id="team_a",
        event_type="register_success",
    )
    _items, total = await store.credits_audit_log("team_a")
    assert total == 0


async def test_audit_log_team_isolation(store: UserStore) -> None:
    await _seed_team(store, "team_a")
    await _seed_team(store, "team_b")
    await _seed_user(store, user_id="u_a", team_id="team_a")
    await _seed_user(store, user_id="u_b", team_id="team_b")
    await store.admin_grant(
        user_id="u_a",
        team_id="team_a",
        amount=1000,
        by_user_id="admin",
    )
    await store.admin_grant(
        user_id="u_b",
        team_id="team_b",
        amount=2000,
        by_user_id="admin",
    )
    items_a, total_a = await store.credits_audit_log("team_a")
    items_b, total_b = await store.credits_audit_log("team_b")
    assert total_a == 1
    assert total_b == 1
    assert items_a[0]["payload"]["target_user_id"] == "u_a"
    assert items_b[0]["payload"]["target_user_id"] == "u_b"
