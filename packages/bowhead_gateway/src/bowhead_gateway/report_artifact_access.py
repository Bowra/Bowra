"""Report references preserve author and recipient file authority."""

from __future__ import annotations

import logging
from typing import Any

from bowhead_core.access_control import AccessContext
from bowhead_core.space_governance import SpaceOperation
from fastapi import HTTPException, Request

from bowhead_gateway.access_context import AccessContextError, build_access_context, has_workspace_access_source
from bowhead_gateway.files_sharing import shared_artifact_ids

logger = logging.getLogger(__name__)


async def authorized_report_artifact_ids(request: Request, canvas: Any, artifact_ids: list[str]) -> frozenset[str]:
    """Check author authority and each recipient's access to borrowed originals.

    Legacy author-owned files without a recorded Space retain their existing
    ownership authority. Unattributed files owned by anyone else never qualify.
    Recorded Spaces always enforce the report's Team, including for the author.
    """
    store = getattr(request.app.state, "artifact_store", None)
    if store is None or not artifact_ids or not getattr(canvas, "user_id", None):
        return frozenset()
    # Deletion affects availability, not the identity of a Results snapshot.
    # Every byte/preview exit still uses the store's non-deleted lookup.
    try:
        artifacts = await store.get_many(artifact_ids, include_deleted=True)
    except Exception as error:
        # Optional source projection remains readable, but a failed lookup
        # never turns an unverified report reference into access authority.
        logger.warning("report_artifact_metadata_unavailable error_type=%s", type(error).__name__)
        return frozenset()
    users = getattr(request.app.state, "user_store", None)
    team_id = await users.get_workspace_team_id(canvas.workspace_id) if users and canvas.workspace_id else None
    teams: dict[str, str | None] = {}
    readable_spaces: dict[str, bool] = {}
    grants: dict[str, frozenset[str]] = {}
    allowed: set[str] = set()

    async def can_read_space(workspace_id: str) -> bool:
        if workspace_id not in readable_spaces:
            try:
                await build_access_context(
                    user_store=users,
                    access_store=getattr(request.app.state, "access_control_store", None),
                    user_id=canvas.user_id,
                    workspace_id=workspace_id,
                    expected_team_id=team_id,
                    operation=SpaceOperation.READ_HISTORY,
                )
            except AccessContextError:
                readable_spaces[workspace_id] = False
            else:
                readable_spaces[workspace_id] = True
        return readable_spaces[workspace_id]

    for artifact in artifacts:
        workspace_id = getattr(artifact, "workspace_id", None)
        if workspace_id == "__team__":
            # Team-scoped registry assets have a separate publication contract.
            # Their owner alone cannot establish which Team may delegate them.
            continue
        if workspace_id:
            if workspace_id not in teams:
                teams[workspace_id] = await users.get_workspace_team_id(workspace_id) if users else None
            if not team_id or teams[workspace_id] != team_id:
                continue
            if has_workspace_access_source(request) and not await can_read_space(workspace_id):
                continue
        # Existing ownerless files are readable through verified source-Space
        # membership; a report must not invent an owner or require a fake grant.
        if artifact.created_by == canvas.user_id or (not artifact.created_by and readable_spaces.get(workspace_id, False)):
            allowed.add(artifact.id)
            continue
        # A guessed ID, a report PATCH, or a persisted section cannot stand in
        # for the author's independent file grant. Recheck grants on every read.
        if not team_id or not workspace_id or workspace_id == "__team__":
            continue
        # A report-Space publication and a source-Space publication both count,
        # but only while the author can read that grant's own Space context.
        for grant_space in dict.fromkeys((workspace_id, canvas.workspace_id)):
            if not grant_space or (has_workspace_access_source(request) and not await can_read_space(grant_space)):
                continue
            if grant_space not in grants:
                access = getattr(request.app.state, "access_control_store", None)
                groups = await access.principal_group_ids(team_id, canvas.user_id, grant_space) if access else ()
                grants[grant_space] = await shared_artifact_ids(
                    access,
                    team_id=team_id,
                    user_id=canvas.user_id,
                    workspace_id=grant_space,
                    group_ids=tuple(groups),
                )
            if artifact.id in grants[grant_space]:
                allowed.add(artifact.id)
                break
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    viewer_id = resolve_user_id_sync(request)
    if viewer_id == canvas.user_id:
        return frozenset(allowed)

    delivered: set[str] = set()
    recipient_grants: dict[str, frozenset[str]] = {}
    recipient_contexts: dict[str, AccessContext | None] = {}

    async def recipient_context(workspace_id: str) -> AccessContext | None:
        if workspace_id not in recipient_contexts:
            try:
                recipient_contexts[workspace_id] = await build_access_context(
                    user_store=users,
                    access_store=getattr(request.app.state, "access_control_store", None),
                    user_id=viewer_id,
                    workspace_id=workspace_id,
                    expected_team_id=team_id,
                    operation=SpaceOperation.READ_HISTORY,
                )
            except AccessContextError:
                recipient_contexts[workspace_id] = None
        return recipient_contexts[workspace_id]

    for artifact in artifacts:
        if artifact.id not in allowed:
            continue
        if artifact.created_by == canvas.user_id:
            delivered.add(artifact.id)
            continue
        if not viewer_id or not team_id:
            continue
        try:
            # Borrowed originals require the recipient's own verified source
            # Space, even when the report is shared outside that Space.
            if await recipient_context(artifact.workspace_id) is None:
                continue
            if getattr(artifact, "producer_kind", None) == "template_document_v1":
                from bowhead_gateway.routes.documents import published_file_identity

                await published_file_identity(request, artifact, viewer_id)
        except HTTPException as error:
            if error.status_code not in {403, 404, 410}:
                raise
            continue
        if not artifact.created_by or artifact.created_by == viewer_id:
            delivered.add(artifact.id)
            continue
        for grant_space in dict.fromkeys((artifact.workspace_id, canvas.workspace_id)):
            if not grant_space:
                continue
            if grant_space not in recipient_grants:
                context = await recipient_context(grant_space)
                if context is None:
                    recipient_grants[grant_space] = frozenset()
                else:
                    recipient_grants[grant_space] = await shared_artifact_ids(
                        getattr(request.app.state, "access_control_store", None),
                        team_id=team_id,
                        user_id=viewer_id,
                        workspace_id=context.workspace_id,
                        group_ids=tuple(context.group_ids),
                    )
            if artifact.id in recipient_grants[grant_space]:
                delivered.add(artifact.id)
                break
    return frozenset(delivered)
