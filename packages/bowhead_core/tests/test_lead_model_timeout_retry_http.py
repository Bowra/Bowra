"""A model call that times out before any output is retried, not failed as internal.

Lead's first call received no byte for 60 seconds. The
durable invocation receipt marked it unknown, nothing retried, and the turn
failed as ``lead_internal_error``. A no-output timeout is now settled as not
applied, the Router retries it with a visible ``llm.retry_scheduled`` event, and
exhaustion is published as ``llm_provider_timeout``.
"""

import json

import anyio
import httpx2
import pytest
import respx
from test_ordinary_lead_question_http import response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("recovers", [True, False])
async def test_no_output_timeout_retries_with_visible_progress(http_start, monkeypatch, recovers):
    case = http_start
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    orch = case.app.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    # One retry keeps the real 2-second backoff short.
    orch.router.max_retries = 1
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("tools"):
            # Background model work (titles, learning) is not Lead's call.
            return response(body, {"role": "assistant", "content": "Sales"}, "stop")
        calls.append(body)
        if len(calls) == 1 or not recovers:
            raise httpx2.ReadTimeout("no response")
        return response(body, {"role": "assistant", "content": "Sales were 42."}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json={"workspace_id": case.fixture.scope.workspace_id, "message": "Show sales."})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)

    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    persisted = [(row["event_type"], json.loads(row["payload_json"])) for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    retries = [payload for kind, payload in persisted if kind == "llm.retry_scheduled"]
    assert [(r["retry"], r["max_retries"], r["reason"], r["profile"]) for r in retries] == [(1, 1, "timeout", "lead")]
    if recovers:
        assert turn["status"] == "completed", turn["error"]
        assert len(calls) == 2
    else:
        # Two stream attempts, then the existing single invoke fallback.
        assert len(calls) == 3
        assert turn["status"] == "failed" and turn["error"] == "llm_provider_timeout"
        done = next(payload for kind, payload in persisted if kind == "done")
        assert done["reason_code"] == "llm_provider_timeout"
        assert done["recovery_action"] == "retry"
    with anyio.fail_after(5):
        replay = await case.client.get(accepted.json()["stream_url"])
    replayed = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
    assert [p["retry"] for p in replayed if "max_retries" in p and "delay_s" in p] == [1]
