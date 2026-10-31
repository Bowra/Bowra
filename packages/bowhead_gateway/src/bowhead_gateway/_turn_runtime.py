"""Shared turn-start runtime resolution helpers.

Keep these outside route modules so Web chat, IM adapters, and scheduled jobs
all use the same team-scoped Datarc/vector resolution path.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger("bowhead_gateway.turn_runtime")


class DatarcResolutionError(Exception):
    """Configured team Datarc connection exists but cannot be resolved safely.

    Callers must fail closed instead of falling back to the process-global
    Datarc client, which would break tenant isolation.
    """


async def _resolve_team_datarc_client(request: Any, team_id: str | None) -> Any:
    """Resolve the caller team's per-connection DatarcClient.

    Returns None for benign cases: no team, stores not wired, or no connection
    configured for the team. Raises ``DatarcResolutionError`` when a configured
    team connection is present but unusable.
    """
    from bowhead_gateway.datarc_context import DatarcResolutionError as NativeResolutionError
    from bowhead_gateway.datarc_context import resolve_team_datarc_client

    try:
        return await resolve_team_datarc_client(request, team_id)
    except NativeResolutionError as error:
        raise DatarcResolutionError("Datarc connection resolution failed") from error


async def _resolve_team_embed_client(
    request: Any,
    team_id: str | None,
    *,
    project_ids: Sequence[str] = (),
) -> Any:
    """Resolve the caller team's Datarc-proxy VectorClient, best-effort.

    ``project_ids`` feeds the collection-tenant probe (see
    ``bowhead_memory.resolve_tenant_from_collections``). IM/scheduled turns do not
    carry an accessible-workspace snapshot, so they pass what they have (often just
    the pinned project) and otherwise ride the process-wide factory cache the web
    path populates. Unresolved tenant degrades to「embed 不可用」, never a guess.
    """
    if not team_id:
        return None
    store = getattr(request.app.state, "datarc_connection_store", None)
    factory = getattr(request.app.state, "embed_client_factory", None)
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
    except Exception as e:
        from bowhead_core.scrub import scrub

        logger.warning(
            "embed_proxy.resolve_failed team_id=%s err=%s",
            team_id,
            scrub(str(e)),
        )
        return None


async def _team_has_no_connection(request_or_app: Any, team_id: str) -> bool:
    """ws-datarc-decouple P4: True when the turn must treat Datarc as absent for this team.

    That is the case when the team has zero active connections, and also when the connection
    registry is not wired or cannot be read: there is no deployment-level client to fall back
    to, so any doubt fails closed (Datarc unavailable). False only when the registry positively
    lists at least one connection.

    S2: accepts a request OR an app and resolves ``app.state`` either way, so the web resolver
    (datarc_context, ``request_or_app``) and the IM/scheduled path here share ONE cut helper."""
    app = getattr(request_or_app, "app", request_or_app)
    state = getattr(app, "state", None)
    store = getattr(state, "datarc_connection_store", None) if state is not None else None
    if store is None:
        return True
    try:
        return not await store.list_connections(team_id)  # active-only, team-bound
    except Exception:
        return True


async def _resolve_turn_datarc_runtime(
    request: Any,
    team_id: str | None,
    *,
    project_ids: Sequence[str] = (),
) -> tuple[Any | None, bool, Any | None]:
    """Resolve per-turn Datarc runtime fields.

    Returns ``(datarc_client, datarc_unavailable, vector_client)``. A broken
    configured team Datarc connection fails closed via ``datarc_unavailable=True``.
    Embed/vector resolution remains best-effort and only runs when the team
    Datarc client resolved.

    A Datarc connection is a team setting. Missing team context, a missing connection,
    or an unavailable registry all produce ``datarc_unavailable=True``: there is no
    process-global client to fall back to.
    """
    datarc_client = None
    datarc_unavailable = False
    try:
        datarc_client = await _resolve_team_datarc_client(request, team_id)
    except DatarcResolutionError:
        datarc_unavailable = True

    if datarc_client is None and not datarc_unavailable and (not team_id or await _team_has_no_connection(request, team_id)):
        datarc_unavailable = True

    vector_client = None
    if datarc_client is not None:
        vector_client = await _resolve_team_embed_client(request, team_id, project_ids=project_ids)
    return datarc_client, datarc_unavailable, vector_client
