"""Gateway lifecycle adapter for ordinary-PPTX private drafts."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import anyio
import httpx2
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingError
from bowhead_core.document_chat_continuation import DocumentPlanningAttemptRef, DocumentTurnOwner
from bowhead_core.documents.continuation import DocumentPlanningOutcome, DocumentRuntimeSnapshot
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.executor import PROFILE, DocumentExecutor
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
        if ctx.role != "lead" or not callable(ctx.document_admission_binding):
            raise DocumentRequestError("document_capability_unavailable")
        return await self.jobs.questions_from_turn(
            scope=DocumentScope(team_id=ctx.team_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id), turn_id=ctx.turn_id
        )

    async def submit(self, ctx, proposal, *, answer_to_job_id=None):
        if ctx.role != "lead" or not callable(ctx.document_admission_binding):
            raise DocumentRequestError("document_capability_unavailable")
        scope = DocumentScope(team_id=ctx.team_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
        command_id = "document-submit:" + ctx.turn_id
        previous = await self.jobs.recover_from_turn(
            scope=scope, turn_id=ctx.turn_id, command_id=command_id, proposal=proposal, parent_job_id=answer_to_job_id
        )
        if previous is not None:
            return previous
        try:
            prepared = await self.jobs.prepare_from_turn(scope=scope, turn_id=ctx.turn_id, proposal=proposal, parent_job_id=answer_to_job_id)
        except DocumentRequestError:
            previous = await self.jobs.recover_from_turn(
                scope=scope, turn_id=ctx.turn_id, command_id=command_id, proposal=proposal, parent_job_id=answer_to_job_id
            )
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
        try:
            executor = self.executor()
            async with httpx2.AsyncClient(
                timeout=10, headers={"X-Executor-Token": executor.token}, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.get(executor.url + "/document-runs/capabilities")
                response.raise_for_status()
                readiness = response.json()
                if not isinstance(readiness, dict) or readiness.get("profile") != PROFILE or "image_id" not in readiness:
                    raise DocumentRequestError("document_runtime_unavailable")
                if continuation and (not isinstance(readiness.get("planner_profiles"), list) or CURRENT_PROFILE not in readiness["planner_profiles"]):
                    raise DocumentRequestError("document_runtime_unavailable")
        except (httpx2.HTTPError, ValueError) as exc:
            # No admission has been attempted: a failed readiness read is a
            # known refusal, not an uncertain write or a missing draft card.
            raise DocumentRequestError("document_runtime_unavailable") from exc
        binding = None
        owns_binding = False
        try:
            try:
                binding, owns_binding = await ctx.document_admission_binding()
            except Exception as exc:
                # Another identical admission may have committed after preflight.
                # Only a durable scoped receipt can explain a terminal binding.
                previous = await self.jobs.recover_from_turn(
                    scope=scope, turn_id=ctx.turn_id, command_id=command_id, proposal=proposal, parent_job_id=answer_to_job_id
                )
                if previous is not None:
                    return previous
                if isinstance(exc, AgentExecutionBindingError) and exc.code == "agent_execution_already_terminal":
                    raise DocumentRequestError("document_capability_unavailable") from exc
                raise
            try:
                worker = _worker(ctx) if continuation else None
                snapshot = (
                    DocumentRuntimeSnapshot(
                        schema_version=1,
                        image_id=readiness["image_id"],
                        model_config_version=model_version,
                        planner_profile=CURRENT_PROFILE,
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
                )
            except DocumentRequestError:
                # A concurrent exact request may have won with another runtime
                # snapshot. Compare user intent against the winner, not today's
                # configuration; changed requests must still conflict.
                previous = await self.jobs.recover_from_turn(
                    scope=scope, turn_id=ctx.turn_id, command_id=command_id, proposal=proposal, parent_job_id=answer_to_job_id
                )
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

    async def _invoke(self, scope: DocumentScope, config_version: int, system: str, prompt: str, *, job_id: str):
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
            LLMRequest(model="", system=system, messages=[Message(role="user", content=prompt)], temperature=0),
            user_pref=UserModelPref(selection.provider, selection.model) if selection is not None else None,
            retry_policy=RetryPolicy(max_attempts_per_target=1),
        )
        # The Router owns automatic allowances and explicit administrator overrides.
        # Retain observed limits, not prompt/response text or a guessed truncation cause.
        response_bytes = len(reply.content.encode())
        observed = reply.diagnostics.trace_fields() if reply.diagnostics is not None else {}
        finish = reply.finish_reason if reply.finish_reason in {"stop", "length", "tool_calls", "content_filter"} else "unknown"
        write_log = log.warning if finish != "stop" or response_bytes > 64_000 else log.debug
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
            executor = self.executor()
        except (DocumentRequestError, ValueError, OSError):
            return await fail_document_turn(self.delivery, worker, job_id, "document_runtime_unavailable")

        space_block = await self._space_instructions_block(job_id)

        async def invoke(system, prompt):
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
