"""Why an Agent Eval attempt ended without a verdict, in causes the Owner can act on.

An attempt without a verdict used to settle as ``agent_eval_attempt_failed`` whatever happened, and the
release check then reported every such attempt as one "environment problem". The cause decides what the
Owner does next (wait and test again, ask an admin to fix a model credential, look at a Tool), so the
attempt keeps it:

- the attempt's ``failure_code`` names the cause (``compare_case`` reads only the code);
- its deterministic evidence keeps the safe facts under ``failure`` (the category, the cause, the
  Tool's name, a diagnostic reference). Provider text never enters it: causes come from typed Router
  failures and fixed codes only.

The reference is the attempt's job id. The worker logs the same reference with the exception, so an
admin can find the underlying error from what the Owner copies.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

AttemptFailureCategory = Literal["model_unavailable", "agent_model", "tool", "platform", "cancelled", "data_connection"]

UNCLASSIFIED_FAILURE_CODE = "agent_eval_attempt_failed"
# No usable model for the run: none assigned to the role it runs as, or no team model at all.
MODEL_UNAVAILABLE_CODE = "agent_eval_model_unavailable"
_MODEL_UNAVAILABLE_CAUSES: Mapping[str, str] = {
    "team_model_role_unbound": "role_unassigned",
    "team_model_unconfigured": "unconfigured",
}
TOOL_FAILED_CODE = "agent_eval_tool_failed"
JUDGE_NOT_CONFIGURED_CODE = "agent_eval_judge_not_configured"
CANCELLED_CODE = "agent_eval_cancelled"
COMPARISON_CANCELLED_CODE = "agent_eval_comparison_cancelled"
# FEATURE_AGENT_EVAL_DATARC_READ_ONLY: the live Datarc route ended the attempt for an environment cause.
DATARC_UNAVAILABLE_CODE = "agent_eval_datarc_unavailable"
DATARC_REASON_PREFIX = "attempt_datarc_unavailable_"
# Kept in step with agent_eval_datarc.DATARC_CAUSES (that module imports this one's neighbours).
DATARC_CAUSES = frozenset(
    {
        "no_connection",
        "connection_unusable",
        "connection_rejected",
        "starter_not_owner",
        "owner_identity_unverified",
        "no_workspace",
        "owner_not_member",
        "datarc_unreachable",
        "datarc_busy",
        "datarc_timeout",
        "owner_permission_denied",
    }
)
AGENT_MODEL_FAILURE_PREFIX = "agent_eval_agent_model_"

# Typed model failure (Router projection or subagent result code) -> the cause the Owner reads.
_MODEL_CAUSES: Mapping[str, str] = {
    "llm_provider_rate_limited": "rate_limited",
    "llm_provider_auth_failed": "auth_failed",
    "llm_provider_timeout": "timeout",
    "llm_stream_deadline_exceeded": "timeout",
    "peer_llm_deadline_exceeded": "timeout",
    "llm_provider_unavailable": "unavailable",
    "llm_stream_interrupted": "unavailable",
    "llm_response_invalid": "unavailable",
    "llm_request_rejected": "request_rejected",
    "llm_context_overflow": "context_overflow",
}
AGENT_MODEL_CAUSES = frozenset(_MODEL_CAUSES.values())
AGENT_MODEL_FAILURE_CODES = frozenset(f"{AGENT_MODEL_FAILURE_PREFIX}{cause}" for cause in AGENT_MODEL_CAUSES)

# Attempts without a verdict that have their own release reasons (time limits, a required Tool the
# evaluation can't run); kept in step with agent_version_eval's constants.
_OWN_RULE_FAILURES = frozenset(
    {"agent_eval_attempt_timeout", "agent_eval_duration_budget_exceeded", "agent_eval_required_tool_not_run", "agent_eval_tool_not_run_in_eval"}
)

# How an attempt was cancelled while its comparison still finished (a stopped comparison has no result).
CancelCause = Literal["stopped", "authority_lost", "restart"]
CANCEL_CAUSES = frozenset({"stopped", "authority_lost", "restart"})


@dataclass(frozen=True, slots=True)
class AttemptFailure:
    failure_code: str
    category: AttemptFailureCategory
    cause: str | None = None
    tool: str | None = None
    # The model role that had no usable model (model_unavailable only).
    role: str | None = None

    def evidence(self, *, reference: str) -> dict[str, Any]:
        """The safe facts stored under the attempt's deterministic ``failure`` key."""

        document: dict[str, Any] = {"category": self.category, "reference": reference}
        if self.cause is not None:
            document["cause"] = self.cause
        if self.tool is not None:
            document["tool"] = self.tool
        if self.role is not None:
            document["role"] = self.role
        return {"failure": document}


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def model_unavailable_cause(exc: BaseException) -> tuple[str, str | None] | None:
    """(cause, role) when the run had no usable model at all, from typed facts only."""

    from bowhead_router.adapter import TeamModelUnconfiguredError
    from bowhead_router.router import NoBindingError

    for item in _chain(exc):
        if isinstance(item, NoBindingError):
            return "role_unassigned", item.agent_type
        if isinstance(item, TeamModelUnconfiguredError):
            return "unconfigured", None
        value = getattr(item, "llm_reason_code", None)
        if isinstance(value, str) and value in _MODEL_UNAVAILABLE_CAUSES:
            return _MODEL_UNAVAILABLE_CAUSES[value], getattr(item, "model_role", None)
    return None


def model_failure_cause(exc: BaseException) -> str | None:
    """The Agent's model call failed in a typed way: rate limit, credentials, timeout and so on."""

    from bowhead_core.llm_failure import project_llm_failure

    for item in _chain(exc):
        for attribute in ("llm_reason_code", "code"):
            value = getattr(item, attribute, None)
            if isinstance(value, str) and value in _MODEL_CAUSES:
                return _MODEL_CAUSES[value]
        projection = project_llm_failure(item)
        if projection is not None and projection.reason_code in _MODEL_CAUSES:
            return _MODEL_CAUSES[projection.reason_code]
    return None


def classify_attempt_exception(exc: BaseException, *, failed_tool: str | None) -> AttemptFailure:
    """Name what ended the attempt, from typed facts only; anything else is an unclassified platform error."""

    unavailable = model_unavailable_cause(exc)
    if unavailable is not None:
        cause, role = unavailable
        return AttemptFailure(MODEL_UNAVAILABLE_CODE, "model_unavailable", cause=cause, tool=None, role=role)
    cause = model_failure_cause(exc)
    if cause is not None:
        return AttemptFailure(f"{AGENT_MODEL_FAILURE_PREFIX}{cause}", "agent_model", cause=cause)
    if failed_tool:
        return AttemptFailure(TOOL_FAILED_CODE, "tool", tool=failed_tool[:120])
    return AttemptFailure(UNCLASSIFIED_FAILURE_CODE, "platform")


def incomplete_attempt_reason(status: str, failure_code: str | None, cause: str | None = None) -> str | None:
    """The release-check reason for one finished attempt without a verdict (None for a verdict).

    Time limits and Tools the evaluation can't run are reported by their own rules in ``compare_case``.
    """

    if status == "judge_unknown":
        return "attempt_judge_not_configured" if failure_code == JUDGE_NOT_CONFIGURED_CODE else "attempt_judge_unavailable"
    if status == "cancelled":
        return "attempt_cancelled"
    if status != "infra_failed" or failure_code in _OWN_RULE_FAILURES:
        return None
    if failure_code == MODEL_UNAVAILABLE_CODE:
        return "attempt_model_unavailable"
    if failure_code in AGENT_MODEL_FAILURE_CODES:
        return f"attempt_agent_model_{failure_code.removeprefix(AGENT_MODEL_FAILURE_PREFIX)}"
    if failure_code == TOOL_FAILED_CODE:
        return "attempt_tool_failed"
    if failure_code == DATARC_UNAVAILABLE_CODE:
        # The cause is part of the reason: each has its own action, and only some can be tested again.
        return f"{DATARC_REASON_PREFIX}{cause}" if cause in DATARC_CAUSES else "attempt_infrastructure_incomplete"
    return "attempt_infrastructure_incomplete"


INCOMPLETE_ATTEMPT_REASONS = frozenset(
    {
        "attempt_model_unavailable",
        "attempt_judge_unavailable",
        "attempt_judge_not_configured",
        "attempt_cancelled",
        "attempt_tool_failed",
        "attempt_infrastructure_incomplete",
        *(f"attempt_agent_model_{cause}" for cause in AGENT_MODEL_CAUSES),
        *(f"{DATARC_REASON_PREFIX}{cause}" for cause in DATARC_CAUSES),
    }
)


def attempt_failure_detail(
    *,
    status: str,
    failure_code: str | None,
    deterministic: Mapping[str, Any] | None,
    reference: str,
) -> dict[str, Any] | None:
    """One attempt's cause for the Owner's case list: category, cause, Tool and a diagnostic reference.

    Older attempts have no stored ``failure`` document; they read as an unclassified platform error with
    the attempt's reference, which is what they were.
    """

    stored = deterministic.get("failure") if isinstance(deterministic, Mapping) else None
    stored = stored if isinstance(stored, Mapping) else {}
    stored_cause = stored.get("cause") if isinstance(stored.get("cause"), str) else None
    reason = incomplete_attempt_reason(status, failure_code, stored_cause)
    if reason is None:
        return None
    if status == "judge_unknown":
        detail: dict[str, Any] = {"category": "judge", "cause": "not_configured" if failure_code == JUDGE_NOT_CONFIGURED_CODE else "unavailable"}
    elif status == "cancelled":
        cause = stored.get("cause")
        detail = {
            "category": "cancelled",
            "cause": cause if cause in CANCEL_CAUSES else ("stopped" if failure_code == COMPARISON_CANCELLED_CODE else None),
        }
    elif reason == "attempt_model_unavailable":
        role = stored.get("role")
        detail = {
            "category": "model_unavailable",
            "cause": stored.get("cause") if isinstance(stored.get("cause"), str) else None,
            "role": role if isinstance(role, str) else None,
        }
    elif reason.startswith("attempt_agent_model_"):
        detail = {"category": "agent_model", "cause": reason.removeprefix("attempt_agent_model_")}
    elif reason == "attempt_tool_failed":
        tool = stored.get("tool")
        detail = {"category": "tool", "tool": tool if isinstance(tool, str) and tool else None}
    elif reason.startswith(DATARC_REASON_PREFIX):
        tool = stored.get("tool")
        detail = {"category": "data_connection", "cause": stored_cause, "tool": tool if isinstance(tool, str) and tool else None}
        for key in ("workspace", "owner_name"):
            value = stored.get(key)
            if isinstance(value, str) and value:
                detail[key] = value
    else:
        detail = {"category": "platform"}
    stored_reference = stored.get("reference")
    detail["reference"] = stored_reference if isinstance(stored_reference, str) and stored_reference else reference
    detail["reason_code"] = reason
    return detail


__all__ = [
    "AGENT_MODEL_CAUSES",
    "AGENT_MODEL_FAILURE_CODES",
    "CANCELLED_CODE",
    "COMPARISON_CANCELLED_CODE",
    "DATARC_CAUSES",
    "DATARC_REASON_PREFIX",
    "DATARC_UNAVAILABLE_CODE",
    "INCOMPLETE_ATTEMPT_REASONS",
    "JUDGE_NOT_CONFIGURED_CODE",
    "MODEL_UNAVAILABLE_CODE",
    "TOOL_FAILED_CODE",
    "UNCLASSIFIED_FAILURE_CODE",
    "AttemptFailure",
    "attempt_failure_detail",
    "classify_attempt_exception",
    "incomplete_attempt_reason",
    "model_failure_cause",
    "model_unavailable_cause",
]
