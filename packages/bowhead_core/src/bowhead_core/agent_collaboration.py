"""Durable collaboration questions under existing Turn and Agent authorities.

Internal storage boundary only: runtime admission, result access adapters, yielding,
model continuation, and UI publication must be wired before exposing messaging.
No model argument grants identity, access, ownership, or execution authority.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, require_live_worker_in_tx

Identity = Annotated[str, Field(min_length=1, max_length=128)]
# Adapters retain original caller/resource access and explicit result-sharing
# restrictions. A recipient's Tool whitelist does not restrict result consumption.
AuthorizeExchange = Callable[[aiosqlite.Connection, DocumentTurnOwner, str, str, dict], Awaitable[None]]


async def authorize_result_exchange(db: aiosqlite.Connection, worker: DocumentTurnOwner, sender: str, recipient: str, content: dict) -> None:
    """Reuse original-caller execution and result-attachment authority.

    Artifact IDs identify immutable result versions. S1 evidence references use
    that exact ID for both identity and version; replacement artifacts get new
    IDs. Unsupported reference kinds fail closed without granting Tool access.
    Plain professional replies remain valid without universal provenance data.
    """
    from bowhead_core.chat_attachment_access import ChatAttachmentAccessError, validate_chat_attachment_access_in_connection

    for execution_id in {sender, recipient}:
        if (
            await _row(
                db,
                """SELECT 1 FROM turn_agent_execution e JOIN turns t ON t.id=e.turn_id
                 JOIN sessions s ON s.id=t.session_id
                 WHERE e.id=? AND e.turn_id=? AND e.team_id=? AND s.user_id=? AND s.id=?""",
                (execution_id, worker.turn_id, worker.team_id, worker.user_id, worker.session_id),
            )
            is None
        ):
            raise CollaborationUnavailable("collaboration_material_unavailable")
    refs = []
    for reference in content.get("references", []):
        identity = reference.get("identity", "")
        if not identity.startswith("art_") or reference.get("version") != identity:
            raise CollaborationUnavailable("collaboration_material_unavailable")
        row = await _row(db, "SELECT id,filename,mime,size FROM artifacts WHERE id=? AND status='ready'", (identity,))
        if row is None:
            raise CollaborationUnavailable("collaboration_material_unavailable")
        refs.append({"fileId": row["id"], "filename": row["filename"], "mime": row["mime"], "size": row["size"], "referenceKind": "result"})
    try:
        await validate_chat_attachment_access_in_connection(
            db,
            refs=refs,
            runtime_ids=list(dict.fromkeys(ref["fileId"] for ref in refs)),
            user_id=worker.user_id,
            workspace_id=worker.workspace_id,
            session_id=worker.session_id,
        )
    except ChatAttachmentAccessError as exc:
        raise CollaborationUnavailable("collaboration_material_unavailable") from exc


# Server-authored limitations for required questions that will never be answered.
EXPIRED_QUESTION_LIMITATION = (
    "The response deadline expired without an answer. This dependency is closed as a limitation: "
    "continue without the answer and state the limitation in your result."
)
UNANSWERED_OWNER_LIMITATION = (
    "The task owner could not answer this question. This dependency is closed as a limitation: "
    "continue without the answer and state the limitation in your result."
)


class CollaborationUnavailable(ValueError):
    def __init__(self, code: str = "collaboration_unavailable") -> None:
        self.code = code
        super().__init__(code)


class OperationIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    execution_id: Identity
    frame_id: Identity
    model_step_id: Identity
    native_call_id: Identity

    def key(self) -> tuple[str, str, str, str]:
        return self.execution_id, self.frame_id, self.model_step_id, self.native_call_id


class CollaborationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    max_questions: int = Field(default=32, ge=1, le=32)
    max_open: int = Field(default=8, ge=1, le=8)
    max_open_per_requester: int = Field(default=2, ge=1, le=2)
    max_followups: int = Field(default=2, ge=0, le=2)
    max_operations: int = Field(default=256, ge=3, le=256)
    max_body_bytes: int = Field(default=8192, ge=1, le=8192)
    max_references: int = Field(default=8, ge=0, le=8)
    max_reference_bytes: int = Field(default=2048, ge=2, le=2048)
    question_seconds: int = Field(default=60, ge=1, le=60)
    task_seconds: int = Field(default=900, ge=1, le=900)
    max_model_attempts: int = Field(default=100, ge=1, le=100)
    max_tool_attempts: int = Field(default=256, ge=1, le=256)
    max_coordination_attempts: int = Field(default=8, ge=1, le=8)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    identity: Identity
    version: Identity


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


async def _row(db: aiosqlite.Connection, sql: str, args: tuple = ()) -> aiosqlite.Row | None:
    async with db.execute(sql, args) as cursor:
        cursor.row_factory = aiosqlite.Row
        return await cursor.fetchone()


def _publication(row, turn_id: str) -> dict:
    return {
        "event_id": row["event_id"],
        "event_type": "agent.collaboration",
        "occurred_at": row["created_at"],
        "payload": {**json.loads(row["receipt_json"]), "card_id": turn_id, "collaboration_revision": row["seq"]},
    }


async def recover_collaboration_publications_in_connection(db: aiosqlite.Connection, *, turn_id: str) -> int:
    """Drain accepted facts inside the existing expired-dispatch transaction.

    Recovery already owns the original Turn fence and ordered terminal write.
    It publishes no private content, decrypts no evidence and starts no work.
    """
    from bowhead_core.turn_log_store import _semantic_payload

    if not db.in_transaction:
        raise CollaborationUnavailable("collaboration_recovery_transaction_required")
    if await _row(db, "SELECT 1 FROM sqlite_master WHERE name='agent_collaboration_operation'") is None:
        return 0
    async with db.execute("SELECT * FROM agent_collaboration_operation WHERE turn_id=? AND published=0 ORDER BY seq", (turn_id,)) as cursor:
        cursor.row_factory = aiosqlite.Row
        pending = await cursor.fetchall()
    inserted = 0
    for row in pending:
        intent = _publication(row, turn_id)
        existing = await _row(db, "SELECT * FROM turn_events WHERE id=?", (intent["event_id"],))
        if existing is not None:
            if (
                existing["turn_id"] != turn_id
                or existing["event_type"] != intent["event_type"]
                or _semantic_payload(json.loads(existing["payload_json"])) != intent["payload"]
            ):
                raise CollaborationUnavailable("collaboration_publication_unproven")
        else:
            seq = (await _row(db, "SELECT COALESCE(MAX(seq),0)+1 FROM turn_events WHERE turn_id=?", (turn_id,)))[0]
            await db.execute(
                "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
                (
                    intent["event_id"],
                    turn_id,
                    seq,
                    intent["event_type"],
                    _json({**intent["payload"], "seq": seq, "evt_seq": seq}),
                    intent["occurred_at"],
                ),
            )
            inserted += 1
        await db.execute("UPDATE agent_collaboration_operation SET published=1 WHERE id=?", (row["id"],))
    return inserted


class CollaborationStore:
    """Use the existing core database, scope-bound cipher, and dispatch fence."""

    def __init__(self, db_path: str | Path, *, cipher: ReferenceCipher, clock: Callable[[], float] = time.time) -> None:
        self.db_path = str(db_path)
        self.cipher = cipher
        self.clock = clock

    @asynccontextmanager
    async def _transaction(self, worker: DocumentTurnOwner, *, write: bool = True, history: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        worker = DocumentTurnOwner.model_validate(worker.model_dump())
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                if write:
                    try:
                        await require_live_worker_in_tx(db, worker=worker)
                    except OrdinaryContinuationUnavailable as exc:
                        raise CollaborationUnavailable("collaboration_worker_unavailable") from exc
                else:
                    await self._require_read_scope(db, worker, history=history)
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    def _request_scope(worker: DocumentTurnOwner) -> ReferenceRequestScope:
        return ReferenceRequestScope(**worker.model_dump(exclude={"turn_id", "generation", "owner_id"}))

    async def _require_read_scope(self, db: aiosqlite.Connection, worker: DocumentTurnOwner, *, history: bool = False) -> None:
        await require_reference_request_scope(db, self._request_scope(worker), allow_archived=history)
        if await _row(db, "SELECT 1 FROM turns WHERE id=? AND session_id=?", (worker.turn_id, worker.session_id)) is None:
            raise CollaborationUnavailable()

    def _seal(self, worker: DocumentTurnOwner, identity: str, content: dict) -> str:
        return self.cipher.seal(
            scope=ReferenceScope(**self._request_scope(worker).model_dump(), policy_epoch=0),
            purpose="agent-collaboration.v1",
            identity=identity,
            content=_json(content).encode(),
        )

    def _open(self, worker: DocumentTurnOwner, identity: str, ciphertext: str) -> dict:
        raw = self.cipher.open(
            scope=ReferenceScope(**self._request_scope(worker).model_dump(), policy_epoch=0),
            purpose="agent-collaboration.v1",
            identity=identity,
            ciphertext=ciphertext,
        )
        return json.loads(raw)

    async def admit(self, worker: DocumentTurnOwner, *, owner_execution_id: str, policy: CollaborationPolicy) -> None:
        """Called by trusted admission, never by a communication Tool."""
        policy = CollaborationPolicy.model_validate(policy.model_dump())
        async with self._transaction(worker) as db:
            existing = await _row(db, "SELECT * FROM agent_collaboration_turn WHERE turn_id=?", (worker.turn_id,))
            if existing is not None:
                if (existing["owner_execution_id"], existing["policy_json"]) != (owner_execution_id, _json(policy.model_dump())):
                    raise CollaborationUnavailable("collaboration_admission_conflict")
                return
            owner = await self._execution(db, worker, owner_execution_id)
            if owner["runtime_profile_name"] != "lead" or owner["agent_source_kind"] != "system":
                raise CollaborationUnavailable("collaboration_owner_invalid")
            await db.execute(
                "INSERT INTO agent_collaboration_turn (turn_id,protocol_version,owner_execution_id,policy_json,deadline,created_at) VALUES (?,1,?,?,?,?)",
                (worker.turn_id, owner_execution_id, _json(policy.model_dump()), self.clock() + policy.task_seconds, self._timestamp()),
            )

    async def admission(self, worker: DocumentTurnOwner) -> dict | None:
        async with self._transaction(worker, write=False) as db:
            row = await _row(db, "SELECT * FROM agent_collaboration_turn WHERE turn_id=?", (worker.turn_id,))
            return dict(row) if row is not None else None

    async def retained_operations_in_connection(self, db, worker: DocumentTurnOwner, *, execution_id: str, authorize: AuthorizeExchange) -> dict:
        """Reauthorize the original exchange ledger for an existing checkpoint.

        The checkpoint retains its original model transcript. This digest and
        the inbox cursor bind that transcript to accepted operations; neither
        reading nor restoring them sends a message or replays a Tool.
        """
        await self._require_read_scope(db, worker)
        task, _ = await self._task(db, worker)
        if task["owner_execution_id"] != execution_id:
            raise CollaborationUnavailable("collaboration_owner_invalid")
        async with db.execute("SELECT * FROM agent_collaboration_operation WHERE turn_id=? ORDER BY seq", (worker.turn_id,)) as cursor:
            cursor.row_factory = aiosqlite.Row
            rows = await cursor.fetchall()
        retained = []
        for row in rows:
            if row["action"] in {"ask", "reply", "update"}:
                await authorize(db, worker, row["execution_id"], row["recipient_id"], self._open(worker, row["id"], row["content_ciphertext"]))
            operation = dict(row)
            operation.pop("published")
            retained.append(operation)
        return {"operation_revision": rows[-1]["seq"] if rows else 0, "operation_hash": _hash(retained)}

    async def join(self, worker: DocumentTurnOwner, *, execution_id: str, assignment: str, expected_output: str, assistance_boundary: str) -> None:
        content = {"assignment": assignment, "expected_output": expected_output, "assistance_boundary": assistance_boundary}
        if any(not isinstance(value, str) or not value.strip() or len(value.encode()) > 4096 for value in content.values()):
            raise CollaborationUnavailable("collaboration_assignment_invalid")
        async with self._transaction(worker) as db:
            await self._task(db, worker)
            await self._execution(db, worker, execution_id)
            existing = await _row(db, "SELECT * FROM agent_collaboration_participant WHERE execution_id=?", (execution_id,))
            if existing is not None:
                if existing["turn_id"] != worker.turn_id or existing["assignment_hash"] != _hash(content):
                    raise CollaborationUnavailable("collaboration_assignment_conflict")
                return
            count = await _row(db, "SELECT COUNT(*) FROM agent_collaboration_participant WHERE turn_id=?", (worker.turn_id,))
            if count[0] >= 64:
                raise CollaborationUnavailable("collaboration_participant_limit")
            await db.execute(
                "INSERT INTO agent_collaboration_participant (execution_id,turn_id,assignment_ciphertext,assignment_hash) VALUES (?,?,?,?)",
                (execution_id, worker.turn_id, self._seal(worker, execution_id, content), _hash(content)),
            )

    async def ask(
        self,
        worker: DocumentTurnOwner,
        *,
        identity: OperationIdentity,
        recipient_id: str,
        body: str,
        references: tuple[EvidenceReference, ...] = (),
        required: bool = True,
        follow_up_to: str | None = None,
        authorize: AuthorizeExchange,
    ) -> dict:
        identity = OperationIdentity.model_validate(identity.model_dump())
        content = self._content(body, references)
        arguments = {"action": "ask", "recipient_id": recipient_id, **content, "required": required, "follow_up_to": follow_up_to}
        if type(required) is not bool or identity.execution_id == recipient_id:
            raise CollaborationUnavailable("collaboration_question_invalid")
        async with self._transaction(worker) as db:
            task, policy = await self._task(db, worker)
            await self._participant(db, worker, task["owner_execution_id"])
            self._check_content(content, policy)
            await authorize(db, worker, identity.execution_id, recipient_id, content)
            duplicate = await self._duplicate(db, worker, identity, arguments)
            if duplicate is not None:
                return duplicate
            await self._participant(db, worker, identity.execution_id)
            await self._participant(db, worker, recipient_id)
            counts = await _row(
                db,
                """SELECT COUNT(*) AS total, SUM(required=1 AND dependency_status='open') AS pending,
                     SUM(required=1 AND dependency_status='open' AND execution_id=?) AS own
                     FROM agent_collaboration_operation WHERE turn_id=? AND action='ask'""",
                (identity.execution_id, worker.turn_id),
            )
            if counts["total"] >= policy.max_questions or (
                required and ((counts["pending"] or 0) >= policy.max_open or (counts["own"] or 0) >= policy.max_open_per_requester)
            ):
                raise CollaborationUnavailable("collaboration_question_limit")
            deadline = min(task["deadline"], self.clock() + policy.question_seconds)
            family_id = None
            if follow_up_to is not None:
                previous = await self._question(db, worker, follow_up_to)
                if previous["execution_id"] != identity.execution_id:
                    raise CollaborationUnavailable("collaboration_question_unavailable")
                family_id = previous["family_id"] or previous["id"]
                family = await self._question(db, worker, family_id)
                count = await _row(db, "SELECT COUNT(*) AS n FROM agent_collaboration_operation WHERE family_id=?", (family_id,))
                if count["n"] >= policy.max_followups or family["deadline"] <= self.clock():
                    raise CollaborationUnavailable("collaboration_followup_limit")
                deadline = min(deadline, family["deadline"])
            await self._capacity(db, worker, policy, additional=3 if required else 1)
            return await self._record(
                db,
                worker,
                identity,
                arguments,
                recipient_id=recipient_id,
                content=content,
                family_id=family_id,
                required=required,
                deadline=deadline,
            )

    async def reply(
        self,
        worker: DocumentTurnOwner,
        *,
        identity: OperationIdentity,
        question_id: str,
        body: str,
        references: tuple[EvidenceReference, ...] = (),
        unavailable: bool = False,
        authorize: AuthorizeExchange,
    ) -> dict:
        identity = OperationIdentity.model_validate(identity.model_dump())
        content = self._content(body, references)
        arguments = {"action": "reply", "question_id": question_id, **content, "unavailable": unavailable}
        if type(unavailable) is not bool:
            raise CollaborationUnavailable("collaboration_reply_invalid")
        async with self._transaction(worker) as db:
            _, policy = await self._task(db, worker)
            self._check_content(content, policy)
            question = await self._question(db, worker, question_id)
            if question["recipient_id"] != identity.execution_id:
                raise CollaborationUnavailable("collaboration_question_unavailable")
            await authorize(db, worker, identity.execution_id, question["execution_id"], content)
            duplicate = await self._duplicate(db, worker, identity, arguments)
            if duplicate is not None:
                return duplicate
            await self._participant(db, worker, identity.execution_id)
            await self._participant(db, worker, question["execution_id"])
            if question["response_status"] != "pending" or question["deadline"] <= self.clock():
                raise CollaborationUnavailable("collaboration_reply_closed")
            await self._capacity(db, worker, policy, additional=0 if question["required"] else 1)
            receipt = await self._record(
                db, worker, identity, arguments, recipient_id=question["execution_id"], content=content, question_id=question_id
            )
            await db.execute(
                "UPDATE agent_collaboration_operation SET response_status=?,reply_id=? WHERE id=?",
                ("unavailable" if unavailable else "answered", receipt["operation_id"], question_id),
            )
            return receipt

    async def resolve(
        self,
        worker: DocumentTurnOwner,
        *,
        identity: OperationIdentity,
        question_id: str,
        reply_id: str | None,
        disposition: Literal["resolved", "waived", "blocked"],
        reason: str,
        authorize: AuthorizeExchange,
    ) -> dict:
        identity = OperationIdentity.model_validate(identity.model_dump())
        content = self._content(reason, ())
        arguments = {"action": "resolve", "question_id": question_id, "reply_id": reply_id, "disposition": disposition, **content}
        if disposition not in {"resolved", "waived", "blocked"}:
            raise CollaborationUnavailable("collaboration_resolution_invalid")
        async with self._transaction(worker) as db:
            task, policy = await self._task(db, worker)
            self._check_content(content, policy)
            question = await self._question(db, worker, question_id)
            expected = question["execution_id"] if disposition == "resolved" else task["owner_execution_id"]
            if identity.execution_id != expected:
                raise CollaborationUnavailable("collaboration_resolution_forbidden")
            await authorize(db, worker, identity.execution_id, question["execution_id"], content)
            # Verification reads protected evidence. An owner can record a
            # limitation after source revocation without reading that material.
            for operation_id in (question_id, reply_id) if disposition == "resolved" else ():
                if operation_id is not None:
                    await self._read_in_tx(db, worker, operation_id, authorize=authorize)
            duplicate = await self._duplicate(db, worker, identity, arguments)
            if duplicate is not None:
                return duplicate
            await self._participant(db, worker, identity.execution_id)
            if question["dependency_status"] != "open":
                raise CollaborationUnavailable("collaboration_dependency_closed")
            if disposition == "resolved":
                correction = await _row(
                    db,
                    "SELECT id FROM agent_collaboration_operation WHERE turn_id=? AND question_id=? AND action='update' ORDER BY seq DESC LIMIT 1",
                    (worker.turn_id, question_id),
                )
                current_evidence = correction[0] if correction else question["reply_id"]
                if question["response_status"] != "answered" or not reply_id or reply_id != current_evidence:
                    raise CollaborationUnavailable("collaboration_evidence_unavailable")
            await self._capacity(db, worker, policy, additional=0)
            receipt = await self._record(
                db, worker, identity, arguments, recipient_id=question["execution_id"], content=content, question_id=question_id
            )
            await db.execute(
                """UPDATE agent_collaboration_operation SET dependency_status=?,
                     response_status=CASE WHEN response_status='pending' THEN 'unavailable' ELSE response_status END WHERE id=?""",
                (disposition, question_id),
            )
            return receipt

    async def update(
        self,
        worker: DocumentTurnOwner,
        *,
        identity: OperationIdentity,
        recipient_id: str,
        body: str,
        references: tuple[EvidenceReference, ...] = (),
        supersedes: str | None = None,
        authorize: AuthorizeExchange,
    ) -> dict:
        """Correct an exact reply without rewriting it or reopening its author.

        A completed consumer's unresolved requirement belongs to the owner.
        The original completion is retained, but cannot prove root completion.
        """
        identity = OperationIdentity.model_validate(identity.model_dump())
        content = self._content(body, references)
        arguments = {"action": "update", "recipient_id": recipient_id, "supersedes": supersedes, **content}
        async with self._transaction(worker) as db:
            task, policy = await self._task(db, worker)
            self._check_content(content, policy)
            if identity.execution_id == recipient_id:
                raise CollaborationUnavailable("collaboration_recipient_unavailable")
            recipient = await _row(
                db,
                """SELECT e.status FROM agent_collaboration_participant p JOIN turn_agent_execution e ON e.id=p.execution_id
                     WHERE p.execution_id=? AND p.turn_id=?""",
                (recipient_id, worker.turn_id),
            )
            if recipient is None:
                raise CollaborationUnavailable("collaboration_recipient_unavailable")
            target = recipient_id if recipient["status"] in {"admitted", "running"} else task["owner_execution_id"]
            if target != recipient_id and supersedes is None:
                raise CollaborationUnavailable("collaboration_recipient_unavailable")
            await authorize(db, worker, identity.execution_id, target, content)
            duplicate = await self._duplicate(db, worker, identity, arguments)
            if duplicate is not None:
                return duplicate
            await self._participant(db, worker, identity.execution_id)
            await self._participant(db, worker, target)
            question = None
            if supersedes is not None:
                old = await _row(
                    db,
                    """SELECT * FROM agent_collaboration_operation WHERE id=? AND turn_id=? AND execution_id=?
                         AND ((action='reply' AND recipient_id=?) OR
                         (action='update' AND question_id IS NOT NULL AND json_extract(receipt_json,'$.affected_recipient_id')=?))""",
                    (supersedes, worker.turn_id, identity.execution_id, recipient_id, recipient_id),
                )
                if old is None:
                    raise CollaborationUnavailable("collaboration_evidence_unavailable")
                question = await self._question(db, worker, old["question_id"])
                already = await _row(
                    db,
                    "SELECT 1 FROM agent_collaboration_operation WHERE turn_id=? AND action='update' AND json_extract(receipt_json,'$.supersedes')=?",
                    (worker.turn_id, supersedes),
                )
                if already is not None:
                    raise CollaborationUnavailable("collaboration_evidence_superseded")
            # Reopening a previously resolved dependency reserves the owner's
            # eventual disposition even when communication is nearly exhausted.
            reopen = question is not None and question["dependency_status"] == "resolved"
            await self._capacity(db, worker, policy, additional=2 if reopen else 1)
            receipt = await self._record(
                db,
                worker,
                identity,
                arguments,
                recipient_id=target,
                content=content,
                question_id=question["id"] if question is not None else None,
            )
            if reopen:
                await db.execute("UPDATE agent_collaboration_operation SET dependency_status='open' WHERE id=?", (question["id"],))
            return receipt

    async def reserve_attempt(
        self,
        worker: DocumentTurnOwner,
        *,
        identity: OperationIdentity,
        kind: Literal["model", "tool"],
        coordination: bool = False,
    ) -> dict:
        """Reserve real work, including retries, within the original task budget.

        This is an admission hold. Existing physical usage receipts remain the
        only billing authority. Reservations are never refunded on recovery.
        """
        identity = OperationIdentity.model_validate(identity.model_dump())
        if kind not in {"model", "tool"} or type(coordination) is not bool:
            raise CollaborationUnavailable("collaboration_attempt_invalid")
        arguments = {"action": f"{kind}_attempt", "coordination": coordination}
        async with self._transaction(worker) as db:
            task, policy = await self._task(db, worker)
            duplicate = await self._duplicate(db, worker, identity, arguments)
            if duplicate is not None:
                return duplicate
            await self._participant(db, worker, identity.execution_id)
            participant = await _row(
                db, "SELECT coordination_attempts FROM agent_collaboration_participant WHERE execution_id=?", (identity.execution_id,)
            )
            limit = policy.max_model_attempts if kind == "model" else policy.max_tool_attempts
            if task[f"{kind}_attempts"] >= limit or (kind == "model" and coordination and participant[0] >= policy.max_coordination_attempts):
                raise CollaborationUnavailable("collaboration_effort_exhausted")
            # kind is a closed server enum, never an arbitrary SQL identifier.
            await db.execute(f"UPDATE agent_collaboration_turn SET {kind}_attempts={kind}_attempts+1 WHERE turn_id=?", (worker.turn_id,))
            if kind == "model" and coordination:
                await db.execute(
                    "UPDATE agent_collaboration_participant SET coordination_attempts=coordination_attempts+1 WHERE execution_id=?",
                    (identity.execution_id,),
                )
            receipt = await self._record(
                db, worker, identity, arguments, recipient_id=identity.execution_id, content={"body": "Execution admission hold.", "references": []}
            )
            await db.execute("UPDATE agent_collaboration_operation SET published=1 WHERE id=?", (receipt["operation_id"],))
            return receipt

    async def roster(self, worker: DocumentTurnOwner, *, execution_id: str, authorize: AuthorizeExchange) -> list[dict]:
        """Project accepted responsibilities, not callable profile candidates."""
        async with self._transaction(worker, write=False) as db:
            task, _ = await self._task(db, worker)
            await self._participant(db, worker, execution_id)
            async with db.execute(
                """SELECT p.*, e.status,e.runtime_profile_name FROM agent_collaboration_participant p
                     JOIN turn_agent_execution e ON e.id=p.execution_id WHERE p.turn_id=? ORDER BY p.execution_id LIMIT 64""",
                (worker.turn_id,),
            ) as cursor:
                cursor.row_factory = aiosqlite.Row
                rows = await cursor.fetchall()
            cards = []
            for row in rows:
                assignment = self._open(worker, row["execution_id"], row["assignment_ciphertext"])
                # Assignment text can itself contain inherited source material.
                await authorize(db, worker, task["owner_execution_id"], execution_id, {"body": _json(assignment), "references": []})
                cards.append(
                    {
                        "execution_id": row["execution_id"],
                        "profile": row["runtime_profile_name"],
                        "is_owner": row["execution_id"] == task["owner_execution_id"],
                        "status": row["status"],
                        "waiting": row["wait_operation_id"] is not None,
                        **assignment,
                    }
                )
            return cards

    async def inbox(self, worker: DocumentTurnOwner, *, execution_id: str, after: int = 0, authorize: AuthorizeExchange) -> dict:
        if type(after) is not int or after < 0:
            raise CollaborationUnavailable("collaboration_cursor_invalid")
        async with self._transaction(worker, write=False) as db:
            await self._participant(db, worker, execution_id)
            return await self._inbox_in_tx(db, worker, execution_id, after, authorize)

    async def obligations(self, worker: DocumentTurnOwner, *, execution_id: str) -> list[dict]:
        """Explain the same completion barrier enforced by the database trigger.

        This read guides the model; the final status transition remains atomic
        against newly accepted questions. It does not grant completion authority.
        """
        async with self._transaction(worker) as db:
            task, _ = await self._task(db, worker)
            await self._participant(db, worker, execution_id)
            async with db.execute(
                """SELECT id AS question_id,execution_id AS requester_id,recipient_id,
                          response_status,dependency_status,reply_id
                     FROM agent_collaboration_operation
                     WHERE turn_id=? AND action='ask' AND required=1 AND (
                       (recipient_id=? AND response_status='pending') OR
                       (execution_id=? AND dependency_status='open') OR
                       (?=1 AND (response_status='pending' OR dependency_status='open')))
                     ORDER BY seq LIMIT 32""",
                (worker.turn_id, execution_id, execution_id, int(execution_id == task["owner_execution_id"])),
            ) as cursor:
                cursor.row_factory = aiosqlite.Row
                return [dict(row) for row in await cursor.fetchall()]

    async def complete(self, worker: DocumentTurnOwner, *, execution_id: str) -> None:
        """Settle the existing execution before publishing successful completion.

        The worker fence and question/finish race share this transaction. The
        existing execution trigger remains the final obligation authority.
        """
        async with self._transaction(worker) as db:
            await self._task(db, worker)
            await self._participant(db, worker, execution_id)
            try:
                updated = await db.execute(
                    """UPDATE turn_agent_execution SET status='completed',ended_at=COALESCE(ended_at,?),failure_code=NULL
                         WHERE id=? AND turn_id=? AND status='running'""",
                    (self._timestamp(), execution_id, worker.turn_id),
                )
            except aiosqlite.IntegrityError as exc:
                if "unresolved collaboration obligations" in str(exc):
                    raise CollaborationUnavailable("collaboration_unresolved") from exc
                raise
            if updated.rowcount != 1:
                raise CollaborationUnavailable("collaboration_execution_unavailable")

    async def next_deadline(self, worker: DocumentTurnOwner, *, execution_id: str) -> float:
        """Derive a wake deadline from the original task and pending questions."""
        async with self._transaction(worker) as db:
            task, _ = await self._task(db, worker)
            await self._participant(db, worker, execution_id)
            row = await _row(
                db,
                """SELECT MIN(deadline) FROM agent_collaboration_operation WHERE turn_id=? AND action='ask'
                     AND response_status='pending' AND (execution_id=? OR recipient_id=?)""",
                (worker.turn_id, execution_id, execution_id),
            )
            return min(task["deadline"], row[0]) if row[0] is not None else task["deadline"]

    async def _inbox_in_tx(self, db, worker, execution_id, after, authorize) -> dict:
        async with db.execute(
            """SELECT * FROM agent_collaboration_operation WHERE turn_id=? AND recipient_id=? AND seq>?
                 AND action IN ('ask','reply','resolve','update','expire') ORDER BY seq LIMIT 256""",
            (worker.turn_id, execution_id, after),
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
            rows = await cursor.fetchall()
        items = []
        for row in rows:
            try:
                content = await self._authorized_content(db, worker, row, authorize)
            except CollaborationUnavailable:
                # No protected body, references, exception detail or fabricated
                # answer crosses the refusal boundary.
                content = {"unavailable": "collaboration_material_unavailable"}
            items.append(
                {
                    **json.loads(row["receipt_json"]),
                    **content,
                    "seq": row["seq"],
                    "response_status": row["response_status"],
                    "dependency_status": row["dependency_status"],
                }
            )
        return {"revision": rows[-1]["seq"] if rows else after, "items": items}

    async def begin_wait(
        self,
        worker: DocumentTurnOwner,
        *,
        identity: OperationIdentity,
        question_ids: tuple[str, ...],
        after: int,
        authorize: AuthorizeExchange,
    ) -> dict:
        """Register and inspect a bounded wait in the same write transaction."""
        identity = OperationIdentity.model_validate(identity.model_dump())
        if not question_ids or len(question_ids) > 2 or len(set(question_ids)) != len(question_ids) or type(after) is not int or after < 0:
            raise CollaborationUnavailable("collaboration_wait_invalid")
        # The cursor is captured server state, not part of the model's native
        # Tool arguments. A retry retains the original accepted cursor.
        arguments = {"action": "wait", "question_ids": list(question_ids)}
        async with self._transaction(worker) as db:
            _, policy = await self._task(db, worker)
            await self._participant(db, worker, identity.execution_id)
            duplicate = await self._duplicate(db, worker, identity, arguments)
            if duplicate is not None:
                return duplicate
            questions = [await self._question(db, worker, item) for item in question_ids]
            if any(row["execution_id"] != identity.execution_id or not row["required"] for row in questions):
                raise CollaborationUnavailable("collaboration_wait_invalid")
            inbox = await self._inbox_in_tx(db, worker, identity.execution_id, after, authorize)
            ready = bool(inbox["items"]) or any(
                row["response_status"] != "pending" or row["dependency_status"] != "open" or row["deadline"] <= self.clock() for row in questions
            )
            participant = await _row(
                db, "SELECT wait_operation_id FROM agent_collaboration_participant WHERE execution_id=?", (identity.execution_id,)
            )
            if participant[0] is not None:
                raise CollaborationUnavailable("collaboration_wait_already_active")
            await self._capacity(db, worker, policy, additional=1)
            receipt = await self._record(
                db,
                worker,
                identity,
                arguments,
                recipient_id=identity.execution_id,
                content={"body": "Waiting for required evidence.", "references": [], "question_ids": list(question_ids), "after": after},
            )
            if not ready:
                await db.execute(
                    "UPDATE agent_collaboration_participant SET wait_operation_id=?,wait_revision=? WHERE execution_id=?",
                    (receipt["operation_id"], after, identity.execution_id),
                )
            return receipt

    async def wait_state(self, worker: DocumentTurnOwner, *, execution_id: str, operation_id: str, authorize: AuthorizeExchange) -> dict:
        """Read durable wake facts; a notification alone never resumes a model."""
        async with self._transaction(worker) as db:
            await self._participant(db, worker, execution_id)
            operation = await _row(
                db,
                "SELECT * FROM agent_collaboration_operation WHERE id=? AND turn_id=? AND execution_id=? AND action='wait'",
                (operation_id, worker.turn_id, execution_id),
            )
            if operation is None:
                raise CollaborationUnavailable("collaboration_wait_invalid")
            content = self._open(worker, operation_id, operation["content_ciphertext"])
            task = await _row(db, "SELECT deadline FROM agent_collaboration_turn WHERE turn_id=?", (worker.turn_id,))
            questions = [await self._question(db, worker, item) for item in content["question_ids"]]
            inbox = await self._inbox_in_tx(db, worker, execution_id, content["after"], authorize)
            deadline = min(task[0], *(row["deadline"] for row in questions))
            ready = (
                bool(inbox["items"])
                or any(row["response_status"] != "pending" or row["dependency_status"] != "open" for row in questions)
                or deadline <= self.clock()
            )
            if ready:
                await db.execute(
                    "UPDATE agent_collaboration_participant SET wait_operation_id=NULL,wait_revision=NULL WHERE execution_id=? AND wait_operation_id=?",
                    (execution_id, operation_id),
                )
            return {
                "ready": ready,
                "deadline": deadline,
                **inbox,
                "questions": [
                    {
                        "question_id": row["id"],
                        "response_status": row["response_status"],
                        "dependency_status": row["dependency_status"],
                        "reply_id": row["reply_id"],
                    }
                    for row in questions
                ],
            }

    async def read(self, worker: DocumentTurnOwner, *, operation_id: str, authorize: AuthorizeExchange) -> dict:
        """Authorized history read, never a resume or delivery acknowledgement."""
        async with self._transaction(worker, write=False) as db:
            return await self._read_in_tx(db, worker, operation_id, authorize=authorize)

    async def projection(self, worker: DocumentTurnOwner, *, authorize: AuthorizeExchange) -> dict:
        """Read bounded, currently authorized trace facts in one snapshot.

        The existing Turn outcome remains authoritative. Reading history never
        expires a question, acknowledges an inbox, or resumes an execution.
        Bodies stay out of replayable progress events and are read here instead.
        """
        async with self._transaction(worker, write=False, history=True) as db:
            task = await _row(db, "SELECT * FROM agent_collaboration_turn WHERE turn_id=?", (worker.turn_id,))
            if task is None:
                return {"turn_id": worker.turn_id, "revision": 0, "participants": [], "topics": []}
            turn = await _row(db, "SELECT status FROM turns WHERE id=?", (worker.turn_id,))
            async with db.execute(
                """SELECT p.*,e.status,e.runtime_profile_name FROM agent_collaboration_participant p
                     JOIN turn_agent_execution e ON e.id=p.execution_id WHERE p.turn_id=? ORDER BY e.created_at,e.id LIMIT 64""",
                (worker.turn_id,),
            ) as cursor:
                cursor.row_factory = aiosqlite.Row
                rows = await cursor.fetchall()
            participants = []
            for row in rows:
                assignment = self._open(worker, row["execution_id"], row["assignment_ciphertext"])
                try:
                    await authorize(db, worker, task["owner_execution_id"], row["execution_id"], {"body": _json(assignment), "references": []})
                except CollaborationUnavailable:
                    assignment = {"unavailable": "collaboration_material_unavailable"}
                participants.append(
                    {
                        "execution_id": row["execution_id"],
                        "profile": row["runtime_profile_name"],
                        "is_owner": row["execution_id"] == task["owner_execution_id"],
                        "status": row["status"],
                        "waiting": row["wait_operation_id"] is not None,
                        **assignment,
                    }
                )
            async with db.execute(
                """SELECT * FROM agent_collaboration_operation WHERE turn_id=?
                     AND action IN ('ask','reply','resolve','update','expire','wait') ORDER BY seq""",
                (worker.turn_id,),
            ) as cursor:
                cursor.row_factory = aiosqlite.Row
                operations = await cursor.fetchall()
            topics: dict[str, dict] = {}
            for row in operations:
                if row["action"] == "wait":
                    continue
                topic_id = row["question_id"] or row["id"]
                topic = topics.setdefault(topic_id, {"topic_id": topic_id, "messages": []})
                topic["revision"] = row["seq"]
                if row["action"] == "ask":
                    topic.update(
                        sender_id=row["execution_id"],
                        recipient_id=row["recipient_id"],
                        required=bool(row["required"]),
                        response_status=row["response_status"],
                        dependency_status=row["dependency_status"],
                        reply_id=row["reply_id"],
                    )
                try:
                    content = await self._authorized_content(db, worker, row, authorize)
                except CollaborationUnavailable:
                    content = {"unavailable": "collaboration_material_unavailable"}
                topic["messages"].append({**json.loads(row["receipt_json"]), **content, "created_at": row["created_at"]})
            return {
                "turn_id": worker.turn_id,
                "revision": operations[-1]["seq"] if operations else 0,
                "turn_status": turn["status"],
                "owner_execution_id": task["owner_execution_id"],
                "participants": participants,
                "topics": list(topics.values()),
            }

    async def _authorized_content(self, db, worker, row, authorize) -> dict:
        """Preserve explicit restrictions within a recorded exchange.

        Replies and verification reasons can summarize prior topic evidence.
        Keep those explicit references, replacing an earlier version only when
        a later message names a new version of the same source. This does not
        infer restrictions from Tool grants or unrelated model input.
        """
        content = self._open(worker, row["id"], row["content_ciphertext"])
        await authorize(db, worker, row["execution_id"], row["recipient_id"], content)
        receipt = json.loads(row["receipt_json"])
        if row["action"] in {"ask", "expire"} or (row["action"] == "resolve" and receipt["disposition"] in {"blocked", "waived"}):
            # A later reply cannot revoke an earlier unrestricted question.
            # The owner must also be able to report material unavailability
            # without reading that material or reopening the dependency.
            return content
        topic_id = row["question_id"] or row["id"]
        async with db.execute(
            "SELECT * FROM agent_collaboration_operation WHERE turn_id=? AND (id=? OR question_id=?) AND seq<? ORDER BY seq",
            (worker.turn_id, topic_id, topic_id, row["seq"]),
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
            related = await cursor.fetchall()
        references = {}
        for source in related:
            source_content = self._open(worker, source["id"], source["content_ciphertext"])
            for reference in source_content.get("references", []):
                references[reference["identity"]] = reference
        for reference in content.get("references", []):
            references[reference["identity"]] = reference
        inherited = {**content, "references": list(references.values())}
        await authorize(db, worker, row["execution_id"], row["recipient_id"], inherited)
        return inherited

    async def expire(self, worker: DocumentTurnOwner) -> list[dict]:
        """Close expired answering obligations without claiming dependency resolution.

        Expiry is deterministic server work and consumes no model or message
        allowance. It remains available when the task's effort/time is exhausted.
        An open required dependency closes as `blocked`: a recorded limitation,
        never evidence. Otherwise nobody is prompted to close it, and the
        requester could never complete (116 t_649dfe4abb48).
        """
        async with self._transaction(worker) as db:
            task = await _row(db, "SELECT * FROM agent_collaboration_turn WHERE turn_id=?", (worker.turn_id,))
            if task is None:
                raise CollaborationUnavailable()
            async with db.execute(
                """SELECT * FROM agent_collaboration_operation WHERE turn_id=? AND action='ask'
                     AND response_status='pending' AND deadline<=? ORDER BY seq""",
                (worker.turn_id, self.clock()),
            ) as cursor:
                cursor.row_factory = aiosqlite.Row
                questions = await cursor.fetchall()
            receipts = []
            for question in questions:
                identity = OperationIdentity(
                    execution_id=task["owner_execution_id"],
                    frame_id="server",
                    model_step_id="expiry",
                    native_call_id=question["id"],
                )
                arguments = {"action": "expire", "question_id": question["id"]}
                receipt = await self._record(
                    db,
                    worker,
                    identity,
                    arguments,
                    recipient_id=question["execution_id"],
                    content={"body": EXPIRED_QUESTION_LIMITATION, "references": []},
                    question_id=question["id"],
                )
                await db.execute(
                    """UPDATE agent_collaboration_operation SET response_status='unavailable',
                         dependency_status=CASE WHEN dependency_status='open' THEN 'blocked' ELSE dependency_status END WHERE id=?""",
                    (question["id"],),
                )
                receipts.append(receipt)
            return receipts

    async def close_unanswered_owner_questions(self, worker: DocumentTurnOwner, *, question_ids: tuple[str, ...]) -> list[dict]:
        """Close required questions the task owner left pending after its coordination round.

        The owner's own disposition authority records an unavailable reply and a
        `blocked` limitation in one transaction, so the waiting requester resumes
        at once instead of waiting for expiry. Answered or closed questions, other
        recipients and optional questions are untouched; replays are idempotent.
        """
        async with self._transaction(worker) as db:
            task, _ = await self._task(db, worker)
            owner = task["owner_execution_id"]
            content = {"body": UNANSWERED_OWNER_LIMITATION, "references": []}
            receipts = []
            for question_id in dict.fromkeys(question_ids):
                question = await _row(
                    db,
                    "SELECT * FROM agent_collaboration_operation WHERE id=? AND turn_id=? AND action='ask'",
                    (question_id, worker.turn_id),
                )
                if question is None or question["recipient_id"] != owner or not question["required"] or question["response_status"] != "pending":
                    continue
                for step, arguments in (
                    ("owner_fallback_reply", {"action": "reply", "question_id": question_id, **content, "unavailable": True}),
                    (
                        "owner_fallback_resolve",
                        {"action": "resolve", "question_id": question_id, "reply_id": None, "disposition": "blocked", **content},
                    ),
                ):
                    identity = OperationIdentity(execution_id=owner, frame_id="server", model_step_id=step, native_call_id=question_id)
                    receipt = await self._duplicate(db, worker, identity, arguments) or await self._record(
                        db, worker, identity, arguments, recipient_id=question["execution_id"], content=content, question_id=question_id
                    )
                    receipts.append(receipt)
                await db.execute(
                    """UPDATE agent_collaboration_operation SET response_status='unavailable',reply_id=?,
                         dependency_status=CASE WHEN dependency_status='open' THEN 'blocked' ELSE dependency_status END WHERE id=?""",
                    (receipts[-2]["operation_id"], question_id),
                )
            return receipts

    async def _read_in_tx(self, db: aiosqlite.Connection, worker: DocumentTurnOwner, operation_id: str, *, authorize: AuthorizeExchange) -> dict:
        row = await _row(db, "SELECT * FROM agent_collaboration_operation WHERE id=? AND turn_id=?", (operation_id, worker.turn_id))
        if row is None:
            raise CollaborationUnavailable()
        content = await self._authorized_content(db, worker, row, authorize)
        return {
            **json.loads(row["receipt_json"]),
            **content,
            "response_status": row["response_status"],
            "dependency_status": row["dependency_status"],
        }

    async def pending_publications(self, worker: DocumentTurnOwner) -> list[dict]:
        """Return content-free intent for the existing ordered event publisher."""
        async with self._transaction(worker, write=False) as db:
            async with db.execute(
                "SELECT id,event_id,receipt_json,seq,created_at FROM agent_collaboration_operation WHERE turn_id=? AND published=0 ORDER BY seq LIMIT 256",
                (worker.turn_id,),
            ) as cursor:
                cursor.row_factory = aiosqlite.Row
                rows = await cursor.fetchall()
            return [_publication(row, worker.turn_id) for row in rows]

    async def acknowledge_publication(self, worker: DocumentTurnOwner, *, operation_id: str) -> None:
        # Projection recovery can finish after execution is terminal. It cannot
        # mutate questions or start another Agent/model execution.
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_read_scope(db, worker)
                row = await _row(
                    db,
                    """SELECT o.receipt_json,e.payload_json FROM agent_collaboration_operation o
                         JOIN turn_events e ON e.id=o.event_id AND e.turn_id=o.turn_id AND e.event_type='agent.collaboration'
                         WHERE o.id=? AND o.turn_id=?""",
                    (operation_id, worker.turn_id),
                )
                if row is None or any(json.loads(row[1]).get(key) != value for key, value in json.loads(row[0]).items()):
                    raise CollaborationUnavailable("collaboration_publication_unproven")
                await db.execute("UPDATE agent_collaboration_operation SET published=1 WHERE id=?", (operation_id,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _task(self, db: aiosqlite.Connection, worker: DocumentTurnOwner) -> tuple[aiosqlite.Row, CollaborationPolicy]:
        row = await _row(db, "SELECT * FROM agent_collaboration_turn WHERE turn_id=?", (worker.turn_id,))
        if row is None or row["deadline"] <= self.clock():
            raise CollaborationUnavailable("collaboration_task_unavailable")
        await self._execution(db, worker, row["owner_execution_id"])
        return row, CollaborationPolicy.model_validate_json(row["policy_json"])

    @staticmethod
    async def _execution(db: aiosqlite.Connection, worker: DocumentTurnOwner, execution_id: str) -> aiosqlite.Row:
        row = await _row(
            db,
            "SELECT * FROM turn_agent_execution WHERE id=? AND turn_id=? AND team_id=? AND status IN ('admitted','running')",
            (execution_id, worker.turn_id, worker.team_id),
        )
        if row is None:
            raise CollaborationUnavailable("collaboration_recipient_unavailable")
        return row

    async def _participant(self, db: aiosqlite.Connection, worker: DocumentTurnOwner, execution_id: str) -> None:
        await self._execution(db, worker, execution_id)
        if await _row(db, "SELECT 1 FROM agent_collaboration_participant WHERE execution_id=? AND turn_id=?", (execution_id, worker.turn_id)) is None:
            raise CollaborationUnavailable("collaboration_recipient_unavailable")

    @staticmethod
    async def _question(db: aiosqlite.Connection, worker: DocumentTurnOwner, question_id: str) -> aiosqlite.Row:
        row = await _row(db, "SELECT * FROM agent_collaboration_operation WHERE id=? AND turn_id=? AND action='ask'", (question_id, worker.turn_id))
        if row is None:
            raise CollaborationUnavailable("collaboration_question_unavailable")
        return row

    @staticmethod
    async def _duplicate(db: aiosqlite.Connection, worker: DocumentTurnOwner, identity: OperationIdentity, arguments: dict) -> dict | None:
        row = await _row(
            db,
            "SELECT * FROM agent_collaboration_operation WHERE execution_id=? AND frame_id=? AND model_step_id=? AND native_call_id=?",
            identity.key(),
        )
        if row is None:
            return None
        if row["turn_id"] != worker.turn_id or row["arguments_hash"] != _hash(arguments):
            raise CollaborationUnavailable("collaboration_operation_conflict")
        return json.loads(row["receipt_json"])

    @staticmethod
    async def _capacity(db: aiosqlite.Connection, worker: DocumentTurnOwner, policy: CollaborationPolicy, *, additional: int) -> None:
        row = await _row(
            db,
            """SELECT COALESCE(SUM(action IN ('ask','reply','resolve','update','wait')),0) AS operations, COALESCE(SUM(CASE WHEN action='ask' AND required=1 THEN
                 (response_status='pending')+(dependency_status='open') ELSE 0 END),0) AS reserved
                 FROM agent_collaboration_operation WHERE turn_id=?""",
            (worker.turn_id,),
        )
        if row["operations"] + row["reserved"] + additional > policy.max_operations:
            raise CollaborationUnavailable("collaboration_operation_limit")

    @staticmethod
    def _content(body: str, references: tuple[EvidenceReference, ...]) -> dict:
        if not isinstance(body, str) or not body.strip():
            raise CollaborationUnavailable("collaboration_body_invalid")
        return {"body": body, "references": [EvidenceReference.model_validate(ref.model_dump()).model_dump() for ref in references]}

    @staticmethod
    def _check_content(content: dict, policy: CollaborationPolicy) -> None:
        if len(content["body"].encode()) > policy.max_body_bytes or len(content["references"]) > policy.max_references:
            raise CollaborationUnavailable("collaboration_payload_limit")
        if len(_json(content["references"]).encode()) > policy.max_reference_bytes:
            raise CollaborationUnavailable("collaboration_payload_limit")

    def _timestamp(self) -> str:
        return datetime.fromtimestamp(self.clock(), UTC).isoformat()

    async def _record(
        self,
        db: aiosqlite.Connection,
        worker: DocumentTurnOwner,
        identity: OperationIdentity,
        arguments: dict,
        *,
        recipient_id: str,
        content: dict,
        question_id: str | None = None,
        family_id: str | None = None,
        required: bool = False,
        deadline: float | None = None,
    ) -> dict:
        operation_id = "aco_" + _hash([worker.turn_id, *identity.key()])
        action = arguments["action"]
        receipt = {
            "operation_id": operation_id,
            "action": action,
            "question_id": question_id or operation_id,
            "sender_id": identity.execution_id,
            "recipient_id": recipient_id,
            "accepted": True,
        }
        if action == "ask":
            receipt["required"] = required
        elif action == "reply":
            receipt["outcome"] = "unavailable" if arguments["unavailable"] else "answered"
        elif action == "resolve":
            receipt.update(disposition=arguments["disposition"], reply_id=arguments["reply_id"])
        elif action == "expire":
            receipt["outcome"] = "unavailable"
        elif action == "update":
            receipt.update(supersedes=arguments["supersedes"], affected_recipient_id=arguments["recipient_id"])
        elif action == "wait":
            receipt["question_ids"] = arguments["question_ids"]
        await db.execute(
            """INSERT INTO agent_collaboration_operation
                 (id,turn_id,execution_id,recipient_id,frame_id,model_step_id,native_call_id,action,arguments_hash,
                  content_ciphertext,receipt_json,question_id,family_id,required,response_status,dependency_status,deadline,event_id,created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                worker.turn_id,
                identity.execution_id,
                recipient_id,
                *identity.key()[1:],
                action,
                _hash(arguments),
                self._seal(worker, operation_id, content),
                _json(receipt),
                question_id,
                family_id,
                int(required),
                "pending" if action == "ask" else None,
                "open" if required else None,
                deadline,
                "evt_" + operation_id,
                self._timestamp(),
            ),
        )
        return receipt
