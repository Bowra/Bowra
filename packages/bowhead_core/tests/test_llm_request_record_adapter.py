"""Real adapter sends are recorded byte-exact and linked to their billing receipt."""

from __future__ import annotations

import json
import sqlite3

import pytest
import respx
from bowhead_core.llm_request_record import rebuild_body, split_body
from bowhead_router.invocation import canonical_transport_bytes, physical_observation_scope
from bowhead_router.request_record import set_request_recorder
from test_chat_reference_storage import case as case
from test_physical_usage import context, observer, request, rows
from test_skill_model_access import adapter_for, call, router_for, success

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_recorded_body_is_the_wire_body_and_matches_the_receipt(case, family, mode):
    adapter = adapter_for(family)
    recorded = []
    sent = []
    previous = set_request_recorder(recorded.append)

    def respond(req):
        sent.append(json.loads(req.content))
        return success(family, mode)

    try:
        with context(case), physical_observation_scope(observer(case)), respx.mock(assert_all_called=False) as mock:
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=respond)
            await call(router_for(adapter), request(), mode)
    finally:
        set_request_recorder(previous)
        await adapter.close()

    assert len(recorded) == len(sent) == 1
    entry = recorded[0]
    # The recorded bytes are exactly what went over the wire.
    assert entry.canonical_body == canonical_transport_bytes(sent[0])
    receipt_row = rows(case)[0]
    assert entry.usage_event_id == receipt_row["id"]
    assert entry.request_digest == json.loads(receipt_row["receipt_json"])["request_digest"]
    assert rebuild_body((chunk.field, json.loads(chunk.data)) for chunk in split_body(entry.canonical_body)) == entry.canonical_body


async def test_a_raising_recorder_leaves_the_call_and_billing_unchanged(case):
    adapter = adapter_for("openai")

    def explode(entry):
        raise RuntimeError("recorder down")

    previous = set_request_recorder(explode)
    try:
        with context(case), physical_observation_scope(observer(case)), respx.mock(assert_all_called=False) as mock:
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(return_value=success("openai", "invoke"))
            await call(router_for(adapter), request(), "invoke")
    finally:
        set_request_recorder(previous)
        await adapter.close()
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT receipt_state FROM llm_usage_events").fetchall() == [("completed",)]
