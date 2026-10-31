"""Real ordinary dispatches prove multi-helper card and refusal boundaries."""

import asyncio
import json
from dataclasses import replace

import httpx2
import pytest
import respx
from bowhead_core.agent_execution_binding import AgentExecutionBindingError, AgentExecutionBindingStore
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.ordinary_lead_continuation import canonical_json
from bowhead_core.subagent import PROFILES
from bowhead_gateway.routes import clarifications
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events, ordinary_lead_runtime
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("reuse_history_call_id", [False, True])
async def test_new_turn_retains_history_without_replaying_previous_helpers(http_start, monkeypatch, reuse_history_call_id):
    """A prior accepted Turn is model context, not this Turn's execution prefix."""
    case, scope = http_start, http_start.fixture.scope
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    orch = case.app.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    register_clarification_tool(orch.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    lead_calls, helper_calls = [], []
    question = "Which audience?"

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" not in names:
            if "subagent_return" in names:
                helper_calls.append(body)
            return response(body, {"role": "assistant", "content": "The retained synthetic profit is 42."}, "stop")
        lead_calls.append(body)
        index = len(lead_calls)
        if index in {1, 3, 4}:
            historical_ids = [tool["id"] for message in body["messages"] for tool in message.get("tool_calls") or []]
            call_id = historical_ids[0] if reuse_history_call_id and index == 3 else f"helper-{index}"
            message = call(
                call_id,
                "subagent_spawn",
                {"profile": case.fixture.case.args["reference_start"].profile_name, "task": "Inspect synthetic profit.", "delegation_role": "helper"},
            )
        elif index == 5:
            message = call("question", "clarification_ask", {"questions": [{"question": question, "type": "free_text", "options": []}]})
        else:
            if index == 6:
                assert "Board members" in json.dumps(body)
                assert "First independent task" in json.dumps(body)
            return response(body, {"role": "assistant", "content": "The synthetic profit is 42."}, "stop")
        return response(body, message, "tool_calls")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        first = await case.client.post("/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": "First independent task"})
        assert first.status_code == 200, first.text
        await settled(first.json()["turn_id"])
        assert next(row for row in rows(case, "turns") if row["id"] == first.json()["turn_id"])["status"] == "completed"
        second = await case.client.post(
            "/api/v1/chat/send",
            json={
                "session_id": first.json()["session_id"],
                "workspace_id": scope.workspace_id,
                "message": "Inspect two parts, then ask for an audience.",
            },
        )
        assert second.status_code == 200, second.text
        turn_id = second.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1, [(row["status"], row["error"]) for row in rows(case, "turns") if row["id"] == turn_id]
        resolved = await case.client.post(
            f"/api/v1/clarifications/{cards[0]['request_id']}/resolve",
            json={"answers": [{"question": question, "answer": "Board members", "source": "custom"}]},
        )
        assert resolved.status_code == 200, resolved.text
        await settled(turn_id)
        assert next(row for row in rows(case, "turns") if row["id"] == turn_id)["status"] == "completed"
        assert len(lead_calls) == 6 and len(helper_calls) == 3
        assert len([row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id and row["dispatch_key"].startswith("call:")]) == 2
        head = next(row for row in rows(case, "model_context_conversation_heads") if row["session_id"] == first.json()["session_id"])
        assert head["published_turn_id"] == turn_id


@pytest.mark.parametrize(
    "boundary",
    [
        *[
            "parallel_" + case
            for case in (
                "race",
                "failed_first",
                "missing_binding",
                "duplicate_binding",
                "foreign_binding",
                "changed_result",
                "first_denied_race",
                "commit_disabled",
            )
        ],
        "race",
        "conflicting_race",
        "skip",
        "disabled",
        "commit_disabled",
        "failed_first",
        "system",
        "system_only",
        "system_failed",
        "provider_error",
        "single_system",
        "system_commit_disabled",
        "single_failed",
        "system_disabled",
        "system_changed_race",
        "terminal_write_failed",
        "unknown_reason",
        "system_gate_disabled",
        "first_denied",
        "first_denied_race",
        "missing_binding",
        "swapped_binding",
        "changed_result",
        "second_question",
    ],
)
async def test_original_card_retains_all_helpers_and_refuses_unproven_prefixes(http_start, monkeypatch, boundary):
    case = http_start
    parallel = boundary.startswith("parallel_")
    boundary = boundary.removeprefix("parallel_")
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.registry import AgentRegistry

    monkeypatch.setitem(PROFILES, "kb-analyst", manifest_to_profile(AgentRegistry.bootstrap().get("kb-analyst")))
    helper_count = 1 if boundary.startswith("single_") else 2
    question_round = helper_count + 1 - int(parallel)
    failed_first = boundary in {"failed_first", "single_failed", "system_failed", "provider_error"}
    system_first = boundary in {"system_only", "system_failed", "single_system", "system_disabled", "system_changed_race"}
    case.client.headers["Content-Type"] = "application/json"
    scope = case.fixture.scope
    enable_lead_writer(case, monkeypatch)

    if boundary in {"disabled", "system_gate_disabled", "provider_error"}:
        monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
    else:
        monkeypatch.delenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", raising=False)
    monkeypatch.setenv("BOWHEAD_PI_K_ENABLED", "0")
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask"), max_iterations=8))
    orch = case.app.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    register_clarification_tool(orch.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    lead_calls, helper_calls = [], []
    question = {"question": "Which audience?", "type": "free_text", "options": []}
    capture = ordinary_lead_runtime.capture_ordinary_lead_loop

    async def capture_boundary(**kwargs):
        state = kwargs["ctx"].ordinary_lead_state
        if boundary == "missing_binding":
            state.helper_bindings.pop(0)
        elif boundary == "duplicate_binding":
            state.helper_bindings[1]["dispatch_key"] = state.helper_bindings[0]["dispatch_key"]
        elif boundary == "foreign_binding":
            state.helper_bindings[1]["dispatch_key"] = "call:foreign"
        elif boundary == "swapped_binding":
            state.helper_bindings.reverse()
        elif boundary == "changed_result":
            kwargs["invocations"][0].result["final_answer"] = "A result changed after the native message."
        elif boundary == "unknown_reason":
            state.helper_bindings[0]["source_kind"] = "system"
            state.helper_bindings[0]["outcome"]["reason"] = None
        checkpoint = await capture(**kwargs)
        if boundary in {"commit_disabled", "system_commit_disabled"}:
            monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
        return checkpoint

    monkeypatch.setattr(ordinary_lead_runtime, "capture_ordinary_lead_loop", capture_boundary)

    if boundary == "terminal_write_failed":
        from bowhead_core.agent_collaboration import CollaborationStore

        mark_terminal = AgentExecutionBindingStore.mark_terminal
        complete = CollaborationStore.complete

        async def lose_terminal_write(self, execution_id, **kwargs):
            if len(helper_calls) == 1:
                raise OSError("Synthetic terminal write failure")
            return await mark_terminal(self, execution_id, **kwargs)

        monkeypatch.setattr(AgentExecutionBindingStore, "mark_terminal", lose_terminal_write)

        async def lose_collaboration_terminal_write(self, worker, **kwargs):
            if len(helper_calls) == 1:
                raise OSError("Synthetic terminal write failure")
            return await complete(self, worker, **kwargs)

        monkeypatch.setattr(CollaborationStore, "complete", lose_collaboration_terminal_write)

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            index = len(lead_calls)
            if parallel:
                if index > 1:
                    index += 1
                else:
                    message = call(
                        "helper-1",
                        "subagent_spawn",
                        {
                            "profile": case.fixture.case.args["reference_start"].profile_name,
                            "task": "Inspect part 1.",
                            "delegation_role": "helper",
                        },
                    )
                    message["tool_calls"].extend(
                        call(
                            "helper-2",
                            "subagent_spawn",
                            {
                                "profile": case.fixture.case.args["reference_start"].profile_name,
                                "task": "Inspect part 2.",
                                "delegation_role": "helper",
                            },
                        )["tool_calls"]
                    )
                    message["tool_calls"][1]["index"] = 1
                    return response(body, message, "tool_calls")
            if index <= helper_count:
                profile = case.fixture.case.args["reference_start"].profile_name
                if (system_first and index == 1) or (boundary in {"system", "system_gate_disabled", "system_commit_disabled"} and index == 2):
                    profile = "compute"
                elif boundary == "system_only" and index == 2:
                    profile = "kb-analyst"
                message = call(
                    f"helper-{index}", "subagent_spawn", {"profile": profile, "task": f"Inspect part {index}.", "delegation_role": "helper"}
                )
            elif index == helper_count + 1 or (boundary == "second_question" and index == helper_count + 2):
                message = call(f"question-{index}", "clarification_ask", {"questions": [question]})
            else:
                assert index == helper_count + (3 if boundary == "second_question" else 2)
                return response(body, {"role": "assistant", "content": "Both inspections are retained for the selected audience."}, "stop")
            return response(body, message, "tool_calls")
        if "subagent_return" in names:
            helper_calls.append(body)
            if boundary == "provider_error" and len(lead_calls) == 1:
                return httpx2.Response(503, json={"error": {"message": "Synthetic transport failure"}})
            if boundary == "system_iteration_limit" and len(lead_calls) == 1:
                return response(body, call(f"invalid-return-{len(helper_calls)}", "subagent_return", {"payload": {}}), "tool_calls")
            if failed_first and boundary != "system_iteration_limit" and len(helper_calls) == 1:
                return response(body, {"role": "assistant", "content": ""}, "stop")
            return response(
                body,
                call("same-child-id", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "Inspection complete."}}),
                "tool_calls",
            )
        return response(body, {"role": "assistant", "content": "Inspection"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": "Inspect both parts, then ask for an audience."}
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        executions = [
            item
            for item in rows(case, "turn_agent_execution")
            if item["turn_id"] == turn_id and item["dispatch_key"] != "collaboration-lead:" + turn_id
        ]
        if boundary == "provider_error":
            # Physical invocation ownership stops the original worker before
            # another helper/question. A failed row is not settlement evidence.
            assert len(executions) == 1 and executions[0]["status"] == "failed"
            assert len(lead_calls) == 1
            assert not [item for item in rows(case, "clarification_requests") if item["turn_id"] == turn_id]
            assert next(item for item in rows(case, "turns") if item["id"] == turn_id)["status"] == "failed"
            return
        helper_model_calls = len(helper_calls)
        assert len(executions) == helper_count
        if boundary == "system_iteration_limit":
            assert helper_model_calls == helper_count + 1
        elif boundary != "provider_error":
            assert helper_model_calls == helper_count
        if failed_first:
            assert sorted(item["status"] for item in executions) == (["completed"] * (helper_count - 1) + ["failed"])
        elif boundary == "terminal_write_failed":
            assert sorted(item["status"] for item in executions) == ["cancelled", "completed"]
        else:
            assert all(item["status"] == "completed" for item in executions)
        if boundary == "system":
            assert {item["agent_source_kind"] for item in executions} == {"team", "system"}
        cards = [item for item in rows(case, "clarification_requests") if item["turn_id"] == turn_id]
        refused_capture = boundary in {
            "disabled",
            "commit_disabled",
            "missing_binding",
            "duplicate_binding",
            "foreign_binding",
            "swapped_binding",
            "changed_result",
            "system_gate_disabled",
            "system_commit_disabled",
            "terminal_write_failed",
            "unknown_reason",
            "provider_error",
        }
        if refused_capture:
            assert cards == []
            assert len(lead_calls) == question_round
            failed = next(item for item in rows(case, "turns") if item["id"] == turn_id)
            assert failed["status"] == "failed" and failed["error"] == "clarification_resume_unavailable"
            diagnostics = [
                json.loads(item["payload_json"])["diagnostics"]
                for item in rows(case, "turn_events")
                if item["turn_id"] == turn_id and item["event_type"] in {"turn_failed", "done"}
            ]
            assert len(diagnostics) == 2 and diagnostics[0] == diagnostics[1]
            assert diagnostics[0]["safe_detail"] == "phase=clarification_capture; failure_class=configuration"
            assert diagnostics[0]["retryable"] is False
            return
        assert len(cards) == 1
        card = cards[0]
        if system_first or boundary == "system" or failed_first:
            from bowhead_core._db import open_shared_ctx
            from bowhead_core.ordinary_lead_continuation import read_ordinary_card_in_tx, reopen_ordinary_checkpoint

            async with open_shared_ctx(case.path) as db:
                await db.execute("BEGIN")
                checkpoint = reopen_ordinary_checkpoint(await read_ordinary_card_in_tx(db, card_id=card["request_id"]))
            assert checkpoint.loop_budget is not None
            assert len(checkpoint.helper_materials()) == helper_count
            if failed_first:
                failed_index = (
                    next(i for i, helper in enumerate(checkpoint.helper_materials()) if helper["outcome"]["status"] == "failed") if parallel else 0
                )
                assert checkpoint.helper_materials()[failed_index]["outcome"]["reason"] == "empty_answer"
                assert checkpoint.invocations()[failed_index].result["ok"] is False
        if boundary == "system_disabled":
            original_compute = PROFILES["compute"]
            monkeypatch.delitem(PROFILES, "compute")
        original_reader = AgentExecutionBindingStore.read_settled_helper_in_connection
        first_id = next(item["id"] for item in executions if item["dispatch_key"] == "call:helper-1")
        deny_first = boundary == "first_denied"

        async def current_reader(self, db, **kwargs):
            # Fault-inject one execution's current authority, preserving the
            # real reader for the other. Its policy semantics have own tests.
            if deny_first and kwargs["execution_id"] == first_id:
                raise AgentExecutionBindingError("agent_not_runnable")
            return await original_reader(self, db, **kwargs)

        monkeypatch.setattr(AgentExecutionBindingStore, "read_settled_helper_in_connection", current_reader)
        if boundary in {"first_denied_race", "system_changed_race"}:
            original_validate = ordinary_lead_runtime.validate_ordinary_retained_materials

            async def deny_after_preflight(ctx, checkpoint):
                nonlocal deny_first
                await original_validate(ctx, checkpoint)
                if getattr(ctx, "ordinary_lead_state", None) is not None:
                    if boundary == "system_changed_race":
                        monkeypatch.setitem(PROFILES, "compute", replace(PROFILES["compute"], skill_invoke_allowlist=("different-grant",)))
                    else:
                        deny_first = True

            monkeypatch.setattr(ordinary_lead_runtime, "validate_ordinary_retained_materials", deny_after_preflight)
        answer = {"skip": True} if boundary == "skip" else {"answers": [{"question": question["question"], "source": "custom", "answer": "Board"}]}
        url = f"/api/v1/clarifications/{card['request_id']}/resolve"
        monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
        attempts = [answer]
        if boundary == "race":
            attempts.append(answer)
        elif boundary in {"conflicting_race", "system"}:
            attempts.append({"answers": [{"question": question["question"], "source": "custom", "answer": "Analysts"}]})
        responses = await asyncio.gather(*[case.client.post(url, json=item) for item in attempts])
        assert all(item.status_code in {200, 409} for item in responses)
        assert any(item.status_code == (409 if boundary in {"first_denied", "system_disabled"} else 200) for item in responses)
        if any(item.status_code == 200 for item in responses):
            await settled(turn_id)
        retained = next(item for item in rows(case, "clarification_requests") if item["request_id"] == card["request_id"])
        assert retained["status"] == "resolved" and retained["resume_context_json"] == card["resume_context_json"]
        assert len(helper_calls) == helper_model_calls
        assert [
            item
            for item in rows(case, "turn_agent_execution")
            if item["turn_id"] == turn_id and item["dispatch_key"] != "collaboration-lead:" + turn_id
        ] == executions
        if boundary in {"first_denied", "first_denied_race", "system_disabled", "system_changed_race"}:
            assert retained["injected_at"] is None and retained["continuation_generation"] is None
            assert len(lead_calls) == question_round
            if boundary == "system_disabled":
                history = await case.client.get(f"/api/v1/clarifications/turn/{turn_id}")
                assert history.status_code == 200 and history.json()["resume_failure"]["request_id"] == card["request_id"]
                monkeypatch.setitem(PROFILES, "compute", original_compute)
                restored_history = await case.client.get(f"/api/v1/clarifications/turn/{turn_id}")
                assert restored_history.status_code == 200 and restored_history.json()["resume_failure"] is None
                assert len(lead_calls) == question_round and len(helper_calls) == helper_model_calls
        else:
            assert retained["continuation_generation"] == 2 and retained["injected_at"] is not None
            messages = lead_calls[question_round]["messages"]
            answers = [item for item in messages if item.get("tool_call_id") == f"question-{helper_count + 1}"]
            assert len(answers) == 1
            if boundary in {"conflicting_race", "system"}:
                winning_answer = json.loads(retained["answers_json"])
                assert winning_answer[0]["answer"] in {"Board", "Analysts"}
                assert json.loads(answers[0]["content"])["answers"] == winning_answer
                for item in responses:
                    if item.status_code == 200:
                        assert item.json()["answers"] == winning_answer
                replay = await case.client.post(
                    url, json={"answers": [{"question": question["question"], "source": "custom", "answer": "Changed after resolution"}]}
                )
                assert replay.status_code == 200 and replay.json()["answers"] == winning_answer
                assert len(lead_calls) == question_round + (2 if boundary == "second_question" else 1) and len(helper_calls) == helper_count
            else:
                assert ("skip" if boundary == "skip" else "Board") in answers[0]["content"]
            assert len([item for item in messages if item.get("name") == "subagent_spawn"]) == helper_count
            if boundary == "second_question":
                assert len([item for item in rows(case, "clarification_requests") if item["turn_id"] == turn_id]) == 1
                refusal = next(item for item in lead_calls[-1]["messages"] if item.get("tool_call_id") == f"question-{helper_count + 2}")
                assert "clarification_refused_on_resume" in refusal["content"]
                assert next(item for item in rows(case, "turns") if item["id"] == turn_id)["status"] == "completed"
            else:
                assert next(item for item in rows(case, "turns") if item["id"] == turn_id)["status"] == "completed"
        assert "Both inspections" not in canonical_json(card["resume_context_json"])
