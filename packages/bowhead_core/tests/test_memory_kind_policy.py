from __future__ import annotations

import pytest
from bowhead_core.user_memory import UserMemoryStore


@pytest.fixture
async def store(tmp_path):
    memory = UserMemoryStore(tmp_path / "memory.db")
    await memory.initialize()
    return memory


@pytest.mark.asyncio
async def test_fresh_store_is_ready_for_current_policy(store) -> None:
    assert await store.v2_readiness() == (True, None)


@pytest.mark.asyncio
async def test_product_reader_excludes_operational_rows(store) -> None:
    visible = await store.upsert_product_memory(
        user_id="u1",
        kind="constraint",
        key="report_order",
        value="先结论后依据",
    )
    await store.upsert_operational_memory(
        user_id="u1",
        kind="satisfaction",
        key="t1",
        value={"score": 4},
        source="turn_feedback",
    )

    rows = await store.list_product_for_user("u1")
    assert [row.id for row in rows] == [visible.id]
    assert await store.v2_readiness() == (True, None)


@pytest.mark.asyncio
async def test_unclassified_row_fails_readiness_and_stays_hidden(store) -> None:
    await store.upsert(
        user_id="u1",
        kind="preference",
        key="legacy",
        value="legacy value",
    )

    assert await store.list_product_for_user("u1") == []
    assert await store.v2_readiness() == (
        False,
        "memory_policy_backfill_incomplete",
    )


@pytest.mark.asyncio
async def test_unsupported_product_shape_fails_closed(store) -> None:
    await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="unsafe_object",
        value={"raw_id": "secret"},
    )

    assert await store.list_product_for_user("u1") == []
    assert await store.v2_readiness() == (
        False,
        "memory_product_policy_incomplete",
    )
