"""Atomic governance writes for user-owned Memory.

This module deliberately uses the shared SQLite schema contract instead of
importing ``bowhead_memory``. Candidate approval must update the target Memory
row and the candidate decision in one transaction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.memory_notes import MAX_BULLET_CHARS, MAX_CONTENT_CHARS, NotesCharLimitError, NotesLockedError
from bowhead_core.memory_privacy import ensure_memory_write_allowed
from bowhead_core.user_memory import (
    CURRENT_MEMORY_POLICY_VERSION,
    MemoryConfidenceConflictError,
    MemoryEntryLockedError,
    MemoryRole,
    MemoryScopeConflictError,
)

ApprovalTarget = Literal["structured", "notes"]


class CandidateNotFoundError(LookupError):
    """Candidate does not exist or is owned by another user."""


class CandidateTargetError(ValueError):
    """Candidate cannot be committed to the requested target."""


class CandidateWriteGateError(ValueError):
    """记忆重做 批2：候选内容没过 write gate（一次性要求 / 语言设置 / 含密钥）。

    与 `CandidateTargetError` 分开是因为**处置不同**：target 错是调用方传错了目标面
    （改个参数重试即可），闸没过是**内容本身不该进长期记忆**，重试多少次都一样，
    回执要把理由原样告诉用户。
    """

    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        self.gate_message = message
        super().__init__(f"{reason}: {message}")


@dataclass(frozen=True)
class GovernanceCandidate:
    id: str
    user_id: str
    subject_type: str
    kind: str
    key: str
    value: Any
    reason: str | None
    source_turn_id: str | None
    status: str
    decided_by: str | None
    decided_at: str | None
    created_at: str
    updated_at: str
    workspace_id: str | None = None
    version: int = 1
    decision_kind: str | None = None
    approved_resource_type: str | None = None
    approved_resource_id: str | None = None
    # Memory redesign batches 1/2: existing memories this approval will **soft-delete**.
    # Without exposing this, users approving cannot see what they destroy (card shows only kind/key/value).
    supersedes: tuple[str, ...] = ()
    memory_type: str | None = None


class PersonalMemoryGovernanceStore:
    """Cross-table Memory transaction seam."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        root_secret: str | bytes | None = None,
    ) -> None:
        self._db_path = str(db_path)
        seed = root_secret or f"dev-only:{Path(self._db_path).resolve()}"
        seed_bytes = seed.encode("utf-8") if isinstance(seed, str) else seed
        self._note_key_secret = hmac.new(
            seed_bytes,
            b"bowhead/personal-memory/note-key/v1",
            hashlib.sha256,
        ).digest()

    async def approve_candidate(
        self,
        *,
        user_id: str,
        candidate_id: str,
        target: ApprovalTarget,
        decided_by: str,
    ) -> GovernanceCandidate:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                candidate = await _get_owned_candidate(
                    db,
                    user_id=user_id,
                    candidate_id=candidate_id,
                )
                if candidate["status"] != "pending":
                    await db.rollback()
                    return _candidate_from_row(candidate)

                # ── Memory redesign batch 2: write gate runs **before** target dispatch ────────
                # ★ Must be here, never inside `_upsert_structured_candidate`; otherwise
                # `target == "notes"` (all kind='note' candidates without workspace_id)
                # entirely bypasses the gate, allowing legacy one-off/secret-bearing note candidates to be approved
                # into `memory_notes`. Module docs require both write paths to pass the gate,
                # and approve has two branches itself.
                _ensure_candidate_passes_write_gate(candidate)

                if target == "structured":
                    resource_type, resource_id = await _upsert_structured_candidate(
                        db,
                        candidate,
                        decided_by=decided_by,
                        note_key_secret=self._note_key_secret,
                    )
                elif target == "notes":
                    if candidate["kind"] != "note":
                        raise CandidateTargetError(
                            "only kind 'note' can be approved to About you",
                        )
                    resource_type, resource_id = await _append_candidate_note(
                        db,
                        candidate,
                    )
                else:
                    raise CandidateTargetError(f"unsupported target: {target}")

                now = _now()
                candidate_fingerprint = _optional_column(
                    candidate,
                    "content_fingerprint",
                )
                if candidate_fingerprint is not None:
                    candidate_workspace_id = _optional_column(
                        candidate,
                        "workspace_id",
                    )
                    await db.execute(
                        """
                        INSERT INTO memory_candidate_suppressions(
                            user_id, source_scope_key, kind, fingerprint,
                            decision_kind, created_at, expires_at
                        )
                        VALUES (?, ?, ?, ?, 'user_approved', ?,
                                datetime(?, '+30 days'))
                        ON CONFLICT(
                            user_id, source_scope_key, kind, fingerprint
                        )
                        DO UPDATE SET
                            decision_kind=excluded.decision_kind,
                            created_at=excluded.created_at,
                            expires_at=excluded.expires_at
                        """,
                        (
                            user_id,
                            candidate_workspace_id or "__no_space__",
                            candidate["kind"],
                            candidate_fingerprint,
                            now,
                            now,
                        ),
                    )
                await db.execute(
                    """
                    UPDATE memory_candidates
                    SET status='approved', decision_kind='user_approved',
                        decided_by=?, decided_at=?, updated_at=?, version=version+1,
                        target_scope_type=?, target_workspace_id=?,
                        approved_resource_type=?, approved_resource_id=?,
                        key='', value_json='null', reason=NULL,
                        source_turn_id=NULL, workspace_id=NULL,
                        content_fingerprint=NULL, supersedes_json=NULL,
                        content_redacted_at=?
                    WHERE id=? AND user_id=? AND status='pending'
                    """,
                    (
                        decided_by,
                        now,
                        now,
                        "global" if target == "notes" or not _optional_column(candidate, "workspace_id") else "space",
                        None if target == "notes" else _optional_column(candidate, "workspace_id"),
                        resource_type,
                        resource_id,
                        now,
                        candidate_id,
                        user_id,
                    ),
                )
                async with db.execute(
                    "SELECT * FROM memory_candidates WHERE id=? AND user_id=?",
                    (candidate_id, user_id),
                ) as cursor:
                    decided = await cursor.fetchone()
                if decided is None:
                    raise CandidateNotFoundError(candidate_id)
                await db.commit()
                return _candidate_from_row(decided)
            except BaseException:
                await db.rollback()
                raise


def _ensure_candidate_passes_write_gate(candidate: aiosqlite.Row) -> None:
    """候选内容过 write gate（记忆重做 批2 / tech-design §4.3）。

    用户点了批准**也要**过闸：候选是后台推断的产物，用户批的是「看着像那么回事」，
    不是「我逐字确认过这里面没有密钥、也不是一次性要求」。

    ⚠ 运维 kind（`datarc_binding`）不过闸：那是机器状态不是偏好，
    拿偏好词表去判它，一个带 token 的绑定会被判成「含密钥」而被静默丢掉
    —— 批1 已经定过「取代/闸不许碰运维行」。
    """
    from bowhead_core.memory_write_gate import check
    from bowhead_core.memory_write_policy import comparable_text

    if candidate["kind"] == "datarc_binding":
        return
    verdict = check(
        # Gate key and value together: language candidates often carry all meaning in key
        # （key='answer_language', value='English'）。
        value_text=comparable_text(candidate["value_json"]),
        has_turn_id=bool(candidate["source_turn_id"]),
        # approve is a user action on the memory page ⇒ attribution holds (criterion ② UI-actor branch).
        actor_type="user",
        key=candidate["key"],
    )
    if not verdict.allowed:
        raise CandidateWriteGateError(reason=verdict.reason or "", message=verdict.message)


async def _get_owned_candidate(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    candidate_id: str,
) -> aiosqlite.Row:
    async with db.execute(
        "SELECT * FROM memory_candidates WHERE id=? AND user_id=?",
        (candidate_id, user_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise CandidateNotFoundError(candidate_id)
    return row


async def _upsert_structured_candidate(
    db: aiosqlite.Connection,
    candidate: aiosqlite.Row,
    *,
    decided_by: str,
    note_key_secret: bytes,
) -> tuple[str, str]:
    """把一条候选提交进 `memory_entries`。

    功能已正式发布；权限、作用域和存储就绪校验仍须执行。

        - 置信度由写入身份推出：候选来源恒是 `dreaming_light_approved` ⇒ `low`；
        - `low` 覆盖不了 `high`：抛 `MemoryConfidenceConflictError`，整个 approve 事务回滚
          ⇒ **候选保持 pending**（= 落成候选而不是覆盖），由用户自己裁；
        - 规范化逐字判重命中 ⇒ 只 bump `last_confirmed_at`，不新增行；
        - 候选带的 `supersedes` 被真正执行（旧条软删 + `superseded_by`），不再是没人解析的自由文本。
    """
    from bowhead_core.memory_readiness import memory_storage_ready
    from bowhead_core.memory_write_policy import (
        can_overwrite,
        comparable_text,
        confidence_for_source,
        is_verbatim_duplicate,
        normalize_memory_type,
    )

    canonical = memory_storage_ready()
    # ★ Track B batch 1b: derive write identity by `origin`; previously hardcoded `dreaming_light_approved`,
    # treating user-verbatim-confirmed drafts as background inference (brief pitfall 2).
    # ⚠ `feedback_confirmed` still implies `low`, an intentional choice after sprint consolidation review:
    # LLM drafts and names the key, so it must not overwrite user-authored entries
    # (reason below `memory_write_policy._HIGH_CONFIDENCE_SOURCES`).
    # This split enables **traceable provenance** (memory page explains origin), never privilege escalation.
    _origin = _optional_column(candidate, "origin") or "dreaming_light"
    entry_source = "feedback_confirmed" if _origin == "feedback_draft" else "dreaming_light_approved"
    confidence = confidence_for_source(entry_source)
    entry_key = candidate["key"]
    if candidate["kind"] == "note":
        entry_key = _stable_note_key(
            candidate["value_json"],
            secret=note_key_secret,
        )
    incoming_workspace_id = _optional_column(candidate, "workspace_id")
    now = _now()

    confirmed_id: str | None = None
    if canonical:
        # Verbatim duplicate check precedes identity lookup; identical text with a changed key must not create a second entry
        # (net-margin proposal repeated 5 times).
        # ⚠ **Do not return early** on a match: candidate may repeat existing definitions while naming another entry to supersede
        # (same discipline as `commit_memory_write`; early return swallows supersedes).
        async with db.execute(
            """
            SELECT id, value, locked, workspace_id FROM memory_entries
            WHERE subject_type='user' AND subject_id=? AND deleted_at IS NULL
              AND memory_role != 'operational' AND kind != 'datarc_binding'
              -- ⚠ 逐字判重也必须**按作用域收窄**：不收窄的话，一条 ws_A 的候选会命中
              -- 一条全局（或 ws_B）的同文条目，然后走 `confirmed_id` 分支**跳过**下面的
              -- 作用域冲突红线（memory-revive S2），把 approved_resource_id 指到别的
              -- 作用域的行上，而 ws_A 什么也没写成。
              AND COALESCE(workspace_id, '*') = COALESCE(?, '*')
            """,
            (candidate["user_id"], incoming_workspace_id),
        ) as cursor:
            scan_rows = await cursor.fetchall()
        incoming_text = comparable_text(candidate["value_json"])
        for row in scan_rows:
            if is_verbatim_duplicate(incoming_text, comparable_text(row["value"])):
                # Locked entries cannot change even `last_confirmed_at`; this shortcut otherwise bypasses
                # identity lookup's `MemoryEntryLockedError`.
                if bool(row["locked"]):
                    raise MemoryEntryLockedError(str(row["id"]))
                await db.execute(
                    "UPDATE memory_entries SET last_confirmed_at=? WHERE id=?",
                    (now, row["id"]),
                )
                confirmed_id = str(row["id"])
                break

    # ⚠ Identity remains `(kind, key)` and scope conflicts still raise; **this batch preserves scope semantics**
    # (defaulting writes to current Space belongs to batch 4 / `FEATURE_MEMORY_SCOPE_WRITE`).
    # Premature scope partitioning leaves the same key globally and in a Space, both injected together.
    # ⚠ Identity lookup must exclude operational rows **exactly like** duplicate scanning. Without this, approving a
    # `datarc_binding` candidate matches the operational BI-binding row, rewriting it to
    # `memory_role=product_memory`; dict binding values infer `object_v1`,
    # absent from visible combinations, so `list_product_for_user` drops the row and
    # `v2_readiness()` (**no user filter**) makes everyone's memory pages return 503,
    # also clearing BI context state. Batch 1 required gate/supersede to leave operational rows untouched;
    # previously enforced only in scans, not identity lookup (caught in 2026-09-04 consolidation review).
    existing = None
    if confirmed_id is None:
        async with db.execute(
            """
            SELECT id, locked, workspace_id, confidence
            FROM memory_entries
            WHERE subject_type='user' AND subject_id=? AND kind=? AND key=?
              AND memory_role != 'operational' AND kind != 'datarc_binding'
            """,
            (candidate["user_id"], candidate["kind"], entry_key),
        ) as cursor:
            existing = await cursor.fetchone()

    if confirmed_id is not None:
        entry_id = confirmed_id
    elif existing is not None:
        if bool(existing["locked"]):
            raise MemoryEntryLockedError(existing["id"])
        existing_workspace_id = existing["workspace_id"]
        if incoming_workspace_id != existing_workspace_id:
            raise MemoryScopeConflictError(
                entry_id=existing["id"],
                existing_workspace_id=existing_workspace_id,
                requested_workspace_id=incoming_workspace_id,
            )
        if canonical and not can_overwrite(
            new_confidence=confidence,
            existing_confidence=existing["confidence"],
        ):
            raise MemoryConfidenceConflictError(
                entry_id=str(existing["id"]),
                existing_confidence=existing["confidence"],
            )
        if canonical:
            await db.execute(
                """
                UPDATE memory_entries
                SET value=?, source=?, updated_at=?, deleted_at=NULL,
                    version=version+1,
                    memory_role=?, value_schema=?, memory_policy_version=?,
                    memory_type=?, confidence=?, last_confirmed_at=?,
                    previous_value=value, previous_value_at=updated_at,
                    superseded_by=NULL,
                    last_modified_actor_type='user',
                    last_modified_actor_id=?,
                    last_modified_at=?
                WHERE id=?
                """,
                (
                    candidate["value_json"],
                    entry_source,
                    now,
                    MemoryRole.PRODUCT_MEMORY,
                    _entry_value_schema(candidate),
                    CURRENT_MEMORY_POLICY_VERSION,
                    normalize_memory_type(_optional_column(candidate, "memory_type")),
                    confidence,
                    now,
                    decided_by,
                    now,
                    existing["id"],
                ),
            )
        else:
            # ⚠ Legacy defect, **unchanged in this batch** (gate-off behavior must be byte-identical), documented only: this UPDATE uses
            # `_value_schema_from_json`, assigning `text_v1` to kind='note', rather than
            # `note_text_v1` ⇒ `resolve_memory_policy` rejects visibility; memory no longer enters
            # `<remembered_about_user>` or appears on memory pages. Gate-on branch uses
            # `_entry_value_schema` to fix it; gate-off branch waits for legacy migration cleanup.
            await db.execute(
                """
                UPDATE memory_entries
                SET value=?, source=?, updated_at=?, deleted_at=NULL,
                    version=version+1,
                    memory_role=?,
                    value_schema=?,
                    memory_policy_version=?,
                    last_modified_actor_type='user',
                    last_modified_actor_id=?,
                    last_modified_at=?
                WHERE id=?
                """,
                (
                    candidate["value_json"],
                    entry_source,
                    now,
                    MemoryRole.PRODUCT_MEMORY,
                    _value_schema_from_json(candidate["value_json"]),
                    CURRENT_MEMORY_POLICY_VERSION,
                    decided_by,
                    now,
                    existing["id"],
                ),
            )
        entry_id = str(existing["id"])
    else:
        entry_id = f"mem_{uuid4().hex[:12]}"
        await db.execute(
            """
            INSERT INTO memory_entries(
                id, subject_type, subject_id, kind, key, value, locked,
                lock_reason, source, created_at, updated_at, workspace_id,
                version, memory_role, value_schema, memory_policy_version,
                created_source_type, created_source_turn_id,
                created_source_candidate_id, created_source_workspace_id,
                last_modified_actor_type, last_modified_actor_id, last_modified_at,
                memory_type, confidence, last_confirmed_at
            )
            VALUES (
                ?, 'user', ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?,
                1, ?, ?, ?, 'candidate', ?, ?, ?, 'user', ?, ?, ?, ?, ?
            )
            """,
            (
                entry_id,
                candidate["user_id"],
                candidate["kind"],
                entry_key,
                candidate["value_json"],
                entry_source,
                now,
                now,
                incoming_workspace_id,
                MemoryRole.PRODUCT_MEMORY,
                _entry_value_schema(candidate),
                CURRENT_MEMORY_POLICY_VERSION,
                candidate["source_turn_id"],
                candidate["id"],
                incoming_workspace_id,
                decided_by,
                now,
                (normalize_memory_type(_optional_column(candidate, "memory_type")) if canonical else None),
                confidence if canonical else None,
                now if canonical else None,
            ),
        )

    if canonical:
        for target_id in _candidate_supersedes(candidate):
            if target_id == entry_id:
                continue
            async with db.execute(
                """
                SELECT id, locked, confidence FROM memory_entries
                WHERE id=? AND subject_type='user' AND subject_id=? AND deleted_at IS NULL
                          -- 取代只能打到真记忆：运维行（tool_context / datarc_binding）
                          -- 也在这张表里，被 supersede 掉就是把 BI 上下文状态删了。
                          AND memory_role != 'operational' AND kind != 'datarc_binding'
                """,
                (target_id, candidate["user_id"]),
            ) as cursor:
                target = await cursor.fetchone()
            if target is None or bool(target["locked"]):
                continue
            if not can_overwrite(
                new_confidence=confidence,
                existing_confidence=target["confidence"],
            ):
                # Background-inferred supersedes cannot affect direct user statements; retain them for the user to decide.
                continue
            await db.execute(
                """
                UPDATE memory_entries
                SET deleted_at=?, superseded_by=?, updated_at=?, version=version+1
                WHERE id=?
                """,
                (now, entry_id, now, target_id),
            )

    return "memory_entry", entry_id


def _entry_value_schema(candidate: aiosqlite.Row) -> str:
    """`kind='note'` 的 policy 键是 `note_text_v1`，不是 `text_v1`——写错一个字
    这条记忆过不了 `resolve_memory_policy`，会在注入路径上被静默丢掉。"""
    if candidate["kind"] == "note":
        return "note_text_v1"
    return _value_schema_from_json(candidate["value_json"])


def _candidate_supersedes(candidate: aiosqlite.Row) -> list[str]:
    raw = _optional_column(candidate, "supersedes_json")
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in parsed if str(item or "").strip()))


async def _append_candidate_note(
    db: aiosqlite.Connection,
    candidate: aiosqlite.Row,
) -> tuple[str, str]:
    bullet = _normalize_bullet(candidate["value_json"])
    async with db.execute(
        """
        SELECT *
        FROM memory_notes
        WHERE subject_type='user' AND subject_id=?
        """,
        (candidate["user_id"],),
    ) as cursor:
        existing = await cursor.fetchone()

    if existing is not None and existing["editing_locked_until"]:
        async with db.execute(
            "SELECT datetime(?) > datetime('now')",
            (existing["editing_locked_until"],),
        ) as cursor:
            active_row = await cursor.fetchone()
        if active_row and bool(active_row[0]):
            raise NotesLockedError(existing["editing_locked_until"])

    new_line = f"• {bullet}"
    existing_content = str(existing["content"] or "") if existing is not None else ""
    if new_line in existing_content.splitlines():
        content = existing_content
    else:
        content = f"{existing_content.rstrip()}\n{new_line}".lstrip("\n")
    if len(content) > MAX_CONTENT_CHARS:
        raise NotesCharLimitError(len(content))

    now = _now()
    note_id = existing["id"] if existing is not None else f"note_{uuid4().hex[:12]}"
    await db.execute(
        """
        INSERT INTO memory_notes(
            id, subject_type, subject_id, content, source, source_turn_id,
            created_at, updated_at, version,
            created_source_type, created_source_turn_id,
            created_source_candidate_id, created_source_workspace_id,
            last_modified_actor_type, last_modified_actor_id, last_modified_at
        )
        VALUES (
            ?, 'user', ?, ?, 'dreaming_approved', ?, ?, ?, 1,
            'candidate', ?, ?, ?, 'user', ?, ?
        )
        ON CONFLICT(subject_type, subject_id) DO UPDATE SET
            content=excluded.content,
            source=excluded.source,
            source_turn_id=excluded.source_turn_id,
            updated_at=excluded.updated_at,
            deleted_at=NULL,
            version=version+1,
            last_modified_actor_type='user',
            last_modified_actor_id=excluded.last_modified_actor_id,
            last_modified_at=excluded.last_modified_at
        """,
        (
            note_id,
            candidate["user_id"],
            content,
            candidate["source_turn_id"],
            now,
            now,
            candidate["source_turn_id"],
            candidate["id"],
            _optional_column(candidate, "workspace_id"),
            candidate["user_id"],
            now,
        ),
    )
    return "memory_note", note_id


def _normalize_bullet(value_json: str) -> str:
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError:
        value = value_json
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value or "")
    text = text.replace("\n", " ").strip()
    if not text:
        raise CandidateTargetError("empty candidate value")
    if len(text) > MAX_BULLET_CHARS:
        raise CandidateTargetError(f"candidate note exceeds {MAX_BULLET_CHARS} characters")
    return text


def _stable_note_key(value_json: str, *, secret: bytes) -> str:
    normalized = _normalize_bullet(value_json).casefold()
    digest = hmac.new(
        secret,
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"note_{encoded[:26]}"


def _value_schema_from_json(value_json: str) -> str:
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError:
        return "text_v1"
    if isinstance(value, bool):
        return "boolean_v1"
    if isinstance(value, str):
        return "text_v1"
    if isinstance(value, (int, float)):
        return "number_v1"
    if isinstance(value, list):
        return "list_v1"
    if isinstance(value, dict):
        return "object_v1"
    return "null_v1"


def _candidate_from_row(row: aiosqlite.Row) -> GovernanceCandidate:
    return GovernanceCandidate(
        id=row["id"],
        user_id=row["user_id"],
        subject_type=row["subject_type"],
        kind=row["kind"],
        key=row["key"],
        value=json.loads(row["value_json"] or "null"),
        reason=row["reason"],
        source_turn_id=row["source_turn_id"],
        status=row["status"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        workspace_id=_optional_column(row, "workspace_id"),
        version=int(_optional_column(row, "version") or 1),
        decision_kind=_optional_column(row, "decision_kind"),
        approved_resource_type=_optional_column(row, "approved_resource_type"),
        approved_resource_id=_optional_column(row, "approved_resource_id"),
        supersedes=_candidate_supersedes(row),
        memory_type=_optional_column(row, "memory_type"),
    )


def _optional_column(row: aiosqlite.Row, name: str) -> Any:
    # sqlite Row membership checks values rather than column names.
    return row[name] if name in row.keys() else None  # noqa: SIM118


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")
