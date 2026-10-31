"""The wrap-up call a subagent makes when it stops at an agent run limit.

Design: docs/design/agent-run-limits-phase-2-tech-design.md §3. One model call with tools
declared but forbidden (``tool_choice="none"``) states what is done and what remains, so
the run returns partial work instead of discarding it.
"""

from __future__ import annotations

import re

StopReason = str  # "step_limit" | "time_limit" | "tool_call_limit" | "no_progress" | "budget"

STOP_REASONS = ("step_limit", "time_limit", "tool_call_limit", "no_progress", "budget", "agent_stalled")
WRAP_UP_TIMEOUT_S = 90.0
WRAP_UP_MAX_OUTPUT_TOKENS = 2000
# Lead receives each section up to this length; persisted events use a shorter bound.
SECTION_MAX_CHARS = 4000

_REASON_TEXT = {
    "step_limit": "the effective model-step limit for one task",
    "agent_stalled": "the local execution stall timeout",
    "time_limit": "the time limit of this turn",
    "tool_call_limit": "the Team's tool-call limit for this turn",
    "no_progress": "several steps in a row that produced no new results",
    "budget": "the available credit",
}


def wrap_up_instruction(stop_reason: StopReason) -> str:
    reason = _REASON_TEXT.get(stop_reason, "a run limit")
    return (
        f"[System] You have stopped because you reached {reason}. Do not call any tool. "
        "Reply in the language of the task with exactly two sections:\n"
        "Done: the verified results you obtained, keeping figures, names and file names exactly as produced. "
        "Write 'nothing verified yet' if there are none.\n"
        "Remaining: what is unfinished and the next concrete step to finish it."
    )


_REMAINING_HEADING = re.compile(r"(?im)^\s*(?:[#*>\-\s]*)\**\s*(remaining|not done|未完成|剩余|待完成)\s*\**\s*[:：]?")
_DONE_HEADING = re.compile(r"(?is)^\s*(?:[#*>\-\s]*)\**\s*(done|已完成|完成)\s*\**\s*[:：]?\s*")


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= SECTION_MAX_CHARS else text[: SECTION_MAX_CHARS - 1] + "…"


def parse_wrap_up(text: str) -> tuple[str | None, str | None]:
    """Split the wrap-up reply into (done, remaining). Text without headings goes to done."""
    text = (text or "").strip()
    if not text:
        return None, None
    match = _REMAINING_HEADING.search(text)
    done_part, remaining_part = (text[: match.start()], text[match.end() :]) if match else (text, "")
    done_part = _DONE_HEADING.sub("", done_part, count=1)
    return (_clip(done_part) or None), (_clip(remaining_part) or None)


EVENT_SECTION_MAX_CHARS = 600


def partial_directive(stop_reason: StopReason) -> str:
    """What Lead must do with a partial result (phase 2 §4)."""
    reason = _REASON_TEXT.get(stop_reason, "a run limit")
    return (
        f"This helper stopped at {reason} and returned partial work. Tell the user the task is only partially "
        "complete and why, give the finished results from `done`, list what remains from `remaining`, and say "
        "continuation is available only if this result includes a supported checkpoint. "
        "Never present it as complete. Do not dispatch this task again in this turn."
    )


def ordinary_reason_for(stop_reason: StopReason) -> str:
    """The persisted helper-outcome reason; existing values only, so older workers can read it."""
    return "loop_budget" if stop_reason in ("time_limit", "budget") else "iteration_limit"


__all__ = [
    "EVENT_SECTION_MAX_CHARS",
    "SECTION_MAX_CHARS",
    "STOP_REASONS",
    "WRAP_UP_MAX_OUTPUT_TOKENS",
    "WRAP_UP_TIMEOUT_S",
    "ordinary_reason_for",
    "partial_directive",
    "parse_wrap_up",
    "wrap_up_instruction",
]
