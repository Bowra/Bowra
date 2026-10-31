"""Gateway lifecycle adapter for ordinary-PPTX private drafts."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path

import anyio
import httpx2
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingError
from bowhead_core.agent_run_limits import DEFAULT_LIMITS, get_tool_call_budget
from bowhead_core.document_chat_continuation import DocumentPlanningAttemptRef, DocumentTurnOwner
from bowhead_core.documents.agent_caller import authorize_document_job_caller, read_document_caller_in_tx
from bowhead_core.documents.authoring_context import CANVAS_REFERENCE, runtime_api_reference
from bowhead_core.documents.authoring_context import SYSTEM as AUTHORING_SYSTEM
from bowhead_core.documents.authoring_executor import AuthoringExecutor
from bowhead_core.documents.authoring_protocol import PROFILE as AUTHORING_PROFILE
from bowhead_core.documents.authoring_runner import run_authoring_turn
from bowhead_core.documents.canvas_inputs import CanvasInputStore, inherit_canvas_input_in_tx
from bowhead_core.documents.continuation import DocumentPlanningOutcome, DocumentRuntimeSnapshot
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.executor import PROFILE, DocumentExecutor
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.pptx_plan import CURRENT_PROFILE
from bowhead_core.documents.request import DocumentModelSelection, DocumentRequestError, DocumentScope
from bowhead_core.documents.runner import run_document
from bowhead_core.documents.store import DocumentReceipt, DocumentRuntimeAdmission, DocumentStore
from bowhead_core.documents.turn_runner import fail_document_turn, run_document_turn
from bowhead_core.documents.usage import settle_pending_usage
from bowhead_core.model_clock import turn_time_zone_name
from bowhead_core.team_llm_config import build_catalog, is_allowed_preferred_model
from bowhead_router.errors import RetryPolicy
from bowhead_router.router import UserModelPref
from bowhead_router.types import LLMRequest, Message

from bowhead_gateway.document_canvas_inputs import DocumentCanvasInputs
from bowhead_gateway.turn_service import resolve_team_model_runtime

log = logging.getLogger(__name__)


def document_chat_continuation_enabled() -> bool:
    """Admission only; retained jobs never switch execution owners on flag-off."""
    return os.environ.get("FEATURE_DOCUMENT_CHAT_CONTINUATION", "1") == "1"


def _worker(ctx) -> DocumentTurnOwner:
    return DocumentTurnOwner(
        team_id=ctx.team_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        generation=ctx.execution_generation,
        owner_id=ctx.execution_owner_id,
    )


def _with_space_block(system: str, block: str) -> str:
    return f"{system}\n\n{block}" if block else system


class DocumentService:
    def __init__(self, state):
        self.state = state
        self.delivery = DocumentDeliveryStore(state.artifact_store)
        self.jobs = DocumentStore(state.artifact_store.db_path)
        self.canvas_inputs = CanvasInputStore(state.artifact_store)
        self.canvas_sources = DocumentCanvasInputs(state)

    async def authorize_canvas(self, scope: DocumentScope, job_id: str):
        # Validate the original job before applying its publication lifecycle.
        if await self.jobs.get(scope=scope, job_id=job_id) is None:
            raise DocumentRequestError("document_not_found")
        async with (
            open_shared_ctx(self.delivery.path) as db,
            db.execute("SELECT state FROM document_executions WHERE job_id=?", (job_id,)) as cursor,
        ):
            execution = await cursor.fetchone()
        if execution is not None and execution[0] == "published":
            return
        material = await self.canvas_inputs.get(scope=scope, job_id=job_id, frozen=False)
        if material is not None:
            await self.canvas_sources.authorize(scope=scope, manifest=material[1])

    def executor(self) -> DocumentExecutor:
        root = os.environ.get("BOWHEAD_SKILL_WORKDIR_ROOT", "")
        if not root:
            raise DocumentRequestError("document_runtime_unavailable")
        return DocumentExecutor(
            root=Path(root), url=os.environ.get("BOWHEAD_SANDBOX_EXECUTOR_URL", ""), token=os.environ.get("BOWHEAD_SANDBOX_EXECUTOR_TOKEN", "")
        )

    async def authorize_publication(self, scope: DocumentScope):
        entitlements = getattr(self.state, "entitlements", None)
        if entitlements is None:
            if getattr(self.state, "deployment_mode", "saas") == "local":
                raise DocumentRequestError("document_capability_unavailable")
        else:
            await entitlements.assert_can_run_turn(scope.team_id)

    async def authorize_model(self, scope: DocumentScope):
        await self.authorize_publication(scope)
        runtime = await resolve_team_model_runtime(self.state, scope.team_id, entrypoint="tool_continuation")
        if not runtime.available:
            raise DocumentRequestError("document_model_unavailable")
        return runtime.router

    async def questions(self, ctx):
        if not callable(ctx.document_admission_binding):
            raise DocumentRequestError("document_capability_unavailable")
        if ctx.document_caller_execution_id is not None:
            async with open_shared_ctx(self.delivery.path) as db:
                await db.execute("BEGIN")
                await read_document_caller_in_tx(
                    db,
                    path=self.delivery.path,
                    scope=DocumentScope(team_id=ctx.team_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id),
                    turn_id=ctx.turn_id,
                    execution_id=ctx.document_caller_execution_id,
                )
        return await self.jobs.questions_from_turn(
            scope=DocumentScope(team_id=ctx.team_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id), turn_id=ctx.turn_id
        )

    async def submit(self, ctx, proposal, *, answer_to_job_id=None):
        if not callable(ctx.document_admission_binding):
            raise DocumentRequestError("document_capability_unavailable")
        scope = DocumentScope(team_id=ctx.team_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
        command_id = "document-submit:" + ctx.turn_id

        async def recover():
            previous = await self.jobs.recover_from_turn(
                scope=scope, turn_id=ctx.turn_id, command_id=command_id, proposal=proposal, parent_job_id=answer_to_job_id
            )
            if previous is not None and ctx.document_caller_execution_id is not None:
                await authorize_document_job_caller(
                    self.delivery.path,
                    scope=scope,
                    turn_id=ctx.turn_id,
                    job_id=previous.job_id,
                    execution_id=ctx.document_caller_execution_id,
                )
            return previous

        previous = await recover()
        if previous is not None:
            return previous
        try:
            prepared = await self.jobs.prepare_from_turn(scope=scope, turn_id=ctx.turn_id, proposal=proposal, parent_job_id=answer_to_job_id)
        except DocumentRequestError:
            previous = await recover()
            if previous is not None:
                return previous
            raise
        preference = UserModelPref.from_request_fields(ctx.preferred_provider, ctx.preferred_model)
        config, model_version = await self.state.user_store.get_team_llm_config_with_version(scope.team_id)
        router = await self.authorize_model(scope)
        _, current_version = await self.state.user_store.get_team_llm_config_with_version(scope.team_id)
        if current_version != model_version:
            raise DocumentRequestError("document_model_configuration_changed")
        provider, model = router.resolve("lead", user_pref=preference).primary
        selection = DocumentModelSelection(provider=provider, model=model)
        if not is_allowed_preferred_model(build_catalog(config=config, version=model_version), provider, model):
            raise DocumentRequestError("document_model_unavailable")
        continuation = document_chat_continuation_enabled()
        if not continuation and ctx.document_caller_execution_id is not None:
            raise DocumentRequestError("document_capability_unavailable")
        authoring = continuation and os.environ.get("FEATURE_DOCUMENT_SCRIPT_AUTHORING", "0") == "1"
        try:
            executor = self.executor()
            if authoring:
                executor = AuthoringExecutor(root=executor.root, url=executor.url, token=executor.token)
                readiness = await executor.capabilities()
            else:
                async with httpx2.AsyncClient(
                    timeout=10, headers={"X-Executor-Token": executor.token}, trust_env=False, follow_redirects=False
                ) as client:
                    response = await client.get(executor.url + "/document-runs/capabilities")
                    response.raise_for_status()
                    readiness = response.json()
                    if not isinstance(readiness, dict) or readiness.get("profile") != PROFILE or "image_id" not in readiness:
                        raise DocumentRequestError("document_runtime_unavailable")
                    if continuation and (
                        not isinstance(readiness.get("planner_profiles"), list) or CURRENT_PROFILE not in readiness["planner_profiles"]
                    ):
                        raise DocumentRequestError("document_runtime_unavailable")
        except (httpx2.HTTPError, ValueError, OfficePackageError) as exc:
            # No admission has been attempted: a failed readiness read is a
            # known refusal, not an uncertain write or a missing draft card.
            raise DocumentRequestError("document_runtime_unavailable") from exc
        canvas_input = None
        if prepared.revision is not None or prepared.answer is not None:
            parent_id = prepared.revision.target.job_id if prepared.revision is not None else prepared.answer.parent_job_id
            async with open_shared_ctx(self.delivery.path) as db:
                await db.execute("BEGIN")
                canvas_input = await inherit_canvas_input_in_tx(db, scope=scope, job_id=parent_id)
            if canvas_input is not None:
                await self.canvas_sources.authorize(scope=scope, manifest=canvas_input.manifest)
        elif prepared.proposal.canvas is not None:
            if not authoring:
                raise DocumentRequestError("document_canvas_unavailable")
            canvas_input = await self.canvas_sources.prepare(scope=scope, session_id=ctx.session_id, selection=prepared.proposal.canvas)
        binding = None
        owns_binding = False
        try:
            try:
                binding, owns_binding = await ctx.document_admission_binding()
                if callable(ctx.document_admission_guard):
                    await ctx.document_admission_guard(binding.execution_id)
                if ctx.document_caller_execution_id is not None:
                    if binding.execution_id != ctx.document_caller_execution_id or owns_binding:
                        raise DocumentRequestError("document_capability_unavailable")
                    async with open_shared_ctx(self.delivery.path) as db:
                        await db.execute("BEGIN")
                        binding = await read_document_caller_in_tx(
                            db,
                            path=self.delivery.path,
                            scope=scope,
                            turn_id=ctx.turn_id,
                            execution_id=binding.execution_id,
                        )
            except Exception as exc:
                # Another identical admission may have committed after preflight.
                # Only a durable scoped receipt can explain a terminal binding.
                previous = await recover()
                if previous is not None:
                    return previous
                if isinstance(exc, AgentExecutionBindingError) and exc.code == "agent_execution_already_terminal":
                    raise DocumentRequestError("document_capability_unavailable") from exc
                raise
            try:
                worker = _worker(ctx) if continuation else None
                authoring_limits = asdict(getattr(ctx, "agent_run_limits", None) or DEFAULT_LIMITS)
                runtime_budget = getattr(ctx, "agent_runtime_budget", None)
                if authoring and runtime_budget is not None and runtime_budget.step_limit is not None:
                    configured_steps = authoring_limits["step_limit"]
                    authoring_limits["step_limit"] = (
                        min(configured_steps, runtime_budget.step_limit) if configured_steps is not None else runtime_budget.step_limit
                    )
                tool_budget = get_tool_call_budget()
                if authoring and tool_budget is not None:
                    authoring_limits["tool_call_limit"] = tool_budget.limit - tool_budget.used
                    if authoring_limits["tool_call_limit"] <= 0:
                        raise DocumentRequestError("document_authoring_budget_exhausted")
                snapshot = (
                    DocumentRuntimeSnapshot(
                        schema_version=2 if authoring else 1,
                        image_id=readiness["image_id"],
                        model_config_version=model_version,
                        planner_profile=AUTHORING_PROFILE if authoring else CURRENT_PROFILE,
                        authoring_system=AUTHORING_SYSTEM + runtime_api_reference(readiness) + (CANVAS_REFERENCE if canvas_input is not None else "")
                        if authoring
                        else None,
                        authoring_limits=authoring_limits if authoring else None,
                        # The Turn's bound viewer zone also covers i18n-off Turns, whose language
                        # context has none; an empty zone leaves the snapshot (and digest) unchanged.
                        language=ctx.language_context.with_time_zone(turn_time_zone_name()).to_dict(),
                    )
                    if worker
                    else None
                )
                runtime = DocumentRuntimeAdmission(readiness["image_id"], model_version, binding, owns_binding, worker, snapshot, selection)
                return await self.jobs.submit_from_turn(
                    scope=scope,
                    turn_id=ctx.turn_id,
                    command_id=command_id,
                    proposal=prepared.proposal,
                    runtime=runtime,
                    _answer=prepared.answer,
                    _revision=prepared.revision,
                    _canvas=canvas_input,
                )
            except DocumentRequestError:
                # A concurrent exact request may have won with another runtime
                # snapshot. Compare user intent against the winner, not today's
                # configuration; changed requests must still conflict.
                previous = await recover()
                if previous is not None:
                    return previous
                raise
        except BaseException as error:
            if binding is None or owns_binding:
                with anyio.CancelScope(shield=True):
                    try:
                        # The binding commit can finish before its await returns.
                        # Resolve only the reserved document-owned identity; never
                        # close a borrowed Lead or an already committed request.
                        await self.jobs.close_uncommitted_admission(
                            scope=scope,
                            turn_id=ctx.turn_id,
                            cancelled=isinstance(error, asyncio.CancelledError),
                        )
                    except Exception as exc:
                        log.warning("document_admission.cleanup_failed error_type=%s", type(exc).__name__)
            raise

    async def _space_instructions_block(self, job_id: str) -> str:
        """Space instructions the job's originating Turn received; every call of the job uses the same version."""
        store = getattr(self.state, "space_instructions_store", None)
        if store is None:
            return ""
        try:
            async with (
                open_shared_ctx(self.delivery.path) as db,
                db.execute("SELECT origin_turn_id FROM document_jobs WHERE id=?", (job_id,)) as cursor,
            ):
                row = await cursor.fetchone()
            if row is None or not row[0]:
                return ""
            snapshot = await store.block_for_turn(row[0])
        except Exception as exc:
            log.warning("document.space_instructions_unavailable error_type=%s", type(exc).__name__)
            return ""
        return snapshot.block if snapshot is not None else ""

    async def _invoke(self, scope: DocumentScope, config_version: int, system: str, prompt: str, *, job_id: str, messages=None, tools=()):
        async with (
            open_shared_ctx(self.delivery.path) as db,
            db.execute(
                """SELECT d.model_selection_json FROM document_dispatches d JOIN document_jobs j ON j.id=d.job_id
                WHERE j.id=? AND j.team_id=? AND j.workspace_id=? AND j.requested_by=?""",
                (job_id, *scope.sql_params()),
            ) as cursor,
        ):
            dispatch = await cursor.fetchone()
        if dispatch is None:
            raise DocumentRequestError("document_not_found")
        selection = DocumentModelSelection.model_validate_json(dispatch[0]) if dispatch[0] is not None else None
        config, current_version = await self.state.user_store.get_team_llm_config_with_version(scope.team_id)
        if config_version != current_version:
            raise DocumentRequestError("document_model_configuration_changed")
        router = await self.authorize_model(scope)
        _, after_version = await self.state.user_store.get_team_llm_config_with_version(scope.team_id)
        if after_version != current_version:
            raise DocumentRequestError("document_model_configuration_changed")
        if selection is not None and not is_allowed_preferred_model(
            build_catalog(config=config, version=current_version), selection.provider, selection.model
        ):
            raise DocumentRequestError("document_model_unavailable")
        reply = await router.invoke(
            "lead" if selection is not None else "echo",
            LLMRequest(
                model="",
                system=system,
                messages=messages if messages is not None else [Message(role="user", content=prompt)],
                tools=list(tools),
                temperature=0,
            ),
            user_pref=UserModelPref(selection.provider, selection.model) if selection is not None else None,
            retry_policy=RetryPolicy(max_attempts_per_target=1),
        )
        # The Router owns automatic allowances and explicit administrator overrides.
        # Retain observed limits, not prompt/response text or a guessed truncation cause.
        response_bytes = len(reply.content.encode())
        observed = reply.diagnostics.trace_fields() if reply.diagnostics is not None else {}
        finish = reply.finish_reason if reply.finish_reason in {"stop", "length", "tool_calls", "tool_use", "content_filter"} else "unknown"
        write_log = log.warning if finish not in {"stop", "tool_use", "tool_calls"} or response_bytes > 64_000 else log.debug
        write_log(
            "document.planning_response job_id=%s finish_reason=%s response_bytes=%d response_byte_limit=64000 "
            "caller_allowance=automatic wire_allowance=%s upstream_finish_reason=%s incomplete_reason=%s",
            job_id,
            finish,
            response_bytes,
            observed.get("output_token_limit"),
            observed.get("upstream_finish_reason"),
            observed.get("incomplete_reason"),
        )
        return reply

    async def run_turn(self, ctx, receipt: DocumentReceipt) -> DocumentPlanningOutcome | None:
        scope = DocumentScope(team_id=ctx.team_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
        job = await self.jobs.get(scope=scope, job_id=receipt.job_id)
        if job is None:
            raise DocumentRequestError("document_not_found")
        async with (
            open_shared_ctx(self.delivery.path) as db,
            db.execute(
                "SELECT d.runtime_snapshot_json FROM document_jobs j LEFT JOIN document_turn_dispatches d ON d.job_id=j.id WHERE j.id=? AND j.origin_turn_id=? AND j.session_id=?",
                (receipt.job_id, ctx.turn_id, ctx.session_id),
            ) as cursor,
        ):
            dispatch = await cursor.fetchone()
        if dispatch is None:
            raise DocumentRequestError("document_not_found")
        if dispatch[0] is None:
            return None
        return await self._run_turn(ctx, receipt.job_id, 1, DocumentRuntimeSnapshot.model_validate_json(dispatch[0]))

    async def resume_turn(self, ctx, attempt: DocumentPlanningAttemptRef) -> DocumentPlanningOutcome:
        if attempt.attempt_no != 2 or attempt.worker != _worker(ctx):
            raise DocumentRequestError("document_execution_fenced")
        async with (
            open_shared_ctx(self.delivery.path) as db,
            db.execute("SELECT runtime_snapshot_json FROM document_turn_dispatches WHERE job_id=?", (attempt.job_id,)) as cursor,
        ):
            dispatch = await cursor.fetchone()
        if dispatch is None:
            raise DocumentRequestError("document_checkpoint_unavailable")
        return await self._run_turn(ctx, attempt.job_id, 2, DocumentRuntimeSnapshot.model_validate_json(dispatch[0]))

    async def _run_turn(self, ctx, job_id, attempt_no, snapshot):
        scope = DocumentScope(team_id=ctx.team_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
        worker = _worker(ctx)
        try:
            await authorize_document_job_caller(self.delivery.path, scope=scope, turn_id=worker.turn_id, job_id=job_id)
            executor = self.executor()
        except DocumentRequestError as exc:
            return await fail_document_turn(self.delivery, worker, job_id, exc.code)
        except (ValueError, OSError):
            return await fail_document_turn(self.delivery, worker, job_id, "document_runtime_unavailable")

        space_block = await self._space_instructions_block(job_id)

        if snapshot.schema_version == 2:
            executor = AuthoringExecutor(root=executor.root, url=executor.url, token=executor.token)

            async def invoke_authoring(system, messages, tools):
                return await self._invoke(
                    scope, snapshot.model_config_version, _with_space_block(system, space_block), "", job_id=job_id, messages=messages, tools=tools
                )

            async def authorize_inputs():
                await self.authorize_canvas(scope, job_id)
                await authorize_document_job_caller(self.delivery.path, scope=scope, turn_id=worker.turn_id, job_id=job_id)

            async def prepare_inputs():
                await self.canvas_inputs.freeze(
                    scope=scope,
                    job_id=job_id,
                    materialize=lambda manifest: self.canvas_sources.materialize(scope=scope, manifest=manifest),
                    authorize=lambda manifest: self.canvas_sources.authorize(scope=scope, manifest=manifest),
                )

            return await run_authoring_turn(
                delivery=self.delivery,
                executor=executor,
                worker=worker,
                job_id=job_id,
                attempt_no=attempt_no,
                clarification_store=ctx.clarification_store,
                invoke=invoke_authoring,
                authorize_inputs=authorize_inputs,
                prepare_inputs=prepare_inputs,
            )

        async def invoke(system, prompt):
            await authorize_document_job_caller(self.delivery.path, scope=scope, turn_id=worker.turn_id, job_id=job_id)
            return await self._invoke(scope, snapshot.model_config_version, _with_space_block(system, space_block), prompt, job_id=job_id)

        return await run_document_turn(
            delivery=self.delivery,
            executor=executor,
            worker=worker,
            job_id=job_id,
            attempt_no=attempt_no,
            clarification_store=ctx.clarification_store,
            invoke=invoke,
        )

    async def _run(self, scope: DocumentScope, job_id: str, image_id: str, config_version: int):

        space_block = await self._space_instructions_block(job_id)

        async def invoke(system: str, prompt: str):
            return await self._invoke(scope, config_version, _with_space_block(system, space_block), prompt, job_id=job_id)

        try:
            executor = self.executor()
        except (DocumentRequestError, ValueError, OSError):
            owner = await self.delivery.claim(scope=scope, job_id=job_id)
            if owner:
                await self.delivery.fail(job_id=job_id, owner=owner, code="document_runtime_unavailable")
            return
        await run_document(delivery=self.delivery, executor=executor, scope=scope, job_id=job_id, invoke=invoke, expected_image_id=image_id)

    async def run(self):
        # One process executes one job at a time; database claims prevent the
        # same job from being dispatched by another application instance.
        next_cleanup = 0.0
        scan_after = ("", "")
        while True:
            try:
                await self.delivery.recover_expired()
                await settle_pending_usage(str(self.delivery.path))
                if time.monotonic() >= next_cleanup:
                    await self.delivery.cleanup()
                    next_cleanup = time.monotonic() + 60
                async with (
                    open_shared_ctx(self.delivery.path) as db,
                    db.execute(
                        """SELECT j.id,j.team_id,j.workspace_id,j.requested_by,d.image_id,d.model_config_version FROM document_jobs j
                        JOIN document_dispatches d ON d.job_id=j.id
                        LEFT JOIN document_executions e ON e.job_id=j.id
                        WHERE j.status='queued' AND (e.job_id IS NULL OR (e.state IN ('preparing','planning','rendering') AND e.lease_until<=?))
                        AND NOT EXISTS(SELECT 1 FROM document_turn_dispatches td WHERE td.job_id=j.id)
                        AND (j.created_at,j.id)>(?,?) ORDER BY j.created_at,j.id LIMIT 10""",
                        (time.time(), *scan_after),
                    ) as cursor,
                ):
                    candidates = await cursor.fetchall()
                # Advance past unclaimable jobs as well. Revocation or one busy
                # team must not pin the first page and starve other teams.
                if candidates:
                    async with (
                        open_shared_ctx(self.delivery.path) as db,
                        db.execute("SELECT created_at,id FROM document_jobs WHERE id=?", (candidates[-1][0],)) as cursor,
                    ):
                        scan_after = tuple(await cursor.fetchone())
                else:
                    scan_after = ("", "")
                for job_id, team_id, workspace_id, user_id, image_id, model_version in candidates:
                    try:
                        await self._run(DocumentScope(team_id=team_id, workspace_id=workspace_id, user_id=user_id), job_id, image_id, model_version)
                    except Exception as exc:
                        # A scope revoked before claim is not authority to
                        # execute; it can still be cancelled by its owner.
                        log.warning("document_job.claim_failed job=%s error_type=%s", job_id, type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("document_job.scan_failed error_type=%s", type(exc).__name__)
            await anyio.sleep(2)
