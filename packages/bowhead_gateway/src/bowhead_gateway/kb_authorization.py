"""Current KB access checks shared by long-running chat and matrix operations."""

from __future__ import annotations

from typing import Any

from bowhead_core.access_control import resource_grants_enforce_enabled
from bowhead_core.kb.permission import KbPermission, ScopeContext
from bowhead_core.kb.schemas import KbFileDTO

from bowhead_gateway.access_context import SpaceOperation, access_context_to_dict, build_access_context


def kb_file_authorizer(user_store: Any, access_store: Any):
    async def authorize(file: KbFileDTO, user_id: str, team_id: str, workspace_id: str, *, execute: bool = False, write: bool = False) -> bool:
        try:
            ctx = await build_access_context(
                user_store=user_store,
                access_store=access_store,
                user_id=user_id,
                workspace_id=workspace_id,
                allow_missing_workspace=True,
                operation=SpaceOperation.EXECUTE if execute or write else SpaceOperation.READ_HISTORY,
                expected_team_id=team_id,
            )
            scope = ScopeContext(
                user_id=user_id,
                team_id=team_id,
                workspace_id=workspace_id,
                role="team_admin" if ctx.is_team_admin else "ws_admin" if ctx.workspace_role == "ws_admin" else "user",
            )
            if file.soft_deleted_at or file.team_id != team_id or not KbPermission.can_read(file, scope):
                return False
            if write and not KbPermission.can_delete(file, scope, scope.role):
                return False
            if resource_grants_enforce_enabled():
                if access_store is None:
                    return False
                resources = [("kb_file", file.file_id)]
                if file.scope == "team":
                    resources.append(("kb_scope", f"team:{file.scope_id}"))
                elif file.scope in {"space", "workspace"}:
                    resources.append(("kb_scope", f"workspace:{file.scope_id}"))
                for kind, identity in resources:
                    result = await access_store.explain_decision(access_context_to_dict(ctx), kind, identity, "write" if write else "read")
                    if not result.get("allowed"):
                        return False
            return True
        except Exception:
            return False

    return authorize
