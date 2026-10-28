"""BowheadState — HOT/WARM/COLD layered state per IntelliVault progressive disclosure.

Sprint η — Hot/Warm/Cold State.

3-layer model (IntelliVault `state.py` port, Bowhead-specific impl):
- HOT (TypedDict, < 5KB always-in-memory): current_user_message, active_hitl,
  pending_actions, recent_context (rolling 5 compressed summaries)
- WARM (PG-backed summaries, 200 tok each): files_summary, tasks_summary,
  findings_summary
- COLD (按需 reload via load_warm_data): canvas sections, full history,
  artifacts (走 session_store / canvas_store / artifact_store)

Layer 1 enforcement: writes to `recent_context` LRU-evict on overflow.
Pinned fields (`current_user_message`, `active_hitl`, `pending_actions`)
overflow → raise `HotStateOverflowError` (anti-metric per PRD §4.3).

Public API:
- ``BowheadState`` TypedDict
- ``push_recent_context(state, entry)`` LRU-safe push
- ``serialize_hot(state) / deserialize_hot(payload)`` JSON round-trip for
  ``turns.hot_state_json``
- ``is_state_lifecycle_enabled()`` η feature flag（env BOWHEAD_HOT_WARM_COLD_ENABLED，
  default ON since η-#9 ship 0.8.4）。曾与 ζ skill 侧的
  skill_metadata.is_progressive_load_enabled 同名不同义（不同 env）——2026-07-03
  改名消歧（docs/skills-tools-ssot.md §5-G7）

η design refs:
- discovery.md §5.3 7-step design draft
- PRD §5.1 BowheadState schema + soft cap behavior
- tech-design.md §1.2 sample code
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


# Soft cap: HOT fields total bytes ≤ 5120. Beyond → LRU evict recent_context;
# pinned fields exceeding alone → raise HotStateOverflowError.
_HOT_CAP_BYTES = 5120
_RECENT_CONTEXT_MAX_ENTRIES = 5
_RECENT_CONTEXT_MAX_CHARS_PER_ENTRY = 800


class HotStateOverflowError(Exception):
    """HOT pinned fields alone exceed cap. Indicates a real bug
    (e.g., HitlState 太大, pending_actions 队列爆) — not graceful degrade.

    Hook protocol: ``on_state_overflow`` 路径 graceful drop oldest pending_action;
    若 drop 后仍超 → re-raise to abort turn (anti-metric).
    """


@dataclass
class HitlState:
    """Cross-turn HITL pause state (subagent triggered HITL waiting on user).

    Mapped to / from ClarificationStore via η-#5 reconstruction.
    """

    clarification_id: str
    kind: str  # "token" | "options" | "task_plan" — per V2.7 HITL extension
    prompt: str
    options: list[str] | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class PendingAction:
    """Write-leaf 队列项 — canvas / report 等 single-write-leaf 操作."""

    action_type: str  # "canvas_add_section" | "add_chart" | etc.
    payload: dict[str, Any]
    submitted_at: str  # ISO 8601


class BowheadState(TypedDict, total=False):
    """3-layer state per IntelliVault progressive disclosure.

    HOT (< 5KB total, soft cap):
    - current_user_message: pinned (raw user prompt, may be PII — see PRD §5.4)
    - active_hitl: pinned (HitlState | None)
    - pending_actions: pinned (write-leaf queue)
    - recent_context: evictable (rolling, max 5 entries < 800 chars each)

    WARM (≤ 200 tok each, on-demand fetch via load_warm_data):
    - files_summary: canvas section / artifact 摘要
    - tasks_summary: 当前 session 任务进度摘要
    - findings_summary: subagent 已返 finding 摘要
    """

    # HOT
    current_user_message: str
    active_hitl: HitlState | None
    pending_actions: list[PendingAction]
    recent_context: list[str]

    # WARM
    files_summary: str
    tasks_summary: str
    findings_summary: str


_HOT_FIELDS = (
    "current_user_message",
    "active_hitl",
    "pending_actions",
    "recent_context",
)
_PINNED_FIELDS = (
    "current_user_message",
    "active_hitl",
    "pending_actions",
)


def _default_state() -> BowheadState:
    """Empty state with defaults — used by hydrate-fail fallback."""
    return BowheadState(
        current_user_message="",
        active_hitl=None,
        pending_actions=[],
        recent_context=[],
        files_summary="",
        tasks_summary="",
        findings_summary="",
    )


def _hot_size_bytes(state: BowheadState) -> int:
    """Estimate HOT fields total serialized size (UTF-8 JSON)."""
    total = 0
    for k in _HOT_FIELDS:
        v = state.get(k)
        if v is None:
            continue
        try:
            total += len(json.dumps(v, default=_default_serializer, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            # Non-serializable field — count repr() bytes as best-effort
            total += len(str(v).encode("utf-8"))
    return total


def _default_serializer(obj: Any) -> Any:
    """Handle dataclasses (HitlState, PendingAction) for JSON serialize."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: getattr(obj, k) for k in obj.__dataclass_fields__}
    raise TypeError(f"Type {type(obj)} not JSON-serializable")


def push_recent_context(state: BowheadState, entry: str) -> None:
    """Push compressed digest entry to recent_context with LRU evict on overflow.

    Behavior (PRD §5.1 + tech-design §1.2):
    1. Truncate entry to <_RECENT_CONTEXT_MAX_CHARS_PER_ENTRY (800).
    2. Append entry; pop oldest if list exceeds 5 entries.
    3. Re-check HOT total bytes; while > 5120, pop oldest recent_context.
    4. If pinned alone > 5120 → raise HotStateOverflowError.
    """
    entry = entry[:_RECENT_CONTEXT_MAX_CHARS_PER_ENTRY]
    rc = state.setdefault("recent_context", [])
    rc.append(entry)
    while len(rc) > _RECENT_CONTEXT_MAX_ENTRIES:
        rc.pop(0)
    # Cap-driven LRU evict
    while _hot_size_bytes(state) > _HOT_CAP_BYTES and rc:
        evicted = rc.pop(0)
        logger.warning(
            "eta.hot_evict reason=cap_overflow evicted_chars=%d remaining=%d",
            len(evicted),
            len(rc),
        )
    if _hot_size_bytes(state) > _HOT_CAP_BYTES:
        raise HotStateOverflowError(f"pinned HOT fields alone exceed {_HOT_CAP_BYTES}B — drop oldest pending_action via on_state_overflow hook")


def serialize_hot(state: BowheadState) -> str:
    """Serialize HOT fields to JSON string for turns.hot_state_json column."""
    hot = {k: state.get(k) for k in _HOT_FIELDS}
    return json.dumps(hot, default=_default_serializer, ensure_ascii=False)


def deserialize_hot(payload: str | None) -> BowheadState:
    """Deserialize JSON payload back to BowheadState. None payload → default state."""
    if not payload:
        return _default_state()
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("eta.deserialize_failed err=%s payload_len=%d", e, len(payload))
        return _default_state()
    state = _default_state()
    if "current_user_message" in raw:
        state["current_user_message"] = str(raw["current_user_message"])
    if raw.get("active_hitl"):
        h = raw["active_hitl"]
        state["active_hitl"] = HitlState(
            clarification_id=h.get("clarification_id", ""),
            kind=h.get("kind", ""),
            prompt=h.get("prompt", ""),
            options=h.get("options"),
            metadata=h.get("metadata"),
        )
    if "pending_actions" in raw:
        state["pending_actions"] = [
            PendingAction(
                action_type=p.get("action_type", ""),
                payload=p.get("payload", {}),
                submitted_at=p.get("submitted_at", ""),
            )
            for p in raw["pending_actions"]
        ]
    if "recent_context" in raw:
        state["recent_context"] = [str(s) for s in raw["recent_context"]]
    return state


# ─── Contextvar for cross-async state propagation ──────────────
# Used to thread BowheadState from turn_runner → subagent without changing
# every intermediate function signature. Set by turn_runner.run_turn at entry,
# read by subagent._legacy_run when explicit state arg is None.
_current_state: contextvars.ContextVar[BowheadState | None] = contextvars.ContextVar(
    "bowhead_state",
    default=None,
)


def get_current_state() -> BowheadState | None:
    """Read current BowheadState from async context. Returns None if not set
    (flag off path or pre-η call site)."""
    return _current_state.get()


def set_current_state(state: BowheadState | None) -> contextvars.Token:
    """Set current state. Caller MUST `_current_state.reset(token)` to clean up."""
    return _current_state.set(state)


def reset_current_state(token: contextvars.Token) -> None:
    """Pair with set_current_state to clean up context on turn end."""
    _current_state.reset(token)


def is_state_lifecycle_enabled() -> bool:
    """η flag — default ON since η-#9 ship 0.8.4.

    2026-07-03 rename（原名 is_progressive_load_enabled 与 ζ skill 侧
    skill_metadata 的同名函数撞名、env 各异，审计误判过 flag 复用——
    docs/skills-tools-ssot.md §5-G7）。env 语义不变。

    Env override:
    - ``BOWHEAD_HOT_WARM_COLD_ENABLED=false`` → OFF (rollback path)
    - ``BOWHEAD_HOT_WARM_COLD_ENABLED=true`` (or unset) → ON

    History: stub default OFF at η-#1; flipped to ON at η-#9 ship.
    """
    raw = os.getenv("BOWHEAD_HOT_WARM_COLD_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "off")  # default ON since η-#9 ship 0.8.4
