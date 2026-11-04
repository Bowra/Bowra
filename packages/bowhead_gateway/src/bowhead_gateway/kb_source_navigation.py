"""Original-user context and bounded background execution for source navigation."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import anyio
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.source_description import description_assignment, description_request, generate_description, validate_description
from bowhead_core.kb.source_navigation_store import NavigationConflict, SourceNavigationStore
from bowhead_core.rate_card import RATE_CARD_VERSION, ModelCreditRates, ModelTokenUsage, ModelUsageLine, credits_for_model_lines
from bowhead_router.usage import usage_context

from bowhead_gateway.kb_authorization import kb_file_authorizer

log = logging.getLogger(__name__)


def navigation_context(app: Any, scope: ScopeContext) -> Any:
    """Bind server identity and original authorizers, never model-authored ACLs."""
    return SimpleNamespace(
        user_id=scope.user_id,
        team_id=scope.team_id,
        workspace_id=scope.workspace_id,
        kb_files_store=app.state.kb_files_store,
        knowledge_scope=None,
        kb_file_authorizer=kb_file_authorizer(
            app.state.user_store, getattr(app.state, "access_control_store", None), getattr(app.state, "kb_matrix_store", None)
        ),
        knowledge_navigation_uses={},
        knowledge_navigation_ancestors=(),
    )


async def current_description_assignment(app: Any, team_id: str) -> Any:
    config, _ = await app.state.user_store.get_team_llm_config_with_version(team_id)
    return description_assignment(config)


async def refresh_matrix_navigation(app: Any, scope: Any, matrix: Any, files: dict, cells: list) -> None:
    """Optional cache only: use already authorized native results, never run Matrix."""
    columns = {column["column_id"]: column for column in matrix.columns if column.get("navigation_role", "none") != "none"}
    if not columns:
        return
    try:
        jobs = SourceNavigationStore(app.state.kb_files_store._db_path)
        with anyio.move_on_after(0.2):
            eligible = [cell for cell in cells if cell.column_id in columns and cell.file_id in files][:100]
            published = await jobs.published_generations([files[cell.file_id] for cell in eligible])
            for cell in eligible:
                file = files[cell.file_id]
                if file.active_generation_id not in published:
                    continue
                column = columns[cell.column_id]
                if (
                    cell.status != "done"
                    or cell.edited_by_user
                    or not file.active_generation_id
                    or (cell.kb_generation_id, cell.file_version, cell.prompt_version)
                    != (file.active_generation_id, file.version, column["prompt_version"])
                ):
                    continue
                primary = [value for value in cell.variants if value.get("is_primary")]
                if (
                    len(primary) != 1
                    or primary[0].get("extracted_by") != "tier1"
                    or not primary[0].get("chunk_id")
                    or not primary[0].get("source_excerpt")
                ):
                    continue
                normalized = primary[0].get("normalized")
                value = normalized.get("value") if isinstance(normalized, dict) else None
                role = column["navigation_role"]
                text, topics = "", []
                if role == "description" and isinstance(value, str):
                    text = " ".join(value.split())[:300]
                elif role == "topics":
                    values = value if isinstance(value, list) else [value]
                    if all(isinstance(item, str) for item in values):
                        topics = [" ".join(item.split())[:80] for item in values[:8] if item.strip()]
                if not text and not topics:
                    continue
                proof = {
                    "matrix_id": matrix.matrix_id,
                    "workspace_id": matrix.workspace_id,
                    "column_id": cell.column_id,
                    "structure_seq": matrix.structure_seq,
                    "prompt_version": cell.prompt_version,
                    "cell_seq": cell.seq,
                    "role": role,
                }
                await jobs.cache_matrix(
                    file,
                    text=text,
                    topics=topics,
                    matrix=proof,
                    actor_user_id=scope.user_id,
                    chunks=[primary[0]["chunk_id"]],
                    provider=None,
                    model=None,
                )
    except Exception as exc:
        log.warning("kb.matrix_navigation_refresh_unavailable kind=%s", type(exc).__name__)


async def _job_input(app: Any, job: dict, *, check_balance: bool) -> tuple[Any, Any, dict, int]:
    files = app.state.kb_files_store
    file = await files.get(job["file_id"])
    if (
        not file
        or file.soft_deleted_at
        or file.source_absent
        or file.parse_status != "done"
        or (file.team_id, file.active_generation_id, file.membership_revision, file.version)
        != (job["team_id"], job["generation_id"], job["membership_revision"], job["source_version"])
    ):
        raise NavigationConflict("knowledge_source_changed")
    if file.active_generation_id not in await SourceNavigationStore(files._db_path).published_generations([file]):
        raise NavigationConflict("knowledge_generation_unverified")
    authorize = kb_file_authorizer(app.state.user_store, getattr(app.state, "access_control_store", None))
    if not await authorize(file, job["actor_user_id"], job["team_id"], job["workspace_id"], write=True):
        raise NavigationConflict("knowledge_source_changed")
    config, version = await app.state.user_store.get_team_llm_config_with_version(job["team_id"])
    assignment = description_assignment(config)
    if assignment is None or assignment.fingerprint != job["assignment_fingerprint"]:
        raise NavigationConflict("knowledge_description_assignment_changed")
    if await SourceNavigationStore(files._db_path).revision(file.file_id) != job["manual_revision"]:
        raise NavigationConflict("knowledge_navigation_changed")
    if check_balance:
        entitlements = getattr(app.state, "entitlements", None)
        if entitlements is not None:
            await entitlements.assert_can_run_turn(job["team_id"])
        user = await app.state.user_store.get_user(job["actor_user_id"])
        team = await app.state.user_store.get_team(job["team_id"])
        if user is None or team is None or min(user.credit_balance, team.team_credit_balance) <= 0:
            raise ValueError("knowledge_description_credit_unavailable")
    return file, assignment, config, version


async def run_description_job(app: Any, job: dict) -> None:
    """One claimed invocation; terminal state precedes its one credit settlement."""
    jobs = SourceNavigationStore(app.state.kb_files_store._db_path)
    response = None
    router = None
    usage = {}
    dispatched = False
    try:
        with anyio.fail_after(180):
            file, assignment, config, version = await _job_input(app, job, check_balance=False)
            existing = await jobs.file_wide_descriptions(file)
            if any(row["method"] in {"manual", "standalone"} and row["text"] for row in existing):
                await jobs.finish(job, "ready")
                return
            runtime = await app.state.team_kb_runtime_registry.get(job["team_id"], config, version)
            if runtime is None:
                raise ValueError("knowledge_description_runtime_unavailable")
            chunks = await runtime.kb_store.sample_current_chunks(file)
            request, references, source_text = description_request(file, chunks, assignment)
            # Resolve the owning Team directly: a global runtime Router is not a fallback.
            router = await app.state.team_router_registry.get(job["team_id"], config, version)
            if router is None:
                raise ValueError("knowledge_description_runtime_unavailable")
            await _job_input(app, job, check_balance=True)
            if not await jobs.dispatch(job, min(2, router.max_retries + 1)):
                return
            dispatched = True
            with usage_context(
                team_id=job["team_id"], user_id=job["actor_user_id"], workspace_id=job["workspace_id"], reason="kb_source_description"
            ):
                response = await generate_description(router, assignment, request)
            usage = {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
                "cached": response.usage.cached_tokens,
                "provider": response.provider or assignment.provider,
                "model": response.model or assignment.model,
            }
            output = validate_description(response.content, references, source_text)
            if (
                response.tool_calls
                or (response.provider and response.provider != assignment.provider)
                or (response.model and response.model != assignment.model)
            ):
                raise ValueError("knowledge_description_target_changed")
            current, _, _, _ = await _job_input(app, job, check_balance=False)
            await jobs.adopt(
                job,
                current,
                text=output.description,
                topics=output.topics,
                chunks=output.supporting_chunks,
                provider=usage["provider"],
                model=usage["model"],
                usage=usage,
            )
    except NavigationConflict as exc:
        await jobs.finish(job, "superseded", error=str(exc), usage=usage)
    except Exception as exc:
        safe_errors = {
            "knowledge_description_credit_unavailable",
            "knowledge_description_source_unavailable",
            "knowledge_description_input_too_large",
            "knowledge_description_runtime_unavailable",
        }
        error = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc) in safe_errors
            else "output_invalid"
            if response is not None
            else "outcome_unknown"
            if dispatched
            else "source_unavailable"
        )
        await jobs.finish(job, "failed", error=error, usage=usage)
        log.warning("kb.description_failed job=%s kind=%s", job["id"], type(exc).__name__)
    finally:
        if response is not None and router is not None:
            # Never resubmit a completed/unknown model request to repair accounting.
            with anyio.CancelScope(shield=True), anyio.move_on_after(10):
                try:
                    await jobs.finish(job, "failed", error="adoption_incomplete", usage=usage)
                    rates = ModelCreditRates.from_config(router.model_credit_rates.get((usage["provider"], usage["model"])))
                    model_usage = ModelTokenUsage.from_provider_counts(
                        usage["input"],
                        usage["cached"],
                        usage["output"],
                        cached_in_input=router.model_provider_types.get(usage["provider"]) != "anthropic_compatible",
                        rates=rates,
                    )
                    line = ModelUsageLine(usage["provider"], usage["model"], model_usage)
                    settled, _ = await app.state.user_store.try_deduct_credit(
                        user_id=job["actor_user_id"],
                        team_id=job["team_id"],
                        cost_credits=credits_for_model_lines([line]),
                        turn_id=job["id"],
                        workspace_id=job["workspace_id"],
                        tokens_input=usage["input"],
                        tokens_output=usage["output"],
                        reason="kb_source_description",
                        model_usage=(line,),
                        rate_card_version=RATE_CARD_VERSION,
                    )
                    if not settled:
                        log.warning("kb.description_settlement_declined job=%s", job["id"])
                except Exception as exc:
                    log.warning("kb.description_settlement_unavailable job=%s kind=%s", job["id"], type(exc).__name__)


async def description_loop(app: Any) -> None:
    """Use the Gateway lifecycle; only durable admitted jobs can reach a model."""
    owner = uuid4().hex
    jobs = SourceNavigationStore(app.state.kb_files_store._db_path)
    while True:
        try:
            async with anyio.create_task_group() as group:
                for _ in range(2):
                    job = await jobs.claim(owner)
                    if job is not None:
                        group.start_soon(run_description_job, app, job)
        except Exception as exc:
            log.warning("kb.description_worker_unavailable kind=%s", type(exc).__name__)
        await anyio.sleep(2)
