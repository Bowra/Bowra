from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import pytest
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_store import UserStore, _id, _now


@pytest.fixture
async def store(tmp_path: Any) -> UserStore:
    db = tmp_path / "credit_activation_foundation.db"
    s = UserStore(str(db))
    await s.initialize()
    return s


async def _seed_team(store: UserStore, *, balance: int = 100_000, quota: int = 100_000) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES ('team_a', 'Team A', 10, 'ultra', ?, ?, '{}', 'active', ?)",
            (balance, quota, _now()),
        )
        await db.commit()


async def _seed_user(
    store: UserStore,
    *,
    user_id: str = "u_a",
    balance: int = 1_000,
    quota: int = 30_000,
    credit_period_start: str = "2024-01-01T00:00:00+00:00",
    created_at: str | None = None,
) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "tier, credit_balance, credit_quota, credit_period_start, status, created_at) "
            "VALUES (?, 'team_a', ?, 1, '', 'ultra', ?, ?, ?, 'active', ?)",
            (
                user_id,
                f"{user_id}@test.com",
                balance,
                quota,
                credit_period_start,
                created_at or _now(),
            ),
        )
        await db.commit()


async def _seed_turn_spend(store: UserStore, *, user_id: str = "u_a", n: int = 3) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        for i in range(n):
            await db.execute(
                "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
                "delta, reason, tokens_input, tokens_output, created_at) "
                "VALUES (?, ?, 'team_a', 'ws_1', ?, -100, 'turn', 350, 150, ?)",
                (_id("led"), user_id, f"t_{i}", _now()),
            )
        await db.commit()


async def _seed_turn_spend_for_workspace(
    store: UserStore,
    *,
    user_id: str = "u_a",
    workspace_id: str,
    spend: int,
    turn_id: str,
) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
            "delta, reason, tokens_input, tokens_output, created_at) "
            "VALUES (?, ?, 'team_a', ?, ?, ?, 'turn', 350, 150, ?)",
            (_id("led"), user_id, workspace_id, turn_id, -abs(spend), _now()),
        )
        await db.commit()


async def test_initialize_migrates_legacy_team_pool_event_check(tmp_path: Any) -> None:
    db_path = tmp_path / "credit_legacy_check.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE credit_exhaustion_events (
              id TEXT PRIMARY KEY,
              team_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              workspace_id TEXT,
              code TEXT NOT NULL CHECK(code IN (
                'user_credit_exhausted',
                'org_pool_exhausted',
                'credit_deduct_failed'
              )),
              credit_balance INTEGER,
              credit_quota INTEGER,
              team_credit_balance INTEGER,
              team_credit_quota INTEGER,
              turn_id TEXT,
              created_at TEXT NOT NULL
            );
            CREATE INDEX idx_credit_exhaustion_org_user_at
              ON credit_exhaustion_events(team_id, user_id, created_at DESC);
        """)
        await db.execute(
            """INSERT INTO credit_exhaustion_events(
                id, team_id, user_id, workspace_id, code, turn_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "cex_legacy",
                "team_a",
                "u_a",
                "ws_1",
                "org_pool_exhausted",
                "t_legacy",
                "2026-07-20T00:00:00+00:00",
            ),
        )
        await db.commit()

    migrated = UserStore(db_path)
    await migrated.initialize()
    await migrated.initialize()

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='credit_exhaustion_events'",
        )
        sql = (await cur.fetchone())[0] or ""
        assert "team_pool_exhausted" in sql
        assert "org_pool_exhausted" not in sql

        cur = await db.execute(
            "SELECT code, team_id, user_id, workspace_id, turn_id FROM credit_exhaustion_events WHERE id = ?",
            ("cex_legacy",),
        )
        assert await cur.fetchone() == (
            "team_pool_exhausted",
            "team_a",
            "u_a",
            "ws_1",
            "t_legacy",
        )

        cur = await db.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' AND tbl_name='credit_exhaustion_events'",
        )
        index_names = {row[0] for row in await cur.fetchall()}
        assert "idx_credit_exhaustion_team_user_at" in index_names
        assert "idx_credit_exhaustion_team_code_at" in index_names
        assert "idx_credit_exhaustion_org_user_at" not in index_names

    event = await migrated.record_credit_exhaustion_event(
        team_id="team_a",
        user_id="u_a",
        workspace_id="ws_1",
        code="team_pool_exhausted",
    )
    assert event["code"] == "team_pool_exhausted"


async def _age_attention_acted_at(
    store: UserStore,
    attention_item_id: str,
    *,
    days: int = 8,
) -> None:
    acted_at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE credit_attention_state SET acted_at = ? WHERE attention_item_id = ?",
            (acted_at, attention_item_id),
        )
        await db.commit()


async def _metric_events(store: UserStore, name: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, user_id, attrs_json FROM metrics_event_log WHERE name = ? ORDER BY rowid",
            (name,),
        ) as cur:
            rows = await cur.fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "name": row["name"],
                "user_id": row["user_id"],
                "attrs": json.loads(row["attrs_json"] or "{}"),
            }
        )
    return out


async def test_credit_activation_tables_are_initialized(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        tables = {row[0] async for row in await db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] async for row in await db.execute("SELECT name FROM sqlite_master WHERE type='index'")}

    assert "credit_attention_state" in tables
    assert "credit_exhaustion_events" in tables
    assert "uq_credit_attention_active_dedupe" in indexes


async def test_active_dedupe_unique_index_blocks_duplicate_open_items(store: UserStore) -> None:
    now = _now()
    values = (
        _id("attn"),
        "team_a",
        "u_a",
        None,
        "low_balance_power_user",
        "team_a:u_a:_:low_balance_power_user",
        "open",
        "warning",
        2,
        now,
        now,
        now,
        now,
    )
    sql = (
        "INSERT INTO credit_attention_state("
        "attention_item_id, team_id, user_id, workspace_id, policy_key, dedupe_key, "
        "state, severity, severity_rank, opened_at, expires_at, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(sql, values)
        await db.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(sql, (_id("attn"), *values[1:]))


async def test_resolved_item_allows_new_open_with_same_dedupe(store: UserStore) -> None:
    now = _now()
    dedupe = "team_a:u_a:_:low_balance_power_user"
    sql = (
        "INSERT INTO credit_attention_state("
        "attention_item_id, team_id, user_id, policy_key, dedupe_key, state, "
        "severity, severity_rank, opened_at, expires_at, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            sql,
            (_id("attn"), "team_a", "u_a", "low_balance_power_user", dedupe, "resolved", "warning", 2, now, now, now, now),
        )
        await db.execute(
            sql,
            (_id("attn"), "team_a", "u_a", "low_balance_power_user", dedupe, "open", "warning", 2, now, now, now, now),
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM credit_attention_state WHERE dedupe_key = ?",
            (dedupe,),
        ) as cur:
            row = await cur.fetchone()

    assert row is not None
    assert row[0] == 2


async def test_record_credit_exhaustion_event_writes_normalized_event(store: UserStore) -> None:
    out = await store.record_credit_exhaustion_event(
        team_id="team_a",
        user_id="u_a",
        workspace_id="ws_a",
        code="user_credit_exhausted",
        credit_balance=0,
        credit_quota=1_000,
        team_credit_balance=10_000,
        team_credit_quota=100_000,
        turn_id="turn_a",
    )

    assert out["id"].startswith("cex_")
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM credit_exhaustion_events WHERE id = ?",
            (out["id"],),
        ) as cur:
            row = await cur.fetchone()

    assert row is not None
    assert row["code"] == "user_credit_exhausted"
    assert row["workspace_id"] == "ws_a"
    assert row["credit_balance"] == 0


async def test_record_credit_exhaustion_event_rejects_unknown_code(store: UserStore) -> None:
    with pytest.raises(ValueError):
        await store.record_credit_exhaustion_event(
            team_id="team_a",
            user_id="u_a",
            code="not_a_code",
        )


async def test_materialize_credit_attention_opens_and_resolves_items(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)

    items = await store.materialize_credit_attention("team_a")

    item = next(i for i in items if i["user_id"] == "u_a")
    assert item["policy_key"] == "low_balance_power_user"
    assert item["state"] == "open"
    assert item["recommended_action_key"] == "grant_credit"

    # A second materialization updates the same active dedupe instead of opening duplicates.
    await store.materialize_credit_attention("team_a")
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT COUNT(*) FROM credit_attention_state WHERE team_id = 'team_a' AND state = 'open' AND user_id = 'u_a'") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_balance = credit_quota WHERE id = 'u_a'")
        await db.commit()

    assert await store.materialize_credit_attention("team_a") == []
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT state, resolved_at FROM credit_attention_state WHERE user_id = 'u_a'") as cur:
            resolved = await cur.fetchone()
    assert resolved is not None
    assert resolved["state"] == "resolved"
    assert resolved["resolved_at"] is not None


async def test_materialize_credit_attention_ignores_tiny_turns_for_meaningful_count(
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    async with aiosqlite.connect(store.db_path) as db:
        for i in range(3):
            await db.execute(
                "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
                "delta, reason, tokens_input, tokens_output, created_at) "
                "VALUES (?, 'u_a', 'team_a', 'ws_1', ?, -100, 'turn', 100, 50, ?)",
                (_id("led"), f"t_tiny_{i}", _now()),
            )
        await db.commit()

    assert await store.materialize_credit_attention("team_a") == []


async def test_materialize_credit_attention_does_not_treat_period_reset_as_new_user(
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(
        store,
        balance=10_000,
        quota=30_000,
        credit_period_start=_now(),
        created_at="2024-01-01T00:00:00+00:00",
    )
    old_at = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
            "delta, reason, tokens_input, tokens_output, created_at) "
            "VALUES (?, 'u_a', 'team_a', 'ws_1', 't_old_first', -100, 'turn', 350, 150, ?)",
            (_id("led"), old_at),
        )
        await db.commit()
    await _seed_turn_spend(store)

    assert await store.materialize_credit_attention("team_a") == []


async def test_materialize_credit_attention_tracks_opened_and_resolved_metrics(
    store: UserStore,
) -> None:
    metrics_log = MetricsLog(store.db_path)
    await metrics_log.initialize()
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)

    await store.materialize_credit_attention("team_a", metrics_log=metrics_log)

    opened = await _metric_events(store, "credit.attention.item_opened")
    assert len(opened) == 1
    assert opened[0]["user_id"] == "u_a"
    assert opened[0]["attrs"]["policy_key"] == "low_balance_power_user"
    assert opened[0]["attrs"]["action_key"] == "grant_credit"

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_balance = credit_quota WHERE id = 'u_a'")
        await db.commit()

    assert await store.materialize_credit_attention("team_a", metrics_log=metrics_log) == []
    resolved = await _metric_events(store, "credit.attention.resolved")
    assert len(resolved) == 1
    assert resolved[0]["attrs"]["resolved_reason"] == "materializer_resolved"
    assert resolved[0]["attrs"]["previous_severity"] == "warning"


async def test_credit_attention_list_marks_items_shown(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)

    item = (await store.materialize_credit_attention("team_a"))[0]
    await store.list_credit_attention_items("team_a")

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT first_shown_at, last_shown_at, shown_count FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            row = await cur.fetchone()

    assert row is not None
    assert row["first_shown_at"] is not None
    assert row["last_shown_at"] is not None
    assert row["shown_count"] >= 1
    assert item["expires_at"] is not None


async def test_materialize_credit_attention_sets_top_workspace(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=1_000, quota=30_000)
    await _seed_turn_spend_for_workspace(
        store,
        workspace_id="ws_small",
        spend=100,
        turn_id="t_ws_small",
    )
    await _seed_turn_spend_for_workspace(
        store,
        workspace_id="ws_big",
        spend=900,
        turn_id="t_ws_big",
    )
    await _seed_turn_spend_for_workspace(
        store,
        workspace_id="ws_big",
        spend=800,
        turn_id="t_ws_big_2",
    )

    item = next(i for i in await store.materialize_credit_attention("team_a") if i["user_id"] == "u_a")

    assert item["workspace_id"] == "ws_big"
    assert item["evidence"]["top_workspace_id"] == "ws_big"


async def test_credits_summary_reads_materialized_attention_only(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)

    before = await store.credits_summary("team_a")
    assert before["activation_attention"] == []

    await store.materialize_credit_attention("team_a")
    after = await store.credits_summary("team_a")
    assert after["activation_attention"][0]["user_id"] == "u_a"


async def test_credits_summary_does_not_mark_attention_shown(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)

    item = (await store.materialize_credit_attention("team_a"))[0]
    await store.credits_summary("team_a")

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT shown_count FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur,
    ):
        row = await cur.fetchone()

    assert row is not None
    assert row[0] == 0


async def test_acted_attention_reopens_when_risk_persists(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    acted = await store.mark_credit_attention_acted(
        team_id="team_a",
        actor_user_id="u_admin",
        action_type="grant_credit",
        user_id="u_a",
    )
    assert acted["acted_count"] == 1

    reopened = await store.materialize_credit_attention("team_a")
    assert reopened[0]["attention_item_id"] == item["attention_item_id"]
    assert reopened[0]["state"] == "open"
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT state_reason FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["state_reason"] == "protection_failed"


async def test_acted_attention_becomes_protected_when_risk_clears(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    await store.mark_credit_attention_acted(
        team_id="team_a",
        actor_user_id="u_admin",
        action_type="grant_credit",
        user_id="u_a",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_balance = credit_quota WHERE id = 'u_a'")
        await db.commit()
    await _age_attention_acted_at(store, item["attention_item_id"])

    assert await store.materialize_credit_attention("team_a") == []
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT state, state_reason, protected_at FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            protected = await cur.fetchone()

    assert protected is not None
    assert protected["state"] == "protected"
    assert protected["state_reason"] == "materializer_protected"
    assert protected["protected_at"] is not None

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_balance = 3000 WHERE id = 'u_a'")
        await db.commit()

    reopened = await store.materialize_credit_attention("team_a")
    assert reopened[0]["attention_item_id"] == item["attention_item_id"]
    assert reopened[0]["state"] == "open"
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT state, state_reason, protected_at, protection_reason, "
            "protection_snapshot_json FROM credit_attention_state "
            "WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            failed = await cur.fetchone()
    assert failed is not None
    assert failed["state"] == "open"
    assert failed["state_reason"] == "protection_failed"
    assert failed["protected_at"] is None
    assert failed["protection_reason"] is None
    assert failed["protection_snapshot_json"] == "{}"


async def test_acted_attention_waits_for_verification_window_before_protection(
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    await store.mark_credit_attention_acted(
        team_id="team_a",
        actor_user_id="u_admin",
        action_type="grant_credit",
        user_id="u_a",
    )
    old_at = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_balance = credit_quota WHERE id = 'u_a'")
        await db.execute(
            "UPDATE credit_ledger SET created_at = ? WHERE user_id = 'u_a'",
            (old_at,),
        )
        await db.commit()

    assert await store.materialize_credit_attention("team_a") == []
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT state, protected_at FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            pending = await cur.fetchone()
    assert pending is not None
    assert pending["state"] == "acted"
    assert pending["protected_at"] is None

    await _age_attention_acted_at(store, item["attention_item_id"])
    assert await store.materialize_credit_attention("team_a") == []
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT state, state_reason, protected_at FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            protected = await cur.fetchone()

    assert protected is not None
    assert protected["state"] == "protected"
    assert protected["state_reason"] == "materializer_protected"
    assert protected["protected_at"] is not None


async def test_acted_attention_resolves_unprotected_when_user_churns_after_window(
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    await store.mark_credit_attention_acted(
        team_id="team_a",
        actor_user_id="u_admin",
        action_type="grant_credit",
        user_id="u_a",
    )
    old_at = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE credit_ledger SET created_at = ? WHERE user_id = 'u_a'",
            (old_at,),
        )
        await db.commit()
    await _age_attention_acted_at(store, item["attention_item_id"])

    assert await store.materialize_credit_attention("team_a") == []
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT state, state_reason, protected_at FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            resolved = await cur.fetchone()

    assert resolved is not None
    assert resolved["state"] == "resolved"
    assert resolved["state_reason"] == "materializer_resolved_unprotected"
    assert resolved["protected_at"] is None


async def test_acted_attention_resolves_without_protection_when_all_guards_fail(
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    await store.mark_credit_attention_acted(
        team_id="team_a",
        actor_user_id="u_admin",
        action_type="grant_credit",
        user_id="u_a",
    )
    old_at = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE users SET credit_balance = 100 WHERE id = 'u_a'",
        )
        await db.execute(
            "UPDATE credit_ledger SET created_at = ? WHERE user_id = 'u_a'",
            (old_at,),
        )
        for i in range(2):
            await db.execute(
                "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
                "delta, reason, tokens_input, tokens_output, created_at) "
                "VALUES (?, 'u_a', 'team_a', 'ws_1', ?, -100, 'turn', 100, 50, ?)",
                (_id("led"), f"t_tiny_after_action_{i}", _now()),
            )
        await db.commit()
    await _age_attention_acted_at(store, item["attention_item_id"])
    await store.record_credit_exhaustion_event(
        team_id="team_a",
        user_id="u_a",
        workspace_id="ws_1",
        code="user_credit_exhausted",
        credit_balance=0,
        credit_quota=30_000,
    )

    assert await store.materialize_credit_attention("team_a") == []
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT state, state_reason, protected_at FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            resolved = await cur.fetchone()

    assert resolved is not None
    assert resolved["state"] == "resolved"
    assert resolved["state_reason"] == "materializer_resolved_unprotected"
    assert resolved["protected_at"] is None


async def test_user_protection_ignores_team_pool_exhaustion_events(
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    await store.mark_credit_attention_acted(
        team_id="team_a",
        actor_user_id="u_admin",
        action_type="grant_credit",
        user_id="u_a",
    )
    old_at = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_balance = credit_quota WHERE id = 'u_a'")
        await db.execute(
            "UPDATE credit_ledger SET created_at = ? WHERE user_id = 'u_a'",
            (old_at,),
        )
        await db.commit()
    await _age_attention_acted_at(store, item["attention_item_id"])
    await store.record_credit_exhaustion_event(
        team_id="team_a",
        user_id="u_a",
        workspace_id="ws_1",
        code="team_pool_exhausted",
        credit_balance=30_000,
        credit_quota=30_000,
    )

    assert await store.materialize_credit_attention("team_a") == []
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT state, protection_snapshot_json FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            protected = await cur.fetchone()

    assert protected is not None
    assert protected["state"] == "protected"
    snapshot = json.loads(protected["protection_snapshot_json"])
    assert "user_balance" in snapshot["guard_reasons"]
    assert "user_no_hard_block" in snapshot["guard_reasons"]


async def test_materialize_credit_attention_tracks_protected_and_reopened_metrics(
    store: UserStore,
) -> None:
    metrics_log = MetricsLog(store.db_path)
    await metrics_log.initialize()
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    await store.mark_credit_attention_acted(
        team_id="team_a",
        actor_user_id="u_admin",
        action_type="grant_credit",
        user_id="u_a",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_balance = credit_quota WHERE id = 'u_a'")
        await db.commit()
    await _age_attention_acted_at(store, item["attention_item_id"])

    assert await store.materialize_credit_attention("team_a", metrics_log=metrics_log) == []
    protected = await _metric_events(store, "credit.attention.protected")
    assert len(protected) == 1
    assert protected[0]["attrs"]["attention_item_id"] == item["attention_item_id"]
    assert protected[0]["attrs"]["protection_reason"] == "materializer_protected"

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_balance = 3000 WHERE id = 'u_a'")
        await db.commit()

    reopened = await store.materialize_credit_attention("team_a", metrics_log=metrics_log)
    assert reopened[0]["attention_item_id"] == item["attention_item_id"]
    opened = await _metric_events(store, "credit.attention.item_opened")
    assert opened[-1]["attrs"]["state_reason"] == "protection_failed"


async def test_mark_credit_attention_acted_does_not_close_unrelated_policy(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=1_000, quota=30_000)
    now = _now()
    async with aiosqlite.connect(store.db_path) as db:
        for policy in ("low_balance_power_user", "growth_spike"):
            await db.execute(
                "INSERT INTO credit_attention_state("
                "attention_item_id, team_id, user_id, policy_key, dedupe_key, state, "
                "severity, severity_rank, opened_at, expires_at, payload_json, created_at, updated_at"
                ") VALUES (?, 'team_a', 'u_a', ?, ?, 'open', 'warning', 2, ?, ?, ?, ?, ?)",
                (
                    _id("cai"),
                    policy,
                    f"team_a:u_a:_:{policy}",
                    now,
                    now,
                    '{"recommended_action_key":"edit_user_quota"}' if policy == "growth_spike" else '{"recommended_action_key":"grant_credit"}',
                    now,
                    now,
                ),
            )
        await db.commit()

    out = await store.mark_credit_attention_acted(
        team_id="team_a",
        actor_user_id="u_admin",
        action_type="grant_credit",
        user_id="u_a",
    )

    assert out["acted_count"] == 1
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT policy_key, state FROM credit_attention_state ORDER BY policy_key") as cur:
            rows = await cur.fetchall()
    states = {row["policy_key"]: row["state"] for row in rows}
    assert states == {
        "growth_spike": "open",
        "low_balance_power_user": "acted",
    }


async def test_dismissed_attention_suppresses_for_7_days(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    out = await store.update_credit_attention_state(
        team_id="team_a",
        attention_item_id=item["attention_item_id"],
        state="dismissed",
        actor_user_id="u_admin",
        reason="handled_offline",
    )
    assert out["state"] == "dismissed"
    assert await store.materialize_credit_attention("team_a") == []

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT expires_at FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            dismissed = await cur.fetchone()
    assert dismissed is not None
    expires_at = datetime.fromisoformat(dismissed["expires_at"])
    remaining = expires_at - datetime.now(UTC)
    assert remaining <= timedelta(days=8)


async def test_materialize_credit_attention_tracks_suppressed_metric(
    store: UserStore,
) -> None:
    metrics_log = MetricsLog(store.db_path)
    await metrics_log.initialize()
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    await store.update_credit_attention_state(
        team_id="team_a",
        attention_item_id=item["attention_item_id"],
        state="dismissed",
        actor_user_id="u_admin",
        reason="handled_offline",
    )

    assert await store.materialize_credit_attention("team_a", metrics_log=metrics_log) == []
    suppressed = await _metric_events(store, "credit.attention.suppressed")
    assert len(suppressed) == 1
    assert suppressed[0]["attrs"]["attention_item_id"] == item["attention_item_id"]
    assert suppressed[0]["attrs"]["suppressed_reason"] == "dismissed"


async def test_dismissed_attention_reopens_after_suppression_expires(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]
    await store.update_credit_attention_state(
        team_id="team_a",
        attention_item_id=item["attention_item_id"],
        state="dismissed",
        actor_user_id="u_admin",
        reason="watch_later",
    )
    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE credit_attention_state SET expires_at = ? WHERE attention_item_id = ?",
            (expired_at, item["attention_item_id"]),
        )
        await db.commit()

    reopened = await store.materialize_credit_attention("team_a")
    assert reopened[0]["state"] == "open"
    assert reopened[0]["user_id"] == "u_a"


async def test_repeated_dismiss_without_reason_returns_existing_state(store: UserStore) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]
    await store.update_credit_attention_state(
        team_id="team_a",
        attention_item_id=item["attention_item_id"],
        state="dismissed",
        actor_user_id="u_admin",
        reason="handled_offline",
    )

    repeated = await store.update_credit_attention_state(
        team_id="team_a",
        attention_item_id=item["attention_item_id"],
        state="dismissed",
        actor_user_id="u_admin",
    )

    assert repeated["state"] == "dismissed"


async def test_update_credit_attention_state_rejects_manual_outcome_states(
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(store, balance=3_000, quota=30_000)
    await _seed_turn_spend(store)
    item = (await store.materialize_credit_attention("team_a"))[0]

    for state in ("acted", "protected"):
        with pytest.raises(ValueError, match=f"invalid_attention_state:{state}"):
            await store.update_credit_attention_state(
                team_id="team_a",
                attention_item_id=item["attention_item_id"],
                state=state,
                actor_user_id="u_admin",
                reason="manual outcome should fail",
            )

    current = await store.get_credit_attention_item("team_a", item["attention_item_id"])
    assert current is not None
    assert current["state"] == "open"
