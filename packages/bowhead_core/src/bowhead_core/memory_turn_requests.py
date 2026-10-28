"""Per-turn "the user explicitly asked to remember" facts for the memory receipt.

Each row is written by the code path that observed the request: the ``remember`` tool
handler, the zero-click capture path (including its language-setting branch), and the
post-turn ``nl_intent`` enqueue. No row is ever derived from model prose. The capture and
``nl_intent`` outcomes are known only after ``done``, so they live in a table rather than on
the turn event stream; the receipt is a REST projection read from this table and from
persisted memory state, identical live and after a refresh.

Rows hold only finite codes and IDs. Migration ``0153_memory_turn_requests`` deletes them
together with their turn and with the user account.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from bowhead_core._db import open_shared_ctx
from bowhead_core.memory_privacy import is_memory_read_hidden

logger = logging.getLogger(__name__)

RequestSource = Literal["remember_tool", "capture", "language_setting", "nl_intent"]
RequestOutcome = Literal["remembered", "candidate", "not_remembered"]

SOURCE_REMEMBER_TOOL: Final = "remember_tool"
SOURCE_CAPTURE: Final = "capture"
SOURCE_LANGUAGE_SETTING: Final = "language_setting"
SOURCE_NL_INTENT: Final = "nl_intent"

#: Most specific first. With several sources in one turn, the receipt reports the first one present.
SOURCE_PRECEDENCE: Final[tuple[RequestSource, ...]] = (
    SOURCE_REMEMBER_TOOL,
    SOURCE_CAPTURE,
    SOURCE_LANGUAGE_SETTING,
    SOURCE_NL_INTENT,
)

OUTCOME_REMEMBERED: Final = "remembered"
OUTCOME_CANDIDATE: Final = "candidate"
OUTCOME_NOT_REMEMBERED: Final = "not_remembered"

#: Same value as ``bowhead_memory.candidates.ENQUEUE_*`` and ``memory_auto_capture.ENQUEUE_*``.
#: bowhead_core does not import bowhead_memory; a guard test pins the parity.
ENQUEUE_CREATED: Final = "created"
ENQUEUE_DUPLICATE_PENDING: Final = "duplicate_pending"

#: Refusal codes the receipt may name. Every other reason is returned as ``null`` and renders the
#: generic "not remembered" copy. The frontend owns the wording (``memory_card.not_remembered_reason.*``)
#: and a registry test requires one key per code in every locale.
#:
#: - Write-gate reasons shared by ``remember`` and the capture path (``memory_write_gate.REASON_*``).
#: - ``locked``: the notes-backed ``remember`` path hit the editing lock while the user edits memory.
#: - ``entry_locked``: the canonical ``remember`` path found a locked entry on the same topic.
#: - Capture reasons with ``memory_auto_capture.REASON_USER_FACING == True`` that mean "not remembered".
#:   ``tainted_input`` is absent on purpose: it produces a candidate, not a refusal.
DISPLAYABLE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "language_is_a_setting",
        "one_off",
        "no_attribution",
        "contains_secret",
        "locked",
        "entry_locked",
        "trigger_language_setting",
        "draft_says_not_lasting",
        "rate_limited",
        "draft_too_long",
        "kind_not_allowed",
        "draft_echoed_sanitizer_marker",
        "key_too_long",
        "blocked_by_stronger_entry",
    }
)

#: Capture reasons meaning the capture path never acted on a request. They record no row.
_CAPTURE_NOT_A_REQUEST: Final[frozenset[str]] = frozenset({"flag_off", "learning_paused"})


@dataclass(frozen=True)
class MemoryTurnRequest:
    source: RequestSource
    outcome: RequestOutcome
    reason: str | None = None
    entry_id: str | None = None
    candidate_id: str | None = None


def public_reason(reason: str | None) -> str | None:
    """The reason code the receipt may show, or ``None`` for the generic copy."""
    return reason if reason in DISPLAYABLE_REASONS else None


def from_remember_result(result: object) -> MemoryTurnRequest:
    """Map one ``remember`` tool receipt. Any invocation is an explicit request, whatever its result."""
    if isinstance(result, Mapping) and result.get("ok") is True:
        entry_id = result.get("id")
        return MemoryTurnRequest(
            SOURCE_REMEMBER_TOOL,
            OUTCOME_REMEMBERED,
            entry_id=str(entry_id) if entry_id else None,
        )
    error = result.get("error") if isinstance(result, Mapping) else None
    return MemoryTurnRequest(
        SOURCE_REMEMBER_TOOL,
        OUTCOME_NOT_REMEMBERED,
        reason=str(error) if error else None,
    )


def from_capture_outcome(outcome: Any) -> MemoryTurnRequest | None:
    """Map a zero-click ``CaptureOutcome``; ``None`` when the user's message was not a request.

    Only a deterministic ``persistent_instruction`` or ``language_setting`` classification counts.
    The flag being off, learning being paused and every other trigger decision record nothing.
    """
    from bowhead_core.memory_auto_capture import (
        ACTION_NEEDS_CONFIRMATION,
        ACTION_SETTING_SUGGESTED,
        ACTION_WRITTEN,
    )

    action = getattr(outcome, "action", None)
    reason = getattr(outcome, "reason", None)
    if action == ACTION_SETTING_SUGGESTED:
        return MemoryTurnRequest(SOURCE_LANGUAGE_SETTING, OUTCOME_NOT_REMEMBERED, reason=reason)
    if reason in _CAPTURE_NOT_A_REQUEST or (isinstance(reason, str) and reason.startswith("trigger_")):
        return None
    entry_id = getattr(outcome, "entry_id", None)
    if action == ACTION_WRITTEN:
        return MemoryTurnRequest(SOURCE_CAPTURE, OUTCOME_REMEMBERED, entry_id=entry_id)
    if action == ACTION_NEEDS_CONFIRMATION:
        # A created card carries its candidate id; an equal pending card from another turn does not.
        return MemoryTurnRequest(
            SOURCE_CAPTURE,
            OUTCOME_CANDIDATE,
            reason=None if entry_id else ENQUEUE_DUPLICATE_PENDING,
            candidate_id=entry_id,
        )
    return MemoryTurnRequest(SOURCE_CAPTURE, OUTCOME_NOT_REMEMBERED, reason=reason)


def from_nl_intent_enqueue(candidate: Any, enqueue_outcome: str) -> MemoryTurnRequest:
    """Map the recorded enqueue outcome of a confident ``nl_intent`` detection."""
    if enqueue_outcome == ENQUEUE_CREATED and candidate is not None:
        return MemoryTurnRequest(SOURCE_NL_INTENT, OUTCOME_CANDIDATE, candidate_id=getattr(candidate, "id", None))
    if enqueue_outcome == ENQUEUE_DUPLICATE_PENDING:
        return MemoryTurnRequest(SOURCE_NL_INTENT, OUTCOME_CANDIDATE, reason=ENQUEUE_DUPLICATE_PENDING)
    return MemoryTurnRequest(SOURCE_NL_INTENT, OUTCOME_NOT_REMEMBERED, reason=enqueue_outcome or None)


#: Outcomes that gave the user's sentence a landing place (a memory entry or a card to confirm).
LANDED_OUTCOMES: Final[frozenset[str]] = frozenset({OUTCOME_REMEMBERED, OUTCOME_CANDIDATE})


def select_explicit_request(rows: Iterable[MemoryTurnRequest]) -> MemoryTurnRequest | None:
    """Pick the receipt's explicit request.

    A landed outcome from any source wins over a ``not_remembered`` from another source: one
    sentence can be skipped by capture and still become an ``nl_intent`` card, and the receipt
    must not then also say "not remembered". Within the same tier, the most specific source wins.
    """
    rows = list(rows)
    landed = [row for row in rows if row.outcome in LANDED_OUTCOMES]
    by_source = {row.source: row for row in (landed or rows)}
    for source in SOURCE_PRECEDENCE:
        if source in by_source:
            return by_source[source]
    return None


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class MemoryTurnRequestStore:
    """Writer and reader of ``memory_turn_requests`` (core database, created by migration 0153)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def record(self, *, user_id: str, turn_id: str, request: MemoryTurnRequest) -> None:
        """Upsert one row per source.

        A later observation replaces an earlier one (a retried call replaces a refusal), except that
        ``remembered`` is sticky: a later refusal in the same turn must not hide a memory already written.
        """
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO memory_turn_requests(user_id, turn_id, source, outcome, reason, entry_id, candidate_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, turn_id, source) DO UPDATE SET
                  outcome=excluded.outcome, reason=excluded.reason, entry_id=excluded.entry_id,
                  candidate_id=excluded.candidate_id, created_at=excluded.created_at
                WHERE NOT (memory_turn_requests.outcome='remembered' AND excluded.outcome='not_remembered')
                """,
                (
                    user_id,
                    turn_id,
                    request.source,
                    request.outcome,
                    request.reason,
                    request.entry_id,
                    request.candidate_id,
                    _now(),
                ),
            )
            await db.commit()

    async def list_for_turn(self, *, user_id: str, turn_id: str) -> list[MemoryTurnRequest]:
        async with open_shared_ctx(self._db_path) as db:
            if await is_memory_read_hidden(db, user_id=user_id):
                return []
            async with db.execute(
                "SELECT source, outcome, reason, entry_id, candidate_id FROM memory_turn_requests WHERE user_id=? AND turn_id=?",
                (user_id, turn_id),
            ) as cur:
                rows = await cur.fetchall()
        return [MemoryTurnRequest(row[0], row[1], row[2], row[3], row[4]) for row in rows]


def store_for(*candidates: Any) -> MemoryTurnRequestStore | None:
    """Build a store on the core database path of the first memory store that exposes one."""
    for candidate in candidates:
        path = getattr(candidate, "db_path", None) or getattr(candidate, "_db_path", None)
        if isinstance(path, str | Path) and str(path):
            return MemoryTurnRequestStore(path)
    return None


async def record_best_effort(
    store: MemoryTurnRequestStore | None,
    *,
    user_id: str | None,
    turn_id: str | None,
    request: MemoryTurnRequest | None,
) -> None:
    """Record a request without ever failing the turn; a missing row only means "no explicit request" on read."""
    if store is None or request is None or not user_id or not turn_id:
        return
    try:
        await store.record(user_id=user_id, turn_id=turn_id, request=request)
    except Exception as exc:  # noqa: BLE001 - the receipt is supplementary; the turn must not fail on it
        logger.warning(
            "memory_turn_request.record_failed turn=%s source=%s err=%s",
            turn_id,
            request.source,
            exc,
        )


__all__ = [
    "DISPLAYABLE_REASONS",
    "OUTCOME_CANDIDATE",
    "OUTCOME_NOT_REMEMBERED",
    "OUTCOME_REMEMBERED",
    "SOURCE_CAPTURE",
    "SOURCE_LANGUAGE_SETTING",
    "SOURCE_NL_INTENT",
    "SOURCE_PRECEDENCE",
    "SOURCE_REMEMBER_TOOL",
    "MemoryTurnRequest",
    "MemoryTurnRequestStore",
    "from_capture_outcome",
    "from_nl_intent_enqueue",
    "from_remember_result",
    "public_reason",
    "record_best_effort",
    "select_explicit_request",
    "store_for",
]
