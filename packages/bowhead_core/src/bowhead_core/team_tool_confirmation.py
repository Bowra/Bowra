"""Requester confirmation for team MCP tool calls (``FEATURE_TEAM_TOOL_CONFIRMATION``).

A team MCP tool whose category is not ``read`` waits inside its handler while the
member who started the task reviews the exact arguments in the browser. Only an
explicit approval releases the call; rejection, timeout and cancellation leave it
undone. The member may also allow the same tool, with the same schema, for the rest
of the current conversation.

The queue reuses :mod:`bowhead_core.delivery_confirmation`: it is in-process because
the waiting handler lives in the same single application process, so a restart ends
both the wait and the Turn. Session allowances are in-process for the same reason and
are bounded.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from typing import Any, Final, Literal

from bowhead_core.delivery_confirmation import MAX_PENDING_PER_OWNER, Decision, DeliveryConfirmations

__all__ = [
    "CONFIRM_SECONDS",
    "MAX_ARGUMENT_BYTES",
    "MAX_PENDING_PER_OWNER",
    "Decision",
    "TeamToolConfirmations",
    "default_team_tool_confirmations",
    "resolve_team_mcp_category",
    "team_mcp_category",
    "team_tool_confirmation_enabled",
    "team_tool_schema_digest",
]

TeamMcpCategory = Literal["read", "external_write", "destructive", "unknown"]
CategorySource = Literal["mcp_annotation", "unknown_default", "admin_override"]
CATEGORY_RANK: Final = {"read": 0, "external_write": 1, "destructive": 2}

# Matches the personal MCP confirmation window shown in its help.
CONFIRM_SECONDS: Final = 120
MAX_ARGUMENT_BYTES: Final = 64 * 1024
MAX_SESSION_ALLOWANCES: Final = 4096
# Allowance identity travels with the pending item but is never shown to the requester.
_INTERNAL_FIELDS: Final = frozenset({"schema_digest"})


def team_tool_confirmation_enabled() -> bool:
    """Read on every call so a deployment change applies to the next Turn."""
    return os.environ.get("FEATURE_TEAM_TOOL_CONFIRMATION", "0").strip().lower() in {"1", "true", "yes"}


def team_mcp_category(*, read_only: bool | None, destructive: bool | None) -> tuple[TeamMcpCategory, CategorySource]:
    """Category of a team MCP tool from its annotations, as ``(risk, risk_source)``.

    Read-only and destructive keep the conflict rules of ``classify_mcp_risk``. A tool
    explicitly annotated as neither read-only nor destructive is an additive write.
    Anything else stays ``unknown``: it is confirmed on every call at run time and is
    not offered for Agent authoring until its category is known.
    """
    if read_only is True and destructive is False:
        return "read", "mcp_annotation"
    if read_only is False and destructive is True:
        return "destructive", "mcp_annotation"
    if read_only is False and destructive is False:
        return "external_write", "mcp_annotation"
    return "unknown", "unknown_default"


def resolve_team_mcp_category(
    *, read_only: bool | None, destructive: bool | None, admin_category: str | None = None
) -> tuple[TeamMcpCategory, CategorySource]:
    """Annotation category, raised or settled by a valid administrator confirmation.

    ``admin_category`` is only passed after its schema digest matched (``team_tool_categories``);
    it may settle an ``unknown`` tool or raise a category, never lower one.
    """
    base, source = team_mcp_category(read_only=read_only, destructive=destructive)
    if admin_category in CATEGORY_RANK and (base == "unknown" or CATEGORY_RANK[admin_category] > CATEGORY_RANK[base]):
        return admin_category, "admin_override"  # type: ignore[return-value]
    return base, source


def team_tool_schema_digest(*, name: str, description: str, input_schema: dict[str, Any], annotations: dict[str, Any]) -> str:
    """Identity of the tool definition a session allowance applies to."""
    blob = json.dumps(
        {"name": name, "description": description, "input_schema": input_schema, "annotations": annotations},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


class TeamToolConfirmations(DeliveryConfirmations):
    def __init__(self, *, timeout_seconds: float = CONFIRM_SECONDS) -> None:
        super().__init__(id_prefix="ttc", timeout_seconds=timeout_seconds)
        self._allowances: OrderedDict[tuple[str, str, str, str], None] = OrderedDict()

    async def request(
        self,
        *,
        owner_user_id: str,
        turn_id: str,
        session_id: str,
        team_id: str,
        source_name: str,
        tool_ref: str,
        tool_name: str,
        description: str,
        schema_digest: str,
        arguments: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> Decision:
        """Wait for the requester's decision on this exact call."""
        if self.session_allows(owner_user_id, session_id, tool_ref, schema_digest):
            return "approved"
        return await self.request_snapshot(
            owner_user_id=owner_user_id,
            turn_id=turn_id,
            session_id=session_id,
            payload={
                "team_id": team_id,
                "source_name": source_name,
                "tool_ref": tool_ref,
                "tool_name": tool_name,
                "description": description,
                "arguments": arguments,
                "schema_digest": schema_digest,
            },
            timeout_seconds=timeout_seconds,
        )

    def pending(self, owner_user_id: str) -> list[dict[str, Any]]:
        return [{key: value for key, value in item.items() if key not in _INTERNAL_FIELDS} for item in super().pending(owner_user_id)]

    def approve_for_session(self, owner_user_id: str, confirmation_id: str) -> bool:
        """Approve this call and allow the same tool definition for the rest of its session."""
        item = self._pending.get(confirmation_id)
        if item is None or item.owner_user_id != owner_user_id or item.decision is not None or not item.session_id:
            return False
        key = (owner_user_id, item.session_id, str(item.payload["tool_ref"]), str(item.payload["schema_digest"]))
        if not self.decide(owner_user_id, confirmation_id, True):
            return False
        self._allowances[key] = None
        self._allowances.move_to_end(key)
        while len(self._allowances) > MAX_SESSION_ALLOWANCES:
            self._allowances.popitem(last=False)
        return True

    def session_allows(self, owner_user_id: str, session_id: str, tool_ref: str, schema_digest: str) -> bool:
        if not owner_user_id or not session_id:
            return False
        return (owner_user_id, session_id, tool_ref, schema_digest) in self._allowances


_default = TeamToolConfirmations()


def default_team_tool_confirmations() -> TeamToolConfirmations:
    return _default
