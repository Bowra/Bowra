"""A scheduled run in "only when flagged" mode must not end on a claimed alert.

On 116 (2026-11-10) the Lead twice answered "I alerted you urgently" after a helper judged the
result urgent, without calling ``routine_notify``. In that delivery mode a run without an alert
delivers nothing, so the owner received nothing. The Lead's final-answer gate now corrects such
an answer once; the Lead then alerts or answers without the claim. Runs delivered every time are
not checked, and the correction never repeats.

A run that asks its owner a question continues after the answer as the same scheduled run: it can
still alert, its answer is still checked, and an alert raised before the question counts.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.subagent import PROFILES
from test_ordinary_helper_question_http import QUESTION, _prepare
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events, routine_notify_tool
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_concept_resolve"], indirect=True),
]


class _AlertService:
    """The notification service surface ``routine_notify`` uses, recording each alert."""

    delivery_enabled = False

    def __init__(self) -> None:
        self.levels: list[str] = []
        self.runs: set[str] = set()

    async def emit_routine_alert(self, job, *, run_id, session_id, level, **_):
        self.levels.append(level)
        self.runs.add(run_id)
        return "queued", None

    async def routine_alert_raised(self, job, *, run_id):
        return run_id in self.runs


@pytest.mark.parametrize(
    "deliver,after_note",
    [
        ("on_alert", "alert"),  # The condition holds: the corrected Lead raises the alert.
        ("on_alert", "answer"),  # The Lead answers again without an alert: no second correction.
        ("always", None),  # Delivered every run: never corrected.
    ],
)
async def test_a_claimed_alert_is_corrected_once_when_only_flagged_runs_deliver(http_start, monkeypatch, deliver, after_note):
    from bowhead_gateway._scheduled_runner import run_scheduled_turn

    case = http_start
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", routine_notify_tool.TOOL_NAME)))
    for name in ("_service_provider", "_urgent_email", "_result_email_reaches_owner"):
        monkeypatch.setattr(routine_notify_tool, name, getattr(routine_notify_tool, name))
    service = _AlertService()
    orch = case.app.state.orchestrator
    assert routine_notify_tool.register_routine_notify_tool(orch.tool_registry, service_provider=lambda: service)
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    helper_profile = case.fixture.case.args["reference_start"].profile_name
    lead_calls = []

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            assert routine_notify_tool.TOOL_NAME in names
            lead_calls.append(body)
            if len(lead_calls) == 1:
                spawn = {
                    "profile": helper_profile,
                    "task": "Total the orders and say whether the total is above the limit.",
                    "delegation_role": "helper",
                }
                return response(body, call("helper-native", "subagent_spawn", spawn), "tool_calls")
            if len(lead_calls) == 2:
                return response(body, {"role": "assistant", "content": "The total is above the limit, so I alerted you urgently."}, "stop")
            if after_note == "alert" and routine_notify_tool.UNRAISED_ALERT_NOTE in str(body["messages"][-1].get("content")):
                alert = {"level": "urgent", "reason": "The total is above the limit."}
                return response(body, call("alert-native", routine_notify_tool.TOOL_NAME, alert), "tool_calls")
            return response(body, {"role": "assistant", "content": "The total is above the limit."}, "stop")
        if "subagent_return" in names:
            summary = {"exec_summary": "The total is above the limit; this needs urgent attention."}
            return response(body, call("helper-return", "subagent_return", {"payload": summary}), "tool_calls")
        return response(body, {"role": "assistant", "content": "Orders"}, "stop")

    scope = case.fixture.scope
    job = SimpleNamespace(
        id="sj_alert",
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        name="Order watch",
        query="Total the orders. If the total is above the limit, alert me urgently; otherwise do not alert.",
        tz=None,
        anchor_session_id=None,
        notify_channels={"deliver": deliver},
        datarc_workspace_id=None,
        recipe_ref=None,
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        outcome = await run_scheduled_turn(case.app, job)

    assert outcome.error is None, outcome
    turn = next(row for row in rows(case, "turns") if row["id"] == outcome.turn_id)
    assert turn["status"] == "completed", turn["error"]
    # Later Lead calls (such as the delivery review) carry the whole history, so the last one
    # shows every correction this run received.
    notes = json.dumps(lead_calls[-1]["messages"]).count(routine_notify_tool.UNRAISED_ALERT_NOTE)
    if deliver == "always":
        assert notes == 0 and service.levels == []
    else:
        # Before the fix the run ended on the claim: no correction and no alert.
        assert notes == 1
        assert service.levels == (["urgent"] if after_note == "alert" else [])


class _PausedRun:
    """The scheduled store as the answer reads it: this Turn is a run of ``job`` awaiting input."""

    def __init__(self, job, turn_id: str) -> None:
        self.job, self.turn_id = job, turn_id

    async def get_run_by_turn(self, turn_id):
        if turn_id != self.turn_id:
            return None
        return SimpleNamespace(id="sr_paused", job_id=self.job.id, turn_id=turn_id, status="awaiting_input", error=None)

    async def get(self, job_id):
        return self.job if job_id == self.job.id else None


@pytest.mark.parametrize("flow", ["ask_then_alert", "alert_then_ask", "ask_then_claim"])
async def test_a_run_that_asks_its_owner_can_still_alert_after_the_answer(http_start, monkeypatch, flow):
    # Regression check N10: the answer resumed the Turn without its scheduled run, so the resumed half had
    # no routine_notify and its answer was not checked; in "only when flagged" mode a run whose
    # condition held after the answer delivered nothing.
    from bowhead_gateway._scheduled_runner import run_scheduled_turn

    case = http_start
    orch = _prepare(case, monkeypatch)
    lead_tools = ("subagent_spawn", "clarification_ask", routine_notify_tool.TOOL_NAME)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=lead_tools))
    for name in ("_service_provider", "_urgent_email", "_result_email_reaches_owner"):
        monkeypatch.setattr(routine_notify_tool, name, getattr(routine_notify_tool, name))
    service = _AlertService()
    assert routine_notify_tool.register_routine_notify_tool(orch.tool_registry, service_provider=lambda: service)
    alert = {"level": "urgent", "reason": "The total is above the limit."}
    state = {"answered": False}
    lead_calls, resumed_tools = [], []

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" not in names:
            return response(body, {"role": "assistant", "content": "Orders"}, "stop")
        lead_calls.append(body)
        alerted = "alert-native" in json.dumps(body["messages"])
        if not state["answered"]:
            if flow == "alert_then_ask" and not alerted:
                return response(body, call("alert-native", routine_notify_tool.TOOL_NAME, alert), "tool_calls")
            return response(body, call("question-native", "clarification_ask", {"questions": [QUESTION]}), "tool_calls")
        resumed_tools.append(names)
        noted = routine_notify_tool.UNRAISED_ALERT_NOTE in str(body["messages"][-1].get("content"))
        if not alerted and (flow == "ask_then_alert" or noted):
            return response(body, call("alert-native", routine_notify_tool.TOOL_NAME, alert), "tool_calls")
        text = "The total is above the limit." if flow == "ask_then_alert" else "The total is above the limit, so I alerted you urgently."
        return response(body, {"role": "assistant", "content": text}, "stop")

    scope = case.fixture.scope
    job = SimpleNamespace(
        id="sj_ask_alert",
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        name="Order watch",
        query="Ask me which metric to total. If the total is above the limit, alert me urgently; otherwise do not alert.",
        tz=None,
        anchor_session_id=None,
        notify_channels={"deliver": "on_alert"},
        datarc_workspace_id=None,
        recipe_ref=None,
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        outcome = await run_scheduled_turn(case.app, job)
        assert outcome.awaiting is True, outcome
        # The scheduler records the paused run; the answer finds its task through that record.
        monkeypatch.setattr(case.app.state, "scheduled_store", _PausedRun(job, outcome.turn_id), raising=False)
        card = next(row for row in rows(case, "clarification_requests") if row["turn_id"] == outcome.turn_id)
        state["answered"] = True
        answered = await case.client.post(
            f"/api/v1/clarifications/{card['request_id']}/resolve",
            json={"answers": [{"question": QUESTION["question"], "source": "candidate", "answer": "Net sales", "option_index": 0}]},
        )
        assert answered.status_code == 200, answered.text
        await settled(outcome.turn_id)

    turn = next(row for row in rows(case, "turns") if row["id"] == outcome.turn_id)
    assert turn["status"] == "completed", turn["error"]
    assert resumed_tools and all(routine_notify_tool.TOOL_NAME in names for names in resumed_tools)
    assert service.levels == ["urgent"]
    corrections = sum(routine_notify_tool.UNRAISED_ALERT_NOTE in str(body["messages"][-1].get("content")) for body in lead_calls)
    # A claim after the answer is corrected once; an alert raised before the question is not questioned.
    assert corrections == (1 if flow == "ask_then_claim" else 0)
