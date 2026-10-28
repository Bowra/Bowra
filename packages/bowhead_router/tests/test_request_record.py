"""Every actual send is handed to the diagnostic request recorder without affecting the call."""

from __future__ import annotations

import hashlib

import pytest
from bowhead_router.invocation import begin_physical_invocation, canonical_transport_bytes, canonical_transport_digest
from bowhead_router.request_record import (
    RequestRecordEntry,
    note_request_change,
    request_change_scope,
    set_request_recorder,
)
from bowhead_router.usage import usage_context

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2}


@pytest.fixture
def recorded():
    entries: list[RequestRecordEntry] = []
    previous = set_request_recorder(entries.append)
    try:
        yield entries
    finally:
        set_request_recorder(previous)


def _turn():
    return usage_context(team_id="team_1", user_id="u_1", workspace_id="ws_1", turn_id="t_1", reason="turn")


async def _send(body=BODY):
    return await begin_physical_invocation(provider="p", model="m", adapter_contract_key="openai_compatible/1", mode="invoke", request=body)


async def test_each_send_records_the_exact_canonical_body_and_digest(recorded):
    with _turn():
        await _send()
        await _send()

    assert len(recorded) == 2
    entry = recorded[0]
    assert entry.canonical_body == canonical_transport_bytes(BODY)
    assert entry.request_digest == canonical_transport_digest(BODY) == hashlib.sha256(entry.canonical_body).hexdigest()
    assert (entry.provider, entry.model, entry.mode, entry.usage.turn_id) == ("p", "m", "invoke", "t_1")


async def test_calls_outside_a_turn_are_not_recorded(recorded):
    await _send()
    with usage_context(team_id="team_1", user_id="u_1", workspace_id="ws_1", reason="dreaming"):
        await _send()
    assert recorded == []


async def test_a_failing_recorder_never_breaks_the_call():
    def explode(entry):
        raise RuntimeError("recorder down")

    previous = set_request_recorder(explode)
    try:
        with _turn():
            assert await _send() is None
    finally:
        set_request_recorder(previous)


async def test_pending_changes_travel_with_the_next_send_only(recorded):
    with _turn(), request_change_scope():
        note_request_change("tool_result_capped", tool_call_id="call_1", full_bytes=900, capped_bytes=300)
        await _send()
        await _send()

    assert [change.kind for change in recorded[0].changes] == ["tool_result_capped"]
    assert recorded[0].changes[0].detail == {"tool_call_id": "call_1", "full_bytes": 900, "capped_bytes": 300}
    assert recorded[1].changes == ()


async def test_unknown_change_kinds_are_ignored(recorded):
    with _turn(), request_change_scope():
        note_request_change("not_a_kind", tool_call_id="call_1")
        await _send()
    assert recorded[0].changes == ()
