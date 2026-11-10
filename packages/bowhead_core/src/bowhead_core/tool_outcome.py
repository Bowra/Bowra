"""Shared classification of Tool results before model and public projections.

Only declared execution envelopes are inspected. Business payloads are not
recursively searched, and this module never authorizes another physical call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from bowhead_core.scrub import scrub as _scrub


def _extract_result_ok(result: Any) -> bool | None:
    """Sprint chat-polish-r2 G1 (2026-05-26): inspect tool result for ok signal.

    Used to determine step.state when no Python exception fired. Many tools
    return cleanly with ok=False payload (python_sandbox docker fail, skill_*
    not found, bi_query reject) — these should surface as failure rows, not
    silent ✓.

    Returns:
        True / False if signal found; None if no determination (caller falls
        back to exception-based logic = treat as success).
    """
    if not isinstance(result, dict):
        return None
    execution = execution_result(result)
    if result.get("isError") is True or execution.get("ok") is False:
        return False
    exit_code = execution.get("exit_code")
    if type(exit_code) is int and exit_code != 0:
        return False
    # Explicit `ok` field — most idiomatic signal (used by python_sandbox,
    # subagent_return, skill_invoke).
    if "ok" in result:
        val = result["ok"]
        if isinstance(val, bool):
            return val
    # Explicit error_kind (used by bi_query, datarc_compute, canvas_op_failed).
    if result.get("error_kind"):
        return False
    # Top-level `error` field non-empty (skill_read / skill_list errors).
    err = result.get("error")
    if err and isinstance(err, (str, dict)):
        return False
    return None


def _extract_result_error(result: Any) -> tuple[str | None, str | None]:
    """Sprint chat-polish-r2 G1: pull (error_kind, error_message) from result.

    Surfaced into step_done.error payload so FE renders ⚠ row with context.
    """
    if not isinstance(result, dict):
        return (None, None)
    kind = result.get("error_kind")
    if not isinstance(kind, str):
        kind = None
    msg = result.get("error_message") or result.get("error") or result.get("message")
    if isinstance(msg, dict):
        msg = msg.get("message") or msg.get("detail") or str(msg)[:200]
    if msg and not isinstance(msg, str):
        msg = str(msg)
    if msg:
        msg = _scrub(msg)[:500]  # ④f: chokepoint for all result-dict errors → SSE step_done.error
    return (kind, msg)


def execution_result(result: Any) -> dict[str, Any]:
    """Unwrap only the declared script execution envelope, never business data."""
    if not isinstance(result, dict):
        return {}
    nested = result.get("result_json")
    if isinstance(nested, dict) and "exit_code" in nested and ("stdout" in nested or "stderr" in nested):
        return nested
    return result


EXPECTED_OUTCOME_ERROR_KINDS: frozenset[str] = frozenset(
    {
        # HITL clarification sentinel: must actually include a clarification structure (see _is_non_failure_result's
        # dict validation, aligned with the is_clarification_sentinel predicate).
        "needs_clarification",
        # ⚠ Semantic contract: this kind **may only** express a designed miss with candidates (the requested
        # target is outside the accessible set, and accessible/did_you_mean accompanies the result for the model to ask the user). Tools that
        # truly deny authorization (insufficient permission/invisible content) **must not** reuse this name: those are real failures, and reuse would
        # silently turn them into success here (review P2).
        "not_accessible",
        # feedback-0713 F4 (close a related gap documented in B3):
        # ① switch_context retriggered idempotently in the same turn: a confirmation card already appeared, so repeat calls intentionally
        #   emit no further cards (prevent prompt-injection flooding, tool_registry._switch_clar_emitted);
        #   waiting is not failure. Reserved exclusively for this idempotency guard.
        "pending_user_confirmation",
        # A repeated question or exhausted clarification policy returns an
        # expected refusal; the model should continue with existing answers.
        "clarification_refused_on_resume",
        # bi-workspace-list review c1: tool_list_workspaces has an empty accessible set; for a listing
        # tool, this **is the answer itself** (no accessible workspaces, same meaning as zero_workspaces
        # notice), not failure. Reserved for this read-only listing tool's empty-set outcome;
        # switch's same-named error branch does not carry this kind (the user cannot switch as requested; separate semantics).
        "no_accessible_workspaces",
        # bi-workspace-tour: an intended pause in repeated switching (guide users to switch once, then read).
        # Structural evidence checked below (actual switch_streak ≥ the shared production threshold).
        "switch_streak_paused",
        # Google Workspace reads (builtin_google_tool.EXPECTED_ERROR_KINDS): disconnected / reauthorization needed /
        # permission not granted / file access not granted / file missing; results include a way forward (connect, reauthorize, insert via Picker),
        # intended outcomes under the user's own authorization, not failures. Lead's final answer separately flags connection issues deterministically.
        "google_not_connected",
        "google_reauth_required",
        "google_scope_missing",
        "google_file_not_granted",
        "google_file_not_found",
    }
)


def _is_non_failure_result(result: Any) -> bool:
    """Classify expected outcomes consistently in live events and stored traces.

    Argument rejection is a failed attempt even when the model can correct it.
    It does not determine the final Turn status or prevent a later successful call.
    """
    kind, _ = _extract_result_error(result)
    if kind == "needs_clarification":
        # Review P2: align the exemption predicate with is_clarification_sentinel shape-2: a kind
        # claiming clarification without clarification.options cannot display a card, so it is a failure and must not become
        # success (which would hide the problem more than a red step).
        if not isinstance(result, dict):
            return False
        clar = result.get("clarification")
        return isinstance(clar, dict) and bool(clar.get("options"))
    # feedback-0713 F4 review security hardening: require structural evidence for both switch/resume guardrail kinds;
    # otherwise third-party MCP tools (results are untrusted external data; MCP live since 0.77.0)
    # could turn real failures into success with a magic error_kind string, hiding failures.
    # Same idea as needs_clarification's clarification.options validation: forging a kind
    # also requires forging its structure. (not_accessible was introduced in B3, claimed by review P2; changes need
    # B3 test updates, so address separately in a follow-up.)
    if kind == "pending_user_confirmation":
        return isinstance(result, dict) and isinstance(result.get("accessible"), list)
    if kind == "clarification_refused_on_resume":
        return isinstance(result, dict) and result.get("clarification_refused_on_resume") is True
    if kind == "no_accessible_workspaces":
        # bi-workspace-list review r2#1: structural evidence: a kind claiming an empty list must accompany a result
        # actually shaped as an empty list (total==0 + workspaces==[]). Otherwise third-party MCP tools
        # (untrusted external data) could turn real auth/network failures into green steps with a magic string,
        # following the same F4 hardening principle as the two kinds above.
        return isinstance(result, dict) and result.get("total") == 0 and result.get("workspaces") == []
    if kind == "switch_streak_paused":
        # bi-workspace-tour circuit breaker: an intended pause in repeated switching (switch once, then read),
        # not failure. Structural evidence = an accompanying actual counter that reached the threshold; share
        # one constant with production (review r3#3: separately hardcoded 3s could silently drift, turning all intended pauses red).
        from bowhead_core.tool_registry import SWITCH_STREAK_PAUSE_AT as _pause_at

        _stk = result.get("switch_streak") if isinstance(result, dict) else None
        return isinstance(_stk, int) and _stk >= _pause_at
    if isinstance(kind, str) and kind.startswith("google_") and kind in EXPECTED_OUTCOME_ERROR_KINDS:
        # Structural evidence: only Bowhead's own Google tools stamp this provider. A third-party
        # MCP tool returning the same error_kind stays a failure.
        return isinstance(result, dict) and result.get("provider") == "google_workspace"
    return kind in EXPECTED_OUTCOME_ERROR_KINDS


TOOL_FAILURE_CODES = frozenset(
    {
        "tool_arguments_invalid",
        "tool_resource_unavailable",
        "tool_code_failed",
        "tool_execution_timeout",
        "tool_execution_unknown",
        "tool_execution_failed",
        "tool_delivery_review_required",
    }
)
_ARGUMENT_KINDS = frozenset(
    {
        "tool_arguments_invalid",
        "invalid_args",
        "invalid_arguments",
        "schema_invalid",
        "invalid_chart_expression",
        "invalid_chart_change",
        "invalid_chart_display",
        "invalid_chart_binding",
        "invalid_deck_arguments",
        "invalid_parameters",
        "invalid_input",
        "validation_error",
    }
)
_RESOURCE_KINDS = frozenset({"artifact_not_found", "not_found", "resource_unavailable", "source_unavailable"})
_EXCEPTION_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9]*(?:Error|Exception)):\s*(.*)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """One factual classification, independent of presentation and retry execution."""

    state: Literal["success", "failure", "partial"]
    code: str | None = None
    retryable: bool = False
    field: str | None = None
    exception: str | None = None
    missing_key: str | None = None
    exit_code: int | None = None


def invocation_result(invocation: Any) -> Any:
    """Read the retained execution fact rather than its capped model projection."""
    full = getattr(invocation, "result_full", None)
    return invocation.result if full is None else full


def classify_tool_outcome(result: Any, error: str | None = None) -> ToolOutcome:
    """Classify an invocation without granting authority to retry its effects."""
    if error is None and _is_non_failure_result(result):
        return ToolOutcome("success")
    if error is None and _extract_result_ok(result) is not False:
        return ToolOutcome("partial" if isinstance(result, dict) and result.get("completion") == "partial" else "success")
    body = execution_result(result)
    kind, _ = _extract_result_error(result)
    exit_code = body.get("exit_code")
    exit_code = exit_code if type(exit_code) is int else None
    if exit_code not in (None, 0):
        exception = missing_key = None
        # Runners may retain a Python traceback in either declared output stream.
        # Prefer stderr, while preserving a bounded stdout-only failure summary.
        for output in (body.get("stderr"), body.get("stdout")):
            lines = _EXCEPTION_LINE.findall(output[-8192:]) if isinstance(output, str) else []
            if not lines:
                continue
            exception, detail = lines[-1]
            if exception == "KeyError":
                detail = detail.lstrip("\"'")
                match = re.match(r"""^([\w .()/-]{1,80})["']""", detail)
                if match:
                    missing_key = match[1]
            break
        return ToolOutcome("failure", "tool_code_failed", exception=exception, missing_key=missing_key, exit_code=exit_code)
    if kind in _ARGUMENT_KINDS:
        field = result.get("path") if isinstance(result, dict) else None
        return ToolOutcome("failure", "tool_arguments_invalid", field=field if isinstance(field, str) else None)
    if kind in _RESOURCE_KINDS:
        return ToolOutcome("failure", "tool_resource_unavailable")
    if kind == "tool_delivery_review_required":
        return ToolOutcome("failure", "tool_delivery_review_required")
    if kind in {"peer_execution_uncertain", "effect_outcome_unknown", "tool_outcome_unknown"}:
        return ToolOutcome("failure", "tool_execution_unknown")
    if kind in {"timeout", "execution_timeout", "tool_timeout"} or isinstance(error, str) and error.startswith(("TimeoutError:", "ReadTimeout:")):
        # Mutating calls keep their durable effect disposition; the UI does not
        # claim that repeating the operation is safe.
        return ToolOutcome("failure", "tool_execution_timeout")
    return ToolOutcome("failure", "tool_execution_failed", retryable=isinstance(result, dict) and result.get("retryable") is True)
