from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.subagent_authoring_context_store import (
    SubagentAuthoringContextStore,
)


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def _store(tmp_path, clock: FakeClock) -> SubagentAuthoringContextStore:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    return SubagentAuthoringContextStore(str(db_path), clock=clock)


@pytest.mark.asyncio
async def test_context_is_session_owner_and_team_bound(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 31, tzinfo=UTC))
    store = _store(tmp_path, clock)
    await store.initialize()

    context = await store.record_purpose_prompt(
        team_id="team_a",
        owner_user_id="user_a",
        session_id="session_a",
    )
    assert context.purpose_prompt_count == 1
    assert (
        await store.get(
            team_id="team_a",
            owner_user_id="user_b",
            session_id="session_a",
        )
        is None
    )
    assert (
        await store.get(
            team_id="team_b",
            owner_user_id="user_a",
            session_id="session_a",
        )
        is None
    )

    context = await store.record_purpose_prompt(
        team_id="team_a",
        owner_user_id="user_a",
        session_id="session_a",
    )
    assert context.purpose_prompt_count == 2
    assert (
        await store.record_purpose_prompt(
            team_id="team_a",
            owner_user_id="user_a",
            session_id="session_a",
        )
        is None
    )


@pytest.mark.asyncio
async def test_insufficient_purpose_has_an_independent_single_reprompt(
    tmp_path,
) -> None:
    clock = FakeClock(datetime(2026, 7, 31, tzinfo=UTC))
    store = _store(tmp_path, clock)
    await store.initialize()

    await store.record_purpose_prompt(
        team_id="team_a",
        owner_user_id="user_a",
        session_id="session_a",
    )
    context = await store.record_insufficient_prompt(
        team_id="team_a",
        owner_user_id="user_a",
        session_id="session_a",
        purpose_text="做分析",
    )
    assert context is not None
    assert context.purpose_prompt_count == 1
    assert context.insufficient_reprompt_count == 1
    assert context.purpose_text == "做分析"

    assert (
        await store.record_insufficient_prompt(
            team_id="team_a",
            owner_user_id="user_a",
            session_id="session_a",
            purpose_text="做个厉害的助手",
        )
        is None
    )


@pytest.mark.asyncio
async def test_context_expires_and_is_deleted_in_bounded_batch(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 31, tzinfo=UTC))
    store = _store(tmp_path, clock)
    await store.initialize()
    await store.record_purpose_prompt(
        team_id="team_a",
        owner_user_id="user_a",
        session_id="session_a",
    )

    clock.value += timedelta(minutes=31)
    assert (
        await store.get(
            team_id="team_a",
            owner_user_id="user_a",
            session_id="session_a",
        )
        is None
    )
    assert await store.purge_expired(limit=10) == 1
