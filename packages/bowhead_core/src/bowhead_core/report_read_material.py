"""Native report read observations carried by existing retained context."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, content_hash

_VALIDATION_PASS: ContextVar[dict[Any, Any] | None] = ContextVar("report_read_validation_pass", default=None)


@contextmanager
def report_read_validation_pass() -> Iterator[None]:
    """One pass over a conversation's recorded dependencies.

    A long conversation records the same report many times. Inside a pass the reader may load the report
    and project a given set of sources once and reuse them for every record; nothing is kept after it.
    """
    if _VALIDATION_PASS.get() is not None:
        yield
        return
    token = _VALIDATION_PASS.set({})
    try:
        yield
    finally:
        _VALIDATION_PASS.reset(token)


def validation_pass_cache() -> dict[Any, Any] | None:
    """The current pass's store, or None when each check must read for itself."""
    return _VALIDATION_PASS.get()


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

    async def authorize_references(self, references: list[Any]) -> list[bool]: ...


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


def forget_removed_report_sections(ctx: Any, section_ids: set[str]) -> None:
    """This Turn's recorded report reads stop requiring sections this Turn has removed.

    A recorded read requires every section it observed to still exist when the conversation is
    published or resumed. A writer that read its report and then removed one of its own sections
    with ``remove_report_section`` made that removal itself; without this the Turn ended
    ``clarification_resume_unavailable`` (116, 1.7.73, ``an observed task``). Only this Turn's
    collectors change: a section removed in any other way still makes a read unavailable.
    """
    if not section_ids:
        return
    from bowhead_core.ordinary_lead_material import material_observations

    collectors = {id(c): c for c in (material_observations(ctx), getattr(ctx, "context_source_observations", None)) if c is not None}
    for collector in collectors.values():
        for record in getattr(collector, "tool_materials", {}).values():
            observation = record.get("observation") if isinstance(record, dict) and record.get("kind") == "report_read" else None
            if not isinstance(observation, dict):
                continue
            kept = [section for section in observation.get("sections", []) if section.get("id") not in section_ids]
            if len(kept) != len(observation.get("sections", [])):
                record["observation"] = {**observation, "sections": kept}


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
