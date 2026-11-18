"""A model-only projection over unchanged native execution records.

The Runner continues to capture and validate its original Tool transcript.
Committed replacements affect only outgoing model requests. A replacement can
be reapplied after an append or an original continuation, but never after an
edit to its covered prefix.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bowhead_router.reasoning import message_record
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


# A user-selected Skill keeps its newest full copy per
# Skill verbatim across compaction, within these estimated-token budgets; older
# or over-budget copies are summarized like other history and `skill_read`
# can load the Skill again.
SKILL_KEEP_TOKENS_EACH = 8000
SKILL_KEEP_TOKENS_TOTAL = 24000
_SKILL_COPY = re.compile(r'<skill_content name="([^"]*)"')


def _kept_skill_positions(messages: list[Message], limit: int) -> list[int]:
    kept: list[int] = []
    seen: set[str] = set()
    total = 0
    for index in range(limit - 1, -1, -1):
        message = messages[index]
        if message.role != "user" or message.name != "skill_content" or not isinstance(message.content, str):
            continue
        match = _SKILL_COPY.search(message.content)
        if match is None or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        tokens = estimate_tokens(message.content)
        if tokens > SKILL_KEEP_TOKENS_EACH or total + tokens > SKILL_KEEP_TOKENS_TOTAL:
            continue
        total += tokens
        kept.append(index)
    return sorted(kept)


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
    # A user-selected Skill follows its request and stays verbatim with it; the
    # work after it remains eligible like any other completed transaction.
    while start < canonical_count and messages[start].role == "user" and messages[start].name == "skill_content":
        start += 1
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
            return estimate_tokens(json.dumps([message_record(message) for message in region], ensure_ascii=False, separators=(",", ":")))

        # History splits around the kept Skill copies; the largest remaining
        # stretch is the candidate (the whole history when none is kept).
        cuts = _kept_skill_positions(messages, current_request)
        stretches = [(low, high) for low, high in zip([0, *(cut + 1 for cut in cuts)], [*cuts, current_request], strict=True) if low < high]
        history = max(stretches, key=lambda stretch: size(messages[stretch[0] : stretch[1]]), default=None)
        if history is not None and (end <= start or size(messages[history[0] : history[1]]) >= size(messages[start:end])):
            return history
        if history is None and end <= start:
            return 0, 0
    return start, end
