"""Continue button: the send payload names at most four saved helper runs, only on ordinary starts."""

import json

import pytest
from bowhead_gateway.routes.chat import CONTINUE_CLIENT_KEY, ChatSendRequest, continue_checkpoint_ids
from pydantic import ValidationError

pytestmark = pytest.mark.unit

IDS = ["rsc_0123456789abcdef0123", "rsc_abcdef0123456789abcd"]


def _body(ids: object, **extra: object) -> dict:
    return {"workspace_id": "ws_1", "session_id": "s_1", "message": "继续", "client_info": {CONTINUE_CLIENT_KEY: json.dumps(ids)}, **extra}


def test_ids_are_read_in_order_without_duplicates():
    request = ChatSendRequest.model_validate(_body([*IDS, IDS[0]]))
    assert continue_checkpoint_ids(request.client_info) == IDS
    assert continue_checkpoint_ids({}) == []


@pytest.mark.parametrize("ids", [[], ["rsc_short"], ["../etc"], [1], IDS * 3, "rsc_0123456789abcdef0123"])
def test_malformed_lists_are_rejected(ids):
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate(_body(ids))


def test_continue_cannot_ride_on_a_replay_or_plan_approval():
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate(_body(IDS, plan_execution_id="pe_1"))
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate(_body(IDS, planning_mode="planning"))
