"""A model-only projection over unchanged native execution records.

The Runner continues to capture and validate its original Tool transcript.
Committed replacements affect only outgoing model requests. A replacement can
be reapplied after an append or an original continuation, but never after an
edit to its covered prefix.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from bowhead_router.token_estimator import estimate_tokens
from bowhead_router.types import Message

from bowhead_core.context_compaction import CompactionPreparationError, CompactionSpan, balanced_boundaries, message_digest


@dataclass(frozen=True, slots=True)
class ContextReplacement:
    start: int
    end: int
    prefix_digest: str
    summary: Message

    @classmethod
    def committed(cls, source: list[Message], span: CompactionSpan, candidate: list[Message]) -> ContextReplacement:
        if message_digest(source) != span.source_digest or len(source) != span.source_count:
            raise CompactionPreparationError("context_source_changed")
        if (
            not 0 <= span.start < span.end <= len(source)
            or len(candidate) != len(source) - (span.end - span.start) + 1
            or candidate[: span.start] != source[: span.start]
            or candidate[span.start + 1 :] != source[span.end :]
        ):
            raise CompactionPreparationError("context_replacement_invalid")
        summary = candidate[span.start]
        if summary.role != "user" or summary.name != "context_summary" or summary.tool_calls or summary.tool_call_id or summary.images:
            raise CompactionPreparationError("context_summary_message_invalid")
        balanced_boundaries(candidate)
        return cls(span.start, span.end, message_digest(source[: span.end]), summary)

    def apply(self, messages: list[Message]) -> list[Message]:
        if len(messages) < self.end or message_digest(messages[: self.end]) != self.prefix_digest:
            raise CompactionPreparationError("context_projection_source_changed")
        return [*messages[: self.start], self.summary, *messages[self.end :]]

    def position(self, original: int) -> int:
        """Map a protected source position; replacing it is a contract error."""
        if self.start <= original < self.end:
            raise CompactionPreparationError("context_protected_message_replaced")
        return original if original < self.start else original - (self.end - self.start) + 1


class ContextView:
    """One execution/lane's committed projection, independent of its Runner."""

    def __init__(self) -> None:
        self._replacements: list[ContextReplacement] = []

    def project(self, messages: list[Message]) -> list[Message]:
        result = list(messages)
        for replacement in self._replacements:
            result = replacement.apply(result)
        return result

    def position(self, original: int) -> int:
        for replacement in self._replacements:
            original = replacement.position(original)
        return original

    def adopt(self, replacement: ContextReplacement) -> None:
        """The caller must have durably committed this exact replacement."""
        self._replacements.append(replacement)


def eligible_context_range(messages: list[Message], *, current_request: int, canonical_count: int) -> tuple[int, int]:
    """Protect active user input, newly returned Tools and volatile request data.

    Prefer the larger eligible region so a short retained history cannot
    prevent reduction of older completed transactions in a long execution.
    The current request always remains outside the selected region.
    """
    if not 0 <= current_request < canonical_count <= len(messages) or messages[current_request].role != "user":
        raise CompactionPreparationError(
            f"context_current_request_invalid: current={current_request}, canonical={canonical_count}, messages={len(messages)}"
        )
    start = current_request + 1
    boundaries = balanced_boundaries(messages)
    # The last native transaction has just arrived and must be consumed intact.
    end = max((value for value in boundaries if start <= value < canonical_count), default=start)
    # Later user messages may contain corrections, forced continuation or final
    # instructions. Keep the most recent such message and everything after it.
    users = [index for index in range(start, canonical_count) if messages[index].role == "user" and messages[index].name != "context_summary"]
    if users:
        end = min(end, users[-1])
    if current_request > 1 or (current_request == 1 and messages[0].name != "context_summary"):

        def size(region: list[Message]) -> int:
            return estimate_tokens(json.dumps([asdict(message) for message in region], ensure_ascii=False, separators=(",", ":")))

        if end <= start or size(messages[:current_request]) >= size(messages[start:end]):
            return 0, current_request
    return start, end
