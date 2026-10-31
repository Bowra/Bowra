"""Private, bounded transcript for a question after authorized, successful reads."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

from bowhead_router.types import ImageRef, Message, ToolCall
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_run_limits import MAX_STEP_LIMIT, AgentLoopBudget, persisted_step_limit
from bowhead_core.chat_references import decode_reference_json
from bowhead_core.output_recovery import STATE_NAME, OutputRecovery

if TYPE_CHECKING:
    from bowhead_core.agent_execution_binding import ExactAgentExecutionBinding
    from bowhead_core.tool_registry import ToolExecutionContext
    from bowhead_core.types import ToolInvocation


READ_QUESTION_REPAIR_PROMPT = (
    "You have read the selected Skill, but ended without subagent_return. "
    "Do not ask for missing input in final prose. If a required value is missing, "
    "call subagent_return with payload.error.kind=needs_user_input and "
    "payload.error.clarification_payload.questions. Each question MUST contain a nonempty "
    '"question" string, "type":"free_text", and "options":[] for a numeric value. '
    "Otherwise call subagent_return with the completed result. Do not read the Skill again."
)


def _attachment_fingerprint(context: str | None, images: list[ImageRef] | None) -> str | None:
    """Bind reopened model material without duplicating text or image bytes."""
    if not context and not images:
        return None
    if context is not None and not isinstance(context, str):
        raise ValueError("reference_continuation_changed")
    image_hashes = []
    for item in images or []:
        if not isinstance(item, ImageRef) or item.mime not in {"image/png", "image/jpeg", "image/webp"} or not isinstance(item.base64, str):
            raise ValueError("reference_continuation_changed")
        image_hashes.append((item.mime, hashlib.sha256(item.base64.encode()).hexdigest()))
    encoded = json.dumps([hashlib.sha256((context or "").encode()).hexdigest(), image_hashes], separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


class ReferenceReadQuestion(BaseModel):
    """Conversation material only; the reference runtime owns durable authorization."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1, 2, 3, 4, 5] = 1
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
    extra_instructions: str = Field(max_length=32768)
    language_override: str | None = None
    tool_deny: tuple[str, ...]
    effective_tools: tuple[str, ...]
    max_iterations: int | None = Field(ge=2, le=MAX_STEP_LIMIT)
    loop_budget: AgentLoopBudget | None = Field(default=None, exclude_if=lambda value: value is None)
    output_recovery_json: str | None = Field(default=None, max_length=65536, repr=False, exclude_if=lambda value: value is None)
    completed_read_rounds: int = Field(ge=1, le=8)
    repair_rounds: Literal[0, 1] = 0
    messages_json: str = Field(max_length=65536, repr=False)

    @property
    def next_iteration(self) -> int:
        return self.completed_read_rounds + self.repair_rounds + 1

    def _decode(self) -> tuple[list, str | None]:
        raw = decode_reference_json(self.messages_json, max_bytes=65536, max_depth=16)
        fingerprint = None
        if self.schema_version >= 3:
            keys = {"messages", "attachment_fingerprint"} | (
                {"mcp_receipts"} if self.schema_version == 4 else {"read_tools"} if self.schema_version == 5 else set()
            )
            if not isinstance(raw, dict) or set(raw) != keys:
                raise ValueError("reference_continuation_changed")
            fingerprint = raw["attachment_fingerprint"]
            if not (self.schema_version == 4 and fingerprint is None) and (
                not isinstance(fingerprint, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
            ):
                raise ValueError("reference_continuation_changed")
            raw = raw["messages"]
        if not isinstance(raw, list) or len(raw) != 2 * (self.completed_read_rounds + 1) + 1:
            raise ValueError("reference_continuation_changed")
        return raw, fingerprint

    def mcp_receipts(self) -> list[dict[str, str]]:
        if self.schema_version != 4:
            return []
        raw = decode_reference_json(self.messages_json, max_bytes=65536, max_depth=16)
        receipts = raw.get("mcp_receipts") if isinstance(raw, dict) else None
        if (
            not isinstance(receipts, list)
            or len(receipts) != self.completed_read_rounds
            or any(
                not isinstance(item, dict)
                or set(item) != {"call_id", "operation_name", "parameter_digest", "result_hash"}
                or any(not isinstance(value, str) or not value for value in item.values())
                for item in receipts
            )
        ):
            raise ValueError("reference_continuation_changed")
        return receipts

    def read_tools(self) -> list[str]:
        if self.schema_version == 5:
            raw = decode_reference_json(self.messages_json, max_bytes=65536, max_depth=16)
            names = raw.get("read_tools") if isinstance(raw, dict) else None
            if (
                not isinstance(names, list)
                or len(names) != self.completed_read_rounds
                or "attachment_read" not in names
                or any(not isinstance(name, str) or name not in {"skill_read", "attachment_read"} for name in names)
            ):
                raise ValueError("reference_continuation_changed")
            return names
        receipts = self.mcp_receipts()
        return [receipt["operation_name"] for receipt in receipts] if receipts else ["skill_read"] * self.completed_read_rounds

    def attachment_reads(self) -> list[tuple[str, str]]:
        if self.schema_version != 5:
            return []
        messages, _ = self._decode()
        reads = []
        for index, name in enumerate(self.read_tools()):
            if name != "attachment_read":
                continue
            call = messages[2 * index + 1]["tool_calls"][0]
            result = json.loads(messages[2 * index + 2]["content"])
            arguments = call["arguments"]
            if (
                not isinstance(arguments, dict)
                or set(arguments) != {"artifact_id"}
                or not isinstance(result, dict)
                or result.get("ok") is not True
                or not isinstance(result.get("artifact_id"), str)
                or result["artifact_id"] != arguments["artifact_id"]
                or not isinstance(result.get("content_hash"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", result["content_hash"]) is None
            ):
                raise ValueError("reference_continuation_changed")
            reads.append((result["artifact_id"], result["content_hash"]))
        return reads

    def matches_reads(self, invocations: list[ToolInvocation]) -> bool:
        """Use one read boundary at capture, primary return and Lead handoff."""
        receipts = self.mcp_receipts()
        names = self.read_tools()
        messages, _ = self._decode()
        return len(invocations) == self.completed_read_rounds and all(
            successful_read(inv, tool_name=names[index])
            and (
                names[index] != "attachment_read"
                or canonical_fingerprint(inv.arguments) == canonical_fingerprint(messages[2 * index + 1]["tool_calls"][0]["arguments"])
                and canonical_fingerprint(inv.result) == canonical_fingerprint(json.loads(messages[2 * index + 2]["content"]))
            )
            and (
                not receipts
                or canonical_fingerprint(inv.arguments) == receipts[index]["parameter_digest"]
                and canonical_fingerprint(inv.result) == receipts[index]["result_hash"]
            )
            for index, inv in enumerate(invocations)
        )

    def invocations(self) -> list[ToolInvocation]:
        """Rebuild private receipt state without dispatching retained Tools."""
        from bowhead_core.types import ToolInvocation

        messages = self.transcript()
        return [
            ToolInvocation(
                tool_name=assistant.tool_calls[0].name,
                arguments=assistant.tool_calls[0].arguments,
                result=json.loads(result.content),
            )
            for assistant, result in zip(messages[1::2], messages[2::2], strict=True)
        ]

    def transcript(self) -> list[Message]:
        raw, _ = self._decode()
        read_tools = self.read_tools()
        receipts = self.mcp_receipts()
        first = raw[0]
        if not isinstance(first, dict) or first.get("role") != "user" or first.get("images") != [] or not isinstance(first.get("content"), str):
            raise ValueError("reference_continuation_changed")
        messages = [Message(role="user", content=first["content"])]
        identities: set[str] = set()
        for round_index in range(self.completed_read_rounds + 1):
            assistant, result = raw[2 * round_index + 1 : 2 * round_index + 3]
            tool_name = read_tools[round_index] if round_index < self.completed_read_rounds else "subagent_return"
            if not isinstance(assistant, dict) or not isinstance(result, dict):
                raise ValueError("reference_continuation_changed")
            calls = assistant.get("tool_calls")
            if (
                assistant.get("role") != "assistant"
                or assistant.get("images") != []
                or not isinstance(assistant.get("content"), str)
                or not isinstance(calls, list)
                or len(calls) != 1
                or not isinstance(calls[0], dict)
                or set(calls[0]) != {"id", "name", "arguments"}
                or calls[0].get("name") != tool_name
                or not isinstance(calls[0].get("id"), str)
                or not 1 <= len(calls[0]["id"]) <= 256
                or calls[0]["id"] in identities
                or not isinstance(calls[0].get("arguments"), dict)
                or result.get("role") != "tool"
                or result.get("name") != tool_name
                or result.get("tool_call_id") != calls[0]["id"]
                or result.get("tool_calls") is not None
                or result.get("images") != []
                or not isinstance(result.get("content"), str)
            ):
                raise ValueError("reference_continuation_changed")
            identities.add(calls[0]["id"])
            if (
                receipts
                and round_index < self.completed_read_rounds
                and canonical_fingerprint(calls[0]["arguments"]) != receipts[round_index]["parameter_digest"]
            ):
                raise ValueError("reference_continuation_changed")
            messages.extend(
                [
                    Message(role="assistant", content=assistant["content"], tool_calls=[ToolCall(**calls[0])]),
                    Message(role="tool", content=result["content"], name=tool_name, tool_call_id=calls[0]["id"]),
                ]
            )
        return messages

    @field_validator("max_iterations", mode="before")
    @classmethod
    def _store_unlimited_as_none(cls, value: object) -> object:
        # An unlimited Team step limit is recorded as None, never as the internal loop bound.
        return persisted_step_limit(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @model_validator(mode="after")
    def validate_transcript(self):
        if (
            (self.max_iterations is not None and self.next_iteration >= self.max_iterations)
            or (self.schema_version < 3 and (self.schema_version == 1) != (self.repair_rounds == 0))
            or (self.schema_version in (4, 5) and self.repair_rounds)
        ):
            raise ValueError("reference_continuation_changed")
        self.transcript()
        self.attachment_reads()
        if self.output_recovery_json is not None:
            try:
                recovery = OutputRecovery.restore([Message(role="user", name=STATE_NAME, content=self.output_recovery_json)])
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError("reference_continuation_changed") from exc
            if recovery.attempts or recovery.context_reduced:
                raise ValueError("reference_continuation_changed")
        return self

    @classmethod
    def capture(
        cls,
        *,
        messages: list[Message],
        invocations: list[ToolInvocation],
        model_turn_count: int,
        binding: ExactAgentExecutionBinding | None,
        ctx: ToolExecutionContext,
        task: str,
        extra_instructions: str | None,
        subagent_id: str,
        profile: str,
        tool_deny: frozenset[str],
        effective_tools: frozenset[str],
        max_iterations: int,
        language_override: str | None = None,
        attachment_context: str | None = None,
        loop_budget: AgentLoopBudget | None = None,
    ) -> ReferenceReadQuestion | None:
        # Private runtime state is not another native read/question round.
        # Retain its exact receipt fingerprints separately; they cannot be
        # rebuilt from model-projected Tool text without changing recovery.
        notes = [message for message in messages if message.role == "user" and message.name == STATE_NAME]
        recovery = None
        recovery_json = None
        if notes:
            if len(notes) != 1 or notes[0].tool_calls or notes[0].images:
                return None
            try:
                recovery = OutputRecovery.restore(notes)
            except (ValueError, TypeError, KeyError):
                return None
            if recovery.attempts or recovery.context_reduced:
                return None
            recovery_json = notes[0].content
            messages = [message for message in messages if not (message.role == "user" and message.name == STATE_NAME)]
        repair_rounds = int(len(invocations) == model_turn_count - 1)
        reads = model_turn_count - repair_rounds - 1
        mcp_receipts = getattr(ctx, "reference_mcp_read_receipts", None)
        attachment_reads = any(inv.tool_name == "attachment_read" for inv in invocations[:-1])
        transcript = messages
        if repair_rounds:
            if (
                len(messages) < 5
                or messages[-4].role != "assistant"
                or messages[-4].tool_calls
                or messages[-3].role != "user"
                or messages[-3].content != READ_QUESTION_REPAIR_PROMPT
                or messages[-3].tool_calls
            ):
                return None
            transcript = [*messages[:-4], *messages[-2:]]
        if (
            binding is None
            or not 1 <= reads <= 8
            or len(messages) != 2 * model_turn_count + 1
            or len(invocations) != reads + 1
            or messages[0].role != "user"
            or messages[0].content != task
            or any(message.images for message in messages[1:])
            or (not mcp_receipts and (not ctx.reference_skill_required or ctx.reference_skill_access is None))
            or (mcp_receipts is not None and (len(mcp_receipts) != reads or repair_rounds))
            or (attachment_reads and (mcp_receipts is not None or repair_rounds))
            or any(
                not successful_read(
                    inv,
                    tool_name=mcp_receipts[index]["operation_name"] if mcp_receipts else inv.tool_name if attachment_reads else "skill_read",
                )
                or (attachment_reads and inv.tool_name not in {"skill_read", "attachment_read"})
                for index, inv in enumerate(invocations[:-1])
            )
            or invocations[-1].tool_name != "subagent_return"
            or invocations[-1].error is not None
            or invocations[-1].nested
            or not isinstance(invocations[-1].result, dict)
            or invocations[-1].result.get("ok") is not True
            or invocations[-1].result.get("__terminal") is not True
        ):
            return None
        try:
            fingerprint = _attachment_fingerprint(attachment_context, messages[0].images)
            raw = [asdict(message) for message in transcript]
            raw[0]["images"] = []
            payload = {"messages": raw, "attachment_fingerprint": fingerprint} if fingerprint else raw
            if mcp_receipts:
                payload = {"messages": raw, "attachment_fingerprint": fingerprint, "mcp_receipts": mcp_receipts}
            elif attachment_reads:
                payload = {"messages": raw, "attachment_fingerprint": fingerprint, "read_tools": [inv.tool_name for inv in invocations[:-1]]}
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            checkpoint = cls(
                schema_version=5 if attachment_reads else 4 if mcp_receipts else 3 if fingerprint else (2 if repair_rounds else 1),
                **{key: getattr(ctx, key) for key in ("turn_id", "session_id", "team_id", "user_id", "workspace_id")},
                execution_id=binding.execution_id,
                policy_fingerprint=binding.policy_fingerprint,
                capability_fingerprint=binding.capability_fingerprint,
                subagent_id=subagent_id,
                profile=profile,
                task_hash="sha256:" + hashlib.sha256(task.encode()).hexdigest(),
                extra_instructions=extra_instructions or "",
                language_override=language_override,
                tool_deny=tuple(sorted(tool_deny)),
                effective_tools=tuple(sorted(effective_tools)),
                max_iterations=max_iterations,
                loop_budget=loop_budget,
                output_recovery_json=recovery_json,
                completed_read_rounds=reads,
                repair_rounds=repair_rounds,
                messages_json=encoded,
            )
            if recovery is not None:
                retained_recovery = OutputRecovery()
                retained_recovery.observe(invocations)
                if retained_recovery != recovery:
                    return None
            return checkpoint if checkpoint.matches_reads(invocations[:-1]) else None
        except (ValueError, TypeError, RecursionError):
            return None

    def resume_messages(
        self,
        *,
        binding: ExactAgentExecutionBinding | None,
        ctx: ToolExecutionContext,
        task: str,
        extra_instructions: str | None,
        subagent_id: str,
        profile: str,
        tool_deny: frozenset[str],
        effective_tools: frozenset[str],
        max_iterations: int,
        answer: str | None,
        language_override: str | None = None,
        attachment_context: str | None = None,
        message_images: list[ImageRef] | None = None,
    ) -> list[Message]:
        if (
            binding is None
            or self.execution_id != binding.execution_id
            or self.policy_fingerprint != binding.policy_fingerprint
            or self.capability_fingerprint != binding.capability_fingerprint
            or any(getattr(ctx, key) != getattr(self, key) for key in ("turn_id", "session_id", "team_id", "user_id", "workspace_id"))
            or self.task_hash != "sha256:" + hashlib.sha256(task.encode()).hexdigest()
            or self.extra_instructions != (extra_instructions or "")
            or self.language_override != language_override
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
        _, fingerprint = self._decode()
        if fingerprint != _attachment_fingerprint(attachment_context, message_images):
            raise ValueError("reference_continuation_changed")
        messages = self.transcript()
        if messages[0].content != task:
            raise ValueError("reference_continuation_changed")
        messages[0].images = [ImageRef(base64=item.base64, mime=item.mime) for item in message_images or []]
        if self.output_recovery_json is not None:
            recovery = OutputRecovery.restore([Message(role="user", name=STATE_NAME, content=self.output_recovery_json)])
            recovery.sync(messages)
        return [*messages, Message(role="user", content=answer)]


def successful_read(invocation: ToolInvocation, *, tool_name: str = "skill_read") -> bool:
    result = invocation.result
    return (
        invocation.tool_name == tool_name
        and invocation.error is None
        and not invocation.nested
        and isinstance(result, dict)
        and result.get("ok") is not False
        and not result.get("error")
        and not result.get("error_kind")
        and result.get("status") != "approval_pending"
        and not result.get("clarification")
    )
