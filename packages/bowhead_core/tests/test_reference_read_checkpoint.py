"""Read an exact method, ask for workspace, then continue the same native loop."""

import json

import httpx2
import pytest
import respx
from bowhead_gateway.turn_service import admit_legacy_chat_execution, spawn_resume_turn, spawn_turn
from test_bi_workspace_continuation import question_response
from test_reference_bi_continuation import answer_response, resolve_request, settle_worker
from test_reference_bi_continuation import claimed_case as _claimed_case
from test_reference_bi_continuation import continuation_case as _continuation_case
from test_reference_bi_continuation import receiver_case as _receiver_case
from test_reference_bi_continuation import start_case as _start_case
from test_skill_reference_runtime import rows

claimed_case, continuation_case, receiver_case, start_case = _claimed_case, _continuation_case, _receiver_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
    pytest.mark.parametrize("claimed_case", ["canonical_input"], indirect=True),
]


def read_response(request):
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "read-method", "type": "function", "function": {"name": "skill_read", "arguments": '{"name":"method","file":"method.md"}'}}
        ],
    }
    usage = {"prompt_tokens": 17, "completion_tokens": 4}
    if json.loads(request.content).get("stream"):
        calls = [{"index": index, **call} for index, call in enumerate(message["tool_calls"])]
        events = [
            {"choices": [{"delta": {"content": "", "tool_calls": calls}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": usage},
        ]
        return httpx2.Response(
            200,
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
    return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}], "usage": usage})


@pytest.mark.parametrize("boundary", ["continue", "disabled", "purged"])
async def test_read_result_and_remaining_iteration_budget_survive_the_workspace_pause(continuation_case, monkeypatch, boundary):
    from bowhead_core.skill_bridge import register_skill_tools

    from bowhead_skill import SkillCatalog, SkillRuntime

    case = continuation_case
    register_skill_tools(case.orchestrator.subagent_runner.tool_registry, SkillCatalog(roots=[case.base.fixture.root]), SkillRuntime())
    monkeypatch.setenv("BOWHEAD_PEER_ITER_TELEMETRY", "1")
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return read_response(request) if len(bodies) == 1 else question_response(request) if len(bodies) == 2 else answer_response(request)

    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        bus = await admit_legacy_chat_execution(case.orchestrator, case.request)
        task = await spawn_turn(
            case.orchestrator, case.request, team_id=case.request.team_id, admitted_legacy_bus=bus, reference_skill_sources=case.base.fixture.sources
        )
        await settle_worker(task)
        assert route.call_count == 2
        read_material = next(message["content"] for message in bodies[1]["messages"] if message.get("tool_call_id") == "read-method")
        assert "BODY_method" in read_material, read_material
        assert (await case.case.store.get_turn("t_reference")).status == "clarification_pending"
        assert (task.result().tokens_input, task.result().tokens_output) == (117, 24)
        saved = rows(case, "turn_events")
        cards = [json.loads(row["payload_json"]) for row in saved if row["event_type"] == "clarification_required"]
        assert len(cards) == 1
        question = await case.clarifications.get(cards[0]["clarification_id"])
        original = rows(case, "chat_reference_resolutions")
        pause = rows(case, "chat_reference_bi_continuations")[0]
        assert pause["checkpoint_version"] == 2 and pause["captured_iterations"] == 2
        request = await resolve_request(case, question)
        if boundary != "continue":
            if boundary == "disabled":
                await case.base.fixture.sources.instances.upsert(
                    user_id=request.user_id, name="method", source_type="builtin", source_path=str(case.base.fixture.root / "method"), enabled=False
                )
            else:
                import sqlite3

                with sqlite3.connect(case.path) as db:
                    db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
                retained = rows(case, "chat_reference_bi_continuations")[0]
                assert retained["ciphertext"] is None and retained["state"] == "closed"
            with pytest.raises(ValueError):
                await spawn_resume_turn(case.orchestrator, request, team_id=request.team_id, initial_seq=max(row["seq"] for row in saved))
            assert route.call_count == 2
            assert (await case.clarifications.get(question.request_id)).status == "resolved"
            assert len(rows(case, "chat_reference_model_invocations")) == 2
            return
        spawned = await spawn_resume_turn(
            case.orchestrator,
            request,
            team_id=request.team_id,
            initial_seq=max(row["seq"] for row in saved),
            resume_notice={"clarification_id": question.request_id},
        )
        await settle_worker(spawned.task)
        assert route.call_count == 3
    before = [message for message in bodies[1]["messages"] if message["role"] in {"assistant", "tool"}]
    after = [message for message in bodies[2]["messages"] if message["role"] in {"assistant", "tool"}]
    assert after[: len(before)] == before
    assert "BODY_method" in next(message["content"] for message in after if message.get("tool_call_id") == "read-method")
    assert rows(case, "chat_reference_resolutions") == original
    assert len(rows(case, "turn_agent_execution")) == 1
    assert len(rows(case, "chat_reference_model_invocations")) == 3
    assert (await case.case.store.get_turn("t_reference")).status == "completed"
    assert (spawned.task.result().tokens_input, spawned.task.result().tokens_output) == (13, 3)
    telemetry = [json.loads(row["payload_json"]) for row in rows(case, "turn_events") if row["event_type"] == "peer.iter"]
    # The sentinel ends its round before peer.iter; resume must start at 2, not reset to 1.
    assert [event["iter"] for event in telemetry] == [0, 2]
