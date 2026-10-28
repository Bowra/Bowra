"""Tests for UserStore credits_summary + credits_users_list (V3 admin credits).

聚合查询正确性 + 边界 (空 team / 无 ledger) + 排序 + 筛选 + 分页.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore, _id, _now


async def _seed_team(store: UserStore, team_id: str = "team_a", quota: int = 100_000) -> None:
    """直插一个 team (绕过 create_team 复杂逻辑)."""
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, "Test Team", "test.com", 10, "ultra", quota, quota, '{"pro":5000,"ultra":30000}', "active", _now()),
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
    period_start: str | None = None,
    status: str = "active",
    is_creator: bool = False,
    created_at: str | None = None,
) -> None:
    period_start = period_start or _now()
    created_at = created_at or _now()
    email = email or f"{user_id}@test.com"
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES (?, ?, ?, 1, '', '[]', ?, ?, ?, ?, 'zh-CN', 'UTC', '{}', ?, ?)",
            (user_id, team_id, email, tier, balance, quota, period_start, status, created_at),
        )
        # After multi-team migration, active/seat counts use active team_members (matching production after backfill).
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, ?, ?, 'member', ?, ?)",
            (f"om_{user_id}", user_id, team_id, status, created_at),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


async def _seed_ledger(
    store: UserStore,
    *,
    user_id: str,
    team_id: str = "team_a",
    workspace_id: str | None = None,
    turn_id: str | None = None,
    delta: int = -100,
    reason: str = "turn",
    tokens_in: int = 800,
    tokens_out: int = 200,
    days_ago: int = 0,
) -> None:
    """插一行 ledger. days_ago 控制 created_at 偏移 (sqlite datetime arithmetic)."""
    turn_id = turn_id or _id("t")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
            "delta, reason, tokens_input, tokens_output, created_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '-{int(days_ago)} days'))",
            (_id("led"), user_id, team_id, workspace_id, turn_id, delta, reason, tokens_in, tokens_out),
        )
        await db.commit()


@pytest.fixture
async def store(tmp_path: Any) -> UserStore:
    db = tmp_path / "credits_test.db"
    s = UserStore(str(db))
    await s.initialize()
    return s


# ─── credits_summary tests ────────────────────────


async def test_summary_empty_team_returns_zero(store: UserStore) -> None:
    """新 team 无 ledger → 全 0, 不抛."""
    await _seed_team(store)
    out = await store.credits_summary("team_a")
    assert out["team"]["balance"] == 100_000
    assert out["last_30d"]["spend"] == 0
    assert out["last_30d"]["turns"] == 0
    assert out["active_users_7d"] == 0
    assert out["total_users_active"] == 0
    assert out["view_attention"] == []
    assert out["view_top_users"] == []
    assert out["view_top_workspaces"] == []


async def test_summary_aggregates_30d_spend(store: UserStore) -> None:
    """3 个 turn 在 30d 内, spend 求和正确."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_alice", balance=29_500)
    await _seed_ledger(store, user_id="u_alice", delta=-100, days_ago=1, turn_id="t1")
    await _seed_ledger(store, user_id="u_alice", delta=-200, days_ago=2, turn_id="t2")
    await _seed_ledger(store, user_id="u_alice", delta=-50, days_ago=3, turn_id="t3")
    out = await store.credits_summary("team_a")
    assert out["last_30d"]["spend"] == 350
    assert out["last_30d"]["turns"] == 3


async def test_summary_excludes_old_ledger(store: UserStore) -> None:
    """40 天前的 ledger 不算 30d 内."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_alice")
    await _seed_ledger(store, user_id="u_alice", delta=-9999, days_ago=40, turn_id="ancient")
    await _seed_ledger(store, user_id="u_alice", delta=-100, days_ago=1, turn_id="recent")
    out = await store.credits_summary("team_a")
    assert out["last_30d"]["spend"] == 100  # Count recent only


async def test_summary_excludes_non_turn_reasons(store: UserStore) -> None:
    """admin_grant 不计入 spend."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_alice")
    await _seed_ledger(store, user_id="u_alice", delta=-100, reason="turn", days_ago=1)
    await _seed_ledger(store, user_id="u_alice", delta=500, reason="admin_grant:admin1", days_ago=1)
    out = await store.credits_summary("team_a")
    assert out["last_30d"]["spend"] == 100  # Turns only


async def test_summary_active_users_7d(store: UserStore) -> None:
    """3 个用户 1 个 7d 内 turn."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    await _seed_user(store, user_id="u_b")
    await _seed_user(store, user_id="u_c")
    await _seed_ledger(store, user_id="u_a", days_ago=1)
    await _seed_ledger(store, user_id="u_b", days_ago=10)  # Outside 7d
    out = await store.credits_summary("team_a")
    assert out["active_users_7d"] == 1
    assert out["total_users_active"] == 3


async def test_summary_top_users_by_spend(store: UserStore) -> None:
    """top 5 按消耗 desc."""
    await _seed_team(store)
    for uid, sp in [("u_a", 5000), ("u_b", 3000), ("u_c", 1000)]:
        await _seed_user(store, user_id=uid)
        await _seed_ledger(store, user_id=uid, delta=-sp, days_ago=1)
    out = await store.credits_summary("team_a")
    assert [u["user_id"] for u in out["view_top_users"]] == ["u_a", "u_b", "u_c"]
    assert out["view_top_users"][0]["spend"] == 5000


async def test_summary_top_workspaces(store: UserStore) -> None:
    """workspace 拆分."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    await _seed_ledger(store, user_id="u_a", workspace_id="ws_1", delta=-300, days_ago=1)
    await _seed_ledger(store, user_id="u_a", workspace_id="ws_2", delta=-500, days_ago=1)
    await _seed_ledger(store, user_id="u_a", workspace_id="ws_1", delta=-100, days_ago=2)
    out = await store.credits_summary("team_a")
    assert out["view_top_workspaces"][0] == {"workspace_id": "ws_2", "spend": 500}
    assert out["view_top_workspaces"][1] == {"workspace_id": "ws_1", "spend": 400}


async def test_summary_attention_low_balance(store: UserStore) -> None:
    """balance% < 20 标 low_balance."""
    await _seed_team(store)
    # created_at (not credit_period_start) drives is_new → seed an old created_at so
    # these users are not flagged new_user; otherwise the healthy control (u_ok) would
    # land in view_attention purely as a new_seat.
    await _seed_user(store, user_id="u_low", balance=1_000, quota=30_000, created_at="2024-01-01T00:00:00+00:00")  # not new
    await _seed_user(store, user_id="u_ok", balance=20_000, quota=30_000, created_at="2024-01-01T00:00:00+00:00")
    out = await store.credits_summary("team_a")
    flagged = {u["user_id"]: u["flags"] for u in out["view_attention"]}
    assert "u_low" in flagged
    assert "low_balance" in flagged["u_low"]
    assert "u_ok" not in flagged


async def test_summary_attention_new_user(store: UserStore) -> None:
    """created_at 在 30d 内 → 标 new_user (credit_period_start reset 不再误触发)."""
    await _seed_team(store)
    # old credit_period_start but recent created_at → new_user is driven by created_at.
    await _seed_user(store, user_id="u_new", balance=30_000, quota=30_000, period_start="2024-01-01T00:00:00+00:00")
    out = await store.credits_summary("team_a")
    flags_by_id = {u["user_id"]: u["flags"] for u in out["view_attention"]}
    assert "new_user" in flags_by_id["u_new"]


# ─── credits_users_list tests ─────────────────────


async def test_users_list_empty_returns_zero(store: UserStore) -> None:
    await _seed_team(store)
    items, total = await store.credits_users_list("team_a")
    assert items == []
    assert total == 0


async def test_users_list_pagination(store: UserStore) -> None:
    """25 个 user, page=2 size=10 → 10 items, total=25."""
    await _seed_team(store)
    for i in range(25):
        await _seed_user(store, user_id=f"u_{i:03d}")
    items, total = await store.credits_users_list(
        "team_a",
        page=2,
        size=10,
        sort="email",
    )
    assert len(items) == 10
    assert total == 25


async def test_users_list_search_q(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_alice", email="alice@test.com")
    await _seed_user(store, user_id="u_bob", email="bob@test.com")
    items, total = await store.credits_users_list("team_a", q="alice")
    assert total == 1
    assert items[0]["email"] == "alice@test.com"


async def test_users_list_tier_filter(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_p", tier="pro")
    await _seed_user(store, user_id="u_u", tier="ultra")
    items, total = await store.credits_users_list("team_a", tier="pro")
    assert total == 1
    assert items[0]["tier"] == "pro"


async def test_users_list_excludes_removed_by_default(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_active")
    await _seed_user(store, user_id="u_gone", status="removed")
    items, total = await store.credits_users_list("team_a")
    assert total == 1
    assert items[0]["user_id"] == "u_active"


async def test_users_list_include_removed(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_active")
    await _seed_user(store, user_id="u_gone", status="removed")
    items, total = await store.credits_users_list(
        "team_a",
        include_removed=True,
        sort="email",
    )
    assert total == 2
    statuses = sorted([i["status"] for i in items])
    assert statuses == ["active", "removed"]


async def test_users_list_workspace_filter(store: UserStore) -> None:
    """workspace_id filter: 仅返 30d 内在该 ws 有 ledger 的用户."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_ws1")
    await _seed_user(store, user_id="u_ws2")
    await _seed_user(store, user_id="u_none")
    await _seed_ledger(store, user_id="u_ws1", workspace_id="ws_1", days_ago=1)
    await _seed_ledger(store, user_id="u_ws2", workspace_id="ws_2", days_ago=1)
    # u_none has no ledger
    items, total = await store.credits_users_list("team_a", workspace_id="ws_1")
    assert total == 1
    assert items[0]["user_id"] == "u_ws1"


async def test_users_list_includes_spend_30d(store: UserStore) -> None:
    """spend_30d join 正确."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    await _seed_ledger(store, user_id="u_a", delta=-300, days_ago=1)
    await _seed_ledger(store, user_id="u_a", delta=-200, days_ago=2)
    items, _total = await store.credits_users_list("team_a")
    assert items[0]["spend_30d"] == 500


async def test_users_list_attention_score(store: UserStore) -> None:
    """3 维度命中 → score=1.0."""
    await _seed_team(store)
    # Low balance + new_user (2 dimensions), no growth (no 7d data)
    await _seed_user(store, user_id="u_2", balance=1_000, quota=30_000)
    items, _ = await store.credits_users_list("team_a")
    assert items[0]["attention_score"] >= 0.5  # At least 2/3


async def test_users_list_attention_sort_prioritizes_materialized_severity(
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_legacy", balance=1_000, quota=30_000)
    await _seed_user(
        store,
        user_id="u_critical",
        balance=30_000,
        quota=30_000,
        period_start="2024-01-01T00:00:00+00:00",
    )
    now = _now()
    payload = {
        "primary_segment": "blocked_valuable_user",
        "segment_flags": ["blocked_valuable_user"],
        "recommended_action_key": "grant_credit",
        "action_intent": {"key": "grant_credit", "label": "Grant credits"},
        "evidence": {},
    }
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO credit_attention_state("
            "attention_item_id, team_id, user_id, workspace_id, policy_key, dedupe_key, "
            "state, severity, severity_rank, opened_at, expires_at, payload_json, "
            "created_at, updated_at"
            ") VALUES (?, 'team_a', 'u_critical', NULL, ?, ?, 'open', 'critical', "
            "3, ?, ?, ?, ?, ?)",
            (
                _id("cai"),
                "blocked_valuable_user",
                "team_a:u_critical:_:blocked_valuable_user",
                now,
                now,
                json.dumps(payload),
                now,
                now,
            ),
        )
        await db.commit()

    items, _ = await store.credits_users_list("team_a")

    assert items[0]["user_id"] == "u_critical"
    assert items[0]["activation_attention"]["severity"] == "critical"


async def test_users_list_is_team_admin_flag(store: UserStore) -> None:
    """creator user is_team_admin=true."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_creator", is_creator=True)
    await _seed_user(store, user_id="u_member")
    items, _ = await store.credits_users_list("team_a", sort="email")
    by_id = {i["user_id"]: i["is_team_admin"] for i in items}
    assert by_id["u_creator"] is True
    assert by_id["u_member"] is False


async def test_users_list_invalid_sort_falls_back_to_default(store: UserStore) -> None:
    """SQL injection 防御: 非白名单 sort 走 default."""
    await _seed_team(store)
    await _seed_user(store, user_id="u_a")
    items, _ = await store.credits_users_list(
        "team_a",
        sort="email; DROP TABLE users;--",
    )
    assert len(items) == 1


async def test_users_list_team_isolation(store: UserStore) -> None:
    """team_a query 不含 team_b user."""
    await _seed_team(store, team_id="team_a")
    await _seed_team(store, team_id="team_b")
    await _seed_user(store, user_id="u_a", team_id="team_a")
    await _seed_user(store, user_id="u_b", team_id="team_b")
    items, total = await store.credits_users_list("team_a")
    assert total == 1
    assert items[0]["user_id"] == "u_a"
