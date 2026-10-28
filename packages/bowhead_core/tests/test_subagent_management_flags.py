from __future__ import annotations

import pytest
from bowhead_core.subagent_management_flags import subagent_management_v2_ready


@pytest.mark.asyncio
async def test_subagent_management_v2_requires_schema_compatibility_and_cutover() -> None:
    class Store:
        def __init__(self, ready: bool) -> None:
            self.ready = ready

        async def marketplace_ready(self) -> bool:
            return self.ready

    assert await subagent_management_v2_ready(Store(True), schema_ready=True) is True
    assert await subagent_management_v2_ready(Store(False), schema_ready=True) is False
    assert await subagent_management_v2_ready(Store(True), schema_ready=False) is False
    assert await subagent_management_v2_ready(None, schema_ready=True) is False


@pytest.mark.asyncio
async def test_subagent_management_v2_readiness_fails_closed() -> None:
    class BrokenStore:
        async def marketplace_ready(self) -> bool:
            raise RuntimeError("schema drift")

    assert await subagent_management_v2_ready(BrokenStore(), schema_ready=True) is False
