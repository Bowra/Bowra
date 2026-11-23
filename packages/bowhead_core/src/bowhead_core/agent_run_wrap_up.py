"""The wrap-up call a subagent makes when it stops at an agent run limit.

Design: docs/design/agent-run-limits-phase-2-tech-design.md §3. One model call with tools
declared but forbidden (``tool_choice="none"``) states what is done and what remains, so
the run returns partial work instead of discarding it.
"""

from __future__ import annotations

import re

StopReason = str  # "step_limit" | "time_limit" | "tool_call_limit" | "model_call_limit" | "no_progress" | "budget"

STOP_REASONS = ("step_limit", "time_limit", "tool_call_limit", "model_call_limit", "no_progress", "budget", "agent_stalled")
WRAP_UP_TIMEOUT_S = 90.0
WRAP_UP_MAX_OUTPUT_TOKENS = 2000
# Lead receives each section up to this length; persisted events use a shorter bound.
SECTION_MAX_CHARS = 4000

_REASON_TEXT = {
    "step_limit": "the effective model-step limit for one task",
    "agent_stalled": "the local execution stall timeout",
    "time_limit": "the time limit of this turn",
    "tool_call_limit": "the Team's tool-call limit for this turn",
    "model_call_limit": "the Team's model-call limit for this turn",
    "no_progress": "several steps in a row that produced no new results",
    "budget": "the available credit",
}


#: Which balance a `budget` stop ran out of (`TurnCreditGate.exhausted`): the fact, its subject and who restores it.
#: Observed execution: told only "the available credit", Lead dispatched again and its answer
#: offered to "continue" without saying the Team's credits had to be added first.
_CREDIT_FACTS = {
    "team_pool_exhausted": (
        "the Team's credit pool is used up, counting this turn's usage so far",
        "the pool",
        "a Team owner or administrator adds Team credits",
    ),
    "user_credit_exhausted": (
        "the user's own credit allowance is used up, counting this turn's usage so far",
        "the allowance",
        "a Team owner or administrator allocates more credits to the user",
    ),
}


def _credit_fact(stop_reason: StopReason, credit_denial: str | None) -> tuple[str, str, str] | None:
    return _CREDIT_FACTS.get(credit_denial or "") if stop_reason == "budget" else None


def wrap_up_instruction(stop_reason: StopReason, credit_denial: str | None = None) -> str:
    credit = _credit_fact(stop_reason, credit_denial)
    cause = f"stopped because {credit[0]}" if credit else f"stopped because you reached {_REASON_TEXT.get(stop_reason, 'a run limit')}"
    return (
        f"[System] You have {cause}. Do not call any tool. "
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


def partial_directive(stop_reason: StopReason, credit_denial: str | None = None) -> str:
    """What Lead must do with a partial result (phase 2 §4); a credit stop names its balance (phase 3 §4)."""
    credit = _credit_fact(stop_reason, credit_denial)
    if credit:
        fact, subject, action = credit
        return (
            f"This helper stopped because {fact} and returned partial work. While {subject} stays used up, every helper "
            "dispatched in this turn stops after its first step for the same reason. Tell the user the task is only partially "
            "complete and why, give the finished results from `done`, list what remains from `remaining`, and state the action "
            f"needed before it can continue: {action}. Continuation is available only if this result includes a supported "
            "checkpoint. Never present it as complete. Do not dispatch this task again in this turn."
        )
    reason = _REASON_TEXT.get(stop_reason)
    opening = f"This helper stopped at {reason} and returned partial work." if reason else "This helper returned partial work."
    return (
        f"{opening} Tell the user the task is only partially "
        "complete and why, give the finished results from `done`, list what remains from `remaining`, and say "
        "continuation is available only if this result includes a supported checkpoint. "
        "Never present it as complete. Do not dispatch this task again in this turn."
    )


def credit_dispatch_refusal(credit_denial: str) -> str:
    """Why `subagent_spawn` started no helper while the turn's credit is used up (phase 3 §4).

    Observed execution: after the first credit stop Lead dispatched again 6 of 8 times in replay even with
    the named balance; each such helper spent one more step before stopping.
    """
    credit = _CREDIT_FACTS.get(credit_denial) or _CREDIT_FACTS["team_pool_exhausted"]
    fact, subject, action = credit
    return (
        f"credit_exhausted: no helper was started because {fact}. While {subject} stays used up, every helper dispatch in "
        "this turn is refused. Answer from the results you have, say which part is unfinished and why, and state the action "
        f"needed before it can continue: {action}."
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
    "credit_dispatch_refusal",
    "ordinary_reason_for",
    "partial_directive",
    "parse_wrap_up",
    "wrap_up_instruction",
]
