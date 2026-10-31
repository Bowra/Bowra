"""Continue an effect-free delegated workspace switch through fresh admission.

The original helper stopped at its first native switch gate. Its execution stays
blocked; the same Turn can retry that accepted delegation under current grants,
with the resolved switch outcome supplied to the helper that owns the call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_router.types import Message, ToolCall

from bowhead_core.datarc_switch_continuation import DatarcSwitchResolution, capture_datarc_switch_call

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolExecutionContext
    from bowhead_core.types import ToolInvocation


def capture_datarc_helper_switch_call(invocations: list[ToolInvocation]) -> dict | None:
    if not invocations or any(inv.tool_name != "tool_list_workspaces" for inv in invocations[:-1]):
        return None
    delegated = invocations[-1]
    if delegated.tool_name != "subagent_spawn" or delegated.error or not isinstance(delegated.result, dict):
        return None
    switch = capture_datarc_switch_call(delegated.nested)
    if switch is None or delegated.result.get("clarification") != delegated.nested[-1].result.get("clarification"):
        return None
    profile = delegated.result.get("profile")
    if not isinstance(profile, str) or not profile or profile == "lead" or not isinstance(delegated.arguments.get("task"), str):
        return None
    return {
        "contract": "datarc-helper-switch-call.v1",
        "call_id": delegated.call_id,
        "arguments": delegated.arguments,
        "profile": profile,
        "switch_call": switch,
    }


@dataclass(frozen=True)
class DatarcHelperSwitchResolution:
    switch: DatarcSwitchResolution
    original_call_id: str
    arguments_json: str
    profile: str

    @classmethod
    def from_card(cls, card: Any, *, source: dict, persistence: dict | None = None) -> DatarcHelperSwitchResolution | None:
        context = card.resume_context if isinstance(card.resume_context, dict) else {}
        raw = context.get("datarc_helper_switch_call")
        if raw is None:
            return None
        if (
            not isinstance(raw, dict)
            or set(raw) != {"contract", "call_id", "arguments", "profile", "switch_call"}
            or raw["contract"] != "datarc-helper-switch-call.v1"
            or not isinstance(raw["call_id"], str)
            or not 1 <= len(raw["call_id"]) <= 256
            or not isinstance(raw["profile"], str)
            or not 1 <= len(raw["profile"]) <= 256
            or raw["profile"] == "lead"
            or not isinstance(raw["arguments"], dict)
            or not isinstance(raw["arguments"].get("task"), str)
            or not raw["arguments"]["task"].strip()
            or not isinstance(raw["switch_call"], dict)
        ):
            raise ValueError("datarc_resume_scope_changed")
        arguments = json.dumps(raw["arguments"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(arguments.encode()) > 65536:
            raise ValueError("datarc_resume_scope_changed")
        switch = DatarcSwitchResolution.from_card(card, source=source, call=raw["switch_call"], persistence=persistence)
        if switch is None:
            raise ValueError("datarc_resume_scope_changed")
        return cls(switch, raw["call_id"], arguments, raw["profile"])

    @property
    def dispatch_id(self) -> str:
        return "datarc_resume_" + hashlib.sha256((self.switch.clarification_id + "\0" + self.original_call_id).encode()).hexdigest()[:32]

    def dispatch(self, ctx: ToolExecutionContext) -> ToolCall | None:
        self.switch.messages(ctx)
        if self.switch.skipped:
            return None
        return ToolCall(id=self.dispatch_id, name="subagent_spawn", arguments=json.loads(self.arguments_json))

    def helper_outcome(self, ctx: ToolExecutionContext, *, call_id: str | None, profile: str, task: str) -> DatarcSwitchResolution | None:
        if call_id != self.dispatch_id:
            return None
        self.switch.messages(ctx)
        if self.switch.skipped or profile != self.profile or task != json.loads(self.arguments_json)["task"]:
            raise ValueError("datarc_resume_scope_changed")
        return self.switch

    def skipped_messages(self, ctx: ToolExecutionContext) -> list[Message]:
        if not self.switch.skipped:
            return []
        outcome = json.loads(self.switch.messages(ctx)[1].content)
        return [
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=self.original_call_id, name="subagent_spawn", arguments=json.loads(self.arguments_json))],
            ),
            Message(
                role="tool",
                name="subagent_spawn",
                tool_call_id=self.original_call_id,
                content=json.dumps(
                    {
                        "ok": False,
                        "error_kind": "selection_skipped",
                        "helper_status": "blocked",
                        "query_executed": False,
                        "workspace_selection": outcome,
                    }
                ),
            ),
        ]
