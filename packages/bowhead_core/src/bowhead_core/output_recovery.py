"""Bounded output corrections, persisted in the existing private transcript."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bowhead_router.runtime_notes import OUTPUT_RECOVERY_STATE_NAME
from bowhead_router.types import Message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bowhead_router.types import LLMResponse

    from bowhead_core.types import ToolInvocation

STATE_NAME = OUTPUT_RECOVERY_STATE_NAME
CORRECTION_LIMIT = 2
_STATUS_TOOLS = frozenset({"task_plan", "update_plan", "task_status", "agent_status", "subagent_status"})


def output_exhausted(response: LLMResponse) -> bool:
    diagnostics = getattr(response, "diagnostics", None)
    return getattr(response, "finish_reason", None) == "length" or bool(
        diagnostics and (diagnostics.incomplete_reason == "max_output_tokens" or diagnostics.upstream_finish_reason in {"length", "max_tokens"})
    )


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


@dataclass
class OutputRecovery:
    attempts: int = 0
    operations: set[str] = field(default_factory=set)
    results: set[str] = field(default_factory=set)
    instruction: str = ""
    context_reduced: bool = False

    @classmethod
    def restore(cls, messages: Sequence[Message]) -> OutputRecovery:
        """Only server-authored message identity can carry recovery authority."""
        for message in reversed(messages):
            if message.role != "user" or message.name != STATE_NAME:
                continue
            value = json.loads(message.content)
            attempts = value["attempts"]
            if type(attempts) is not int or not 0 <= attempts <= CORRECTION_LIMIT:
                raise ValueError("invalid output recovery checkpoint")
            for key in ("operations", "results"):
                if not isinstance(value[key], list) or any(not isinstance(item, str) or len(item) != 64 for item in value[key]):
                    raise ValueError("invalid output recovery checkpoint")
            if not isinstance(value["instruction"], str):
                raise ValueError("invalid output recovery instruction")
            reduced = value.get("context_reduced", False)
            if type(reduced) is not bool:
                raise ValueError("invalid output recovery checkpoint")
            return cls(attempts, set(value["operations"]), set(value["results"]), value["instruction"], reduced)
        return cls()

    def sync(self, messages: list[Message]) -> None:
        """Keep one durable marker; native Tool batches must already be settled."""
        messages[:] = [message for message in messages if not (message.role == "user" and message.name == STATE_NAME)]
        if not (self.attempts or self.operations):
            return
        messages.append(
            Message(
                role="user",
                name=STATE_NAME,
                content=json.dumps(
                    {
                        "attempts": self.attempts,
                        "operations": sorted(self.operations),
                        "results": sorted(self.results),
                        "instruction": self.instruction,
                        "context_reduced": self.context_reduced,
                    },
                    separators=(",", ":"),
                ),
            )
        )

    def observe(self, invocations: Sequence[ToolInvocation]) -> bool:
        """Failed, proposed, repeated and status-only work cannot buy corrections."""
        progress = False
        for invocation in invocations:
            result = invocation.result
            if invocation.error or invocation.tool_name in _STATUS_TOOLS or result is None:
                continue
            if isinstance(result, dict) and (
                result.get("error")
                or result.get("ok") is False
                or result.get("__skipped")
                or result.get("__pending")
                or result.get("status") in ("pending", "running", "queued", "unchanged", "skipped")
            ):
                continue
            operation = _digest([invocation.tool_name, invocation.arguments])
            receipt = _digest([invocation.tool_name, result])
            progress |= operation not in self.operations and receipt not in self.results
            self.operations.add(operation)
            self.results.add(receipt)
        if progress:
            self.attempts = 0
            self.context_reduced = False
            self.instruction = "New successful Tool receipts are available. Continue from those results; do not repeat completed actions."
        return progress

    def correct(self, response: LLMResponse, messages: list[Message]) -> bool:
        """Discard the entire incomplete native batch before any Tool dispatch."""
        if self.attempts >= CORRECTION_LIMIT:
            return False
        self.attempts += 1
        limit = response.diagnostics.output_token_limit if response.diagnostics else None
        allowance = (
            f"The previous physical request allowed {limit} output tokens."
            if limit is not None
            else "The server output allowance was not observed; do not assume it is unlimited."
        )
        self.instruction = (
            f"Your previous response hit the output token limit. Correction {self.attempts} of {CORRECTION_LIMIT}. "
            f"{allowance} The next request recalculates the allowance for its actual model and input and preserves configured limits. "
            "No Tool in the truncated response was executed, including any apparently complete calls in that batch. "
            "Use the actual earlier successful receipts; do not repeat successful writes or uncertain effects. "
            "Change strategy now: issue one smaller, complete Tool call with valid arguments, or give a complete concise answer. "
            "For long code or HTML, write and assemble smaller pieces only when the advertised Tools explicitly support it. "
            "Do not invent append/continuation arguments, repair partial JSON, or resend the same oversized payload. "
            "Reduce optional prose, styling and repeated data while keeping the requested findings and required evidence. "
            "If assembly is unavailable, return a smaller complete artifact or explain the specific remaining limitation. "
            + ("This is the final correction before the run stops with its existing results preserved." if self.attempts == CORRECTION_LIMIT else "")
        )
        # Partial native arguments are never added as executable assistant history.
        if response.content and not response.tool_calls:
            messages.append(Message(role="assistant", content=response.content))
        self.sync(messages)
        return True
