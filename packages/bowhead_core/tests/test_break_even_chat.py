"""Exercise native card selection, publication and failure at the chat entry point."""

import asyncio
import json
from dataclasses import replace

import pytest
import respx
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import answer_cards

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("mode", ["standalone", "mixed", "multiple", "receipt_failure", "payload_failure", "text_failure", "cancel", "off"])
async def test_break_even_chat(http_start, monkeypatch, mode):
    case = http_start
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("FEATURE_CHAT_ANSWER_CARDS", "0" if mode == "off" else "1")
    case.app.state.orchestrator.tool_registry.register(answer_cards.break_even_spec())
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", answer_cards.TOOL)))
    if mode == "mixed":
        from bowhead_core.tool_registry import ToolSpec
        from bowhead_router.types import ToolSchema

        async def describe_costs(args, ctx):
            return {"explanation": "Variable costs change with volume."}

        case.app.state.orchestrator.tool_registry.register(
            ToolSpec(
                schema=ToolSchema(name="describe_costs", description="Explain variable costs", input_schema={"type": "object"}),
                handler=describe_costs,
            )
        )
        monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=(*PROFILES["lead"].tool_whitelist, "describe_costs")))
    calls = []
    titles = []
    refused = []
    recorded = asyncio.Event()
    release = asyncio.Event()
    emit = TurnEventBus.emit

    async def reject(bus, event):
        if event.persist_before_delivery and (
            (mode == "payload_failure" and event.event_type == "lead.final.payload")
            or (mode == "text_failure" and event.event_type == "final_answer")
            or (mode == "receipt_failure" and event.event_type == "tool_result" and event.payload["tool_name"] == answer_cards.TOOL)
        ):
            refused.append(event.event_type)
            return False
        accepted = await emit(bus, event)
        if mode == "cancel" and event.event_type == "tool_result" and event.payload["tool_name"] == answer_cards.TOOL:
            recorded.set()
            await release.wait()
        return accepted

    monkeypatch.setattr(TurnEventBus, "emit", reject)
    args = {"unit_price": "100", "unit_variable_cost": "60", "fixed_costs": "12000", "currency": "CNY"}

    def respond(request):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if not names and "输出标题：" in str(body["messages"][-1].get("content")):
            titles.append(True)
            return response(body, {"role": "assistant", "content": "Break-even scenario"}, "stop")
        calls.append(names)
        if answer_cards.TOOL in names and len(calls) == 1:
            message = call("scenario", answer_cards.TOOL, args)
            if mode == "multiple":
                sibling = call("alternative", answer_cards.TOOL, {**args, "unit_price": "90"})["tool_calls"][0]
                sibling["index"] = 1
                message["tool_calls"].append(sibling)
            if mode == "mixed":
                sibling = call("costs", "describe_costs", {})["tool_calls"][0]
                sibling["index"] = 1
                message["tool_calls"].append(sibling)
            return response(body, message, "tool_calls")
        return response(body, {"role": "assistant", "content": "The requested scenarios are complete."}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": case.fixture.scope.workspace_id,
                "message": "Give me an editable break-even calculator for price 100, unit cost 60 and fixed costs 12000 CNY.",
            },
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        if mode == "cancel":
            from bowhead_gateway.routes.turns import explicit_stop_request_id

            waiting = asyncio.create_task(settled(turn_id))
            await asyncio.wait_for(recorded.wait(), 10)
            scope = case.fixture.scope
            stop_id = explicit_stop_request_id(team_id=scope.team_id, user_id=scope.user_id, turn_id=turn_id, execution_generation=1)
            try:
                stopped = await case.client.post(
                    f"/api/v1/turns/{turn_id}/cancel",
                    json={
                        "source": "explicit_stop",
                        "expected_execution_generation": 1,
                        "cancel_request_id": str(stop_id),
                    },
                )
                assert stopped.status_code == 202, stopped.text
            finally:
                release.set()
            await waiting
        elif mode.endswith("failure"):
            with pytest.raises(RuntimeError, match="legacy_chat_durability_recovery_handoff"):
                await settled(turn_id)
        else:
            await settled(turn_id)

    events = [(row["event_type"], json.loads(row["payload_json"])) for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    cards = [data["answer_card"] for kind, data in events if kind == "lead.final.payload" and "answer_card" in data]
    if mode.endswith("failure"):
        assert refused
        assert bool(cards) == (mode == "text_failure")
        assert not any(kind == "done" and data.get("status") == "completed" for kind, data in events)
    elif mode == "cancel":
        assert not cards
        assert not any(kind == "done" and data.get("status") == "completed" for kind, data in events)
        assert next(row for row in rows(case, "turns") if row["id"] == turn_id)["status"] == "cancelled"
    elif mode in {"standalone", "mixed"}:
        assert cards == [{"kind": "break_even", "version": 1, "initial": args}], (calls, [kind for kind, _ in events])
        receipt = next(data for kind, data in events if kind == "tool_result" and data.get("call_id") == "scenario")
        assert receipt["result_meta"]["answer_card"] == {**cards[0], "ignored_fields": []}
        types = [kind for kind, _ in events]
        assert types.index("tool_result") < types.index("lead.final.payload") < types.index("final_answer") < types.index("done")
        text = next(data["text"] for kind, data in events if kind == "final_answer")
        if mode == "standalone":
            assert "300" in text
            assert len(calls) == 1
        else:
            assert "scenarios are complete" in text
            assert len(calls) >= 2
        assert len(titles) <= 1
    else:
        assert not cards
        assert any(kind == "done" and data.get("status") == "completed" for kind, data in events)
        assert all(answer_cards.TOOL not in names for names in calls) if mode == "off" else len(calls) >= 2
