"""Stable-ID, caller-owned transaction semantics for credit deduction."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite
import anyio
import pytest
from bowhead_core.rate_card import ModelCreditRates, ModelTokenUsage, ModelUsageLine
from bowhead_core.user_store import (
    UserStore,
    _deduct_credit_with_stable_id_in_tx,
    _now,
)


@pytest.fixture
async def store(tmp_path: Path) -> UserStore:
    result = UserStore(str(tmp_path / "users.db"))
    await result.initialize()
    async with aiosqlite.connect(result.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id,name,domain,max_seats,default_user_tier,"
            "team_credit_balance,team_credit_quota,tier_credits_json,status,created_at) "
            "VALUES ('team_a','Test Team','test.com',10,'ultra',1000,1000,'{}','active',?)",
            (_now(),),
        )
        await db.execute(
            "INSERT INTO users(id,team_id,email,email_verified,password_hash,"
            "oauth_providers_json,tier,credit_balance,credit_quota,credit_period_start,"
            "locale,timezone,preferences_json,status,created_at) "
            "VALUES ('u_a','team_a','a@test.com',1,'','[]','ultra',1000,1000,?,"
            "'zh-CN','UTC','{}','active',?)",
            (_now(), _now()),
        )
        await db.commit()
    return result


async def _deduct(
    db: aiosqlite.Connection,
    *,
    ledger_id: str = "led_stable",
    user_id: str = "u_a",
    team_id: str = "team_a",
    cost_credits: int = 7,
    turn_id: str | None = "turn_a",
    workspace_id: str | None = "ws_a",
    tokens_input: int = 1200,
    tokens_output: int = 300,
    reason: str = "turn",
    rate_card_version: str = "v1",
) -> tuple[str, str]:
    return await _deduct_credit_with_stable_id_in_tx(
        db,
        ledger_id=ledger_id,
        user_id=user_id,
        team_id=team_id,
        cost_credits=cost_credits,
        turn_id=turn_id,
        workspace_id=workspace_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        reason=reason,
        rate_card_version=rate_card_version,
    )


async def _balances(store: UserStore) -> tuple[int, int, int]:
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute("SELECT credit_balance FROM users WHERE id='u_a'") as cur:
            user_balance = int((await cur.fetchone())[0])
        async with db.execute("SELECT team_credit_balance FROM teams WHERE id='team_a'") as cur:
            team_balance = int((await cur.fetchone())[0])
        async with db.execute("SELECT COUNT(*) FROM credit_ledger") as cur:
            ledger_count = int((await cur.fetchone())[0])
    return user_balance, team_balance, ledger_count


async def test_v2_deduction_keeps_model_and_rate_evidence(tmp_path: Path) -> None:
    from alembic import command
    from test_office_upload_confirmation_migration import _config

    path = tmp_path / "migrated.db"
    command.upgrade(_config(path, "core"), "head")
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO teams(id,name,created_at,team_credit_balance) VALUES('team_a','Test','2026-10-14',1000)")
        await db.execute("INSERT INTO users(id,team_id,email,created_at,credit_balance) VALUES('u_a','team_a','a@test.com','2026-10-14',1000)")
        await db.commit()
    lines = (
        ModelUsageLine("openai", "fast", ModelTokenUsage(200, 600, 100, ModelCreditRates(Decimal("2"), Decimal("0.25"), Decimal("4")))),
        ModelUsageLine("openai", "deep", ModelTokenUsage(100, 0, 100, ModelCreditRates(Decimal("1"), Decimal("1"), Decimal("2")))),
    )
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        result = await _deduct_credit_with_stable_id_in_tx(
            db,
            ledger_id="led_v2",
            user_id="u_a",
            team_id="team_a",
            cost_credits=2,
            turn_id="turn_v2",
            workspace_id="ws_a",
            tokens_input=900,
            tokens_output=200,
            reason="turn",
            rate_card_version="v2",
            model_usage=lines,
        )
        assert result == ("confirmed_applied", "")
        await db.commit()
    async with aiosqlite.connect(path) as db:
        assert await (
            await db.execute("SELECT model,tokens_input,tokens_cached,tokens_output,rate_cached FROM credit_model_usage ORDER BY line_no")
        ).fetchall() == [
            ("fast", 200, 600, 100, "0.25"),
            ("deep", 100, 0, 100, "1"),
        ]
        assert await (await db.execute("SELECT rate_card_version,delta FROM credit_ledger WHERE id='led_v2'")).fetchone() == ("v2", -2)


async def test_stable_credit_deduct_requires_caller_transaction(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        with pytest.raises(RuntimeError, match="active transaction"):
            await _deduct(db)
    assert await _balances(store) == (1000, 1000, 0)


async def test_stable_credit_deduct_exact_replay_does_not_deduct_twice(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await _deduct(db) == ("confirmed_applied", "")
        await db.commit()

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await _deduct(db) == ("confirmed_applied", "")
        await db.commit()

    assert await _balances(store) == (993, 993, 1)


async def test_stable_credit_deduct_serializes_two_wal_writers(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")

    start = anyio.Event()
    outcomes: list[tuple[str, str]] = []

    async def deduct_once() -> None:
        await start.wait()
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            outcomes.append(await _deduct(db))
            await db.commit()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(deduct_once)
        task_group.start_soon(deduct_once)
        start.set()

    assert outcomes == [("confirmed_applied", ""), ("confirmed_applied", "")]
    assert await _balances(store) == (993, 993, 1)


async def test_stable_credit_deduct_replay_ignores_later_balance_exhaustion(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await _deduct(db) == ("confirmed_applied", "")
        await db.commit()
        await db.execute("UPDATE users SET credit_balance=0 WHERE id='u_a'")
        await db.execute("UPDATE teams SET team_credit_balance=0 WHERE id='team_a'")
        await db.commit()

        await db.execute("BEGIN IMMEDIATE")
        assert await _deduct(db) == ("confirmed_applied", "")
        await db.commit()

    assert await _balances(store) == (0, 0, 1)


async def test_stable_credit_deduct_does_not_commit_callers_transaction(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await _deduct(db) == ("confirmed_applied", "")
        assert db.in_transaction
        await db.rollback()

    assert await _balances(store) == (1000, 1000, 0)


@pytest.mark.parametrize(
    "changed_fact",
    [
        {"user_id": "u_other"},
        {"team_id": "team_other"},
        {"workspace_id": None},
        {"turn_id": "turn_other"},
        {"cost_credits": 8},
        {"reason": "kb_matrix_run"},
        {"tokens_input": 1201},
        {"tokens_output": 301},
        {"rate_card_version": "future"},
    ],
)
async def test_stable_credit_deduct_rejects_identity_conflict_without_mutation(
    store: UserStore,
    changed_fact: dict[str, Any],
) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await _deduct(db) == ("confirmed_applied", "")
        await db.commit()

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="credit_ledger_identity_conflict"):
            await _deduct(db, **changed_fact)
        assert db.in_transaction
        await db.rollback()

    assert await _balances(store) == (993, 993, 1)


@pytest.mark.parametrize(
    ("balance_column", "expected"),
    [
        ("user", (-4, 993, 1)),
        ("team", (993, -4, 1)),
    ],
)
async def test_stable_credit_deduct_overdraws_insufficient_balance(
    store: UserStore,
    balance_column: str,
    expected: tuple[int, int, int],
) -> None:
    """余额 3、本笔 7 ⇒ 照实扣到 -4，而不是整笔不扣让余额卡在 3（下一轮 preflight 才拦得住）。"""
    async with aiosqlite.connect(store.db_path) as db:
        if balance_column == "user":
            await db.execute("UPDATE users SET credit_balance=3 WHERE id='u_a'")
        else:
            await db.execute("UPDATE teams SET team_credit_balance=3 WHERE id='team_a'")
        await db.commit()

        await db.execute("BEGIN IMMEDIATE")
        assert await _deduct(db) == ("confirmed_applied", "")
        await db.commit()

    assert await _balances(store) == expected


async def test_stable_credit_deduct_leaves_insert_failure_for_outer_rollback(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "CREATE TRIGGER reject_stable_ledger BEFORE INSERT ON credit_ledger "
            "WHEN NEW.id='led_stable' BEGIN SELECT RAISE(ABORT,'reject stable ledger'); END",
        )
        await db.commit()
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(aiosqlite.IntegrityError, match="reject stable ledger"):
            await _deduct(db)
        assert db.in_transaction
        await db.rollback()

    assert await _balances(store) == (1000, 1000, 0)


async def test_legacy_credit_deduct_keeps_public_contract_and_random_id(store: UserStore) -> None:
    for _ in range(2):
        assert await store.try_deduct_credit(
            user_id="u_a",
            team_id="team_a",
            cost_credits=7,
            turn_id="turn_a",
            workspace_id="ws_a",
            tokens_input=1200,
            tokens_output=300,
        ) == (True, "")
    assert await _balances(store) == (986, 986, 2)

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT id,reason,rate_card_version FROM credit_ledger ORDER BY id",
        ) as cur,
    ):
        rows = await cur.fetchall()
    assert len({row[0] for row in rows}) == 2
    assert all(str(row[0]).startswith("led_") for row in rows)
    assert all(row[1:] == ("turn", "v1") for row in rows)


@pytest.mark.parametrize(
    ("missing", "expected_reason"),
    [
        ({"user_id": "u_missing"}, "user_balance_zero"),
        ({"team_id": "team_missing"}, "team_pool_zero"),
    ],
)
async def test_legacy_credit_deduct_missing_row_is_not_applied(
    store: UserStore,
    missing: dict[str, str],
    expected_reason: str,
) -> None:
    kwargs = {"user_id": "u_a", "team_id": "team_a", **missing}
    assert await store.try_deduct_credit(cost_credits=7, **kwargs) == (False, expected_reason)
    assert await _balances(store) == (1000, 1000, 0)
