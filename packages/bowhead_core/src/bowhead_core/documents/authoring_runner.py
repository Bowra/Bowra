"""Private script authoring under the original document and shared Turn owner."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Literal

import anyio
from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.invocation import physical_invocation_scope
from bowhead_router.output_budget import ContextCapacityError
from bowhead_router.types import Message, ToolCall
from bowhead_router.usage import detached_usage_context, usage_context
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_run_limits import AgentRunLimits, get_tool_call_budget, model_call_budget_s
from bowhead_core.document_chat_continuation import DocumentPlanningCheckpointRef
from bowhead_core.documents.authoring_context import context as authoring_context
from bowhead_core.documents.authoring_context import tools
from bowhead_core.documents.authoring_executor import ScriptExecutionError
from bowhead_core.documents.authoring_formats import AUTHORING_PROFILES, WORD_PROFILE
from bowhead_core.documents.authoring_protocol import COMPUTE_CONTRACT, digest
from bowhead_core.documents.authoring_review import review_result
from bowhead_core.documents.continuation import (
    DocumentCallRef,
    DocumentPlanningFailure,
    DocumentPlanningPause,
    DocumentPlanningReview,
    _dispatch_material,
    _known_observations_in_tx,
    _one,
    begin_document_planning_in_tx,
    document_call_scope,
    load_document_checkpoint_in_tx,
    retain_document_context_in_tx,
    retain_document_question_in_tx,
    validate_document_pause_in_tx,
    validate_document_worker_in_tx,
)
from bowhead_core.documents.delivery import _execution, _fence
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.planning_instruction import document_instruction_in_tx
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_core.documents.store import _json
from bowhead_core.documents.turn_runner import _read_inputs, fail_document_turn
from bowhead_core.documents.usage import DocumentModelHook
from bowhead_core.pptx_reading import inspect_pptx_read_info
from bowhead_core.read_receipts import ReadInfo, ReadRecovery, read_info

log = logging.getLogger(__name__)
MAX_SCRIPT_ATTEMPTS = 2
MAX_UNKNOWN_SOURCES = 2
MATERIAL_REFUSALS = frozenset(
    {
        "document_data_limit",
        "document_data_format_unsupported",
        "document_data_encoding_unsupported",
        "document_data_formula_unsupported",
        "document_data_unsupported",
        "document_data_invalid",
        "planning_invalid_json",
        "planning_duplicate_key",
        "planning_nonfinite_number",
    }
)


async def _read_material(executor, *, run_id, item, expected_image_id):
    """Observe a bounded read of an already authorized, exact script input."""
    try:
        value = await executor.read_material(run_id=run_id, item=item, expected_image_id=expected_image_id)
    except OfficePackageError as error:
        if error.code not in MATERIAL_REFUSALS:
            raise
        metadata = ReadInfo.model_validate(error.read_info or read_info(view="none", unit="bytes", returned=0))
        boundary = ""
        if metadata.limit is not None:
            limit = metadata.limit
            observed = "unknown" if limit.actual is None else str(limit.actual)
            maximum = "unknown" if limit.maximum is None else str(limit.maximum)
            boundary = f" Boundary {limit.kind}: observed {observed} {limit.unit}; maximum {maximum} {limit.unit}."
        value = {
            "error": error.code,
            "read_info": metadata.model_dump(mode="json"),
            "hint": "This file cannot be returned by the normalized data reader."
            + boundary
            + " Its exact full bytes remain in this task's bound script inputs. Use another available source or parse those bytes "
            "in Python when available, or the final edit script; do not retry this unchanged read or treat it as complete data.",
        }
    if "read_info" in value:
        metadata = ReadInfo.model_validate(value["read_info"])
        recovery = ReadRecovery(action="use_full_input") if metadata.view != "complete" else ReadRecovery()
        value = {**value, "read_info": metadata.model_copy(update={"full_input_available": True, "recovery": recovery}).model_dump(mode="json")}
    return value


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = "source"
    slides: list[int] | None = None
    detail: Literal["index", "content", "objects", "datasets"] = "index"
    section_id: str | None = Field(default=None, min_length=1, max_length=128)
    view_id: str | None = Field(default=None, min_length=1, max_length=128)
    dataset_alias: str | None = Field(default=None, min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0)


class WordReadArguments(ReadArguments):
    slides: None = None
    detail: Literal["index", "content", "datasets"] = "index"
    limit: int = Field(default=50, ge=1, le=50)


class ExecuteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    based_on: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    code: str = Field(min_length=1, max_length=64_000)


class ComputeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=64_000)
    intent: str = Field(min_length=1, max_length=500)


class QuestionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=200)
    type: Literal["free_text"] = "free_text"
    options: list[str] = Field(default_factory=list, max_length=0)


class AskArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    questions: list[QuestionArguments] = Field(min_length=1, max_length=1)


def _decode_messages(items):
    messages = []
    for item in items:
        value = dict(item)
        if value.get("tool_calls"):
            value["tool_calls"] = [ToolCall(**call) for call in value["tool_calls"]]
        messages.append(Message(**value))
    return messages


async def _observed(delivery, scope, worker, job_id, attempt_no, call_no):
    async with open_shared_ctx(delivery.path) as db:
        await db.execute("BEGIN")
        await _fence(db, scope, job_id, worker.owner_id)
        await _known_observations_in_tx(db, job_id)
        call = await _one(db, "SELECT state FROM document_model_calls WHERE job_id=? AND attempt_no=? AND call_no=?", (job_id, attempt_no, call_no))
        if (
            call is None
            or call[0] != "completed"
            or await _one(db, "SELECT 1 FROM document_usage_receipts WHERE job_id=? AND settled_at IS NULL", (job_id,))
        ):
            raise DocumentRequestError("document_usage_unresolved")


async def run_authoring_turn(
    *, delivery, executor, worker, job_id, attempt_no, clarification_store, invoke, authorize_inputs=None, prepare_inputs=None
):
    """Successful execution goes straight to existing review; no judging model."""
    scope = DocumentScope(team_id=worker.team_id, workspace_id=worker.workspace_id, user_id=worker.user_id)
    try:
        if authorize_inputs is not None:
            await authorize_inputs()
        async with open_shared_ctx(delivery.path) as db:
            await db.execute("BEGIN")
            await validate_document_worker_in_tx(db, worker=worker, job_id=job_id)
            dispatch = await _one(db, "SELECT * FROM document_turn_dispatches WHERE job_id=?", (job_id,))
            creation = worker.model_copy(update={"generation": dispatch["creation_generation"], "owner_id": dispatch["owner_id"]})
            _, runtime = await _dispatch_material(db, job_id=job_id, creation_worker=creation)
            if runtime.schema_version not in {2, 3} or runtime.planner_profile not in AUTHORING_PROFILES or attempt_no not in (1, 2):
                raise DocumentRequestError("document_execution_fenced")
            execution = await _execution(db, job_id)
            if (
                execution is not None
                and execution["owner"] == worker.owner_id
                and execution["state"] == "review"
                and execution["draft_digest"] == digest(execution["draft_json"].encode())
            ):
                return DocumentPlanningReview(job_id, execution["draft_digest"])
            instruction = await document_instruction_in_tx(db, worker=creation, job_id=job_id)
            if attempt_no == 1:
                if worker != creation:
                    raise DocumentRequestError("document_execution_fenced")
                if execution is not None:
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
                if execution["state"] != "planning" or await _one(
                    db, "SELECT 1 FROM document_physical_receipt_links WHERE job_id=? AND attempt_no=2", (job_id,)
                ):
                    raise DocumentRequestError("document_execution_interrupted")
                original = await _one(db, "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=1", (job_id,))
                attempt = await _one(db, "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=2", (job_id,))
                if attempt is None or (attempt["owner_id"], attempt["creation_generation"]) != (worker.owner_id, worker.generation):
                    raise DocumentRequestError("document_execution_fenced")
                ref = DocumentPlanningCheckpointRef(
                    job_id=job_id, attempt_no=1, checkpoint_digest=original["checkpoint_digest"], source_hash=dispatch["source_hash"]
                )
                material = await load_document_checkpoint_in_tx(db, checkpoint=ref, creation_worker=creation)
                card = await _one(db, "SELECT * FROM clarification_requests WHERE request_id=? AND status='resolved'", (attempt["card_id"],))
                if card is None:
                    raise DocumentRequestError("document_checkpoint_unavailable")
                retained = json.loads(json.loads(original["checkpoint_json"])["response"])
                if retained.get("action") != "ask" or retained.get("question") != material.question:
                    raise DocumentRequestError("document_checkpoint_unavailable")
                messages = _decode_messages(retained["messages"])
                used_tools = retained["used_tools"]
                used_steps = retained["used_steps"]
                answer = json.loads(card["answers_json"])[0]["answer"]
                messages.append(
                    Message(
                        role="tool",
                        name="clarification_ask",
                        tool_call_id=retained["question_call_id"],
                        content=_json({"answers": [{"question": material.question, "answer": answer}]}),
                    )
                )
        if attempt_no == 1:
            if prepare_inputs is not None:
                await prepare_inputs()
            await DocumentInputStore(delivery.artifacts).freeze(scope=scope, job_id=job_id)
            async with open_shared_ctx(delivery.path) as db:
                await db.execute("BEGIN IMMEDIATE")
                if await _execution(db, job_id) is not None:
                    raise DocumentRequestError("document_execution_interrupted")
                await begin_document_planning_in_tx(db, worker=worker, job_id=job_id, instruction=instruction.text)
                await db.commit()
        inputs = await _read_inputs(delivery, scope, job_id)
        if any(item.role == "canvas" for item in inputs) and authorize_inputs is None:
            raise DocumentRequestError("document_canvas_access_unavailable")
        if instruction.history is not None:
            inputs += (instruction.history,)
        source = next(item for item in inputs if item.role == "template")
        data = tuple(item for item in inputs if item is not source)
        if len(data) > 7:
            raise DocumentRequestError("planning_input_limit")
        aliases = {"source": source, **{f"data-{index}": item for index, item in enumerate(data)}}
        word = runtime.planner_profile == WORD_PROFILE
        if attempt_no == 1:
            if word:
                await executor.inspect_source(
                    run_id=digest((job_id + ":word-source-format").encode()),
                    source=source.content,
                    expected_image_id=runtime.image_id,
                    offset=0,
                    limit=1,
                    detail="index",
                )
            context = asdict(
                authoring_context(
                    instruction.text,
                    inputs,
                    api_reference="The private API reference is retained in the runtime system instructions.",
                    profile=runtime.planner_profile,
                )
            )
            async with open_shared_ctx(delivery.path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await retain_document_context_in_tx(db, worker=worker, job_id=job_id, attempt_no=1, context=context)
                await db.commit()
            await delivery.transition(
                scope=scope, job_id=job_id, owner=worker.owner_id, state="planning", image_id=runtime.image_id, context_json=_json(context)
            )
            messages = [Message(role="user", content=context["payload"])]
            used_tools = used_steps = 0
        readiness = await executor.capabilities()
        if readiness.get("image_id") != runtime.image_id:
            raise OfficePackageError("document_runtime_changed")
        compute_enabled = readiness.get("compute_contract") == COMPUTE_CONTRACT
        limits = AgentRunLimits(**runtime.authoring_limits)
        script_attempts = sum(
            message.role == "tool" and message.name == "document_execute" and json.loads(message.content).get("outcome") == "known_failed"
            for message in messages
        )
        unknown_sources = 0
        system = runtime.authoring_system + "\nUse " + runtime.language.output_language + " for questions and explanations."
        call_no = 0
        while used_tools < limits.tool_call_limit and (limits.step_limit is None or used_steps < limits.step_limit):
            if authorize_inputs is not None:
                await authorize_inputs()
            call_no += 1
            used_steps += 1
            hook = DocumentModelHook(str(delivery.path), scope, job_id, worker.owner_id, call_no, attempt_no=attempt_no)
            remaining = anyio.current_effective_deadline() - anyio.current_time()
            timeout = model_call_budget_s(limits, remaining_s=min(remaining, limits.turn_time_limit_s))
            with (
                usage_context(team_id=scope.team_id, user_id=scope.user_id, workspace_id=scope.workspace_id, turn_id=worker.turn_id),
                detached_usage_context(reason="template_document"),
                document_call_scope(DocumentCallRef(job_id, attempt_no, call_no, worker)),
                physical_invocation_scope(hook),
                anyio.fail_after(timeout),
            ):
                reply = await invoke(system, messages, tools(runtime.planner_profile, compute=compute_enabled))
            await _observed(delivery, scope, worker, job_id, attempt_no, call_no)
            if reply.finish_reason not in {"stop", "tool_use"} or not reply.tool_calls:
                raise DocumentRequestError("document_authoring_no_output")
            messages.append(Message(role="assistant", content=reply.content, tool_calls=reply.tool_calls))
            for index, call in enumerate(reply.tool_calls):
                if authorize_inputs is not None:
                    await authorize_inputs()
                shared_budget = get_tool_call_budget()
                if used_tools >= limits.tool_call_limit or (shared_budget is not None and not shared_budget.admit(call.name)):
                    raise DocumentRequestError("document_authoring_budget_exhausted")
                used_tools += 1
                # Every native side effect rechecks the original live worker and
                # exact private copies; model arguments cannot grant file access.
                async with open_shared_ctx(delivery.path) as db:
                    await db.execute("BEGIN")
                    await _fence(db, scope, job_id, worker.owner_id)
                    await validate_document_worker_in_tx(db, worker=worker, job_id=job_id)
                run_id = digest(f"{job_id}:authoring:{attempt_no}:{call_no}:{index}".encode())
                try:
                    if call.name == "document_read":
                        args = (WordReadArguments if word else ReadArguments).model_validate(call.arguments)
                        item = aliases.get(args.source)
                        if item is None:
                            # A model-chosen name is not a lost input: the frozen copies are intact.
                            unknown_sources += 1
                            if unknown_sources >= MAX_UNKNOWN_SOURCES:
                                await fail_document_turn(delivery, worker, job_id, "document_information_incomplete")
                                return DocumentPlanningFailure("incomplete", job_id, "document_information_incomplete")
                            messages.append(
                                Message(
                                    role="tool",
                                    name=call.name,
                                    tool_call_id=call.id,
                                    content=_json(
                                        {
                                            "error": "document_source_unknown",
                                            "available_sources": sorted(aliases),
                                            "hint": "Only these inputs exist, and this task cannot query data systems. "
                                            "If required values are not in them, ask for the values or a data file; do not invent them.",
                                        }
                                    ),
                                )
                            )
                            continue
                        if item.role == "canvas":
                            from bowhead_core.documents.canvas_material import dataset_input, read_material

                            if args.slides is not None:
                                raise DocumentRequestError("document_canvas_selection_invalid")
                            if args.dataset_alias is not None:
                                if args.section_id is not None or args.view_id is not None or args.offset or args.detail not in {"index", "content"}:
                                    raise DocumentRequestError("document_canvas_selection_invalid")
                                linked = dataset_input(item.content, args.dataset_alias)
                                value = await _read_material(executor, run_id=run_id, item=linked, expected_image_id=runtime.image_id)
                                value.update(dataset_alias=args.dataset_alias, canvas_sha256=digest(item.content))
                            else:
                                value = read_material(
                                    item.content, section_id=args.section_id, view_id=args.view_id, offset=args.offset, detail=args.detail
                                )
                        elif args.dataset_alias is not None or args.detail == "datasets":
                            raise DocumentRequestError("document_canvas_selection_invalid")
                        elif word and (item.role == "template" or PurePosixPath(item.name).suffix.lower() == ".docx"):
                            if args.section_id is not None or args.view_id is not None or args.slides is not None:
                                raise DocumentRequestError("document_canvas_selection_invalid")
                            value = await executor.inspect_source(
                                run_id=run_id,
                                source=item.content,
                                expected_image_id=runtime.image_id,
                                offset=args.offset,
                                limit=args.limit,
                                detail=args.detail,
                            )
                        elif args.section_id is not None or args.view_id is not None or args.offset:
                            raise DocumentRequestError("document_canvas_selection_invalid")
                        elif PurePosixPath(item.name).suffix.lower() == ".pptx":
                            value = await executor.inspect_source(
                                run_id=run_id, source=item.content, expected_image_id=runtime.image_id, slides=args.slides, detail=args.detail
                            )
                            value["read_info"] = inspect_pptx_read_info(value, source=args.source, slides=args.slides, detail=args.detail)
                        else:
                            value = await _read_material(executor, run_id=run_id, item=item, expected_image_id=runtime.image_id)
                    elif call.name == "python_exec" and compute_enabled:
                        args = ComputeArguments.model_validate(call.arguments)
                        value = await executor.compute(
                            run_id=run_id,
                            source=source.content,
                            code=args.code,
                            expected_image_id=runtime.image_id,
                            data=data,
                        )
                    elif call.name == "document_execute":
                        args = ExecuteArguments.model_validate(call.arguments)
                        if args.based_on is not None and args.based_on != digest(source.content):
                            raise OfficePackageError("source_hash_mismatch")
                        if script_attempts >= MAX_SCRIPT_ATTEMPTS:
                            raise DocumentRequestError("document_authoring_budget_exhausted")
                        script_attempts += 1
                        candidate = await executor.execute_script(
                            run_id=run_id,
                            source=source.content,
                            code=args.code,
                            parent_sha256=digest(source.content),
                            expected_image_id=runtime.image_id,
                            data=data,
                        )
                        await delivery.transition(scope=scope, job_id=job_id, owner=worker.owner_id, state="rendering")
                        try:
                            if word:
                                output = await executor.render_script_candidate(
                                    run_id=digest((run_id + ":render").encode()), candidate=candidate, source=source.content
                                )
                            else:
                                output = await executor.render_script_candidate(run_id=digest((run_id + ":render").encode()), candidate=candidate)
                        except OfficePackageError as error:
                            correctable = {"document_pdf_text_missing_or_clipped"}
                            if word:
                                correctable.update(
                                    {"word_candidate_format_changed", "word_candidate_structure_changed", "word_candidate_target_unsupported"}
                                )
                            if error.code not in correctable or script_attempts >= MAX_SCRIPT_ATTEMPTS:
                                raise
                            await delivery.transition(scope=scope, job_id=job_id, owner=worker.owner_id, state="planning")
                            messages.append(
                                Message(
                                    role="tool",
                                    name=call.name,
                                    tool_call_id=call.id,
                                    content=_json(
                                        {
                                            "error": error.code,
                                            "outcome": "known_failed",
                                            "stage": "preview",
                                            "run_id": run_id,
                                            "diagnostics": (
                                                "The actual Word candidate failed source-format preservation or visible-text checks. "
                                                "Use DocumentTemplate with actual source blocks; retain styles, tables and page settings. Allow natural reflow. "
                                                "Write the corrected full script against the original source within the remaining budget."
                                                if word
                                                else "The trusted preview could not verify all candidate text at its source positions. "
                                                "Shorten replacement titles, captions and body text to fit the original geometry and fonts. "
                                                "Use document_read if needed, then write the complete corrected script against the same original source. "
                                                "The candidate was not accepted or published. The original remaining task budget still applies."
                                            ),
                                        }
                                    ),
                                )
                            )
                            continue
                        if word:
                            from bowhead_core.documents.executor import WorkerResult

                            output = WorkerResult(
                                {
                                    **output.record,
                                    "kind": "authoring_draft",
                                    "requires_full_review": True,
                                    "execution_run_id": run_id,
                                    "code_sha256": candidate.code_sha256,
                                },
                                output.files,
                                output.image_id,
                            )
                        else:
                            before = await executor.inspect_source(
                                run_id=digest((run_id + ":before").encode()),
                                source=source.content,
                                expected_image_id=runtime.image_id,
                                slides=None,
                                detail="index",
                            )
                            before = await executor.inspect_source(
                                run_id=digest((run_id + ":before-objects").encode()),
                                source=source.content,
                                expected_image_id=runtime.image_id,
                                slides=list(range(1, before["slide_count"] + 1)),
                                detail="objects",
                            )
                            after = await executor.inspect_source(
                                run_id=digest((run_id + ":after-objects").encode()),
                                source=candidate.content,
                                expected_image_id=runtime.image_id,
                                slides=list(range(1, output.record["preview"]["page_count"] + 1)),
                                detail="objects",
                            )
                            output = review_result(output, source=before, candidate=after, execution_run_id=run_id, code_sha256=candidate.code_sha256)
                        if authorize_inputs is not None:
                            await authorize_inputs()
                        await delivery.save_draft(scope=scope, job_id=job_id, owner=worker.owner_id, result=output)
                        saved = await delivery.get(scope=scope, job_id=job_id)
                        return DocumentPlanningReview(job_id, saved["draft_digest"])
                    elif call.name == "clarification_ask":
                        if attempt_no == 2:
                            await fail_document_turn(delivery, worker, job_id, "document_information_incomplete")
                            return DocumentPlanningFailure("incomplete", job_id, "document_information_incomplete")
                        questions = AskArguments.model_validate(call.arguments).model_dump()["questions"]
                        if len(reply.tool_calls) != 1:
                            raise OfficePackageError("planning_invalid_question")
                        question = questions[0]["question"]
                        response = _json(
                            {
                                "action": "ask",
                                "question": question,
                                "question_call_id": call.id,
                                "messages": [asdict(message) for message in messages],
                                "used_tools": used_tools,
                                "used_steps": used_steps,
                            }
                        )
                        if len(response.encode()) > 512_000:
                            raise OfficePackageError("document_worker_output_limit")
                        await delivery.transition(scope=scope, job_id=job_id, owner=worker.owner_id, state="rendering", response=response)
                        async with open_shared_ctx(delivery.path) as db:
                            await db.execute("BEGIN IMMEDIATE")
                            ref = await retain_document_question_in_tx(db, worker=worker, job_id=job_id, question=question, last_call_no=call_no)
                            from bowhead_core.document_clarification_lifecycle import prepare_document_pause_in_tx

                            await prepare_document_pause_in_tx(db, store=clarification_store, worker=worker, checkpoint=ref, questions=questions)
                            await db.commit()
                        return DocumentPlanningPause(job_id, ref, question)
                    else:
                        value = {"error": "document_tool_unavailable"}
                except ScriptExecutionError as error:
                    if script_attempts >= MAX_SCRIPT_ATTEMPTS:
                        raise
                    value = {"error": error.code, "diagnostics": error.diagnostics, "run_id": error.run_id, "outcome": "known_failed"}
                except ValidationError as error:
                    value = {"error": "document_tool_arguments_invalid", "fields": [list(item["loc"]) for item in error.errors(include_input=False)]}
                except DocumentRequestError as error:
                    if call.name != "document_read" or error.code not in {
                        "document_canvas_selection_invalid",
                        "document_canvas_section_unavailable",
                        "document_canvas_view_unavailable",
                        "document_canvas_dataset_unavailable",
                    }:
                        raise
                    value = {
                        "error": error.code,
                        "hint": "Read the Canvas input directory, then choose an offered section/view and next_offset. "
                        "For linked files use detail='datasets', then dataset_alias alone with the same source. "
                        "Unavailable files have no readable bytes; use available sources or ask for the missing data.",
                    }
                messages.append(Message(role="tool", name=call.name, tool_call_id=call.id, content=_json(value)))
        raise DocumentRequestError("document_authoring_budget_exhausted")
    except ContextCapacityError as error:
        budget = error.budget
        log.warning(
            "document.context_limit job_id=%s context_window=%s context_source=%s input_estimate=%s estimate_coverage=%s required_output=%s",
            job_id,
            budget.context_window,
            budget.context_source,
            budget.input_estimate,
            budget.estimate_coverage,
            budget.required_output,
        )
        code = "document_context_limit"
    except InvocationOutcomeUnknownError:
        code = "document_usage_unresolved"
    except (DocumentRequestError, OfficePackageError) as error:
        code = error.code
    except Exception:
        code = "document_execution_failed"
    except BaseException:
        with anyio.CancelScope(shield=True):
            await fail_document_turn(delivery, worker, job_id, "document_execution_interrupted")
        raise
    return await fail_document_turn(delivery, worker, job_id, code)
