"""A Lead stream that drops after its first text is retried, and its text withdrawn.

116 dogfood `t_041e52fa751b`: a model stream ended with ``ReadError`` after it
had started replying, the physical receipt became unknown and the Turn failed as
``lead_internal_error``. The attempt is now settled as not applied, the Router
retries it, and one durable ``llm.stream_restarted`` withdraws the interrupted
narrative so live, persisted, replayed and IM views show only the retried answer.
"""

import json
import sqlite3

import anyio
import httpx2
import pytest
import respx
from bowhead_core.events import stream_restart_narrative_chars
from bowhead_gateway.feishu import renderer
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]

_USAGE = {"prompt_tokens": 17, "completion_tokens": 9}


def _frame(delta: dict, finish: str | None = None, usage: dict | None = None) -> bytes:
    chunk: dict = {"choices": [{"delta": delta, "finish_reason": finish}]}
    if usage is not None:
        chunk["usage"] = usage
    return b"data: " + json.dumps(chunk).encode() + b"\n\n"


class _DropsAfterText(httpx2.AsyncByteStream):
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    async def __aiter__(self):
        for text in self.texts:
            yield _frame({"content": text})
        raise httpx2.ReadError("connection reset by peer")


def _stream(texts: list[str]) -> httpx2.Response:
    body = b"".join(_frame({"content": text}) for text in texts) + _frame({}, "stop", _USAGE) + b"data: [DONE]\n\n"
    return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})


def _narrative(sequence: list[tuple[str, dict]]) -> str:
    """Apply narrative deltas and Lead restarts in order, as every view does."""
    text = ""
    for kind, payload in sequence:
        if kind == "narrative.delta":
            text += payload["delta"]
        elif kind == "llm.stream_restarted":
            text = text[: max(0, len(text) - stream_restart_narrative_chars(payload))]
    return text


async def test_lead_stream_dropped_after_text_is_retried_and_withdrawn(http_start, monkeypatch):
    case = http_start
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    orch = case.app.state.orchestrator
    from bowhead_router import AgentBinding

    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    orch.router.max_retries = 1
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("tools"):
            if not body.get("stream"):
                return httpx2.Response(
                    200, json={"choices": [{"message": {"role": "assistant", "content": "Sales"}, "finish_reason": "stop"}], "usage": _USAGE}
                )
            return _stream(["Sales"])
        calls.append(body)
        assert body.get("stream"), "Lead's first call streams"
        if len(calls) == 1:
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_DropsAfterText(["<response_kind>chat</response_kind><headline>Old title</headline>", "Partial ", "answer 😀"]),
            )
        return _stream(["<response_kind>chat</response_kind><headline>New title</headline>", "Sales were ", "42."])

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json={"workspace_id": case.fixture.scope.workspace_id, "message": "Show sales."})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)

    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert turn["status"] == "completed", turn["error"]
    assert len(calls) == 2
    persisted = [(row["event_type"], json.loads(row["payload_json"])) for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    kinds = [kind for kind, _ in persisted]
    retries = [payload for kind, payload in persisted if kind == "llm.retry_scheduled"]
    assert [(r["retry"], r["reason"], r["profile"]) for r in retries] == [(1, "network", "lead")]
    restarted = [payload for kind, payload in persisted if kind == "llm.stream_restarted"]
    assert len(restarted) == 1
    assert kinds.index("llm.retry_scheduled") < kinds.index("llm.stream_restarted")
    withdrawn = [payload["delta"] for kind, payload in persisted[: kinds.index("llm.stream_restarted")] if kind == "narrative.delta"]
    assert "".join(withdrawn) == "Partial answer 😀"
    assert restarted[0]["discarded_narrative_chars"] == len("Partial answer 😀")
    assert "parent_agent_id" not in restarted[0]
    # The retried attempt's narrative is not appended to the withdrawn one.
    assert _narrative(persisted) == "Sales were 42."
    # The response-kind scanner was reset with the attempt: the retry's headline is published too.
    assert [payload["text"] for kind, payload in persisted if kind == "headline"] == ["Old title", "New title"]

    # The interrupted attempt is not charged; only the completed one is.
    from bowhead_core.sqlite_domains import domain_database_path

    with sqlite3.connect(domain_database_path(str(case.path), "execution")) as db:
        db.row_factory = sqlite3.Row
        receipts = [dict(row) for row in db.execute("SELECT * FROM llm_usage_events WHERE turn_id=? ORDER BY rowid", (turn_id,))]
    lead_receipts = [row for row in receipts if row["outcome_code"] == "provider_transport_interrupted/v1" or row["receipt_state"] == "completed"]
    interrupted = [row for row in lead_receipts if row["outcome_code"] == "provider_transport_interrupted/v1"]
    assert len(interrupted) == 1
    assert interrupted[0]["receipt_state"] == "not_applied"
    assert (interrupted[0]["tokens_input"], interrupted[0]["tokens_output"]) == (0, 0)

    # IM cards fold the same persisted events to the same narrative.
    state = renderer.FeishuTurnCardState()
    for kind, payload in persisted:
        renderer.apply_event_to_state(state, kind, payload)
    assert state.narrative == "Sales were 42."

    with anyio.fail_after(5):
        replay = await case.client.get(accepted.json()["stream_url"])
    frames = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
    replayed = [frame for frame in frames if "discarded_narrative_chars" in frame]
    assert [frame["discarded_narrative_chars"] for frame in replayed] == [len("Partial answer 😀")]
