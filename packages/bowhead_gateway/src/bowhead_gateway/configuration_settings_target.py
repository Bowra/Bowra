"""Authenticate the selected current-Space settings target before turn admission."""

from __future__ import annotations

from bowhead_core.configuration_settings_target import AdmittedWorkspaceSettingsTarget, WorkspaceSettingsSendTarget
from fastapi import HTTPException, Request

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.workspace_settings import _check_workspace_access


async def admit_workspace_settings_target(
    request: Request,
    target: WorkspaceSettingsSendTarget,
    *,
    user_id: str,
    team_id: str,
    workspace_id: str,
) -> AdmittedWorkspaceSettingsTarget:
    if (target.user_id, target.team_id, target.workspace_id) != (user_id, team_id, workspace_id):
        raise HTTPException(403, {"error": {"code": "configuration_target_mismatch", "admission": "not_started"}})
    if target.pending_writes or target.unconfirmed_fields:
        raise HTTPException(409, {"error": {"code": "configuration_target_local_changes", "admission": "not_started"}})
    user = await _current_user(request)
    if (user.id, user.team_id) != (user_id, team_id):
        raise HTTPException(403, {"error": {"code": "configuration_target_mismatch", "admission": "not_started"}})
    store = getattr(request.app.state, "user_store", None)
    if store is None:
        raise HTTPException(503, {"error": {"code": "configuration_target_access_unavailable", "admission": "not_started"}})

    async def recheck() -> None:
        await _check_workspace_access(user=user, user_store=store, workspace_id=workspace_id, require_admin=True)
        await require_not_readonly(request)

    try:
        await recheck()
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        error = detail.get("error", detail)
        error = dict(error) if isinstance(error, dict) else {}
        error.setdefault("code", "configuration_target_access_denied")
        error["admission"] = "not_started"
        raise HTTPException(exc.status_code, {"error": error}) from exc
    return AdmittedWorkspaceSettingsTarget(target=target, recheck=recheck)
