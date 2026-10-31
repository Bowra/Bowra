"""A Lead answer does not turn the primary's first execution into a replay."""

import json
from dataclasses import replace

import pytest
import respx
import test_reference_http_start as reference_http
from bowhead_core._db import open_shared_ctx
from test_reference_assigned_read_question import http_start as http_start
from test_reference_assigned_read_question import model_reply
from test_reference_assigned_read_question import original_receiver_case as original_receiver_case
from test_reference_assigned_read_question import receiver_case as receiver_case
from test_reference_assigned_read_question import start_case as start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("original_receiver_case", ["admission"], indirect=True),
    pytest.mark.parametrize("baseline", ["read_only"]),
]


async def assert_origin_boundaries(case, *, turn_id, post_id, generation, lead_rounds, monkeypatch):
    from bowhead_core import chat_reference_question_origin as reader

    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN")
        original = await reader.read_question_origin_in_tx(db, turn_id=turn_id, postdispatch_card_id=post_id)
        assert original.generation == generation and len(original.answers) == lead_rounds
        with pytest.raises(ValueError):
            await reader.read_question_origin_in_tx(db, turn_id="t_foreign", postdispatch_card_id=post_id)
        with pytest.raises(ValueError):
            await reader.read_question_origin_in_tx(db, turn_id=turn_id, postdispatch_card_id="foreign-card")
        read_row = reader._row
        for field, value in {
            "original_team_id": "team_foreign",
            "original_user_id": "usr_foreign",
            "original_workspace_id": "ws_foreign",
            "original_session_id": "s_foreign",
            "original_user_text": "A different original request",
            "primary_execution_id": "aex_foreign",
            "source_hash": "sha256:" + "0" * 64,
            "composition_digest": "sha256:" + "1" * 64,
            "created_generation": 1,
        }.items():

            async def changed(connection, query, params=(), *, field=field, value=value):
                row = await read_row(connection, query, params)
                return {**dict(row), field: value} if "FROM turns t JOIN sessions s" in query else row

            with monkeypatch.context() as patch:
                patch.setattr(reader, "_row", changed)
                with pytest.raises(ValueError):
                    await reader.read_question_origin_in_tx(db, turn_id=turn_id, postdispatch_card_id=post_id)

        async def missing_consumption(connection, query, params=()):
            if "MAX(json_extract(payload_json" in query:
                return {"generation": None}
            return await read_row(connection, query, params)

        with monkeypatch.context() as patch:
            patch.setattr(reader, "_row", missing_consumption)
            with pytest.raises(ValueError):
                await reader.read_question_origin_in_tx(db, turn_id=turn_id, postdispatch_card_id=post_id)
        if generation > lead_rounds + 1:

            async def unused_consumption(connection, query, params=()):
                if "MAX(json_extract(payload_json" in query and params[-1] == original.card_ids[0]:
                    assert params[2] > params[1] + 1
                    return {"generation": params[2] - 1}
                return await read_row(connection, query, params)

            with monkeypatch.context() as patch:
                patch.setattr(reader, "_row", unused_consumption)
                with pytest.raises(ValueError):
                    await reader.read_question_origin_in_tx(db, turn_id=turn_id, postdispatch_card_id=post_id)
        await db.rollback()


@pytest.mark.parametrize("lead_rounds,retry_before_worker", [(1, None), (2, None), (1, "primary"), (2, "lead")])
async def test_first_primary_after_lead_answer_retains_its_read_and_second_question(http_start, monkeypatch, lead_rounds, retry_before_worker):
    from bowhead_core.agents.clarification_tool import register_clarification_tool
    from bowhead_core.subagent import PROFILES
    from bowhead_gateway.routes import clarifications

    from bowhead_core import events

    case = http_start
    reference_http.enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("clarification_ask", "subagent_spawn")))
    register_clarification_tool(case.app.state.orchestrator.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) <= lead_rounds:
            return model_reply(
                request,
                name="clarification_ask",
                arguments={"questions": [{"question": ("Which quarter?", "Which region?")[len(calls) - 1], "type": "free_text", "options": []}]},
            )
        step = len(calls) - lead_rounds
        if step == 1:
            return model_reply(
                request,
                name="subagent_spawn",
                arguments={
                    "profile": case.fixture.case.args["reference_start"].profile_name,
                    "task": "Read the assigned method, retain the confirmed quarter and ask for the comparison period.",
                    "delegation_role": "primary",
                },
            )
        if step == 2:
            assert "2029 Q1" in json.dumps(calls[-1]), "The first primary request must retain the original confirmed Lead answer"
            if lead_rounds == 2:
                assert "North" in json.dumps(calls[-1])
            return model_reply(request, name="skill_read", arguments={"name": "method"})
        if step == 3:
            return model_reply(
                request,
                name="subagent_return",
                arguments={
                    "payload": {
                        "error": {
                            "kind": "needs_user_input",
                            "message": "The comparison period is required.",
                            "clarification_payload": {"questions": [{"question": "Which comparison period?", "type": "free_text", "options": []}]},
                        }
                    }
                },
            )
        assert len(calls) == lead_rounds + 4, "The original primary must continue without replaying its read"
        return model_reply(request, text="The original quarter and comparison period are confirmed.")

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=reference_http.payload(case, skills=False))
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await reference_http.settled(turn_id)
        primary_id = next(row["id"] for row in reference_http.rows(case, "turn_agent_execution") if row["turn_id"] == turn_id)
        first = next(row for row in reference_http.rows(case, "clarification_requests") if row["turn_id"] == turn_id)
        assert json.loads(first["resume_context_json"])["reference_continuation"]["contract"] == "reference-lead-predispatch.v1"
        store = case.fixture.case.store

        async def retry(card_id, value):
            from bowhead_core.clarification_store import ClarificationStore

            retained = await ClarificationStore(case.path).resolve(card_id, value)
            assert retained.status == "resolved"
            before = await store.get_turn(turn_id)
            unused = before.execution_generation + 1
            assert await store.advance_turn_execution_generation(turn_id=turn_id, reference_clarification_id=card_id) == unused
            assert await store.rollback_turn_resume_generation(turn_id=turn_id, expected_execution_generation=unused, owner_id=None)
            assert not reference_http.rows(case, "chat_reference_lead_postdispatch_attempts")
            assert await store.is_turn_resume_ready(turn_id)
            return await case.client.post(f"/api/v1/clarifications/{card_id}/resume_turn")

        for index, value in enumerate(("2029 Q1", "North")[:lead_rounds]):
            pending = next(
                row for row in reference_http.rows(case, "clarification_requests") if row["turn_id"] == turn_id and row["status"] == "pending"
            )
            answered = (
                await retry(pending["request_id"], value)
                if retry_before_worker == "lead" and index == 0
                else await case.client.post(f"/api/v1/clarifications/{pending['request_id']}/resolve", json={"chosen_value": value})
            )
            assert answered.status_code == 200, answered.text
            await reference_http.settled(turn_id)
        turn = await case.fixture.case.store.get_turn(turn_id)
        assert turn.status == "clarification_pending", turn.error_kind
        origin_generation = lead_rounds + 1 + int(retry_before_worker == "lead")
        assert turn.execution_generation == origin_generation and len(calls) == lead_rounds + 3
        cards = [row for row in reference_http.rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == lead_rounds + 1 and sum(row["status"] == "resolved" for row in cards) == lead_rounds
        second = next(row for row in cards if row["status"] == "pending")
        context = json.loads(second["resume_context_json"])
        assert context["reference_continuation"]["contract"] == "reference-lead-postdispatch.v1"
        assert context.get("reference_read_question"), "The actual initial read must be retained"
        await assert_origin_boundaries(
            case, turn_id=turn_id, post_id=second["request_id"], generation=origin_generation, lead_rounds=lead_rounds, monkeypatch=monkeypatch
        )
        if retry_before_worker == "primary":
            answered = await retry(second["request_id"], "Previous quarter")
        else:
            answered = await case.client.post(
                f"/api/v1/clarifications/{second['request_id']}/resolve",
                json={
                    "answers": [
                        {
                            "question": "Which comparison period?",
                            "source": "custom",
                            "answer": "Previous quarter",
                            "skipped": False,
                            "option_index": None,
                        }
                    ]
                },
            )
        assert answered.status_code == 200, answered.text
        await reference_http.settled(turn_id)
        turn = await case.fixture.case.store.get_turn(turn_id)
        final_generation = origin_generation + 1 + int(retry_before_worker == "primary")
        assert turn.status == "completed" and turn.execution_generation == final_generation, turn.error_kind
        executions = [row for row in reference_http.rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]
        assert len(executions) == 1 and executions[0]["id"] == primary_id and executions[0]["status"] == "completed"
        records = [row for row in reference_http.rows(case, "turn_events") if row["turn_id"] == turn_id]
        results = [json.loads(row["payload_json"]) for row in records if row["event_type"] == "tool_result"]
        assert sum(row.get("tool_name") == "skill_read" for row in results) == 1
        assert sum(row["event_type"] == "peer_dispatched" for row in records) == 1
        assert [json.loads(row["payload_json"])["status"] for row in records if row["event_type"] == "done"] == ["clarification_pending"] * (
            lead_rounds + 1
        ) + ["completed"]
        assert "2029 Q1" in json.dumps(calls[-1]) and "Previous quarter" in json.dumps(calls[-1])
        assert len(calls) == lead_rounds + 4
        duplicate = await case.client.post(f"/api/v1/clarifications/{second['request_id']}/resume_turn")
        assert duplicate.status_code == 200, duplicate.text
        assert len(calls) == lead_rounds + 4
        attempts = [row for row in reference_http.rows(case, "chat_reference_lead_postdispatch_attempts") if row["turn_id"] == turn_id]
        assert len(attempts) == 1 and attempts[0]["generation"] == final_generation
        evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
        assert evidence.status_code == 200, evidence.text
        associations = evidence.json()["continuations"]
        assert [item["clarification_id"] for item in associations] == [row["request_id"] for row in cards]
        assert [item["resume_generation"] for item in associations] == [
            *(2 + index + int(retry_before_worker == "lead") for index in range(lead_rounds)),
            final_generation,
        ]
        assert {item["execution_id"] for item in associations} == {primary_id}
