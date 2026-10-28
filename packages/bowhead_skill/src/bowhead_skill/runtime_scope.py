"""Operation-level Skill scope shared by the compiler and actual handlers.

Names are an operation boundary, not proof of resource identity/materialization.
Legacy contexts have no scope; explicit empty scopes deny all Skill operations.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from bowhead_skill.script_langs import invoke_command_enabled, multiruntime_enabled

PRECISE_SKILL_SCOPE_VERSION = 1
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_WRAPPER_TO_PERMISSION = {"skill_read": "read", "skill_invoke": "invoke", "python_exec": "invoke"}

SKILL_UNAVAILABLE = "skill_unavailable"
SkillOperation = Literal["read", "invoke", "list"]
SkillUnavailableReason = Literal[
    "not_granted",
    "script_runtime_disabled",
    "command_disabled",
    "disabled_by_team",
    "not_approved",
    "pending_reapproval",
    "not_visible",
    "use_denied",
    "source_changed",
]
SKILL_UNAVAILABLE_REASONS: tuple[str, ...] = get_args(SkillUnavailableReason)


def skill_operation_for_tool(tool_name: str) -> SkillOperation:
    if tool_name == "skill_read":
        return "read"
    if tool_name == "skill_list":
        return "list"
    return "invoke"


def skill_name_for_call(tool_name: str, arguments: Mapping[str, Any] | None) -> str:
    return "python_sandbox" if tool_name == "python_exec" else _skill_name_from_arguments(arguments)


def _unavailable_message(skill: str, operation: str, reason: str, cause: str | None) -> str:
    label = f"Skill '{skill}'" if skill else "This Skill"
    verb = {"read": "be read", "list": "be listed"}.get(operation, "run")
    if reason == "not_granted":
        if cause == "no_scope":
            return "This Agent's Skill list is unavailable, so its Skills can't be listed. Ask the Agent's owner to release the Agent again."
        if not skill:
            return "This Skill call doesn't name a Skill. Call skill_list to see the Skills this Agent includes, then pass one as 'name'."
        if cause == "read_only":
            return (
                f"{label} is included in this Agent for reading only, so its scripts can't run here. "
                "If the Skill has scripts, ask the Agent's owner to save and release the Agent again so it can run them."
            )
        return f"{label} isn't included in this Agent. Ask the Agent's owner to add it in the Agent's configuration."
    if reason == "script_runtime_disabled":
        return (
            f"{label} can't run scripts here: this deployment hasn't enabled Skill scripts (BOWHEAD_SKILL_MULTIRUNTIME). "
            "Ask your administrator to enable it, or continue without running the Skill."
        )
    if reason == "command_disabled":
        return (
            f"{label} can't run a custom command here: this deployment hasn't enabled Skill commands (BOWHEAD_SKILL_INVOKE_COMMAND). "
            "Run one of the Skill's scripts with 'script' instead, or ask your administrator."
        )
    if reason == "disabled_by_team":
        if cause == "turn_resources":
            return f"{label} is turned off for this conversation by your team or by you. Turn it back on in the conversation's tools to use it."
        return f"{label} is turned off for this team, so it can't {verb}. Ask a team admin to turn it back on."
    if reason == "not_approved":
        if cause == "import_pending":
            return f"{label} is still being checked after upload. Wait for the check to finish in Skills, then retry."
        return f"{label} hasn't been approved by a team admin yet, so it can't {verb}. Ask a team admin to approve it."
    if reason == "pending_reapproval":
        return f"{label} was edited and is waiting for re-approval by a team admin, so it can't {verb}. Ask a team admin to approve the new version."
    if reason == "not_visible":
        if cause == "personal_disabled":
            return f"{label} is turned off in your Skills. Turn it back on in Skills to use it."
        if cause == "not_shared":
            return f"{label} isn't shared with you. Ask its owner or a team admin to share it with you."
        return f"{label} no longer exists or isn't available to you. Ask the Agent's owner to check the Skill in the Agent's configuration."
    if reason == "use_denied":
        return f"You aren't allowed to use {label}: a team admin restricted it for you. Ask a team admin for access."
    return f"{label} changed or couldn't be loaded while this call was checking it. Retry the call; if it keeps failing, ask the Agent's owner to check the Skill."


def skill_unavailable(skill: str, operation: SkillOperation, reason: SkillUnavailableReason, *, cause: str | None = None) -> dict[str, Any]:
    """The one model- and user-facing result for a denied Skill call.

    ``cause`` only refines the English sentence. It is never returned, so
    internal state (paths, other members, policy details) stays out of the result.
    """
    if reason not in SKILL_UNAVAILABLE_REASONS:
        reason = "source_changed"
    return {
        "ok": False,
        "error_kind": SKILL_UNAVAILABLE,
        "skill": skill,
        "operation": operation,
        "reason": reason,
        "error": _unavailable_message(skill, operation, reason, cause),
        # Only a Skill that changed while this call loaded it can succeed on a retry (its
        # sentence says so). Every other reason needs the fix named in ``error`` first.
        "retryable": reason == "source_changed",
    }


class SkillUnavailableError(ValueError):
    """Carries a decided ``skill_unavailable`` result across an authorizer boundary."""

    def __init__(self, result: dict[str, Any]):
        super().__init__(str(result.get("reason") or SKILL_UNAVAILABLE))
        self.result = result


def skill_script_runtime_denial(skill: str, *, uploaded: bool, command: bool) -> dict[str, Any] | None:
    """The deployment-switch decision for running an uploaded Skill's scripts.

    Callers read it at each dispatch boundary because the switches can change
    during a call; every boundary returns this same result. Installed (builtin)
    Skills run without the switch.
    """
    if not uploaded:
        return None
    if not multiruntime_enabled():
        return skill_unavailable(skill, "invoke", "script_runtime_disabled")
    if command and not invoke_command_enabled():
        return skill_unavailable(skill, "invoke", "command_disabled")
    return None


@dataclass(frozen=True, slots=True)
class ExactSkillRuntimeScope:
    """Exact names allowed at the generic Skill wrapper boundary."""

    read_names: tuple[str, ...]
    invoke_names: tuple[str, ...]
    schema_version: int = PRECISE_SKILL_SCOPE_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PRECISE_SKILL_SCOPE_VERSION:
            raise ValueError("unsupported precise Skill scope version")
        for names in (self.read_names, self.invoke_names):
            if tuple(sorted(set(names))) != names or any(not isinstance(name, str) or not _SAFE_TOKEN_RE.fullmatch(name) for name in names):
                raise ValueError("precise Skill scope names must be sorted and unique")
        if not set(self.invoke_names).issubset(self.read_names):
            raise ValueError("invoke Skill scope must be a subset of read scope")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def error_for_call(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
    ) -> str | None:
        if tool_name == "skill_list":
            return None  # The handler lists only read_names.
        permission = _WRAPPER_TO_PERMISSION.get(tool_name)
        if permission is None:
            return None
        skill_name = "python_sandbox" if tool_name == "python_exec" else _skill_name_from_arguments(arguments)
        allowed = self.read_names if permission == "read" else self.invoke_names
        if skill_name and skill_name in allowed:
            return None
        return "skill_scope_denied"


def _skill_name_from_arguments(arguments: Mapping[str, Any] | None) -> str:
    if not arguments:
        return ""
    raw: Mapping[str, Any] = arguments
    if len(arguments) == 1:
        wrapper_key = next(iter(arguments))
        wrapper_value = arguments[wrapper_key]
        if wrapper_key in {"raw_arguments", "_raw", "arguments"} and isinstance(wrapper_value, str):
            try:
                parsed = json.loads(wrapper_value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                raw = parsed
    value = raw.get("name")
    return str(value).strip() if isinstance(value, str) else ""


def skill_operation_denial(ctx: Any, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """Check before cache/resolve/spawn without importing core from Skill runtime."""
    if tool_name not in {*_WRAPPER_TO_PERMISSION, "skill_list"}:
        return None
    scope = getattr(ctx, "skill_runtime_scope", None)
    if scope is None:
        return None
    if not isinstance(scope, ExactSkillRuntimeScope):
        return skill_unavailable(skill_name_for_call(tool_name, arguments), skill_operation_for_tool(tool_name), "not_granted")
    return scope_denial(scope, tool_name, arguments)


def scope_denial(scope: ExactSkillRuntimeScope, tool_name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``not_granted`` for a call outside an Agent's declared Skills, else None."""
    if scope.error_for_call(tool_name=tool_name, arguments=arguments) is None:
        return None
    skill = skill_name_for_call(tool_name, arguments)
    operation = skill_operation_for_tool(tool_name)
    read_only = operation == "invoke" and skill in scope.read_names
    return skill_unavailable(skill, operation, "not_granted", cause="read_only" if read_only else None)


def filter_listed_skills(ctx: Any, entries: list[Any]) -> list[Any]:
    """An Agent with declared Skills lists only those; unscoped contexts list all."""
    scope = getattr(ctx, "skill_runtime_scope", None)
    if not isinstance(scope, ExactSkillRuntimeScope):
        return entries
    allowed = set(scope.read_names)
    return [entry for entry in entries if getattr(entry, "name", None) in allowed]


@runtime_checkable
class AssignedSkillMaterial(Protocol):
    """A server-owned, call-scoped exact package; model arguments cannot supply it."""

    spec: Any
    source_kind: str

    async def authorize_tool(self, tool_name: str, arguments: dict, ctx: Any) -> None: ...


def assigned_skill_active(ctx: Any) -> bool:
    return getattr(ctx, "skill_runtime_grants", None) is not None


async def assigned_skill_operation_denial(ctx: Any, tool_name: str, arguments: dict) -> dict[str, Any] | None:
    if reference_skill_active(ctx):
        if tool_name in {"skill_read", "skill_list"}:
            return None
    elif not assigned_skill_active(ctx):
        return None
    elif tool_name == "skill_list":
        # Listing returns only declared names and captures no Skill material.
        if isinstance(getattr(ctx, "skill_runtime_scope", None), ExactSkillRuntimeScope):
            return None
        return skill_unavailable("", "list", "not_granted", cause="no_scope")
    material = getattr(ctx, "assigned_skill_material", None)
    try:
        if not isinstance(material, AssignedSkillMaterial):
            raise ValueError()
        await material.authorize_tool(tool_name, arguments, ctx)
        return None
    except SkillUnavailableError as exc:
        return exc.result
    except Exception:
        return skill_unavailable(skill_name_for_call(tool_name, arguments), skill_operation_for_tool(tool_name), "source_changed")


@runtime_checkable
class ReferenceSkillToolAccess(Protocol):
    """Server-owned exact material access; never supplied by model arguments."""

    async def authorize_tool(self, tool_name: str, arguments: dict, ctx: Any) -> None: ...

    async def read_tool(self, arguments: dict, ctx: Any) -> dict: ...


def reference_skill_active(ctx: Any) -> bool:
    return getattr(ctx, "reference_skill_required", False) is not False or getattr(ctx, "reference_skill_access", None) is not None


def _reference_denied() -> dict[str, Any]:
    return {"ok": False, "error_kind": "reference_skill_unavailable", "error": "The exact Skill context is unavailable for this operation."}


async def reference_skill_context_denial(ctx: Any, tool_name: str) -> dict[str, Any] | None:
    if tool_name not in {*_WRAPPER_TO_PERMISSION, "skill_list"}:
        return None
    try:
        refresh = getattr(ctx, "ensure_reference_skill_context", None)
        if refresh is not None:
            await refresh()
    except Exception:
        return _reference_denied()
    return None


async def reference_skill_operation_denial(ctx: Any, tool_name: str, arguments: dict) -> dict[str, Any] | None:
    if tool_name not in {*_WRAPPER_TO_PERMISSION, "skill_list"}:
        return None
    if denial := await reference_skill_context_denial(ctx, tool_name):
        return denial
    if not reference_skill_active(ctx):
        return None
    access = getattr(ctx, "reference_skill_access", None)
    if getattr(ctx, "reference_skill_required", False) is not True or not isinstance(access, ReferenceSkillToolAccess):
        return _reference_denied()
    try:
        await access.authorize_tool(tool_name, arguments, ctx)
    except Exception:
        return _reference_denied()
    return None


async def read_reference_skill(ctx: Any, arguments: dict) -> dict[str, Any]:
    """Raw handler consumes the same exact gate; no legacy name resolution."""
    if denial := await reference_skill_operation_denial(ctx, "skill_read", arguments):
        return denial
    try:
        result = await ctx.reference_skill_access.read_tool(arguments, ctx)
        return result if type(result) is dict else _reference_denied()
    except Exception:
        return _reference_denied()
