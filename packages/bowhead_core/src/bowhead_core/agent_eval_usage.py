"""Per-attempt Agent Eval usage capture (tokens and successful model calls).

The counter observes the router usage seam (`usage_context(turn_usage_sink=...)`)
for one attempt, covering the subject and the Judge. It is metering only: it
moves no balance and does not install the durable physical-send contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from bowhead_router.usage import LLMUsageRecord

#: `usage_context` reason for Agent Eval attempts in `llm_usage_events`.
AGENT_EVAL_USAGE_REASON = "agent_eval"


@dataclass(frozen=True, slots=True)
class EvalAttemptUsage:
    """Spent usage of one settled attempt; cached input is reported beside input, not added to it."""

    tokens_input: int
    tokens_output: int
    tokens_cached: int
    model_call_count: int

    def __post_init__(self) -> None:
        for value in (self.tokens_input, self.tokens_output, self.tokens_cached, self.model_call_count):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("Agent Eval attempt usage must be non-negative integers")


class EvalAttemptUsageCounter:
    """Accumulate successful router usage records for one attempt.

    `record_usage` runs synchronously on the event loop thread, so plain
    integer accumulation is race-free across the attempt's tasks.
    """

    __slots__ = ("_calls", "_tokens_cached", "_tokens_input", "_tokens_output")

    def __init__(self) -> None:
        self._calls = 0
        self._tokens_input = 0
        self._tokens_output = 0
        self._tokens_cached = 0

    def observe(self, record: LLMUsageRecord) -> None:
        self._calls += 1
        self._tokens_input += max(0, int(record.tokens_input or 0))
        self._tokens_output += max(0, int(record.tokens_output or 0))
        self._tokens_cached += max(0, int(record.tokens_cached or 0))

    def snapshot(self) -> EvalAttemptUsage:
        return EvalAttemptUsage(
            tokens_input=self._tokens_input,
            tokens_output=self._tokens_output,
            tokens_cached=self._tokens_cached,
            model_call_count=self._calls,
        )
