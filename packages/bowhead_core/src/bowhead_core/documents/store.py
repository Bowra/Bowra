"""Immutable document admission and cancellation, separate from execution.

Production dispatch additionally requires the server-owned runtime admission.
Historical metadata-only jobs remain non-executable.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import ExactAgentExecutionBinding
from bowhead_core.chat_turn_source import ChatTurnUserSource, chat_turn_user_source_projection
from bowhead_core.documents.request import (
    DocumentModelSelection,
    DocumentProposal,
    DocumentRequestError,
    DocumentScope,
    UserQuote,
    resolve_proposal_positions,
    verify_proposal_source,
)
from bowhead_core.documents.revision_admission import PreparedRevision, commit_revision_in_tx, prepare_revision_in_tx
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

if TYPE_CHECKING:
    from bowhead_core.document_chat_continuation import DocumentTurnOwner
    from bowhead_core.documents.canvas_inputs import CanvasInputAdmission
    from bowhead_core.documents.continuation import DocumentRuntimeSnapshot


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DocumentReceipt:
    job_id: str
    proposal_revision: int


@dataclass(frozen=True, slots=True)
class DocumentRuntimeAdmission:
    """Server-owned readiness snapshot, never an argument exposed to a model."""

    image_id: str
    model_config_version: int
    binding: ExactAgentExecutionBinding
    owns_binding: bool = False
    turn_owner: DocumentTurnOwner | None = None
    turn_snapshot: DocumentRuntimeSnapshot | None = None
    model_selection: DocumentModelSelection | None = None

    def __post_init__(self):
        if self.model_selection is not None:
            object.__setattr__(self, "model_selection", DocumentModelSelection.model_validate_json(self.model_selection.model_dump_json()))
        if re.fullmatch(r"sha256:[a-f0-9]{64}", self.image_id) is None or type(self.model_config_version) is not int or self.model_config_version < 0:
            raise DocumentRequestError("document_runtime_unavailable")
        if (self.turn_owner is None) != (self.turn_snapshot is None):
            raise DocumentRequestError("document_runtime_unavailable")
        if self.turn_snapshot is not None and (self.turn_snapshot.image_id, self.turn_snapshot.model_config_version) != (
            self.image_id,
            self.model_config_version,
        ):
            raise DocumentRequestError("document_runtime_unavailable")


@dataclass(frozen=True, slots=True)
class _AnswerContext:
    parent_job_id: str
    question_digest: str
    instruction: str

    def as_json(self) -> dict[str, str]:
        return {"parent_job_id": self.parent_job_id, "question_digest": self.question_digest, "instruction": self.instruction}


@dataclass(frozen=True, slots=True)
class PreparedDocumentSubmission:
    """Read-only preparation, not authority to execute or skip commit checks."""

    proposal: DocumentProposal
    answer: _AnswerContext | None = None
    revision: PreparedRevision | None = None


async def _question(db, scope: DocumentScope, job_id: str):
    from bowhead_core.documents.delivery import _execution, _scope

    job = await _scope(db, scope, job_id, write=True)
    async with db.execute("SELECT 1 FROM document_turn_dispatches WHERE job_id=?", (job_id,)) as cursor:
        if await cursor.fetchone():
            raise DocumentRequestError("document_question_unavailable")
    execution = await _execution(db, job_id)
    if execution is None or not execution["question"] or not execution["context_json"]:
        raise DocumentRequestError("document_question_unavailable")
    context = json.loads(execution["context_json"])
    instruction = json.loads(context["payload"])["instruction"]
    digest = _digest(_json({"context": context, "question": execution["question"]}))
    return job, execution, instruction, digest


@dataclass(frozen=True, slots=True)
class DocumentJob:
    id: str
    status: str
    phase: str
    proposal_revision: int
    request_hash: str
    user_source_sha256: str
    proposal: DocumentProposal


async def _authorize(db: aiosqlite.Connection, scope: DocumentScope, *, write: bool) -> None:
    # Membership checks are current, not copied from the model or a stale turn.
    # Capability, runtime and model authorization are additional checks in
    # the production adapter; this owner-scope fence cannot replace them.
    async with db.execute(
        """SELECT w.status,wm.role FROM workspaces w JOIN teams t ON t.id=w.team_id
        JOIN team_members tm ON tm.team_id=t.id JOIN users u ON u.id=tm.user_id
        JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=u.id
        WHERE t.id=? AND w.id=? AND u.id=? AND t.status='active' AND u.status='active'
          AND tm.status='active' AND wm.status='active'""",
        scope.sql_params(),
    ) as cur:
        row = await cur.fetchone()
    from bowhead_core.space_governance import normalize_space_role

    if (
        row is None
        or row[0] not in (("active",) if write else ("active", "archived"))
        or (write and normalize_space_role(row[1]) not in {"admin", "member"})
    ):
        raise DocumentRequestError("document_not_found")


async def _receipt(db: aiosqlite.Connection, scope: DocumentScope, command_id: str, request_hash: str) -> DocumentReceipt | None:
    async with db.execute(
        "SELECT request_hash,result_json FROM document_command_receipts WHERE team_id=? AND workspace_id=? AND owner_user_id=? AND command_id=?",
        (*scope.sql_params(), command_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    if row[0] != request_hash:
        raise DocumentRequestError("document_command_conflict")
    return DocumentReceipt(**json.loads(row[1]))


async def _save_receipt(
    db: aiosqlite.Connection, scope: DocumentScope, *, command_id: str, kind: str, request_hash: str, job_id: str, now: str
) -> DocumentReceipt:
    result = DocumentReceipt(job_id=job_id, proposal_revision=1)
    await db.execute(
        """INSERT INTO document_command_receipts
        (team_id,workspace_id,owner_user_id,command_id,command_kind,request_hash,target_id,result_json,created_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (*scope.sql_params(), command_id, kind, request_hash, job_id, _json({"job_id": job_id, "proposal_revision": 1}), now),
    )
    return result


def _command_id(value: str) -> None:
    # Must be supplied by the trusted invocation/click adapter, never invented
    # by a model.
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise DocumentRequestError("document_command_invalid")


def _job(row: aiosqlite.Row) -> DocumentJob:
    return DocumentJob(
        id=row["id"],
        status=row["status"],
        phase=row["phase"],
        proposal_revision=row["proposal_revision"],
        request_hash=row["request_hash"],
        user_source_sha256=row["user_source_sha256"],
        proposal=DocumentProposal.model_validate_json(_json(json.loads(row["request_json"])["proposal"])),
    )


async def _live_source(db: aiosqlite.Connection, scope: DocumentScope, turn_id: str) -> ChatTurnUserSource:
    source = await _read_chat_turn_user_source_in_tx(db, turn_id=turn_id, expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id)
    if source is None:
        raise DocumentRequestError("document_source_unavailable")
    async with db.execute(
        """SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id
        WHERE t.id=? AND s.id=? AND s.user_id=? AND s.workspace_id=?
          AND t.user_text=? AND t.status='running' AND s.deleted_at IS NULL""",
        (turn_id, source.session_id, scope.user_id, scope.workspace_id, source.user_text),
    ) as cur:
        if await cur.fetchone() is None:
            raise DocumentRequestError("document_source_unavailable")
    return source


async def _validate_inputs(
    db: aiosqlite.Connection,
    scope: DocumentScope,
    source: ChatTurnUserSource,
    proposal: DocumentProposal,
    *,
    answer: _AnswerContext | None = None,
) -> None:
    verify_proposal_source(proposal, source)
    if answer is not None:
        from bowhead_core.documents.revision_admission import authorized_revision_inputs_in_tx, has_revision_inputs_in_tx

        if await has_revision_inputs_in_tx(db, answer.parent_job_id):
            parent, _, _, _ = await _question(db, scope, answer.parent_job_id)
            inputs = await authorized_revision_inputs_in_tx(db, job=parent)
            if tuple((item["id"], item["role"]) for item in inputs) != tuple((item.artifact_id, item.role) for item in proposal.inputs):
                raise DocumentRequestError("document_input_unavailable")
            return
    for item in proposal.inputs:
        # Provenance never replaces current file ownership and session access.
        async with db.execute(
            """SELECT 1 FROM artifacts a WHERE a.id=? AND a.created_by=? AND a.deleted_at IS NULL
            AND a.status='ready' AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)
            AND ((? IS NOT NULL AND EXISTS(SELECT 1 FROM turn_attachments ta WHERE ta.turn_id=? AND ta.artifact_id=a.id))
              OR (? IS NULL AND EXISTS(SELECT 1 FROM session_attachments sa
                  WHERE sa.session_id=? AND sa.artifact_id=a.id AND sa.detached_at IS NULL)))""",
            (item.artifact_id, scope.user_id, item.current_message_position, source.turn_id, item.current_message_position, source.session_id),
        ) as cur:
            if await cur.fetchone() is None:
                raise DocumentRequestError("document_input_unavailable")


class DocumentStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)

    async def close_uncommitted_admission(self, *, scope: DocumentScope, turn_id: str, cancelled: bool) -> None:
        """Close only the document-owned reservation, including a lost binding reply.

        SubagentRunner reserves this dispatch key for a separately owned Lead
        binding. Borrowed Lead executions use their original dispatch identity.
        Cleanup needs the original owner scope, not still-active membership;
        revocation or session deletion must not strand the reservation.
        """
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """UPDATE turn_agent_execution SET status=?,ended_at=?,failure_code=?
                WHERE team_id=? AND turn_id=? AND dispatch_source='lead' AND dispatch_key=?
                  AND runtime_profile_name='lead' AND status='admitted'
                  AND EXISTS(SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id
                    JOIN workspaces w ON w.id=s.workspace_id
                    WHERE t.id=turn_agent_execution.turn_id AND s.user_id=? AND w.id=? AND w.team_id=?)
                  AND NOT EXISTS(SELECT 1 FROM turn_capability_execution c WHERE c.agent_execution_id=turn_agent_execution.id)
                  AND NOT EXISTS(SELECT 1 FROM document_jobs j WHERE j.origin_turn_id=turn_agent_execution.turn_id)""",
                (
                    "cancelled" if cancelled else "failed",
                    datetime.now(UTC).isoformat(),
                    None if cancelled else "document_admission_failed",
                    scope.team_id,
                    turn_id,
                    "document-admission:" + turn_id,
                    scope.user_id,
                    scope.workspace_id,
                    scope.team_id,
                ),
            )
            await db.commit()

    async def recover_from_turn(
        self, *, scope: DocumentScope, turn_id: str, command_id: str, proposal: DocumentProposal | UserQuote, parent_job_id: str | None = None
    ) -> DocumentReceipt | None:
        """Read an exact durable admission without reopening its terminal binding.

        Current owner/Space/session access remains mandatory. Recovery compares
        against the admitted runtime snapshot, not today's model or executor.
        It never dispatches or reauthorizes bytes for execution/publication.
        """
        _command_id(command_id)
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN")
            await _authorize(db, scope, write=False)
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT j.*,r.command_kind,r.result_json,r.request_hash AS receipt_hash
                FROM document_command_receipts r JOIN document_jobs j ON j.id=r.target_id
                JOIN sessions s ON s.id=j.session_id
                WHERE r.team_id=? AND r.workspace_id=? AND r.owner_user_id=? AND r.command_id=?
                  AND j.team_id=r.team_id AND j.workspace_id=r.workspace_id AND j.requested_by=r.owner_user_id
                  AND s.deleted_at IS NULL AND s.user_id=j.requested_by AND s.workspace_id=j.workspace_id""",
                (*scope.sql_params(), command_id),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            request = json.loads(row["request_json"])
            if row["command_kind"] != "submit" or request["turn_id"] != turn_id or row["origin_turn_id"] != turn_id:
                raise DocumentRequestError("document_command_conflict")
            source = await _read_chat_turn_user_source_in_tx(
                db, turn_id=turn_id, expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id
            )
            if (
                source is None
                or source.session_id != row["session_id"]
                or _digest(_json(chat_turn_user_source_projection(source))) != row["user_source_sha256"]
            ):
                raise DocumentRequestError("document_source_unavailable")
            if source.document_revision_target is not None:
                if (
                    parent_job_id is not None
                    or not isinstance(proposal, UserQuote)
                    or request.get("revision", {}).get("target") != source.document_revision_target.model_dump(mode="json")
                ):
                    raise DocumentRequestError("document_command_conflict")
                evidence = UserQuote.model_validate_json(proposal.model_dump_json())
                evidence.verify(source.user_text)
                if request["proposal"]["evidence"] != evidence.model_dump(mode="json"):
                    raise DocumentRequestError("document_command_conflict")
            elif parent_job_id is None:
                if not isinstance(proposal, DocumentProposal) or "answer" in request:
                    raise DocumentRequestError("document_command_conflict")
                candidate = resolve_proposal_positions(DocumentProposal.model_validate_json(proposal.model_dump_json()), source)
                request["proposal"] = candidate.model_dump(mode="json")
            else:
                if not isinstance(proposal, UserQuote) or request.get("answer", {}).get("parent_job_id") != parent_job_id:
                    raise DocumentRequestError("document_command_conflict")
                evidence = UserQuote.model_validate_json(proposal.model_dump_json())
                evidence.verify(source.user_text)
                if request["proposal"]["evidence"] != evidence.model_dump(mode="json"):
                    raise DocumentRequestError("document_command_conflict")
            if _digest(_json(request)) != row["request_hash"] or row["receipt_hash"] != row["request_hash"]:
                raise DocumentRequestError("document_command_conflict")
            receipt = DocumentReceipt(**json.loads(row["result_json"]))
            if receipt.job_id != row["id"] or receipt.proposal_revision != row["proposal_revision"]:
                raise DocumentRequestError("document_command_conflict")
            return receipt

    async def prepare_from_turn(
        self, *, scope: DocumentScope, turn_id: str, proposal: DocumentProposal | UserQuote, parent_job_id: str | None = None
    ) -> PreparedDocumentSubmission:
        """Reject repairable input before allocating an Agent execution."""
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN")
            await _authorize(db, scope, write=True)
            source = await _live_source(db, scope, turn_id)
            if source.document_revision_target is not None:
                if parent_job_id is not None or not isinstance(proposal, UserQuote):
                    raise DocumentRequestError("document_revision_required")
                proposal, revision = await prepare_revision_in_tx(db, source=source, evidence=proposal)
                return PreparedDocumentSubmission(proposal=proposal, revision=revision)
            answer = None
            if parent_job_id is not None:
                if not isinstance(proposal, UserQuote):
                    raise DocumentRequestError("document_request_invalid")
                evidence = UserQuote.model_validate_json(proposal.model_dump_json())
                evidence.verify(source.user_text)
                parent, execution, instruction, digest = await _question(db, scope, parent_job_id)
                if (
                    parent["session_id"] != source.session_id
                    or parent["origin_turn_id"] == turn_id
                    or parent["status"] != "queued"
                    or execution["state"] != "question"
                ):
                    raise DocumentRequestError("document_question_unavailable")
                inherited = _job(parent).proposal
                instruction += "\n\nClarification question:\n" + execution["question"] + "\n\nUser answer:\n" + evidence.text
                if len(instruction) > 8000:
                    raise DocumentRequestError("document_answer_limit")
                proposal = inherited.model_copy(
                    update={"evidence": evidence, "inputs": tuple(item.model_copy(update={"evidence": evidence}) for item in inherited.inputs)}
                )
                answer = _AnswerContext(parent_job_id, digest, instruction)
            if not isinstance(proposal, DocumentProposal):
                raise DocumentRequestError("document_request_invalid")
            proposal = resolve_proposal_positions(DocumentProposal.model_validate_json(proposal.model_dump_json()), source)
            await _validate_inputs(db, scope, source, proposal, answer=answer)
            return PreparedDocumentSubmission(proposal, answer)

    async def submit_from_turn(
        self,
        *,
        scope: DocumentScope,
        turn_id: str,
        command_id: str,
        proposal: DocumentProposal,
        runtime: DocumentRuntimeAdmission | None = None,
        _answer: _AnswerContext | None = None,
        _revision: PreparedRevision | None = None,
        _canvas: CanvasInputAdmission | None = None,
    ) -> DocumentReceipt:
        """Commit proposal + exact source hash + receipt/link/event atomically.

        Current references and earlier pins are supported only for this user's
        ready files in the exact session. This is not file retention or freezing;
        the execution layer must re-authorize and freeze actual bytes later.
        """
        _command_id(command_id)
        # Revalidate/detach before any await (model_construct/model_copy are not
        # validation APIs). Only the typed bounded proposal enters the hash.
        proposal = DocumentProposal.model_validate_json(proposal.model_dump_json())
        request_data = {"command_kind": "submit", "turn_id": turn_id, "proposal": proposal.model_dump(mode="json")}
        if _answer is not None:
            request_data["answer"] = _answer.as_json()
        if _revision is not None:
            request_data["revision"] = _revision.as_json()
        if _canvas is not None:
            request_data["canvas_input"] = _canvas.manifest.model_dump(mode="json")
            if _canvas.source_job_id is not None:
                request_data["canvas_source_job_id"] = _canvas.source_job_id
        if runtime is not None:
            request_data["runtime"] = {
                "profile": runtime.turn_snapshot.planner_profile
                if runtime.turn_snapshot is not None and runtime.turn_snapshot.schema_version == 2
                else "ordinary-pptx-worker-v1",
                "image_id": runtime.image_id,
                "model_config_version": runtime.model_config_version,
            }
            if runtime.model_selection is not None:
                request_data["runtime"]["model_selection"] = runtime.model_selection.model_dump(mode="json")
        request_json = _json(request_data)
        request_hash = _digest(request_json)
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await _authorize(db, scope, write=False)
            previous = await _receipt(db, scope, command_id, request_hash)
            if previous is not None:
                if runtime is not None:
                    from bowhead_core.documents.agent_caller import document_job_caller_in_tx

                    caller = await document_job_caller_in_tx(db, scope=scope, turn_id=turn_id, job_id=previous.job_id)
                    if caller != runtime.binding.execution_id:
                        raise DocumentRequestError("document_command_conflict")
                return previous  # Remains recoverable after the Turn ends.
            await _authorize(db, scope, write=True)
            source = await _live_source(db, scope, turn_id)
            if source.document_revision_target is not None:
                if _revision is None or _answer is not None:
                    raise DocumentRequestError("document_revision_required")
                current_proposal, current_revision = await prepare_revision_in_tx(db, source=source, evidence=proposal.evidence)
                if current_revision != _revision or current_proposal != proposal:
                    raise DocumentRequestError("document_revision_conflict")
            elif _revision is not None:
                raise DocumentRequestError("document_revision_conflict")
            else:
                await _validate_inputs(db, scope, source, proposal, answer=_answer)
            if _canvas is not None:
                from bowhead_core.documents.canvas_inputs import inherit_canvas_input_in_tx, validate_current_canvas_in_tx

                if _revision is not None or _answer is not None:
                    parent_id = _revision.target.job_id if _revision is not None else _answer.parent_job_id
                    inherited = await inherit_canvas_input_in_tx(db, scope=scope, job_id=parent_id)
                    if inherited != _canvas:
                        raise DocumentRequestError("document_canvas_input_unavailable")
                elif proposal.canvas is None or _canvas.source_job_id is not None:
                    raise DocumentRequestError("document_canvas_selection_invalid")
                else:
                    await validate_current_canvas_in_tx(db, scope=scope, manifest=_canvas.manifest, section_ids=proposal.canvas.section_ids)
            elif proposal.canvas is not None:
                raise DocumentRequestError("document_canvas_unavailable")
            elif _revision is not None or _answer is not None:
                from bowhead_core.documents.canvas_inputs import inherit_canvas_input_in_tx

                parent_id = _revision.target.job_id if _revision is not None else _answer.parent_job_id
                if await inherit_canvas_input_in_tx(db, scope=scope, job_id=parent_id) is not None:
                    raise DocumentRequestError("document_canvas_input_unavailable")
            if _answer is not None:
                parent, execution, instruction, digest = await _question(db, scope, _answer.parent_job_id)
                expected_instruction = (
                    instruction + "\n\nClarification question:\n" + execution["question"] + "\n\nUser answer:\n" + proposal.evidence.text
                )
                if (
                    parent["session_id"] != source.session_id
                    or parent["origin_turn_id"] == turn_id
                    or parent["status"] != "queued"
                    or execution["state"] != "question"
                    or digest != _answer.question_digest
                    or expected_instruction != _answer.instruction
                ):
                    raise DocumentRequestError("document_question_unavailable")
            now = datetime.now(UTC).isoformat()
            job_id = f"djob_{uuid.uuid4().hex}"
            await db.execute(
                """INSERT INTO document_jobs
                (id,team_id,workspace_id,requested_by,session_id,origin_turn_id,action,idempotency_key,
                 request_json,request_hash,user_source_sha256,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    *scope.sql_params(),
                    source.session_id,
                    turn_id,
                    proposal.action,
                    command_id,
                    request_json,
                    request_hash,
                    _digest(_json(chat_turn_user_source_projection(source))),
                    now,
                    now,
                ),
            )
            result = await _save_receipt(db, scope, command_id=command_id, kind="submit", request_hash=request_hash, job_id=job_id, now=now)
            if runtime is not None:
                from bowhead_core.documents.capability import admit_capability

                capability_id = await admit_capability(
                    db, path=self._path, runtime=runtime, scope=scope, turn_id=turn_id, inputs=proposal.inputs, now=now
                )
                await db.execute(
                    "INSERT INTO document_dispatches(job_id,profile,image_id,model_config_version,capability_execution_id,model_selection_json) VALUES (?,?,?,?,?,?)",
                    (
                        job_id,
                        request_data["runtime"]["profile"],
                        runtime.image_id,
                        runtime.model_config_version,
                        capability_id,
                        runtime.model_selection.model_dump_json() if runtime.model_selection is not None else None,
                    ),
                )
                if runtime.turn_owner is not None:
                    from bowhead_core.documents.continuation import register_document_turn_in_tx

                    await register_document_turn_in_tx(db, worker=runtime.turn_owner, job_id=job_id, runtime=runtime.turn_snapshot)
            await db.execute(
                """INSERT INTO document_job_links
                (job_id,team_id,workspace_id,owner_user_id,session_id,turn_id,relation,command_id,created_at) VALUES (?,?,?,?,?,?,'submitted',?,?)""",
                (job_id, *scope.sql_params(), source.session_id, turn_id, command_id, now),
            )
            if _answer is not None:
                await db.execute(
                    "INSERT INTO document_continuations(parent_job_id,job_id,question_digest,instruction) VALUES (?,?,?,?)",
                    (_answer.parent_job_id, job_id, _answer.question_digest, _answer.instruction),
                )
                await db.execute(
                    """INSERT INTO document_revision_inputs(job_id,input_index,artifact_id,role,filename,mime,path,sha256,size_bytes)
                    SELECT ?,input_index,artifact_id,role,filename,mime,path,sha256,size_bytes
                    FROM document_revision_inputs WHERE job_id=?""",
                    (job_id, _answer.parent_job_id),
                )
                await db.execute(
                    "UPDATE document_jobs SET status='cancelled',phase='terminal',cancel_reason='user_requested',updated_at=? WHERE id=?",
                    (now, _answer.parent_job_id),
                )
            if _revision is not None:
                await commit_revision_in_tx(db, revision=_revision, job_id=job_id, now=now)
            if _canvas is not None:
                from bowhead_core.documents.canvas_inputs import commit_canvas_input_in_tx

                await commit_canvas_input_in_tx(db, scope=scope, job_id=job_id, admission=_canvas, now=now)
            await db.commit()
            return result

    async def submit_revision_from_turn(
        self, *, scope: DocumentScope, turn_id: str, command_id: str, evidence: UserQuote, runtime: DocumentRuntimeAdmission
    ) -> DocumentReceipt:
        """Admit a revision only from the immutable canonical target."""
        previous = await self.recover_from_turn(scope=scope, turn_id=turn_id, command_id=command_id, proposal=evidence)
        if previous is not None:
            return previous
        try:
            prepared = await self.prepare_from_turn(scope=scope, turn_id=turn_id, proposal=evidence)
            return await self.submit_from_turn(
                scope=scope,
                turn_id=turn_id,
                command_id=command_id,
                proposal=prepared.proposal,
                runtime=runtime,
                _revision=prepared.revision,
            )
        except DocumentRequestError:
            previous = await self.recover_from_turn(scope=scope, turn_id=turn_id, command_id=command_id, proposal=evidence)
            if previous is not None:
                return previous
            raise

    async def answer_from_turn(
        self, *, scope: DocumentScope, turn_id: str, command_id: str, parent_job_id: str, evidence: UserQuote, runtime: DocumentRuntimeAdmission
    ) -> DocumentReceipt:
        """Admit one successor atomically; the model cannot rewrite prior context."""
        previous = await self.recover_from_turn(scope=scope, turn_id=turn_id, command_id=command_id, proposal=evidence, parent_job_id=parent_job_id)
        if previous is not None:
            return previous
        try:
            prepared = await self.prepare_from_turn(scope=scope, turn_id=turn_id, proposal=evidence, parent_job_id=parent_job_id)
        except DocumentRequestError:
            previous = await self.recover_from_turn(
                scope=scope, turn_id=turn_id, command_id=command_id, proposal=evidence, parent_job_id=parent_job_id
            )
            if previous is not None:
                return previous
            raise
        return await self.submit_from_turn(
            scope=scope,
            turn_id=turn_id,
            command_id=command_id,
            proposal=prepared.proposal,
            runtime=runtime,
            _answer=prepared.answer,
        )

    async def questions_from_turn(self, *, scope: DocumentScope, turn_id: str) -> list[dict]:
        """Only current-owner questions in the canonical Turn's exact session."""
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN")
            await _authorize(db, scope, write=True)
            source = await _read_chat_turn_user_source_in_tx(
                db, turn_id=turn_id, expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id
            )
            if source is None:
                raise DocumentRequestError("document_source_unavailable")
            async with db.execute(
                """SELECT j.id,e.question FROM document_jobs j JOIN document_executions e ON e.job_id=j.id
                JOIN sessions s ON s.id=j.session_id WHERE j.team_id=? AND j.workspace_id=? AND j.requested_by=? AND j.session_id=?
                AND j.status='queued' AND e.state='question' AND s.deleted_at IS NULL AND s.user_id=j.requested_by
                AND NOT EXISTS(SELECT 1 FROM document_turn_dispatches d WHERE d.job_id=j.id)
                ORDER BY j.created_at DESC,j.id DESC LIMIT 5""",
                (*scope.sql_params(), source.session_id),
            ) as cursor:
                return [{"job_id": row[0], "question": row[1]} for row in await cursor.fetchall()]

    async def get(self, *, scope: DocumentScope, job_id: str) -> DocumentJob | None:
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN")
            await _authorize(db, scope, write=False)
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT j.* FROM document_jobs j JOIN sessions s ON s.id=j.session_id
                WHERE j.id=? AND j.team_id=? AND j.workspace_id=? AND j.requested_by=?
                  AND s.deleted_at IS NULL AND s.user_id=j.requested_by AND s.workspace_id=j.workspace_id""",
                (job_id, *scope.sql_params()),
            ) as cur:
                row = await cur.fetchone()
            return _job(row) if row is not None else None

    async def list_for_session(self, *, scope: DocumentScope, session_id: str, after_id: str = "", limit: int = 50) -> list[DocumentJob]:
        """Read-only bounded keyset projection, including jobs without files.

        Ordered by stable ID, not recency. No hydration side effects or turn SSE.
        """
        if not 1 <= limit <= 100:
            raise DocumentRequestError("document_page_invalid")
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN")
            await _authorize(db, scope, write=False)
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT j.* FROM document_job_links l JOIN document_jobs j ON j.id=l.job_id
                JOIN sessions s ON s.id=l.session_id
                WHERE l.team_id=? AND l.workspace_id=? AND l.owner_user_id=? AND l.session_id=?
                  AND l.job_id>? AND s.deleted_at IS NULL AND s.user_id=l.owner_user_id AND s.workspace_id=l.workspace_id
                ORDER BY l.job_id LIMIT ?""",
                (*scope.sql_params(), session_id, after_id, limit),
            ) as cur:
                return [_job(row) for row in await cur.fetchall()]

    async def cancel(self, *, scope: DocumentScope, job_id: str, command_id: str, expected_proposal_revision: int) -> DocumentReceipt:
        """Explicit trusted user cancellation; works with feature off.

        The migration trigger fences active execution in this transaction.
        Published file identities are immutable and cannot be cancelled.
        """
        _command_id(command_id)
        request_hash = _digest(_json({"command_kind": "cancel", "job_id": job_id, "expected_proposal_revision": expected_proposal_revision}))
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await _authorize(db, scope, write=False)
            previous = await _receipt(db, scope, command_id, request_hash)
            if previous is not None:
                return previous
            async with db.execute(
                "SELECT status,proposal_revision FROM document_jobs WHERE id=? AND team_id=? AND workspace_id=? AND requested_by=?",
                (job_id, *scope.sql_params()),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise DocumentRequestError("document_not_found")
            async with db.execute("SELECT state FROM document_executions WHERE job_id=?", (job_id,)) as cur:
                execution = await cur.fetchone()
            if execution is not None and execution[0] == "published":
                raise DocumentRequestError("document_already_published")
            if type(expected_proposal_revision) is not int or row[1] != expected_proposal_revision:
                raise DocumentRequestError("document_revision_conflict")
            now = datetime.now(UTC).isoformat()
            if row[0] == "queued":
                cur = await db.execute(
                    """UPDATE document_jobs SET status='cancelled',phase='terminal',cancel_reason='user_requested',updated_at=?
                    WHERE id=? AND status='queued' AND proposal_revision=?""",
                    (now, job_id, expected_proposal_revision),
                )
                if cur.rowcount != 1:
                    raise DocumentRequestError("document_revision_conflict")
            result = await _save_receipt(db, scope, command_id=command_id, kind="cancel", request_hash=request_hash, job_id=job_id, now=now)
            await db.commit()
            return result
