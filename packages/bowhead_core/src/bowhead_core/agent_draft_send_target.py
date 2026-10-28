"""Exact creation-draft identity carried by a supported Chat start.

This pointer is durable intent, never permission or an editor capability.
"""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentDraftSendTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["agent_draft"] = "agent_draft"
    id: str = Field(pattern=r"^sad_[A-Za-z0-9_]{1,76}$")
    version: int = Field(ge=1, le=999_999_999)


def agent_draft_target_from_client_info(client_info: Mapping[str, str]) -> AgentDraftSendTarget | None:
    identity = client_info.get("agent_draft_id")
    version = client_info.get("agent_draft_version")
    dirty = client_info.get("agent_draft_dirty")
    if identity is None and version is None and dirty is None:
        return None
    if not identity or not version or not version.isascii() or not version.isdecimal() or len(version) > 9 or dirty not in {None, "0", "1"}:
        raise ValueError("invalid agent draft binding")
    return AgentDraftSendTarget(id=identity, version=int(version))
