"""Authorized workspace preparation and scoped preference saving for native callers."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from bowhead_core.datarc_selection import (
    DatarcPersistenceReceipt,
    DatarcPreferenceScope,
    DatarcSelection,
    DatarcSelectionSnapshot,
)
from bowhead_core.space_governance import SpaceOperation

from bowhead_gateway.access_context import build_access_context
from bowhead_gateway.connector_context_provider import DefaultContextLadder, commit_connector_context_choice
from bowhead_gateway.datarc_context import resolve_team_datarc_binding


class DatarcContextSwitcher:
    def __init__(self, app: Any, *, user_id: str, workspace_id: str, session_id: str, binding: Any, log: Any):
        self.app = app
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.binding = binding
        self.log = log

    async def prepare_selection(self, identifier: str, name: str, *, current: DatarcSelection) -> DatarcSelection:
        binding = current.binding
        if binding is None or binding != self.binding or current.user_id != self.user_id:
            raise ValueError("datarc_scope_missing")
        state = self.app.state
        await build_access_context(
            user_store=state.user_store,
            access_store=getattr(state, "access_control_store", None),
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            operation=SpaceOperation.EXECUTE,
            expected_team_id=binding.team_id,
        )
        user = await state.user_store.get_user(self.user_id)
        if user is None or not user.email or not user.email_verified:
            raise ValueError("datarc_identity_required")
        refs, reason = await state.tool_context_service.accessible_workspaces_ex(current.client, self.user_id, refresh=True)
        if reason or identifier not in {item.id for item in refs}:
            raise ValueError("datarc_workspace_unavailable")
        member = await state.member_resolver.resolve(user.email, identifier, datarc=current.client, refresh=True)
        if not member:
            raise ValueError("datarc_identity_required")
        vector = await binding.vector_resolver([identifier]) if binding.vector_resolver is not None else None
        latest = await resolve_team_datarc_binding(self.app, binding.team_id)
        if latest is None or (latest.connection_id, latest.revision) != (binding.connection_id, binding.revision):
            raise ValueError("datarc_connection_changed")
        return replace(current, datarc_workspace_id=identifier, name=name, member_uid=member, vector_client=vector)

    async def save_preference(self, selection: DatarcSelection, *, remember: bool) -> DatarcPersistenceReceipt:
        return await commit_connector_context_choice(
            getattr(self.app.state, "user_memory_store", None),
            user_id=self.user_id,
            tool="datarc",
            context_id=selection.datarc_workspace_id or "",
            context_name=selection.name,
            remember=remember,
            session_id=self.session_id,
            preference_scope=DatarcPreferenceScope(self.binding.team_id, self.binding.connection_id),
            log=self.log,
        )

    async def resolve_saved_selection(self) -> tuple[str, str] | None:
        if self.binding is None:
            return None
        refs = await self.app.state.tool_context_service.accessible_workspaces(self.binding.client, self.user_id)
        resolved = await DefaultContextLadder(
            tool="datarc",
            umem=getattr(self.app.state, "user_memory_store", None),
            preference_scope=DatarcPreferenceScope(self.binding.team_id, self.binding.connection_id),
        ).resolve(self.user_id, session_id=self.session_id, accessible=refs, read_only=True)
        return (resolved.context_id, resolved.context_name) if resolved.context_id else None


async def commit_picker_selection(app: Any, card: Any, *, user_id: str, identifier: str, name: str, remember: bool) -> DatarcPersistenceReceipt:
    """Prepare the original candidate through its authoritative Space and connection."""
    context = card.resume_context if isinstance(card.resume_context, dict) else {}
    if card.tool and card.tool != "datarc":
        return await commit_connector_context_choice(
            getattr(app.state, "user_memory_store", None),
            user_id=user_id,
            tool=card.tool,
            context_id=identifier,
            context_name=name,
            remember=remember,
            session_id=str(context.get("session_id") or card.session_id or ""),
        )
    service, selection = await prepare_picker_selection(app, card, user_id=user_id, identifier=identifier, name=name)
    return await service.save_preference(selection, remember=remember)


async def prepare_picker_selection(app: Any, card: Any, *, user_id: str, identifier: str, name: str) -> tuple[DatarcContextSwitcher, DatarcSelection]:
    """Validate a pending native choice without publishing or saving it."""
    context = card.resume_context if isinstance(card.resume_context, dict) else {}
    access = await build_access_context(
        user_store=app.state.user_store,
        access_store=getattr(app.state, "access_control_store", None),
        user_id=user_id,
        workspace_id=card.workspace_id,
        operation=SpaceOperation.EXECUTE,
    )
    binding = await resolve_team_datarc_binding(app, access.team_id)
    if binding is None:
        raise ValueError("datarc_connection_changed")
    if context.get("datarc_selection") is not None:
        original = DatarcSelectionSnapshot.model_validate(context["datarc_selection"])
        if (original.team_id, original.user_id, original.connection_id, original.connection_revision) != (
            access.team_id,
            user_id,
            binding.connection_id,
            binding.revision,
        ):
            raise ValueError("datarc_connection_changed")
    service = DatarcContextSwitcher(
        app,
        user_id=user_id,
        workspace_id=card.workspace_id,
        session_id=str(context.get("session_id") or card.session_id or ""),
        binding=binding,
        log=logging.getLogger(__name__),
    )
    selection = await service.prepare_selection(identifier, name, current=DatarcSelection(binding, user_id, None, "", "", binding.client))
    return service, selection
