from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.subagent_draft_accounting import (
    SubagentDraftCreditAccountant,
)


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def _db(tmp_path) -> str:
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema("core"))
        now = "2026-07-31T00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO teams(
              id,name,status,created_at,team_credit_balance,team_credit_quota
            ) VALUES (?,?,?,?,?,?)
            """,
            ("team_a", "A", "active", now, 100, 100),
        )
        connection.execute(
            """
            INSERT INTO users(
              id,team_id,email,status,created_at,credit_balance,credit_quota
            ) VALUES (?,?,?,?,?,?,?)
            """,
            ("user_a", "team_a", "a@example.com", "active", now, 100, 100),
        )
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    return str(path)


@pytest.mark.asyncio
async def test_credit_reservation_and_settlement_are_idempotent(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 31, tzinfo=UTC))
    accountant = SubagentDraftCreditAccountant(
        _db(tmp_path),
        clock=clock,
        reserved_credits=10,
    )
    await accountant.initialize()
    kwargs = {
        "team_id": "team_a",
        "owner_user_id": "user_a",
        "request_key_hash": "a" * 64,
    }
    await accountant.preflight(**kwargs)
    await accountant.preflight(**kwargs)
    with sqlite3.connect(accountant.db_path) as connection:
        assert connection.execute(
            "SELECT credit_balance FROM users WHERE id='user_a'",
        ).fetchone() == (90,)
        assert connection.execute(
            "SELECT team_credit_balance FROM teams WHERE id='team_a'",
        ).fetchone() == (90,)

    await accountant.settle(
        **kwargs,
        draft_id="sad_1",
        input_tokens=100,
        output_tokens=200,
        attempts=1,
    )
    await accountant.settle(
        **kwargs,
        draft_id="sad_1",
        input_tokens=100,
        output_tokens=200,
        attempts=1,
    )
    with sqlite3.connect(accountant.db_path) as connection:
        assert connection.execute(
            "SELECT credit_balance FROM users WHERE id='user_a'",
        ).fetchone() == (99,)
        assert connection.execute(
            """
            SELECT state,reserved_credits,actual_credits
            FROM team_subagent_draft_usage
            """,
        ).fetchone() == ("settled", 10, 1)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM credit_ledger
            WHERE reason LIKE 'subagent_draft_%'
            """,
        ).fetchone() == (2,)


@pytest.mark.asyncio
async def test_stale_reservation_is_released_once(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 31, tzinfo=UTC))
    accountant = SubagentDraftCreditAccountant(
        _db(tmp_path),
        clock=clock,
        reserved_credits=10,
    )
    await accountant.initialize()
    await accountant.preflight(
        team_id="team_a",
        owner_user_id="user_a",
        request_key_hash="b" * 64,
    )
    clock.value += timedelta(minutes=11)
    assert await accountant.release_stale(limit=10) == 1
    assert await accountant.release_stale(limit=10) == 0
    with sqlite3.connect(accountant.db_path) as connection:
        assert connection.execute(
            "SELECT credit_balance FROM users WHERE id='user_a'",
        ).fetchone() == (100,)


@pytest.mark.asyncio
async def test_prepared_settlement_survives_until_maintenance_retry(
    tmp_path,
) -> None:
    accountant = SubagentDraftCreditAccountant(
        _db(tmp_path),
        clock=FakeClock(datetime(2026, 7, 31, tzinfo=UTC)),
        reserved_credits=10,
    )
    await accountant.initialize()
    kwargs = {
        "team_id": "team_a",
        "owner_user_id": "user_a",
        "request_key_hash": "c" * 64,
    }
    await accountant.preflight(**kwargs)
    await accountant.prepare_settlement(
        **kwargs,
        draft_id="sad_pending",
        input_tokens=100,
        output_tokens=200,
        attempts=1,
    )
    with sqlite3.connect(accountant.db_path) as connection:
        assert connection.execute(
            """
            SELECT state,input_tokens,output_tokens,draft_id
            FROM team_subagent_draft_usage
            """,
        ).fetchone() == (
            "settlement_pending",
            100,
            200,
            "sad_pending",
        )

    assert await accountant.settle_pending(limit=10) == 1
    assert await accountant.settle_pending(limit=10) == 0
    with sqlite3.connect(accountant.db_path) as connection:
        assert connection.execute(
            """
            SELECT state,actual_credits
            FROM team_subagent_draft_usage
            """,
        ).fetchone() == ("settled", 1)
        assert connection.execute(
            "SELECT credit_balance FROM users WHERE id='user_a'",
        ).fetchone() == (99,)
