"""chat-v4 W3 CTO P1-A fix UT — strong-ref task list bounded via add_done_callback.

CTO finding on commit 0a1d8cb (lead_orchestrator.py:~916): `_v4_emit_task_refs`
stored `asyncio.create_task(...)` to prevent GC of fire-and-forget emit
tasks, but never pruned completed entries. For long turns / replay storms
the list grows unbounded (low risk per-turn but observable). Fix: each
created task gets `add_done_callback(_v4_emit_task_refs.remove)` so the
list drains as tasks complete.

This UT mirrors the closure pattern from lead_orchestrator (same as the
dedup test) and verifies the list is empty after all tasks await.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _build_emit_with_prune() -> tuple[list[Any], Any, list[tuple[str, dict]]]:
    """Replicate the production strong-ref + prune pattern."""
    emitted: list[tuple[str, dict]] = []
    task_refs: list[Any] = []

    class _FakeBus:
        async def emit(self, name: str, payload: dict) -> None:
            # Yield to scheduler so the task isn't synchronously done before
            # add_done_callback wires up.
            await asyncio.sleep(0)
            emitted.append((name, payload))

    bus = _FakeBus()

    def emit_sync(name: str, payload: dict) -> None:
        loop = asyncio.get_event_loop()
        t = loop.create_task(bus.emit(name, payload))
        task_refs.append(t)
        t.add_done_callback(task_refs.remove)

    return task_refs, emit_sync, emitted


@pytest.mark.asyncio
async def test_task_refs_pruned_on_done() -> None:
    """Fire 5 emit tasks, await all → ref list must be empty."""
    task_refs, emit_sync, emitted = _build_emit_with_prune()

    for i in range(5):
        emit_sync("lead.subagent_summary", {"i": i})

    # Before tasks run: all 5 refs present.
    assert len(task_refs) == 5

    # Drain all pending tasks.
    pending = list(task_refs)
    await asyncio.gather(*pending)
    # One more scheduler tick so done_callback fires.
    await asyncio.sleep(0)

    assert task_refs == [], f"expected empty refs after completion, got {len(task_refs)}: {task_refs}"
    assert len(emitted) == 5


@pytest.mark.asyncio
async def test_task_refs_prune_interleaved() -> None:
    """Refs prune as tasks finish, even with new emits arriving mid-flight."""
    task_refs, emit_sync, emitted = _build_emit_with_prune()

    emit_sync("lead.subagent_summary", {"phase_id": "p1", "status": "running"})
    emit_sync("lead.subagent_summary", {"phase_id": "p1", "status": "done"})
    await asyncio.gather(*list(task_refs))
    await asyncio.sleep(0)
    assert task_refs == []
    assert len(emitted) == 2

    # New batch — list grew and drained again.
    emit_sync("lead.final.payload", {})
    assert len(task_refs) == 1
    await asyncio.gather(*list(task_refs))
    await asyncio.sleep(0)
    assert task_refs == []
