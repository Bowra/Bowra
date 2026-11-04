"""Shared Tool outcomes and the observed 116 failure structures."""

from __future__ import annotations

import pytest
from bowhead_core.published_diagnostics import publish_tool_failure
from bowhead_core.tool_outcome import _extract_result_ok, classify_tool_outcome


@pytest.mark.parametrize(
    "result",
    [
        {"ok": True, "result_json": {"exit_code": 1, "stdout": "retained output " * 2000 + "\nKeyError: 'profit'", "stderr": ""}},
        {"ok": True, "result_json": {"ok": False, "exit_code": 1, "stdout": "", "stderr": "KeyError: 'profit'"}},
        {"ok": False, "exit_code": 1, "stdout": "useful results " * 100, "stderr": "KeyError: 'profit'"},
    ],
)
def test_script_failure_retains_cause_despite_outer_success_or_partial_output(result: dict) -> None:
    outcome = classify_tool_outcome(result)
    assert _extract_result_ok(result) is False
    assert outcome.state == "failure"
    assert outcome.code == "tool_code_failed"
    assert outcome.exception == "KeyError"
    assert outcome.missing_key == "profit"
    assert not outcome.retryable
    diagnostic = publish_tool_failure(outcome)
    assert diagnostic.safe_detail == "exception=KeyError; missing_key=profit; exit_code=1"


def test_chart_validation_has_safe_field_and_requires_correction() -> None:
    result = {"error_kind": "invalid_chart_expression", "error": "sensitive provider text", "path": "/column_units"}
    outcome = classify_tool_outcome(result)
    assert outcome.code == "tool_arguments_invalid"
    assert publish_tool_failure(outcome).safe_detail == "field=column_units"
    assert not outcome.retryable


@pytest.mark.parametrize("key", ["/private/secret", "https://private.invalid", "<script>", "Authorization: Bearer secret"])
def test_unpublishable_missing_key_does_not_erase_finite_script_failure(key: str) -> None:
    outcome = classify_tool_outcome({"ok": False, "exit_code": 1, "stderr": f"KeyError: '{key}'"})
    diagnostic = publish_tool_failure(outcome)
    assert diagnostic is not None
    assert key not in (diagnostic.safe_detail or "")


def test_mcp_declared_error_and_exception_win_over_success() -> None:
    assert classify_tool_outcome({"ok": True, "isError": True}).state == "failure"
    assert classify_tool_outcome({"ok": True}, "TimeoutError: private detail").code == "tool_execution_timeout"
    assert classify_tool_outcome({"ok": True}, "unexpected_error: private detail").state == "failure"


def test_business_payload_is_not_recursively_interpreted_as_execution() -> None:
    result = {"records": [{"error": "a business column"}], "result_json": {"exit_code": 8}}
    assert classify_tool_outcome(result).state == "success"


def test_expected_human_pause_stays_outside_failures() -> None:
    result = {"ok": False, "error_kind": "needs_clarification", "clarification": {"options": ["A", "B"]}}
    assert classify_tool_outcome(result).state == "success"


def test_partial_delivery_does_not_hide_an_execution_failure() -> None:
    assert classify_tool_outcome({"ok": True, "completion": "partial"}).state == "partial"
    assert classify_tool_outcome({"ok": False, "completion": "partial", "error": "Write failed"}).state == "failure"
