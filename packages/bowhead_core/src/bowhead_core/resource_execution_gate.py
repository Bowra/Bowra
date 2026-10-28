"""Last-moment revocation checks for mutable Skills and team Tool sources.

Per-turn capability views remain useful for prompt stability, but they are not
authorization leases.  These checks run immediately before materializing a
Skill or making a remote Tool call, and intentionally do no remote I/O.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bowhead_core.access_control import resource_grants_enforce_enabled

logger = logging.getLogger(__name__)


def _denied(error_kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_kind": error_kind, "error": message}


async def check_team_tool_execution(
    ctx: Any,
    *,
    team_id: str,
    source_id: str,
    operation_id: str,
    advertised_name: str,
) -> dict[str, Any] | None:
    """Return a stable denial envelope, or ``None`` when dispatch may proceed."""

    if not getattr(ctx, "team_id", "") or ctx.team_id != team_id:
        return _denied("resource_revoked", "team tool source is no longer available")
    store = getattr(ctx, "skill_instance_store", None)
    db_path = getattr(store, "db_path", None) or getattr(store, "_db_path", None)
    if not db_path:
        # Gateway contexts always carry the shared UserStore-backed skill store.
        # Non-gateway/eval contexts retain their previous behavior.
        return None
    try:
        from bowhead_core._db import open_shared_ctx

        async with (
            open_shared_ctx(str(db_path)) as db,
            db.execute(
                "SELECT kind,status,selected_op_ids_json,tools_schema_json,openapi_spec_json,"
                "auth_type,auth_ref "
                "FROM team_tool_source WHERE team_id=? AND id=?",
                (team_id, source_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
    except Exception:
        logger.warning("resource_execution_gate.team_tool_state_failed source=%s", source_id, exc_info=True)
        return _denied("resource_state_unavailable", "team tool state could not be verified")
    if row is None or str(row[1]) != "enabled":
        return _denied("resource_revoked", "team tool source is no longer available")
    if str(row[5]) != "none" and not row[6]:
        return _denied("resource_revoked", "team tool credentials are no longer available")
    try:
        selected = {str(item) for item in json.loads(str(row[2]) or "[]")}
    except (TypeError, ValueError, json.JSONDecodeError):
        return _denied("resource_state_unavailable", "team tool selection could not be verified")
    if selected and operation_id not in selected:
        return _denied("operation_unavailable", "team tool operation is no longer selected")
    if str(row[0]) == "mcp":
        try:
            available = {str(item.get("name")) for item in json.loads(str(row[3]) or "[]") if isinstance(item, dict) and item.get("name")}
        except (TypeError, ValueError, json.JSONDecodeError):
            return _denied("resource_state_unavailable", "team tool catalog could not be verified")
        if operation_id not in available:
            return _denied("operation_unavailable", "team tool operation is no longer advertised")

    access_store = getattr(ctx, "access_control_store", None)
    access_ctx = getattr(ctx, "access_context", None)
    if access_store is not None and resource_grants_enforce_enabled():
        if not access_ctx:
            return _denied("resource_revoked", "team tool permission could not be verified")
        from bowhead_core.grant_evaluation import grant_verdicts

        try:
            verdicts = await grant_verdicts(
                ctx,
                access_store,
                access_ctx,
                [("team_tool_source", source_id, "use"), ("team_tool_op", advertised_name, "use")],
            )
        except Exception:
            logger.warning("resource_execution_gate.team_tool_grant_failed source=%s", source_id, exc_info=True)
            return _denied("resource_state_unavailable", "team tool permission could not be verified")
        if not all(verdict.allowed for verdict in verdicts):
            return _denied("resource_revoked", "team tool permission was revoked")
    return None


async def check_skill_execution(
    ctx: Any,
    *,
    catalog: Any,
    skill_name: str,
    permission: str,
) -> dict[str, Any] | None:
    """Re-read current preference, team lifecycle and audience before Skill use."""

    if not skill_name:
        return None
    user_id = getattr(ctx, "user_id", None)
    user_store = getattr(ctx, "skill_instance_store", None)
    team_id = getattr(ctx, "team_id", None)
    team_store = getattr(ctx, "team_skill_store", None)
    try:
        personal = await user_store.get(user_id=user_id, name=skill_name) if user_store is not None and user_id else None
    except Exception:
        logger.warning("resource_execution_gate.skill_preference_failed skill=%s", skill_name, exc_info=True)
        return _denied("resource_state_unavailable", "skill state could not be verified")
    if personal is not None and not bool(getattr(personal, "enabled", False)):
        return _denied("resource_revoked", "skill was disabled")

    # Trusted catalog entries cannot be shadowed by user/team records.  Their
    # only mutable runtime state is the personal enable preference above.
    if catalog.get(skill_name) is not None:
        return await _check_skill_grant(ctx, skill_name, permission=permission)

    if personal is not None and getattr(personal, "source_type", None) in {"user_upload", "user_generated"}:
        if getattr(personal, "import_status", None) == "pending":
            return _denied("skill_import_pending", "Skill import requires resolution in Settings → Skills. Do not retry until it is resolved.")
        if not getattr(personal, "source_path", None):
            return _denied("resource_revoked", "skill is no longer available")
        return await _check_skill_grant(ctx, skill_name, permission=permission)

    try:
        team_skill = await team_store.get_by_name(team_id, skill_name, only_enabled=False) if team_store is not None and team_id else None
    except Exception:
        logger.warning("resource_execution_gate.team_skill_state_failed skill=%s", skill_name, exc_info=True)
        return _denied("resource_state_unavailable", "skill state could not be verified")
    if team_skill is None or getattr(team_skill, "status", None) != "enabled" or getattr(team_skill, "approval", "approved") != "approved":
        return _denied("resource_revoked", "skill is no longer available")
    try:
        preference = (
            await user_store.get(
                user_id=user_id,
                name=f"team:{getattr(team_skill, 'id', '')}",
            )
            if user_store is not None and user_id
            else None
        )
    except Exception:
        logger.warning("resource_execution_gate.team_skill_preference_failed skill=%s", skill_name, exc_info=True)
        return _denied("resource_state_unavailable", "skill state could not be verified")
    if preference is not None and not bool(getattr(preference, "enabled", False)):
        return _denied("resource_revoked", "skill was disabled")
    visible = getattr(team_skill, "visibility", "team_wide") == "team_wide" or getattr(team_skill, "created_by", None) == user_id
    if not visible:
        access_store = getattr(ctx, "access_control_store", None)
        access_ctx = getattr(ctx, "access_context", None)
        if access_store is None or not access_ctx:
            return _denied("skill_scope_denied", "skill is no longer shared with this user")
        try:
            decision = await access_store.explain_decision(
                access_ctx,
                "team_skill",
                str(getattr(team_skill, "id", "")),
                permission,
            )
        except Exception:
            return _denied("resource_state_unavailable", "skill permission could not be verified")
        if not decision.get("allowed"):
            return _denied("skill_scope_denied", "skill is no longer shared with this user")
    return await _check_skill_grant(
        ctx,
        skill_name,
        permission=permission,
        team_skill=team_skill,
    )


async def _check_skill_grant(
    ctx: Any,
    skill_name: str,
    *,
    permission: str,
    team_skill: Any | None = None,
) -> dict[str, Any] | None:
    try:
        from bowhead_core.skill_access import skill_visible_by_grants

        allowed = await skill_visible_by_grants(
            ctx,
            skill_name,
            permission=permission,
            audit=True,
            audit_payload={"gate": "dispatch"},
            team_skill_id=(str(getattr(team_skill, "id", "")) if team_skill is not None else None),
            team_skill_created_by=(getattr(team_skill, "created_by", None) if team_skill is not None else None),
        )
    except Exception:
        logger.warning("resource_execution_gate.skill_grant_failed skill=%s", skill_name, exc_info=True)
        return _denied("resource_state_unavailable", "skill permission could not be verified")
    if not allowed:
        return _denied("skill_scope_denied", "skill permission was revoked")
    return None


__all__ = ["check_skill_execution", "check_team_tool_execution"]
