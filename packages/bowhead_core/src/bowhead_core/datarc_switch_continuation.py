"""Project a resolved native workspace choice into its original Tool call.

This is model-visible outcome data, not execution permission or a replay of a
switch. The gateway must first reauthorize the selected source for the card.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_router.types import Message, ToolCall

from bowhead_core.datarc_selection import DatarcSelectionSnapshot, accepted_datarc_selection

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolExecutionContext
    from bowhead_core.types import ToolInvocation


def capture_datarc_switch_call(invocations: list[ToolInvocation]) -> dict | None:
    """Retain a sole native switch gate after optional workspace-list reads."""
    from bowhead_core.agents.clarification_tool import is_tool_context_selection

    if not invocations or any(inv.tool_name != "tool_list_workspaces" for inv in invocations[:-1]):
        return None
    inv = invocations[-1]
    if inv.tool_name != "tool_switch_context" or inv.error or not inv.call_id or not is_tool_context_selection(inv.result):
        return None
    if inv.result["clarification"].get("tool") != "datarc":
        return None
    return {"contract": "datarc-switch-call.v1", "call_id": inv.call_id, "arguments": inv.arguments}


@dataclass(frozen=True)
class DatarcSwitchResolution:
    clarification_id: str
    owner: tuple[str, str, str, str, str]
    call_id: str
    arguments_json: str
    source: DatarcSelectionSnapshot
    candidate_count: int
    skipped: bool = False
    persistence_json: str | None = None

    @classmethod
    def from_card(cls, card: Any, *, source: dict, call: dict | None = None, persistence: dict | None = None) -> DatarcSwitchResolution | None:
        context = card.resume_context if isinstance(card.resume_context, dict) else {}
        raw = call if call is not None else context.get("datarc_switch_call")
        if raw is None:
            return None
        snapshot = DatarcSelectionSnapshot.model_validate(source)
        skipped = card.chosen_value == "__bowhead_clarification_skip__"
        if (
            card.kind != "tool_context_select"
            or card.tool != "datarc"
            or card.status != "resolved"
            or (not skipped and snapshot.state != "selected")
            or snapshot.user_id != card.user_id
            or snapshot.team_id != context.get("team_id")
            or (not skipped and snapshot.datarc_workspace_id != card.chosen_value)
            or (not skipped and not any(candidate.value == card.chosen_value for candidate in card.candidates))
            or not card.candidates
            or not isinstance(raw, dict)
            or set(raw) != {"contract", "call_id", "arguments"}
            or raw["contract"] != "datarc-switch-call.v1"
            or not isinstance(raw["call_id"], str)
            or not 1 <= len(raw["call_id"]) <= 256
            or not isinstance(raw["arguments"], dict)
            or not isinstance(raw["arguments"].get("name"), str)
            or not raw["arguments"]["name"].strip()
            or raw["arguments"].get("tool", "datarc") != "datarc"
        ):
            raise ValueError("datarc_resume_scope_changed")
        arguments = json.dumps(raw["arguments"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(arguments.encode()) > 65536:
            raise ValueError("datarc_resume_scope_changed")
        if persistence is not None and (
            not isinstance(persistence, dict)
            or set(persistence) != {"status", "scope", "reason"}
            or persistence["status"] not in {"saved", "skipped", "failed", "unknown", "not_attempted"}
            or persistence["scope"] not in {"session", "default"}
            or not isinstance(persistence["reason"], str)
            or len(persistence["reason"]) > 1024
            or skipped
        ):
            raise ValueError("datarc_resume_scope_changed")
        return cls(
            card.request_id,
            (snapshot.team_id, card.user_id, card.workspace_id, card.session_id, card.turn_id),
            raw["call_id"],
            arguments,
            snapshot,
            len(card.candidates),
            skipped,
            json.dumps(persistence) if persistence is not None else None,
        )

    def messages(self, ctx: ToolExecutionContext) -> list[Message]:
        if self.owner != (ctx.team_id, ctx.user_id, ctx.workspace_id, ctx.session_id, ctx.turn_id) or self.source != accepted_datarc_selection(
            ctx
        ).snapshot(team_id=ctx.team_id or ""):
            raise ValueError("datarc_resume_scope_changed")
        result = {
            "ok": not self.skipped,
            "selection": {
                "kind": "tool_context_select",
                "status": "skipped" if self.skipped else "selected",
                "candidate_count": self.candidate_count,
                "selected_by": "user",
            },
            "clarification_id": self.clarification_id,
            "preference_write_during_restore": False,
            "current_preference_verified": False,
        }
        if self.skipped:
            result.update(
                error_kind="selection_skipped",
                note="The original name matched multiple candidates. The user skipped the choice; this switch was not completed.",
            )
        else:
            result.update(
                switched={"id": self.source.datarc_workspace_id, "name": self.source.name},
                effective="run",
                runtime_scope="owning_run",
                note="The original name was ambiguous. The user selected this source through the choice card. Restoring this result did not repeat the switch or its preference write.",
            )
        if self.persistence_json is not None:
            result["persistence"] = json.loads(self.persistence_json)
            result["persistence_receipt_origin"] = "original_selection"
        return [
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=self.call_id, name="tool_switch_context", arguments=json.loads(self.arguments_json))],
            ),
            Message(role="tool", name="tool_switch_context", tool_call_id=self.call_id, content=json.dumps(result, ensure_ascii=False)),
        ]
