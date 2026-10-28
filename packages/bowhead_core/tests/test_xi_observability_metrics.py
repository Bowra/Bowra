"""0.39.7 #6 — ξ gate observability metric tracking.

`_xi_track_metric` is a fire-and-forget helper that writes to ctx.metrics_log
so the /admin/metrics/rollup?name=xi.gate.* endpoint can chart 0.39.4-0.39.6
citation-reform impact over time.

Tests cover: track when metrics_log present, no-op when None, attrs include
turn_id, never raises on bad ctx.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from bowhead_core.subagent_return_tool import _xi_track_metric


@dataclass
class _CapturingMetricsLog:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def track(self, name: str, *, attrs: dict[str, Any] | None = None, user_id: str | None = None) -> None:
        self.calls.append((name, dict(attrs or {})))


@dataclass
class _Ctx:
    turn_id: str = "t_test"
    metrics_log: Any = None


@pytest.mark.asyncio
async def test_xi_track_metric_writes_to_metrics_log() -> None:
    m = _CapturingMetricsLog()
    ctx = _Ctx(metrics_log=m)
    _xi_track_metric(ctx, "xi.gate.skip", reason="callout_only")
    # create_task is async — give it a tick to run
    await asyncio.sleep(0.01)
    assert len(m.calls) == 1
    name, attrs = m.calls[0]
    assert name == "xi.gate.skip"
    assert attrs == {"turn_id": "t_test", "reason": "callout_only"}


@pytest.mark.asyncio
async def test_xi_track_metric_noop_when_metrics_log_none() -> None:
    """metrics_log None must NOT raise — common in unit-test ctx stubs."""
    ctx = _Ctx(metrics_log=None)
    _xi_track_metric(ctx, "xi.gate.reject", retry="0")
    # Should not raise, no observable effect


@pytest.mark.asyncio
async def test_xi_track_metric_includes_multi_attrs() -> None:
    m = _CapturingMetricsLog()
    ctx = _Ctx(turn_id="t_xyz", metrics_log=m)
    _xi_track_metric(ctx, "xi.gate.reject", retry="1", error_count="3")
    await asyncio.sleep(0.01)
    assert m.calls[0][1] == {
        "turn_id": "t_xyz",
        "retry": "1",
        "error_count": "3",
    }


@pytest.mark.asyncio
async def test_xi_track_metric_never_raises_on_bad_ctx() -> None:
    """Even malformed ctx (e.g. missing turn_id attr) shouldn't raise —
    instrumentation must NEVER break the peer return path."""

    class BadCtx:
        pass

    ctx = BadCtx()
    # No metrics_log attr, no turn_id — should silently no-op
    _xi_track_metric(ctx, "xi.gate.skip", reason="x")
