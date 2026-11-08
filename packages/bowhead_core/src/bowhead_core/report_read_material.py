"""Native report read observations carried by existing retained context."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, content_hash


class ReportReadObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: int = Field(default=1, ge=1, le=1)
    team_id: str
    user_id: str
    workspace_id: str
    session_id: str
    canvas_id: str | None
    sections: list[dict[str, Any]]
    source_sections: list[dict[str, Any]]
    source_refs: list[dict[str, Any]]
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReportReadAccess(Protocol):
    async def read(self, arguments: dict[str, Any], ctx: Any) -> tuple[dict[str, Any], ReportReadObservation | None]: ...

    async def validate(self, observation: ReportReadObservation) -> None: ...

    async def authorize_reference(self, reference: Any) -> bool: ...


def note_report_read(ctx: Any, arguments: dict[str, Any], result: dict[str, Any], observation: ReportReadObservation | None) -> None:
    from bowhead_core.ordinary_lead_material import collecting, material_observations

    if not collecting(ctx):
        return
    record = {
        "kind": "report_read" if observation is not None else "rejected",
        "skills": [],
        "tool": "read_report",
        "arguments_hash": content_hash(arguments),
        "result_hash": content_hash(result),
        "error": None,
    }
    if observation is not None:
        if observation.result_hash != record["result_hash"]:
            raise OrdinaryContinuationUnavailable()
        record["observation"] = observation.model_dump()
    elif result.get("ok") is not False:
        raise OrdinaryContinuationUnavailable()
    material_observations(ctx).tool_materials[ctx.ordinary_call_identity] = record


async def validate_report_read(ctx: Any, record: dict[str, Any]) -> None:
    try:
        observation = ReportReadObservation.model_validate(record["observation"])
        if any(getattr(observation, name) != getattr(ctx, name, None) for name in ("team_id", "user_id", "workspace_id", "session_id")):
            raise ValueError("Report read scope changed")
        if observation.result_hash != record["result_hash"]:
            raise ValueError("Report read observation changed")
        access = getattr(ctx, "report_read_access", None)
        if access is None:
            raise ValueError("Report reader authority unavailable")
        await access.validate(observation)
    except (ValueError, TypeError, KeyError) as exc:
        raise OrdinaryContinuationUnavailable() from exc
