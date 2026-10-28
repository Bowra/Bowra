"""Interpret supported pause shapes without treating ordinary prose as a card."""

import json

import pytest
from bowhead_core.chat_reference_lead_continuation import _context
from bowhead_core.chat_reference_runtime import _requires_continuation
from bowhead_core.subagent import SubagentResult
from bowhead_core.types import ToolInvocation

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"_clarification_pending": True}, True),
        ({"error_kind": "needs_clarification", "clarification": {"options": ["A", "B"]}}, True),
        ({"_plan_propose_pending": True}, True),
        ({"status": "approval_pending", "approval_id": "approval-1"}, True),
        ({"status": "approval_pending", "approval_id": ""}, False),
        ({"status": "approval_unsupported"}, False),
        ({"message": "approval_pending"}, False),
        ({"error_kind": "needs_clarification", "clarification": {"options": []}}, False),
        ({"_clarification_pending": "true"}, False),
        ({"ok": True, "rows": [{"status": "approval_pending", "approval_id": "record-id"}]}, False),
    ],
)
def test_tool_outcomes_require_a_structured_continuation(value, expected):
    result = SubagentResult(
        "profile", "subagent", True, "Answer", tool_invocations=[ToolInvocation(tool_name="bi_query", arguments={}, result=value)]
    )
    assert _requires_continuation(result) is expected


@pytest.mark.parametrize("options", [["A", "B"], []])
def test_primary_bail_never_becomes_a_completed_answer(options):
    result = SubagentResult(
        "profile",
        "subagent",
        True,
        "Question",
        payload={
            "error": {
                "kind": "needs_user_input",
                "clarification_payload": {"questions": [{"question": "Which?", "options": options}]},
            }
        },
    )
    assert _requires_continuation(result)


def test_corrupt_lead_plan_is_a_failed_checkpoint_not_an_uncaught_recovery_error():
    row = {
        "resume_context_json": json.dumps(
            {
                "reference_continuation": {
                    "contract": "reference-lead-predispatch.v1",
                    "execution_id": "e1",
                    "source_hash": "hash",
                    "composition_digest": "digest",
                }
            }
        ),
        "plan_json": "[]",
        "primary_execution_id": "e1",
        "source_hash": "hash",
        "composition_digest": "digest",
    }
    with pytest.raises(ValueError, match="reference_continuation_changed"):
        _context(row)
