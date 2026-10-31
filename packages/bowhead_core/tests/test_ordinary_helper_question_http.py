"""A helper's structured question inside a retained ordinary Lead turn.

Before the fix, the spawn result became a legacy clarification sentinel that
the retained-question guard could not capture, so the whole Turn failed with
``clarification_resume_unavailable`` (``phase=clarification_capture``). The
question now reaches Lead as bail data; Lead asks it natively and the answer
continues the same Turn without rerunning the helper.

A whole-request primary that hands the request back with a question delivered
nothing, so it resumes like a helper. A primary that
cannot be retained must refuse the question without failing the Turn. With
i18n switched off the paused Turn has no language context, and the resume must
restore exactly that.
"""

import json
from dataclasses import replace

import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import ToolSpec
from bowhead_gateway.routes import clarifications
from bowhead_router.types import ToolSchema
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events, tool_registry
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_concept_resolve"], indirect=True),
]

QUESTION = {"question": "Which sales metric?", "options": ["Net sales", "Gross sales"], "type": "single_select"}


def _prepare(case, monkeypatch):
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    orch = case.app.state.orchestrator
    register_clarification_tool(orch.tool_registry)
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    return orch


@pytest.mark.parametrize(
    "helper_style,lead_asks,role,setting",
    [
        *[(style, asks, "helper", "team") for style in ("bail", "prose") for asks in (True, False)],
        ("bail", True, "primary", "team"),
        ("prose", True, "primary", "team"),
        # The built-in compute Agent, with i18n switched off.
        ("bail", True, "primary", "system"),
        ("bail", True, "helper", "i18n_off"),
        ("bail", True, "primary", "i18n_off"),
    ],
)
async def test_helper_question_reaches_lead_without_failing_the_turn(http_start, monkeypatch, lead_asks, helper_style, role, setting):
    case = http_start
    orch = _prepare(case, monkeypatch)
    if setting == "i18n_off":
        # The paused Turn has no language context; resume must not invent one.
        monkeypatch.setenv("BOWHEAD_I18N_V1_KILL", "1")
    lead_calls, helper_calls = [], []
    helper_profile = case.fixture.case.args["reference_start"].profile_name
    if setting == "system":
        from test_system_reference_baseline import configure

        _, profile = await configure(case.fixture, monkeypatch, legacy=True)
        monkeypatch.setitem(PROFILES, "compute", replace(profile, tool_whitelist=(*profile.tool_whitelist, "subagent_return")))
        helper_profile = "compute"
    if helper_style == "prose":

        async def resolve(args, ctx):
            hint = {"next_action_hint": "call_clarification_ask", "clarification_payload": {"questions": [QUESTION]}}
            return {"results": [{"token": "sales", "verdict": "ambiguous", **hint}]}

        # The admitted helper grants the actual producer of this protocol.
        schema = ToolSchema(name="bi_concept_resolve", description="Resolve terms.", input_schema={"type": "object", "properties": {}})
        orch.tool_registry.register(ToolSpec(schema=schema, handler=resolve))
        monkeypatch.setattr(tool_registry, "_tool_requires_datarc", lambda name: False)
        monkeypatch.setattr(tool_registry, "connector_for_tool", lambda name: None)

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) == 1:
                return response(
                    body,
                    call(
                        "helper-native",
                        "subagent_spawn",
                        {"profile": helper_profile, "task": "Compute sales.", "delegation_role": role},
                    ),
                    "tool_calls",
                )
            if len(lead_calls) == 2 and lead_asks:
                return response(body, call("question-native", "clarification_ask", {"questions": [QUESTION]}), "tool_calls")
            return response(body, {"role": "assistant", "content": "Net sales were selected."}, "stop")
        if "subagent_return" in names:
            helper_calls.append(body)
            if helper_style == "prose":
                if len(helper_calls) == 1:
                    return response(body, call("helper-resolve", "bi_concept_resolve", {}), "tool_calls")
                prose = {"delivery_surface": "chat", "exec_summary": "Which sales metric? Net sales or gross sales."}
                return response(body, call("helper-return", "subagent_return", {"payload": prose}), "tool_calls")
            bail = {"kind": "needs_user_input", "message": "Which sales metric?", "clarification_payload": {"questions": [QUESTION]}}
            return response(body, call("helper-return", "subagent_return", {"payload": {"error": bail}}), "tool_calls")
        return response(body, {"role": "assistant", "content": "Sales"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json={"workspace_id": case.fixture.scope.workspace_id, "message": "Show sales."})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)

        # Lead receives the helper's normalized question as data, not a pause sentinel.
        spawn_result = json.loads(next(m for m in lead_calls[1]["messages"] if m.get("tool_call_id") == "helper-native")["content"])
        assert "clarification_sentinel" not in json.dumps(spawn_result)
        assert spawn_result["bail"]["clarification_payload"]["questions"] == [QUESTION]
        assert "clarification_ask" in spawn_result["directive"]

        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        if not lead_asks:
            assert turn["status"] == "completed", turn["error"]
            assert cards == []
            return
        assert turn["status"] == "clarification_pending", turn["error"]
        assert len(cards) == 1
        answered = await case.client.post(
            f"/api/v1/clarifications/{cards[0]['request_id']}/resolve",
            json={"answers": [{"question": QUESTION["question"], "source": "candidate", "answer": "Net sales", "option_index": 0}]},
        )
        assert answered.status_code == 200, answered.text
        await settled(turn_id)

    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert turn["status"] == "completed", turn["error"]
    assert len(helper_calls) == (2 if helper_style == "prose" else 1)
    assert len(lead_calls) == 3
    answer = next(m for m in lead_calls[2]["messages"] if m.get("tool_call_id") == "question-native")
    assert "Net sales" in answer["content"]


async def test_unretained_primary_refuses_the_question_without_failing_the_turn(http_start, monkeypatch):
    """A primary that gave up for another reason keeps its result but cannot resume."""
    case = http_start
    _prepare(case, monkeypatch)
    lead_calls = []
    helper_profile = case.fixture.case.args["reference_start"].profile_name

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) == 1:
                spawn = {"profile": helper_profile, "task": "Compute sales.", "delegation_role": "primary"}
                return response(body, call("primary-native", "subagent_spawn", spawn), "tool_calls")
            if len(lead_calls) == 2:
                return response(body, call("question-native", "clarification_ask", {"questions": [QUESTION]}), "tool_calls")
            return response(body, {"role": "assistant", "content": "Which sales metric should I use: net or gross?"}, "stop")
        if "subagent_return" in names:
            bail = {"kind": "domain_mismatch", "message": "This Space has no sales metric with that name."}
            return response(body, call("primary-return", "subagent_return", {"payload": {"error": bail}}), "tool_calls")
        return response(body, {"role": "assistant", "content": "Sales"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json={"workspace_id": case.fixture.scope.workspace_id, "message": "Show sales."})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)

    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert turn["status"] == "completed", turn["error"]
    assert [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id] == []
    assert len(lead_calls) == 3
    refusal = json.loads(next(m for m in lead_calls[2]["messages"] if m.get("tool_call_id") == "question-native")["content"])
    assert refusal["error_kind"] == "clarification_capture_unavailable"
    assert "dispatched Agent result cannot be retained" in refusal["note"]
