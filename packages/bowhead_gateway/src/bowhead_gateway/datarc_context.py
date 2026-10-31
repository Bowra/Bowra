"""Per-turn Datarc context resolution for gateway entrypoints.

Chat, clarification resume, approval execution, and scheduled jobs all need to
construct the same runtime Datarc envelope before they enter bowhead_core. The
core runner already knows how to honor per-turn clients and fail-closed flags;
this module keeps the gateway entrypoints from drifting.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from bowhead_core.datarc_selection import DatarcConnectionBinding, DatarcPreferenceScope
from bowhead_core.scrub import scrub

from bowhead_gateway.connector_context_provider import (
    ConnectorContextProvider,
    DatarcContextProvider,
)

logger = logging.getLogger(__name__)


class DatarcResolutionError(Exception):
    """The team has a Datarc connection, but it could not be resolved safely.

    This is distinct from "no connection configured". Both cases prohibit a
    process-global fallback; an unusable configured connection must additionally
    surface its failure to read-only preflight callers.
    """


@dataclass(frozen=True)
class DatarcTurnContext:
    active_datarc_project: str | None = None
    datarc_client: Any | None = None
    datarc_unavailable: bool = False
    vector_client: Any | None = None
    datarc_project_for_calls: str = ""
    # Power BI adapter (Scenario ①): per-turn PbiTurnHandle (or None). Resolved in
    # resolve_datarc_turn_context so all 5 turn entrypoints that use this envelope get it.
    # Batch 2 (workspace-space-split PRD §3.3b/§3.11): per-user mode carries in the envelope
    # the selected workspace's display name (provenance) + this user's accessible snapshot: (id,name) dict tuple
    # (dispatch-gate sentinel options; core cannot access the store for names, so inject them here).
    active_datarc_workspace_name: str = ""
    accessible_datarc_workspaces: tuple[dict, ...] = ()
    # bi-absent-turn R3.1: BI absence reason ("" | "no_connection" | "no_identity" |
    # "zero_workspaces" | "transient") for wording selection (PRD R3.3). "" = unknown/not applicable.
    datarc_absence_reason: str = ""
    connection_binding: DatarcConnectionBinding | None = None


def _state(request_or_app: Any) -> Any | None:
    app = getattr(request_or_app, "app", request_or_app)
    return getattr(app, "state", None)


async def resolve_team_datarc_binding(request_or_app: Any, team_id: str | None) -> DatarcConnectionBinding | None:
    """Resolve the caller team's per-connection DatarcClient.

    Returns ``None`` for benign misses (no team, store/factory not wired, no active
    connection). Raises ``DatarcResolutionError`` when a configured connection is
    present but unusable, so callers can set ``datarc_unavailable=True`` and avoid
    process-global fallback.
    """
    if not team_id:
        return None
    state = _state(request_or_app)
    store = getattr(state, "datarc_connection_store", None) if state is not None else None
    factory = getattr(state, "datarc_client_factory", None) if state is not None else None
    if store is None or factory is None:
        return None
    try:
        resolved = await store.resolve_active_binding(team_id)
        if resolved is None:
            return None
        conn, auth_key, revision = resolved
        if not auth_key:
            raise DatarcResolutionError(f"datarc connection {conn.id} has no usable auth")

        async def resolve_vector(project_ids: Sequence[str]) -> Any:
            embed_factory = getattr(state, "embed_client_factory", None)
            if embed_factory is None:
                return None
            try:
                return await embed_factory.get_verified_client(conn.base_url, auth_key, project_ids=project_ids)
            except Exception:
                logger.warning("datarc_connection.vector_unavailable team_id=%s", team_id)
                return None

        return DatarcConnectionBinding(team_id, conn.id, revision, factory.get_client(conn.base_url, auth_key), resolve_vector)
    except DatarcResolutionError:
        raise
    except Exception as exc:
        logger.warning(
            "datarc_connection.resolve_failed team_id=%s err=%s",
            team_id,
            scrub(str(exc)),
        )
        raise DatarcResolutionError("datarc connection resolution failed") from exc


async def resolve_team_datarc_client(request_or_app: Any, team_id: str | None) -> Any:
    """Return the authorized Team registry client without a process-global fallback."""
    binding = await resolve_team_datarc_binding(request_or_app, team_id)
    return binding.client if binding else None


def clarification_runtime_context(card: Any, *, db_path=None) -> dict:
    """Read the original native identity context, including a sealed ordinary request."""
    context = card.resume_context if isinstance(card.resume_context, dict) else {}
    if context.get("__schema_version") == 4:
        from bowhead_core.ordinary_lead_continuation import ordinary_resume_request_material

        return ordinary_resume_request_material(card, db_path=db_path)[1]
    return context


def recovered_datarc_source(context: DatarcTurnContext, user_id: str, team_id: str, member_uid: str) -> dict:
    from bowhead_core.datarc_selection import DatarcSelection

    return (
        DatarcSelection(
            context.connection_binding,
            user_id,
            context.active_datarc_project,
            context.active_datarc_workspace_name,
            member_uid,
            context.datarc_client,
        )
        .snapshot(team_id=team_id)
        .model_dump(mode="json")
    )


async def resolve_datarc_recovery_context(
    request_or_app: Any,
    *,
    team_id: str,
    user_id: str,
    workspace_id: str,
    session_id: str,
    evidence: dict,
    selected_project: str | None = None,
) -> tuple[DatarcTurnContext, str]:
    """Reauthorize an accepted source, without consulting a newer personal choice."""
    from dataclasses import replace

    from bowhead_core.datarc_selection import DatarcSelectionSnapshot, validate_datarc_source_evidence

    source = DatarcSelectionSnapshot.model_validate(evidence)
    if source.state == "unavailable":
        context = DatarcTurnContext(datarc_unavailable=True)
    else:
        context = await resolve_datarc_turn_context(
            request_or_app,
            team_id=team_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            pinned_project=selected_project or source.datarc_workspace_id,
            read_only=True,
        )
        binding = context.connection_binding
        if binding is None or (binding.team_id, binding.connection_id, binding.revision) != (
            source.team_id,
            source.connection_id,
            source.connection_revision,
        ):
            raise ValueError("datarc_resume_scope_changed")
        if source.state == "unselected" and selected_project is None:
            context = replace(context, active_datarc_project=None, datarc_project_for_calls="", active_datarc_workspace_name="")
        elif source.name and context.active_datarc_project == source.datarc_workspace_id:
            context = replace(context, active_datarc_workspace_name=source.name)
    member = ""
    if context.datarc_project_for_calls:
        state = _state(request_or_app)
        user = await state.user_store.get_user(user_id)
        if user is None or not user.email or not user.email_verified:
            raise ValueError("datarc_identity_required")
        refs, reason = await state.tool_context_service.accessible_workspaces_ex(context.datarc_client, user_id, refresh=True)
        selected = next((item for item in refs if item.id == context.datarc_project_for_calls), None)
        if reason or selected is None:
            raise ValueError("datarc_resume_scope_changed")
        context = replace(context, accessible_datarc_workspaces=tuple({"id": item.id, "name": item.name} for item in refs))
        if selected_project is not None or not context.active_datarc_workspace_name:
            context = replace(context, active_datarc_workspace_name=selected.name)
        member = await state.member_resolver.resolve(user.email, context.datarc_project_for_calls, datarc=context.datarc_client, refresh=True)
    validate_datarc_source_evidence(
        evidence,
        binding=context.connection_binding,
        team_id=team_id,
        user_id=user_id,
        datarc_workspace_id=context.active_datarc_project,
        member_uid=member,
        allow_workspace_change=selected_project is not None,
    )
    if context.connection_binding is not None and context.connection_binding.vector_resolver is not None:
        context = replace(
            context,
            vector_client=await context.connection_binding.vector_resolver(
                [context.datarc_project_for_calls] if context.datarc_project_for_calls else []
            ),
        )
    return context, member


async def resolve_team_embed_client(
    request_or_app: Any,
    team_id: str | None,
    *,
    project_ids: Sequence[str] = (),
) -> Any:
    """Resolve an team-bound Datarc embed-proxy VectorClient for this turn.

    Embed lookup is best-effort: all failures return ``None``, and the tool layer
    reports「embed 不可用」rather than pretending the dictionary had no match.

    ``project_ids`` = 本连接可见的 datarc 工作区 id，用于判 collection 的 tenant 段
    （见 ``bowhead_memory.resolve_tenant_from_collections``）。这里传是因为**只有这一层
    同时握着 BI 侧的可及集合和 embed client**；判不出来不影响 client 返回。
    """
    if not team_id:
        return None
    state = _state(request_or_app)
    store = getattr(state, "datarc_connection_store", None) if state is not None else None
    factory = getattr(state, "embed_client_factory", None) if state is not None else None
    if store is None or factory is None:
        return None
    try:
        conns = await store.list_connections(team_id)
        if not conns:
            return None
        conn = conns[0]
        auth_key = await store.get_connection_auth(conn.id, team_id)
        if not auth_key:
            return None
        return await factory.get_verified_client(conn.base_url, auth_key, project_ids=project_ids)
    except Exception as exc:
        logger.warning(
            "embed_proxy.resolve_failed team_id=%s err=%s",
            team_id,
            scrub(str(exc)),
        )
        return None


async def _resolve_per_user_datarc_workspace(
    state: Any,
    datarc_client: Any,
    user_id: str | None,
    *,
    session_id: str | None = None,
    read_only: bool = False,
    log: logging.Logger | None = None,
    connection_binding: DatarcConnectionBinding | None = None,
) -> tuple[str | None, str, tuple[dict, ...], str]:
    """批2 (PRD §2.1 阶梯)：per-user 模式的「当前北极九章工作区」解析——薄壳.

    返回 (workspace_id|None, 显示名, 可及集合快照, 缺席原因)。None = 无有效选择 →
    datarc_project_for_calls="" → bi_* dispatch 闸出 sentinel 引导。
    阶梯语义的唯一权威 = tool_context_provider.DefaultContextLadder（框架文档
    docs/tool-context-framework.md）；datarc 是契约实例 #1，新 tool 禁止另起炉灶。
    """
    svc = getattr(state, "tool_context_service", None) if state is not None else None
    umem = getattr(state, "user_memory_store", None) if state is not None else None
    if svc is None or umem is None or not user_id or datarc_client is None:
        # bi-absent-turn R3.1: missing user_id/dependency means unknown identity, not lack of permission;
        # keep reason "" (e.g. headless channels) and fall back to neutral wording.
        return None, "", (), ""

    # Annotate with the contract type so the type checker verifies DatarcContextProvider against the Protocol
    # (review: otherwise contract drift would not fail CI).
    provider: ConnectorContextProvider = DatarcContextProvider(
        service=svc,
        umem=umem,
        datarc_client=datarc_client,
        log=log or logger,
        preference_scope=DatarcPreferenceScope(connection_binding.team_id, connection_binding.connection_id) if connection_binding else None,
    )
    resolved = await provider.resolve(
        user_id,
        session_id=session_id,
        read_only=read_only,
    )
    return resolved.context_id, resolved.context_name, resolved.accessible, resolved.absence_reason


async def resolve_datarc_turn_context(
    request_or_app: Any,
    *,
    team_id: str | None,
    workspace_id: str,
    user_id: str | None = None,
    session_id: str | None = None,
    pinned_project: str | None = None,
    read_only: bool = False,
    log: logging.Logger | None = None,
) -> DatarcTurnContext:
    """Build the Datarc runtime envelope shared by all turn entrypoints.

    ``pinned_project``（scheduled/approvals，批2 §3.9 决议10）：产物工作区 = 创建时
    快照，**不走 per-user 阶梯**——省一次 accessible 调用 + 避免 headless 通道触发
    auto-select / 撤权清偏好等用户偏好副作用（架构 review 2026-07-13，framework 文档 §6）。
    """
    # User-context validation needs the accessible set from the team client.
    connection_binding = None
    datarc_client = None
    datarc_unavailable = False
    try:
        connection_binding = await resolve_team_datarc_binding(request_or_app, team_id)
        datarc_client = connection_binding.client if connection_binding else None
    except DatarcResolutionError:
        if read_only:
            raise
        datarc_unavailable = True

    active_workspace_name = ""
    accessible_workspaces: tuple[dict, ...] = ()
    absence_reason = ""
    if pinned_project:
        # Pin short-circuit: headless channels have no picker/switcher or accessible-snapshot consumer.
        active_project: str | None = pinned_project
    else:
        active_project, active_workspace_name, accessible_workspaces, absence_reason = await _resolve_per_user_datarc_workspace(
            _state(request_or_app),
            datarc_client,
            user_id,
            session_id=session_id,
            read_only=read_only,
            log=log,
            connection_binding=connection_binding,
        )

    if not read_only:
        from bowhead_gateway._turn_runtime import _team_has_no_connection

        if datarc_client is None and not datarc_unavailable and (not team_id or await _team_has_no_connection(request_or_app, team_id)):
            datarc_unavailable = True
            # bi-absent-turn R3.1: confirmed team connection absence -> no_connection, overriding the per-user
            # path's "" when client is None. Resolution failure (datarc_unavailable already True)
            # leaves reason unchanged: a connection failure differs from absence semantics.
            if not absence_reason:
                absence_reason = "no_connection"

    vector_client = None
    if datarc_client is not None and not read_only:
        # Determine the tenant from the workspaces visible to this key; reuse the accessible set computed above,
        # plus this turn's selected project (the pin path has only that, no accessible set).
        _tenant_probe_projects = [str(w.get("id")) for w in accessible_workspaces if isinstance(w, dict) and w.get("id")]
        if active_project and str(active_project) not in _tenant_probe_projects:
            _tenant_probe_projects.append(str(active_project))
        if connection_binding is not None and connection_binding.vector_resolver is not None:
            vector_client = await connection_binding.vector_resolver(_tenant_probe_projects)

    if workspace_id and not workspace_id.startswith("ws_") and not active_project:
        (log or logger).warning(
            "datarc_turn_context.non_canonical_workspace_dropped ws=%r",
            workspace_id,
        )

    return DatarcTurnContext(
        connection_binding=connection_binding,
        active_datarc_project=active_project,
        datarc_client=datarc_client,
        datarc_unavailable=datarc_unavailable,
        vector_client=vector_client,
        # Batch 1/c2+c3 (workspace-space-split): the Bowhead space id NEVER stands in for a
        # datarc workspace — same rule as ToolExecutionContext.resolve_datarc_project
        # (legacy numeric escape hatch retired; 2026-07-07 116 scan found zero non-ws_*
        # ids). No seeded/selected datarc workspace → "" (BI absent for the turn).
        datarc_project_for_calls=active_project or "",
        active_datarc_workspace_name=active_workspace_name,
        accessible_datarc_workspaces=accessible_workspaces,
        datarc_absence_reason=absence_reason,
    )
