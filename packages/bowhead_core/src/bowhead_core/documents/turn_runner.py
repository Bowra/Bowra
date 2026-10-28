"""Bounded document planning under the original shared Turn worker lease."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict

import anyio
from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.invocation import physical_invocation_scope
from bowhead_router.types import LLMResponse
from bowhead_router.usage import detached_usage_context, usage_context

from bowhead_core._db import open_shared_ctx
from bowhead_core.document_chat_continuation import DocumentPlanningCheckpointRef, DocumentTurnOwner
from bowhead_core.documents.continuation import (
    DocumentCallRef,
    DocumentPlanningFailure,
    DocumentPlanningOutcome,
    DocumentPlanningPause,
    DocumentPlanningReview,
    _dispatch_material,
    _input_digest,
    _known_observations_in_tx,
    _one,
    _validated_context,
    begin_document_planning_in_tx,
    document_call_scope,
    load_document_checkpoint_in_tx,
    retain_document_context_in_tx,
    retain_document_question_in_tx,
    validate_document_pause_in_tx,
    validate_document_worker_in_tx,
)
from bowhead_core.documents.delivery import DocumentDeliveryStore, _execution, _fence, _now
from bowhead_core.documents.executor import DocumentExecutor, WorkerInput
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.planning_instruction import document_instruction_in_tx
from bowhead_core.documents.planning_resume import prepare_answer_context
from bowhead_core.documents.pptx_plan import PlanningContext, planning_repair_instruction, planning_system_prompt
from bowhead_core.documents.pptx_planning import REPAIRABLE_ERRORS
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_core.documents.store import _digest, _json
from bowhead_core.documents.usage import DocumentModelHook
from bowhead_core.rate_card import RATE_CARD_VERSION


async def fail_document_turn(delivery: DocumentDeliveryStore, worker: DocumentTurnOwner, job_id: str, code: str) -> DocumentPlanningFailure:
    # Terminalize only this retained worker identity, including failure before
    # input freezing. Revocation must not leave a permanently queued request.
    async with open_shared_ctx(delivery.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        identity = await _one(
            db,
            """SELECT 1 FROM document_jobs j JOIN document_turn_dispatches d ON d.job_id=j.id
          LEFT JOIN document_executions e ON e.job_id=j.id WHERE j.id=? AND j.team_id=? AND j.requested_by=? AND j.workspace_id=?
          AND j.session_id=? AND j.origin_turn_id=? AND ((d.owner_id=? AND d.creation_generation=?) OR EXISTS(
            SELECT 1 FROM document_planning_attempts a WHERE a.job_id=j.id AND a.owner_id=? AND a.creation_generation=? AND e.owner=a.owner_id))""",
            (
                job_id,
                worker.team_id,
                worker.user_id,
                worker.workspace_id,
                worker.session_id,
                worker.turn_id,
                worker.owner_id,
                worker.generation,
                worker.owner_id,
                worker.generation,
            ),
        )
        if identity is None:
            return DocumentPlanningFailure("failed", job_id, code)
        # Respect the original preparing-only admission guard. The terminal
        # transition shares this transaction, so no intermediate row escapes.
        await db.execute(
            """INSERT INTO document_executions(job_id,state,owner,lease_until,rate_card_version,created_at,updated_at)
          SELECT j.id,'preparing',d.owner_id,0,?,?,? FROM document_turn_dispatches d JOIN document_jobs j ON j.id=d.job_id
          WHERE j.id=? AND j.status='queued' AND j.team_id=? AND j.requested_by=? AND j.workspace_id=? AND j.session_id=? AND j.origin_turn_id=?
            AND d.owner_id=? AND d.creation_generation=? AND NOT EXISTS(SELECT 1 FROM document_executions WHERE job_id=j.id)""",
            (
                RATE_CARD_VERSION,
                _now(),
                _now(),
                job_id,
                worker.team_id,
                worker.user_id,
                worker.workspace_id,
                worker.session_id,
                worker.turn_id,
                worker.owner_id,
                worker.generation,
            ),
        )
        await db.execute(
            """UPDATE document_executions SET state='failed',error_code=?,updated_at=?
            WHERE job_id=? AND owner=? AND state IN ('preparing','planning','rendering')""",
            (code, _now(), job_id, worker.owner_id),
        )
        await db.commit()
    return DocumentPlanningFailure("failed", job_id, code)


async def _read_inputs(delivery, scope, job_id):
    inputs_store = DocumentInputStore(delivery.artifacts)
    async with open_shared_ctx(delivery.path) as db:
        await db.execute("BEGIN")
        await _input_digest(db, job_id)
        async with db.execute("SELECT artifact_id,filename,role FROM document_input_copies WHERE job_id=? ORDER BY input_index", (job_id,)) as cursor:
            rows = await cursor.fetchall()
    return tuple(
        [
            WorkerInput(row["filename"], row["role"], await inputs_store.read(scope=scope, job_id=job_id, artifact_id=row["artifact_id"]))
            for row in rows
        ]
    )


async def run_document_turn(
    *,
    delivery: DocumentDeliveryStore,
    executor: DocumentExecutor,
    worker: DocumentTurnOwner,
    job_id: str,
    attempt_no: int,
    clarification_store,
    invoke: Callable[[str, str], Awaitable[LLMResponse]],
) -> DocumentPlanningOutcome:
    """Reuse shared pause preparation; never publish or create a background claim."""
    scope = DocumentScope(team_id=worker.team_id, workspace_id=worker.workspace_id, user_id=worker.user_id)
    try:
        if type(attempt_no) is not int or attempt_no not in (1, 2):
            raise DocumentRequestError("document_execution_fenced")
        async with open_shared_ctx(delivery.path) as db:
            await db.execute("BEGIN")
            await validate_document_worker_in_tx(db, worker=worker, job_id=job_id)
            dispatch = await _one(db, "SELECT * FROM document_turn_dispatches WHERE job_id=?", (job_id,))
            if dispatch is None:
                raise DocumentRequestError("document_execution_fenced")
            creation = worker.model_copy(update={"generation": dispatch["creation_generation"], "owner_id": dispatch["owner_id"]})
            _, runtime = await _dispatch_material(db, job_id=job_id, creation_worker=creation)
            execution = await _execution(db, job_id)
            if (
                execution is not None
                and execution["owner"] == worker.owner_id
                and execution["state"] == "review"
                and execution["draft_digest"] == _digest(execution["draft_json"])
            ):
                return DocumentPlanningReview(job_id, execution["draft_digest"])
            if attempt_no == 1:
                if worker != creation:
                    raise DocumentRequestError("document_execution_fenced")
                if execution is not None:
                    # A recovered admission never replays inventory or planning.
                    saved = await _one(db, "SELECT checkpoint_digest FROM document_planning_attempts WHERE job_id=? AND attempt_no=1", (job_id,))
                    if execution["state"] == "question" and saved and saved[0]:
                        ref = DocumentPlanningCheckpointRef(
                            job_id=job_id, attempt_no=1, checkpoint_digest=saved[0], source_hash=dispatch["source_hash"]
                        )
                        material = await validate_document_pause_in_tx(db, worker=worker, checkpoint=ref)
                        return DocumentPlanningPause(job_id, ref, material.question)
                    raise DocumentRequestError(execution["error_code"] or "document_execution_interrupted")
            else:
                await _fence(db, scope, job_id, worker.owner_id)
                if execution["state"] != "planning":
                    raise DocumentRequestError("document_execution_fenced")
                if await _one(db, "SELECT 1 FROM document_physical_receipt_links WHERE job_id=? AND attempt_no=2", (job_id,)):
                    raise DocumentRequestError("document_usage_unresolved")
            instruction = await document_instruction_in_tx(db, worker=creation, job_id=job_id)
            history = instruction.history
            if attempt_no == 2:
                original = await _one(db, "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=1", (job_id,))
                attempt = await _one(db, "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=2", (job_id,))
                if attempt is None or (attempt["owner_id"], attempt["creation_generation"]) != (worker.owner_id, worker.generation):
                    raise DocumentRequestError("document_execution_fenced")
                ref = DocumentPlanningCheckpointRef(
                    job_id=job_id, attempt_no=1, checkpoint_digest=original["checkpoint_digest"], source_hash=dispatch["source_hash"]
                )
                material = await load_document_checkpoint_in_tx(db, checkpoint=ref, creation_worker=creation)
                card = await _one(db, "SELECT * FROM clarification_requests WHERE request_id=? AND status='resolved'", (attempt["card_id"],))
                if card is None or instruction.text != original["instruction"] or attempt["inputs_digest"] != await _input_digest(db, job_id):
                    raise DocumentRequestError("document_checkpoint_unavailable")
                count = await _one(db, "SELECT count(*) FROM document_input_copies WHERE job_id=? AND role<>'template'", (job_id,))
                answer = json.loads(card["answers_json"])[0]["answer"]
                rebased = prepare_answer_context(
                    PlanningContext(**json.loads(original["context_json"])),
                    question=material.question,
                    answer=answer,
                    external_input_count=count[0] + int(history is not None),
                )
                context = _validated_context(json.loads(attempt["context_json"]), attempt["instruction"], profile=runtime.planner_profile)
                if asdict(rebased.context) != context or rebased.instruction != attempt["instruction"] or execution["context_json"] != _json(context):
                    raise DocumentRequestError("document_checkpoint_unavailable")
                instruction_text = rebased.instruction
        if attempt_no == 1:
            await DocumentInputStore(delivery.artifacts).freeze(scope=scope, job_id=job_id)
            async with open_shared_ctx(delivery.path) as db:
                await db.execute("BEGIN IMMEDIATE")
                if await _execution(db, job_id) is not None:
                    raise DocumentRequestError("document_execution_interrupted")
                await begin_document_planning_in_tx(db, worker=worker, job_id=job_id, instruction=instruction.text)
                await db.commit()
            instruction_text = instruction.text
        inputs = await _read_inputs(delivery, scope, job_id)
        if history is not None:
            inputs += (history,)
        if attempt_no == 2:
            inputs += (rebased.evidence,)
        if len(inputs) > 8:
            raise DocumentRequestError("planning_input_limit")
        if attempt_no == 1:
            inventory = await executor.run(
                run_id=_digest(job_id + ":turn:inventory"),
                inputs=inputs,
                instruction=instruction_text,
                operation="inventory",
                expected_image_id=runtime.image_id,
                planner_profile=runtime.planner_profile,
            )
            if inventory.record["kind"] != "context":
                raise OfficePackageError(inventory.record.get("error_code", "document_worker_failed"))
            context = inventory.record["context"]
            async with open_shared_ctx(delivery.path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await retain_document_context_in_tx(db, worker=worker, job_id=job_id, attempt_no=1, context=context)
                await db.commit()
            await delivery.transition(
                scope=scope, job_id=job_id, owner=worker.owner_id, state="planning", image_id=runtime.image_id, context_json=_json(context)
            )
        prompt = context["payload"]
        system = (
            planning_system_prompt(runtime.planner_profile)
            + "\nAsk at most one concise free-text question, no longer than 200 characters. Use "
            + runtime.language.output_language
            + " for questions and explanations."
        )
        for call_no in (1, 2):
            hook = DocumentModelHook(str(delivery.path), scope, job_id, worker.owner_id, call_no, attempt_no=attempt_no)
            with (
                usage_context(team_id=scope.team_id, user_id=scope.user_id, workspace_id=scope.workspace_id, turn_id=worker.turn_id),
                detached_usage_context(reason="template_document"),
                document_call_scope(DocumentCallRef(job_id, attempt_no, call_no, worker)),
                physical_invocation_scope(hook),
                anyio.fail_after(90),
            ):
                reply = await invoke(system, prompt)
            async with open_shared_ctx(delivery.path) as db:
                await db.execute("BEGIN")
                await _fence(db, scope, job_id, worker.owner_id)
                await _known_observations_in_tx(db, job_id)
                call = await _one(
                    db, "SELECT state FROM document_model_calls WHERE job_id=? AND attempt_no=? AND call_no=?", (job_id, attempt_no, call_no)
                )
                if (
                    call is None
                    or call[0] != "completed"
                    or await _one(db, "SELECT 1 FROM document_usage_receipts WHERE job_id=? AND settled_at IS NULL", (job_id,))
                ):
                    raise DocumentRequestError("document_usage_unresolved")
            if reply.finish_reason != "stop" or len(reply.content.encode()) > 64_000:
                raise OfficePackageError("planning_incomplete_response")
            await delivery.transition(scope=scope, job_id=job_id, owner=worker.owner_id, state="rendering", response=reply.content)
            output = await executor.run(
                run_id=_digest(job_id + f":attempt:{attempt_no}:apply:{call_no}"),
                inputs=inputs,
                instruction=instruction_text,
                operation="apply_verify",
                response=reply.content,
                context_digest=context["digest"],
                expected_image_id=runtime.image_id,
                planner_profile=runtime.planner_profile,
            )
            error = output.record.get("error_code", "document_worker_failed")
            if output.record["kind"] == "question":
                if attempt_no == 2:
                    await delivery.fail(job_id=job_id, owner=worker.owner_id, code="document_information_incomplete")
                    return DocumentPlanningFailure("incomplete", job_id, "document_information_incomplete")
                try:
                    async with open_shared_ctx(delivery.path) as db:
                        await db.execute("BEGIN IMMEDIATE")
                        question = output.record["review"]["question"]
                        ref = await retain_document_question_in_tx(db, worker=worker, job_id=job_id, question=question, last_call_no=call_no)
                        from bowhead_core.document_clarification_lifecycle import prepare_document_pause_in_tx

                        await prepare_document_pause_in_tx(
                            db,
                            store=clarification_store,
                            worker=worker,
                            checkpoint=ref,
                            questions=[{"question": question, "type": "free_text", "options": []}],
                        )
                        await db.commit()
                    return DocumentPlanningPause(job_id, ref, question)
                except DocumentRequestError as exc:
                    if exc.code != "planning_invalid_question":
                        raise
                    error = exc.code
            elif output.record["kind"] == "draft":
                await delivery.save_draft(scope=scope, job_id=job_id, owner=worker.owner_id, result=output)
                saved = await delivery.get(scope=scope, job_id=job_id)
                if saved["state"] != "review" or not saved.get("draft_digest"):
                    raise DocumentRequestError("document_draft_unavailable")
                return DocumentPlanningReview(job_id, saved["draft_digest"])
            if call_no == 2 or error not in REPAIRABLE_ERRORS:
                raise OfficePackageError(error)
            await delivery.transition(scope=scope, job_id=job_id, owner=worker.owner_id, state="planning", error_code=error)
            prompt = _json(
                {
                    "input": json.loads(context["payload"]),
                    "repair": {
                        "error": error,
                        "previous_response": reply.content,
                        "instruction": planning_repair_instruction(runtime.planner_profile),
                    },
                }
            )
    except InvocationOutcomeUnknownError:
        code = "document_usage_unresolved"
    except (OfficePackageError, DocumentRequestError) as exc:
        code = exc.code
    except Exception:
        code = "document_execution_failed"
    except BaseException:
        with anyio.CancelScope(shield=True):
            await fail_document_turn(delivery, worker, job_id, "document_execution_interrupted")
        raise
    return await fail_document_turn(delivery, worker, job_id, code)
