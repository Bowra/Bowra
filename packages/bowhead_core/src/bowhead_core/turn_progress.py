"""In-process progress of the agents running in a turn (agent run limits, phase 3).

Subagents record their current step here; the SSE stream sends a transient
``turn.progress`` frame with this snapshot next to each keepalive so a long quiet
stretch can show "still running · agent · step N". It describes the present only:
it is never put on the event bus, persisted or replayed. Only the gateway process
that runs the turn has it; other processes send no progress frames.
"""

from __future__ import annotations

from typing import Any

_PROGRESS: dict[str, dict[str, dict[str, Any]]] = {}


def note_step(turn_id: str | None, subagent_id: str | None, *, profile: str, display_name: str | None, step: int) -> None:
    if not turn_id or not subagent_id:
        return
    _PROGRESS.setdefault(turn_id, {})[subagent_id] = {
        "subagent_id": subagent_id,
        "profile": profile,
        "display_name": display_name or "",
        "step": step,
    }


def finish(turn_id: str | None, subagent_id: str | None) -> None:
    if turn_id and subagent_id and turn_id in _PROGRESS:
        _PROGRESS[turn_id].pop(subagent_id, None)
        if not _PROGRESS[turn_id]:
            _PROGRESS.pop(turn_id, None)


def clear_turn(turn_id: str | None) -> None:
    if turn_id:
        _PROGRESS.pop(turn_id, None)


def snapshot(turn_id: str | None) -> list[dict[str, Any]]:
    return [dict(item) for item in _PROGRESS.get(turn_id or "", {}).values()]


__all__ = ["clear_turn", "finish", "note_step", "snapshot"]
