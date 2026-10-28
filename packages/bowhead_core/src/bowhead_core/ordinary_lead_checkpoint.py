"""Bounded provider-neutral Lead loop material, without execution authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

from bowhead_router.types import ImageRef, Message, ToolCall
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.chat_references import decode_reference_json
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.documents.routing import DocumentRoutingSnapshot
from bowhead_core.ordinary_lead_continuation import (
    OrdinaryContinuationUnavailable,
    canonical_json,
    content_hash,
    ordinary_execution_refusal,
    retained_lead_call,
)
from bowhead_core.types import ToolInvocation

if TYPE_CHECKING:
    from bowhead_core.language_context import LanguageContext

MAX_CHECKPOINT_BYTES = 256 * 1024


def current_turn_messages(messages: list[Message], history_sizes: list[int]) -> list[Message]:
    """Separate retained model context from this Turn's executable prefix."""
    from bowhead_core.context_assembly import turn_sizes_from_messages

    offset = sum(history_sizes)
    if (
        any(type(size) is not int or size <= 0 for size in history_sizes)
        or offset >= len(messages)
        or messages[offset].role != "user"
        or turn_sizes_from_messages(messages[:offset]) != history_sizes
    ):
        raise OrdinaryContinuationUnavailable()
    return messages[offset:]


OrdinaryHelperCompletionReason = Literal[
    "completed",
    "empty_answer",
    "iteration_limit",
    "loop_budget",
    "return_limit",
    "source_unverified",
    "model_failure",
    "model_unknown",
    "tool_unknown",
]


class OrdinaryHelperOutcome(BaseModel):
    """Native loop exit evidence; effect settlement and authorization are separate."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    status: Literal["completed", "failed"]
    reason: OrdinaryHelperCompletionReason
    failure_code: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")

    @classmethod
    def read(cls, value: object) -> OrdinaryHelperOutcome:
        try:
            if not isinstance(value, dict) or set(value) != {"status", "reason", "failure_code"}:
                raise ValueError("Invalid native outcome shape")
            return cls.model_validate(value)
        except ValueError as exc:
            raise OrdinaryContinuationUnavailable() from exc

    def require_result(self, ok: object) -> None:
        if type(ok) is not bool or ok != (self.status == "completed") or (self.reason == "completed") != ok or (self.failure_code is None) != ok:
            raise OrdinaryContinuationUnavailable()


class OrdinaryLeadCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract: Literal[
        "ordinary-lead-loop.v1",
        "ordinary-lead-loop.v2",
        "ordinary-lead-loop.v3",
        "ordinary-lead-loop.v4",
        "ordinary-lead-loop.v5",
        "ordinary-lead-loop.v6",
        "ordinary-lead-loop.v7",
        "ordinary-lead-loop.v8",
        "ordinary-lead-loop.v9",
        "ordinary-lead-loop.v10",
        "ordinary-lead-loop.v11",
    ] = "ordinary-lead-loop.v1"
    # Historical provenance only; new captures omit this field.
    document_routing: DocumentRoutingSnapshot | None = Field(default=None, repr=False)
    subagent_id: str = Field(min_length=1, max_length=128)
    system: str
    turn_tail: str
    language_tail: str
    attachment_context: str | None
    max_iterations: int = Field(ge=1, le=101)
    next_iteration: int = Field(ge=1, le=100)
    tool_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    preferred_provider: str | None
    preferred_model: str | None
    model_config_version: int = Field(ge=0)
    language_json: str
    messages_json: str = Field(repr=False)
    invocations_json: str = Field(repr=False)
    context_json: str = Field(repr=False)
    request_json: str = Field(default="{}", repr=False)
    question_call_id: str = Field(min_length=1, max_length=128)
    question_arguments_json: str = Field(repr=False)
    consumed_tokens: int = Field(ge=0)
    remaining_wallclock_s: float = Field(gt=0, allow_inf_nan=False)
    remaining_action_deadline_s: float = Field(ge=0, allow_inf_nan=False)
    final_bounce_count: int = Field(ge=0, le=2)
    source_bounce_used: bool
    overflow_retried: bool
    deadline_retried: bool
    history_sizes: list[int]
    material_json: str = Field(repr=False)
    collaboration: dict | None = Field(default=None, repr=False)

    def encode(self) -> bytes:
        if self.contract not in {
            "ordinary-lead-loop.v5",
            "ordinary-lead-loop.v6",
            "ordinary-lead-loop.v7",
            "ordinary-lead-loop.v8",
            "ordinary-lead-loop.v9",
            "ordinary-lead-loop.v10",
            "ordinary-lead-loop.v11",
        } and (self.contract == "ordinary-lead-loop.v4") != (self.document_routing is not None):
            raise OrdinaryContinuationUnavailable()
        if self.contract not in {"ordinary-lead-loop.v10", "ordinary-lead-loop.v11"} and (self.contract == "ordinary-lead-loop.v9") != (
            self.collaboration is not None
        ):
            raise OrdinaryContinuationUnavailable()
        if self.collaboration is not None:
            retained = self.collaboration
            if (
                set(retained) != {"execution_id", "frame_id", "cursor", "operation_revision", "operation_hash"}
                or any(not isinstance(retained[key], str) or not 1 <= len(retained[key]) <= 128 for key in ("execution_id", "frame_id"))
                or any(type(retained[key]) is not int for key in ("cursor", "operation_revision"))
                or not 0 <= retained["cursor"] <= retained["operation_revision"]
                or not isinstance(retained["operation_hash"], str)
                or len(retained["operation_hash"]) != 64
                or any(char not in "0123456789abcdef" for char in retained["operation_hash"])
            ):
                raise OrdinaryContinuationUnavailable()
        excluded = {key for key in ("document_routing", "collaboration") if getattr(self, key) is None}
        payload = canonical_json(self.model_dump(exclude=excluded)).encode()
        if len(payload) > MAX_CHECKPOINT_BYTES:
            raise OrdinaryContinuationUnavailable()
        if self.next_iteration >= self.max_iterations:
            raise OrdinaryContinuationUnavailable()
        for encoded in (self.context_json, self.request_json, self.material_json, self.question_arguments_json):
            if not isinstance(decode_reference_json(encoded, max_bytes=MAX_CHECKPOINT_BYTES, max_depth=20), dict):
                raise OrdinaryContinuationUnavailable()
        material = decode_reference_json(self.material_json, max_bytes=MAX_CHECKPOINT_BYTES, max_depth=20)
        if self.contract in {
            "ordinary-lead-loop.v5",
            "ordinary-lead-loop.v6",
            "ordinary-lead-loop.v7",
            "ordinary-lead-loop.v8",
            "ordinary-lead-loop.v9",
            "ordinary-lead-loop.v10",
            "ordinary-lead-loop.v11",
        }:
            self.helper_materials()
        elif "helpers" in material:
            raise OrdinaryContinuationUnavailable()
        elif self.contract in {"ordinary-lead-loop.v2", "ordinary-lead-loop.v3", "ordinary-lead-loop.v4"}:
            if not isinstance(material.get("tools"), list) or "canvas" not in material:
                raise OrdinaryContinuationUnavailable()
        if any(type(value) is not int or value < 0 for value in self.history_sizes):
            raise OrdinaryContinuationUnavailable()
        self.messages()
        self.invocations()
        return payload

    @classmethod
    def decode(cls, payload: bytes) -> OrdinaryLeadCheckpoint:
        value = decode_reference_json(payload, max_bytes=MAX_CHECKPOINT_BYTES, max_depth=24)
        if (
            isinstance(value, dict)
            and value.get("contract")
            not in {
                "ordinary-lead-loop.v4",
                "ordinary-lead-loop.v5",
                "ordinary-lead-loop.v6",
                "ordinary-lead-loop.v7",
                "ordinary-lead-loop.v8",
                "ordinary-lead-loop.v9",
                "ordinary-lead-loop.v10",
                "ordinary-lead-loop.v11",
            }
            and "document_routing" in value
        ):
            raise OrdinaryContinuationUnavailable()
        checkpoint = cls.model_validate(value)
        checkpoint.encode()
        return checkpoint

    def messages(self) -> list[Message]:
        """Restore exact call/result pairs ending at the sole unanswered question."""
        raw = decode_reference_json(self.messages_json, max_bytes=MAX_CHECKPOINT_BYTES, max_depth=20)
        if not isinstance(raw, list) or not 3 <= len(raw) <= 128:
            raise OrdinaryContinuationUnavailable()
        messages = []
        pending: dict[str, str] = {}
        seen: set[str] = set()
        boundaries, offset = set(), 0
        for size in self.history_sizes:
            offset += size
            boundaries.add(offset)
        for index, item in enumerate(raw):
            if index in boundaries:
                if pending:
                    raise OrdinaryContinuationUnavailable()
                # Provider call identities are local to their original Turn.
                seen.clear()
            if (
                not isinstance(item, dict)
                or set(item) != {"role", "content", "name", "tool_call_id", "tool_calls", "images"}
                or item["role"] not in {"user", "assistant", "tool"}
                or not isinstance(item["content"], str)
                or not isinstance(item["images"], list)
            ):
                raise OrdinaryContinuationUnavailable()
            calls = item["tool_calls"]
            if calls is not None:
                if item["role"] != "assistant" or not isinstance(calls, list) or not calls or pending:
                    raise OrdinaryContinuationUnavailable()
                for call in calls:
                    if (
                        not isinstance(call, dict)
                        or set(call) != {"id", "name", "arguments"}
                        or not isinstance(call["id"], str)
                        or not call["id"]
                        or call["id"] in seen
                        or not isinstance(call["name"], str)
                        or not isinstance(call["arguments"], dict)
                    ):
                        raise OrdinaryContinuationUnavailable()
                    seen.add(call["id"])
                    pending[call["id"]] = call["name"]
            elif item["role"] == "tool":
                if item["tool_call_id"] not in pending or item["name"] not in (None, pending[item["tool_call_id"]]):
                    raise OrdinaryContinuationUnavailable()
                del pending[item["tool_call_id"]]
            elif pending:
                raise OrdinaryContinuationUnavailable()
            images = []
            for image in item["images"]:
                if (
                    item["role"] != "user"
                    or not isinstance(image, dict)
                    or set(image) != {"base64", "mime"}
                    or image["mime"] not in {"image/png", "image/jpeg", "image/webp"}
                    or not isinstance(image["base64"], str)
                ):
                    raise OrdinaryContinuationUnavailable()
                images.append(ImageRef(**image))
            messages.append(Message(**{**item, "images": images, "tool_calls": [ToolCall(**call) for call in calls] if calls else None}))
        current_turn_messages(messages, self.history_sizes)
        last = messages[-1]
        if (
            pending != {self.question_call_id: "clarification_ask"}
            or last.role != "assistant"
            or len(last.tool_calls or []) != 1
            or canonical_json(last.tool_calls[0].arguments) != self.question_arguments_json
        ):
            raise OrdinaryContinuationUnavailable()
        return messages

    def invocations(self) -> list[ToolInvocation]:
        value = decode_reference_json(self.invocations_json, max_bytes=MAX_CHECKPOINT_BYTES, max_depth=20)

        def restore(items: object, *, depth: int = 0) -> list[ToolInvocation]:
            if not isinstance(items, list) or depth > 1 or len(items) > 64:
                raise OrdinaryContinuationUnavailable()
            result = []
            for item in items:
                if not isinstance(item, dict) or set(item) != set(ToolInvocation.__dataclass_fields__):
                    raise OrdinaryContinuationUnavailable()
                if not item["ended_at"] or not isinstance(item["arguments"], dict):
                    raise OrdinaryContinuationUnavailable()
                result.append(ToolInvocation(**{**item, "nested": restore(item["nested"], depth=depth + 1) if item["nested"] else []}))
            return result

        return restore(value)

    def resume_messages(self, answers: tuple[dict, ...]) -> list[Message]:
        return [
            *self.messages(),
            Message(role="tool", name="clarification_ask", tool_call_id=self.question_call_id, content=canonical_json({"answers": list(answers)})),
        ]

    def language_context(self) -> LanguageContext | None:
        """The paused Turn's exact language, including none when i18n was off.

        A resume must not substitute a default or a locale fallback: the
        restore check compares this sealed snapshot with the resumed context.
        """
        from bowhead_core.language_context import LanguageContext

        data = decode_reference_json(self.language_json, max_bytes=MAX_CHECKPOINT_BYTES, max_depth=2)
        if data is None:
            return None
        if not isinstance(data, dict):
            raise OrdinaryContinuationUnavailable()
        return LanguageContext.from_dict(data)

    def helper_materials(self) -> list[dict]:
        """Read ordered execution evidence without rewriting legacy envelopes."""
        material = decode_reference_json(self.material_json, max_bytes=MAX_CHECKPOINT_BYTES, max_depth=20)
        if not isinstance(material, dict):
            raise OrdinaryContinuationUnavailable()
        if self.contract not in {
            "ordinary-lead-loop.v5",
            "ordinary-lead-loop.v6",
            "ordinary-lead-loop.v7",
            "ordinary-lead-loop.v8",
            "ordinary-lead-loop.v9",
            "ordinary-lead-loop.v10",
            "ordinary-lead-loop.v11",
        }:
            if "helpers" in material:
                raise OrdinaryContinuationUnavailable()
            return [material]
        helpers = material.get("helpers")
        if (
            set(material)
            != {"helpers", "attachments", "canvas"}
            | ({"input_context"} if self.contract in {"ordinary-lead-loop.v10", "ordinary-lead-loop.v11"} else set())
            or not isinstance(helpers, list)
            or not (
                1
                if self.contract
                in {
                    "ordinary-lead-loop.v6",
                    "ordinary-lead-loop.v7",
                    "ordinary-lead-loop.v8",
                    "ordinary-lead-loop.v9",
                    "ordinary-lead-loop.v10",
                    "ordinary-lead-loop.v11",
                }
                else 2
            )
            <= len(helpers)
            <= 64
            or any(not isinstance(helper, dict) for helper in helpers)
        ):
            raise OrdinaryContinuationUnavailable()
        if self.contract in {"ordinary-lead-loop.v10", "ordinary-lead-loop.v11"}:
            from bowhead_core._ref_allowlist import RefAllowlist
            from bowhead_core.ordinary_lead_input_context import validate_shape

            context = decode_reference_json(self.context_json, max_bytes=MAX_CHECKPOINT_BYTES, max_depth=20)
            if self.contract != "ordinary-lead-loop.v11" or material["input_context"] is not None:
                validate_shape(material["input_context"], context)
            if "reference_inventory" not in context:
                raise OrdinaryContinuationUnavailable()
            if context["reference_inventory"] is not None:
                try:
                    RefAllowlist.from_snapshot(context["reference_inventory"])
                except ValueError as exc:
                    raise OrdinaryContinuationUnavailable() from exc
        invocations = self.invocations()
        if sum(1 + len(inv.nested) for inv in invocations) > 64:
            raise OrdinaryContinuationUnavailable()
        messages = current_turn_messages(self.messages(), self.history_sizes)
        calls = [call for message in messages for call in message.tool_calls or []]
        parallel_calls = [call for message in messages if len(message.tool_calls or []) > 1 for call in message.tool_calls]
        if (
            len(calls) != len(invocations) + 1
            or (self.contract == "ordinary-lead-loop.v11") != bool(parallel_calls)
            or any(call.name != "subagent_spawn" for call in parallel_calls)
        ):
            raise OrdinaryContinuationUnavailable()
        if any(not inv.call_id for inv in invocations) or len({inv.call_id for inv in invocations}) != len(invocations):
            raise OrdinaryContinuationUnavailable()
        for invocation, call in zip(invocations, calls[:-1], strict=True):
            # Spawn invocation IDs are trace nesting keys, distinct from the
            # provider call ID bound by dispatch_key. Neither may be rewritten.
            if (invocation.tool_name, invocation.arguments) != (call.name, call.arguments) or (
                invocation.tool_name != "subagent_spawn" and invocation.call_id != call.id
            ):
                raise OrdinaryContinuationUnavailable()
        if invocations and invocations[0].tool_name == "template_document_submit":
            lookup, *invocations = invocations
            if (
                lookup.arguments != {"action": "list_questions"}
                or lookup.error
                or lookup.nested
                or not isinstance(lookup.result, dict)
                or lookup.result.get("ok") is not True
                or lookup.result.get("error") is not None
                or lookup.result.get("questions") != []
            ):
                raise OrdinaryContinuationUnavailable()
        if self.collaboration is not None:
            invocations = [inv for inv in invocations if inv.tool_name != "agent_message"]
        if self.contract in {
            "ordinary-lead-loop.v7",
            "ordinary-lead-loop.v8",
            "ordinary-lead-loop.v9",
            "ordinary-lead-loop.v10",
            "ordinary-lead-loop.v11",
        }:
            filtered = []
            uncertain_seen = False
            for inv in invocations:
                if inv.result == ordinary_execution_refusal() and not inv.error and not inv.nested:
                    if not uncertain_seen:
                        raise OrdinaryContinuationUnavailable()
                    continue
                if uncertain_seen:
                    raise OrdinaryContinuationUnavailable()
                if inv.tool_name != "subagent_spawn" and self.contract != "ordinary-lead-loop.v7":
                    # Lead's own settled call: its result is a retained tool
                    # message and the resume never executes it again.
                    if not retained_lead_call(inv):
                        raise OrdinaryContinuationUnavailable()
                    continue
                filtered.append(inv)
                if len(filtered) <= len(helpers):
                    uncertain_seen |= OrdinaryHelperOutcome.read(helpers[len(filtered) - 1].get("outcome")).reason in {
                        "model_unknown",
                        "tool_unknown",
                    }
            invocations = filtered
        if len(invocations) != len(helpers):
            raise OrdinaryContinuationUnavailable()
        identities = {key: set() for key in ("execution_id", "dispatch_key", "subagent_id")}
        spawn_calls = [
            call for call in calls if call.name == "subagent_spawn" and any(helper.get("dispatch_key") == "call:" + call.id for helper in helpers)
        ]
        if len(spawn_calls) != len(helpers):
            raise OrdinaryContinuationUnavailable()
        for helper, invocation, call in zip(helpers, invocations, spawn_calls, strict=True):
            if not isinstance(helper, dict) or set(helper) != {
                *identities,
                "binding_hash",
                "tools",
                *(
                    {"outcome"}
                    if self.contract
                    in {
                        "ordinary-lead-loop.v6",
                        "ordinary-lead-loop.v7",
                        "ordinary-lead-loop.v8",
                        "ordinary-lead-loop.v9",
                        "ordinary-lead-loop.v10",
                        "ordinary-lead-loop.v11",
                    }
                    else set()
                ),
            }:
                raise OrdinaryContinuationUnavailable()
            for key, seen in identities.items():
                value = helper[key]
                if not isinstance(value, str) or not 1 <= len(value) <= 517 or value in seen:
                    raise OrdinaryContinuationUnavailable()
                seen.add(value)
            digest = helper["binding_hash"]
            if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise OrdinaryContinuationUnavailable()
            if (
                invocation.tool_name != "subagent_spawn"
                or invocation.error
                or not isinstance(invocation.result, dict)
                or (
                    self.contract
                    not in {
                        "ordinary-lead-loop.v6",
                        "ordinary-lead-loop.v7",
                        "ordinary-lead-loop.v8",
                        "ordinary-lead-loop.v9",
                        "ordinary-lead-loop.v10",
                        "ordinary-lead-loop.v11",
                    }
                    and invocation.result.get("ok") is not True
                )
                or invocation.result.get("subagent_id") != helper["subagent_id"]
                or helper["dispatch_key"] != "call:" + call.id
            ):
                raise OrdinaryContinuationUnavailable()
            if self.contract in {
                "ordinary-lead-loop.v6",
                "ordinary-lead-loop.v7",
                "ordinary-lead-loop.v8",
                "ordinary-lead-loop.v9",
                "ordinary-lead-loop.v10",
                "ordinary-lead-loop.v11",
            }:
                outcome = OrdinaryHelperOutcome.read(helper["outcome"])
                outcome.require_result(invocation.result.get("ok"))
                if self.contract not in {
                    "ordinary-lead-loop.v7",
                    "ordinary-lead-loop.v8",
                    "ordinary-lead-loop.v9",
                    "ordinary-lead-loop.v10",
                    "ordinary-lead-loop.v11",
                } and outcome.reason in {
                    "model_failure",
                    "model_unknown",
                    "tool_unknown",
                }:
                    raise OrdinaryContinuationUnavailable()
                if outcome.reason in {"model_unknown", "tool_unknown"} and invocation.result.get("execution_outcome") != "unknown":
                    raise OrdinaryContinuationUnavailable()
            children = invocation.nested
            if any(not child.call_id or child.nested for child in children) or len({child.call_id for child in children}) != len(children):
                raise OrdinaryContinuationUnavailable()
            tools = helper["tools"]
            children = [
                child
                for child in children
                if child.tool_name != "subagent_return" and not (self.collaboration is not None and child.tool_name == "agent_message")
            ]
            if not isinstance(tools, list) or len(tools) != len(children):
                raise OrdinaryContinuationUnavailable()
            for record, child in zip(tools, children, strict=True):
                if (
                    not isinstance(record, dict)
                    or record.get("kind")
                    not in {
                        "skill_read",
                        "skill_list",
                        "sandbox",
                        "rejected",
                        *(
                            {"attachment_read", "read_result"}
                            if self.contract in {"ordinary-lead-loop.v8", "ordinary-lead-loop.v9", "ordinary-lead-loop.v10", "ordinary-lead-loop.v11"}
                            else set()
                        ),
                        *(
                            {"uncertain_sandbox"}
                            if self.contract
                            in {
                                "ordinary-lead-loop.v7",
                                "ordinary-lead-loop.v8",
                                "ordinary-lead-loop.v9",
                                "ordinary-lead-loop.v10",
                                "ordinary-lead-loop.v11",
                            }
                            else set()
                        ),
                    }
                    or not isinstance(record.get("skills"), list)
                    or any(
                        record.get(key) != expected
                        for key, expected in {
                            "call_id": child.call_id,
                            "tool": child.tool_name,
                            "arguments_hash": content_hash(child.arguments),
                            "result_hash": content_hash(child.result),
                            "error": child.error,
                        }.items()
                    )
                ):
                    raise OrdinaryContinuationUnavailable()
                if record["kind"] == "attachment_read":
                    attachment = record.get("attachment")
                    if (
                        set(record) != {"kind", "skills", "attachment", "error", "tool", "arguments_hash", "result_hash", "call_id"}
                        or record["skills"] != []
                        or child.tool_name != "attachment_read"
                        or child.error is not None
                        or not isinstance(attachment, dict)
                        or set(attachment) != {"artifact_id", "content_hash", "metadata_hash"}
                        or child.arguments != {"artifact_id": attachment["artifact_id"]}
                        or not isinstance(child.result, dict)
                        or child.result.get("ok") is not True
                        or child.result.get("artifact_id") != attachment["artifact_id"]
                        or child.result.get("content_hash") != attachment["content_hash"]
                        or not isinstance(attachment["content_hash"], str)
                        or len(attachment["content_hash"]) != 71
                        or not attachment["content_hash"].startswith("sha256:")
                        or any(char not in "0123456789abcdef" for char in attachment["content_hash"][7:])
                        or not isinstance(attachment["metadata_hash"], str)
                        or len(attachment["metadata_hash"]) != 64
                        or any(char not in "0123456789abcdef" for char in attachment["metadata_hash"])
                    ):
                        raise OrdinaryContinuationUnavailable()
                if record["kind"] == "read_result":
                    from bowhead_core.ordinary_lead_material import retained_read_tool

                    if (
                        set(record) != {"kind", "skills", "error", "tool", "arguments_hash", "result_hash", "call_id"}
                        or record["skills"] != []
                        or not retained_read_tool(child.tool_name)
                        or not child.ended_at
                    ):
                        raise OrdinaryContinuationUnavailable()
                if record["kind"] == "uncertain_sandbox" and (
                    helper["outcome"]["reason"] != "tool_unknown"
                    or set(record)
                    != {"kind", "skills", "artifacts", "normalized_input_hash", "error", "tool", "arguments_hash", "result_hash", "call_id"}
                    or not isinstance(record["artifacts"], list)
                    or any(not isinstance(item, dict) or item.get("output") is not False for item in record["artifacts"])
                ):
                    raise OrdinaryContinuationUnavailable()
        parallel_keys = {"call:" + call.id for call in parallel_calls}
        if not parallel_keys <= identities["dispatch_key"] or any(
            helper["dispatch_key"] in parallel_keys and OrdinaryHelperOutcome.read(helper.get("outcome")).reason in {"model_unknown", "tool_unknown"}
            for helper in helpers
        ):
            raise OrdinaryContinuationUnavailable()
        return helpers

    def retained_call_ids(self) -> frozenset[str]:
        ids = {call.id for message in current_turn_messages(self.messages(), self.history_sizes) for call in message.tool_calls or []}
        for invocation in self.invocations():
            ids.add(invocation.call_id)
            ids.update(child.call_id for child in invocation.nested)
        return frozenset(ids)


@dataclass(frozen=True)
class OrdinaryLeadCapture:
    worker: DocumentTurnOwner
    checkpoint: OrdinaryLeadCheckpoint
    questions: list[dict]


@dataclass
class OrdinaryLeadRunState:
    """One run-local collector, shared by per-call context copies."""

    card_id: str | None = None
    checkpoint: OrdinaryLeadCheckpoint | None = None
    answers: tuple[dict, ...] = ()
    helper_bindings: list[dict] = field(default_factory=list)
    rejected_returns: dict[tuple[str, str], str] = field(default_factory=dict)
    tool_materials: dict[tuple[str, str], dict] = field(default_factory=dict)
    pending_skill_reads: dict[tuple[str, str], tuple] = field(default_factory=dict, repr=False)
    blocked_calls: dict[str, str] = field(default_factory=dict)
    final_bounce_count: dict[str, int] = field(default_factory=lambda: {"n": 0})
    captured: OrdinaryLeadCheckpoint | None = None
    consumed: bool = False
    model_config_version: int = 0
    request_context: dict = field(default_factory=dict)


def capture_message_prefix(messages: list[Message], *, question_call_id: str) -> str:
    """Remove only the original pause sentinel, never a completed Tool result."""
    if not messages or messages[-1].role != "tool" or messages[-1].tool_call_id != question_call_id:
        raise OrdinaryContinuationUnavailable()
    return canonical_json([asdict(message) for message in messages[:-1]])
