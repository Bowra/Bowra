from __future__ import annotations

import asyncio

import pytest
from bowhead_core.kb.ingestion_gate import TeamKbIngestionGate


@pytest.mark.asyncio
async def test_promotion_waits_for_active_ingestion_and_blocks_new_ingestion():
    gate = TeamKbIngestionGate()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    promotion_started = asyncio.Event()
    release_promotion = asyncio.Event()
    second_started = asyncio.Event()

    async def first_ingestion() -> None:
        async with gate.ingestion("team_a"):
            first_started.set()
            await release_first.wait()

    async def promote() -> None:
        async with gate.promotion("team_a"):
            promotion_started.set()
            await release_promotion.wait()

    async def second_ingestion() -> None:
        async with gate.ingestion("team_a"):
            second_started.set()

    first_task = asyncio.create_task(first_ingestion())
    await first_started.wait()
    promotion_task = asyncio.create_task(promote())
    await asyncio.sleep(0)
    second_task = asyncio.create_task(second_ingestion())
    await asyncio.sleep(0)

    assert not promotion_started.is_set()
    assert not second_started.is_set()

    release_first.set()
    await promotion_started.wait()
    assert not second_started.is_set()

    release_promotion.set()
    await asyncio.gather(first_task, promotion_task, second_task)
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_cancelled_waiting_promotion_releases_the_team_gate():
    gate = TeamKbIngestionGate()
    release_ingestion = asyncio.Event()

    async def ingestion() -> None:
        async with gate.ingestion("team_a"):
            await release_ingestion.wait()

    async def promotion() -> None:
        async with gate.promotion("team_a"):
            pass

    ingestion_task = asyncio.create_task(ingestion())
    await asyncio.sleep(0)
    promotion_task = asyncio.create_task(promotion())
    await asyncio.sleep(0)
    promotion_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await promotion_task

    release_ingestion.set()
    await ingestion_task
    async with gate.ingestion("team_a"):
        pass
