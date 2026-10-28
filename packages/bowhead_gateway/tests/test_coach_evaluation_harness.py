"""The evaluation harness keeps unknown usage and bounds physical attempts."""

from types import SimpleNamespace

import pytest
from bowhead_router.types import TokenUsage
from test_coach_model_tasks import TABLES, Attempts, original_receipt_read, outcome_checks

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mismatch", [None, "validation_only", "duplicate", "unknown", "different_error", "write"])
def test_rejected_submission_case_requires_an_actual_original_writer_rejection(mismatch):
    before = {table: [] for table in TABLES}
    after = dict(before)
    receipt = {"ok": False, "error": "proposal_invalid", "issues": [{"code": "subagent_tool_required"}]}
    submission = SimpleNamespace(
        tool_name="agent_authoring_propose", arguments={"patch": {"display_name": "Focused name edit"}}, error=None, result=receipt
    )
    calls = [SimpleNamespace(tool_name="skill_read", arguments={"name": "agent-authoring"}, error=None, result="Choose operations"), submission]
    if mismatch == "validation_only":
        submission.tool_name = "agent_authoring_validate"
        receipt.update(ok=True, validation_status="invalid")
        receipt.pop("error")
    elif mismatch == "duplicate":
        calls.append(submission)
    elif mismatch == "unknown":
        submission.error = "Response unknown"
    elif mismatch == "different_error":
        receipt["error"] = "draft_unavailable_or_changed"
    elif mismatch == "write":
        after["team_subagent_draft_suggestion"] = ["unexpected suggestion"]
    checks = outcome_checks(
        {"outcome": "draft_scope_blocked", "submission_required": True},
        before,
        after,
        SimpleNamespace(ok=True, tool_invocations=calls),
        {},
        None,
        (None, None, None, None),
        None,
    )
    assert all(checks.values()) is (mismatch is None)


@pytest.mark.parametrize("mismatch", [None, "missing", "retry", "success", "duplicate", "coach_started", "write"])
def test_admission_conflict_requires_one_original_rejection_without_coach_or_writes(mismatch):
    before = {table: [] for table in TABLES}
    after = dict(before)
    receipt = {"ok": False, "error": "draft_unavailable_or_changed", "retry": "new_user_turn"}
    dispatches = [receipt]
    result = None
    if mismatch == "missing":
        dispatches = []
    elif mismatch == "retry":
        receipt.pop("retry")
    elif mismatch == "success":
        receipt["ok"] = True
    elif mismatch == "duplicate":
        dispatches.append(receipt)
    elif mismatch == "coach_started":
        result = SimpleNamespace(ok=True)
    elif mismatch == "write":
        after["team_subagent_draft_suggestion"] = ["unexpected suggestion"]
    checks = outcome_checks({"outcome": "draft_conflict", "conflict_stage": "admission"}, before, after, result, {}, None, None, None, dispatches)
    assert all(checks.values()) is (mismatch is None)


@pytest.mark.parametrize("tool", ["agent_operation_get", "agent_skill_operation_get"])
@pytest.mark.parametrize("mismatch", [None, "identity", "recover", "failed", "write"])
def test_receipt_acceptance_requires_original_read_only_result(tool, mismatch):
    reference = {"kind": "skill_save", "operation_id": "csw_" + "a" * 64}
    call = SimpleNamespace(tool_name=tool, arguments={}, error=None, result={"ok": True, "operation_ref": reference})
    if mismatch == "identity":
        call.result["operation_ref"] = {**reference, "operation_id": "csw_" + "b" * 64}
    elif mismatch == "recover":
        call.arguments["recover"] = True
    elif mismatch == "failed":
        call.result["ok"] = False
    elif mismatch == "write":
        call.tool_name = "agent_skill_save"
    assert bool(original_receipt_read(call, reference)) is (mismatch is None)


async def test_attempt_limit_counts_unknown_and_non_applied_transports():
    observer = Attempts(2)
    spec = SimpleNamespace(provider="test", model="fixture", request_digest="a" * 64)
    first = await observer.begin(spec)
    await first.unknown(reason_code="response_lost")
    second = await observer.begin(spec)
    await second.confirmed_not_applied(evidence_code="rejected", evidence_digest="b" * 64)
    with pytest.raises(ValueError, match="limit"):
        await observer.begin(spec)
    assert [row["status"] for row in observer.rows] == ["unknown", "not_applied"]
    assert all(row["tokens"] is None for row in observer.rows)


async def test_provider_usage_is_recorded_without_transport_bodies():
    observer = Attempts(1)
    attempt = await observer.begin(SimpleNamespace(provider="test", model="fixture", request_digest="a" * 64), body={"private": "excluded"})
    await attempt.complete(usage=TokenUsage(input_tokens=100, output_tokens=20, cached_tokens=10), response_digest="b" * 64)
    assert observer.rows == [
        {
            "provider": "test",
            "model": "fixture",
            "request_digest": "a" * 64,
            "status": "complete",
            "tokens": {"input": 100, "output": 20, "cached": 10},
        }
    ]
