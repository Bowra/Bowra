"""canvas-comprehensive Batch 1 (WP-5 / FR-1.6 / B7) — psi compose lock timeout
surfaces an explicit canvas_op_failed instead of a silent empty return.

The silent `return []` on lock timeout was B7: "narrative says report, canvas blank,
no explanation". Now the Lead-end compose (emit_lifecycle=True) emits canvas_op_failed
(op=compose, reason_code=lock_timeout); an eager-peer compose (emit_lifecycle=False)
stays silent because the subsequent Lead compose retries it (emitting = false alarm).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.canvas_composer import (
    _get_canvas_lock,
    compose_canvas_from_payload,
)
from bowhead_core.canvas_store import CanvasStore

from bowhead_core import canvas_composer

if TYPE_CHECKING:
    from pathlib import Path


class _CaptureBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


async def _prime(tmp_path: Path):
    store = CanvasStore(tmp_path / "t.db")
    await store.initialize()
    canvas = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return store, canvas


def _req() -> SimpleNamespace:
    return SimpleNamespace(session_id="s1", workspace_id="w", user_id="u", turn_id="t1")


def _op_failed(bus: _CaptureBus) -> list:
    return [e for e in bus.events if getattr(e, "event_type", None) == "canvas_op_failed"]


@pytest.mark.asyncio
async def test_lock_timeout_lead_compose_emits_canvas_op_failed(tmp_path, monkeypatch) -> None:
    store, canvas = await _prime(tmp_path)
    monkeypatch.setattr(canvas_composer, "_PSI_COMPOSE_LOCK_TIMEOUT_SEC", 0.01)
    lock = _get_canvas_lock(canvas.id)
    await lock.acquire()  # hold it so compose's acquire times out
    try:
        bus = _CaptureBus()
        out = await compose_canvas_from_payload(
            canvas_store=store,
            request=_req(),
            bus=bus,
            payload={"exec_summary": "x"},
            emit_lifecycle=True,
        )
        assert out == []  # timed out → empty
        failed = _op_failed(bus)
        assert len(failed) == 1
        assert failed[0].payload["reason_code"] == "canvas_compose_timeout"
        assert failed[0].payload["op"] == "compose"
        assert failed[0].payload["turn_id"] == "t1"
        assert failed[0].payload["diagnostics"]["diagnostic_code"] == ("chat_canvas_failed")
        assert failed[0].payload["diagnostics"]["turn_id"] == "t1"
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_lock_timeout_eager_compose_stays_silent(tmp_path, monkeypatch) -> None:
    store, canvas = await _prime(tmp_path)
    monkeypatch.setattr(canvas_composer, "_PSI_COMPOSE_LOCK_TIMEOUT_SEC", 0.01)
    lock = _get_canvas_lock(canvas.id)
    await lock.acquire()
    try:
        bus = _CaptureBus()
        out = await compose_canvas_from_payload(
            canvas_store=store,
            request=_req(),
            bus=bus,
            payload={"exec_summary": "x"},
            emit_lifecycle=False,  # eager peer
        )
        assert out == []
        assert _op_failed(bus) == []  # silent — Lead compose will retry
    finally:
        lock.release()
