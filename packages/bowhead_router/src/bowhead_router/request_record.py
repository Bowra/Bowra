"""Diagnostic record of every actual model send, plus why its content was changed.

The router calls `record_request` from `begin_physical_invocation` with the exact
canonical wire body it hashed into `request_digest`. A Gateway recorder buffers it
and writes it later (docs/design/llm-request-record-tech-design.md). Recording is
diagnostics only: nothing here may block, await or raise into a model call.

Core code that changes model-visible content after assembly (tool result capping,
window guard, overflow shrink, chat-direct demotion) calls `note_request_change`.
Pending changes travel with the next recorded request of the same run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bowhead_router.usage import UsageContext, current_usage_context

log = logging.getLogger(__name__)

CHANGE_KINDS = frozenset({"tool_result_capped", "tool_result_cleared", "history_turns_dropped", "overflow_shrink", "chat_direct_demoted"})


@dataclass(frozen=True, slots=True)
class RequestChange:
    kind: str
    #: Identifiers, sizes, hashes and parameters only; never content.
    detail: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RequestRecordEntry:
    usage: UsageContext
    provider: str
    model: str
    family: str | None
    mode: str
    canonical_body: bytes
    request_digest: str
    usage_event_id: str | None
    subagent_id: str | None
    created_at: datetime
    changes: tuple[RequestChange, ...] = field(default_factory=tuple)


RequestRecorder = Callable[[RequestRecordEntry], None]

_RECORDER: RequestRecorder | None = None
# One mutable list per run; tasks started inside the run copy the context and share it.
_PENDING_CHANGES: ContextVar[list[RequestChange] | None] = ContextVar("bowhead_pending_request_changes", default=None)


def set_request_recorder(recorder: RequestRecorder | None) -> RequestRecorder | None:
    """Install (or remove with None) the process recorder; returns the previous one."""
    global _RECORDER
    previous = _RECORDER
    _RECORDER = recorder
    return previous


@contextmanager
def request_change_scope() -> Iterator[None]:
    """Give one model-calling run its own pending-change list."""
    token = _PENDING_CHANGES.set([])
    try:
        yield
    finally:
        _PENDING_CHANGES.reset(token)


def note_request_change(kind: str, **detail: object) -> None:
    """Remember why model-visible content changed; attached to the run's next recorded request."""
    try:
        if kind not in CHANGE_KINDS:
            raise ValueError(f"unknown request change kind: {kind}")
        pending = _PENDING_CHANGES.get()
        if pending is not None:
            pending.append(RequestChange(kind=kind, detail=dict(detail)))
    except Exception:  # noqa: BLE001 — diagnostics must never break the caller.
        log.warning("llm_request_record.change_failed kind=%s", kind, exc_info=True)


def record_request(
    *,
    provider: str,
    model: str,
    family: str | None,
    mode: str,
    canonical_body: bytes,
    request_digest: str,
    usage_event_id: str | None,
    subagent_id: str | None,
) -> None:
    """Hand one actual send to the installed recorder. Never raises."""
    recorder = _RECORDER
    if recorder is None:
        return
    try:
        usage = current_usage_context()
        if usage.turn_id is None or usage.team_id is None or usage.user_id is None or usage.workspace_id is None:
            return
        pending = _PENDING_CHANGES.get()
        changes = tuple(pending) if pending else ()
        if pending:
            pending.clear()
        recorder(
            RequestRecordEntry(
                usage=usage,
                provider=provider,
                model=model,
                family=family,
                mode=mode,
                canonical_body=canonical_body,
                request_digest=request_digest,
                usage_event_id=usage_event_id,
                subagent_id=subagent_id,
                created_at=datetime.now(UTC),
                changes=changes,
            )
        )
    except Exception:  # noqa: BLE001 — diagnostics must never break a model call.
        log.warning("llm_request_record.enqueue_failed provider=%s model=%s", provider, model, exc_info=True)
