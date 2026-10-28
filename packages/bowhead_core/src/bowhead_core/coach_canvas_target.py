"""An original resource editor baseline captured for one ordinary Chat send."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

COACH_CANVAS_TARGET_KEY = "coach_canvas_target"


class ExactEditorTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SkillEditorTarget(ExactEditorTarget):
    kind: Literal["skill"]
    id: str = Field(min_length=1, max_length=160)
    source_kind: Literal["user", "team", "builtin"]
    revision: str = Field(min_length=1, max_length=160)


class PluginEditorTarget(ExactEditorTarget):
    kind: Literal["plugin"]
    id: str = Field(pattern=r"^tcap_[0-9a-f]{32}$")
    version: int = Field(ge=1, le=999_999_999)
    revision: int = Field(ge=1, le=999_999_999)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class WorkingCopyBaseline(ExactEditorTarget):
    id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1, le=999_999_999)
    base_release_id: str | None = Field(max_length=160)


class AgentEditorTarget(ExactEditorTarget):
    kind: Literal["agent"]
    id: str = Field(pattern=r"^osa_[A-Za-z0-9_-]+$", max_length=160)
    version: int = Field(ge=1, le=999_999_999)
    live_release_id: str = Field(min_length=1, max_length=160)
    working_copy: WorkingCopyBaseline | None


class CoachCanvasSendTarget(ExactEditorTarget):
    schema_version: Literal[1] = 1
    user_id: str = Field(min_length=1, max_length=160)
    team_id: str = Field(min_length=1, max_length=160)
    workspace_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    target: Annotated[SkillEditorTarget | PluginEditorTarget | AgentEditorTarget, Field(discriminator="kind")]
    dirty: bool
    pending_write: bool


@dataclass(frozen=True)
class AdmittedCoachCanvasTarget:
    target: CoachCanvasSendTarget
    recheck: Callable[[], Awaitable[dict]]


def unrestored_coach_canvas_target(resume_context: dict) -> str | None:
    """Persist intent without reconstructing live editor authority after a pause."""
    return "coach_canvas_target_requires_new_send" if COACH_CANVAS_TARGET_KEY in resume_context else None


def coach_canvas_projection(admitted: object) -> dict:
    """Return a safe historical pointer, with no resource contents or authority."""
    if type(admitted) is not AdmittedCoachCanvasTarget:
        return {}
    return {COACH_CANVAS_TARGET_KEY: admitted.target.model_dump()}
