"""Ordinary HTTP helper/question continuation without a document-only Tool set."""

import asyncio
import json
from dataclasses import replace

import anyio
import httpx2
import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import ToolSpec
from bowhead_gateway.routes import clarifications
from bowhead_router.types import ToolSchema
from cryptography.fernet import Fernet
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


def response(body, message, finish):
    usage = {"prompt_tokens": 17, "completion_tokens": 9}
    if not body.get("stream"):
        return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": finish}], "usage": usage})
    chunks = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": finish}], "usage": usage}]
    return httpx2.Response(
        200,
        text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def call(call_id, name, arguments):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


@pytest.mark.parametrize(
    "mode",
    [
        "custom",
        "candidate",
        "multi",
        "partial",
        "skip",
        "lost_key",
        "reused_call",
        "second_question",
        "window_guard",
        "context_overflow",
        "mixed_prefix",
        "external_prefix",
        "capture_size",
        "question_batch",
        "question_batch_serial",
        "race",
        "streamed",
        "repair",
        "hard_reject",
        "narrative",
        "no_tools",
        "unclassified_return",
        "capture_internal",
        "changed_return",
        "reused_child",
        "reused_rejection",
        "protocol_repair",
        "self_closing_protocol_repair",
    ],
)
async def test_original_answer_continues_non_document_tools_without_replaying_helper(http_start, monkeypatch, caplog, mode):
    case = http_start
    live_events = []
    original_emit = events.TurnEventBus.emit

    async def observe_emit(bus, event):
        live_events.append(event)
        return await original_emit(bus, event)

    monkeypatch.setattr(events.TurnEventBus, "emit", observe_emit)
    own_tool = "skill_list"
    external_runs = []
    if mode == "external_prefix":
        # A write with an external effect: the resume continues from its
        # retained result and must not execute it a second time.
        own_tool, mode = "ticket_create", "mixed_prefix"

        async def create_ticket(args, ctx):
            external_runs.append(args)
            return {"ticket_id": "T-1"}

        schema = ToolSchema(name=own_tool, description="Create a ticket.", input_schema={"type": "object", "properties": {}})
        case.app.state.orchestrator.tool_registry.register(ToolSpec(schema=schema, handler=create_ticket))
    if mode == "question_batch_serial":
        monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "0")
        mode = "question_batch"
    if mode == "capture_size":
        monkeypatch.setattr("bowhead_core.ordinary_lead_checkpoint.MAX_CHECKPOINT_BYTES", 512)
        caplog.set_level("INFO", logger="bowhead_core.ordinary_lead_runtime")
    protocol_repair = mode in {"protocol_repair", "self_closing_protocol_repair"}
    if mode == "capture_internal":
        from bowhead_core import ordinary_lead_runtime

        async def fail_capture(**kwargs):
            raise RuntimeError("Internal capture implementation failed: password=raw-secret")

        monkeypatch.setattr(ordinary_lead_runtime, "capture_ordinary_lead_loop", fail_capture)
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    monkeypatch.setenv("BOWHEAD_PAYLOAD_SOFT_GATE", "1")
    if mode == "streamed":
        monkeypatch.setenv("BOWHEAD_LEAD_CANVAS_AUTOCHOP", "1")
    repair = mode in {"repair", "hard_reject", "narrative", "unclassified_return", "changed_return", "reused_child", "reused_rejection"}
    rejection_count = 5 if mode == "narrative" else 1
    refused_capture = mode in {"unclassified_return", "changed_return", "reused_child"}
    capture_buses = []
    if refused_capture:
        from bowhead_core import ordinary_lead_runtime

        capture = ordinary_lead_runtime.capture_ordinary_lead_loop

        async def observe_capture(**kwargs):
            capture_buses.append(kwargs["bus"])
            return await capture(**kwargs)

        monkeypatch.setattr(ordinary_lead_runtime, "capture_ordinary_lead_loop", observe_capture)
    if mode in {"unclassified_return", "changed_return"}:
        from bowhead_core import ordinary_lead_runtime

        record_rejection = ordinary_lead_runtime.note_ordinary_return_rejection

        def incomplete_evidence(ctx, result):
            if mode == "unclassified_return":
                return result
            record_rejection(ctx, result)
            return {**result, "error": "A changed result cannot reuse the original rejection evidence."}

        monkeypatch.setattr(ordinary_lead_runtime, "note_ordinary_return_rejection", incomplete_evidence)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask", "skill_list", own_tool)))
    orchestrator = case.app.state.orchestrator
    context_reductions = []
    overflow_raised = False
    if mode in {"window_guard", "context_overflow"}:
        from bowhead_router.errors import LLMTargetFailure, ProviderFailureKind, StreamingTargetsExhaustedError

        from bowhead_core import context_assembly

        original_guard = context_assembly.guard_window
        original_stream = orchestrator.router.stream
        monkeypatch.setattr(orchestrator.router, "context_window_for", lambda *args, **kwargs: 1_000_000)

        def reduce_resumed_context(messages, **kwargs):
            answered = any(message.role == "tool" and message.tool_call_id == "question-native" for message in messages)
            if answered and not context_reductions and (mode == "window_guard" or overflow_raised):
                cleared = original_guard(messages, budget_tokens=1, extra_tokens=0, estimate=len, hysteresis=1.0)
                assert cleared > 0
                context_reductions.append(cleared)
                return cleared
            return original_guard(messages, **kwargs)

        async def stream_with_overflow(agent_type, request, **kwargs):
            nonlocal overflow_raised
            if (
                mode == "context_overflow"
                and not overflow_raised
                and any(message.role == "tool" and message.tool_call_id == "question-native" for message in request.messages)
            ):
                overflow_raised = True
                raise StreamingTargetsExhaustedError(
                    [
                        LLMTargetFailure(
                            provider="primary",
                            model="large",
                            provider_family="openai",
                            mode="stream",
                            target_index=1,
                            attempt=1,
                            kind=ProviderFailureKind.CONTEXT_OVERFLOW,
                            retryable=False,
                            status_code=400,
                        )
                    ]
                )
            async for chunk in original_stream(agent_type, request, **kwargs):
                yield chunk

        monkeypatch.setattr(context_assembly, "guard_window", reduce_resumed_context)
        monkeypatch.setattr(orchestrator.router, "stream", stream_with_overflow)
    register_clarification_tool(orchestrator.tool_registry)
    orchestrator.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orchestrator.event_registry)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "ordinary-question-uploads"))
    await artifacts.initialize()
    case.app.state.artifact_store = orchestrator.artifact_store = artifacts
    scope = case.fixture.scope
    original = "Inspect the request, ask which audience to address, then list the available Skills."
    options = ["Board members", "Operations"] if mode in {"candidate", "multi"} else []
    question = {
        "question": "Which audience?",
        "options": options,
        "type": "multi_select" if mode == "multi" else "single_select" if options else "free_text",
    }
    questions = ([{"question": "Which date?", "options": [], "type": "free_text"}] if mode == "partial" else []) + [question]
    lead_calls, helper_calls = [], []

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            index = len(lead_calls)
            if mode in {"mixed_prefix", "question_batch", "capture_size"}:
                if mode == "mixed_prefix":
                    if index == 1:
                        return response(body, call("list-before-helper", own_tool, {}), "tool_calls")
                    index -= 1
                elif index == 3:
                    return response(body, {"role": "assistant", "content": "The inspection is complete; the audience is still unspecified."}, "stop")
            if protocol_repair and index == 2:
                from test_stray_clarification_ask import ARGUMENT_TEXT, SELF_CLOSING_CALL

                text = SELF_CLOSING_CALL if mode == "self_closing_protocol_repair" else ARGUMENT_TEXT
                return response(body, {"role": "assistant", "content": text}, "stop")
            if protocol_repair and index > 2:
                index -= 1
            if index == 1:
                message = call(
                    "helper-native",
                    "subagent_spawn",
                    {"profile": case.fixture.case.args["reference_start"].profile_name, "task": "Inspect this request.", "delegation_role": "helper"},
                )
            elif index == 2:
                message = call("question-native", "clarification_ask", {"questions": questions})
                if mode == "question_batch":
                    message["tool_calls"].extend(call("parallel-list", "skill_list", {})["tool_calls"])
                    message["tool_calls"][1]["index"] = 1
            elif index == 3:
                message = (
                    call("second-question", "clarification_ask", {"questions": [question]})
                    if mode == "second_question"
                    else call(
                        "helper-native" if mode == "reused_call" else "invalid-return" if mode == "reused_rejection" else "new-list", "skill_list", {}
                    )
                )
            else:
                assert index == 4
                return response(body, {"role": "assistant", "content": "The available Skills have been listed for the selected audience."}, "stop")
            if mode == "streamed":
                message["content"] = "## Inspection progress\n\nChecking the requested audience.\n"
            return response(body, message, "tool_calls")
        if "subagent_return" in names:
            helper_calls.append(body)
            if mode == "no_tools":
                assert len(helper_calls) == 1
                return response(body, {"role": "assistant", "content": "The request needs an audience before its Skill list."}, "stop")
            if repair:
                if len(helper_calls) <= rejection_count:
                    rejection_id = "invalid-return" if len(helper_calls) == 1 else f"invalid-return-{len(helper_calls)}"
                    return response(
                        body,
                        call(
                            "helper-return" if mode == "reused_child" else rejection_id,
                            "subagent_return",
                            {
                                "payload": {}
                                if mode == "hard_reject"
                                else {"delivery_surface": "chat", "title": f"Synthetic correction {len(helper_calls)}"}
                            },
                        ),
                        "tool_calls",
                    )
                assert len(helper_calls) == rejection_count + 1
                if mode == "narrative":
                    return response(body, {"role": "assistant", "content": "The request needs an audience before its Skill list."}, "stop")
            else:
                assert len(helper_calls) == 1
            return response(
                body,
                call(
                    "helper-return",
                    "subagent_return",
                    {"payload": {"delivery_surface": "chat", "exec_summary": "The request needs an audience before its Skill list."}},
                ),
                "tool_calls",
            )
        return response(body, {"role": "assistant", "content": "Skill selection"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": original})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        if mode in {"question_batch", "capture_size"}:
            assert cards == []
            assert len(helper_calls) == 1
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "completed", turn["error"]
            refusal = next(message for message in lead_calls[-1]["messages"] if message.get("tool_call_id") == "question-native")
            assert json.loads(refusal["content"])["error_kind"] == "clarification_capture_unavailable"
            native = [
                json.loads(row["payload_json"])
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id
                and row["event_type"] == "tool_result"
                and json.loads(row["payload_json"]).get("call_id") == "question-native"
            ]
            assert len(native) == 1
            assert native[0]["ok"] is False
            steps = [
                json.loads(row["payload_json"])
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id
                and row["event_type"] == "step_done"
                and json.loads(row["payload_json"]).get("call_id") == "question-native"
            ]
            assert len(steps) == 1
            assert steps[0]["state"] == "failure" and steps[0]["ok"] is False
            assert steps[0]["error"]["type"] == "clarification_capture_unavailable"
            assert steps[0]["output_preview"] == "Question not created · add the needed details in chat"
            assert [
                event.payload for event in live_events if event.event_type == "tool_result" and event.payload.get("call_id") == "question-native"
            ] == native
            assert [
                event.payload for event in live_events if event.event_type == "step_done" and event.payload.get("call_id") == "question-native"
            ] == steps
            completed_tools = [
                json.loads(row["payload_json"])["tool_name"]
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id and row["event_type"] == "tool_result"
            ]
            assert completed_tools.count("skill_list") == (0 if mode == "capture_size" else 1)
            assert "_clarification_pending" not in json.dumps(native)
            assert "clarification_capture_unavailable" in json.dumps(native)
            with anyio.fail_after(5):
                replay = await case.client.get(accepted.json()["stream_url"])
            assert replay.status_code == 200
            assert "clarification_capture_unavailable" in replay.text
            assert steps[0]["output_preview"] in replay.text
            assert "clarification_required" not in replay.text
            if mode == "capture_size":
                diagnostics = [record for record in caplog.records if "ordinary_lead.question_capture_unavailable" in record.message]
                assert len(diagnostics) == 1
                assert "failure_site=bowhead_core.ordinary_lead_checkpoint.encode:" in diagnostics[0].message
                assert "next_iteration=2" in diagnostics[0].message
                assert original not in diagnostics[0].message and question["question"] not in diagnostics[0].message
            return
        if mode == "capture_internal":
            assert cards == []
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "failed" and turn["error"] == "lead_internal_error"
            terminal = [
                json.loads(row["payload_json"])
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id and row["event_type"] == "turn_failed"
            ][-1]
            assert terminal["diagnostics"]["safe_detail"] == "phase=chat; failure_class=internal"
            assert terminal["diagnostics"]["retryable"] is True
            assert "raw-secret" not in str(terminal)
            return
        if refused_capture:
            assert cards == []
            assert len(lead_calls) == 2 and len(helper_calls) == 2
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "failed" and turn["error"] == "clarification_resume_unavailable"
            persisted = [
                json.loads(row["payload_json"])
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id and row["event_type"] in {"turn_failed", "done"}
            ]
            failures = [payload["diagnostics"] for payload in persisted if payload.get("diagnostics")]
            assert len(failures) == 2
            assert failures[0] == failures[1]
            assert failures[0]["turn_id"] == turn_id
            assert failures[0]["safe_detail"] == "phase=clarification_capture; failure_class=configuration"
            assert failures[0]["retryable"] is False
            assert persisted[-1]["recovery_action"] == "edit_and_resend"
            assert len(capture_buses) == 1
            bus = capture_buses[0]
            live = [
                event.payload["diagnostics"]
                for event in bus.snapshot()
                if event.event_type in {"turn_failed", "done"} and event.payload.get("diagnostics")
            ]
            assert live == failures
            with anyio.fail_after(5):
                replay = await case.client.get(accepted.json()["stream_url"])
            assert replay.status_code == 200
            replayed = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
            replayed_diagnostics = [
                payload["diagnostics"]
                for payload in replayed
                if (payload.get("diagnostics") or {}).get("occurrence_id") == failures[0]["occurrence_id"]
            ]
            assert replayed_diagnostics == failures
            return
        assert len(cards) == 1 and json.loads(cards[0]["resume_context_json"])["__schema_version"] == 4
        if protocol_repair:
            assert len(lead_calls) == 3 and len(helper_calls) == 1
            paused = [
                json.loads(row["payload_json"])
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id and row["event_type"] in {"clarification_required", "done"}
            ]
            assert len(paused) == 2 and all(event["narrative_superseded"] for event in paused)
        card_id = cards[0]["request_id"]
        if mode == "streamed":
            paused_sections = [
                json.loads(row["payload_json"])["section_id"]
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id and row["event_type"] == "canvas.section_streaming"
            ]
            assert paused_sections
        visible = await case.client.get(f"/api/v1/clarifications/{card_id}")
        assert visible.status_code == 200 and visible.json()["questions"] == questions
        if mode in {"candidate", "multi"}:
            answers = [
                {"question": question["question"], "source": "candidate", "answer": option, "option_index": index}
                for index, option in enumerate(options[: 2 if mode == "multi" else 1])
            ]
            answer_payload = {"answers": answers}
        elif mode == "skip":
            answer_payload = {"skip": True}
        else:
            answer_payload = {"answers": [{"question": question["question"], "source": "custom", "answer": "Board members"}]}
        if mode == "lost_key":
            monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
        if mode == "race":
            concurrent = await asyncio.gather(*[case.client.post(f"/api/v1/clarifications/{card_id}/resolve", json=answer_payload) for _ in range(2)])
            assert all(result.status_code in {200, 409} for result in concurrent)
            answered = next(result for result in concurrent if result.status_code == 200)
        else:
            answered = await case.client.post(f"/api/v1/clarifications/{card_id}/resolve", json=answer_payload)
        if mode == "lost_key":
            assert answered.status_code == 409
            assert len(lead_calls) == 2 and len(helper_calls) == 1
            return
        assert answered.status_code == 200, answered.text
        assert answered.json()["resume_turn_id"] == turn_id
        await settled(turn_id)
        if mode == "streamed":
            all_sections = [
                json.loads(row["payload_json"])["section_id"]
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id and row["event_type"] == "canvas.section_streaming"
            ]
            assert all_sections[: len(paused_sections)] == paused_sections
            assert len(all_sections) > len(paused_sections)
            assert len(all_sections) == len(set(all_sections)), "Resuming a Turn must not reopen its previous preview sections"
        assert len(helper_calls) == (rejection_count + 1 if repair else 1)
        refused = mode in {"reused_call", "reused_rejection"}
        # Lead's own call before the helper stays in the retained transcript.
        own = 1 if mode == "mixed_prefix" else 0
        assert len(lead_calls) == (3 if refused else 5 if protocol_repair else 4) + own
        messages = lead_calls[(3 if protocol_repair else 2) + own]["messages"]
        if own:
            listed = [m for m in lead_calls[1]["messages"] if m.get("tool_call_id") == "list-before-helper"]
            assert len(listed) == 1 and listed == [m for m in messages if m.get("tool_call_id") == "list-before-helper"]
        original_helper = [
            message for message in lead_calls[1 + own]["messages"] if message.get("role") == "tool" and message.get("tool_call_id") == "helper-native"
        ]
        retained_helper = [message for message in messages if message.get("role") == "tool" and message.get("tool_call_id") == "helper-native"]
        assert len(original_helper) == len(retained_helper) == 1
        if mode in {"window_guard", "context_overflow"}:
            assert len(context_reductions) == 1
            assert retained_helper[0]["content"] != original_helper[0]["content"]
            assert context_assembly.is_cleared(retained_helper[0]["content"])
        else:
            assert retained_helper == original_helper
        answered_tools = [message for message in messages if message.get("role") == "tool" and message.get("tool_call_id") == "question-native"]
        assert len(answered_tools) == 1
        typed = json.loads(answered_tools[0]["content"])["answers"]
        assert all(answer["question"] == question["question"] for answer in typed)
        assert typed[0]["source"] == ("skip" if mode == "skip" else "candidate" if options else "custom")
        results = [
            json.loads(row["payload_json"]) for row in rows(case, "turn_events") if row["turn_id"] == turn_id and row["event_type"] == "tool_result"
        ]
        expected_returns = rejection_count if mode == "narrative" else 0 if mode == "no_tools" else 2 if repair else 1
        assert sum(result.get("tool_name") == "subagent_return" for result in results) == expected_returns
        assert sum(result.get("tool_name") == "skill_list" for result in results) == (0 if refused or mode == "second_question" else 1) + (
            own and own_tool == "skill_list"
        )
        assert sum(result.get("tool_name") == own_tool for result in results) >= own
        assert len(external_runs) == (own if own_tool == "ticket_create" else 0)
        if refused:
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "failed" and turn["error"] == "clarification_resume_unavailable"
            failures = [
                json.loads(row["payload_json"])
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id and row["event_type"] == "turn_failed"
            ]
            assert failures[-1]["reason"] == turn["error"]
            assert failures[-1]["diagnostics"]["retryable"] is False
        if mode in {"window_guard", "context_overflow"}:
            saved = next(row for row in rows(case, "clarification_requests") if row["request_id"] == card_id)
            assert saved["resume_context_json"] == cards[0]["resume_context_json"]
        if not refused:
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "completed", turn["error"]
        if mode == "second_question":
            refusal = next(message for message in lead_calls[-1]["messages"] if message.get("tool_call_id") == "second-question")
            assert "clarification_refused_on_resume" in refusal["content"]
            assert len([row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]) == 1
        source = await orchestrator.session_store.get_chat_turn_user_source(turn_id=turn_id, user_id=scope.user_id, workspace_id=scope.workspace_id)
        assert source.user_text == original
