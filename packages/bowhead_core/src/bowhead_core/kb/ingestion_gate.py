"""Per-team coordination between normal KB ingestion and index promotion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _TeamGateState:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    active_ingestions: int = 0
    promotion_pending: bool = False


class TeamKbIngestionGate:
    """Let ingestion run concurrently, but give final index promotion exclusivity."""

    def __init__(self) -> None:
        self._states: dict[str, _TeamGateState] = {}

    def _state(self, team_id: str) -> _TeamGateState:
        return self._states.setdefault(team_id, _TeamGateState())

    @asynccontextmanager
    async def ingestion(self, team_id: str) -> AsyncIterator[None]:
        state = self._state(team_id)
        async with state.condition:
            await state.condition.wait_for(lambda: not state.promotion_pending)
            state.active_ingestions += 1
        try:
            yield
        finally:
            async with state.condition:
                state.active_ingestions -= 1
                state.condition.notify_all()

    @asynccontextmanager
    async def promotion(self, team_id: str) -> AsyncIterator[None]:
        state = self._state(team_id)
        async with state.condition:
            await state.condition.wait_for(lambda: not state.promotion_pending)
            state.promotion_pending = True
            try:
                await state.condition.wait_for(lambda: state.active_ingestions == 0)
            except BaseException:
                state.promotion_pending = False
                state.condition.notify_all()
                raise
        try:
            yield
        finally:
            async with state.condition:
                state.promotion_pending = False
                state.condition.notify_all()
