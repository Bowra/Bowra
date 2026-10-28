"""Read the authenticated requester's minimal account profile for Lead."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from bowhead_router.types import ToolSchema

from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY
from bowhead_core.tool_registry import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
    from bowhead_core.user_store import UserStore

logger = structlog.get_logger(__name__)


def register_current_user_tool(registry: ToolRegistry, *, store_provider: Callable[[], UserStore | None]) -> None:
    async def handler(arguments: dict[str, object], ctx: ToolExecutionContext) -> dict[str, object]:
        if arguments:
            return {"ok": False, "error": "invalid_arguments", "message": "Call without arguments; other accounts cannot be selected."}
        if not ctx.user_id:
            return {
                "ok": False,
                "error": "user_identity_unavailable",
                "message": "No authenticated requester. Ask the user for their recipient address.",
            }
        try:
            store = store_provider()
            user = await store.get_user(ctx.user_id) if store is not None else None
        except Exception:
            logger.warning("current_user.lookup_failed", user_id=ctx.user_id)
            return {
                "ok": False,
                "error": "user_profile_unavailable",
                "message": "Account details are unavailable. Ask the user for their recipient address.",
            }
        if user is None or user.status != "active":
            return {"ok": False, "error": "user_profile_unavailable", "message": "No active account found. Ask the user for their recipient address."}
        return {"ok": True, "display_name": user.display_name, "email": user.email or None, "email_verified": user.email_verified}

    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="get_current_user",
                description=(
                    "Read the current authenticated requester's account display name, email and email verification status. "
                    "When the user asks to send something to 'my email', use this tool to resolve their account email; "
                    "an explicitly supplied recipient takes precedence. Never guess an address. If email is missing, ask for it. "
                    "This read does not authorize sending: keep the delivery tool's confirmation. "
                    "Read only when these account details are needed; this tool cannot query other people."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            handler=handler,
            allowed_for_roles=frozenset({"lead"}),
            effect_spec=TOOL_EFFECT_READ_ONLY,
        )
    )
