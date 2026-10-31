"""Continue the native loop after its first BI workspace-selection gate.

This is private conversation material, not an execution permit or a complete
Agent checkpoint. V1 captures the first call; V2 also retains preceding exact
Skill reads; V3 also retains original attachment data; V4 preserves the exact
discovery tool at the same workspace gate. None has contacted BI.
A caller must durably bind the answer
and reauthorize the original execution before use.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from bowhead_router.types import Message, ToolCall
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bowhead_core.agent_run_limits import MAX_STEP_LIMIT, AgentLoopBudget, persisted_step_limit
from bowhead_core.chat_references import decode_reference_json

if TYPE_CHECKING:
    from bowhead_core.agent_execution_binding import ExactAgentExecutionBinding
    from bowhead_core.tool_registry import ToolExecutionContext
    from bowhead_core.types import ToolInvocation


def _capture_workspace_call(
    *,
    tool_names: frozenset[str],
    messages: list[Message],
    invocations: list[ToolInvocation],
    iteration: int,
    binding: ExactAgentExecutionBinding | None,
    ctx: ToolExecutionContext,
    task: str,
    subagent_id: str,
    profile: str,
    tool_deny: frozenset[str],
    effective_tools: frozenset[str],
    max_iterations: int,
    loop_budget: AgentLoopBudget | None = None,
) -> dict | None:
    if binding is None or profile == "lead" or iteration != 0 or len(invocations) != 1 or len(messages) != 3:
        return None
    invocation = invocations[0]
    result = invocation.result
    question = result.get("clarification") if isinstance(result, dict) else None
    if (
        invocation.tool_name not in tool_names
        or invocation.error is not None
        or not isinstance(question, dict)
        or question.get("kind") != "tool_context_select"
        or question.get("tool") != "datarc"
        or not isinstance(question.get("options"), list)
        or len(question["options"]) < 2
        or result.get("error_kind") != "needs_clarification"
        or ctx.datarc is None
        or ctx.active_datarc_project is not None
        or len(ctx.accessible_datarc_workspaces or ()) <= 1
        or [message.role for message in messages] != ["user", "assistant", "tool"]
        or messages[0].content != task
        or any(message.images for message in messages)
        or len(messages[1].tool_calls or ()) != 1
    ):
        return None
    call = messages[1].tool_calls[0]
    if call.name != invocation.tool_name or messages[2].tool_call_id != call.id:
        return None
    # The transcript contains precisely the real result sent back to the
    # model, including the original tool-call identity and arguments.
    try:
        if not isinstance(json.loads(messages[2].content), dict):
            return None
    except (ValueError, TypeError):
        return None
    encoded_arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(messages[2].content.encode()) + len(messages[1].content.encode()) + len(encoded_arguments.encode()) > 131072:
        return None
    return dict(
        tool_name=invocation.tool_name,
        **{key: getattr(ctx, key) for key in ("turn_id", "session_id", "team_id", "user_id", "workspace_id")},
        execution_id=binding.execution_id,
        policy_fingerprint=binding.policy_fingerprint,
        capability_fingerprint=binding.capability_fingerprint,
        subagent_id=subagent_id,
        profile=profile,
        task_hash="sha256:" + hashlib.sha256(task.encode()).hexdigest(),
        tool_deny=tuple(sorted(tool_deny)),
        effective_tools=tuple(sorted(effective_tools)),
        max_iterations=persisted_step_limit(max_iterations) if loop_budget is not None else max_iterations,
        loop_budget=loop_budget,
        assistant_text=messages[1].content,
        tool_call_id=call.id,
        arguments_json=encoded_arguments,
        result_json=messages[2].content,
    )


class BiWorkspaceContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    turn_id: str
    session_id: str
    team_id: str
    user_id: str
    workspace_id: str
    execution_id: str
    policy_fingerprint: str
    capability_fingerprint: str
    subagent_id: str
    profile: str
    task_hash: str
    tool_deny: tuple[str, ...]
    effective_tools: tuple[str, ...]
    max_iterations: int | None = Field(ge=1, le=MAX_STEP_LIMIT)
    loop_budget: AgentLoopBudget | None = Field(default=None, exclude_if=lambda value: value is None)
    assistant_text: str = Field(max_length=131072)
    tool_call_id: str = Field(min_length=1, max_length=256)
    arguments_json: str = Field(max_length=65536)
    result_json: str = Field(max_length=131072)

    @model_validator(mode="after")
    def validate_material(self) -> BiWorkspaceContinuation:
        arguments = decode_reference_json(self.arguments_json, max_bytes=65536, max_depth=10)
        result = decode_reference_json(self.result_json, max_bytes=131072, max_depth=10)
        question = result.get("clarification") if isinstance(result, dict) else None
        if (
            not isinstance(arguments, dict)
            or not isinstance(question, dict)
            or result.get("error_kind") != "needs_clarification"
            or question.get("kind") != "tool_context_select"
            or question.get("tool") != "datarc"
            or not isinstance(question.get("options"), list)
            or len(question["options"]) < 2
        ):
            raise ValueError("reference_continuation_changed")
        return self

    @classmethod
    def capture(cls, **args) -> BiWorkspaceContinuation | None:
        fields = _capture_workspace_call(tool_names=frozenset({"bi_query"}), **args)
        if fields is None:
            return None
        fields.pop("tool_name")
        return cls(**fields)

    @property
    def next_iteration(self) -> int:
        return 1

    def resume_messages(
        self,
        *,
        binding: ExactAgentExecutionBinding | None,
        ctx: ToolExecutionContext,
        task: str,
        subagent_id: str,
        profile: str,
        tool_deny: frozenset[str],
        effective_tools: frozenset[str],
        max_iterations: int,
        answer: str | None,
    ) -> list[Message]:
        if (
            binding is None
            or self.execution_id != binding.execution_id
            or self.policy_fingerprint != binding.policy_fingerprint
            or self.capability_fingerprint != binding.capability_fingerprint
            or any(getattr(ctx, key) != getattr(self, key) for key in ("turn_id", "session_id", "team_id", "user_id", "workspace_id"))
            or self.task_hash != "sha256:" + hashlib.sha256(task.encode()).hexdigest()
            or self.subagent_id != subagent_id
            or self.profile != profile
            or self.tool_deny != tuple(sorted(tool_deny))
            or self.effective_tools != tuple(sorted(effective_tools))
            or self.max_iterations != persisted_step_limit(max_iterations)
            or not isinstance(answer, str)
            or not answer.strip()
            or len(answer.encode()) > 32768
        ):
            raise ValueError("reference_continuation_changed")
        return [
            Message(role="user", content=task),
            Message(
                role="assistant",
                content=self.assistant_text,
                tool_calls=[ToolCall(id=self.tool_call_id, name=getattr(self, "tool_name", "bi_query"), arguments=json.loads(self.arguments_json))],
            ),
            Message(role="tool", name=getattr(self, "tool_name", "bi_query"), tool_call_id=self.tool_call_id, content=self.result_json),
            Message(role="user", content=answer),
        ]


def _capture_read_history(**args) -> str | None:
    from dataclasses import asdict

    messages, invocations, iteration, ctx = (args[key] for key in ("messages", "invocations", "iteration", "ctx"))
    if (
        not 1 <= iteration <= 32
        or len(messages) != 2 * iteration + 3
        or len(invocations) != iteration + 1
        or not ctx.reference_skill_required
        or ctx.reference_skill_access is None
        or any(
            inv.tool_name != "skill_read"
            or inv.error is not None
            or not isinstance(inv.result, dict)
            or inv.result.get("ok") is False
            or inv.result.get("error")
            or inv.result.get("error_kind")
            for inv in invocations[:-1]
        )
    ):
        return None
    try:
        return json.dumps([asdict(message) for message in messages[1:-2]], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None


class BiWorkspaceReadContinuation(BiWorkspaceContinuation):
    """A first workspace gate after successful exact, local Skill reads only.

    The previous model/tool messages are immutable material. Other tools,
    parallel calls, failures and repeated pauses require a broader checkpoint.
    """

    schema_version: Literal[2] = 2
    completed_read_rounds: int = Field(ge=1, le=32)
    read_history_json: str = Field(max_length=524288, repr=False)

    def read_messages(self) -> list[Message]:
        history = decode_reference_json(self.read_history_json, max_bytes=524288, max_depth=16)
        if not isinstance(history, list) or len(history) != 2 * self.completed_read_rounds:
            raise ValueError("reference_continuation_changed")
        messages = []
        identities = {self.tool_call_id}
        for index in range(0, len(history), 2):
            assistant, result = history[index : index + 2]
            expected = {"role", "content", "name", "tool_call_id", "tool_calls", "images"}
            if any(
                not isinstance(item, dict) or set(item) != expected or item["images"] != [] or not isinstance(item["content"], str)
                for item in (assistant, result)
            ):
                raise ValueError("reference_continuation_changed")
            calls = assistant["tool_calls"]
            if (
                assistant["role"] != "assistant"
                or assistant["name"] is not None
                or assistant["tool_call_id"] is not None
                or not isinstance(calls, list)
                or len(calls) != 1
                or not isinstance(calls[0], dict)
                or set(calls[0]) != {"id", "name", "arguments"}
            ):
                raise ValueError("reference_continuation_changed")
            call = calls[0]
            if (
                call["name"] != "skill_read"
                or not isinstance(call["id"], str)
                or not call["id"]
                or len(call["id"]) > 256
                or call["id"] in identities
                or not isinstance(call["arguments"], dict)
                or result["role"] != "tool"
                or result["name"] != "skill_read"
                or result["tool_call_id"] != call["id"]
                or result["tool_calls"] is not None
            ):
                raise ValueError("reference_continuation_changed")
            identities.add(call["id"])
            messages.extend(
                [
                    Message(role="assistant", content=assistant["content"], tool_calls=[ToolCall(**call)]),
                    Message(role="tool", content=result["content"], name="skill_read", tool_call_id=call["id"]),
                ]
            )
        return messages

    @field_validator("max_iterations", mode="before")
    @classmethod
    def _store_unlimited_as_none(cls, value: object) -> object:
        # An unlimited Team step limit is recorded as None, never as the internal loop bound.
        return persisted_step_limit(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @model_validator(mode="after")
    def validate_read_history(self):
        if self.max_iterations is not None and self.next_iteration > self.max_iterations:
            raise ValueError("reference_continuation_changed")
        self.read_messages()
        return self

    @property
    def next_iteration(self) -> int:
        return self.completed_read_rounds + 1

    @classmethod
    def capture_after_reads(cls, **args) -> BiWorkspaceReadContinuation | None:
        history = _capture_read_history(**args)
        if history is None:
            return None
        messages, invocations = args["messages"], args["invocations"]
        final = BiWorkspaceContinuation.capture(
            **{**args, "iteration": 0, "messages": [messages[0], *messages[-2:]], "invocations": invocations[-1:]}
        )
        if final is None:
            return None
        try:
            return cls(**{**final.model_dump(), "schema_version": 2}, completed_read_rounds=args["iteration"], read_history_json=history)
        except (ValueError, TypeError, RecursionError):
            return None

    def resume_messages(self, **args) -> list[Message]:
        messages = super().resume_messages(**args)
        return [messages[0], *self.read_messages(), *messages[1:]]


class BiWorkspaceAttachmentContinuation(BiWorkspaceReadContinuation):
    """Retain the exact user-data tail alongside the existing BI transcript."""

    schema_version: Literal[3] = 3
    completed_read_rounds: int = Field(ge=0, le=32)
    attachment_context: str = Field(min_length=1, max_length=262144, repr=False)

    @model_validator(mode="after")
    def validate_checkpoint_size(self):
        if len(self.model_dump_json().encode()) > 1048576:
            raise ValueError("reference_continuation_changed")
        return self


class BiWorkspaceToolContinuation(BiWorkspaceReadContinuation):
    """The same pre-network workspace gate from the three BI discovery tools."""

    schema_version: Literal[4] = 4
    tool_name: Literal["bi_schema", "bi_list_measures", "bi_list_dimensions"]
    completed_read_rounds: int = Field(ge=0, le=32)
    attachment_context: str | None = Field(default=None, min_length=1, max_length=262144, repr=False)

    @model_validator(mode="after")
    def validate_checkpoint_size(self):
        if self.tool_name not in self.effective_tools or self.tool_name in self.tool_deny or len(self.model_dump_json().encode()) > 1048576:
            raise ValueError("reference_continuation_changed")
        return self

    @classmethod
    def capture_tool(cls, *, attachment_context: str | None, **args) -> BiWorkspaceToolContinuation | None:
        iteration = args["iteration"]
        history = "[]" if iteration == 0 else _capture_read_history(**args)
        if history is None:
            return None
        messages, invocations = args["messages"], args["invocations"]
        fields = _capture_workspace_call(
            tool_names=frozenset({"bi_schema", "bi_list_measures", "bi_list_dimensions"}),
            **(args if iteration == 0 else {**args, "iteration": 0, "messages": [messages[0], *messages[-2:]], "invocations": invocations[-1:]}),
        )
        if fields is None:
            return None
        try:
            return cls(**fields, completed_read_rounds=iteration, read_history_json=history, attachment_context=attachment_context or None)
        except (ValueError, TypeError, RecursionError):
            return None


def capture_bi_workspace_checkpoint(*, attachment_context: str | None = None, **args) -> BiWorkspaceContinuation | None:
    checkpoint = BiWorkspaceContinuation.capture(**args) or BiWorkspaceReadContinuation.capture_after_reads(**args)
    if checkpoint is None:
        return BiWorkspaceToolContinuation.capture_tool(attachment_context=attachment_context, **args)
    if not attachment_context:
        return checkpoint
    try:
        return BiWorkspaceAttachmentContinuation(
            **{
                "completed_read_rounds": 0,
                "read_history_json": "[]",
                **checkpoint.model_dump(),
                "schema_version": 3,
                "attachment_context": attachment_context,
            }
        )
    except ValueError:
        return None
