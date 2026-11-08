"""Native corrections and unmet requirements preserve existing result contracts."""

from types import SimpleNamespace

import pytest
from bowhead_core.agents._lead_context import set_current_subagent_invocations
from bowhead_core.subagent import _prettify_input_preview
from bowhead_core.subagent_return_tool import consume_returned_payload, register_subagent_return_tool
from bowhead_core.tool_delivery_review import delivery_summary_input, failed_calls, review_terminal_payload, validate_delivery_review
from bowhead_core.tool_outcome import classify_tool_outcome
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.types import ToolInvocation


@pytest.mark.parametrize("tool", ["subagent_return", "review_delivery"])
def test_native_parser_metadata_is_not_a_public_step_label(tool):
    arguments = {"_args_parse_error": "Private model repair instruction", "_raw_args_preview": "Invalid JSON"}
    assert _prettify_input_preview(tool, arguments) == ""
    assert arguments["_args_parse_error"] == "Private model repair instruction"
    assert _prettify_input_preview("business_tool", {"question": "Required revenue"}) == "Required revenue"


@pytest.mark.parametrize(
    "arguments",
    [{"_args_parse_error": "Private parser hint"}, {"payload": {"delivery_surface": "report", "exec_summary": "Incomplete report schema"}}],
)
async def test_native_argument_correction_keeps_failure_without_business_failure(arguments):
    registry = ToolRegistry()
    register_subagent_return_tool(registry)
    context = ToolExecutionContext(workspace_id="ws_test", user_id="usr_test", member_uid="m_test", turn_id="t_test", datarc=None)
    token = set_current_subagent_invocations([])
    try:
        rejected = await registry.execute("subagent_return", arguments, context)
        rejected.call_id = "correction"
        outcome = classify_tool_outcome(rejected.result, rejected.error)
        assert outcome.state == "failure" and outcome.code == "tool_arguments_invalid"
        assert not failed_calls([rejected])
        assert consume_returned_payload(context) is None
        business = ToolInvocation("business_tool", {}, call_id="business", result=dict(rejected.result))
        assert set(failed_calls([business])) == {"business"}
    finally:
        token.var.reset(token)


@pytest.mark.parametrize("calls", [[], [ToolInvocation("python_sandbox", {}, call_id="calculate", result={"ok": True})]])
def test_missing_required_work_is_partial_without_failed_tools(calls):
    review = {"requirements": [{"requirement": "Required profit needs cost data", "status": "unfulfilled", "evidence_call_ids": []}], "issues": []}
    evaluated = validate_delivery_review(review, calls)
    assert evaluated.valid and not evaluated.complete
    payload = {"exec_summary": "Revenue retained; profit is missing.", "delivery_review": review}
    assert review_terminal_payload(payload, calls) is None
    assert payload["completion"] == "partial"
    assert payload["caveats"] == ["Required profit needs cost data"]
    assert not failed_calls(calls)


@pytest.mark.parametrize(
    "completion,reason,expected",
    [
        ("partial", "completed", "partial"),
        ("complete", "tool_unknown", "unverified"),
        ("complete", "model_unknown", "unverified"),
        ("complete", "completed", None),
    ],
)
def test_summary_preserves_explicit_partial_and_unknown_without_tool_failure(completion, reason, expected):
    result = SimpleNamespace(tool_invocations=[], completion=completion, ordinary_completion_reason=reason)
    assert delivery_summary_input(result) == ({"_delivery_completion": expected} if expected else {})


async def test_advertised_native_partial_survives_real_factory_and_registry():
    registry = ToolRegistry()
    register_subagent_return_tool(registry)
    fields = registry.to_schemas(["subagent_return"])[0].input_schema["properties"]["payload"]["properties"]
    assert fields["completion"]["enum"] == ["complete", "partial"]
    context = ToolExecutionContext(workspace_id="ws_test", user_id="usr_test", member_uid="m_test", turn_id="t_test", datarc=None)
    token = set_current_subagent_invocations([])
    try:
        for extra in (
            {"completion": "partial"},
            {
                "delivery_review": {
                    "requirements": [{"requirement": "Required profit", "status": "unfulfilled", "evidence_call_ids": []}],
                    "issues": [],
                }
            },
        ):
            result = await registry.execute(
                "subagent_return",
                {"payload": {"delivery_surface": "chat", "exec_summary": "Revenue is retained. Profit requires costs.", **extra}},
                context,
            )
            assert result.result["ok"] is True and result.result["completion"] == "partial"
            assert consume_returned_payload(context)["completion"] == "partial"
    finally:
        token.var.reset(token)


def test_optional_caveat_and_successful_empty_data_do_not_require_review():
    calls = [ToolInvocation("read", {}, call_id="empty", result={"ok": True, "rows": []})]
    payload = {"exec_summary": "No matching stores.", "caveats": ["Only active stores were requested."]}
    assert review_terminal_payload(payload, calls) is None
    assert "completion" not in payload
    assert validate_delivery_review(None, calls).complete


@pytest.mark.parametrize("required_profit_missing", [False, True])
def test_mixed_batch_can_be_covered_by_successful_read_without_requery(required_profit_missing):
    calls = [
        ToolInvocation(
            "bi_query",
            {"queries": ["Required revenue", "Optional refusal"]},
            call_id="mixed",
            result={"ok": True, "completion": "partial", "results": [{"ok": True, "rows": [[4200]]}, {"ok": False, "error_kind": "non_query_text"}]},
        )
    ]
    review = {
        "requirements": [{"requirement": "Deliver required revenue", "status": "fulfilled", "evidence_call_ids": ["read"]}],
        "issues": [
            {
                "call_id": "mixed",
                "resolution": "covered",
                "evidence_call_ids": ["read"],
                "explanation": "The original reader verified the required revenue; the optional rejected text was not a required result.",
            }
        ],
    }
    assert not validate_delivery_review(review, calls).valid
    calls.append(ToolInvocation("read_data_artifact", {"artifact_id": "art_revenue"}, call_id="read", result={"ok": True, "rows": [[4200]]}))
    if required_profit_missing:
        review["requirements"].append({"requirement": "Deliver required profit", "status": "unfulfilled", "evidence_call_ids": []})
    evaluated = validate_delivery_review(review, calls)
    assert evaluated.valid and evaluated.complete is not required_profit_missing
    payload = {"exec_summary": "Revenue verified from the original result.", "delivery_review": review}
    assert review_terminal_payload(payload, calls) is None
    assert payload["completion"] == ("partial" if required_profit_missing else "complete")
    assert classify_tool_outcome(calls[0].result).state == "partial"
    assert len([call for call in calls if call.tool_name == "bi_query"]) == 1


def test_complete_review_does_not_upgrade_explicit_partial():
    calls = [ToolInvocation("read", {}, call_id="read", result={"ok": True})]
    payload = {
        "completion": "partial",
        "delivery_review": {"requirements": [{"requirement": "Read revenue", "status": "fulfilled", "evidence_call_ids": ["read"]}], "issues": []},
    }
    assert review_terminal_payload(payload, calls) is None
    assert payload["completion"] == "partial"


def test_no_failure_summary_preserves_only_a_trusted_current_complete_review():
    result = SimpleNamespace(tool_invocations=[], completion="complete", ordinary_completion_reason="completed")
    assert delivery_summary_input(result) == {}
    result.reviewed_delivery_complete = "true"
    assert delivery_summary_input(result) == {}
    result.reviewed_delivery_complete = True
    assert delivery_summary_input(result) == {"_delivery_completion": "reviewed_complete"}
    result.completion = "partial"
    assert delivery_summary_input(result) == {"_delivery_completion": "partial"}


async def test_explicit_no_failure_review_rechecks_retained_evidence_access(monkeypatch):
    from bowhead_core.tool_delivery_review import review_evidence_availability

    async def admitted(context):
        return None

    async def revoked(context, artifact_id):
        raise PermissionError("Controlled access revocation")

    monkeypatch.setattr("bowhead_core.tool_delivery_review._require_evidence_scope", admitted)
    monkeypatch.setattr("bowhead_core.artifact_presentation.available_artifact", revoked)
    calls = [ToolInvocation("read_data_artifact", {}, call_id="read", result={"ok": True, "artifact_id": "art_revenue"})]
    review = {"requirements": [{"requirement": "Read revenue", "status": "fulfilled", "evidence_call_ids": ["read"]}], "issues": []}
    problem = await review_evidence_availability(review, calls, SimpleNamespace())
    assert problem and "read" in problem and "unavailable" in problem


@pytest.mark.parametrize("paused", [False, True])
def test_no_failure_partial_review_keeps_feature_and_human_pause_boundaries(monkeypatch, paused):
    payload = {
        "delivery_review": {"requirements": [{"requirement": "Required cost", "status": "unfulfilled", "evidence_call_ids": []}], "issues": []}
    }
    if paused:
        payload["error"] = {"kind": "needs_user_input"}
    else:
        monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "0")
    assert review_terminal_payload(payload, []) is None
    assert "completion" not in payload
