"""Mid-run steering: a running ordinary Lead takes instructions queued for its Turn.

An instruction the user sends while an answer is running is queued in the Session's
steer buffer. The Lead takes the plain-text head of that buffer at a step boundary and
continues the same Turn with it. Whatever is not taken waits for the Turn to end and
runs as a follow-up Turn.

Taking is read, record, claim, in that order, so an accepted instruction is never lost:
until the claim commits it is still in the buffer, where finalization and recovery
already own it, and the ``steer.applied`` row exists before the model sees the text.
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from bowhead_core.events import OrchestratorEvent

logger = logging.getLogger(__name__)


def _waited_ms(queued_at: object) -> int | None:
    try:
        queued = datetime.fromisoformat(str(queued_at))
    except ValueError:
        return None
    if queued.tzinfo is None:
        queued = queued.replace(tzinfo=UTC)
    return max(0, int((datetime.now(tz=UTC) - queued).total_seconds() * 1000))


@dataclass
class SteerInbox:
    """The running Lead's view of the instructions queued for its Turn."""

    store: Any
    bus: Any
    session_id: str
    turn_id: str
    request_text: str
    taken: list[str] = field(default_factory=list)
    closed: bool = False
    # Set by the Runner while the Lead waits on a delegation: wakes that wait.
    waker: Callable[[], Awaitable[None]] | None = field(default=None, repr=False, compare=False)

    async def take(self, ctx: Any, *, iteration: int, while_waiting: bool = False) -> list[str]:
        """Take the queued plain-text instructions and return their texts in order.

        ``ctx`` is the Lead's running context: its request text is extended so helpers
        started later check their work against everything the user asked in this Turn.
        """
        if self.closed:
            return []
        try:
            items = await self.store.read_steer_head(session_id=self.session_id, turn_id=self.turn_id)
        except Exception as exc:  # noqa: BLE001 — a failed read takes nothing; the next step reads again
            logger.warning("steer.read_failed turn=%s error_type=%s", self.turn_id, type(exc).__name__)
            return []
        if not items:
            return []
        waited_ms = _waited_ms(items[0].get("ts"))
        recorded = await self.bus.emit(
            OrchestratorEvent(
                event_type="steer.applied",
                turn_id=self.turn_id,
                payload={
                    "items": [
                        {
                            "text": item["text"],
                            "queued_at": item.get("ts"),
                            **({"submission_id": item["submission_id"]} if item.get("submission_id") else {}),
                        }
                        for item in items
                    ],
                    # Ordinal of the first item among everything this Turn has taken: a stable
                    # identity for each instruction in live delivery and replay.
                    "position": len(self.taken),
                    "iteration": iteration,
                    "while_waiting": while_waiting,
                    "waited_ms": waited_ms,
                },
                persist_before_delivery=True,
            )
        )
        if not recorded:
            # The bus is closing: the Turn is ending and the buffer stays with finalization.
            return []
        try:
            claimed = await self.store.claim_steer_head(session_id=self.session_id, turn_id=self.turn_id, items=items)
            condition = "dispatch_or_buffer_changed"
        except Exception as exc:  # noqa: BLE001 — an unwritten claim is a refused claim
            claimed, condition = False, f"store_error:{type(exc).__name__}"
        if not claimed:
            # The Turn lost its dispatch, recovery rewrote the buffer, or the store failed.
            # The instructions stay queued; taking again would record the same text twice.
            self.closed = True
            logger.warning("steer.claim_refused turn=%s items=%d condition=%s", self.turn_id, len(items), condition)
            return []
        texts = [str(item["text"]) for item in items]
        self.taken.extend(texts)
        ctx.user_text = "\n\n".join([self.request_text, *self.taken])
        logger.info(
            "steer.applied turn=%s iter=%d items=%d waited_ms=%s while_waiting=%s",
            self.turn_id,
            iteration,
            len(texts),
            waited_ms,
            while_waiting,
        )
        return texts


# Inboxes of the Lead Turns running in this process. Entries leave with their Turn's contexts.
_LOCAL_INBOXES: weakref.WeakValueDictionary[str, SteerInbox] = weakref.WeakValueDictionary()


async def notify_local(turn_id: str) -> None:
    """Tell a Lead that waits on a delegation in this process that an instruction was queued.

    A no-op when the Turn runs elsewhere or its Lead is not waiting: a waiting Lead also
    looks every few seconds, and a Lead between steps reads the buffer at its next step.
    """
    inbox = _LOCAL_INBOXES.get(turn_id)
    waker = inbox.waker if inbox is not None else None
    if waker is not None:
        await waker()


async def attach_steer_inbox(ctx: Any, *, bus: Any, turn_id: str) -> None:
    """Give an eligible ordinary Lead context its inbox; leave every other context unchanged.

    A Turn that resumes after a pause already took instructions in its earlier run. They
    are restored into the request text so later helpers still check against them.
    """
    from bowhead_core.context_history import is_ordinary_lead_context

    store = getattr(ctx, "session_store", None)
    session_id = str(getattr(ctx, "session_id", "") or "")
    if (
        not session_id
        or not is_ordinary_lead_context(ctx)
        or not callable(getattr(store, "read_steer_head", None))
        or not callable(getattr(store, "claim_steer_head", None))
        or not callable(getattr(store, "list_turn_applied_instructions", None))
    ):
        return
    request_text = str(getattr(ctx, "user_text", "") or "")
    try:
        taken = (await store.list_turn_applied_instructions([turn_id])).get(turn_id, [])
    except Exception as exc:  # noqa: BLE001 — without its record this run takes nothing; instructions follow up
        logger.warning("steer.attach_failed turn=%s error_type=%s", turn_id, type(exc).__name__)
        return
    ctx.steer_inbox = SteerInbox(store=store, bus=bus, session_id=session_id, turn_id=turn_id, request_text=request_text, taken=list(taken))
    _LOCAL_INBOXES[turn_id] = ctx.steer_inbox
    if taken:
        ctx.user_text = "\n\n".join([request_text, *taken])
