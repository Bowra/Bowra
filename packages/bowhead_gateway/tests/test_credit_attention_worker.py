from __future__ import annotations

from typing import Any

import pytest
from bowhead_gateway.credit_attention_worker import (
    env_materializer_enabled,
    materialize_credit_attention_once,
)


class _FakeUserStore:
    def __init__(self, team_ids: list[str]) -> None:
        self.team_ids = team_ids
        self.list_calls: list[dict[str, int]] = []
        self.materialized: list[str] = []
        self.metrics_logs: list[Any | None] = []

    async def list_active_team_ids(self, *, limit: int = 500, offset: int = 0) -> list[str]:
        self.list_calls.append({"limit": limit, "offset": offset})
        return self.team_ids[offset : offset + limit]

    async def materialize_credit_attention(
        self,
        team_id: str,
        *,
        limit: int = 20,
        metrics_log: Any | None = None,
    ) -> list[Any]:
        self.materialized.append(team_id)
        self.metrics_logs.append(metrics_log)
        return []


@pytest.mark.integration
async def test_materialize_credit_attention_once_honors_team_offset() -> None:
    store = _FakeUserStore(["team_0", "team_1", "team_2", "team_3"])

    result = await materialize_credit_attention_once(store, team_limit=2, team_offset=2)

    assert result == {"team_count": 2, "ok": 2, "failed": 0}
    assert store.list_calls == [{"limit": 2, "offset": 2}]
    assert store.materialized == ["team_2", "team_3"]
    assert store.metrics_logs == [None, None]


@pytest.mark.integration
async def test_materialize_credit_attention_once_passes_metrics_log() -> None:
    metrics_log = object()
    store = _FakeUserStore(["team_0"])

    result = await materialize_credit_attention_once(store, metrics_log=metrics_log)

    assert result == {"team_count": 1, "ok": 1, "failed": 0}
    assert store.metrics_logs == [metrics_log]


@pytest.mark.integration
async def test_materialize_credit_attention_once_invalidates_team_cache() -> None:
    store = _FakeUserStore(["team_0", "team_1"])
    invalidated: list[str] = []

    async def _invalidate(team_id: str) -> None:
        invalidated.append(team_id)

    result = await materialize_credit_attention_once(
        store,
        cache_invalidator=_invalidate,
    )

    assert result == {"team_count": 2, "ok": 2, "failed": 0}
    assert invalidated == ["team_0", "team_1"]


def test_env_materializer_enabled_supports_documented_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_CREDIT_ATTENTION_WORKER_DISABLED", raising=False)
    monkeypatch.setenv("BOWHEAD_CREDIT_ATTENTION_MATERIALIZER_ENABLED", "false")
    assert env_materializer_enabled() is False

    monkeypatch.setenv("BOWHEAD_CREDIT_ATTENTION_MATERIALIZER_ENABLED", "true")
    assert env_materializer_enabled() is True

    monkeypatch.setenv("BOWHEAD_CREDIT_ATTENTION_WORKER_DISABLED", "1")
    assert env_materializer_enabled() is False
