"""Context maintenance retains its exact intent through ordinary request identity."""

import pytest
from bowhead_gateway.routes.chat import ChatActionLookupResponse, ChatSendRequest, ChatSendResponse
from pydantic import ValidationError
from test_chat_reference_submission import fingerprint  # noqa: F401

pytestmark = pytest.mark.unit
ACTION = {"contract": "context-maintenance.v1", "expected_generation": "mcg_" + "a" * 32}


def payload():
    return {
        "session_id": "s_context",
        "workspace_id": "ws_test",
        "message": "",
        "context_maintenance": ACTION,
        "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "planning_mode": "off",
    }


def test_maintenance_retry_fingerprint_binds_source_generation(fingerprint):  # noqa: F811
    request = ChatSendRequest.model_validate(payload())
    assert request.context_maintenance.model_dump() == ACTION
    assert fingerprint(payload()) != fingerprint({**payload(), "context_maintenance": {**ACTION, "expected_generation": "mcg_" + "b" * 32}})
    ordinary = payload()
    del ordinary["context_maintenance"]
    assert fingerprint(payload()) != fingerprint(ordinary)


@pytest.mark.parametrize(
    "change",
    [
        {"message": "Continue the task"},
        {"session_id": None},
        {"attachments": ["art_other"]},
        {"plan_execution_id": "plan_other"},
        {"planning_mode": "planning"},
        {"replay_source_turn_id": "t_prior"},
        {"submission_kind": "steer", "expected_parent_turn_id": "t_prior", "expected_execution_generation": 1},
        {"client_info": {"agent_draft_id": "sad_other"}},
        {"context_maintenance": {**ACTION, "contract": "context-maintenance.v2"}},
        {"context_maintenance": {**ACTION, "expected_generation": "t_prior"}},
        {"context_maintenance": {**ACTION, "authority": "owner"}},
    ],
)
def test_maintenance_cannot_admit_business_input_or_an_unknown_contract(change):
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate({**payload(), **change})


@pytest.mark.parametrize("code", ["context_conversation_head_changed", "context_maintenance_session_busy", "context_maintenance_pending_operation"])
def test_maintenance_admission_races_return_an_actionable_conflict(code):
    from bowhead_core.context_action import ContextActionRejected
    from bowhead_gateway.routes.chat import _terminal_admission_http_error

    response = _terminal_admission_http_error(ContextActionRejected(code))
    assert response.status_code == 409
    assert response.detail == {"error": {"code": code}}


@pytest.mark.parametrize("generation", [None, ACTION["expected_generation"]])
def test_receipt_serialization_retains_required_generation_fence(generation):
    action = {**ACTION, "expected_generation": generation}
    common = {"session_id": "s_context", "turn_id": "t_context", "stream_url": "/stream/t_context", "context_maintenance": action}
    receipts = [
        ChatSendResponse(**common),
        ChatActionLookupResponse(**common, action_id="action_context", action_kind="start", outcome="admitted", execution_generation=1),
    ]
    for receipt in receipts:
        encoded = receipt.model_dump(mode="json", exclude_none=True)
        assert encoded["context_maintenance"] == action
        assert type(receipt).model_validate(encoded) == receipt
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate({**payload(), "context_maintenance": {"contract": "context-maintenance.v1"}})
