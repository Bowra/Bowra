"""Owner confirmation queue for Lead delivery actions (email, Google Workspace writes).

The tool handler waits inside the turn while the owner reviews the exact snapshot
in the browser, then approves or rejects it. Only an explicit approval releases the
action; rejection, timeout and cancellation all leave it undone.

The queue is in-process on purpose: the waiting handler lives in the same process
(the app runs a single uvicorn process — see ``deploy/deploy.md``), so a restart
ends both the wait and the turn and there is nothing to recover. Each delivery kind
owns its own queue instance and endpoint, so one kind's UI never sees another's.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import anyio

CONFIRM_SECONDS: Final = 180
MAX_PENDING_PER_OWNER: Final = 10

Decision = Literal["approved", "rejected", "expired", "unavailable"]


@dataclass
class _Pending:
    id: str
    owner_user_id: str
    turn_id: str
    session_id: str
    payload: dict[str, Any]
    expires_at: str
    created: float
    event: anyio.Event = field(default_factory=anyio.Event)
    decision: Decision | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "turn_id": self.turn_id, "session_id": self.session_id, **self.payload, "expires_at": self.expires_at}


class DeliveryConfirmations:
    def __init__(self, *, id_prefix: str, timeout_seconds: float = CONFIRM_SECONDS) -> None:
        self._prefix = id_prefix
        self._timeout = timeout_seconds
        self._pending: dict[str, _Pending] = {}

    async def request_snapshot(
        self,
        *,
        owner_user_id: str,
        turn_id: str,
        session_id: str,
        payload: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> Decision:
        """Wait for the owner's decision on this exact snapshot."""
        timeout = self._timeout if timeout_seconds is None else max(0.0, min(self._timeout, timeout_seconds))
        if not owner_user_id or sum(1 for p in self._pending.values() if p.owner_user_id == owner_user_id) >= MAX_PENDING_PER_OWNER:
            return "unavailable"
        item = _Pending(
            id=f"{self._prefix}_{uuid.uuid4().hex}",
            owner_user_id=owner_user_id,
            turn_id=turn_id,
            session_id=session_id,
            payload=dict(payload),
            expires_at=(datetime.now(UTC) + timedelta(seconds=timeout)).isoformat(),
            created=time.monotonic(),
        )
        self._pending[item.id] = item
        try:
            with anyio.move_on_after(timeout):
                await item.event.wait()
            return item.decision or "expired"
        finally:
            self._pending.pop(item.id, None)

    def pending(self, owner_user_id: str) -> list[dict[str, Any]]:
        items = sorted(
            (p for p in self._pending.values() if p.owner_user_id == owner_user_id and p.decision is None),
            key=lambda p: p.created,
        )
        return [p.public() for p in items]

    def decide(self, owner_user_id: str, confirmation_id: str, approve: bool) -> bool:
        """Record the owner's decision; False when the request is gone or not theirs."""
        item = self._pending.get(confirmation_id)
        if item is None or item.owner_user_id != owner_user_id or item.decision is not None:
            return False
        item.decision = "approved" if approve else "rejected"
        item.event.set()
        return True
