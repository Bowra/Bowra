"""Owner-bound persistence and maintenance for conversational Subagent drafts."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx

DRAFT_SCHEMA_VERSION = 1
# Must exceed the generator's total timeout (MODEL_HARD_TIMEOUT_SECONDS); otherwise the lease expires mid-generation and same-key replay generates twice.
REQUEST_LEASE_SECONDS = 180
REQUEST_TTL_HOURS = 25
DRAFT_ACTIVE_HOURS = 24
DRAFT_CONTENT_DAYS = 7
DRAFT_PURGE_DAYS = 30
# Must exceed the lease: maintenance must not classify still-generating drafts as generation_interrupted.
STALE_GENERATING_SECONDS = 240
_REQUIRED_TABLES = frozenset(
    {
        "team_subagent_authoring_context",
        "team_subagent_draft_request",
        "team_subagent_draft",
        "team_subagent_draft_suggestion",
    },
)

DraftStatus = Literal[
    "generating",
    "needs_clarification",
    "editing",
    "ready",
    "blocked",
    "expired",
    "consumed",
    "discarded",
]
ValidationStatus = Literal["valid", "warning", "invalid", "unknown"]
SourceMode = Literal["intent", "turn", "manual"]


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SubagentDraftSchemaUnavailableError(RuntimeError):
    pass


class SubagentDraftSchemaUnsupportedError(RuntimeError):
    pass


class SubagentDraftVersionConflictError(RuntimeError):
    def __init__(self, current_version: int) -> None:
        super().__init__("subagent_draft_version_conflict")
        self.current_version = current_version


class SubagentDraftIdempotencyConflictError(RuntimeError):
    pass


class DraftClaimOutcome(StrEnum):
    ACQUIRED = "acquired"
    IN_PROGRESS = "in_progress"
    REPLAY = "replay"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class DraftClaimResult:
    outcome: DraftClaimOutcome
    idempotency_key_hash: str
    lease_token: str | None = None
    expires_at: str | None = None
    result_draft_id: str | None = None
    outcome_code: str | None = None
    taken_over: bool = False


@dataclass(frozen=True, slots=True)
class CanvasDraftSuggestion:
    id: str
    draft_id: str
    request_key_hash: str
    base_version: int
    kind: Literal["definition", "clarification"]
    status: Literal["pending", "applied", "dismissed"]
    payload: dict[str, Any]
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class SubagentDraftCreate:
    id: str
    team_id: str
    owner_user_id: str
    source_mode: SourceMode
    source_turn_id: str | None
    status: DraftStatus
    validation_status: ValidationStatus
    locale: str
    purpose_text: str | None
    goal_summary: str | None
    definition: dict[str, Any] | None
    recommendations: list[dict[str, Any]] | None
    assumptions: list[str]
    warnings: list[dict[str, Any]]
    clarification: dict[str, Any] | None
    validation_issues: list[dict[str, Any]]
    catalog_fingerprint: str | None
    definition_fingerprint: str | None
    idempotency_key_hash: str
    mutation_id: str
    generation_attempt: int = 1
    business_clarification_count: int = 0
    outcome_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class SubagentDraft:
    id: str
    team_id: str
    owner_user_id: str
    workspace_id: str | None
    source_mode: str
    entry_mode: str
    source_turn_id: str | None
    status: str
    validation_status: str
    locale: str
    purpose_text: str | None
    goal_summary: str | None
    definition: dict[str, Any] | None
    recommendations: list[dict[str, Any]] | None
    assumptions: list[str]
    warnings: list[dict[str, Any]]
    clarification: dict[str, Any] | None
    validation_issues: list[dict[str, Any]]
    catalog_fingerprint: str | None
    definition_fingerprint: str | None
    idempotency_key_hash: str
    mutation_id: str
    generation_attempt: int
    business_clarification_count: int
    row_version: int
    outcome_reason_code: str | None
    consumed_definition_id: str | None
    consumed_at: str | None
    discarded_at: str | None
    expired_at: str | None
    expires_at: str
    content_scrub_at: str
    purge_at: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DraftMaintenanceResult:
    interrupted: int
    expired: int
    scrubbed: int
    purged: int
    request_claims_purged: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _envelope(kind: str, value: Any) -> str:
    return json.dumps(
        {
            "schema_version": DRAFT_SCHEMA_VERSION,
            "kind": kind,
            "value": value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_envelope(
    raw: str | None,
    *,
    kind: str,
    default: Any,
) -> Any:
    if raw is None:
        return default
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SubagentDraftSchemaUnsupportedError(
            "subagent_draft_schema_unsupported",
        ) from exc
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version") != DRAFT_SCHEMA_VERSION
        or envelope.get("kind") != kind
        or "value" not in envelope
    ):
        raise SubagentDraftSchemaUnsupportedError(
            "subagent_draft_schema_unsupported",
        )
    return envelope["value"]


class SubagentDraftStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        hmac_key: bytes | None,
        clock: Clock | None = None,
    ) -> None:
        if hmac_key is not None and len(hmac_key) < 32:
            raise ValueError("subagent draft HMAC key must be at least 32 bytes")
        self.db_path = str(db_path)
        self._hmac_key = bytes(hmac_key) if hmac_key is not None else None
        self._clock = clock or SystemClock()

    async def initialize(self) -> None:
        """Verify the Alembic-owned schema without creating or altering it."""
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                """,
            ) as cursor:
                tables = {str(row[0]) for row in await cursor.fetchall()}
            missing = _REQUIRED_TABLES - tables
            if missing:
                raise SubagentDraftSchemaUnavailableError(
                    "subagent_draft_schema_missing:" + ",".join(sorted(missing)),
                )
            async with db.execute(
                """
                SELECT schema_version
                FROM team_subagent_schema_meta
                WHERE singleton=1
                """,
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or int(row[0]) < 3:
                raise SubagentDraftSchemaUnavailableError(
                    "subagent_draft_schema_version_too_old",
                )

    def hash_idempotency_key(self, value: str) -> str:
        if self._hmac_key is None:
            raise RuntimeError("subagent draft HMAC key unavailable")
        normalized = value.strip()
        if not 8 <= len(normalized) <= 200:
            raise ValueError("Idempotency-Key length must be in [8,200]")
        return hmac.new(
            self._hmac_key,
            b"subagent-draft-idempotency-v1\0" + normalized.encode(),
            hashlib.sha256,
        ).hexdigest()

    def request_fingerprint(self, request: dict[str, Any]) -> str:
        try:
            canonical = json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("draft request must be canonical JSON") from exc
        return hmac.new(
            self._hmac_key,
            b"subagent-draft-request-v1\0" + canonical,
            hashlib.sha256,
        ).hexdigest()

    async def claim_request(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        idempotency_key: str,
        request: dict[str, Any],
    ) -> DraftClaimResult:
        key_hash = self.hash_idempotency_key(idempotency_key)
        fingerprint = self.request_fingerprint(request)
        now = _utc(self._clock.now())
        now_s = _timestamp(now)
        lease_s = _timestamp(now + timedelta(seconds=REQUEST_LEASE_SECONDS))
        expires_s = _timestamp(now + timedelta(hours=REQUEST_TTL_HOURS))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT *
                    FROM team_subagent_draft_request
                    WHERE team_id=? AND owner_user_id=?
                      AND idempotency_key_hash=?
                    """,
                    (team_id, owner_user_id, key_hash),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    if str(row["request_fingerprint"]) != fingerprint:
                        await db.rollback()
                        return DraftClaimResult(
                            DraftClaimOutcome.CONFLICT,
                            key_hash,
                            expires_at=str(row["expires_at"]),
                        )
                    if str(row["state"]) in {"completed", "failed"}:
                        await db.rollback()
                        return DraftClaimResult(
                            DraftClaimOutcome.REPLAY,
                            key_hash,
                            expires_at=str(row["expires_at"]),
                            result_draft_id=row["result_draft_id"],
                            outcome_code=row["outcome_code"],
                        )
                    # A process may have inserted the draft and crashed before
                    # completing its request claim. Recover the durable result
                    # before considering the still-live assessing lease.
                    async with db.execute(
                        """
                        SELECT id, status, expires_at
                        FROM team_subagent_draft
                        WHERE team_id=? AND owner_user_id=?
                          AND idempotency_key_hash=?
                        """,
                        (team_id, owner_user_id, key_hash),
                    ) as cursor:
                        recovered_draft = await cursor.fetchone()
                    if recovered_draft is not None:
                        await db.rollback()
                        return DraftClaimResult(
                            DraftClaimOutcome.REPLAY,
                            key_hash,
                            expires_at=str(recovered_draft["expires_at"]),
                            result_draft_id=str(recovered_draft["id"]),
                            outcome_code=(f"draft_{recovered_draft['status']}"),
                        )
                    if str(row["lease_expires_at"]) > now_s:
                        await db.rollback()
                        return DraftClaimResult(
                            DraftClaimOutcome.IN_PROGRESS,
                            key_hash,
                            expires_at=str(row["expires_at"]),
                        )
                    cursor = await db.execute(
                        """
                        UPDATE team_subagent_draft_request
                        SET lease_expires_at=?, expires_at=?, updated_at=?
                        WHERE team_id=? AND owner_user_id=?
                          AND idempotency_key_hash=?
                          AND request_fingerprint=?
                          AND state='assessing'
                          AND lease_expires_at<=?
                        """,
                        (
                            lease_s,
                            expires_s,
                            now_s,
                            team_id,
                            owner_user_id,
                            key_hash,
                            fingerprint,
                            now_s,
                        ),
                    )
                    await db.commit()
                    if cursor.rowcount == 1:
                        return DraftClaimResult(
                            DraftClaimOutcome.ACQUIRED,
                            key_hash,
                            lease_token=lease_s,
                            expires_at=expires_s,
                            taken_over=True,
                        )
                    return DraftClaimResult(
                        DraftClaimOutcome.IN_PROGRESS,
                        key_hash,
                        expires_at=str(row["expires_at"]),
                    )

                async with db.execute(
                    """
                    SELECT id, status, expires_at
                    FROM team_subagent_draft
                    WHERE team_id=? AND owner_user_id=?
                      AND idempotency_key_hash=?
                    """,
                    (team_id, owner_user_id, key_hash),
                ) as cursor:
                    draft = await cursor.fetchone()
                if draft is not None:
                    await db.rollback()
                    return DraftClaimResult(
                        DraftClaimOutcome.REPLAY,
                        key_hash,
                        expires_at=str(draft["expires_at"]),
                        result_draft_id=str(draft["id"]),
                        outcome_code=f"draft_{draft['status']}",
                    )

                await db.execute(
                    """
                    INSERT INTO team_subagent_draft_request(
                      team_id, owner_user_id, idempotency_key_hash,
                      request_fingerprint, state, result_draft_id,
                      outcome_code, lease_expires_at, expires_at,
                      created_at, updated_at
                    ) VALUES (?,?,?,?, 'assessing', NULL, NULL, ?,?,?,?)
                    """,
                    (
                        team_id,
                        owner_user_id,
                        key_hash,
                        fingerprint,
                        lease_s,
                        expires_s,
                        now_s,
                        now_s,
                    ),
                )
                await db.commit()
                return DraftClaimResult(
                    DraftClaimOutcome.ACQUIRED,
                    key_hash,
                    lease_token=lease_s,
                    expires_at=expires_s,
                )
            except BaseException:
                await db.rollback()
                raise

    async def complete_request(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        idempotency_key_hash: str,
        lease_token: str | None,
        outcome_code: str,
        result_draft_id: str | None,
        failed: bool = False,
    ) -> bool:
        if not lease_token:
            return False
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE team_subagent_draft_request
                SET state=?, result_draft_id=?, outcome_code=?, updated_at=?
                WHERE team_id=? AND owner_user_id=?
                  AND idempotency_key_hash=?
                  AND state='assessing'
                  AND lease_expires_at=?
                """,
                (
                    "failed" if failed else "completed",
                    result_draft_id,
                    outcome_code,
                    now_s,
                    team_id,
                    owner_user_id,
                    idempotency_key_hash,
                    lease_token,
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def create_draft(
        self,
        create: SubagentDraftCreate,
    ) -> SubagentDraft:
        now = _utc(self._clock.now())
        now_s = _timestamp(now)
        expires_at = _timestamp(now + timedelta(hours=DRAFT_ACTIVE_HOURS))
        content_scrub_at = _timestamp(
            now + timedelta(days=DRAFT_CONTENT_DAYS),
        )
        purge_at = _timestamp(now + timedelta(days=DRAFT_PURGE_DAYS))
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO team_subagent_draft(
                  id, team_id, owner_user_id, source_mode, entry_mode,
                  source_turn_id, status, validation_status, locale,
                  purpose_text, goal_summary, definition_json,
                  recommendations_json, assumptions_json, warnings_json,
                  clarification_json, validation_issues_json,
                  catalog_fingerprint, definition_fingerprint,
                  idempotency_key_hash, mutation_id, generation_attempt,
                  business_clarification_count, row_version,
                  outcome_reason_code, expires_at, content_scrub_at,
                  purge_at, created_at, updated_at
                ) VALUES (
                  ?,?,?,?,'conversational',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                  ?,?,1,?,?,?,?,?,?
                )
                """,
                (
                    create.id,
                    create.team_id,
                    create.owner_user_id,
                    create.source_mode,
                    create.source_turn_id,
                    create.status,
                    create.validation_status,
                    create.locale,
                    create.purpose_text,
                    create.goal_summary,
                    (
                        _envelope(
                            "subagent_draft_definition",
                            create.definition,
                        )
                        if create.definition is not None
                        else None
                    ),
                    (
                        _envelope(
                            "subagent_draft_recommendations",
                            create.recommendations,
                        )
                        if create.recommendations is not None
                        else None
                    ),
                    _envelope(
                        "subagent_draft_assumptions",
                        create.assumptions,
                    ),
                    _envelope("subagent_draft_warnings", create.warnings),
                    (
                        _envelope(
                            "subagent_draft_clarification",
                            create.clarification,
                        )
                        if create.clarification is not None
                        else None
                    ),
                    _envelope(
                        "subagent_draft_validation_issues",
                        create.validation_issues,
                    ),
                    create.catalog_fingerprint,
                    create.definition_fingerprint,
                    create.idempotency_key_hash,
                    create.mutation_id,
                    create.generation_attempt,
                    create.business_clarification_count,
                    create.outcome_reason_code,
                    expires_at,
                    content_scrub_at,
                    purge_at,
                    now_s,
                    now_s,
                ),
            )
            await db.commit()
        result = await self.get_draft(
            create.id,
            team_id=create.team_id,
            owner_user_id=create.owner_user_id,
        )
        if result is None:
            raise RuntimeError("subagent_draft_create_lost")
        return result

    async def create_manual_draft(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
        locale: str,
        purpose: str,
        idempotency_key: str,
        source_mode: Literal["manual", "intent", "turn"] = "manual",
        source_turn_id: str | None = None,
        authorize: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
    ) -> SubagentDraft:
        """Create one incomplete Canvas draft; replay the same request without another row."""
        if not workspace_id or len(purpose) > 2000:
            raise ValueError("manual draft requires a space and bounded purpose")
        key_hash = self.hash_idempotency_key(idempotency_key)
        create_payload = {"workspace_id": workspace_id, "locale": locale, "purpose": purpose}
        if source_mode != "manual":
            create_payload["source_mode"] = source_mode
        if source_turn_id is not None:
            if source_mode != "turn" or not source_turn_id:
                raise ValueError("only conversational drafts accept a source turn")
            create_payload["source_turn_id"] = source_turn_id
        elif source_mode == "turn":
            raise ValueError("conversational drafts require a source turn")
        payload_hash = self.request_fingerprint(create_payload)
        now = _utc(self._clock.now())
        now_s = _timestamp(now)
        draft_id = f"sad_{secrets.token_hex(6)}"
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                if authorize is not None:
                    await authorize(db)
                async with db.execute(
                    """
                    SELECT id, source_mode, workspace_id, create_payload_hash
                    FROM team_subagent_draft
                    WHERE team_id=? AND owner_user_id=? AND idempotency_key_hash=?
                    """,
                    (team_id, owner_user_id, key_hash),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    if (
                        existing["source_mode"] != source_mode
                        or existing["workspace_id"] != workspace_id
                        or existing["create_payload_hash"] != payload_hash
                    ):
                        raise SubagentDraftIdempotencyConflictError("idempotency_key_reused")
                    draft_id = str(existing["id"])
                else:
                    await db.execute(
                        """
                        INSERT INTO team_subagent_draft(
                          id, team_id, owner_user_id, workspace_id, create_payload_hash, source_mode,
                          entry_mode, source_turn_id, status, validation_status,
                          locale, purpose_text, recommendations_json,
                          assumptions_json, warnings_json,
                          validation_issues_json, idempotency_key_hash, mutation_id,
                          expires_at, content_scrub_at, purge_at, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?, 'canvas', ?, 'editing',
                                  'unknown', ?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            draft_id,
                            team_id,
                            owner_user_id,
                            workspace_id,
                            payload_hash,
                            source_mode,
                            source_turn_id,
                            locale,
                            purpose,
                            _envelope("subagent_draft_recommendations", []),
                            _envelope("subagent_draft_assumptions", []),
                            _envelope("subagent_draft_warnings", []),
                            _envelope("subagent_draft_validation_issues", []),
                            key_hash,
                            str(uuid4()),
                            _timestamp(now + timedelta(hours=DRAFT_ACTIVE_HOURS)),
                            _timestamp(now + timedelta(days=DRAFT_CONTENT_DAYS)),
                            _timestamp(now + timedelta(days=DRAFT_PURGE_DAYS)),
                            now_s,
                            now_s,
                        ),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        result = await self.get_draft(draft_id, team_id=team_id, owner_user_id=owner_user_id)
        if result is None:
            raise RuntimeError("manual draft create lost")
        return result

    async def save_manual_draft(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
        expected_version: int,
        purpose: str,
        definition: dict[str, Any],
        text_only_confirmed: bool,
    ) -> SubagentDraft | None:
        """Persist incomplete fields with CAS, invalidating the old review result."""
        if len(purpose) > 2000:
            raise ValueError("manual draft purpose too long")
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT row_version FROM team_subagent_draft
                    WHERE id=? AND team_id=? AND owner_user_id=?
                      AND workspace_id=? AND entry_mode='canvas'
                    """,
                    (draft_id, team_id, owner_user_id, workspace_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return None
                current_version = int(row["row_version"])
                if current_version != expected_version:
                    raise SubagentDraftVersionConflictError(current_version)
                cursor = await db.execute(
                    """
                    UPDATE team_subagent_draft
                    SET status='editing', validation_status='unknown',
                        purpose_text=?, goal_summary=?, definition_json=?,
                        warnings_json=?, validation_issues_json=?,
                        catalog_fingerprint=NULL, definition_fingerprint=NULL,
                        row_version=row_version+1, updated_at=?
                    WHERE id=? AND team_id=? AND owner_user_id=? AND workspace_id=?
                      AND entry_mode='canvas' AND row_version=?
                      AND status IN ('editing', 'ready', 'blocked') AND expires_at>?
                    """,
                    (
                        purpose,
                        str(definition.get("description") or "")[:500],
                        _envelope("subagent_draft_definition", definition),
                        _envelope(
                            "subagent_draft_warnings",
                            ([{"code": "subagent_text_only_confirmation"}] if text_only_confirmed and not definition.get("tool_refs") else []),
                        ),
                        _envelope("subagent_draft_validation_issues", []),
                        now_s,
                        draft_id,
                        team_id,
                        owner_user_id,
                        workspace_id,
                        expected_version,
                        now_s,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SubagentDraftVersionConflictError(current_version)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_draft(draft_id, team_id=team_id, owner_user_id=owner_user_id)

    async def record_canvas_suggestion(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
        request_key_hash: str,
        lease_token: str | None,
        base_version: int,
        kind: Literal["definition", "clarification"],
        payload: dict[str, Any],
        require_current_base: bool = False,
    ) -> CanvasDraftSuggestion:
        """Commit the suggestion and request receipt together; never overwrite edits."""
        encoded = _envelope("subagent_canvas_suggestion", payload)
        if len(encoded) > 131072 or not lease_token:
            raise ValueError("canvas suggestion invalid")
        now_s = _timestamp(self._clock.now())
        suggestion_id = f"sas_{secrets.token_hex(6)}"
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """SELECT content_scrub_at,row_version FROM team_subagent_draft
                       WHERE id=? AND team_id=? AND owner_user_id=? AND workspace_id=?
                         AND entry_mode='canvas' AND status IN ('editing','ready','blocked')
                         AND expires_at>?""",
                    (draft_id, team_id, owner_user_id, workspace_id, now_s),
                ) as cursor:
                    draft = await cursor.fetchone()
                if draft is None:
                    raise ValueError("canvas draft unavailable")
                if require_current_base and int(draft["row_version"]) != base_version:
                    raise SubagentDraftVersionConflictError(int(draft["row_version"]))
                async with db.execute(
                    """SELECT state,lease_expires_at FROM team_subagent_draft_request
                       WHERE team_id=? AND owner_user_id=? AND idempotency_key_hash=?""",
                    (team_id, owner_user_id, request_key_hash),
                ) as cursor:
                    claim = await cursor.fetchone()
                if claim is None or claim["state"] != "assessing" or claim["lease_expires_at"] != lease_token:
                    raise ValueError("canvas generation lease lost")
                await db.execute(
                    """INSERT INTO team_subagent_draft_suggestion(
                         id,draft_id,team_id,owner_user_id,request_key_hash,base_version,
                         kind,status,payload_json,created_at,updated_at,expires_at
                       ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)""",
                    (
                        suggestion_id,
                        draft_id,
                        team_id,
                        owner_user_id,
                        request_key_hash,
                        base_version,
                        kind,
                        encoded,
                        now_s,
                        now_s,
                        draft["content_scrub_at"],
                    ),
                )
                cursor = await db.execute(
                    """UPDATE team_subagent_draft_request
                       SET state='completed',result_draft_id=?,outcome_code=?,updated_at=?
                       WHERE team_id=? AND owner_user_id=? AND idempotency_key_hash=?
                         AND state='assessing' AND lease_expires_at=?""",
                    (draft_id, f"canvas_{kind}", now_s, team_id, owner_user_id, request_key_hash, lease_token),
                )
                if cursor.rowcount != 1:
                    raise ValueError("canvas generation lease lost")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return CanvasDraftSuggestion(
            id=suggestion_id,
            draft_id=draft_id,
            request_key_hash=request_key_hash,
            base_version=base_version,
            kind=kind,
            status="pending",
            payload=payload,
            created_at=now_s,
            expires_at=str(draft["content_scrub_at"]),
        )

    async def get_canvas_suggestion_for_request(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
        request_key_hash: str,
    ) -> CanvasDraftSuggestion | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT s.* FROM team_subagent_draft_suggestion s
                   JOIN team_subagent_draft d ON d.id=s.draft_id
                   WHERE s.team_id=? AND s.owner_user_id=? AND d.workspace_id=?
                     AND s.request_key_hash=? AND s.expires_at>?""",
                (team_id, owner_user_id, workspace_id, request_key_hash, _timestamp(self._clock.now())),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_canvas_suggestion(row) if row is not None else None

    async def list_canvas_suggestions(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
    ) -> list[CanvasDraftSuggestion]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT s.* FROM team_subagent_draft_suggestion s
                   JOIN team_subagent_draft d ON d.id=s.draft_id
                   WHERE d.id=? AND d.team_id=? AND d.owner_user_id=? AND d.workspace_id=?
                     AND s.status='pending' AND s.expires_at>?
                   ORDER BY s.created_at DESC""",
                (draft_id, team_id, owner_user_id, workspace_id, _timestamp(self._clock.now())),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_canvas_suggestion(row) for row in rows]

    async def get_canvas_suggestion(
        self,
        draft_id: str,
        suggestion_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
    ) -> CanvasDraftSuggestion | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT s.* FROM team_subagent_draft_suggestion s
                   JOIN team_subagent_draft d ON d.id=s.draft_id
                   WHERE d.id=? AND s.id=? AND d.team_id=? AND d.owner_user_id=?
                     AND d.workspace_id=? AND s.expires_at>?""",
                (draft_id, suggestion_id, team_id, owner_user_id, workspace_id, _timestamp(self._clock.now())),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_canvas_suggestion(row) if row is not None else None

    async def dismiss_canvas_suggestion(
        self,
        draft_id: str,
        suggestion_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
    ) -> bool:
        """Hide a suggestion without changing the draft or another owner's work."""
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """UPDATE team_subagent_draft_suggestion
                       SET status='dismissed',updated_at=?
                       WHERE id=? AND draft_id=? AND team_id=? AND owner_user_id=?
                         AND status='pending' AND expires_at>?
                         AND EXISTS (
                           SELECT 1 FROM team_subagent_draft d
                           WHERE d.id=team_subagent_draft_suggestion.draft_id
                             AND d.team_id=? AND d.owner_user_id=? AND d.workspace_id=?
                             AND d.entry_mode='canvas' AND d.status IN ('editing','ready','blocked')
                         )""",
                    (now_s, suggestion_id, draft_id, team_id, owner_user_id, now_s, team_id, owner_user_id, workspace_id),
                )
                changed = cursor.rowcount == 1
                await db.commit()
                return changed
            except BaseException:
                await db.rollback()
                raise

    async def apply_canvas_suggestion(
        self,
        draft_id: str,
        suggestion_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
        expected_version: int,
        definition: dict[str, Any],
        purpose: str,
        selected_fields: list[str],
        mutation_id: str,
    ) -> tuple[SubagentDraft, bool]:
        """Apply selected fields and mark their source in the same CAS transaction."""
        mutation_hash = self.request_fingerprint(
            {
                "mutation_id": mutation_id,
                "selected_fields": selected_fields,
            }
        )
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """SELECT row_version,status FROM team_subagent_draft
                       WHERE id=? AND team_id=? AND owner_user_id=? AND workspace_id=?
                         AND entry_mode='canvas' AND expires_at>?""",
                    (draft_id, team_id, owner_user_id, workspace_id, now_s),
                ) as cursor:
                    draft = await cursor.fetchone()
                async with db.execute(
                    """SELECT status,kind,accepted_mutation_hash FROM team_subagent_draft_suggestion
                       WHERE id=? AND draft_id=? AND team_id=? AND owner_user_id=? AND expires_at>?""",
                    (suggestion_id, draft_id, team_id, owner_user_id, now_s),
                ) as cursor:
                    suggestion = await cursor.fetchone()
                if draft is None or suggestion is None or suggestion["kind"] != "definition":
                    raise ValueError("canvas suggestion unavailable")
                if suggestion["status"] == "applied":
                    if suggestion["accepted_mutation_hash"] != mutation_hash:
                        raise SubagentDraftIdempotencyConflictError("canvas suggestion already applied")
                    await db.rollback()
                    current = await self.get_draft(draft_id, team_id=team_id, owner_user_id=owner_user_id)
                    if current is None:
                        raise ValueError("canvas draft unavailable")
                    return current, True
                if suggestion["status"] != "pending" or draft["status"] not in {"editing", "ready", "blocked"}:
                    raise ValueError("canvas suggestion unavailable")
                if int(draft["row_version"]) != expected_version:
                    raise SubagentDraftVersionConflictError(int(draft["row_version"]))
                cursor = await db.execute(
                    """UPDATE team_subagent_draft
                       SET status='editing',validation_status='unknown',
                           purpose_text=?,goal_summary=?,definition_json=?,
                           warnings_json=?,validation_issues_json=?,
                           catalog_fingerprint=NULL,definition_fingerprint=NULL,
                           row_version=row_version+1,updated_at=?
                       WHERE id=? AND team_id=? AND owner_user_id=? AND workspace_id=?
                         AND row_version=? AND status IN ('editing','ready','blocked') AND expires_at>?""",
                    (
                        purpose,
                        str(definition.get("description") or "")[:500],
                        _envelope("subagent_draft_definition", definition),
                        _envelope("subagent_draft_warnings", []),
                        _envelope("subagent_draft_validation_issues", []),
                        now_s,
                        draft_id,
                        team_id,
                        owner_user_id,
                        workspace_id,
                        expected_version,
                        now_s,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SubagentDraftVersionConflictError(int(draft["row_version"]))
                await db.execute(
                    """UPDATE team_subagent_draft_suggestion
                       SET status='applied',accepted_mutation_hash=?,accepted_version=?,updated_at=?
                       WHERE id=? AND status='pending'""",
                    (mutation_hash, expected_version + 1, now_s, suggestion_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        current = await self.get_draft(draft_id, team_id=team_id, owner_user_id=owner_user_id)
        if current is None:
            raise RuntimeError("canvas suggestion apply lost")
        return current, False

    async def convert_to_manual_draft(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
        expected_version: int,
        purpose: str,
        definition: dict[str, Any],
        text_only_confirmed: bool,
    ) -> SubagentDraft | None:
        """Move an AI draft into Canvas without changing its identity or provenance."""
        if not workspace_id or len(purpose) > 2000:
            raise ValueError("manual draft requires a space and bounded purpose")
        request_hash = self.request_fingerprint(
            {
                "workspace_id": workspace_id,
                "expected_version": expected_version,
                "purpose": purpose,
                "definition": definition,
                "text_only_confirmed": text_only_confirmed,
            }
        )
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT source_mode, entry_mode, workspace_id, row_version,
                           create_payload_hash, status
                    FROM team_subagent_draft
                    WHERE id=? AND team_id=? AND owner_user_id=?
                    """,
                    (draft_id, team_id, owner_user_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or row["workspace_id"] not in {None, workspace_id}:
                    await db.rollback()
                    return None
                if row["entry_mode"] == "canvas":
                    if row["workspace_id"] != workspace_id or row["create_payload_hash"] != request_hash:
                        raise SubagentDraftIdempotencyConflictError("subagent_draft_conversion_conflict")
                else:
                    current_version = int(row["row_version"])
                    if current_version != expected_version:
                        raise SubagentDraftVersionConflictError(current_version)
                    if row["source_mode"] not in {"intent", "turn"} or row["status"] not in {
                        "generating",
                        "needs_clarification",
                        "ready",
                        "blocked",
                    }:
                        raise ValueError("subagent_draft_not_convertible")
                    cursor = await db.execute(
                        """
                        UPDATE team_subagent_draft
                        SET workspace_id=?, entry_mode='canvas', status='editing',
                            validation_status='unknown', purpose_text=?, goal_summary=?,
                            definition_json=?, warnings_json=?, validation_issues_json=?,
                            catalog_fingerprint=NULL, definition_fingerprint=NULL,
                            create_payload_hash=?, row_version=row_version+1, updated_at=?
                        WHERE id=? AND team_id=? AND owner_user_id=?
                          AND row_version=? AND entry_mode='conversational'
                          AND status IN ('generating', 'needs_clarification', 'ready', 'blocked')
                          AND expires_at>?
                        """,
                        (
                            workspace_id,
                            purpose,
                            str(definition.get("description") or "")[:500],
                            _envelope("subagent_draft_definition", definition),
                            _envelope(
                                "subagent_draft_warnings",
                                ([{"code": "subagent_text_only_confirmation"}] if text_only_confirmed and not definition.get("tool_refs") else []),
                            ),
                            _envelope("subagent_draft_validation_issues", []),
                            request_hash,
                            now_s,
                            draft_id,
                            team_id,
                            owner_user_id,
                            expected_version,
                            now_s,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise SubagentDraftVersionConflictError(current_version)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_draft(draft_id, team_id=team_id, owner_user_id=owner_user_id)

    async def get_draft(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
    ) -> SubagentDraft | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM team_subagent_draft
                WHERE id=? AND team_id=? AND owner_user_id=?
                """,
                (draft_id, team_id, owner_user_id),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_draft(row) if row is not None else None

    async def list_active_drafts(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        limit: int = 20,
        source_mode: str | None = None,
        entry_mode: str | None = None,
        workspace_id: str | None = None,
    ) -> list[SubagentDraft]:
        """Return only the owner's resumable drafts, newest update first."""
        if not 1 <= limit <= 50:
            raise ValueError("draft list limit must be in [1, 50]")
        if source_mode not in {None, "manual", "conversational"}:
            raise ValueError("unsupported draft source filter")
        if entry_mode not in {None, "canvas", "conversational"}:
            raise ValueError("unsupported draft entry filter")
        if workspace_id is not None and source_mode != "manual" and entry_mode != "canvas":
            raise ValueError("space filter requires manual drafts")
        source_filter = (
            " AND source_mode='manual'" if source_mode == "manual" else " AND source_mode!='manual'" if source_mode == "conversational" else ""
        )
        space_filter = " AND workspace_id=?" if workspace_id is not None else ""
        entry_filter = " AND entry_mode=?" if entry_mode is not None else ""
        params: tuple[object, ...] = (
            team_id,
            owner_user_id,
            _timestamp(_utc(self._clock.now())),
            *((entry_mode,) if entry_mode is not None else ()),
            *((workspace_id,) if workspace_id is not None else ()),
            limit,
        )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT * FROM team_subagent_draft
                WHERE team_id=? AND owner_user_id=?
                  AND status IN ('generating', 'needs_clarification', 'editing', 'ready', 'blocked')
                  AND expires_at>?
                  {source_filter}{entry_filter}{space_filter}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_draft(row) for row in rows]

    async def discard_draft(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        expected_version: int,
        request_outcome_code: str | None = None,
    ) -> SubagentDraft | None:
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT row_version
                    FROM team_subagent_draft
                    WHERE id=? AND team_id=? AND owner_user_id=?
                    """,
                    (draft_id, team_id, owner_user_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return None
                current_version = int(row["row_version"])
                if current_version != expected_version:
                    await db.rollback()
                    raise SubagentDraftVersionConflictError(current_version)
                cursor = await db.execute(
                    """
                    UPDATE team_subagent_draft
                    SET status='discarded',
                        purpose_text=NULL,
                        goal_summary=NULL,
                        definition_json=NULL,
                        recommendations_json=NULL,
                        assumptions_json=?,
                        warnings_json=?,
                        clarification_json=NULL,
                        validation_issues_json=?,
                        discarded_at=?,
                        row_version=row_version+1,
                        updated_at=?
                    WHERE id=? AND team_id=? AND owner_user_id=?
                      AND row_version=?
                      AND status NOT IN ('consumed', 'discarded')
                    """,
                    (
                        _envelope("subagent_draft_assumptions", []),
                        _envelope("subagent_draft_warnings", []),
                        _envelope("subagent_draft_validation_issues", []),
                        now_s,
                        now_s,
                        draft_id,
                        team_id,
                        owner_user_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise SubagentDraftVersionConflictError(current_version)
                await db.execute(
                    "DELETE FROM team_subagent_draft_suggestion WHERE draft_id=?",
                    (draft_id,),
                )
                if request_outcome_code is not None:
                    await db.execute(
                        """
                        UPDATE team_subagent_draft_request
                        SET state='failed',
                            result_draft_id=NULL,
                            outcome_code=?,
                            updated_at=?
                        WHERE team_id=? AND owner_user_id=?
                          AND result_draft_id=?
                          AND state='completed'
                        """,
                        (
                            request_outcome_code,
                            now_s,
                            team_id,
                            owner_user_id,
                            draft_id,
                        ),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_draft(
            draft_id,
            team_id=team_id,
            owner_user_id=owner_user_id,
        )

    async def update_review(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        expected_version: int,
        definition: dict[str, Any],
        recommendations: list[dict[str, Any]],
        assumptions: list[str],
        warnings: list[dict[str, Any]],
        validation_issues: list[dict[str, Any]],
        catalog_fingerprint: str,
        definition_fingerprint: str,
        validation_status: ValidationStatus,
        status: Literal["ready", "blocked"],
    ) -> SubagentDraft | None:
        if status == "ready" and validation_status not in {
            "valid",
            "warning",
        }:
            raise ValueError("ready draft requires valid or warning status")
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT row_version
                    FROM team_subagent_draft
                    WHERE id=? AND team_id=? AND owner_user_id=?
                    """,
                    (draft_id, team_id, owner_user_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return None
                current_version = int(row["row_version"])
                if current_version != expected_version:
                    raise SubagentDraftVersionConflictError(
                        current_version,
                    )
                cursor = await db.execute(
                    """
                    UPDATE team_subagent_draft
                    SET status=?,
                        validation_status=?,
                        definition_json=?,
                        recommendations_json=?,
                        assumptions_json=?,
                        warnings_json=?,
                        validation_issues_json=?,
                        catalog_fingerprint=?,
                        definition_fingerprint=?,
                        row_version=row_version+1,
                        updated_at=?
                    WHERE id=? AND team_id=? AND owner_user_id=?
                      AND row_version=?
                      AND status IN ('editing', 'ready', 'blocked')
                      AND expires_at>?
                    """,
                    (
                        status,
                        validation_status,
                        _envelope(
                            "subagent_draft_definition",
                            definition,
                        ),
                        _envelope(
                            "subagent_draft_recommendations",
                            recommendations,
                        ),
                        _envelope(
                            "subagent_draft_assumptions",
                            assumptions,
                        ),
                        _envelope("subagent_draft_warnings", warnings),
                        _envelope(
                            "subagent_draft_validation_issues",
                            validation_issues,
                        ),
                        catalog_fingerprint,
                        definition_fingerprint,
                        now_s,
                        draft_id,
                        team_id,
                        owner_user_id,
                        expected_version,
                        now_s,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SubagentDraftVersionConflictError(
                        current_version,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_draft(
            draft_id,
            team_id=team_id,
            owner_user_id=owner_user_id,
        )

    async def begin_clarification(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        expected_version: int,
        answer: str,
    ) -> SubagentDraft | None:
        """CAS-claim the single business clarification before model work."""
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT row_version,clarification_json
                    FROM team_subagent_draft
                    WHERE id=? AND team_id=? AND owner_user_id=?
                    """,
                    (draft_id, team_id, owner_user_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return None
                current_version = int(row["row_version"])
                if current_version != expected_version:
                    raise SubagentDraftVersionConflictError(
                        current_version,
                    )
                existing_clarification = _decode_envelope(
                    row["clarification_json"],
                    kind="subagent_draft_clarification",
                    default={},
                )
                cursor = await db.execute(
                    """
                    UPDATE team_subagent_draft
                    SET status='generating',
                        clarification_json=?,
                        business_clarification_count=1,
                        generation_attempt=generation_attempt+1,
                        row_version=row_version+1,
                        updated_at=?
                    WHERE id=? AND team_id=? AND owner_user_id=?
                      AND row_version=?
                      AND status='needs_clarification'
                      AND business_clarification_count=0
                      AND expires_at>?
                    """,
                    (
                        _envelope(
                            "subagent_draft_clarification",
                            {
                                **(
                                    existing_clarification
                                    if isinstance(
                                        existing_clarification,
                                        dict,
                                    )
                                    else {}
                                ),
                                "answer": answer,
                                "answered": True,
                            },
                        ),
                        now_s,
                        draft_id,
                        team_id,
                        owner_user_id,
                        expected_version,
                        now_s,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SubagentDraftVersionConflictError(
                        current_version,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_draft(
            draft_id,
            team_id=team_id,
            owner_user_id=owner_user_id,
        )

    async def finish_clarification(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        expected_version: int,
        definition: dict[str, Any] | None,
        recommendations: list[dict[str, Any]] | None,
        assumptions: list[str],
        warnings: list[dict[str, Any]],
        validation_issues: list[dict[str, Any]],
        catalog_fingerprint: str | None,
        definition_fingerprint: str | None,
        validation_status: ValidationStatus,
        status: Literal["ready", "blocked"],
        outcome_reason_code: str | None,
    ) -> SubagentDraft | None:
        """Finish a previously claimed clarification generation."""
        if status == "ready" and (definition is None or definition_fingerprint is None or validation_status not in {"valid", "warning"}):
            raise ValueError("ready clarification requires valid definition")
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT row_version
                    FROM team_subagent_draft
                    WHERE id=? AND team_id=? AND owner_user_id=?
                    """,
                    (draft_id, team_id, owner_user_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return None
                current_version = int(row["row_version"])
                if current_version != expected_version:
                    raise SubagentDraftVersionConflictError(
                        current_version,
                    )
                cursor = await db.execute(
                    """
                    UPDATE team_subagent_draft
                    SET status=?,
                        validation_status=?,
                        definition_json=?,
                        recommendations_json=?,
                        assumptions_json=?,
                        warnings_json=?,
                        validation_issues_json=?,
                        catalog_fingerprint=?,
                        definition_fingerprint=?,
                        outcome_reason_code=?,
                        row_version=row_version+1,
                        updated_at=?
                    WHERE id=? AND team_id=? AND owner_user_id=?
                      AND row_version=?
                      AND status='generating'
                      AND business_clarification_count=1
                      AND expires_at>?
                    """,
                    (
                        status,
                        validation_status,
                        (
                            _envelope(
                                "subagent_draft_definition",
                                definition,
                            )
                            if definition is not None
                            else None
                        ),
                        (
                            _envelope(
                                "subagent_draft_recommendations",
                                recommendations,
                            )
                            if recommendations is not None
                            else None
                        ),
                        _envelope(
                            "subagent_draft_assumptions",
                            assumptions,
                        ),
                        _envelope("subagent_draft_warnings", warnings),
                        _envelope(
                            "subagent_draft_validation_issues",
                            validation_issues,
                        ),
                        catalog_fingerprint,
                        definition_fingerprint,
                        outcome_reason_code,
                        now_s,
                        draft_id,
                        team_id,
                        owner_user_id,
                        expected_version,
                        now_s,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SubagentDraftVersionConflictError(
                        current_version,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_draft(
            draft_id,
            team_id=team_id,
            owner_user_id=owner_user_id,
        )

    async def run_maintenance(
        self,
        *,
        limit: int = 100,
    ) -> DraftMaintenanceResult:
        if not 1 <= limit <= 1000:
            raise ValueError("maintenance limit must be in [1,1000]")
        now = _utc(self._clock.now())
        now_s = _timestamp(now)
        stale_s = _timestamp(
            now - timedelta(seconds=STALE_GENERATING_SECONDS),
        )
        empty_assumptions = _envelope("subagent_draft_assumptions", [])
        empty_warnings = _envelope("subagent_draft_warnings", [])
        empty_issues = _envelope("subagent_draft_validation_issues", [])
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                interrupted = await db.execute(
                    """
                    UPDATE team_subagent_draft
                    SET status='blocked',
                        validation_status='unknown',
                        outcome_reason_code='subagent_draft_generation_interrupted',
                        row_version=row_version+1,
                        updated_at=?
                    WHERE id IN (
                      SELECT id FROM team_subagent_draft
                      WHERE status='generating' AND updated_at<=?
                      ORDER BY updated_at
                      LIMIT ?
                    )
                    """,
                    (now_s, stale_s, limit),
                )
                expired = await db.execute(
                    """
                    UPDATE team_subagent_draft
                    SET status='expired',
                        expired_at=?,
                        row_version=row_version+1,
                        updated_at=?
                    WHERE id IN (
                      SELECT id FROM team_subagent_draft
                      WHERE status IN (
                        'generating', 'needs_clarification', 'ready', 'blocked'
                      )
                        AND expires_at<=?
                      ORDER BY expires_at
                      LIMIT ?
                    )
                    """,
                    (now_s, now_s, now_s, limit),
                )
                await db.execute(
                    """DELETE FROM team_subagent_draft_suggestion WHERE id IN (
                         SELECT s.id FROM team_subagent_draft_suggestion s
                         LEFT JOIN team_subagent_draft d ON d.id=s.draft_id
                         WHERE s.expires_at<=? OR d.id IS NULL OR d.status IN ('consumed','discarded')
                         ORDER BY s.expires_at LIMIT ?
                       )""",
                    (now_s, limit),
                )
                scrubbed = await db.execute(
                    """
                    UPDATE team_subagent_draft
                    SET purpose_text=NULL,
                        goal_summary=NULL,
                        definition_json=NULL,
                        recommendations_json=NULL,
                        assumptions_json=?,
                        warnings_json=?,
                        clarification_json=NULL,
                        validation_issues_json=?,
                        row_version=row_version+1,
                        updated_at=?
                    WHERE id IN (
                      SELECT id FROM team_subagent_draft
                      WHERE content_scrub_at<=?
                        AND (
                          purpose_text IS NOT NULL
                          OR goal_summary IS NOT NULL
                          OR definition_json IS NOT NULL
                          OR recommendations_json IS NOT NULL
                          OR clarification_json IS NOT NULL
                        )
                      ORDER BY content_scrub_at
                      LIMIT ?
                    )
                    """,
                    (
                        empty_assumptions,
                        empty_warnings,
                        empty_issues,
                        now_s,
                        now_s,
                        limit,
                    ),
                )
                purged = await db.execute(
                    """
                    DELETE FROM team_subagent_draft
                    WHERE id IN (
                      SELECT id FROM team_subagent_draft
                      WHERE purge_at<=?
                      ORDER BY purge_at
                      LIMIT ?
                    )
                    """,
                    (now_s, limit),
                )
                claims = await db.execute(
                    """
                    DELETE FROM team_subagent_draft_request
                    WHERE rowid IN (
                      SELECT rowid FROM team_subagent_draft_request
                      WHERE expires_at<=?
                      ORDER BY expires_at
                      LIMIT ?
                    )
                    """,
                    (now_s, limit),
                )
                await db.commit()
                return DraftMaintenanceResult(
                    interrupted=max(interrupted.rowcount, 0),
                    expired=max(expired.rowcount, 0),
                    scrubbed=max(scrubbed.rowcount, 0),
                    purged=max(purged.rowcount, 0),
                    request_claims_purged=max(claims.rowcount, 0),
                )
            except BaseException:
                await db.rollback()
                raise


def _row_to_canvas_suggestion(row: aiosqlite.Row) -> CanvasDraftSuggestion:
    payload = _decode_envelope(row["payload_json"], kind="subagent_canvas_suggestion", default=None)
    if not isinstance(payload, dict):
        raise SubagentDraftSchemaUnsupportedError("subagent_draft_schema_unsupported")
    return CanvasDraftSuggestion(
        id=str(row["id"]),
        draft_id=str(row["draft_id"]),
        request_key_hash=str(row["request_key_hash"]),
        base_version=int(row["base_version"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        payload=payload,
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
    )


def _row_to_draft(row: aiosqlite.Row) -> SubagentDraft:
    definition = _decode_envelope(
        row["definition_json"],
        kind="subagent_draft_definition",
        default=None,
    )
    recommendations = _decode_envelope(
        row["recommendations_json"],
        kind="subagent_draft_recommendations",
        default=None,
    )
    assumptions = _decode_envelope(
        row["assumptions_json"],
        kind="subagent_draft_assumptions",
        default=[],
    )
    warnings = _decode_envelope(
        row["warnings_json"],
        kind="subagent_draft_warnings",
        default=[],
    )
    clarification = _decode_envelope(
        row["clarification_json"],
        kind="subagent_draft_clarification",
        default=None,
    )
    validation_issues = _decode_envelope(
        row["validation_issues_json"],
        kind="subagent_draft_validation_issues",
        default=[],
    )
    if definition is not None and not isinstance(definition, dict):
        raise SubagentDraftSchemaUnsupportedError(
            "subagent_draft_schema_unsupported",
        )
    for value in (recommendations, assumptions, warnings, validation_issues):
        if value is not None and not isinstance(value, list):
            raise SubagentDraftSchemaUnsupportedError(
                "subagent_draft_schema_unsupported",
            )
    if clarification is not None and not isinstance(clarification, dict):
        raise SubagentDraftSchemaUnsupportedError(
            "subagent_draft_schema_unsupported",
        )
    return SubagentDraft(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        owner_user_id=str(row["owner_user_id"]),
        workspace_id=row["workspace_id"],
        source_mode=str(row["source_mode"]),
        entry_mode=str(row["entry_mode"]),
        source_turn_id=row["source_turn_id"],
        status=str(row["status"]),
        validation_status=str(row["validation_status"]),
        locale=str(row["locale"]),
        purpose_text=row["purpose_text"],
        goal_summary=row["goal_summary"],
        definition=definition,
        recommendations=recommendations,
        assumptions=assumptions,
        warnings=warnings,
        clarification=clarification,
        validation_issues=validation_issues,
        catalog_fingerprint=row["catalog_fingerprint"],
        definition_fingerprint=row["definition_fingerprint"],
        idempotency_key_hash=str(row["idempotency_key_hash"]),
        mutation_id=str(row["mutation_id"]),
        generation_attempt=int(row["generation_attempt"]),
        business_clarification_count=int(
            row["business_clarification_count"],
        ),
        row_version=int(row["row_version"]),
        outcome_reason_code=row["outcome_reason_code"],
        consumed_definition_id=row["consumed_definition_id"],
        consumed_at=row["consumed_at"],
        discarded_at=row["discarded_at"],
        expired_at=row["expired_at"],
        expires_at=str(row["expires_at"]),
        content_scrub_at=str(row["content_scrub_at"]),
        purge_at=str(row["purge_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


__all__ = [
    "DraftClaimOutcome",
    "DraftClaimResult",
    "DraftMaintenanceResult",
    "SubagentDraft",
    "SubagentDraftCreate",
    "SubagentDraftSchemaUnavailableError",
    "SubagentDraftSchemaUnsupportedError",
    "SubagentDraftStore",
    "SubagentDraftVersionConflictError",
]
