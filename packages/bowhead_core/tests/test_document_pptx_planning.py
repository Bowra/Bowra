"""Bounded repair tests through the real proposal parser and evidence reread."""

from __future__ import annotations

import asyncio
import json

import pytest
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.pptx_planning import PlanningReply, plan_pptx_text
from test_document_pptx_plan import case as case

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_valid_proposal_uses_one_call_and_remains_unapproved(case):
    calls = []

    async def invoke(system, prompt):
        calls.append((system, prompt))
        return PlanningReply(json.dumps(case[4]), "stop")

    result = await plan_pptx_text(*case[:3], invoke=invoke)
    assert len(calls) == len(result.attempts) == 1
    assert result.review.semantic_status == "unapproved"
    assert result.error is None
    assert result.attempts[0].request_digest == case[3].digest


@pytest.mark.asyncio
async def test_completed_protocol_failure_gets_one_grounded_repair(case):
    calls = []
    invalid = json.dumps({**case[4], "approved": True})

    async def invoke(system, prompt):
        calls.append(prompt)
        return PlanningReply(invalid if len(calls) == 1 else json.dumps(case[4]), "stop")

    result = await plan_pptx_text(*case[:3], invoke=invoke)
    repair = json.loads(calls[1])
    assert repair["input"] == json.loads(case[3].payload)
    assert repair["repair"]["previous_response"] == invalid
    assert repair["repair"]["error"] == "planning_invalid_response"
    assert result.attempts[0].error == "planning_invalid_response"
    assert result.attempts[0].request_digest != result.attempts[1].request_digest
    assert result.review.semantic_status == "unapproved"
    assert len(result.attempts) == 2 and result.error is None


@pytest.mark.asyncio
async def test_repeated_failure_stops_without_inventing_a_question(case):
    calls = []

    async def invoke(system, prompt):
        calls.append(prompt)
        return PlanningReply('{"action":"propose","updates":[],"keep":[]}', "stop")

    result = await plan_pptx_text(*case[:3], invoke=invoke)
    assert len(calls) == len(result.attempts) == 2
    assert result.error == "planning_incomplete_scope" and result.review is None


@pytest.mark.asyncio
async def test_valid_question_does_not_trigger_repair_or_auto_approval(case):
    async def invoke(system, prompt):
        return PlanningReply(json.dumps({"action": "ask", "reason": "comparison", "slots": ["s0"], "question": "Which comparison period?"}), "stop")

    result = await plan_pptx_text(*case[:3], invoke=invoke)
    assert len(result.attempts) == 1
    assert result.review.action == "ask" and result.review.semantic_status == "unapproved"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError, ConnectionError, asyncio.CancelledError])
async def test_unknown_transport_outcome_or_cancellation_never_retries(case, error):
    calls = []

    async def invoke(system, prompt):
        calls.append(prompt)
        raise error()

    with pytest.raises(error):
        await plan_pptx_text(*case[:3], invoke=invoke)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (PlanningReply("partial", "length"), "planning_incomplete_response"),
        (PlanningReply("x" * 64_001, "stop"), "planning_response_limit"),
        (PlanningReply("\ud800", "stop"), "planning_invalid_unicode"),
    ],
    ids=["incomplete", "oversized", "invalid_unicode"],
)
async def test_unusable_response_is_not_replayed_into_a_repair_prompt(case, reply, expected):
    calls = []

    async def invoke(system, prompt):
        calls.append(prompt)
        return reply

    result = await plan_pptx_text(*case[:3], invoke=invoke)
    assert len(calls) == 1
    assert result.error == expected and result.review is None


@pytest.mark.asyncio
async def test_invalid_input_fails_before_any_model_call(case):
    calls = []

    async def invoke(system, prompt):
        calls.append(prompt)
        raise AssertionError("Invalid input must not reach the model")

    with pytest.raises(OfficePackageError, match="planning_input_limit"):
        await plan_pptx_text(case[0], "", case[2], invoke=invoke)
    assert not calls
