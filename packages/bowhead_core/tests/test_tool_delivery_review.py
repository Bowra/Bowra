"""Terminal review shares admission and never equates unrelated success with coverage."""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.agents._lead_context import set_current_subagent_invocations
from bowhead_core.tool_delivery_review import failed_calls, review_guidance, review_terminal_payload, validate_delivery_review
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSchema, ToolSpec
from bowhead_core.types import ToolInvocation


def _calls() -> list[ToolInvocation]:
    return [
        ToolInvocation("add_chart", {"title": "Store ranking"}, call_id="ranking", result={"error_kind": "invalid_chart_expression"}),
        ToolInvocation("add_chart", {"title": "Province totals"}, call_id="province", result={"section_id": "sec_province"}),
        ToolInvocation("python_sandbox", {}, call_id="table", result={"ok": True, "output_artifacts": [{"artifact_id": "art_table"}]}),
    ]


def _review(resolution: str = "covered", *, fulfilled: bool = True) -> dict[str, Any]:
    return {
        "requirements": [
            {
                "requirement": "Store performance",
                "status": "fulfilled" if fulfilled else "unfulfilled",
                "evidence_call_ids": ["table"] if fulfilled else [],
            }
        ],
        "issues": [
            {
                "call_id": "ranking",
                "resolution": resolution,
                "explanation": "The delivered table contains all store rankings.",
                "evidence_call_ids": ["table"] if resolution == "covered" else [],
            }
        ],
    }


def test_unrelated_chart_success_does_not_resolve_failed_ranking() -> None:
    assert set(failed_calls(_calls())) == {"ranking"}
    assert not validate_delivery_review(None, _calls()).complete


def test_equivalent_successful_table_can_cover_optional_failed_chart() -> None:
    review = validate_delivery_review(_review(), _calls())
    assert review.valid and review.complete


def test_missing_required_work_is_valid_partial_delivery() -> None:
    review = validate_delivery_review(_review("unfulfilled", fulfilled=False), _calls())
    assert review.valid and not review.complete


@pytest.mark.parametrize("evidence", [["invented"], ["ranking"], [], ["table", "invented"]])
def test_review_requires_successful_owned_call_evidence(evidence: list[str]) -> None:
    review = _review()
    review["issues"][0]["evidence_call_ids"] = evidence
    assert not validate_delivery_review(review, _calls()).valid


def test_recovery_must_follow_failure_and_use_the_original_tool() -> None:
    review = _review("recovered")
    review["issues"][0]["evidence_call_ids"] = ["table"]
    assert not validate_delivery_review(review, _calls()).valid
    review["issues"][0]["evidence_call_ids"] = ["province"]
    assert not validate_delivery_review(review, _calls()).valid
    calls = _calls() + [ToolInvocation("add_chart", {"title": "Store ranking"}, call_id="ranking_fixed", result={"section_id": "sec_ranking"})]
    review["issues"][0]["evidence_call_ids"] = ["ranking_fixed"]
    assert validate_delivery_review(review, calls).valid
    assert not validate_delivery_review(review, list(reversed(calls))).valid


@pytest.mark.parametrize("caveats", [[], ["The source covers open stores only."]])
def test_verified_recovery_does_not_append_historical_failure_warning(monkeypatch: pytest.MonkeyPatch, caveats: list[str]) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    calls = _calls() + [ToolInvocation("add_chart", {"title": "Store ranking"}, call_id="ranking_fixed", result={"section_id": "sec_ranking"})]
    review = _review("recovered")
    review["issues"][0]["evidence_call_ids"] = ["ranking_fixed"]
    payload = {"exec_summary": "The store performance results were delivered.", "delivery_review": review, "caveats": list(caveats)}

    assert review_terminal_payload(payload, calls) is None
    assert payload["completion"] == "complete"
    assert payload["caveats"] == caveats
    assert set(failed_calls(calls)) == {"ranking"}


def test_correction_feedback_supplies_usable_current_run_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    calls = _calls()
    review = _review()
    for item in [*review["requirements"], *review["issues"]]:
        item["evidence_call_ids"] = ["25"]
    caveats = ["The rankings are delivered as a table."]
    payload = {"exec_summary": "The equivalent ranking table was delivered.", "delivery_review": review, "caveats": list(caveats)}
    refused = review_terminal_payload(payload, calls)
    assert refused is not None
    context = refused["review_context"]
    assert context["successful_calls"] == [{"call_id": "province", "tool": "add_chart"}, {"call_id": "table", "tool": "python_sandbox"}]
    assert context["failed_calls"][0]["call_id"] == "ranking"
    assert not context["truncated"]
    assert refused["error"].count("fulfilled coverage requires actual successful evidence_call_ids") == 1
    calls.append(ToolInvocation("finish", {}, call_id="correction", result=refused))
    table_call = next(item["call_id"] for item in context["successful_calls"] if item["tool"] == "python_sandbox")
    for item in [*review["requirements"], *review["issues"]]:
        item["evidence_call_ids"] = [table_call]
    assert review_terminal_payload(payload, calls) is None
    assert payload["completion"] == "complete"
    assert payload["caveats"] == caveats
    assert payload["delivery_review"]["issues"] == review["issues"]
    guidance = review_guidance(calls)
    assert '"call_id": "table"' in guidance
    assert '"call_id": "correction"' not in guidance
    assert "art_table" not in guidance


def test_correction_feedback_bounds_large_runs_without_trusting_omitted_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    calls = _calls()
    calls.extend(ToolInvocation("lookup", {}, call_id=f"extra_{i}", result={"ok": True}) for i in range(2001))
    refused = review_terminal_payload({}, calls)
    assert refused is not None
    assert refused["review_context"]["truncated"]
    assert len(refused["review_context"]["successful_calls"]) == 2000
    review = {"requirements": [{"requirement": "Ranking", "status": "fulfilled", "evidence_call_ids": ["extra_2000"]}], "issues": []}
    assert "every failed call must be accounted for" in validate_delivery_review(review, calls).problems


@pytest.mark.parametrize("retain_explicit_partial", [False, True])
@pytest.mark.parametrize("review_tool", [None, "review_delivery"])
def test_post_read_correction_rechecks_scope_without_clearing_explicit_partial(retain_explicit_partial: bool, review_tool: str | None) -> None:
    calls = [
        ToolInvocation("bi_query", {}, call_id="mixed", result={"ok": True, "completion": "partial"}),
        ToolInvocation("python_exec", {}, call_id="read", result={"ok": True, "stdout": "revenue=4200"}),
    ]
    review = {
        "requirements": [
            {"requirement": "Deliver required revenue", "status": "fulfilled", "evidence_call_ids": ["read"]},
            {"requirement": "Report optional rejection", "status": "fulfilled", "evidence_call_ids": []},
        ],
        "issues": [
            {
                "call_id": "mixed",
                "resolution": "covered",
                "evidence_call_ids": ["read"],
                "explanation": "The original reader verified revenue; the other observation is optional.",
            }
        ],
    }
    payload = {"completion": "partial", "delivery_review": review}
    refusal = review_terminal_payload(payload, calls, review_tool=review_tool)
    assert refusal is not None
    assert refusal["review_context"]["invalid_evidence_fields"] == ["requirements[1].evidence_call_ids"]
    assert refusal["review_context"]["successful_calls"] == [{"call_id": "read", "tool": "python_exec"}]
    assert refusal["review_context"]["failed_calls"][0]["state"] == "partial"
    problem, guidance = refusal["error"].split("\n", 1)
    assert "successful evidence_call_ids" in problem
    assert guidance.startswith("Fix requirement scope before filling evidence IDs.")
    assert "never an extra success or negative-test requirement" in guidance
    assert "known state=failure, never partial or unknown" in guidance
    assert "Preserve genuine unmet requirements" in guidance
    assert "Reassess explicit completion after correction" in guidance
    assert ("Call review_delivery alone" in guidance) is (review_tool is not None)
    assert len(review["requirements"]) == 2 and payload["completion"] == "partial"
    calls.append(ToolInvocation("subagent_return", {}, call_id="correction", result=refusal))
    # The model owns scope correction. Native feedback must not prune requirements.
    review["requirements"].pop()
    if not retain_explicit_partial:
        payload.pop("completion")
    assert validate_delivery_review(review, calls).complete
    assert review_terminal_payload(payload, calls) is None
    assert payload["completion"] == ("partial" if retain_explicit_partial else "complete")


def test_expected_failure_observations_do_not_become_positive_delivery_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    calls = _calls()
    review = _review()
    final_outcome = review["requirements"][0]
    review["requirements"].extend(
        [
            {"requirement": "Observe the deliberately failed chart attempt", "status": "fulfilled", "evidence_call_ids": ["ranking"]},
            {"requirement": "Explain the failure and the delivered table", "status": "fulfilled", "evidence_call_ids": ["ranking", "table"]},
        ]
    )
    payload = {"delivery_review": review}
    refused = review_terminal_payload(payload, calls)
    assert refused is not None
    assert refused["review_context"]["invalid_evidence_fields"] == ["requirements[1].evidence_call_ids", "requirements[2].evidence_call_ids"]
    assert len(review["requirements"]) == 3
    assert review["issues"][0]["call_id"] == "ranking"
    assert refused["review_context"]["failed_calls"][0]["state"] == "failure"
    review["requirements"] = [final_outcome]
    assert review_terminal_payload(payload, calls) is None
    assert payload["completion"] == "complete"
    assert not payload.get("caveats")


async def test_accepted_lead_review_directs_a_quiet_reply_after_covered_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.tool_delivery_review import LeadDeliveryReview

    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    calls = _calls()
    calls[-1].result = {"ok": True, "rows": [["Store 1", 1]], "columns": ["Store", "Revenue"]}
    review = LeadDeliveryReview()
    registry = ToolRegistry()
    registry.register(review.tool_spec())
    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="member", user_id="user", turn_id="turn", datarc=None, role="lead")
    token = set_current_subagent_invocations(calls)
    try:
        receipt = await registry.execute("review_delivery", {"payload": {"delivery_review": _review()}}, ctx)
        assert receipt.result["reviewed_completion"] == "complete"
        instruction = receipt.result["instruction"]
        assert "Omit delivery-review narration and tool failure tallies" in instruction
        assert "'no calls failed'" in instruction
        assert "Report actual failures when the user asks for diagnostics" in instruction
        payload = await review.current(calls, ctx)
        assert payload is not None
        assert payload["delivery_review"]["issues"][0]["call_id"] == "ranking"
        assert not payload.get("caveats")
    finally:
        token.var.reset(token)


def test_partial_delivery_feedback_preserves_the_recorded_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    calls = [ToolInvocation("worker", {}, call_id="partial_worker", result={"ok": True, "completion": "partial"})]
    refused = review_terminal_payload({}, calls)
    assert refused is not None
    assert refused["review_context"]["failed_calls"][0]["state"] == "partial"


def _negative_test_review() -> dict[str, Any]:
    review = _review()
    review["requirements"].append(
        {
            "requirement": "Verify the explicitly requested invalid chart input is rejected",
            "expected_outcome": "failure",
            "status": "fulfilled",
            "evidence_call_ids": ["ranking"],
        }
    )
    review["issues"][0].update(
        resolution="expected",
        evidence_call_ids=["ranking"],
        explanation="The user requested one invalid-input attempt; it failed as requested. The ranking table was delivered separately.",
    )
    return review


def test_explicit_negative_test_and_delivered_ranking_can_complete_without_hiding_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    calls = _calls()
    review = _negative_test_review()
    payload = {"delivery_review": review}
    assert validate_delivery_review(review, calls).complete
    assert review_terminal_payload(payload, calls) is None
    assert payload["completion"] == "complete"
    assert payload["caveats"] == [review["issues"][0]["explanation"]]
    assert set(failed_calls(calls)) == {"ranking"}
    review["requirements"].append({"requirement": "Required profit analysis", "status": "unfulfilled", "evidence_call_ids": []})
    assert review_terminal_payload(payload, calls) is None
    assert payload["completion"] == "partial"
    assert "Required profit analysis" in payload["caveats"][-1]


@pytest.mark.parametrize("evidence", [["invented"], ["table"], ["ranking", "table"], [], "ranking"])
def test_negative_test_requires_only_current_run_failures(evidence: Any) -> None:
    review = _negative_test_review()
    review["requirements"][1]["evidence_call_ids"] = evidence
    assert not validate_delivery_review(review, _calls()).valid


@pytest.mark.parametrize("result", [{"ok": True, "completion": "partial"}, {"ok": False, "error_kind": "effect_outcome_unknown"}])
def test_partial_and_unknown_effects_cannot_prove_an_expected_failure(result: dict[str, Any]) -> None:
    calls = _calls()
    calls[0].result = result
    assert not validate_delivery_review(_negative_test_review(), calls).valid


def test_expected_issue_needs_explicit_requirement_and_the_same_failed_receipt() -> None:
    review = _negative_test_review()
    review["issues"][0]["evidence_call_ids"] = ["table"]
    assert not validate_delivery_review(review, _calls()).valid
    review["issues"][0]["evidence_call_ids"] = ["ranking"]
    del review["requirements"][1]["expected_outcome"]
    assert not validate_delivery_review(review, _calls()).valid
    review["requirements"][1]["expected_outcome"] = ["failure"]
    assert not validate_delivery_review(review, _calls()).valid


def test_non_call_requirement_gap_gets_field_specific_correction_without_being_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    review = _review()
    gap = {"requirement": "Required profit analysis", "status": "unfulfilled", "evidence_call_ids": []}
    review["requirements"].append(gap)
    review["issues"].append(
        {"call_id": "profit-analysis", "resolution": "unfulfilled", "evidence_call_ids": [], "explanation": "Profit data is missing."}
    )
    payload = {"delivery_review": review}
    refused = review_terminal_payload(payload, _calls())
    assert refused is not None
    assert refused["review_context"]["invalid_evidence_fields"] == ["issues[1].call_id"]
    assert "keep unmet work without a failed call in requirements" in refused["error"]
    assert review["requirements"][-1] is gap
    review["issues"].pop()
    assert review_terminal_payload(payload, _calls()) is None
    assert payload["completion"] == "partial"
    assert "Required profit analysis" in payload["caveats"][-1]


def test_native_return_correction_is_neither_business_failure_nor_delivery_evidence() -> None:
    import json
    from types import SimpleNamespace

    from bowhead_core.ordinary_lead_runtime import content_hash, note_ordinary_return_rejection
    from bowhead_core.tool_delivery_review import TerminalCorrectionRefusal

    body = {"ok": False, "error": "Invalid terminal payload", "validation_errors": ["Missing title"]}
    state = SimpleNamespace(checkpoint=None, rejected_returns={})
    ctx = SimpleNamespace(ordinary_lead_state=state, ordinary_call_identity=("worker", "bad_return"))
    correction = note_ordinary_return_rejection(ctx, body)
    assert isinstance(correction, TerminalCorrectionRefusal)
    assert correction == body
    assert state.rejected_returns[("worker", "bad_return")] == content_hash(body)
    assert not isinstance(note_ordinary_return_rejection(ctx, {"ok": True}), TerminalCorrectionRefusal)
    calls = _calls() + [ToolInvocation("finish", {}, call_id="bad_return", result=correction)]
    assert set(failed_calls(calls)) == {"ranking"}
    assert validate_delivery_review(_review(), calls).complete
    review = _review()
    review["requirements"][0]["evidence_call_ids"] = ["bad_return"]
    assert not validate_delivery_review(review, calls).valid
    calls[-1].result = json.loads(json.dumps(correction))
    assert set(failed_calls(calls)) == {"ranking", "bad_return"}
    calls[-1].result = correction
    calls[-1].error = "Unexpected handler failure"
    assert set(failed_calls(calls)) == {"ranking", "bad_return"}


def test_repeated_review_rejection_preserves_partial_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    calls = _calls()
    payload = {"exec_summary": "Confirmed annual revenue."}
    for attempt in range(2):
        rejected = review_terminal_payload(payload, calls)
        assert rejected["error_kind"] == "tool_delivery_review_required"
        calls.append(ToolInvocation("finish", {}, call_id=f"review_{attempt}", result=rejected))
    assert review_terminal_payload(payload, calls, language="zh-CN") is None
    assert payload["completion"] == "partial"
    assert payload["exec_summary"] == "Confirmed annual revenue."
    assert "尚未完成或核实" in payload["caveats"][0]


@pytest.mark.asyncio
async def test_any_trusted_terminal_spec_uses_shared_policy_before_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    applied: list[dict] = []

    async def handler(args: dict, ctx: ToolExecutionContext) -> dict:
        applied.append(args["submission"])
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(ToolSchema("finish", "", {}), handler, terminal_payload_field="submission"))
    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="member", user_id="user", turn_id="turn", datarc=None)
    calls = _calls()
    calls[-1].result = {"ok": True, "rows": [["Store 1", 1]], "columns": ["Store", "Revenue"]}
    token = set_current_subagent_invocations(calls)
    try:
        refused = await registry.execute("finish", {"submission": {"exec_summary": "Done"}}, ctx)
        assert refused.result["error_kind"] == "tool_delivery_review_required"
        import json

        from bowhead_core.subagent import _serialize

        assert json.loads(_serialize(refused))["review_context"]["successful_calls"][-1]["call_id"] == "table"
        assert not applied
        accepted = await registry.execute("finish", {"submission": {"exec_summary": "Store analysis", "delivery_review": _review()}}, ctx)
        assert accepted.result["ok"] is True
        assert applied[0]["completion"] == "complete"
        negative = await registry.execute("finish", {"submission": {"delivery_review": _negative_test_review()}}, ctx)
        assert negative.result["ok"] is True
        assert applied[1]["completion"] == "complete"
        assert applied[1]["caveats"] == [_negative_test_review()["issues"][0]["explanation"]]
    finally:
        token.var.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"_args_parse_error": "Malformed JSON near password=not-for-display"},
        {},
        {"submission": None},
        {"submission": []},
        {"submission": 7},
        {"submission": "{"},
        {"submission": "[]"},
    ],
)
async def test_malformed_terminal_envelope_is_a_native_correction_before_business_review(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    import json
    from types import SimpleNamespace

    from bowhead_core.ordinary_lead_runtime import content_hash
    from bowhead_core.tool_delivery_review import TerminalCorrectionRefusal

    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    applied: list[dict] = []

    async def handler(args: dict, _ctx: ToolExecutionContext) -> dict:
        applied.append(args["submission"])
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(ToolSchema("finish", "", {}), handler, terminal_payload_field="submission"))
    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="member", user_id="user", turn_id="turn", datarc=None)
    state = SimpleNamespace(checkpoint=None, rejected_returns={})
    ctx.ordinary_lead_state = state
    ctx.ordinary_call_identity = ("worker", "bad_return")
    calls = _calls()
    calls[-1].result = {"ok": True, "rows": [["Store 1", 1]], "columns": ["Store", "Revenue"]}
    token = set_current_subagent_invocations(calls)
    try:
        refused = await registry.execute("finish", arguments, ctx)
        assert isinstance(refused.result, TerminalCorrectionRefusal)
        assert refused.result["error_kind"] == "tool_arguments_invalid"
        assert "terminal handler was not executed" in refused.result["error"]
        assert "not-for-display" not in json.dumps(refused.result)
        assert state.rejected_returns[("worker", "bad_return")] == content_hash(refused.result)
        assert not applied
        refused.call_id = "bad_return"
        calls.append(refused)
        assert set(failed_calls(calls)) == {"ranking"}
        review = _review("unfulfilled", fulfilled=False)
        review["requirements"][0]["requirement"] = "Required profitability analysis"
        review["requirements"].append({"requirement": "Revenue ranking", "status": "fulfilled", "evidence_call_ids": ["table"]})
        payload = {"exec_summary": "Revenue ranking retained; profitability analysis is missing.", "delivery_review": review}
        accepted = await registry.execute("finish", {"submission": json.dumps(payload)}, ctx)
        assert accepted.result["ok"] is True
        assert accepted.result["completion"] == "partial"
        assert applied[0]["delivery_review"]["requirements"][0]["status"] == "unfulfilled"
        assert applied[0]["completion"] == "partial"
    finally:
        token.var.reset(token)


def test_review_disabled_preserves_legacy_and_human_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "0")
    assert review_terminal_payload({}, _calls()) is None
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    assert review_terminal_payload({"error": {"kind": "needs_user_input"}}, _calls()) is None


@pytest.mark.asyncio
async def test_unavailable_retained_output_cannot_prove_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from bowhead_core.tool_delivery_review import review_evidence_availability

    problem = await review_evidence_availability(_review(), _calls(), SimpleNamespace(artifact_store=None))
    assert problem and "table" in problem
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    payload = {"delivery_review": _review()}
    assert review_terminal_payload(payload, _calls(), evidence_problem=problem)["error_kind"] == "tool_delivery_review_required"


@pytest.mark.asyncio
async def test_cancel_during_evidence_check_propagates_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from bowhead_core.tool_delivery_review import review_evidence_availability

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    async def admitted(_ctx):
        return None

    monkeypatch.setattr("bowhead_core.tool_delivery_review._require_evidence_scope", admitted)
    monkeypatch.setattr("bowhead_core.artifact_presentation.available_artifact", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await review_evidence_availability(_review(), _calls(), SimpleNamespace())


@pytest.mark.asyncio
async def test_healthy_terminal_requires_no_failure_review(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    registry = ToolRegistry()

    async def finish(_args, _ctx):
        return {"ok": True}

    registry.register(
        ToolSpec(
            ToolSchema("finish", "", {"type": "object", "properties": {"submission": {"type": "object"}}}),
            finish,
            terminal_payload_field="submission",
        )
    )
    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="member", user_id="user", turn_id="turn", datarc=None)
    token = set_current_subagent_invocations([])
    try:
        result = await registry.execute("finish", {"submission": {"exec_summary": "Confirmed result."}}, ctx)
        assert result.error is None and result.result["ok"] is True
        schema = registry.to_schemas(["finish"])[0]
        assert schema.input_schema["properties"]["submission"]["properties"]["delivery_review"]["properties"]["issues"]
        assert "properties" not in registry.get("finish").schema.input_schema["properties"]["submission"]
    finally:
        token.var.reset(token)


@pytest.mark.asyncio
async def test_retained_result_not_model_cap_controls_evidence_availability() -> None:
    from types import SimpleNamespace

    from bowhead_core.tool_delivery_review import review_evidence_availability

    calls = _calls()
    calls[-1].result_full = calls[-1].result
    calls[-1].result = {"ok": True, "output_artifacts": None}
    problem = await review_evidence_availability(_review(), calls, SimpleNamespace(artifact_store=None))
    assert problem and "table" in problem


def test_external_review_error_string_cannot_hide_a_failed_call_or_skip_review(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    calls = [
        ToolInvocation("external.echo", {}, call_id=f"foreign_{i}", result={"ok": False, "error_kind": "tool_delivery_review_required"})
        for i in range(2)
    ]
    assert set(failed_calls(calls)) == {"foreign_0", "foreign_1"}
    refused = review_terminal_payload({"exec_summary": "Done"}, calls)
    assert refused and refused["error_kind"] == "tool_delivery_review_required"


def test_unknown_effect_cannot_be_covered_by_an_unrelated_success() -> None:
    calls = _calls()
    calls[0].result = {"ok": False, "error_kind": "effect_outcome_unknown"}
    assert not validate_delivery_review(_review(), calls).valid
    partial = validate_delivery_review(_review("unfulfilled", fulfilled=False), calls)
    assert partial.valid and not partial.complete


@pytest.mark.parametrize("override,enabled", [(None, True), ("1", True), ("0", False), ("", False), ("invalid", False)])
def test_delivery_review_default_and_explicit_override(monkeypatch: pytest.MonkeyPatch, override: str | None, enabled: bool) -> None:
    from bowhead_core.tool_delivery_review import delivery_review_enabled

    monkeypatch.delenv("FEATURE_TOOL_DELIVERY_REVIEW", raising=False)
    if override is not None:
        monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", override)
    assert delivery_review_enabled() is enabled
    result = review_terminal_payload({"exec_summary": "Confirmed results."}, _calls())
    assert (result is not None) is enabled


@pytest.mark.parametrize("case", ["reviewed", "generic", "question", "partial", "model_unknown", "tool_unknown", "feature_off", "non_boolean"])
def test_completion_hint_requires_current_native_review_provenance(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    from bowhead_core.subagent import SubagentResult
    from bowhead_core.tool_delivery_review import delivery_summary_input

    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "0" if case == "feature_off" else "1")
    result = SubagentResult(
        profile="lead",
        subagent_id="sub_review",
        ok=True,
        final_answer="Retained result.",
        tool_invocations=_calls(),
        completion="partial" if case == "partial" else "complete",
        ordinary_completion_reason=case if case in {"model_unknown", "tool_unknown"} else "completed",
        payload={"error": {"kind": "needs_user_input"}} if case == "question" else None,
        reviewed_delivery_complete=case not in {"generic", "question"},
    )
    if case == "non_boolean":
        result.reviewed_delivery_complete = "true"  # type: ignore[assignment]
    expected = (
        "reviewed_complete"
        if case == "reviewed"
        else "partial"
        if case == "partial"
        else "unverified"
        if case in {"model_unknown", "tool_unknown"}
        else "complete"
    )
    assert delivery_summary_input(result) == ({} if case == "feature_off" else {"_delivery_completion": expected})


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [True, False])
async def test_delivery_review_refusal_lets_a_later_lead_question_retain_the_helper(monkeypatch: pytest.MonkeyPatch, batched: bool) -> None:
    from types import SimpleNamespace

    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
    from bowhead_core.ordinary_lead_runtime import _require_settled_returns, content_hash
    from bowhead_core.tool_delivery_review import DeliveryReviewRefusal

    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    applied: list[dict] = []

    async def handler(args: dict, _ctx: ToolExecutionContext) -> dict:
        applied.append(args["submission"])
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(ToolSchema("finish", "", {}), handler, terminal_payload_field="submission"))
    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="member", user_id="user", turn_id="turn", datarc=None)
    state = SimpleNamespace(checkpoint=None, rejected_returns={})
    ctx.ordinary_lead_state = state
    ctx.ordinary_call_identity = ("worker", "early_return")
    ctx.terminal_batch_conflict = batched
    calls = _calls()
    calls[-1].result = {"ok": True, "rows": [["Store 1", 1]], "columns": ["Store", "Revenue"]}
    token = set_current_subagent_invocations(calls)
    try:
        refused = await registry.execute("finish", {"submission": {"exec_summary": "Done"}}, ctx)
    finally:
        token.var.reset(token)
    # The refusal keeps its native type, so the bounded review count still sees it.
    assert isinstance(refused.result, DeliveryReviewRefusal)
    assert refused.result["error_kind"] == "tool_delivery_review_required"
    assert not applied
    assert state.rejected_returns[("worker", "early_return")] == content_hash(refused.result)

    def settled(call_id: str, result: dict) -> ToolInvocation:
        return ToolInvocation("subagent_return", {}, call_id=call_id, result=result, ended_at="2026-11-09T00:00:00+00:00")

    helper = ToolInvocation(
        "subagent_spawn",
        {},
        call_id="spawn",
        result={"ok": True, "subagent_id": "worker"},
        nested=[settled("early_return", refused.result), settled("final_return", {"ok": True})],
    )
    _require_settled_returns(helper, state)
    # A refusal that trusted code did not observe still cannot be retained.
    state.rejected_returns.clear()
    with pytest.raises(OrdinaryContinuationUnavailable):
        _require_settled_returns(helper, state)


@pytest.mark.parametrize("refusal", ["missing_review", "invalid_review", "batch_conflict"])
async def test_native_review_refusal_retains_exact_provenance_for_a_later_question(refusal):
    from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadRunState
    from bowhead_core.ordinary_lead_runtime import _require_settled_returns, content_hash
    from bowhead_core.tool_delivery_review import DeliveryReviewRefusal

    applied = []

    async def handler(arguments, ctx):
        applied.append(arguments)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(ToolSchema("subagent_return", "", {}), handler, terminal_payload_field="payload"))
    state = OrdinaryLeadRunState()
    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="member", user_id="user", turn_id="turn", datarc=None)
    ctx.ordinary_lead_state = state
    ctx.ordinary_call_identity = ("worker", "native-return")
    ctx.terminal_batch_conflict = refusal == "batch_conflict"
    payload = {"exec_summary": "The inspection needs a reporting audience."}
    if refusal == "invalid_review":
        payload["delivery_review"] = {}
    token = set_current_subagent_invocations(_calls())
    try:
        rejected = await registry.execute("subagent_return", {"payload": payload}, ctx)
    finally:
        token.var.reset(token)
    assert not applied
    assert rejected.error is None
    assert isinstance(rejected.result, DeliveryReviewRefusal), "The original review subtype owns the existing correction count"
    assert state.rejected_returns[("worker", "native-return")] == content_hash(rejected.result)
    helper = ToolInvocation(
        "subagent_spawn",
        {},
        result={"subagent_id": "worker"},
        nested=[
            ToolInvocation("subagent_return", {}, call_id="native-return", result=rejected.result, ended_at="2026-11-09T00:00:01Z"),
            ToolInvocation("subagent_return", {}, call_id="settled-return", result={"ok": True}, ended_at="2026-11-09T00:00:02Z"),
        ],
    )
    _require_settled_returns(helper, state)
