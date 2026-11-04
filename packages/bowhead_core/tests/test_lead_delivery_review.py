"""The ordinary Lead reviews delivery without acquiring terminal write authority."""

import asyncio
import copy
import json

import pytest
from bowhead_core.agents.lead_orchestrator import _LEAD_TOOL_DENY
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_delivery_review import delivery_summary_input, validate_delivery_review
from bowhead_core.types import ToolInvocation
from test_agent_collaboration_runner import RunnerFixture, receipts
from test_agent_collaboration_runner import collab as collab

from bowhead_router import LLMResponse, ToolCall

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "case", ["complete", "expected_negative", "missing", "corrected_review", "stale", "revoked", "mixed", "malformed_limit", "text_only"]
)
async def test_lead_reviews_corrected_dispatch_through_native_control(collab, monkeypatch, tmp_path, case):
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    requests = 0
    business_calls, views = [], []
    review = None
    availability_checks = 0
    if case == "revoked":

        async def unavailable_after_acceptance(*args):
            nonlocal availability_checks
            availability_checks += 1
            return None if availability_checks == 1 else "Evidence is no longer available."

        monkeypatch.setattr("bowhead_core.tool_delivery_review.review_evidence_availability", unavailable_after_acceptance)

    async def spawn(args, ctx):
        business_calls.append(args["profile"])
        views.append(ctx.tool_view)
        if case == "expected_negative":
            from bowhead_core.events import OrchestratorEvent

            started = ctx.parent_bus._step_outcome_facts[-1]
            assert started.event_type == "step_started" and started.tool_name == "subagent_spawn"
            await ctx.parent_bus.emit(
                OrchestratorEvent(
                    event_type="peer_dispatch_failed" if args["profile"] == "wrong" else "lead.subagent_summary",
                    turn_id=ctx.turn_id,
                    payload={
                        "call_id": started.call_id,
                        "subagent_id": "sub_invalid" if args["profile"] == "wrong" else "sub_valid",
                        **({"peer_name": "wrong"} if args["profile"] == "wrong" else {"profile": "selected", "status": "succeeded"}),
                    },
                )
            )
        if args["profile"] == "selected" and case == "complete":
            child = await harness.run(collab.requester, context_changes={"tool_view": ctx.tool_view})
            assert child.ok and child.completion == "complete", child.error
        return (
            {"ok": False, "error_kind": "invalid_args", "error": "Correct the selected profile."}
            if args["profile"] == "wrong"
            else {"ok": True, "final_answer": "ROE is 60%."}
        )

    async def probe(args, ctx):
        business_calls.append("probe")
        return {"ok": True}

    def submit(value):
        return LLMResponse(content="", tool_calls=[ToolCall(id="review", name="review_delivery", arguments={"payload": value})])

    async def provider(request):
        nonlocal requests, review
        schemas = {schema.name: schema.input_schema for schema in request.tools}
        if request.invocation_execution_id == collab.requester:
            assert "review_delivery" not in schemas, "An inherited view must not grant the Lead control to a helper"
            assert "delivery_review" in schemas["subagent_return"]["properties"]["payload"]["properties"]
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="helper-return",
                        name="subagent_return",
                        arguments={"payload": {"delivery_surface": "chat", "exec_summary": "Read-only result."}},
                    )
                ],
            )
        requests += 1
        assert "subagent_return" not in schemas
        assert "review_delivery" in schemas, "The actual Lead request needs a reachable native review carrier"
        if requests <= 2:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id=f"spawn-{requests}", name="subagent_spawn", arguments={"profile": "wrong" if requests == 1 else "selected"})],
            )
        if requests == 3:
            return LLMResponse(content="ROE is 60%.")
        if requests == 4:
            guidance = next(message.content for message in request.messages if "Before finishing, check the current user request" in message.content)
            assert "review_delivery" in guidance and "identical arguments" in guidance
            context = json.loads(guidance.split("review_context (JSON data): ", 1)[1])
            failed = context["failed_calls"][0]["call_id"]
            success = context["successful_calls"][0]["call_id"]
            review = {
                "requirements": [
                    {
                        "requirement": "Calculate ROE",
                        "status": "unfulfilled" if case == "missing" else "fulfilled",
                        "evidence_call_ids": [] if case == "missing" else [success],
                    }
                ],
                "issues": [
                    {
                        "call_id": failed,
                        "resolution": "unfulfilled" if case == "missing" else "covered",
                        "evidence_call_ids": [] if case == "missing" else [success],
                        "explanation": "The selected profile supplied the requested calculation.",
                    }
                ],
            }
            if case == "expected_negative":
                review["requirements"].append(
                    {
                        "requirement": "Observe one requested invalid-profile rejection",
                        "status": "fulfilled",
                        "expected_outcome": "failure",
                        "evidence_call_ids": [failed],
                    }
                )
                review["issues"][0].update(
                    resolution="expected",
                    evidence_call_ids=[failed],
                    explanation="The requested invalid-profile attempt was rejected; the calculation was delivered separately.",
                )
            if case == "text_only":
                return LLMResponse(content="<delivery_review>" + json.dumps(review) + "</delivery_review>")
            if case == "corrected_review":
                bad = copy.deepcopy(review)
                bad["issues"][0]["resolution"] = "recovered"
                return submit({"delivery_review": bad})
            if case == "mixed":
                response = submit({"delivery_review": review})
                response.tool_calls.append(ToolCall(id="probe", name="probe", arguments={}))
                return response
        if case == "malformed_limit" and requests in {4, 5, 6}:
            if requests > 4:
                assert receipts(request)[-1]["error_kind"] == "tool_delivery_review_required"
            return submit({"exec_summary": "A review cannot finalize or write a report."})
        if case in {"mixed", "corrected_review"} and requests == 5:
            failures = [item for item in receipts(request) if item.get("error_kind") == "tool_delivery_review_required"]
            assert len(failures) == 1
            assert "review_delivery" in failures[0]["error"] and "terminal payload" not in failures[0]["error"]
            return submit({"delivery_review": review})
        if requests == 4:
            return submit({"delivery_review": review})
        if case == "stale" and requests == 5:
            return LLMResponse(content="", tool_calls=[ToolCall(id="later-probe", name="probe", arguments={})])
        assert receipts(request)[-1].get("ok") is True, receipts(request)[-1]
        return LLMResponse(content="The calculation is unavailable." if case == "missing" else "ROE is 60%.")

    harness = RunnerFixture(collab, provider)
    if case == "expected_negative":
        from bowhead_core.events import TurnEventBus
        from bowhead_core.turn_log_store import TurnLogStore

        store = TurnLogStore(tmp_path / "native-review-events.db")
        await store.initialize()
        published = []

        async def persist(event):
            await store.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload)
            published.append(event)

        harness.bus = TurnEventBus(log_sink=persist, persist_before_live=True)
    harness.register("subagent_spawn", spawn)
    harness.register("probe", probe)
    register_subagent_return_tool(harness.registry)
    original_return = harness.registry.get("subagent_return").handler

    async def helper_return(args, ctx):
        assert ctx.tool_view is None or not ctx.tool_view.has("review_delivery"), "A helper cannot access another run's control handler"
        return await original_return(args, ctx)

    harness.registry.get("subagent_return").handler = helper_return
    result = await asyncio.wait_for(harness.run(collab.lead, tool_deny=_LEAD_TOOL_DENY), 15)
    assert not harness.provider.failures, harness.provider.failures
    assert result.ok, result.error
    complete = case in {"complete", "expected_negative", "corrected_review", "mixed"}
    assert result.completion == ("complete" if complete else "partial")
    assert getattr(result, "reviewed_delivery_complete", False) is complete
    assert delivery_summary_input(result) == {"_delivery_completion": "reviewed_complete" if complete else "partial"}
    assert business_calls == ["wrong", "selected"] + (["probe"] if case in {"stale", "mixed"} else [])
    assert requests == {"corrected_review": 6, "stale": 6, "mixed": 6, "malformed_limit": 7, "text_only": 4}.get(case, 5)
    assert result.payload is None, "Review does not become a report/terminal payload"
    assert ("<delivery_review>" in result.final_answer) == (case == "text_only"), "Answer text is never promoted into review evidence"
    assert not harness.registry.has("review_delivery"), "Run-local control must not widen the shared registry"
    if complete:
        assert "not yet" not in result.final_answer and "unverified" not in result.final_answer
    if case == "complete":
        assert not views[-1].overlay_names.intersection({"review_delivery"}), "The native control must not be unioned into helper permissions"
    if case == "expected_negative":
        from bowhead_core.agents.lead_orchestrator import _emit_event
        from bowhead_core.turn_step_outcome import step_outcome_field

        before = step_outcome_field(harness.bus, "completed")["step_outcome"]
        assert before["answer_affected"] is True, "The original failed peer must reproduce the structural mismatch"
        await _emit_event(
            harness.bus, collab.worker.turn_id, "done", {"turn_id": collab.worker.turn_id, "status": "completed", **delivery_summary_input(result)}
        )
        done = next(event.payload for event in published if event.event_type == "done")
        assert done["step_outcome"] == {**before, "answer_affected": False}
        assert "_delivery_completion" not in done and "reviewed_delivery_complete" not in done
        saved = await store.list_events(collab.worker.turn_id)
        assert next(event.payload for event in saved if event.event_type == "done") == done


@pytest.mark.parametrize("disabled", ["feature", "denied"])
async def test_unavailable_lead_control_does_not_advertise_an_impossible_carrier(collab, monkeypatch, disabled):
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "0" if disabled == "feature" else "1")
    calls = 0

    async def failed(args, ctx):
        return {"ok": False, "error_kind": "invalid_args", "error": "Controlled failure."}

    async def provider(request):
        nonlocal calls
        calls += 1
        assert "review_delivery" not in {schema.name for schema in request.tools}
        assert "subagent_return" not in {schema.name for schema in request.tools}
        if calls == 1:
            return LLMResponse(content="", tool_calls=[ToolCall(id="failure", name="subagent_spawn", arguments={})])
        return LLMResponse(content="The requested operation failed.")

    harness = RunnerFixture(collab, provider)
    harness.register("subagent_spawn", failed)
    register_subagent_return_tool(harness.registry)
    result = await harness.run(collab.lead, tool_deny=_LEAD_TOOL_DENY | ({"review_delivery"} if disabled == "denied" else set()))
    assert not harness.provider.failures and calls == 2
    assert result.completion == ("complete" if disabled == "feature" else "partial")


def test_review_control_receipts_cannot_be_business_evidence():
    calls = [
        ToolInvocation("probe", {}, call_id="failed", result={"ok": False, "error_kind": "invalid_args"}),
        ToolInvocation("review_delivery", {}, call_id="control", result={"ok": True, "reviewed_completion": "complete"}),
    ]
    review = {
        "requirements": [{"requirement": "Business work", "status": "fulfilled", "evidence_call_ids": ["control"]}],
        "issues": [
            {
                "call_id": "failed",
                "resolution": "covered",
                "evidence_call_ids": ["control"],
                "explanation": "A control response is not delivered work.",
            }
        ],
    }
    assert not validate_delivery_review(review, calls).valid


def test_review_control_declares_no_durable_business_effect():
    from bowhead_core.terminal_effect_contract import EffectClass, ToolEffectIdempotency
    from bowhead_core.tool_delivery_review import LeadDeliveryReview

    spec = LeadDeliveryReview().tool_spec()
    assert spec.effect_spec.effect_class is EffectClass.NONE
    assert spec.effect_spec.idempotency is ToolEffectIdempotency.NONE
    assert spec.allowed_for_roles == frozenset({"lead"})
    assert spec.terminal_payload_field is None, "Review cannot invoke a terminal business handler"
    assert spec.annotations.read_only and not spec.annotations.destructive


@pytest.mark.parametrize("reader_ok", [True, False])
async def test_source_failure_review_requires_original_reader_attempt(reader_ok, monkeypatch):
    from types import SimpleNamespace

    from bowhead_core.tool_delivery_review import LeadDeliveryReview

    items = [{"artifact_id": "art_original"}]
    calls = [
        ToolInvocation(
            "subagent_spawn",
            {},
            call_id="s",
            result={
                "ok": False,
                "failure_stage": "source_verification",
                "retained_artifacts": items,
            },
        )
    ]
    monkeypatch.setattr("bowhead_core.agents._lead_context.run_invocations", lambda ctx: calls)
    review = {
        "payload": {
            "delivery_review": {
                "requirements": [{"requirement": "Verify required source", "status": "unfulfilled", "evidence_call_ids": []}],
                "issues": [{"call_id": "s", "resolution": "unfulfilled", "explanation": "No verified source read.", "evidence_call_ids": []}],
            }
        }
    }
    ctx = SimpleNamespace()
    control = LeadDeliveryReview(reader_available=True).tool_spec()
    correction = await control.handler(review, ctx)
    assert correction["ok"] is False and "present_artifacts" in correction["error"] and "art_original" in correction["error"]
    without_reader = await LeadDeliveryReview().tool_spec().handler(review, ctx)
    assert without_reader["reviewed_completion"] == "partial"
    calls.append(ToolInvocation("present_artifacts", {"artifacts": items}, call_id="r", result={"ok": reader_ok}))
    if not reader_ok:
        review["payload"]["delivery_review"]["issues"].append(
            {
                "call_id": "r",
                "resolution": "unfulfilled",
                "explanation": "Original read denied.",
                "evidence_call_ids": [],
            }
        )
    outcome = await control.handler(review, ctx)
    assert outcome.get("reviewed_completion") == "partial", outcome


async def test_replayed_review_acknowledgement_does_not_restore_run_provenance(monkeypatch):
    from types import SimpleNamespace

    from bowhead_core.subagent import SubagentResult
    from bowhead_core.tool_delivery_review import LeadDeliveryReview

    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    calls = [
        ToolInvocation("subagent_spawn", {"profile": "invalid"}, call_id="failed", result={"ok": False, "error_kind": "invalid_args"}),
        ToolInvocation("subagent_spawn", {"profile": "valid"}, call_id="success", result={"ok": True}),
    ]
    monkeypatch.setattr("bowhead_core.agents._lead_context.run_invocations", lambda ctx: calls)
    control = LeadDeliveryReview()
    ctx = SimpleNamespace()
    payload = {
        "delivery_review": {
            "requirements": [{"requirement": "Deliver result", "status": "fulfilled", "evidence_call_ids": ["success"]}],
            "issues": [
                {
                    "call_id": "failed",
                    "resolution": "covered",
                    "explanation": "Valid Agent delivered the required result.",
                    "evidence_call_ids": ["success"],
                }
            ],
        }
    }
    acknowledgement = await control.tool_spec().handler({"payload": payload}, ctx)
    assert acknowledgement["reviewed_completion"] == "complete"
    calls.append(ToolInvocation("review_delivery", {}, call_id="review", result=json.loads(json.dumps(acknowledgement))))
    assert await control.current(calls, ctx) is not None
    assert await LeadDeliveryReview().current(calls, ctx) is None
    restored_result = SubagentResult(profile="lead", subagent_id="sub_restored", ok=True, final_answer="Old result.", tool_invocations=calls)
    assert restored_result.reviewed_delivery_complete is False
    assert delivery_summary_input(restored_result) == {"_delivery_completion": "complete"}


@pytest.mark.parametrize("with_tools", [False, True])
async def test_lead_retains_missing_requirement_without_business_failure(collab, monkeypatch, tmp_path, with_tools):
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    requests = 0

    async def provider(request):
        nonlocal requests
        requests += 1
        if with_tools and requests == 1:
            return LLMResponse(content="", tool_calls=[ToolCall(id="calculate", name="subagent_spawn", arguments={})])
        if requests == (2 if with_tools else 1):
            review = {
                "requirements": [{"requirement": "Required profit needs cost data", "status": "unfulfilled", "evidence_call_ids": []}],
                "issues": [],
            }
            return LLMResponse(
                content="", tool_calls=[ToolCall(id="review", name="review_delivery", arguments={"payload": {"delivery_review": review}})]
            )
        return LLMResponse(content="Revenue is retained. Required profit is missing; provide costs.")

    harness = RunnerFixture(collab, provider)

    async def success(args, ctx):
        return {"ok": True, "rows": [[4200]]}

    from bowhead_core.events import TurnEventBus
    from bowhead_core.turn_log_store import TurnLogStore

    store = TurnLogStore(tmp_path / "no-failure-partial.db")
    await store.initialize()
    live = []

    async def persist(event):
        await store.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload)
        live.append(event)

    harness.bus = TurnEventBus(log_sink=persist, persist_before_live=True)
    harness.register("subagent_spawn", success)
    register_subagent_return_tool(harness.registry)
    result = await harness.run(collab.lead, tool_deny=_LEAD_TOOL_DENY)
    assert not harness.provider.failures
    assert result.completion == "partial"
    assert not result.reviewed_delivery_complete
    assert delivery_summary_input(result) == {"_delivery_completion": "partial"}
    assert "Required profit" in result.final_answer
    from bowhead_core.agents.lead_orchestrator import _emit_event

    await _emit_event(harness.bus, collab.worker.turn_id, "done", {"status": "completed", **delivery_summary_input(result)})
    done = next(event.payload for event in live if event.event_type == "done")
    assert done["step_outcome"]["answer_affected"] is True
    assert "_delivery_completion" not in done
    saved = await store.list_events(collab.worker.turn_id)
    assert [(event.event_type, event.payload) for event in saved] == [(event.event_type, event.payload) for event in live]
