"""Current-Space instructions through the Configuration Agent, using the versioned Space instructions store.

Reads follow the Space instructions page: any member of the Space or a Team manager. Saves need a Space admin or a
Team manager, exactly like `PUT /api/v1/space-instructions/{workspace_id}`, and append a version with the same
optimistic `expected_version` check. A confirmed suggestion is never undone here; the version history restores it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from bowhead_core.config_copilot import AppliedChange, FormContext, LiteApplyError, MaterializeResult
from bowhead_core.space_instructions import MAX_CONTENT_CHARS, SpaceInstructionsError, SpaceInstructionsStore
from bowhead_core.user_store import User, UserStore
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bowhead_gateway.routes.space_instructions import _access


class SpaceInstructionsDraftDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(
        default=None,
        json_schema_extra={"maxLength": MAX_CONTENT_CHARS},
        description=(
            "Complete new Space instructions text. Preserve every unrequested line exactly; empty text clears only on explicit request. "
            "The length limit applies after removing byte-order marks and trimming surrounding whitespace."
        ),
    )

    @field_validator("content")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        # Same trim as the store, which also drops a byte-order mark, so a retry compares equal text.
        if value is None:
            return None
        value = value.replace("\ufeff", "").strip()
        if len(value) > MAX_CONTENT_CHARS:
            raise ValueError(f"Space instructions are limited to {MAX_CONTENT_CHARS} characters")
        return value


async def space_instructions_access(user_store: UserStore, user: User, workspace_id: str, *, manage: bool) -> tuple[bool, bool]:
    """(can_manage, archived) for a caller who may read; PermissionError otherwise, or when `manage` is not met."""
    try:
        can_read, can_manage, archived = await _access(user_store, workspace_id, user)
    except HTTPException as exc:
        raise PermissionError("Space instructions target unavailable") from exc
    if not can_read or (manage and (not can_manage or archived)):
        raise PermissionError("Space instructions access denied")
    return can_manage, archived


async def _authorized(ctx: FormContext, *, manage: bool) -> tuple[UserStore, SpaceInstructionsStore, dict[str, Any]]:
    store: UserStore = ctx.deps["user_store"]
    user = await store.get_user(ctx.user_id)
    if (
        not ctx.workspace_id
        or not ctx.team_id
        or user is None
        or user.status != "active"
        or not await store.user_has_active_team_membership(ctx.user_id, ctx.team_id)
    ):
        raise PermissionError("Space instructions actor unavailable")
    user = replace(user, team_id=ctx.team_id, active_team_id=ctx.team_id)
    await space_instructions_access(store, user, ctx.workspace_id, manage=manage)
    workspace = await store.get_workspace(ctx.workspace_id)
    if workspace is None or workspace["team_id"] != ctx.team_id:
        raise PermissionError("Space instructions target unavailable")
    return store, ctx.deps["space_instructions_store"], workspace


async def space_instructions_read(ctx: FormContext) -> dict[str, Any]:
    _, instructions, workspace = await _authorized(ctx, manage=False)
    current = await instructions.current(ctx.workspace_id)
    return {
        "workspace_id": ctx.workspace_id,
        "workspace_name": workspace["name"],
        "revision": current.version if current else 0,
        "fields": {"content": {"value": current.content if current else "", "unset": False}},
    }


async def space_instructions_context(ctx: FormContext) -> str:
    return str(await space_instructions_read(ctx))


async def space_instructions_materialize(ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    await _authorized(ctx, manage=True)
    return MaterializeResult(normalized=SpaceInstructionsDraftDTO.model_validate(data).model_dump(exclude_none=True))


async def space_instructions_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str) -> list[AppliedChange]:
    if op != "apply":
        raise LiteApplyError("configuration_action_not_supported", "Space instructions are restored from their version history")
    _, instructions, _ = await _authorized(ctx, manage=True)
    if len(rows) != 1 or rows[0].get("field") != "content":
        raise LiteApplyError("apply_validation_failed", "Space instructions take exactly one content row")
    row = rows[0]
    expected = row.get("baseline_revision")
    if type(expected) is not int or expected < 0:
        raise LiteApplyError("configuration_baseline_missing", "The Space instructions version is required")
    content = SpaceInstructionsDraftDTO.model_validate({"content": row.get("to")}).content or ""
    current = await instructions.current(ctx.workspace_id)
    before = current.content if current else ""
    if before == content:
        # Already saved (a retry after a lost response, or the same edit made by hand): nothing changes.
        return []
    if (current.version if current else 0) != expected or before != (row.get("from") or ""):
        raise LiteApplyError("configuration_baseline_changed", "Space instructions changed after the proposal")
    try:
        saved = await instructions.save(workspace_id=ctx.workspace_id, content=content, created_by=ctx.user_id, expected_version=expected)
    except SpaceInstructionsError as exc:
        if exc.code == "unchanged":
            return []
        code = "configuration_baseline_changed" if exc.code == "stale_version" else "apply_validation_failed"
        raise LiteApplyError(code, "The Space instructions save was rejected") from exc
    # The audit row points at versions instead of copying up to two full texts; the version history holds them.
    return [
        AppliedChange(
            scope="workspace",
            key="space_instructions",
            before=f"version {current.version}" if current else None,
            after=f"version {saved.version}",
            operation="put",
        )
    ]
