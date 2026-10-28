"""Capability-backed work proposals for the Bowhead homepage."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4
from weakref import WeakValueDictionary

from bowhead_core.capability_resolver import (
    CapabilityPrincipal,
    CapabilityRef,
    CapabilityResolver,
)
from bowhead_core.home_proposal_cache_store import (
    HomeProposalCacheEntry,
    HomeProposalCacheStore,
)
from bowhead_core.home_proposals import (
    ContextRef,
    HomeProposal,
    ProposalCandidateOutcome,
    ProposalFallbackReason,
    ProposalRejectionReason,
    build_envelope,
    generate_deterministic_proposals,
    generate_llm_proposals,
    platform_capability_refs,
    proposal_copy_fingerprint,
    skill_capability_ref,
    subagent_capability_ref,
    validate_proposals,
)
from bowhead_core.skill_access import skill_visible_by_grants
from bowhead_core.tool_registry import ToolExecutionContext
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway.access_context import (
    access_context_to_dict,
    build_access_context_from_request,
)
from bowhead_gateway.routes import home
from bowhead_gateway.routes.auth import _current_user
from bowhead_skill import load_skill

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

router = APIRouter()
logger = logging.getLogger(__name__)
_CACHE_TTL = timedelta(hours=24)
_CONTEXT_STALE_TTL = timedelta(hours=6)
_LLM_FAILURE_RETRY_TTL = timedelta(minutes=15)
_PROPOSAL_LOCKS: WeakValueDictionary[tuple[str, str, str], asyncio.Lock] = WeakValueDictionary()
HomeProposalFallbackReason = (
    ProposalFallbackReason
    | Literal[
        "router_unavailable",
        "timeout",
        "provider_error",
        "empty_result",
    ]
)


class HomeProposalItemDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["analyze", "create", "act", "automate"]
    title: str
    reason: str
    prompt: str
    capabilityRefs: list[str]
    contextRefs: list[str] = Field(default_factory=list)


class _CachedHomeProposalItemDTO(HomeProposalItemDTO):
    outputFormat: str | None = None


class HomeProposalResponseDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["home.proposals.v1"] = "home.proposals.v1"
    generatedAt: str
    strategy: Literal["llm", "deterministic"]
    cached: bool
    generationId: str
    items: list[HomeProposalItemDTO]


def _cache_store(request: Request) -> HomeProposalCacheStore:
    store: HomeProposalCacheStore | None = getattr(
        request.app.state,
        "home_proposal_cache_store",
        None,
    )
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "home proposal cache not configured",
        )
    return store


async def _visible_skill_refs(
    request: Request,
    ctx: ToolExecutionContext,
    *,
    user_id: str,
    team_id: str,
    user_store: UserStore,
    shared_skill_ids: frozenset[str] | None,
) -> tuple[CapabilityRef, ...]:
    catalog = getattr(request.app.state, "skill_catalog", None)
    if catalog is None:
        return ()
    instance_store = getattr(request.app.state, "skill_instance_store", None)
    refs: list[CapabilityRef] = []
    seen: set[str] = set()
    # One read of the user's instances (unique per user+name) instead of one
    # connection per catalog skill on every proposal request.
    instances = await instance_store.list_for_user(user_id=user_id) if instance_store is not None else []
    instance_by_name = {str(getattr(instance, "name", "")): instance for instance in instances}
    for spec in catalog.list_specs():
        if str(getattr(spec, "audience", "all")) != "all":
            continue
        name = str(spec.name)
        instance = instance_by_name.get(name)
        is_user_collision = instance is not None and str(getattr(instance, "source_type", "")) in {"user_upload", "user_generated"}
        if instance is not None and not is_user_collision and not bool(getattr(instance, "enabled", True)):
            continue
        if not await skill_visible_by_grants(ctx, name):
            continue
        refs.append(skill_capability_ref(spec))
        seen.add(name)

    if instance_store is not None:
        for instance in instances:
            name = str(getattr(instance, "name", ""))
            if (
                not name
                or name in seen
                or str(getattr(instance, "source_type", "")) not in {"user_upload", "user_generated"}
                or not bool(getattr(instance, "enabled", False))
                or not await skill_visible_by_grants(ctx, name)
            ):
                continue
            spec = _load_skill_spec(str(getattr(instance, "source_path", "")), expected_name=name)
            if spec is None or str(getattr(spec, "audience", "all")) != "all":
                continue
            refs.append(skill_capability_ref(spec))
            seen.add(name)

    from bowhead_core.workflow_creator_trigger import workflow_creator_enabled

    if workflow_creator_enabled():
        for record in await user_store.list_team_skills(team_id, only_enabled=True):
            name = str(getattr(record, "name", ""))
            visible = str(getattr(record, "visibility", "team_wide")) == "team_wide" or getattr(record, "created_by", None) == user_id
            visible = visible or bool(shared_skill_ids and getattr(record, "id", None) in shared_skill_ids)
            if (
                not name
                or name in seen
                or not visible
                # P1a: team origin passes id (+created_by for narrow owner exemption).
                or not await skill_visible_by_grants(
                    ctx,
                    name,
                    team_skill_id=str(getattr(record, "id", "") or "") or None,
                    team_skill_created_by=getattr(record, "created_by", None),
                )
            ):
                continue
            spec = _load_skill_spec(str(getattr(record, "source_path", "")), expected_name=name)
            if spec is None or str(getattr(spec, "audience", "all")) != "all":
                continue
            refs.append(skill_capability_ref(spec))
            seen.add(name)
    return tuple(refs)


def _load_skill_spec(source_path: str, *, expected_name: str) -> Any | None:
    try:
        spec = load_skill(Path(source_path))
    except Exception:
        return None
    return spec if str(getattr(spec, "name", "")) == expected_name else None


async def _visible_subagent_refs(
    orchestrator: Any,
    workspace_id: str,
    *,
    user_id: str,
    team_id: str,
    shared_subagent_ids: frozenset[str] | None,
) -> tuple[CapabilityRef, ...]:
    registry = getattr(orchestrator, "multi_agent_registry", None)
    if registry is None:
        return ()
    from bowhead_core.subagent import is_subagent_spec_routing_enabled

    if is_subagent_spec_routing_enabled():
        from bowhead_core.agents.lead_orchestrator import _gather_lead_peers
        from bowhead_core.subagent_creator_trigger import subagent_creator_enabled

        peers = await _gather_lead_peers(
            registry=registry,
            workspace_id=workspace_id,
            custom_store=getattr(orchestrator, "custom_subagent_store", None),
            user_id=user_id,
            team_id=team_id,
            team_subagent_store=getattr(orchestrator, "user_store", None) if subagent_creator_enabled() else None,
            shared_grant_ids=shared_subagent_ids,
        )
        return tuple(subagent_capability_ref(peer) for peer in peers)
    try:
        lead = registry.get("lead", workspace_id)
    except KeyError:
        return ()
    refs: list[CapabilityRef] = []
    for name in lead.callable_peers:
        try:
            refs.append(subagent_capability_ref(registry.get(name, workspace_id)))
        except KeyError:
            logger.warning("home_proposal.missing_peer name=%s", name)
    return tuple(refs)


async def _shared_content_grants(
    request: Request,
    *,
    team_id: str,
    user_id: str,
) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    from bowhead_core.share_service import content_sharing_enabled

    access = getattr(request.app.state, "access_control_store", None)
    if not content_sharing_enabled() or access is None or not team_id or not user_id:
        return None, None
    group_ids = await access.principal_group_ids(team_id, user_id)
    subagents, skills = await asyncio.gather(
        access.granted_resource_ids(team_id, user_id=user_id, group_ids=group_ids, kind="team_subagent"),
        access.granted_resource_ids(team_id, user_id=user_id, group_ids=group_ids, kind="team_skill"),
    )
    return subagents, skills


def _lead_tool_whitelist(orchestrator: Any, workspace_id: str) -> tuple[str, ...]:
    registry = getattr(orchestrator, "multi_agent_registry", None)
    if registry is None:
        return ()
    try:
        return tuple(registry.get("lead", workspace_id).tools.allow)
    except KeyError:
        return ()


async def _context_refs(
    request: Request,
    user: User,
    scope: home._WorkspaceScope,
) -> tuple[ContextRef, ...]:
    contexts: list[ContextRef] = []
    try:
        sessions = await home._sessions_for_scope(request, user, scope)
        contexts.extend(
            ContextRef(
                id=f"session:{session.id}",
                kind="session",
                title=str(session.title or "Untitled session"),
                format=None,
                updated_at=str(session.last_active_at or ""),
            )
            for session in sessions[:5]
        )
    except Exception as exc:
        logger.warning("home_proposal.context_sessions_failed error=%s", type(exc).__name__)

    outputs: list[Any] = []
    report_canvas_ids: set[str] = set()
    try:
        reports = await home._collect_reports(request, user, scope)
        outputs.extend(reports)
        report_canvas_ids = {str(item.metadata["canvasId"]) for item in reports if item.metadata.get("canvasId")}
    except Exception as exc:
        logger.warning("home_proposal.context_reports_failed error=%s", type(exc).__name__)
    try:
        outputs.extend(
            await home._collect_deliverables(
                request,
                user,
                scope,
                report_canvas_ids,
            )
        )
    except Exception as exc:
        logger.warning("home_proposal.context_artifacts_failed error=%s", type(exc).__name__)
    for output in sorted(outputs, key=lambda item: item.updatedAt, reverse=True)[:4]:
        contexts.append(
            ContextRef(
                id=output.id,
                kind=output.kind,
                title=output.title,
                format=output.format,
                updated_at=output.updatedAt,
            )
        )
    return tuple(contexts)


def _proposals_from_cache(
    entry: HomeProposalCacheEntry,
    *,
    envelope: Any,
    now: datetime,
) -> tuple[HomeProposal, ...] | None:
    try:
        expires_at = datetime.fromisoformat(entry.expires_at)
        generated_at = datetime.fromisoformat(entry.generated_at)
    except ValueError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    if now >= expires_at or entry.capability_fingerprint != envelope.capability_fingerprint:
        return None
    if entry.context_fingerprint != envelope.context_fingerprint and now - generated_at > _CONTEXT_STALE_TTL:
        return None
    # Deterministic rows are rendered from the locale copy table; a table edit (e.g. a de-DE/fr-FR translation
    # replacing its English placeholder) must not keep serving the old text. Rows without the field predate it.
    if entry.strategy == "deterministic" and entry.payload.get("copyFingerprint") != proposal_copy_fingerprint(envelope.locale):
        return None
    raw_items = entry.payload.get("items")
    if not isinstance(raw_items, list):
        return None
    candidates: list[HomeProposal] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return None
        try:
            dto = _CachedHomeProposalItemDTO.model_validate(raw)
            candidates.append(
                HomeProposal(
                    id=dto.id,
                    kind=dto.kind,
                    title=dto.title,
                    reason=dto.reason,
                    prompt=dto.prompt,
                    capability_refs=tuple(dto.capabilityRefs),
                    context_refs=tuple(dto.contextRefs),
                    output_format=dto.outputFormat,
                )
            )
        except Exception:
            return None
    proposals = validate_proposals(candidates, envelope)
    if len(proposals) != len(candidates):
        return None
    if entry.strategy == "llm" and len(proposals) < 2:
        return None
    return proposals


def _response(
    *,
    generation_id: str,
    generated_at: str,
    strategy: Literal["llm", "deterministic"],
    cached: bool,
    proposals: tuple[HomeProposal, ...],
) -> HomeProposalResponseDTO:
    return HomeProposalResponseDTO(
        generatedAt=generated_at,
        strategy=strategy,
        cached=cached,
        generationId=generation_id,
        items=[HomeProposalItemDTO.model_validate(item.to_payload()) for item in proposals],
    )


def _cached_response(
    entry: HomeProposalCacheEntry | None,
    *,
    envelope: Any,
    now: datetime,
    llm_enabled: bool,
) -> HomeProposalResponseDTO | None:
    if entry is None:
        return None
    if not llm_enabled and entry.strategy == "llm":
        return None
    if llm_enabled and entry.strategy != "llm" and entry.payload.get("llmAttempted") is not True:
        return None
    proposals = _proposals_from_cache(entry, envelope=envelope, now=now)
    if proposals is None:
        return None
    return _response(
        generation_id=entry.generation_id,
        generated_at=entry.generated_at,
        strategy=entry.strategy,
        cached=True,
        proposals=proposals,
    )


async def _proposal_router(
    request: Request,
    *,
    team_id: str | None,
) -> Any | None:
    # Batch 2-a site 7: both fallbacks previously used **process-assembled router** (placeholder) without
    # a home_proposal binding, so router.py resolve raised raw KeyError before adapter execution.
    # Refusal never counted this site (first evidence for §1.3's broken detector).
    # Use None: callers (:730+) already handle it. Fallback reason changes provider_error ->
    # router_unavailable (**attribution correction, not behavior equivalence**; label-based dashboards lose continuity).
    if not team_id:
        return None

    from bowhead_gateway.turn_service import resolve_team_model_runtime

    resolved = await resolve_team_model_runtime(
        request.app.state,
        team_id,
        entrypoint="tool_continuation",
    )
    if not resolved.available:
        return None
    try:
        resolved.router.resolve("home_proposal")
    except KeyError:
        logger.info("home_proposal.team_binding_missing team=%s", team_id)
        return None
    return resolved.router


def _proposal_lock(user_id: str, workspace_id: str, locale: str) -> asyncio.Lock:
    key = (user_id, workspace_id, locale)
    lock = _PROPOSAL_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PROPOSAL_LOCKS[key] = lock
    return lock


def _record_metric(
    *,
    strategy: str,
    outcome: str,
    cached: bool,
    started_at: float,
) -> None:
    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_home_proposals_total",
            labels={
                "strategy": strategy,
                "outcome": outcome,
                "cached": "true" if cached else "false",
            },
            help="Homepage proposal request outcomes.",
        )
        metrics.observe(
            "bowhead_home_proposals_latency_ms",
            value=(monotonic() - started_at) * 1000,
            labels={"strategy": strategy},
            buckets=(25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 3000.0, 8000.0),
            help="Homepage proposal latency in milliseconds.",
        )
    except Exception:  # noqa: S110
        pass


def _record_proposal_rejection(reason: ProposalRejectionReason) -> None:
    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_home_proposal_rejected_total",
            labels={"reason": reason},
            help="Homepage LLM proposal candidates rejected by validation.",
        )
    except Exception:  # noqa: S110
        pass


def _record_proposal_candidate(outcome: ProposalCandidateOutcome) -> None:
    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_home_proposal_candidates_total",
            labels={"outcome": outcome},
            help="Homepage LLM proposal candidates by validation outcome.",
        )
    except Exception:  # noqa: S110
        pass


def _record_proposal_fallback(reason: HomeProposalFallbackReason) -> None:
    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_home_proposal_fallback_total",
            labels={"reason": reason},
            help="Homepage LLM proposal fallback reasons.",
        )
    except Exception:  # noqa: S110
        pass


def _record_proposal_llm_attempt(outcome: Literal["success", "fallback"]) -> None:
    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_home_proposal_llm_attempts_total",
            labels={"outcome": outcome},
            help="Homepage LLM proposal generation attempts.",
        )
    except Exception:  # noqa: S110
        pass


@router.get("/proposals", response_model=HomeProposalResponseDTO)
async def get_home_proposals(
    workspace_id: str,
    request: Request,
    locale: str = "en-US",
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(home._user_store),
    cache_store: HomeProposalCacheStore = Depends(_cache_store),
) -> HomeProposalResponseDTO:
    started_at = monotonic()
    if os.environ.get("BOWHEAD_HOME_PROPOSALS", "1") != "1":
        _record_metric(
            strategy="deterministic",
            outcome="disabled",
            cached=False,
            started_at=started_at,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "home proposals disabled")

    try:
        scope = await home._resolve_scope(user, user_store, workspace_id)
        access_context = await build_access_context_from_request(
            request,
            user=user,
            workspace_id=scope.workspace_id,
        )
    except HTTPException:
        _record_metric(
            strategy="deterministic",
            outcome="scope_rejected",
            cached=False,
            started_at=started_at,
        )
        raise

    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "orchestrator not configured")
    execution_context = ToolExecutionContext(
        workspace_id=scope.workspace_id,
        member_uid="",
        user_id=user.id,
        turn_id=f"home_{uuid4().hex}",
        datarc=cast("Any", None),
        role="lead",
        team_id=access_context.team_id,
        access_context=access_context_to_dict(access_context),
        access_control_store=getattr(request.app.state, "access_control_store", None),
    )
    from bowhead_core.capability_resolver import resolve_unconfigured_capability_tools

    execution_context.unconfigured_capability_tools = await resolve_unconfigured_capability_tools(
        orchestrator,
        access_context.team_id,
    )
    read_only = access_context.workspace_role == "viewer"
    resolver = CapabilityResolver.from_runtime(orchestrator)
    try:
        shared_subagent_ids, shared_skill_ids = await _shared_content_grants(
            request,
            team_id=access_context.team_id,
            user_id=user.id,
        )
    except Exception as exc:
        logger.warning(
            "home_proposal.capability_shares_failed error=%s",
            type(exc).__name__,
        )
        shared_subagent_ids, shared_skill_ids = None, None
    tool_refs: tuple[CapabilityRef, ...] = ()
    skill_refs: tuple[CapabilityRef, ...] = ()
    async with contextlib.AsyncExitStack() as stack:
        try:
            resolved = await resolver.resolve(
                stack,
                CapabilityPrincipal(
                    user_id=user.id,
                    team_id=access_context.team_id,
                    workspace_id=scope.workspace_id,
                    role="lead",
                    read_only=read_only,
                ),
                execution_context,
                profile_whitelist=_lead_tool_whitelist(
                    orchestrator,
                    scope.workspace_id,
                ),
            )
            tool_refs = resolved.refs
        except Exception as exc:
            logger.warning(
                "home_proposal.capability_tools_failed error=%s",
                type(exc).__name__,
            )
        try:
            skill_refs = await _visible_skill_refs(
                request,
                execution_context,
                user_id=user.id,
                team_id=access_context.team_id,
                user_store=user_store,
                shared_skill_ids=shared_skill_ids,
            )
        except Exception as exc:
            logger.warning(
                "home_proposal.capability_skills_failed error=%s",
                type(exc).__name__,
            )

    try:
        subagent_refs = await _visible_subagent_refs(
            orchestrator,
            scope.workspace_id,
            user_id=user.id,
            team_id=access_context.team_id,
            shared_subagent_ids=shared_subagent_ids,
        )
    except Exception as exc:
        logger.warning(
            "home_proposal.capability_subagents_failed error=%s",
            type(exc).__name__,
        )
        subagent_refs = ()
    runtime_refs = tuple(sorted((*tool_refs, *skill_refs, *subagent_refs), key=lambda item: item.id))
    all_refs = tuple(
        sorted(
            (
                *runtime_refs,
                *platform_capability_refs(
                    runtime_refs,
                    read_only=read_only,
                ),
            ),
            key=lambda item: item.id,
        )
    )
    envelope = build_envelope(
        workspace_id=scope.workspace_id,
        workspace_name=scope.workspace_name,
        capabilities=all_refs,
        contexts=await _context_refs(request, user, scope),
        locale=locale,
    )
    now = datetime.now(tz=UTC)
    llm_enabled = os.environ.get("BOWHEAD_HOME_PROPOSAL_LLM", "0") == "1"
    entry = await cache_store.get(
        user_id=user.id,
        workspace_id=scope.workspace_id,
        locale=envelope.locale,
    )
    cached_response = _cached_response(
        entry,
        envelope=envelope,
        now=now,
        llm_enabled=llm_enabled,
    )
    if cached_response is not None:
        _record_metric(
            strategy=cached_response.strategy,
            outcome="success",
            cached=True,
            started_at=started_at,
        )
        return cached_response

    async with _proposal_lock(user.id, scope.workspace_id, envelope.locale):
        entry = await cache_store.get(
            user_id=user.id,
            workspace_id=scope.workspace_id,
            locale=envelope.locale,
        )
        cached_response = _cached_response(
            entry,
            envelope=envelope,
            now=now,
            llm_enabled=llm_enabled,
        )
        if cached_response is not None:
            _record_metric(
                strategy=cached_response.strategy,
                outcome="success",
                cached=True,
                started_at=started_at,
            )
            return cached_response

        generated_at = now.isoformat(timespec="seconds")
        generation_id = f"hp_{uuid4().hex}"
        strategy: Literal["llm", "deterministic"] = "deterministic"
        llm_attempted = False
        proposals: tuple[HomeProposal, ...] = ()
        if llm_enabled:
            llm_attempted = True
            fallback_recorded = False

            def record_fallback(reason: HomeProposalFallbackReason) -> None:
                nonlocal fallback_recorded
                fallback_recorded = True
                _record_proposal_fallback(reason)

            try:
                proposal_router = await _proposal_router(
                    request,
                    team_id=access_context.team_id,
                )
                if proposal_router is not None:
                    proposals = await generate_llm_proposals(
                        proposal_router,
                        envelope,
                        generation_id=generation_id,
                        on_candidate=_record_proposal_candidate,
                        on_reject=_record_proposal_rejection,
                        on_fallback=record_fallback,
                    )
                else:
                    record_fallback("router_unavailable")
            except TimeoutError as exc:
                record_fallback("timeout")
                logger.warning(
                    "home_proposal.llm_failed error=%s",
                    type(exc).__name__,
                )
            except Exception as exc:
                record_fallback("provider_error")
                logger.warning(
                    "home_proposal.llm_failed error=%s",
                    type(exc).__name__,
                )
            if proposals:
                strategy = "llm"
            elif not fallback_recorded:
                record_fallback("empty_result")
            _record_proposal_llm_attempt("success" if proposals else "fallback")
        if not proposals:
            proposals = generate_deterministic_proposals(envelope)
        expires_at = now + (_LLM_FAILURE_RETRY_TTL if llm_attempted and strategy == "deterministic" else _CACHE_TTL)
        new_entry = HomeProposalCacheEntry(
            user_id=user.id,
            workspace_id=scope.workspace_id,
            locale=envelope.locale,
            generation_id=generation_id,
            generated_at=generated_at,
            expires_at=expires_at.isoformat(timespec="seconds"),
            capability_fingerprint=envelope.capability_fingerprint,
            context_fingerprint=envelope.context_fingerprint,
            strategy=strategy,
            payload={
                "items": [item.to_cache_payload() for item in proposals],
                "llmAttempted": llm_attempted,
                "copyFingerprint": proposal_copy_fingerprint(envelope.locale),
            },
        )
        await cache_store.upsert(new_entry)
        _record_metric(
            strategy=strategy,
            outcome="success" if strategy == "llm" or not llm_attempted else "fallback",
            cached=False,
            started_at=started_at,
        )
        return _response(
            generation_id=generation_id,
            generated_at=generated_at,
            strategy=strategy,
            cached=False,
            proposals=proposals,
        )
