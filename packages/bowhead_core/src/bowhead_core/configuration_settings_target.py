"""One current-Space settings selection admitted for an ordinary Web send."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SETTINGS_TARGET_CLIENT_KEY = "configuration_settings_target"


def unrestored_settings_target(resume_context: dict[str, Any]) -> str | None:
    """Retain a restriction after pause; persisted metadata cannot restore authority."""
    return "configuration_target_requires_new_send" if SETTINGS_TARGET_CLIENT_KEY in resume_context else None


class WorkspaceSettingsSendTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    form_id: Literal["workspace_i18n", "workspace_metadata"]
    user_id: str = Field(min_length=1, max_length=256)
    team_id: str = Field(min_length=1, max_length=256)
    workspace_id: str = Field(min_length=1, max_length=256)
    edit_generation: int = Field(ge=0, le=9_007_199_254_740_991)
    saved_ready: bool
    pending_writes: int = Field(ge=0)
    unconfirmed_fields: list[str] = Field(max_length=6)

    @model_validator(mode="after")
    def validate_ready(self) -> WorkspaceSettingsSendTarget:
        if not self.saved_ready:
            raise ValueError("settings page has no saved snapshot")
        return self

    def projection(self) -> dict[str, Any]:
        return self.model_dump(exclude={"saved_ready", "pending_writes", "unconfirmed_fields"})


@dataclass(frozen=True)
class AdmittedWorkspaceSettingsTarget:
    target: WorkspaceSettingsSendTarget
    recheck: Callable[[], Awaitable[None]]


async def validate_settings_target(ctx: Any, form_id: str) -> dict[str, Any] | None:
    """Selection is a restriction; only the Gateway can supply its access callback."""
    admitted = getattr(ctx, "configuration_settings_target", None)
    if admitted is None:
        return None
    if type(admitted) is not AdmittedWorkspaceSettingsTarget:
        return {"ok": False, "error": "configuration_target_not_admitted"}
    target = admitted.target
    if form_id != target.form_id or any(getattr(ctx, key, "") != getattr(target, key) for key in ("user_id", "team_id", "workspace_id")):
        return {"ok": False, "error": "configuration_target_changed"}
    try:
        await admitted.recheck()
    except Exception:
        return {"ok": False, "error": "configuration_target_access_changed"}
    return None


def active_settings_target_prompt(ctx: Any) -> str:
    """Describe only the admitted form; selection cannot broaden the specialist's scope."""
    admitted = getattr(ctx, "configuration_settings_target", None)
    if admitted is None:
        return ""
    if type(admitted) is not AdmittedWorkspaceSettingsTarget:
        return "\n<active_settings_target>The settings selection is not admitted. Ask for a new send from the original settings page; do not read or propose settings through another form.</active_settings_target>"
    from bowhead_core.configuration_agent import configuration_form_fields

    form_id = admitted.target.form_id
    fields = ", ".join(sorted(configuration_form_fields(form_id)))
    return (
        "\n<active_settings_target>\n"
        f"This send is bound to the user's original Space settings page: configuration_form_id={form_id}. "
        f"Only these fields are admitted: {fields}. For edits or settings reads, delegate to configuration-agent with that exact form. "
        "Do not call config_read/config_propose for this domain. The server rechecks the original scope and access; tab changes cannot retarget this send. "
        "Return a pending suggestion for user confirmation, never describe a candidate as saved. "
        "Other fields and other settings targets need their own supported path. Ordinary questions remain ordinary answers.\n"
        "If this turn resumed without its live settings admission, ask for a new send from the original settings page; "
        "do not attempt settings reads or proposals through another form.\n</active_settings_target>"
    )
