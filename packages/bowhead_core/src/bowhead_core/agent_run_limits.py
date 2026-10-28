"""Team-wide agent run limits: turn time, subagent steps and tool calls per turn.

Design: docs/design/agent-run-limits.md and agent-run-limits-phase-1-tech-design.md.
The Team settings page is the only source of these values; one snapshot is resolved
per turn and shared by Lead and every subagent of that turn through a ContextVar.
``FEATURE_AGENT_RUN_LIMITS`` is on by default (user decision, 2026-10-21); an explicit
``0`` restores the earlier limits.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_KEY = "agent_run_limits"

DEFAULT_TURN_TIME_LIMIT_S = 1800
MIN_TURN_TIME_LIMIT_S = 600
MAX_TURN_TIME_LIMIT_S = 36_000
DEFAULT_TOOL_CALL_LIMIT = 300
MAX_STEP_LIMIT = 10_000
MAX_TOOL_CALL_LIMIT = 10_000
MIN_MODEL_CALL_TIME_LIMIT_S = 30
MAX_MODEL_CALL_TIME_LIMIT_S = MAX_TURN_TIME_LIMIT_S
# Internal loop bound for an unlimited step setting. It is never persisted or shown:
# records store None (see ``persisted_step_limit``) and the run is ended by time, tool
# calls, credits, no-progress or its own return.
UNBOUNDED_STEPS = 1_000_000
# No-progress criterion: nudge after this many consecutive steps without a new result,
# stop after this many.
NO_PROGRESS_NUDGE_STEPS = 8
NO_PROGRESS_STOP_STEPS = 12
# Subagents stop this long before the turn deadline so Lead can write the answer.
WRAP_UP_RESERVE_S = 300


def agent_run_limits_enabled() -> bool:
    """On unless the deployment sets FEATURE_AGENT_RUN_LIMITS to an explicit off value."""
    return os.environ.get("FEATURE_AGENT_RUN_LIMITS", "1").strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class AgentRunLimits:
    """One Team's limits as applied to a turn. ``step_limit=None`` means unlimited."""

    turn_time_limit_s: int = field(
        default=DEFAULT_TURN_TIME_LIMIT_S,
        metadata={
            "minimum": MIN_TURN_TIME_LIMIT_S,
            "maximum": MAX_TURN_TIME_LIMIT_S,
            "description": "Maximum duration of one turn in seconds, including all agents.",
        },
    )
    step_limit: int | None = field(
        default=None,
        metadata={
            "minimum": 1,
            "maximum": MAX_STEP_LIMIT,
            "nullable": True,
            "description": "Maximum steps per subagent; null means unlimited steps within the other turn limits.",
        },
    )
    tool_call_limit: int = field(
        default=DEFAULT_TOOL_CALL_LIMIT,
        metadata={"minimum": 1, "maximum": MAX_TOOL_CALL_LIMIT, "description": "Maximum tool calls shared by the agents in one turn."},
    )
    model_call_time_limit_s: int | None = field(
        default=None,
        metadata={
            "minimum": MIN_MODEL_CALL_TIME_LIMIT_S,
            "maximum": MAX_MODEL_CALL_TIME_LIMIT_S,
            "nullable": True,
            "description": "Maximum duration of a model call in seconds; null selects the automatic budget.",
        },
    )

    @property
    def wrap_up_reserve_s(self) -> int:
        return WRAP_UP_RESERVE_S

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


DEFAULT_LIMITS = AgentRunLimits()


class AgentRunLimitsInvalid(ValueError):
    """A submitted value is outside its allowed range."""

    def __init__(self, field: str, minimum: int, maximum: int) -> None:
        self.field = field
        self.minimum = minimum
        self.maximum = maximum
        super().__init__(f"{field} must be between {minimum} and {maximum}")


def _int_field(raw: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        raise AgentRunLimitsInvalid(field, minimum, maximum)
    return raw


def validate(payload: dict[str, Any]) -> AgentRunLimits:
    """Validate a settings submission. Raises ``AgentRunLimitsInvalid`` naming the field."""
    return AgentRunLimits(
        **{
            item.name: None
            if payload.get(item.name) is None and item.metadata.get("nullable")
            else _int_field(payload.get(item.name), item.name, item.metadata["minimum"], item.metadata["maximum"])
            for item in fields(AgentRunLimits)
        }
    )


def parse_stored(value: str | None) -> AgentRunLimits:
    """Read a stored value; anything missing or invalid yields the defaults."""
    if not value:
        return DEFAULT_LIMITS
    try:
        raw = json.loads(value)
        return validate(raw) if isinstance(raw, dict) else DEFAULT_LIMITS
    except (ValueError, TypeError):
        logger.warning("agent_run_limits.stored_value_invalid")
        return DEFAULT_LIMITS


async def load_team_limits(team_settings_store: Any, team_id: str | None) -> AgentRunLimits:
    """Resolve a Team's limits for one turn. Store errors fall back to defaults."""
    if team_settings_store is None or not team_id:
        return DEFAULT_LIMITS
    try:
        return parse_stored(await team_settings_store.get(team_id, SETTINGS_KEY))
    except Exception:
        logger.warning("agent_run_limits.load_failed team=%s", team_id, exc_info=True)
        return DEFAULT_LIMITS


def run_time_budget_s(limits: AgentRunLimits, *, is_lead: bool, turn_deadline: float | None, now: float) -> float:
    """Seconds a run may use: the turn's remaining time, minus the wrap-up reserve for subagents.

    Without a turn deadline (agent evaluation runs a subagent outside a Lead turn) the
    run gets the full turn time limit minus the reserve.
    """
    reserve = 0 if is_lead else limits.wrap_up_reserve_s
    if turn_deadline is None:
        return float(max(0, limits.turn_time_limit_s - reserve))
    return max(0.0, turn_deadline - now - reserve)


def model_call_budget_s(limits: AgentRunLimits, *, remaining_s: float) -> float:
    """Fit the configured call deadline inside the run's remaining time."""
    selected = limits.model_call_time_limit_s
    return max(0.0, min(float(selected) if selected is not None else remaining_s, remaining_s))


# Tools that never count against the per-turn tool-call limit, so an agent can always return.
TOOL_CALL_BUDGET_EXEMPT = frozenset({"subagent_return"})


@dataclass
class TurnToolCallBudget:
    """One counter per turn, shared by Lead and every subagent (all tools count alike)."""

    limit: int
    used: int = 0

    def admit(self, tool_name: str) -> bool:
        if tool_name in TOOL_CALL_BUDGET_EXEMPT:
            return True
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    def refusal(self) -> str:
        return (
            f"tool_call_limit: this turn has used its Team limit of {self.limit} tool calls. "
            "Do not call more tools. A helper returns what it has with subagent_return; "
            "Lead answers from the results so far and says which part is unfinished."
        )


_TOOL_CALL_BUDGET_CONTEXTVAR: contextvars.ContextVar[TurnToolCallBudget | None] = contextvars.ContextVar("turn_tool_call_budget", default=None)


def set_tool_call_budget(budget: TurnToolCallBudget | None) -> contextvars.Token:
    return _TOOL_CALL_BUDGET_CONTEXTVAR.set(budget)


def get_tool_call_budget() -> TurnToolCallBudget | None:
    return _TOOL_CALL_BUDGET_CONTEXTVAR.get()


def reset_tool_call_budget(token: contextvars.Token) -> None:
    _TOOL_CALL_BUDGET_CONTEXTVAR.reset(token)


class TurnCreditGate:
    """In-turn credit check shared by every agent of a turn (agent run limits, phase 3).

    ``read_balances`` returns (user balance, Team pool balance), either None when not
    tracked; ``accrued`` returns the credits this turn has used so far (settled only at
    the end of the turn). Balances are re-read at most every ``interval_s`` seconds.
    """

    def __init__(
        self,
        read_balances: Callable[[], Awaitable[tuple[int | None, int | None]]],
        accrued: Callable[[], int],
        *,
        interval_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read_balances
        self._accrued = accrued
        self._interval = interval_s
        self._clock = clock
        self._balances: tuple[int | None, int | None] | None = None
        self._read_at = 0.0

    async def exhausted(self) -> str | None:
        """``user_credit_exhausted`` / ``team_pool_exhausted`` once this turn has used the balance up."""
        now = self._clock()
        if self._balances is None or now - self._read_at >= self._interval:
            try:
                self._balances = await self._read()
                self._read_at = now
            except Exception:
                logger.warning("agent_run_limits.credit_read_failed", exc_info=True)
                return None
        user_balance, team_balance = self._balances
        spent = self._accrued()
        if user_balance is not None and user_balance - spent <= 0:
            return "user_credit_exhausted"
        if team_balance is not None and team_balance - spent <= 0:
            return "team_pool_exhausted"
        return None


_CREDIT_GATE_CONTEXTVAR: contextvars.ContextVar[TurnCreditGate | None] = contextvars.ContextVar("turn_credit_gate", default=None)


def set_credit_gate(gate: TurnCreditGate | None) -> contextvars.Token:
    return _CREDIT_GATE_CONTEXTVAR.set(gate)


def get_credit_gate() -> TurnCreditGate | None:
    return _CREDIT_GATE_CONTEXTVAR.get()


def reset_credit_gate(token: contextvars.Token) -> None:
    _CREDIT_GATE_CONTEXTVAR.reset(token)


def loop_step_limit(limits: AgentRunLimits) -> int:
    return limits.step_limit if limits.step_limit is not None else UNBOUNDED_STEPS


def persisted_step_limit(value: int | None) -> int | None:
    """The step limit as stored in continuation records: None for unlimited."""
    return None if value is None or value >= UNBOUNDED_STEPS else value


_RUN_LIMITS_CONTEXTVAR: contextvars.ContextVar[AgentRunLimits | None] = contextvars.ContextVar("agent_run_limits", default=None)


def set_run_limits(limits: AgentRunLimits | None) -> contextvars.Token:
    return _RUN_LIMITS_CONTEXTVAR.set(limits)


def get_run_limits() -> AgentRunLimits | None:
    """The current turn's limits, or None when the feature is off or no turn is active."""
    return _RUN_LIMITS_CONTEXTVAR.get()


def reset_run_limits(token: contextvars.Token) -> None:
    _RUN_LIMITS_CONTEXTVAR.reset(token)


__all__ = [
    "DEFAULT_LIMITS",
    "MAX_STEP_LIMIT",
    "MAX_TOOL_CALL_LIMIT",
    "MAX_TURN_TIME_LIMIT_S",
    "MIN_MODEL_CALL_TIME_LIMIT_S",
    "MAX_MODEL_CALL_TIME_LIMIT_S",
    "MIN_TURN_TIME_LIMIT_S",
    "NO_PROGRESS_NUDGE_STEPS",
    "NO_PROGRESS_STOP_STEPS",
    "SETTINGS_KEY",
    "TOOL_CALL_BUDGET_EXEMPT",
    "TurnCreditGate",
    "UNBOUNDED_STEPS",
    "WRAP_UP_RESERVE_S",
    "AgentRunLimits",
    "AgentRunLimitsInvalid",
    "TurnToolCallBudget",
    "agent_run_limits_enabled",
    "get_credit_gate",
    "get_run_limits",
    "get_tool_call_budget",
    "load_team_limits",
    "loop_step_limit",
    "model_call_budget_s",
    "parse_stored",
    "persisted_step_limit",
    "reset_credit_gate",
    "reset_run_limits",
    "reset_tool_call_budget",
    "run_time_budget_s",
    "set_credit_gate",
    "set_run_limits",
    "set_tool_call_budget",
    "validate",
]
