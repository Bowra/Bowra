"""Native cancellation of an owned pending ordinary question."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from bowhead_router.types import ToolSchema

from bowhead_core.tool_registry import ToolSpec
from bowhead_core.types import ToolAnnotations

if TYPE_CHECKING:
    from collections.abc import Callable

    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry


class ClarificationDismissService(Protocol):
    async def dismiss(self, request_id: str | None, ctx: ToolExecutionContext) -> dict[str, Any]: ...


def register_clarification_dismiss_tool(registry: ToolRegistry, service: Callable[[], ClarificationDismissService]) -> None:
    async def dismiss(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        if ctx.role != "lead":
            return {"ok": False, "error": "clarification_dismiss_lead_only"}
        request_id = arguments.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
            return {"ok": False, "error": "clarification_dismiss_invalid_target"}
        return await service().dismiss(request_id, ctx)

    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="clarification_dismiss",
                description=(
                    "Cancel an unanswered ordinary clarification in this user's current conversation when the user explicitly asks to cancel it. "
                    "This executes the real dismissal; prose such as 'cancelled' does not change state. "
                    "Pass request_id when known. Omit it only to select a single unambiguous pending question in this conversation. "
                    "If multiple targets are returned, ask which one; never guess. "
                    "Only a result with dismissed=true confirms cancellation. This does not stop running work or answer/skip a question. "
                    "An unrelated new message is not a cancellation request."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"request_id": {"type": "string", "minLength": 1, "maxLength": 128}},
                    "additionalProperties": False,
                },
            ),
            handler=dismiss,
            allowed_for_roles=frozenset({"lead"}),
            risk_level="low",
            needs_approval=False,
            source="compute",
            annotations=ToolAnnotations(read_only=False, destructive=False, open_world=False),
        )
    )
