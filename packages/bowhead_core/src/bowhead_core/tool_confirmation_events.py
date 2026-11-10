"""Confirmation cards attached to the waiting tool step (``FEATURE_INLINE_TOOL_CONFIRMATION``).

A tool call that waits for the requester's decision (personal MCP, outgoing email,
Shopify content update, Team MCP) announces the wait on its own step with
``confirmation_requested`` and records how it ended with ``confirmation_resolved``.
Both go on the Turn bus the step was emitted on, so they are persisted and replayed
with the step. They carry labels only: arguments, email bodies and diffs stay in the
owner-only queue and are read through ``/api/v1/tool-confirmations/{id}``.

Stop and restart emit nothing: a cancelled handler must not emit while it unwinds, and
views derive "not run" from the Turn's own terminal events. Emission is best effort;
a missing card never approves or runs a call, because only the queue decides.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final, Literal

from bowhead_core.events import OrchestratorEvent

__all__ = [
    "ConfirmationKind",
    "ConfirmationOutcome",
    "announce_confirmation",
    "confirmation_callbacks",
    "inline_tool_confirmation_enabled",
    "resolve_confirmation",
]

logger = logging.getLogger(__name__)

ConfirmationKind = Literal["personal_mcp", "email_send", "shopify_content", "team_mcp"]
ConfirmationOutcome = Literal["approved", "approved_session", "rejected", "expired", "unavailable"]
ConfirmationChoice = Literal["reject", "approve", "approve_session"]
_OUTCOMES: Final = frozenset({"approved", "approved_session", "rejected", "expired", "unavailable"})
_TITLE_LIMIT: Final = 120


def inline_tool_confirmation_enabled() -> bool:
    """Read on every call so a deployment change applies to the next confirmation."""
    return os.environ.get("FEATURE_INLINE_TOOL_CONFIRMATION", "0").strip().lower() in {"1", "true", "yes"}


def _step(ctx: Any) -> Any | None:
    if not inline_tool_confirmation_enabled():
        return None
    step = getattr(ctx, "tool_step", None)
    if step is None or getattr(step, "bus", None) is None or not step.step_id:
        return None
    return step


async def _emit(step: Any, event: OrchestratorEvent) -> None:
    try:
        await step.bus.emit(event)
    except Exception as exc:  # noqa: BLE001 - the queue, not the card, decides the call
        logger.warning("tool_confirmation.emit_failed event=%s step=%s err=%s", event.event_type, step.step_id, type(exc).__name__)


async def announce_confirmation(
    ctx: Any,
    *,
    confirmation_id: str,
    kind: ConfirmationKind,
    requester_user_id: str,
    title: str,
    source_label: str | None,
    choices: list[ConfirmationChoice],
    expires_at: str,
    category: str | None = None,
) -> None:
    """Attach a pending confirmation to the caller's tool step."""
    step = _step(ctx)
    if step is None:
        return
    try:
        remaining = max(0, round((datetime.fromisoformat(expires_at) - datetime.now(UTC)).total_seconds()))
    except ValueError:
        remaining = 0
    await _emit(
        step,
        OrchestratorEvent(
            event_type="confirmation_requested",
            turn_id=step.card_id,
            payload={
                "card_id": step.card_id,
                "step_id": step.step_id,
                "call_id": step.call_id,
                "confirmation_id": confirmation_id,
                "kind": kind,
                "requester_user_id": requester_user_id,
                "title": (title or "")[:_TITLE_LIMIT],
                "source_label": source_label[:_TITLE_LIMIT] if source_label else None,
                "category": category if category in {"external_write", "destructive", "unknown"} else None,
                "choices": list(choices),
                "expires_at": expires_at,
                "timeout_seconds": remaining,
            },
            parent_agent_id=step.parent_agent_id,
        ),
    )


async def resolve_confirmation(ctx: Any, *, confirmation_id: str, outcome: str) -> None:
    """Record how a confirmation that was announced on this step ended."""
    step = _step(ctx)
    if step is None:
        return
    await _emit(
        step,
        OrchestratorEvent(
            event_type="confirmation_resolved",
            turn_id=step.card_id,
            payload={
                "card_id": step.card_id,
                "step_id": step.step_id,
                "confirmation_id": confirmation_id,
                "outcome": outcome if outcome in _OUTCOMES else "unavailable",
                "decided_at": datetime.now(UTC).isoformat(),
            },
            parent_agent_id=step.parent_agent_id,
        ),
    )


def confirmation_callbacks(
    ctx: Any,
    *,
    kind: ConfirmationKind,
    requester_user_id: str,
    title: str,
    source_label: str | None,
    choices: list[ConfirmationChoice],
    category: str | None = None,
) -> tuple[Callable[[dict[str, Any]], Awaitable[None]], Callable[[str], Awaitable[None]]]:
    """``on_pending`` / ``on_resolved`` for ``DeliveryConfirmations.request_snapshot``."""
    announced: list[str] = []

    async def on_pending(item: dict[str, Any]) -> None:
        announced.append(str(item["id"]))
        await announce_confirmation(
            ctx,
            confirmation_id=str(item["id"]),
            kind=kind,
            requester_user_id=requester_user_id,
            title=title,
            source_label=source_label,
            choices=choices,
            expires_at=str(item["expires_at"]),
            category=category,
        )

    async def on_resolved(outcome: str) -> None:
        if announced:
            await resolve_confirmation(ctx, confirmation_id=announced[0], outcome=outcome)

    return on_pending, on_resolved
