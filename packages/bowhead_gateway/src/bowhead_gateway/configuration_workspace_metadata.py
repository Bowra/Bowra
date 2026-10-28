"""Current-Space metadata uses the original governed revision and save service."""

from dataclasses import replace
from typing import Any

from bowhead_core.config_copilot import AppliedChange, FormContext, LiteApplyError, MaterializeResult
from bowhead_core.space_governance import SpaceGovernanceError
from bowhead_core.user_store import _UNSET, UserStore
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from bowhead_gateway.routes.workspace_settings import _check_workspace_access


class WorkspaceMetadataDraftDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        max_length=120,
        json_schema_extra={"minLength": 1},
        description="Current Space name; surrounding whitespace is trimmed and an empty name is rejected.",
    )
    description: str | None = Field(
        default=None, max_length=2000, description="Current Space description; empty text clears it. Surrounding whitespace is trimmed."
    )

    @field_validator("name", "description")
    @classmethod
    def normalize_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if info.field_name == "name" and not value:
            raise ValueError("Space name cannot be empty")
        return value


async def _authorized_workspace(ctx: FormContext) -> tuple[UserStore, dict[str, Any]]:
    store: UserStore = ctx.deps["user_store"]
    user = await store.get_user(ctx.user_id)
    if (
        not ctx.workspace_id
        or not ctx.team_id
        or user is None
        or user.status != "active"
        or not await store.user_has_active_team_membership(ctx.user_id, ctx.team_id)
    ):
        raise PermissionError("Space metadata actor unavailable")
    user = replace(user, team_id=ctx.team_id, active_team_id=ctx.team_id)
    await _check_workspace_access(user=user, user_store=store, workspace_id=ctx.workspace_id, require_admin=True)
    workspace = await store.get_workspace(ctx.workspace_id)
    if workspace is None or workspace["team_id"] != ctx.team_id:
        raise PermissionError("Space metadata target unavailable")
    return store, workspace


async def workspace_metadata_read(ctx: FormContext) -> dict[str, Any]:
    _, workspace = await _authorized_workspace(ctx)
    return {
        "workspace_id": ctx.workspace_id,
        "workspace_name": workspace["name"],
        "revision": int(workspace["revision"]),
        "fields": {key: {"value": workspace[key] or "", "unset": False} for key in ("name", "description")},
    }


async def workspace_metadata_context(ctx: FormContext) -> str:
    return str(await workspace_metadata_read(ctx))


async def workspace_metadata_materialize(ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    await _authorized_workspace(ctx)
    return MaterializeResult(normalized=WorkspaceMetadataDraftDTO.model_validate(data).model_dump(exclude_none=True))


async def workspace_metadata_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str) -> list[AppliedChange]:
    if op != "apply":
        raise LiteApplyError("configuration_action_not_supported", "Space metadata has no automatic undo action")
    store, workspace = await _authorized_workspace(ctx)
    revisions = {row.get("baseline_revision") for row in rows}
    if len(revisions) != 1 or any(type(value) is not int or value < 1 for value in revisions):
        raise LiteApplyError("configuration_baseline_missing", "A governed Space revision is required")
    expected = revisions.pop()
    patch = {row["field"]: row["to"] for row in rows}
    if len(patch) != len(rows) or not patch:
        raise LiteApplyError("apply_validation_failed", "Metadata rows must be unique and nonempty")
    patch = WorkspaceMetadataDraftDTO.model_validate(patch).model_dump(exclude_none=True)
    if all((workspace[key] or "") == (value or "") for key, value in patch.items()):
        # The Space already holds every suggested value (a retry after a lost response, or the same edit made by
        # hand): confirming it changes nothing, so it is applied rather than reported as a changed baseline.
        return []
    if workspace["revision"] != expected or any(row.get("from_unset") or (workspace[row["field"]] or "") != row.get("from") for row in rows):
        raise LiteApplyError("configuration_baseline_changed", "Space metadata changed after the proposal")
    try:
        updated = await store.update_governed_workspace(
            team_id=ctx.team_id,
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            expected_revision=expected,
            name=patch.get("name"),
            description=(patch["description"] or None) if "description" in patch else _UNSET,
        )
    except SpaceGovernanceError as exc:
        code = "configuration_baseline_changed" if exc.code == "stale_workspace_update" else exc.code
        raise LiteApplyError(code, "The governed Space save was rejected") from exc
    return [AppliedChange(scope="workspace", key=key, before=workspace[key], after=updated[key], operation="put") for key in patch]
