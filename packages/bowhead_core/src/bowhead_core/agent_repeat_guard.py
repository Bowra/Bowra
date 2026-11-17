"""Repeated identical tool calls: notice at the 3rd and 5th, refuse from the 8th.

Design: docs/sprints/agent-repeat-guard-2026-11-16/tech-design.md §3.2–§3.4.

A call *repeats* the previous one when it uses the same tool with exactly the same
arguments, the previous call succeeded with the same result, and no other call ran in
between. Positions count model decisions: copies of the call in one decision count once,
and a decision that executes any other call starts a new chain with that call. Calls that
were refused before execution and calls to declared status-poll tools neither count nor
break the chain. Failed calls are not part of a chain; rule 8 and the failure limits
handle them. ``subagent_return`` is never refused.

Pure functions and a small state object; no I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from bowhead_core.agent_run_limits import REPEAT_NOTICE_AT, REPEAT_REFUSE_FROM

#: Result fields that change on every run of the same call; removed before results are compared.
VOLATILE_RESULT_KEYS = frozenset(
    {
        "artifact_id",
        "execution",
        "duration_ms",
        "elapsed_ms",
        "call_id",
        "request_id",
        "created_at",
        "updated_at",
    }
)
#: Artifact ids also appear inside text (for example the sandbox's "Complete stdout: Data artifact art_…").
_ARTIFACT_ID_IN_TEXT = re.compile(r"\bart_[0-9a-f]{6,}\b")
#: The terminal contract keeps its own failure limit and is never refused here.
NEVER_REFUSED_TOOLS = frozenset({"subagent_return"})


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def call_key(tool_name: str, arguments: Mapping[str, Any] | None) -> tuple[str, str]:
    """Same tool and exactly the same arguments, independent of key order and never truncated."""
    digest = hashlib.sha256(_canonical(dict(arguments or {})).encode("utf-8")).hexdigest()
    return tool_name, digest


def _project(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _project(item) for key, item in value.items() if str(key) not in VOLATILE_RESULT_KEYS}
    if isinstance(value, list | tuple):
        return [_project(item) for item in value]
    if isinstance(value, str):
        return _ARTIFACT_ID_IN_TEXT.sub("art_*", value)
    return value


def result_digest(result: Any, error: str | None = None) -> str:
    """Digest of a call's outcome without the values that change on every run."""
    return hashlib.sha256(_canonical({"error": error, "result": _project(result)}).encode("utf-8")).hexdigest()


def notice_text(tool_name: str, position: int | None) -> str | None:
    """The paragraph appended to the tool result at a notice position, else None."""
    if position is None or position not in REPEAT_NOTICE_AT:
        return None
    text = (
        f"[repeat notice] This is call {position} in a row of {tool_name} with exactly the same arguments, and it returned "
        "the same result as the previous ones. Running it again will not change anything. Use the result you already have "
        "and move on to the next step, or change the arguments if you need something different."
    )
    if position == max(REPEAT_NOTICE_AT):
        text += f" From the {_ordinal(REPEAT_REFUSE_FROM)} identical call in a row, the call is not run."
    return text


def refusal_text(tool_name: str, position: int, executed: int) -> str:
    """The tool message for a call refused before execution."""
    return (
        f"not_run: this is call {position} in a row of {tool_name} with exactly the same arguments; the previous {executed} "
        "returned the same result, shown above. It was not run again. Use that result and continue, change the arguments, "
        "or finish with what you have."
    )


def _ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


@dataclass
class RepeatChain:
    """The current chain of identical calls in one agent run."""

    key: tuple[str, str] | None = None
    result: str | None = None
    #: Executed identical calls with identical results, one per decision.
    count: int = 0
    last_decision: int | None = None
    #: Decisions whose identical call was refused after the chain reached the refusal point.
    refused: int = 0
    last_refused_decision: int | None = None

    def observe(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        *,
        result: str,
        failed: bool,
        decision: int,
        status_poll: bool = False,
    ) -> int | None:
        """Record one executed call; return its chain position when it starts or extends the chain.

        Returns None for a status poll (transparent), a failed call (it ends the chain) and a
        further copy of the same call in a decision that already counted it.
        """
        if status_poll:
            return None
        if failed:
            self._reset()
            return None
        key = call_key(tool_name, arguments)
        if key == self.key and result == self.result:
            if decision == self.last_decision:
                return None
            self.count += 1
            self.last_decision = decision
            return self.count
        self._reset()
        self.key, self.result, self.count, self.last_decision = key, result, 1, decision
        return 1

    def refusal_position(self, tool_name: str, arguments: Mapping[str, Any] | None, *, status_poll: bool = False) -> int | None:
        """The call's position when it must be refused before execution, else None."""
        if status_poll or tool_name in NEVER_REFUSED_TOOLS or self.count < REPEAT_REFUSE_FROM - 1:
            return None
        if call_key(tool_name, arguments) != self.key:
            return None
        return self.count + self.refused + 1

    def note_refused(self, decision: int) -> None:
        """Count one refused decision; further copies in the same decision do not count again."""
        if decision != self.last_refused_decision:
            self.refused += 1
            self.last_refused_decision = decision

    def _reset(self) -> None:
        self.key = self.result = self.last_decision = self.last_refused_decision = None
        self.count = self.refused = 0

    @classmethod
    def from_invocations(
        cls,
        invocations: Iterable[Any],
        *,
        is_failure: Callable[[Any], bool],
        is_status_poll: Callable[[str], bool],
    ) -> RepeatChain:
        """Rebuild the chain from a resumed run's retained invocations, one decision each."""
        chain = cls()
        for index, inv in enumerate(list(invocations)):
            name = str(getattr(inv, "tool_name", "") or "")
            chain.observe(
                name,
                getattr(inv, "arguments", None) or {},
                result=result_digest(getattr(inv, "result", None), getattr(inv, "error", None)),
                failed=is_failure(inv),
                decision=-1_000_000 + index,
                status_poll=is_status_poll(name),
            )
        return chain


__all__ = [
    "NEVER_REFUSED_TOOLS",
    "VOLATILE_RESULT_KEYS",
    "RepeatChain",
    "call_key",
    "notice_text",
    "refusal_text",
    "result_digest",
]
