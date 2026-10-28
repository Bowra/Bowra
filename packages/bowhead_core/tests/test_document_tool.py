"""Native document admission, bounded repair and private-workflow enforcement."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.documents.request import DOCUMENT_TOOL_ARGUMENTS, DocumentRequestError
from bowhead_core.documents.tool import register_document_tool
from bowhead_core.documents.turn_policy import DocumentTurnState, abandoned_generation_rejection, empty_lookup_before_question
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_core.types import ToolInvocation
from bowhead_router.types import ToolCall, ToolSchema
from jsonschema import ValidationError as SchemaError
from jsonschema import validate
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_exact_revision_requires_document_workflow_before_model_selection():
    ordinary = DocumentTurnState()
    revision = DocumentTurnState(revision_required=True)
    assert ordinary.denial("subagent_spawn") is None
    assert revision.denial("subagent_spawn")["error"] == "document_workflow_required"
    assert revision.denial("template_document_submit") is None
    revision.select([])
    assert revision.denial("subagent_spawn")["error"] == "document_workflow_required"


@pytest.mark.parametrize(
    "boundary",
    [
        "empty",
        "nonempty",
        "failed",
        "truthy_ok",
        "missing_questions",
        "tuple_questions",
        "result_error",
        "extra_argument",
        "generate",
        "other_tool",
        "reversed",
        "additional",
        "nested",
        "question_error",
        "fake_question",
        "missing_state",
        "revision_required",
        "generation_selected",
        "native_in_batch",
        "attempts",
        "receipt",
        "planning_result",
    ],
)
def test_bounded_question_accepts_only_an_empty_native_lookup(boundary):
    state = DocumentTurnState()
    lookup = ToolInvocation("template_document_submit", {"action": "list_questions"}, result={"ok": True, "questions": []})
    question = ToolInvocation("clarification_ask", {}, result={"_clarification_pending": True})
    calls = [lookup, question]
    if boundary == "nonempty":
        lookup.result["questions"] = [{"job_id": "djob_old", "question": "Which date?"}]
    elif boundary == "failed":
        lookup.error = "read_failed"
    elif boundary == "truthy_ok":
        lookup.result["ok"] = 1
    elif boundary == "missing_questions":
        lookup.result.pop("questions")
    elif boundary == "tuple_questions":
        lookup.result["questions"] = ()
    elif boundary == "result_error":
        lookup.result["error"] = "read_failed"
    elif boundary == "extra_argument":
        lookup.arguments["job_id"] = "djob_old"
    elif boundary == "generate":
        lookup.arguments = proposal()
    elif boundary == "other_tool":
        lookup.tool_name = "subagent_spawn"
    elif boundary == "reversed":
        calls.reverse()
    elif boundary == "additional":
        calls.insert(0, ToolInvocation("template_document_submit", {"action": "list_questions"}, result={"ok": True, "questions": []}))
    elif boundary == "nested":
        lookup.nested = [ToolInvocation("skill_execute", {})]
    elif boundary == "question_error":
        question.error = "question_failed"
    elif boundary == "fake_question":
        question.result = {"_clarification_pending": 1}
    elif boundary == "missing_state":
        state = None
    elif boundary in {"revision_required", "generation_selected", "native_in_batch"}:
        setattr(state, boundary, True)
    elif boundary == "attempts":
        state.attempts = 1
    elif boundary == "receipt":
        state.receipt = {}
    elif boundary == "planning_result":
        state.planning_result = SimpleNamespace(kind="review")
    assert empty_lookup_before_question(calls, state) is (boundary == "empty")


@pytest.mark.parametrize(
    "boundary",
    [
        "known",
        "mixed_prior_result",
        "reference",
        "missing_state",
        "unselected",
        "revision",
        "retried",
        "receipt",
        "planning",
        "no_call",
        "lookup",
        "answer",
        "other_tool",
        "nested",
        "invocation_error",
        "unknown",
        "accepted",
        "truthy",
        "terminal",
        "job",
        "retryable",
        "runtime",
    ],
)
def test_abandoned_admission_requires_a_known_native_generation_refusal(boundary):
    state = DocumentTurnState(generation_selected=True, attempts=1)
    result = {"ok": False, "error": "document_evidence_mismatch", "error_kind": "validation", "retryable": False, "document_terminal": False}
    call = ToolInvocation("template_document_submit", {"action": "generate"}, result=result)
    calls = [call]
    if boundary == "mixed_prior_result":
        calls.insert(0, ToolInvocation("skill_execute", {}, result={"ok": True, "output_artifacts": [{"id": "art_prior_result"}]}))
    elif boundary == "missing_state":
        state = None
    elif boundary == "unselected":
        state.generation_selected = False
    elif boundary == "revision":
        state.revision_required = True
    elif boundary == "retried":
        state.attempts = 2
    elif boundary == "receipt":
        state.receipt = {"ok": True}
    elif boundary == "planning":
        state.planning_result = SimpleNamespace(kind="pause")
    elif boundary == "no_call":
        calls = []
    elif boundary in {"lookup", "answer"}:
        call.arguments["action"] = "list_questions" if boundary == "lookup" else "answer"
    elif boundary == "other_tool":
        call.tool_name = "other_tool"
    elif boundary == "nested":
        call.nested = [ToolInvocation("other_tool", {})]
    elif boundary == "invocation_error":
        call.error = "ToolPermissionError"
    elif boundary == "unknown":
        result["error"] = "document_capability_unavailable"
    elif boundary in {"accepted", "truthy"}:
        result["ok"] = True if boundary == "accepted" else 0
    elif boundary == "terminal":
        result["document_terminal"] = True
    elif boundary == "job":
        result["document_job_id"] = "djob_existing"
    elif boundary == "retryable":
        result["retryable"] = True
    elif boundary == "runtime":
        result["error_kind"] = "runtime"
    before = copy.deepcopy(calls)
    assert abandoned_generation_rejection(calls, state, ordinary=boundary != "reference") is (boundary in {"known", "mixed_prior_result"})
    assert calls == before, "Classifying the template refusal cannot hide or undo earlier Tool evidence"


def proposal():
    return {
        "action": "generate",
        "evidence": {"text": "Use this PPTX as the template."},
        "inputs": [{"artifact_id": "art_template", "role": "template", "evidence": {"text": "Use this PPTX as the template."}}],
        "requested_outputs": ["source", "pdf"],
    }


@pytest.fixture
def runtime(monkeypatch):
    service = SimpleNamespace(
        submit=AsyncMock(return_value=SimpleNamespace(job_id="djob_test")),
        questions=AsyncMock(return_value=[]),
        run_turn=AsyncMock(return_value=None),
    )
    registry = ToolRegistry()
    register_document_tool(registry, service=lambda: service)
    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="", user_id="u_test", turn_id="t_test", datarc=None, role="lead")
    return registry, ctx, service


@pytest.mark.parametrize(
    "arguments",
    [
        proposal(),
        {"action": "list_questions"},
        {"action": "answer", "job_id": "djob_question", "evidence": {"text": "Use last year."}},
        {"action": "revise", "evidence": {"text": "Change the title."}},
    ],
)
def test_advertised_action_schema_matches_runtime_for_supported_requests(runtime, arguments):
    registry, _, _ = runtime
    validate(arguments, registry.get("template_document_submit").schema.input_schema)
    assert DOCUMENT_TOOL_ARGUMENTS.validate_json(json.dumps(arguments)).action == arguments["action"]


@pytest.mark.parametrize("field", ["action", "evidence", "inputs"])
def test_missing_generation_fields_are_rejected_by_advertisement_and_runtime(runtime, field):
    registry, _, _ = runtime
    arguments = proposal()
    del arguments[field]
    with pytest.raises(SchemaError):
        validate(arguments, registry.get("template_document_submit").schema.input_schema)
    with pytest.raises(ValidationError):
        DOCUMENT_TOOL_ARGUMENTS.validate_json(json.dumps(arguments))


def test_registered_schema_exposes_native_types_without_expanding_nested_references(runtime):
    registry, _, _ = runtime
    schema = registry.get("template_document_submit").schema.input_schema
    assert schema["properties"]["action"]["enum"] == ["generate", "list_questions", "answer", "revise"]
    assert schema["properties"]["evidence"]["$ref"] == "#/$defs/UserQuote"
    assert schema["properties"]["inputs"]["type"] == "array"
    assert schema["properties"]["inputs"]["items"]["$ref"] == "#/$defs/InputRoleArguments"
    assert set(schema["$defs"]["InputRoleArguments"]["properties"]) == {"artifact_id", "role", "evidence"}


@pytest.mark.parametrize(
    "arguments",
    [
        {**proposal(), "evidence": json.dumps(proposal()["evidence"])},
        {**proposal(), "inputs": json.dumps(proposal()["inputs"])},
        {"action": "list_questions", "inputs": []},
        {"action": "answer", "job_id": "djob_question"},
        {"action": "answer", "job_id": "djob_question", "evidence": {"text": "Use last year."}, "requested_outputs": ["pdf"]},
        {"action": "revise", "job_id": "djob_model_selected", "evidence": {"text": "Change the title."}},
        {"action": "revise", "evidence": {"text": "Change the title."}, "inputs": []},
        {"action": "revise", "evidence": {"text": "Change the title."}, "requested_outputs": ["pdf"]},
    ],
)
def test_root_properties_do_not_relax_nested_types_or_action_boundaries(runtime, arguments):
    registry, _, _ = runtime
    with pytest.raises(SchemaError):
        validate(arguments, registry.get("template_document_submit").schema.input_schema)
    with pytest.raises(ValidationError):
        DOCUMENT_TOOL_ARGUMENTS.validate_json(json.dumps(arguments))


@pytest.mark.asyncio
async def test_missing_request_quote_has_safe_actionable_repair_and_no_job(runtime):
    registry, ctx, service = runtime
    arguments = proposal()
    del arguments["evidence"]
    result = (await registry.execute("template_document_submit", arguments, ctx)).result
    assert result["field_errors"] == [{"field": "evidence", "issue": "required"}]
    assert result["retryable"] is False and "top-level evidence.text" in result["hint"]
    service.submit.assert_not_awaited()
    repaired = (await registry.execute("template_document_submit", proposal(), ctx)).result
    assert repaired["ok"] and repaired["document_terminal"] and repaired["document_job_id"] == "djob_test"
    service.submit.assert_awaited_once()
    assert (await registry.execute("template_document_submit", proposal(), ctx)).result == repaired
    service.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_file_roles_identify_inputs_and_allow_one_exact_correction(runtime):
    registry, ctx, service = runtime
    arguments = proposal()
    arguments["inputs"].append({**arguments["inputs"][0], "role": "data"})
    rejected = (await registry.execute("template_document_submit", arguments, ctx)).result
    assert rejected["field_errors"] == [{"field": "inputs", "issue": "unique_artifact_required"}]
    assert "each artifact only once" in rejected["hint"]
    assert "art_template" not in json.dumps(rejected)
    assert not rejected.get("document_terminal")
    service.submit.assert_not_awaited()
    repaired = (await registry.execute("template_document_submit", proposal(), ctx)).result
    assert repaired["ok"] and repaired["document_job_id"] == "djob_test"
    assert (await registry.execute("template_document_submit", proposal(), ctx)).result == repaired
    service.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "field", "issue"),
    [
        ("no_template", "inputs", "exactly_one_template_required"),
        ("two_templates", "inputs", "exactly_one_template_required"),
        ("pdf_only", "requested_outputs", "source_or_source_pdf_required"),
    ],
)
async def test_cross_field_validation_identifies_the_actual_request_constraint(runtime, change, field, issue):
    registry, ctx, service = runtime
    arguments = proposal()
    if change == "no_template":
        arguments["inputs"][0]["role"] = "reference"
    elif change == "two_templates":
        arguments["inputs"].append({**arguments["inputs"][0], "artifact_id": "art_second_template"})
    else:
        arguments["requested_outputs"] = ["pdf"]
    for attempt in range(2):
        rejected = (await registry.execute("template_document_submit", arguments, ctx)).result
        assert rejected["field_errors"] == [{"field": field, "issue": issue}]
        assert bool(rejected.get("document_terminal")) == bool(attempt)
    assert not (await registry.execute("template_document_submit", proposal(), ctx)).result["ok"]
    service.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_invalid_request_ends_repair_without_scripts_or_a_job(runtime):
    registry, ctx, service = runtime
    for attempt in range(2):
        result = (await registry.execute("template_document_submit", {"action": "generate"}, ctx)).result
        assert bool(result.get("document_terminal")) == bool(attempt)
    refused = (await registry.execute("template_document_submit", proposal(), ctx)).result
    assert refused["document_terminal"] and not refused["ok"]
    service.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_failure_does_not_retry_or_claim_a_created_draft(runtime):
    registry, ctx, service = runtime
    service.submit.side_effect = DocumentRequestError("document_runtime_unavailable")
    ctx.language_context = SimpleNamespace(output_language="zh-CN")
    result = (await registry.execute("template_document_submit", proposal(), ctx)).result
    assert not result["ok"] and result["document_terminal"] and result["retryable"] is False
    assert "尚未创建草稿" in result["message"]


@pytest.mark.asyncio
async def test_unknown_admission_outcome_is_not_reported_as_absence_or_retried(runtime):
    registry, ctx, service = runtime
    service.submit.side_effect = OSError("Private infrastructure detail")
    result = (await registry.execute("template_document_submit", proposal(), ctx)).result
    assert result["document_terminal"] and "could not be confirmed" in result["message"]
    assert "Private infrastructure detail" not in json.dumps(result)
    assert (await registry.execute("template_document_submit", proposal(), ctx)).result == result
    service.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_values_and_unknown_field_names_do_not_leak_in_repair(runtime):
    registry, ctx, service = runtime
    arguments = proposal()
    arguments["private-field-name"] = "private-field-value"
    arguments["requested_outputs"] = ["private-invalid-value"]
    result = (await registry.execute("template_document_submit", arguments, ctx)).result
    assert not result["ok"] and result["field_errors"]
    assert "private-" not in json.dumps(result)
    service.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delegation_after_invalid_admission_is_terminal_without_side_effects(runtime):
    registry, ctx, service = runtime
    handler = AsyncMock()
    registry.register(ToolSpec(schema=ToolSchema(name="python_exec", description="Execute", input_schema={"type": "object"}), handler=handler))
    ctx.document_turn_state = DocumentTurnState()
    ctx.document_turn_state.select([ToolCall(id="invalid", name="template_document_submit", arguments={"action": "generate"})])
    await registry.execute("template_document_submit", {"action": "generate"}, ctx)
    ctx.document_turn_state.select([ToolCall(id="fallback", name="python_exec", arguments={"code": "pass"})])
    result = (await registry.execute("python_exec", {"code": "pass"}, ctx)).result
    assert result["document_terminal"] and result["error"] == "document_workflow_required"
    assert result["message"]
    handler.assert_not_awaited()
    service.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_selection_blocks_sibling_delegation_before_handler_execution(runtime):
    registry, ctx, _ = runtime
    handler = AsyncMock()
    registry.register(ToolSpec(schema=ToolSchema(name="subagent_spawn", description="Delegate", input_schema={"type": "object"}), handler=handler))
    ctx.document_turn_state = DocumentTurnState()
    ctx.document_turn_state.select(
        [
            ToolCall(id="fallback", name="subagent_spawn", arguments={"profile": "analyst"}),
            ToolCall(id="native", name="template_document_submit", arguments=proposal()),
        ]
    )
    result = (await registry.execute("subagent_spawn", {"profile": "analyst"}, ctx)).result
    assert result["error"] == "document_workflow_required" and result["retryable"] is False
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_question_listing_does_not_latch_ordinary_delegation(runtime):
    registry, ctx, service = runtime
    await registry.execute("template_document_submit", {"action": "list_questions"}, ctx)
    assert ctx.document_turn_state.denial("subagent_spawn") is None
    service.questions.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_action", [proposal(), {"action": "list_questions"}])
async def test_bound_revision_repairs_only_to_evidence_only_action(runtime, wrong_action):
    registry, ctx, service = runtime
    ctx.document_turn_state = DocumentTurnState(revision_required=True)
    rejected = (await registry.execute("template_document_submit", wrong_action, ctx)).result
    assert rejected["error"] == "document_revision_required" and not rejected["document_terminal"]
    service.submit.assert_not_awaited()
    service.questions.assert_not_awaited()
    accepted = (await registry.execute("template_document_submit", {"action": "revise", "evidence": {"text": "Change the title."}}, ctx)).result
    assert accepted["ok"] and accepted["document_terminal"]
    assert service.submit.call_args.args[1].text == "Change the title."
    assert (await registry.execute("template_document_submit", {"action": "list_questions"}, ctx)).result == accepted
    service.submit.assert_awaited_once()
    service.questions.assert_not_awaited()


@pytest.mark.asyncio
async def test_bound_revision_listing_cannot_evade_bounded_repair(runtime):
    registry, ctx, service = runtime
    ctx.document_turn_state = DocumentTurnState(revision_required=True)
    for attempt in range(2):
        result = (await registry.execute("template_document_submit", {"action": "list_questions"}, ctx)).result
        assert result["document_terminal"] == bool(attempt)
    service.questions.assert_not_awaited()
    service.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_typed_planning_outcome_is_shared_without_tool_json_reconstruction(runtime):
    from bowhead_core.documents.continuation import DocumentPlanningReview

    registry, ctx, service = runtime
    outcome = DocumentPlanningReview("djob_test", "a" * 64)
    service.run_turn.return_value = outcome
    result = (await registry.execute("template_document_submit", proposal(), ctx)).result
    assert result["state"] == "review" and result["document_terminal"]
    assert ctx.document_turn_state.planning_result is outcome
    assert "draft_digest" not in result
    assert (await registry.execute("template_document_submit", proposal(), ctx)).result == result
    service.run_turn.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [DocumentRequestError("document_not_found"), OSError("private runtime detail")])
async def test_failure_after_admission_keeps_original_job_in_typed_outcome(runtime, failure):
    registry, ctx, service = runtime
    service.run_turn.side_effect = failure
    result = (await registry.execute("template_document_submit", proposal(), ctx)).result
    assert result["state"] == "failed" and result["document_job_id"] == "djob_test"
    assert ctx.document_turn_state.planning_result.job_id == "djob_test"
    assert "private runtime detail" not in json.dumps(result)
    assert (await registry.execute("template_document_submit", proposal(), ctx)).result == result
    service.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["lead", "compute"])
async def test_personal_execution_context_copy_preserves_only_lead_document_result(monkeypatch, profile):
    from bowhead_core.documents.continuation import DocumentPlanningReview
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import SubagentRunner
    from test_subagent import _router

    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="", user_id="u_test", turn_id="t_test", datarc=None, role="lead")
    ctx.personal_mcp_execution_id = "private_parent_execution"
    ctx.document_turn_state = DocumentTurnState()
    original = ctx.document_turn_state
    outcome = DocumentPlanningReview("djob_test", "a" * 64)
    runner = SubagentRunner(router=_router(), tool_registry=ToolRegistry(), backend="legacy")

    async def execute(**kwargs):
        copied = kwargs["parent_ctx"]
        assert copied is not ctx and copied.personal_mcp_execution_id is None
        if profile == "lead":
            assert copied.document_turn_state is ctx.document_turn_state
            copied.document_turn_state.planning_result = outcome
        else:
            assert copied.document_turn_state is None
        return outcome

    monkeypatch.setattr(runner, "_legacy_run", execute)
    assert (
        await runner.run(profile_name=profile, task="Use the template", parent_turn_id=ctx.turn_id, parent_ctx=ctx, parent_bus=TurnEventBus())
        is outcome
    )
    if profile == "lead":
        assert ctx.document_turn_state.planning_result is outcome
    else:
        assert ctx.document_turn_state is original and original.planning_result is None
