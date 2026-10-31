"""M156-M3b · ClarificationStore.

用于 mid-turn HITL（`clarification_ask` 等）: 把待用户回答的候选请求记下来（term_resolver 已于 2026-10-17 删除）:
  - turn_id 维度
  - request_id (uuid) 唯一标识
  - 含 token / candidates / rationale / workspace_id
  - status: pending | resolved | expired

This store holds short-lived pending clarification requests:
  - 一个 turn 可能有多条 (多 token 同时 clarify)
  - A user answer marks the request resolved.
  - 24h 没答 → expired

接口:
  - put(req)        — 写新 pending
  - get_pending(turn_id)  — 列该 turn 所有 pending
  - resolve(request_id, chosen)  — Mark resolved and return the original request
  - expire_old(ttl_hours, exclude_turn_ids)  — 后台清理 (挂起中的定时任务 turn 可豁免)
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import unicodedata
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core.clarification_resume_context import (
    LEGACY_RESUME_CONTEXT_MAX_VERSION,
    RESUME_CONTEXT_VERSION_KEY,
    UnsupportedResumeContext,
    decode_resume_context,
)
from bowhead_core.clarification_resume_context import resume_context_version as resume_context_version

logger = logging.getLogger(__name__)

# ── resume_context snapshot schema version (Agent SDK evaluation A3) ────────────────────
#
# `resume_context` is a free-form dict Lead writes at the sentinel; `turn_service` may read it
# after a release to reconstruct TurnRequest (a ten-minute suspension with a release in between suffices).
# Previously snapshots had no version marker: after a shape change, new code silently reconstructed old rows; missing fields
# became defaults and fields with changed semantics were interpreted under the new meaning, with no detection point.
#
# This is the problem LangGraph RunState versioning addresses, and the sole SDK-evaluation finding
# where they have something we actually lack (see docs/agent-sdk-evaluation-2026-08.md §8 A3).
# Borrow the intent without the dependency: stamp a version at the storage boundary, and let readers decide
# whether to accept that version. A migration framework would be overengineering.
#
# Compatibility policy (deliberately asymmetric):
#   · Old rows without the key → v0, accepted. Adding versioning must not make all existing rows unrestorable.
#   · Malformed or newer versions reject restoration without changing the snapshot or saved answer.
#     Callers must report the unsupported resume instead of silently reconstructing an ordinary request.
RESUME_CONTEXT_SCHEMA_VERSION = LEGACY_RESUME_CONTEXT_MAX_VERSION


def stamp_resume_context(ctx: dict[str, Any] | None) -> dict[str, Any] | None:
    """写侧：盖上当前 schema 版本（已有则不覆盖，调用方可显式指定）。"""
    if ctx is None:
        return None
    if RESUME_CONTEXT_VERSION_KEY in ctx:
        return ctx
    return {**ctx, RESUME_CONTEXT_VERSION_KEY: 2 if "question_round" in ctx else 1}


def resume_context_is_readable(ctx: dict[str, Any] | None) -> bool:
    """Recognize supported snapshots; execution still requires stored authority."""
    try:
        decode_resume_context(ctx)
        return True
    except UnsupportedResumeContext:
        return False


SCHEMA = """
CREATE TABLE IF NOT EXISTS clarification_requests (
    request_id      TEXT PRIMARY KEY,
    turn_id         TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    workspace_id    TEXT NOT NULL,
    token           TEXT NOT NULL,
    rationale       TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved | expired | dismissed (Sprint chat-v4-streaming Phase 4C)
    chosen_value    TEXT,
    chosen_at       TEXT,
    created_at      TEXT NOT NULL,
    -- V2.7 P1/P2: kind=token (V2.4 默认) / options / task_plan
    kind            TEXT NOT NULL DEFAULT 'token',
    multi_select    INTEGER NOT NULL DEFAULT 0,
    chosen_values_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_clarify_turn ON clarification_requests(turn_id, status);
CREATE INDEX IF NOT EXISTS idx_clarify_status ON clarification_requests(status, created_at);
"""

# V2.7: add columns for old schemas (also ignore ALTER failures on old databases).
_MIGRATIONS = [
    "ALTER TABLE clarification_requests ADD COLUMN kind TEXT NOT NULL DEFAULT 'token'",
    "ALTER TABLE clarification_requests ADD COLUMN multi_select INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE clarification_requests ADD COLUMN chosen_values_json TEXT",
    # S5: structured multi-question answers are source truth. They live beside
    # the clarification, not in the protocol audit/effect tables.
    "ALTER TABLE clarification_requests ADD COLUMN answers_json TEXT",
    # M158 #10 P1 (2026-04-29): editable HITL — users can edit task title/description and write a
    # free-form directive. Persist on resolve; turn_runner injects extra_instructions at force_spawn in the next turn.
    # injected_at: turn_runner writes the timestamp after extra_instructions injection, preventing duplicate injection.
    "ALTER TABLE clarification_requests ADD COLUMN user_directive TEXT",
    "ALTER TABLE clarification_requests ADD COLUMN edited_subs_json TEXT",
    "ALTER TABLE clarification_requests ADD COLUMN injected_at TEXT",
    # M158 #10 P1 follow-up (2026-04-29 CTO review): fix session cross-talk. find_recent_resolved
    # previously filtered only user_id+workspace_id, letting a directive resolved in one session enter an unrelated
    # session's next turn. Add session_id + index and make session_id mandatory in find_recent.
    "ALTER TABLE clarification_requests ADD COLUMN session_id TEXT",
    # cancel: users change their minds after resolve. NULL = not cancelled (default); timestamp = cancelled; find_recent skips it.
    "ALTER TABLE clarification_requests ADD COLUMN directive_cancelled_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_clarify_session ON clarification_requests(session_id, status)",
    # Sprint chat-v4-streaming Phase 4C step 1: HITL same-turn-resume support.
    # resume_context_json: Lead transient state snapshot written at sentinel emit
    #   (dispatch_window / response_kind_pre_flight / user_text / parent_turn_id
    #   / session_id / ...). Sprint phase-plan-removal: phase_plan field deleted.
    #   Used by /api/v1/orchestrator/resume_turn to rehydrate Lead at resolve.
    # dismissed_at: ISO timestamp when user clicked "换个问法" → end clarification
    #   without resume. Lead orchestrator marks status=dismissed (vs resolved).
    "ALTER TABLE clarification_requests ADD COLUMN resume_context_json TEXT",
    "ALTER TABLE clarification_requests ADD COLUMN dismissed_at TEXT",
    # connector-context T1#6 (2026-07-15): carry the connector discriminator end to end; fix the break where the sentinel included
    # tool, the first hop dropped it, and resolve hardcoded datarc to restore it (tech-design §2.1).
    # Nullable without default: NULL = old rows / non-connector cards; written only for kind=tool_context_select.
    "ALTER TABLE clarification_requests ADD COLUMN tool TEXT",
]

# M158 #10 P1: single source for length caps. Keep this + Pydantic max_length + frontend maxLength aligned.
MAX_DIRECTIVE_CHARS = 4000
MAX_EDITED_SUB_TITLE_CHARS = 200
MAX_EDITED_SUB_DESC_CHARS = 1000
MAX_EDITED_SUBS_COUNT = 10
RESOLUTION_WORK_LEASE_SECONDS = 300
INVALIDATION_WORK_LEASE_SECONDS = 30

type ClarificationTerminalStatus = Literal["dismissed", "expired"]
type ClarificationInvalidationActor = Literal["user", "system"]
type ClarificationInvalidationReason = Literal[
    "user_rephrase",
    "scheduled_input_superseded",
    "scheduled_input_timeout",
    "scheduled_session_deleted",
    "scheduled_turn_terminated",
    "scheduled_job_deleted",
    "scheduled_run_terminal_repair",
    "clarification_ttl_expired",
]

_INVALIDATION_REASON_SPEC: dict[
    ClarificationInvalidationReason,
    tuple[ClarificationTerminalStatus, ClarificationInvalidationActor],
] = {
    "user_rephrase": ("dismissed", "user"),
    "scheduled_input_superseded": ("dismissed", "system"),
    "scheduled_input_timeout": ("expired", "system"),
    "scheduled_session_deleted": ("dismissed", "system"),
    "scheduled_turn_terminated": ("dismissed", "system"),
    "scheduled_job_deleted": ("dismissed", "system"),
    "scheduled_run_terminal_repair": ("dismissed", "system"),
    "clarification_ttl_expired": ("expired", "system"),
}


class TerminalProtocolClarificationError(RuntimeError):
    """The legacy invalidation writer was asked to mutate a v1 clarification."""


@dataclass(frozen=True)
class ClarificationIdentity:
    request_id: str
    turn_id: str


@dataclass(frozen=True)
class ClarificationInvalidation:
    request_id: str
    turn_id: str
    event_id: str
    target_status: ClarificationTerminalStatus
    reason: str
    actor: ClarificationInvalidationActor
    transitioned: bool = True

    def event_payload(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "status": self.target_status,
            "reason": self.reason,
            "actor": self.actor,
        }


@dataclass(frozen=True)
class ClarificationInvalidationResult:
    request: ClarificationRequest | None
    invalidation: ClarificationInvalidation | None

    @property
    def transitioned(self) -> bool:
        return bool(self.invalidation and self.invalidation.transitioned)


@dataclass(frozen=True)
class ClarificationInvalidationOutboxRow:
    request_id: str
    turn_id: str
    event_id: str
    target_status: ClarificationTerminalStatus
    reason: str
    actor: ClarificationInvalidationActor
    attempt_count: int
    created_at: str

    def event_payload(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "status": self.target_status,
            "reason": self.reason,
            "actor": self.actor,
        }


def clarification_invalidation_event_id(request_id: str) -> str:
    return f"evt_clarification_invalidation_{request_id}"


def record_clarification_invalidation_metrics(
    invalidations: Collection[ClarificationInvalidation],
    *,
    outcome: str,
) -> None:
    """Record finite, content-free invalidation outcomes best-effort."""

    try:
        from bowhead_core import metrics

        for invalidation in invalidations:
            metrics.inc(
                "bowhead_clarification_invalidation_total",
                labels={
                    "reason": invalidation.reason,
                    "target_status": invalidation.target_status,
                    "outcome": outcome,
                },
                help="Legacy clarification invalidation outcomes.",
            )
    except Exception:  # telemetry must not change the state transition  # noqa: S110
        pass


def _validate_invalidation_spec(
    *,
    target_status: ClarificationTerminalStatus,
    reason: ClarificationInvalidationReason,
    actor: ClarificationInvalidationActor,
) -> None:
    expected = _INVALIDATION_REASON_SPEC.get(reason)
    if expected != (target_status, actor):
        raise ValueError(
            "clarification invalidation reason/status/actor combination is invalid",
        )


async def invalidate_pending_in_tx(
    db: aiosqlite.Connection,
    rows: Sequence[ClarificationIdentity],
    *,
    target_status: ClarificationTerminalStatus,
    reason: ClarificationInvalidationReason,
    actor: ClarificationInvalidationActor,
    now: str,
) -> list[ClarificationInvalidation]:
    """CAS legacy pending rows and enqueue their inverse events atomically.

    The caller owns the surrounding transaction and the v1 boundary check.
    This is the only SQL primitive for legacy non-answer terminal transitions;
    plan mid-run's independent ``resolved(skipped=true)`` protocol is excluded.
    """

    _validate_invalidation_spec(
        target_status=target_status,
        reason=reason,
        actor=actor,
    )
    transitioned: list[ClarificationInvalidation] = []
    for identity in rows:
        cursor = await db.execute(
            """
            UPDATE clarification_requests
            SET status=?, dismissed_at=CASE WHEN ?='dismissed' THEN ? ELSE dismissed_at END
            WHERE request_id=? AND turn_id=? AND status='pending'
            RETURNING request_id,turn_id
            """,
            (
                target_status,
                target_status,
                now,
                identity.request_id,
                identity.turn_id,
            ),
        )
        updated = await cursor.fetchone()
        if updated is None:
            continue
        event_id = clarification_invalidation_event_id(identity.request_id)
        await db.execute(
            """
            INSERT INTO clarification_invalidation_outbox(
              request_id,turn_id,event_id,target_status,reason_code,actor_kind,
              state,lease_owner,lease_expires_at,attempt_count,created_at,
              updated_at,delivered_at
            ) VALUES (?,?,?,?,?,?,'pending',NULL,NULL,0,?,?,NULL)
            """,
            (
                identity.request_id,
                identity.turn_id,
                event_id,
                target_status,
                reason,
                actor,
                now,
                now,
            ),
        )
        transitioned.append(
            ClarificationInvalidation(
                request_id=identity.request_id,
                turn_id=identity.turn_id,
                event_id=event_id,
                target_status=target_status,
                reason=reason,
                actor=actor,
            ),
        )
    return transitioned


async def _has_protocol_projection_tables(
    db: aiosqlite.Connection,
) -> bool:
    async with db.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='table' AND name IN ('turns','turn_terminal_receipts')
        """
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row is not None and int(row[0]) == 2)


def normalize_clarification_resolution(
    *,
    kind: str,
    chosen_value: str | None = None,
    chosen_values: list[str] | None = None,
    user_directive: str | None = None,
    edited_subs: list[dict[str, Any]] | None = None,
    answers: list[dict[str, Any]] | None = None,
) -> dict[str, object | None]:
    """Validate and serialize the caller-owned resolution fields.

    Terminal Protocol v1 consumes the clarification row in the protocol
    transaction, so the route cannot call ``ClarificationStore.resolve`` first.
    Keeping normalization here ensures the legacy and v1 writers persist the
    same source projection.
    """

    values = chosen_values
    if values is None and chosen_value is not None:
        values = [chosen_value]
    values = list(values or [])
    primary = values[0] if values else ""
    normalized_directive = _sanitize_directive(user_directive, kind=kind) if user_directive is not None else None
    return {
        "chosen_value": primary,
        "chosen_values_json": json.dumps(values, ensure_ascii=False),
        "user_directive": normalized_directive,
        "edited_subs_json": (json.dumps(edited_subs, ensure_ascii=False) if edited_subs else None),
        "answers_json": (json.dumps(answers, ensure_ascii=False) if answers else None),
    }


# Allowed clarification kinds. put() validates incoming kind; unknown raises ValueError.
# Add new kinds here first and synchronize the frontend ClarificationKind type.
#   token              — V2.4: single-choice dimension/measure candidates
#   options            — V2.7 P1: multi-choice bullets (e.g. aspen's five analysis directions)
#                          B5 (bi-hitl-cleanup): default kind emitted by the new schema; type goes into attrs
#   task_plan          — V2.7 P2: multi-choice + ordered subtasks
#   business_rule_review — DEPRECATED (2026-10-16): business rules were retired; old rows stay readable
#   ambiguous_turn     — P4.E: extremely vague user input (no metric/time), HITL ladder
#   formula_confirm    — δ-#3: Confidence Gate < 7 → confirm formula
#
# DEPRECATED kinds (bi-hitl-cleanup B3, 2026-05): after removing Shape-2 emission in B3, no writers
# remain; old rows stay readable (clean up after 30 days in Lane C). Do not use in new code. Marked kinds: `concept_resolution`
# / `metric_formula` — see `_DEPRECATED_CLARIFICATION_KINDS` below.
#
# Note (P0-CTO-3): `ambiguous_turn` is not deprecated; it is an active V4-renderable
# kind supporting LLM-decided HITL alongside `options`.
VALID_CLARIFICATION_KINDS: frozenset[str] = frozenset(
    {
        "token",
        "options",
        "task_plan",
        "ambiguous_turn",
        "formula_confirm",
        # Batch 2 (workspace-space-split PRD §3.3b): Datarc workspace selection; candidates carry
        # (value=workspace ID, label=display name). Resolve writes the user's tool_context
        # preference using value; resume does not append the answer to user_text.
        "tool_context_select",
        # DEPRECATED post-B3 (bi-hitl-cleanup, 2026-05): no writers remain; old rows stay readable.
        "concept_resolution",
        "metric_formula",
        # DEPRECATED 2026-10-16: business rules were retired; old rows stay readable and resolve without effects.
        "business_rule_review",
    }
)

# bi-hitl-cleanup P5.1 (2026-05-21): deprecated tier — written by ar2 Shape-2
# path removed in B3. Writes still validate as legal (compatibility with old-row reads),
# but each write emits a WARN log so dashboards / post-B3 audits can catch
# leftover callers. Post-B3 prod logs SHOULD have 0 WARN lines for these.
_DEPRECATED_CLARIFICATION_KINDS: frozenset[str] = frozenset(
    {
        "concept_resolution",
        "metric_formula",
        "business_rule_review",
    }
)


def _warn_if_deprecated_kind(kind: str) -> None:
    """Emit a soft warning when writing a deprecated kind. Post-B3 these should
    be 0 emits — if this warns, B3 砍 sentinel auto-emit wasn't complete or a
    new caller path leaked in."""
    if kind in _DEPRECATED_CLARIFICATION_KINDS:
        logger.warning(
            "clarification_store.deprecated_kind_write kind=%s — expected 0 post-B3 (bi-hitl-cleanup); audit caller",
            kind,
        )


# CTO review #14 + δ-#8.5 Sec P0-2/P1-7: prompt-injection blocklist V2.
# Directives enter the LLM system prompt; matches reject persistence (raise ValueError → gateway 422 + metric).
# The blocklist is not universal, but blocks 80% of casual injections; targeted attacks require LLM-side + provider-layer defense.
#
# δ-#8.5 hardening: before checking, NFKC-normalize, strip Cf/Cc/Co (zero-width / NBSP / Cyrillic
# look-alikes), and collapse whitespace. Prevent bypass with "i g n o r e" / "іgnore" / zero-width insertion,
# etc. Patterns are all lowercase for comparison.
_INJECTION_BLOCKLIST = (
    # English jailbreak prefixes
    "ignore previous instructions",
    "ignore previous",
    "ignore the above",
    "ignore all prior",
    "disregard previous",
    "disregard the above",
    "forget previous",
    "forget the above",
    "<|im_start|>",
    "<|im_end|>",
    "<|system|>",
    "<|/system|>",
    "system:",
    "##system",
    "### system",
    "role: system",
    "<system>",
    "</system>",
    "begin system prompt",
    "new instructions:",
    "you are now",
    "act as if",
    "pretend you are",
    # Chinese jailbreaks — phrases must be jailbreak-specific, not common
    # filler words. δ-#8.5 round-2 review: dropped "作为" / "扮演" / "假设你",
    # normal business prose ("EBITDA 作为衡量指标..." / "客户扮演关键
    # 角色..." / "假设你已经看过 Q3 数据") that would silently kill auto-learn
    # on Chinese inputs — exactly δ's dogfood case.
    "新的角色",
    "新指令",
    "忽略之前",
    "忘记之前",
    "你现在是",
    "你扮演 admin",
    "扮演 system",
    "假设你是 root",
    "假设你是 admin",
)


# Cf/Cc/Co Unicode categories — control / format / private use chars that often
# appear in invisible-bypass payloads (zero-width space, RTL marker, BOM).
_INVISIBLE_CATEGORIES = ("Cf", "Cc", "Co")

# Compact "fingerprint" forms of blocklist phrases — derived from
# _INJECTION_BLOCKLIST + _FORMULA_INJECTION_PATTERNS at module load. Match
# against the no-whitespace normalized form to catch zero-width bypass
# ("ignore\u200bprevious" → "ignoreprevious") and spaced bypass
# ("i g n o r e p r e v i o u s" → "ignoreprevious"). Only phrases ≥10 chars
# get a fingerprint to avoid false positives on common substrings like "system".
_FINGERPRINT_MIN_CHARS = 10


def _fingerprint(phrase: str) -> str:
    """Compact form: NFKC + lowercase + strip invisibles + remove all whitespace."""
    nfkc = unicodedata.normalize("NFKC", phrase).lower()
    stripped = "".join(ch for ch in nfkc if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    return re.sub(r"\s+", "", stripped)


def _normalize_for_blocklist(text: str) -> str:
    """NFKC normalize + strip invisibles + collapse whitespace + lowercase.

    Bypass examples this defeats:
    - "іgnore previous" (Cyrillic і U+0456) → NFKC normalizes look-alikes
    - "ignore\u200bprevious" (ZWSP) → strip Cf (handled in fingerprint form)
    - "i g n o r e   previous" (spaced) → fingerprint form has no whitespace
    - "Ignore PrEvIoUs" → lowercase
    """
    nfkc = unicodedata.normalize("NFKC", text)
    stripped = "".join(ch for ch in nfkc if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    collapsed = re.sub(r"\s+", " ", stripped)
    return collapsed.lower()


# δ-#3 (round 2 SEC-P0-1): formula_confirm passes free-form formulas into LLM context.
# More dangerous than token directives: formula text persists as fact and enters subsequent turns' system prompts.
# This layer blocks code-execution / XSS / data-exfiltration starters, complementing _INJECTION_BLOCKLIST.
_FORMULA_INJECTION_PATTERNS = (
    "__import__",
    "exec(",
    "eval(",
    "os.system",
    "subprocess",
    "open(",
    "compile(",
    "<script",
    "javascript:",
    "data:text/html",
)


def _check_blocklist(text: str, patterns: tuple[str, ...]) -> str | None:
    """Return matched phrase or None.

    Two-layer check:
    1. Normalized-with-spaces form vs original phrases (catches normal spelling)
    2. Fingerprint form (no whitespace) vs fingerprinted long phrases — catches
       zero-width / NBSP / spaced-out bypass for phrases ≥10 chars
    """
    normalized = _normalize_for_blocklist(text)
    for phrase in patterns:
        if phrase in normalized:
            return phrase
    fp_text = _fingerprint(text)
    for phrase in patterns:
        if len(phrase) < _FINGERPRINT_MIN_CHARS:
            continue
        fp_phrase = _fingerprint(phrase)
        if fp_phrase and fp_phrase in fp_text:
            return phrase
    return None


def _sanitize_directive(raw: str, *, kind: str = "token") -> str | None:
    """剔控制字符, 截 MAX_DIRECTIVE_CHARS, 走 injection 黑名单, 空 → None.

    被黑名单挡的 raise ValueError (调用方应转 422), frontend 提示而不是静默吞.

    δ-#3: kind=="formula_confirm" 加 _FORMULA_INJECTION_PATTERNS 二次过滤
    (公式文本写 fact 持久化 + 注 prompt, 比普通 directive 风险更高).

    δ-#8.5 Sec P0-2: blocklist 走两层 (with-space normalized + no-space
    fingerprint) — 防 Unicode 隐形字符 / look-alike / 拆分空格 bypass.
    """
    cleaned = "".join(ch for ch in raw if ch in ("\n", "\r", "\t") or ord(ch) >= 0x20)[:MAX_DIRECTIVE_CHARS].strip()
    if not cleaned:
        return None
    matched = _check_blocklist(cleaned, _INJECTION_BLOCKLIST)
    if matched is not None:
        raise ValueError(f"directive contains blocked phrase {matched!r} (prompt-injection guard)")
    if kind == "formula_confirm":
        f_matched = _check_blocklist(cleaned, _FORMULA_INJECTION_PATTERNS)
        if f_matched is not None:
            logger.warning("delta.formula_injection_blocked pattern=%s", f_matched)
            raise ValueError(f"formula contains blocked pattern {f_matched!r} (code-execution guard)")
    return cleaned


def _now() -> str:
    # M158 #10 P1: use microseconds to prevent multiple resolves in one turn colliding within one second, making ORDER BY chosen_at
    # unstable (observed in smoke tests). Existing timespec=seconds timestamps remain compatible on reads (lexical
    # ordering remains correct: "2026-04-29T08:00:00+00:00" < "2026-04-29T08:00:00.000001+00:00").
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")


@dataclass
class ClarificationCandidate:
    value: str
    label: str
    hint: str = ""
    confidence: str = ""
    kind: str = "measure"  # P1 #12: "measure"|"dimension"|"dim_value"


@dataclass
class ClarificationRequest:
    request_id: str
    turn_id: str
    user_id: str
    workspace_id: str
    token: str
    rationale: str
    candidates: list[ClarificationCandidate]
    status: str = "pending"
    chosen_value: str | None = None  # Legacy (single-choice token kind)
    chosen_values: list[str] | None = None  # V2.7 P1/P2 (multi-choice options/task_plan)
    answers: list[dict[str, Any]] | None = None
    chosen_at: str | None = None
    created_at: str = ""
    # V2.7: kind determines frontend UI rendering + backend processing path.
    # - "token" (V2.4): single-choice dimension/measure candidates
    # - "options" (V2.7 P1): multi-choice bullets (e.g. aspen's five analysis directions)
    # - "task_plan" (V2.7 P2): multi-choice + ordered subtasks
    kind: str = "token"
    multi_select: bool = False  # True for options/task_plan
    # M158 #10 P1: editable HITL. Users write back on resolve; at force_spawn in the next turn, turn_runner
    # finds status=resolved + injected_at IS NULL within session_id and injects extra_instructions.
    user_directive: str | None = None  # Free-form user directive, ≤4000 chars
    edited_subs: list[dict[str, Any]] | None = None  # [{title, description}, ...]
    injected_at: str | None = None  # turn_runner writes the ISO timestamp after injection.
    # P1 follow-up (CTO review #1, #5): session cross-talk protection + user cancellation
    session_id: str | None = None  # Must share the turn_id's session; find_recent filters by this.
    directive_cancelled_at: str | None = None  # ISO timestamp when the user cancelled the directive after resolve
    # Sprint chat-v4-streaming Phase 4C: HITL same-turn-resume.
    # resume_context: Lead transient state snapshot, written at sentinel emit by
    #   lead_orchestrator. Contains phase_plan / dispatch_window / response_kind /
    #   user_text. /api/v1/orchestrator/resume_turn reads this to rehydrate Lead.
    # dismissed_at: ISO timestamp when user clicked "换个问法" — turn ends with
    #   status=clarification_dismissed (no resume).
    resume_context: dict[str, Any] | None = None
    dismissed_at: str | None = None
    # connector-context T1#6: connector instance ID (e.g. "datarc"/"pbi"). Written only for
    # kind=tool_context_select; None = old row / non-connector card. Resolve
    # (routes/clarifications.py) reads it to select commit_connector_context_choice's tool,
    # replacing hardcoding. Semantics: SSOT docs/tool-context-framework.md v2.
    tool: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """SSE event payload 形式."""
        return {
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "workspace_id": self.workspace_id,
            "token": self.token,
            "rationale": self.rationale,
            "kind": self.kind,
            "multi_select": self.multi_select,
            "candidates": [{"value": c.value, "label": c.label, "hint": c.hint, "confidence": c.confidence, "kind": c.kind} for c in self.candidates],
        }


class ClarificationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            # V2.7: migrate old databases (ignore inability to add new columns).
            for stmt in _MIGRATIONS:
                with contextlib.suppress(aiosqlite.OperationalError):
                    await db.execute(stmt)
            await db.commit()

    async def invalidation_schema_ready(self) -> bool:
        """Return whether Alembic installed the complete 0017 contract.

        Store initialization intentionally does not create post-1.0 schema.
        Gateway startup calls this after migrations and fails closed when the
        outbox is absent or only partially installed.
        """

        required_columns = {
            "request_id",
            "turn_id",
            "event_id",
            "target_status",
            "reason_code",
            "actor_kind",
            "state",
            "lease_owner",
            "lease_expires_at",
            "attempt_count",
            "created_at",
            "updated_at",
            "delivered_at",
        }
        required_indexes = {
            "idx_clarification_invalidation_outbox_pending",
            "idx_clarification_invalidation_outbox_turn_claim",
        }
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "PRAGMA table_info(clarification_invalidation_outbox)",
            ) as cursor:
                columns = {str(row[1]) for row in await cursor.fetchall()}
            if not required_columns <= columns:
                return False
            async with db.execute(
                "PRAGMA index_list(clarification_invalidation_outbox)",
            ) as cursor:
                indexes = {str(row[1]) for row in await cursor.fetchall()}
        return required_indexes <= indexes

    async def require_invalidation_schema(self) -> None:
        if not await self.invalidation_schema_ready():
            raise RuntimeError(
                "clarification invalidation schema is unavailable; run core migration 0017 before gateway startup",
            )

    @staticmethod
    async def _is_v1_turn(
        db: aiosqlite.Connection,
        turn_id: str,
    ) -> bool:
        if not await _has_protocol_projection_tables(db):
            return False
        async with db.execute(
            "SELECT terminal_protocol_version FROM turns WHERE id=?",
            (turn_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return bool(row is not None and int(row[0] or 0) == 1)

    @staticmethod
    def _outbox_row(row: aiosqlite.Row) -> ClarificationInvalidationOutboxRow:
        target_status = str(row["target_status"])
        actor = str(row["actor_kind"])
        if target_status not in {"dismissed", "expired"}:
            raise RuntimeError("clarification invalidation outbox has invalid status")
        if actor not in {"user", "system"}:
            raise RuntimeError("clarification invalidation outbox has invalid actor")
        return ClarificationInvalidationOutboxRow(
            request_id=str(row["request_id"]),
            turn_id=str(row["turn_id"]),
            event_id=str(row["event_id"]),
            target_status=target_status,  # type: ignore[arg-type]
            reason=str(row["reason_code"]),
            actor=actor,  # type: ignore[arg-type]
            attempt_count=int(row["attempt_count"]),
            created_at=str(row["created_at"]),
        )

    async def _canonical_invalidation_in_tx(
        self,
        db: aiosqlite.Connection,
        request: ClarificationRequest,
    ) -> ClarificationInvalidation | None:
        async with db.execute(
            "SELECT * FROM clarification_invalidation_outbox WHERE request_id=?",
            (request.request_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            outbox = self._outbox_row(row)
            return ClarificationInvalidation(
                request_id=outbox.request_id,
                turn_id=outbox.turn_id,
                event_id=outbox.event_id,
                target_status=outbox.target_status,
                reason=outbox.reason,
                actor=outbox.actor,
                transitioned=False,
            )

        event_id = clarification_invalidation_event_id(request.request_id)
        async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_events'",
        ) as cursor:
            has_events = await cursor.fetchone()
        if has_events is not None:
            async with db.execute(
                "SELECT payload_json FROM turn_events WHERE id=? AND event_type='clarification.dismissed'",
                (event_id,),
            ) as cursor:
                event_row = await cursor.fetchone()
            if event_row is not None:
                try:
                    payload = json.loads(str(event_row[0]))
                except json.JSONDecodeError:
                    payload = {}
                status = str(payload.get("status") or request.status)
                actor = str(payload.get("actor") or "system")
                if status in {"dismissed", "expired"} and actor in {
                    "user",
                    "system",
                }:
                    return ClarificationInvalidation(
                        request_id=request.request_id,
                        turn_id=request.turn_id,
                        event_id=event_id,
                        target_status=status,  # type: ignore[arg-type]
                        reason=str(payload.get("reason") or "reason_unknown"),
                        actor=actor,  # type: ignore[arg-type]
                        transitioned=False,
                    )

        if request.status not in {"dismissed", "expired"}:
            return None
        return ClarificationInvalidation(
            request_id=request.request_id,
            turn_id=request.turn_id,
            event_id=event_id,
            target_status=request.status,  # type: ignore[arg-type]
            reason="reason_unknown",
            actor="system",
            transitioned=False,
        )

    async def invalidate_one(
        self,
        request_id: str,
        *,
        target_status: ClarificationTerminalStatus,
        reason: ClarificationInvalidationReason,
        actor: ClarificationInvalidationActor,
    ) -> ClarificationInvalidationResult:
        """Atomically invalidate one legacy pending request and enqueue delivery."""

        _validate_invalidation_spec(
            target_status=target_status,
            reason=reason,
            actor=actor,
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM clarification_requests WHERE request_id=?",
                (request_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute("ROLLBACK")
                return ClarificationInvalidationResult(None, None)
            request = self._row_to_request(row)
            if await self._is_v1_turn(db, request.turn_id):
                await db.execute("ROLLBACK")
                raise TerminalProtocolClarificationError(request_id)
            if request.status != "pending":
                canonical = await self._canonical_invalidation_in_tx(db, request)
                await db.execute("ROLLBACK")
                if canonical is not None:
                    record_clarification_invalidation_metrics(
                        [canonical],
                        outcome="already_terminal",
                    )
                return ClarificationInvalidationResult(request, canonical)
            invalidations = await invalidate_pending_in_tx(
                db,
                [ClarificationIdentity(request_id, request.turn_id)],
                target_status=target_status,
                reason=reason,
                actor=actor,
                now=_now(),
            )
            await db.commit()
        record_clarification_invalidation_metrics(
            invalidations,
            outcome="transitioned",
        )
        updated = await self.get(request_id)
        return ClarificationInvalidationResult(
            updated,
            invalidations[0] if invalidations else None,
        )

    async def invalidate_pending_for_turn(
        self,
        turn_id: str,
        *,
        target_status: ClarificationTerminalStatus,
        reason: ClarificationInvalidationReason,
        actor: ClarificationInvalidationActor,
    ) -> list[ClarificationInvalidation]:
        """Invalidate all currently pending legacy requests for a settled Turn."""

        _validate_invalidation_spec(
            target_status=target_status,
            reason=reason,
            actor=actor,
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if await self._is_v1_turn(db, turn_id):
                await db.execute("ROLLBACK")
                raise TerminalProtocolClarificationError(turn_id)
            async with db.execute(
                "SELECT request_id,turn_id FROM clarification_requests WHERE turn_id=? AND status='pending' ORDER BY created_at,request_id",
                (turn_id,),
            ) as cursor:
                rows = [
                    ClarificationIdentity(
                        request_id=str(row["request_id"]),
                        turn_id=str(row["turn_id"]),
                    )
                    for row in await cursor.fetchall()
                ]
            invalidations = await invalidate_pending_in_tx(
                db,
                rows,
                target_status=target_status,
                reason=reason,
                actor=actor,
                now=_now(),
            )
            await db.commit()
        record_clarification_invalidation_metrics(
            invalidations,
            outcome="transitioned",
        )
        return invalidations

    async def repair_terminal_invalidation(
        self,
        request_id: str,
        *,
        target_status: ClarificationTerminalStatus,
        reason: ClarificationInvalidationReason,
        actor: ClarificationInvalidationActor = "system",
    ) -> ClarificationInvalidationResult:
        """Attach an intent to a proven legacy terminal row from an old release.

        This never changes a resolved row and is intentionally separate from
        the normal CAS primitive.  Callers may use it only after durable
        scheduled-run evidence proves why the old row is no longer actionable.
        """

        _validate_invalidation_spec(
            target_status=target_status,
            reason=reason,
            actor=actor,
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM clarification_requests WHERE request_id=?",
                (request_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute("ROLLBACK")
                return ClarificationInvalidationResult(None, None)
            request = self._row_to_request(row)
            if await self._is_v1_turn(db, request.turn_id):
                await db.execute("ROLLBACK")
                raise TerminalProtocolClarificationError(request_id)
            canonical = await self._canonical_invalidation_in_tx(db, request)
            if canonical is not None and canonical.reason != "reason_unknown":
                await db.execute("ROLLBACK")
                return ClarificationInvalidationResult(request, canonical)
            if request.status == "resolved":
                await db.execute("ROLLBACK")
                return ClarificationInvalidationResult(request, None)
            if request.status in {"dismissed", "expired"} and request.status != target_status:
                # A historical terminal row is already the state authority.
                # Never manufacture an inverse event whose status contradicts
                # that row merely because a later lifecycle observer inferred
                # a different reason.
                await db.execute("ROLLBACK")
                return ClarificationInvalidationResult(request, canonical)
            now = _now()
            if request.status == "pending":
                invalidations = await invalidate_pending_in_tx(
                    db,
                    [ClarificationIdentity(request.request_id, request.turn_id)],
                    target_status=target_status,
                    reason=reason,
                    actor=actor,
                    now=now,
                )
                invalidation = invalidations[0] if invalidations else None
            else:
                event_id = clarification_invalidation_event_id(request.request_id)
                await db.execute(
                    """
                    INSERT OR IGNORE INTO clarification_invalidation_outbox(
                      request_id,turn_id,event_id,target_status,reason_code,
                      actor_kind,state,lease_owner,lease_expires_at,attempt_count,
                      created_at,updated_at,delivered_at
                    ) VALUES (?,?,?,?,?,?,'pending',NULL,NULL,0,?,?,NULL)
                    """,
                    (
                        request.request_id,
                        request.turn_id,
                        event_id,
                        target_status,
                        reason,
                        actor,
                        now,
                        now,
                    ),
                )
                invalidation = ClarificationInvalidation(
                    request_id=request.request_id,
                    turn_id=request.turn_id,
                    event_id=event_id,
                    target_status=target_status,
                    reason=reason,
                    actor=actor,
                )
            await db.commit()
        if invalidation is not None:
            record_clarification_invalidation_metrics(
                [invalidation],
                outcome=("transitioned" if request.status == "pending" else "repaired"),
            )
        return ClarificationInvalidationResult(await self.get(request_id), invalidation)

    async def put(
        self,
        *,
        turn_id: str,
        user_id: str,
        workspace_id: str,
        token: str,
        rationale: str,
        candidates: list[ClarificationCandidate],
        kind: str = "token",  # See VALID_CLARIFICATION_KINDS.
        multi_select: bool = False,
        session_id: str | None = None,  # P1 follow-up: prevent directives crossing sessions.
        resume_context: dict[str, Any] | None = None,  # 0.26.2 — atomic write
        tool: str | None = None,  # connector-context T1#6: pass only for tool_context_select.
        connection: aiosqlite.Connection | None = None,
        bounded_question: dict[str, Any] | None = None,
        ordinary_question: Any = None,
    ) -> ClarificationRequest:
        """Insert clarification request.

        0.26.2 fix — `resume_context` is now written **atomically** with the
        INSERT (single transaction). Prior bug: callers did `put()` then
        `set_resume_context()` as 2 separate writes, and any silent-fail in
        the second left rows with `resume_context_json=NULL` → /resolve fell
        through to action="noop" → FE chat-send → visual duplicate turns.
        Verified empty on all 7 prod ambiguous_turn rows (s_e5f180688e54).
        """
        if kind not in VALID_CLARIFICATION_KINDS:
            raise ValueError(f"unknown clarification kind {kind!r}; valid: {sorted(VALID_CLARIFICATION_KINDS)}")
        if resume_context is not None and "question_round" in resume_context:
            raise ValueError("clarification_round_context_is_server_owned")
        if resume_context is not None and (
            resume_context.get("__schema_version") == 4
            or (isinstance(resume_context.get("continuation"), dict) and resume_context["continuation"].get("kind") == "ordinary_lead")
        ):
            raise ValueError("ordinary_question_context_is_server_owned")
        # bi-hitl-cleanup P5.1: soft WARN on deprecated kind write (post-B3
        # writers expected to be zero; if this fires, B3 cleanup missed a caller).
        _warn_if_deprecated_kind(kind)
        req = ClarificationRequest(
            request_id=str(uuid4()),
            turn_id=turn_id,
            user_id=user_id,
            workspace_id=workspace_id,
            token=token,
            rationale=rationale,
            candidates=list(candidates),
            created_at=_now(),
            kind=kind,
            multi_select=multi_select,
            session_id=session_id,
            tool=tool,
        )
        cands_json = json.dumps(
            [{"value": c.value, "label": c.label, "hint": c.hint, "confidence": c.confidence, "kind": c.kind} for c in candidates],
            ensure_ascii=False,
        )
        # A3: stamp the version at the storage boundary, not each write site. Four sites currently write resume_context
        # (clarification_tool / plan_reapprove / plan_midrun / heuristic); patching them individually
        # will inevitably miss a fifth.
        _rc_stamped = stamp_resume_context(resume_context)
        rc_json: str | None = json.dumps(_rc_stamped, ensure_ascii=False, default=str) if _rc_stamped is not None else None

        async def insert(db: aiosqlite.Connection) -> None:
            inserted_context = _rc_stamped
            inserted_json = rc_json
            if bounded_question is not None:
                from bowhead_core.clarification_rounds import CONTEXT_KEY, QUESTIONS_KEY, next_round

                if kind != "ambiguous_turn" or tool or token or not isinstance(resume_context, dict):
                    raise ValueError("clarification_round_contract_invalid")
                marker = await next_round(
                    db,
                    turn_id=turn_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    **bounded_question,
                )
                card_questions = [q for q in bounded_question.get("questions") or [] if isinstance(q, dict)]
                inserted_context = {
                    **resume_context,
                    CONTEXT_KEY: marker,
                    RESUME_CONTEXT_VERSION_KEY: 2,
                    QUESTIONS_KEY: card_questions,
                }
                inserted_json = json.dumps(inserted_context, ensure_ascii=False, allow_nan=False)
            if ordinary_question is not None:
                from bowhead_core.ordinary_lead_continuation import bind_ordinary_capture_in_tx

                if (
                    bounded_question is not None
                    or resume_context is not None
                    or kind != "ambiguous_turn"
                    or token
                    or tool
                    or multi_select
                    or candidates
                ):
                    raise ValueError("ordinary_question_contract_invalid")
                inserted_context = await bind_ordinary_capture_in_tx(db, card=req, capture=ordinary_question)
                inserted_json = json.dumps(inserted_context, ensure_ascii=False, allow_nan=False)
            if isinstance(inserted_context, dict) and inserted_context.get("datarc_selection") is not None:
                from bowhead_core.datarc_selection import DatarcSelectionSnapshot, require_datarc_context_migration

                DatarcSelectionSnapshot.model_validate(inserted_context["datarc_selection"])
                await require_datarc_context_migration(db)
            await db.execute(
                """
                INSERT INTO clarification_requests
                  (request_id, turn_id, user_id, workspace_id, token, rationale,
                   candidates_json, status, created_at, kind, multi_select,
                   session_id, resume_context_json, tool)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    req.request_id,
                    turn_id,
                    user_id,
                    workspace_id,
                    token,
                    rationale,
                    cands_json,
                    req.created_at,
                    kind,
                    1 if multi_select else 0,
                    session_id,
                    inserted_json,
                    tool,
                ),
            )
            req.resume_context = inserted_context

        if connection is None:
            async with aiosqlite.connect(self.db_path) as db:
                if bounded_question is not None or ordinary_question is not None:
                    await db.execute("BEGIN IMMEDIATE")
                await insert(db)
                await db.commit()
        else:
            if not connection.in_transaction:
                raise ValueError("clarification_shared_transaction_required")
            await insert(connection)
        return req

    async def active_for_session(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[ClarificationRequest]:
        """Sprint η P0-#2 fix: pre-η HITL hydrate fallback.

        Returns pending ClarificationRequest entries for a session. Used by
        ``state_lifecycle.DefaultStateLifecycle.on_turn_start`` to reconstruct
        ``HitlState`` when ``hot_state_json`` not present (pre-η session paused
        on HITL).

        η-#7.5 P0-1 fix: query correct table name `clarification_requests`
        (was wrongly `clarifications`).
        η-#7.5 P1-5 fix: optional ``user_id`` / ``workspace_id`` for cross-session
        leak防护 (callers SHOULD pass these from authenticated context).
        """
        params: list[Any] = [session_id]
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            protocol_tables = await _has_protocol_projection_tables(db)
            sql = (
                """
                SELECT c.* FROM clarification_requests c
                LEFT JOIN turns t ON t.id=c.turn_id
                LEFT JOIN turn_terminal_receipts r
                  ON r.receipt_id=t.current_terminal_receipt_id
                WHERE c.session_id = ? AND c.status = 'pending'
                  AND (
                    COALESCE(t.terminal_protocol_version,0)<>1
                    OR r.status='clarification_pending'
                  )
                """
                if protocol_tables
                else """
                SELECT c.* FROM clarification_requests c
                WHERE c.session_id = ? AND c.status = 'pending'
                """
            )
            if user_id is not None:
                sql += " AND c.user_id = ?"
                params.append(user_id)
            if workspace_id is not None:
                sql += " AND c.workspace_id = ?"
                params.append(workspace_id)
            sql += " ORDER BY c.created_at ASC"
            cur = await db.execute(sql, tuple(params))
            rows = await cur.fetchall()
        return [self._row_to_request(row) for row in rows]

    async def get_pending_for_turn(self, turn_id: str) -> list[ClarificationRequest]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            protocol_tables = await _has_protocol_projection_tables(db)
            sql = (
                """
                SELECT c.* FROM clarification_requests c
                LEFT JOIN turns t ON t.id=c.turn_id
                LEFT JOIN turn_terminal_receipts r
                  ON r.receipt_id=t.current_terminal_receipt_id
                WHERE c.turn_id = ? AND c.status = 'pending'
                  AND (
                    COALESCE(t.terminal_protocol_version,0)<>1
                    OR r.status='clarification_pending'
                  )
                ORDER BY c.created_at ASC
                """
                if protocol_tables
                else """
                SELECT c.* FROM clarification_requests c
                WHERE c.turn_id = ? AND c.status = 'pending'
                ORDER BY c.created_at ASC
                """
            )
            cur = await db.execute(
                sql,
                (turn_id,),
            )
            rows = await cur.fetchall()
            return [self._row_to_request(r) for r in rows]

    async def get(self, request_id: str) -> ClarificationRequest | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM clarification_requests WHERE request_id = ?",
                (request_id,),
            )
            row = await cur.fetchone()
            return self._row_to_request(row) if row else None

    async def resolve(
        self,
        request_id: str,
        chosen_value: str | None = None,
        *,
        chosen_values: list[str] | None = None,
        user_directive: str | None = None,
        edited_subs: list[dict[str, Any]] | None = None,
    ) -> ClarificationRequest | None:
        req, _transitioned = await self.resolve_once(
            request_id,
            chosen_value,
            chosen_values=chosen_values,
            user_directive=user_directive,
            edited_subs=edited_subs,
        )
        return req

    async def resolve_once(
        self,
        request_id: str,
        chosen_value: str | None = None,
        *,
        chosen_values: list[str] | None = None,
        user_directive: str | None = None,
        edited_subs: list[dict[str, Any]] | None = None,
        answers: list[dict[str, Any]] | None = None,
        resolution_channel: str | None = None,
        resolution_work: dict[str, Any] | None = None,
        resolution_owner: str | None = None,
    ) -> tuple[ClarificationRequest | None, bool]:
        """Mark resolved and return the original request for follow-up effects.

        V2.7: 兼容 token kind 的 chosen_value (str) + options/task_plan kind 的
        chosen_values (list). 至少传一个; 都传以 chosen_values 优先.

        M158 #10 P1: task_plan kind 可附 user_directive + edited_subs (用户在 HITL UI
        改后的 plan + free-form 指令). 持久化, turn_runner 下一轮 force_spawn 读 + 注入.
        """
        # Normalize: prefer chosen_values; otherwise wrap chosen_value in a list.
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT * FROM clarification_requests WHERE request_id = ?",
                (request_id,),
            )
            row = await cur.fetchone()
            if not row:
                await db.execute("ROLLBACK")
                return None, False
            if row["status"] != "pending":
                await db.execute("ROLLBACK")
                return self._row_to_request(row), False
            # P1 sanitization: cap directive length at MAX_DIRECTIVE_CHARS, strip control characters, and
            # hard-block CTO review #14 prompt injection (blocklisted phrases).
            # δ-#3: formula_confirm additionally passes _FORMULA_INJECTION_PATTERNS.
            row_keys = row.keys() if hasattr(row, "keys") else []
            req_kind = row["kind"] if "kind" in row_keys else "token"
            normalized = normalize_clarification_resolution(
                kind=req_kind,
                chosen_value=chosen_value,
                chosen_values=chosen_values,
                user_directive=user_directive,
                edited_subs=edited_subs,
                answers=answers,
            )
            stored_context = self._row_to_request(row).resume_context
            if isinstance(stored_context, dict) and stored_context.get("__schema_version") == 4:
                from dataclasses import replace

                from bowhead_core.ordinary_lead_continuation import require_ordinary_pause_in_tx, typed_answer

                retained = await require_ordinary_pause_in_tx(db, card_id=request_id)
                answered = replace(retained.card, status="resolved", answers=json.loads(normalized["answers_json"] or "null"))
                typed_answer(replace(retained, card=answered))
            primary = str(normalized["chosen_value"] or "")
            cursor = await db.execute(
                """
                UPDATE clarification_requests
                SET status='resolved', chosen_value=?, chosen_values_json=?,
                    chosen_at=?, user_directive=?, edited_subs_json=?, answers_json=?
                WHERE request_id=? AND status='pending'
                """,
                (
                    primary,
                    normalized["chosen_values_json"],
                    _now(),
                    normalized["user_directive"],
                    normalized["edited_subs_json"],
                    normalized["answers_json"],
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return await self.get(request_id), False
            if resolution_work is not None:
                if resolution_channel not in {"web", "feishu"} or not resolution_owner:
                    await db.execute("ROLLBACK")
                    raise ValueError("resolution work requires a valid channel and owner")
                now = datetime.now(tz=UTC)
                lease_expires_at = (now + timedelta(seconds=RESOLUTION_WORK_LEASE_SECONDS)).isoformat(
                    timespec="seconds",
                )
                encoded_work = json.dumps(
                    resolution_work,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                await db.execute(
                    "INSERT INTO clarification_resolution_outbox("
                    "request_id,turn_id,state,channel,work_json,payload_json,"
                    "lease_owner,lease_expires_at,attempt_count,created_at,updated_at"
                    ") VALUES (?,?,'pending',?,?,NULL,?,?,1,?,?)",
                    (
                        request_id,
                        str(row["turn_id"]),
                        resolution_channel,
                        encoded_work,
                        resolution_owner,
                        lease_expires_at,
                        now.isoformat(timespec="seconds"),
                        now.isoformat(timespec="seconds"),
                    ),
                )
            await db.commit()
            cur = await db.execute(
                "SELECT * FROM clarification_requests WHERE request_id = ?",
                (request_id,),
            )
            row = await cur.fetchone()
            return self._row_to_request(row), True

    async def record_resolution_outcome(
        self,
        *,
        request_id: str,
        turn_id: str,
        payload: dict[str, Any],
        resolution_owner: str | None = None,
    ) -> dict[str, Any]:
        """Persist the canonical resolution event payload before delivery.

        The request id is the outbox identity. Concurrent retries always read
        the winner's payload, so canonical resolution facts cannot be rebuilt
        from a later click with different values.
        """
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT * FROM clarification_resolution_outbox WHERE request_id=?",
                (request_id,),
            )
            existing = await cur.fetchone()
            if existing is None:
                # Explicit legacy adoption only. New resolve paths create their
                # pending work row atomically with pending→resolved.
                if resolution_owner is not None:
                    await db.execute("ROLLBACK")
                    raise RuntimeError("clarification resolution work is missing")
                await db.execute(
                    "INSERT INTO clarification_resolution_outbox("
                    "request_id,turn_id,state,channel,work_json,payload_json,"
                    "lease_owner,lease_expires_at,attempt_count,created_at,updated_at"
                    ") VALUES (?,?,'completed','legacy','{}',?,NULL,NULL,0,?,?)",
                    (request_id, turn_id, encoded, now, now),
                )
            elif str(existing["state"]) == "pending":
                if not resolution_owner or str(existing["lease_owner"]) != resolution_owner:
                    await db.execute("ROLLBACK")
                    raise RuntimeError("clarification resolution work is owned by another worker")
                cursor = await db.execute(
                    "UPDATE clarification_resolution_outbox SET state='completed',"
                    "payload_json=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                    "WHERE request_id=? AND state='pending' AND lease_owner=?",
                    (encoded, now, request_id, resolution_owner),
                )
                if cursor.rowcount != 1:
                    await db.execute("ROLLBACK")
                    raise RuntimeError("clarification resolution ownership was lost")
            cur = await db.execute(
                "SELECT state,payload_json FROM clarification_resolution_outbox WHERE request_id=?",
                (request_id,),
            )
            row = await cur.fetchone()
            await db.commit()
        if row is None or str(row["state"]) != "completed" or row["payload_json"] is None:
            raise RuntimeError("clarification resolution outbox row disappeared")
        stored = json.loads(str(row["payload_json"]))
        if not isinstance(stored, dict):
            raise RuntimeError("clarification resolution outbox payload is invalid")
        return stored

    async def get_resolution_outcome(
        self,
        request_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT payload_json FROM clarification_resolution_outbox WHERE request_id=? AND state='completed'",
                (request_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        return payload if isinstance(payload, dict) else None

    async def get_resolution_work_state(self, request_id: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT state FROM clarification_resolution_outbox WHERE request_id=?",
                (request_id,),
            )
            row = await cur.fetchone()
        return str(row[0]) if row is not None else None

    async def claim_resolution_work(
        self,
        *,
        request_id: str,
        channel: str,
        resolution_owner: str,
    ) -> dict[str, Any] | None:
        """Fence and return expired durable work for same-channel recovery."""
        now_dt = datetime.now(tz=UTC)
        now = now_dt.isoformat(timespec="seconds")
        lease_expires_at = (now_dt + timedelta(seconds=RESOLUTION_WORK_LEASE_SECONDS)).isoformat(
            timespec="seconds",
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE clarification_resolution_outbox SET lease_owner=?,"
                "lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? "
                "WHERE request_id=? AND state='pending' AND channel=? "
                "AND lease_expires_at<=?",
                (
                    resolution_owner,
                    lease_expires_at,
                    now,
                    request_id,
                    channel,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return None
            cur = await db.execute(
                "SELECT work_json FROM clarification_resolution_outbox WHERE request_id=? AND state='pending' AND lease_owner=?",
                (request_id, resolution_owner),
            )
            row = await cur.fetchone()
            await db.commit()
        if row is None:
            return None
        work = json.loads(str(row["work_json"]))
        return work if isinstance(work, dict) else None

    async def claim_invalidation_turn(
        self,
        *,
        owner: str,
        request_ids: Collection[str] | None = None,
        limit: int = 100,
        lease_seconds: int = INVALIDATION_WORK_LEASE_SECONDS,
    ) -> list[ClarificationInvalidationOutboxRow]:
        """Lease one complete Turn group of pending invalidation intents.

        ``request_ids`` narrows candidate Turns for immediate publishing, but
        once a Turn wins, every eligible row for that Turn is leased together.
        This prevents two Gateway processes from generating the same evt_seq
        for separate cards that belong to one Turn.
        """

        if not owner or limit < 1 or lease_seconds < 1:
            raise ValueError("clarification invalidation claim bounds are invalid")
        requested = sorted({value for value in (request_ids or ()) if value})
        now_dt = datetime.now(tz=UTC)
        now = now_dt.isoformat(timespec="microseconds")
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat(
            timespec="microseconds",
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            candidate_sql = (
                "SELECT turn_id FROM clarification_invalidation_outbox o WHERE state='pending' AND (lease_owner IS NULL OR lease_expires_at<=?)"
            )
            params: list[Any] = [now]
            if requested:
                placeholders = ",".join("?" for _ in requested)
                candidate_sql += f" AND request_id IN ({placeholders})"  # noqa: S608 — placeholders only
                params.extend(requested)
            candidate_sql += (
                " AND NOT EXISTS ("
                "SELECT 1 FROM clarification_invalidation_outbox peer "
                "WHERE peer.turn_id=o.turn_id AND peer.state='pending' "
                "AND peer.lease_owner IS NOT NULL AND peer.lease_expires_at>?"
                ") ORDER BY created_at,request_id LIMIT 1"
            )
            params.append(now)
            async with db.execute(candidate_sql, params) as cursor:
                candidate = await cursor.fetchone()
            if candidate is None:
                await db.execute("ROLLBACK")
                return []
            turn_id = str(candidate["turn_id"])
            cursor = await db.execute(
                """
                UPDATE clarification_invalidation_outbox
                SET lease_owner=?,lease_expires_at=?,
                    attempt_count=attempt_count+1,updated_at=?
                WHERE turn_id=? AND state='pending'
                  AND (lease_owner IS NULL OR lease_expires_at<=?)
                """,
                (
                    owner,
                    lease_expires_at,
                    now,
                    turn_id,
                    now,
                ),
            )
            if cursor.rowcount < 1:
                await db.execute("ROLLBACK")
                return []
            async with db.execute(
                "SELECT * FROM clarification_invalidation_outbox "
                "WHERE turn_id=? AND state='pending' AND lease_owner=? "
                "ORDER BY created_at,request_id LIMIT ?",
                (turn_id, owner, max(limit, cursor.rowcount)),
            ) as rows_cursor:
                rows = await rows_cursor.fetchall()
            await db.commit()
        return [self._outbox_row(row) for row in rows]

    async def renew_invalidation_turn(
        self,
        *,
        turn_id: str,
        owner: str,
        lease_seconds: int = INVALIDATION_WORK_LEASE_SECONDS,
    ) -> int:
        if not turn_id or not owner or lease_seconds < 1:
            raise ValueError("clarification invalidation lease is invalid")
        now_dt = datetime.now(tz=UTC)
        now = now_dt.isoformat(timespec="microseconds")
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat(
            timespec="microseconds",
        )
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE clarification_invalidation_outbox SET lease_expires_at=?,updated_at=? WHERE turn_id=? AND state='pending' AND lease_owner=?",
                (lease_expires_at, now, turn_id, owner),
            )
            await db.commit()
        return cursor.rowcount

    async def mark_invalidation_delivered(
        self,
        *,
        request_id: str,
        owner: str,
    ) -> bool:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE clarification_invalidation_outbox
                SET state='delivered',lease_owner=NULL,lease_expires_at=NULL,
                    delivered_at=?,updated_at=?
                WHERE request_id=? AND state='pending' AND lease_owner=?
                """,
                (now, now, request_id, owner),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def count_pending_invalidations(self) -> int:
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM clarification_invalidation_outbox WHERE state='pending'",
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def cleanup_delivered_invalidations(
        self,
        *,
        retention_days: float = 30,
        limit: int = 5000,
    ) -> int:
        """Delete old delivered intents only after their durable event exists."""

        if retention_days <= 0 or not 1 <= limit <= 5000:
            raise ValueError("clarification invalidation cleanup bounds are invalid")
        cutoff = (datetime.now(tz=UTC) - timedelta(days=retention_days)).isoformat(
            timespec="microseconds",
        )
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                DELETE FROM clarification_invalidation_outbox
                WHERE request_id IN (
                  SELECT o.request_id
                  FROM clarification_invalidation_outbox o
                  JOIN turn_events e
                    ON e.id=o.event_id
                   AND e.turn_id=o.turn_id
                   AND e.event_type='clarification.dismissed'
                  WHERE o.state='delivered' AND o.delivered_at<?
                    AND json_extract(e.payload_json,'$.request_id')=o.request_id
                    AND json_extract(e.payload_json,'$.status')=o.target_status
                    AND json_extract(e.payload_json,'$.reason')=o.reason_code
                    AND json_extract(e.payload_json,'$.actor')=o.actor_kind
                  ORDER BY o.delivered_at,o.request_id
                  LIMIT ?
                )
                """,
                (cutoff, limit),
            )
            await db.commit()
        return cursor.rowcount

    async def expire_pending_for_turn(
        self,
        turn_id: str,
        *,
        plan_id: str,
    ) -> list[str]:
        """批2 S5: turn 终局(cancelled/failed)时定向过期未答卡, 防孤儿可答卡.

        返回被过期的 request_id(caller 发 `clarification.resolved` skipped=true——
        payload 不带 chosen 值, FE 摘要渲成「已跳过」而非空「已确认」行, review P1-7)。
        此前只有批量 TTL `expire_old`, 无 per-turn 定向能力。
        """
        if not turn_id or not plan_id:
            raise ValueError("mid-run clarification expiry scope is invalid")
        # Single UPDATE...RETURNING (review P3-4): separate SELECT→UPDATE steps under concurrent
        # resolve can emit false skipped events for just-answered cards or miss expiring new cards; atomicity removes
        # both races (SQLite ≥3.35 supports RETURNING). Narrow by the frozen plan
        # resume context; do not also expire ordinary clarifications in the same turn or cards from another plan.
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                UPDATE clarification_requests
                SET status='expired'
                WHERE turn_id=? AND status='pending'
                  AND json_extract(
                    coalesce(resume_context_json,'{}'),
                    '$.plan_midrun_plan_id'
                  )=?
                RETURNING request_id
                """,
                (turn_id, plan_id),
            ) as cur:
                ids = [str(r["request_id"]) for r in await cur.fetchall()]
            await db.commit()
        return ids

    async def expire_old(
        self,
        *,
        ttl_hours: float = 24.0,
        exclude_turn_ids: Collection[str] | None = None,
    ) -> int:
        """清理过期 pending; 返清掉的条数.

        两个豁免条件叠加，来源不同、都必须留：
        - **terminal protocol turns**（main 侧）：v1 协议的 turn 由 terminal 决策链自己管过期。
        - **`exclude_turn_ids`**（scheduled-recipe-runs S3 · FR-5.4）：定时任务跑到歧义点会
          **挂起等人回答**，那张卡的生命周期归 scheduled 侧管（活到任务自身的下一个周期，
          周级 cron 就是一周），不该被这条统一的 24h 系统清扫收走。照单全收有两层后果：
          ① 周五挂起、周一点深链进来无卡可答；② 过期判据把 expired 行误读成「已被回答」
          → run 永久挂起死锁。集合由 gateway 清扫 loop 从 `scheduled_store.awaiting_turn_ids()`
          取，规模上界 = 单 job 单 in-flight × 用户任务数，不必分批。
        """
        cutoff = (datetime.now(tz=UTC) - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            protocol_tables = await _has_protocol_projection_tables(db)
            sql = (
                """
                SELECT request_id,turn_id FROM clarification_requests c
                WHERE status='pending' AND created_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM turns
                    WHERE turns.id=c.turn_id
                      AND turns.terminal_protocol_version=1
                  )
                """
                if protocol_tables
                else """
                SELECT request_id,turn_id FROM clarification_requests
                WHERE status='pending' AND created_at < ?
                """
            )
            params: list[Any] = [cutoff]
            # An empty set must use the no-exclusion branch: IN () is a syntax error in SQLite, and semantically
            # no exemptions already means the original unconditional sweep.
            excluded = sorted({t for t in (exclude_turn_ids or ()) if t})
            if excluded:
                # Parameterize each placeholder instead of interpolating turn_id into SQL: this is a database-wide UPDATE,
                # and no path that can write turn_id should turn it into an injection surface.
                sql += f" AND turn_id NOT IN ({','.join('?' * len(excluded))})"
                params.extend(excluded)
            sql += " ORDER BY created_at,request_id"
            async with db.execute(sql, params) as cursor:
                identities = [
                    ClarificationIdentity(
                        request_id=str(row["request_id"]),
                        turn_id=str(row["turn_id"]),
                    )
                    for row in await cursor.fetchall()
                ]
            invalidations = await invalidate_pending_in_tx(
                db,
                identities,
                target_status="expired",
                reason="clarification_ttl_expired",
                actor="system",
                now=_now(),
            )
            await db.commit()
        record_clarification_invalidation_metrics(
            invalidations,
            outcome="transitioned",
        )
        return len(invalidations)

    async def list_v1_expiry_candidates(
        self,
        *,
        ttl_hours: float = 24.0,
        limit: int = 100,
    ) -> list[ClarificationRequest]:
        """Read due v1 rows; only the terminal decision coordinator may mutate them."""

        if ttl_hours <= 0 or not 1 <= limit <= 100:
            raise ValueError("clarification expiry bounds are invalid")
        cutoff = (datetime.now(tz=UTC) - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT c.* FROM clarification_requests c
                JOIN turns t ON t.id=c.turn_id
                JOIN turn_terminal_receipts r
                  ON r.receipt_id=t.current_terminal_receipt_id
                WHERE c.status='pending' AND c.created_at<?
                  AND t.terminal_protocol_version=1
                  AND r.status='clarification_pending'
                ORDER BY c.created_at,c.request_id LIMIT ?
                """,
                (cutoff, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_request(row) for row in rows]

    async def list_v1_pending_candidates(
        self,
        *,
        limit: int = 100,
        after_id: str | None = None,
    ) -> list[ClarificationRequest]:
        """Read current paused clarifications for authorization revocation."""

        if not 1 <= limit <= 100 or (after_id is not None and (not isinstance(after_id, str) or not after_id)):
            raise ValueError("clarification pending bounds are invalid")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT c.* FROM clarification_requests c
                JOIN turns t ON t.id=c.turn_id
                JOIN turn_terminal_receipts r
                  ON r.receipt_id=t.current_terminal_receipt_id
                WHERE c.status='pending'
                  AND t.terminal_protocol_version=1
                  AND r.status='clarification_pending'
                  AND (? IS NULL OR c.request_id>?)
                ORDER BY c.request_id LIMIT ?
                """,
                (after_id, after_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_request(row) for row in rows]

    @staticmethod
    def _row_to_request(row: aiosqlite.Row) -> ClarificationRequest:
        cands_raw = json.loads(row["candidates_json"] or "[]")
        cands = [
            ClarificationCandidate(
                value=str(c.get("value") or ""),
                label=str(c.get("label") or c.get("value") or ""),
                hint=str(c.get("hint") or ""),
                confidence=str(c.get("confidence") or ""),
                kind=str(c.get("kind") or "measure"),
            )
            for c in cands_raw
            if isinstance(c, dict)
        ]
        # V2.7: tolerate old databases without kind/multi_select/chosen_values_json (default on KeyError).
        keys = row.keys() if hasattr(row, "keys") else []
        kind = row["kind"] if "kind" in keys else "token"
        multi_select = bool(row["multi_select"]) if "multi_select" in keys else False
        cv_json = row["chosen_values_json"] if "chosen_values_json" in keys else None
        chosen_values = None
        if cv_json:
            try:
                chosen_values = json.loads(cv_json)
            except json.JSONDecodeError:
                chosen_values = None
        answers_raw = row["answers_json"] if "answers_json" in keys else None
        answers: list[dict[str, Any]] | None = None
        if answers_raw:
            try:
                parsed_answers = json.loads(answers_raw)
                if isinstance(parsed_answers, list):
                    answers = [value for value in parsed_answers if isinstance(value, dict)]
            except json.JSONDecodeError:
                answers = None
        # M158 #10 P1: editable HITL fields (default None when old databases lack columns).
        user_directive = row["user_directive"] if "user_directive" in keys else None
        edited_subs_raw = row["edited_subs_json"] if "edited_subs_json" in keys else None
        edited_subs: list[dict[str, Any]] | None = None
        if edited_subs_raw:
            try:
                parsed = json.loads(edited_subs_raw)
                if isinstance(parsed, list):
                    edited_subs = [s for s in parsed if isinstance(s, dict)]
            except json.JSONDecodeError:
                edited_subs = None
        injected_at = row["injected_at"] if "injected_at" in keys else None
        session_id = row["session_id"] if "session_id" in keys else None
        directive_cancelled_at = row["directive_cancelled_at"] if "directive_cancelled_at" in keys else None
        # Sprint chat-v4-streaming Phase 4C: resume_context + dismissed_at (default None if old databases lack columns).
        resume_context_raw = row["resume_context_json"] if "resume_context_json" in keys else None
        resume_context: dict[str, Any] | None = None
        if resume_context_raw:
            try:
                parsed_rc = json.loads(resume_context_raw)
                if isinstance(parsed_rc, dict):
                    resume_context = parsed_rc
            except json.JSONDecodeError:
                resume_context = None
        dismissed_at = row["dismissed_at"] if "dismissed_at" in keys else None
        # connector-context T1#6: default tool to None in old databases without the column (same missing-column compatibility pattern above).
        tool = row["tool"] if "tool" in keys else None
        return ClarificationRequest(
            request_id=row["request_id"],
            turn_id=row["turn_id"],
            user_id=row["user_id"],
            workspace_id=row["workspace_id"],
            token=row["token"],
            rationale=row["rationale"],
            candidates=cands,
            status=row["status"],
            chosen_value=row["chosen_value"],
            chosen_values=chosen_values,
            answers=answers,
            chosen_at=row["chosen_at"],
            created_at=row["created_at"],
            kind=kind,
            multi_select=multi_select,
            user_directive=user_directive,
            edited_subs=edited_subs,
            injected_at=injected_at,
            session_id=session_id,
            directive_cancelled_at=directive_cancelled_at,
            resume_context=resume_context,
            dismissed_at=dismissed_at,
            tool=tool,
        )

    # Sprint chat-v4-streaming Phase 4C step 2: Lead transient state snapshot.
    # Called by lead_orchestrator.py at clarification_ask emit; saves the in-flight
    # phase_plan / dispatch_window / response_kind_pre_flight / user_text so
    # resume_turn endpoint can rehydrate Lead without re-classifying intent.
    async def set_resume_context(
        self,
        request_id: str,
        ctx: dict[str, Any],
    ) -> bool:
        """Persist Lead transient state for HITL same-turn-resume.

        Returns True if row was updated. Idempotent (later set wins).

        0.26.2 — logger.warning on rowcount=0 (no matching row). Prior bug
        let this silently fall through if the caller passed a stale/wrong
        request_id. Combined with broad `except Exception` upstream, the
        resume_context write looked like it succeeded but didn't. We now
        log loud so prod logs flag the regression.

        Prefer atomic ``put(..., resume_context=...)`` over post-hoc UPDATE
        when possible — this method stays for the sentinel path where the
        full snapshot is only known after Lead has populated transient state
        (e.g. dispatch_window after first peer dispatch).
        """
        async with aiosqlite.connect(self.db_path) as db:
            # A3: same storage-boundary rule as put(); this later snapshot also needs a version.
            if ctx.get("datarc_selection") is not None:
                from bowhead_core.datarc_selection import DatarcSelectionSnapshot, require_datarc_context_migration

                DatarcSelectionSnapshot.model_validate(ctx["datarc_selection"])
                await require_datarc_context_migration(db)
            ctx_json = json.dumps(stamp_resume_context(ctx), ensure_ascii=False, default=str)
            cursor = await db.execute(
                "UPDATE clarification_requests SET resume_context_json=? WHERE request_id=?",
                (ctx_json, request_id),
            )
            await db.commit()
            ok = cursor.rowcount > 0
            if not ok:
                # Loud-fail: helps spot stale/wrong request_id at runtime.
                import logging as _lg

                _lg.getLogger(__name__).warning(
                    "clarification_store.set_resume_context no_match "
                    "request_id=%s — row missing or already updated by "
                    "concurrent path; HITL same-turn-resume will fall to "
                    "action=noop for this row",
                    request_id,
                )
            return ok

    # Sprint chat-v4-streaming Phase 4C step 6: dismiss path.
    # User clicked "换个问法 →" → mark dismissed without resume. Lead orchestrator
    # ends turn with status="clarification_dismissed". FE composer pre-fills original
    # user_text from resume_context["user_text"].
    async def mark_dismissed(self, request_id: str) -> ClarificationRequest | None:
        """Mark clarification as dismissed (user clicked 换个问法).

        Returns the updated ClarificationRequest, or None if not found.
        Sets status='dismissed' + dismissed_at=now. Idempotent (re-mark = no-op).
        """
        result = await self.invalidate_one(
            request_id,
            target_status="dismissed",
            reason="user_rephrase",
            actor="user",
        )
        return result.request

    # M158 #10 P1 (CTO review #1, 2026-04-29): at the new-turn force_spawn decision, turn_runner
    # finds the latest resolved task_plan clarification that is neither injected nor cancelled.
    # Require session_id to prevent session cross-talk; default TTL 30min.
    async def find_recent_resolved_with_directive(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,  # P1 follow-up #1: required to prevent session cross-talk.
        within_minutes: float = 30.0,
        kind: str = "task_plan",
    ) -> ClarificationRequest | None:
        cutoff = (datetime.now(tz=UTC) - timedelta(minutes=within_minutes)).isoformat(timespec="microseconds")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM clarification_requests
                WHERE user_id = ? AND workspace_id = ? AND session_id = ?
                  AND kind = ? AND status = 'resolved'
                  AND chosen_at >= ?
                  AND injected_at IS NULL
                  AND directive_cancelled_at IS NULL
                  AND (user_directive IS NOT NULL OR edited_subs_json IS NOT NULL)
                ORDER BY chosen_at DESC
                LIMIT 1
                """,
                (user_id, workspace_id, session_id, kind, cutoff),
            )
            row = await cur.fetchone()
            return self._row_to_request(row) if row else None

    async def mark_injected(self, request_id: str) -> bool:
        """Consume a legacy directive once; retained Lead state needs its worker CAS."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE clarification_requests SET injected_at=? WHERE request_id=? AND injected_at IS NULL "
                "AND CASE WHEN json_valid(resume_context_json) THEN COALESCE(json_extract(resume_context_json,'$.__schema_version'),0)<>4 ELSE 1 END",
                (_now(), request_id),
            )
            await db.commit()
            return cur.rowcount == 1

    async def cancel_directive(self, request_id: str, *, user_id: str) -> bool:
        """用户撤销后保留已消费 source 字段，只写独立取消审计。

        ``user_directive``/``edited_subs_json`` may be bound into a Terminal
        Protocol decision digest.  In-place redaction would make recovery
        impossible; ``directive_cancelled_at`` is the effective product state
        and ``find_recent_resolved_with_directive`` already excludes it.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE clarification_requests SET directive_cancelled_at=? "
                "WHERE request_id=? AND user_id=? AND injected_at IS NULL "
                "AND directive_cancelled_at IS NULL",
                (_now(), request_id, user_id),
            )
            await db.commit()
            return cur.rowcount == 1
