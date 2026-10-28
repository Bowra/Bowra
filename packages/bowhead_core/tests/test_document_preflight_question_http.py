"""Native empty lookup, ordinary question and document planning on one Turn.

Only model/executor transports are controlled. Admission, original question
writes, resume hydration, document history and the fixed worker remain real.
"""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import httpx2
import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.documents.executor import PROFILE
from bowhead_core.documents.tool import register_document_tool
from bowhead_core.subagent import PROFILES
from bowhead_gateway.document_service import DocumentService
from bowhead_gateway.routes import clarifications
from test_document_pptx_text import _deck
from test_document_runner import IMAGE, HostWorker
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events
from bowhead_gateway import document_service, turn_service
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


async def prepare_document_http(case, monkeypatch, *, upload=True):
    enable_lead_writer(case, monkeypatch)
    # The controlled Router must agree with the authorized Team catalog.
    from bowhead_core.team_llm_config import REQUIRED_AGENT_TYPES

    await case.app.state.user_store.update_team_llm_config(
        case.fixture.scope.team_id,
        {
            "schema_version": 2,
            "providers": {
                "primary": {
                    "type": "openai_compatible",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "test-only",
                    "models": ["large"],
                }
            },
            "agents": {
                role: (
                    {"default": {"primary": {"provider": "primary", "model": "large"}}}
                    if role == "analyst"
                    else {"primary": {"provider": "primary", "model": "large"}}
                )
                for role in REQUIRED_AGENT_TYPES
            },
        },
    )
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "1")
    # Exact provider-call counts below exclude automatic memory capture's classifier call.
    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "0")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("template_document_submit", "clarification_ask")))
    orchestrator = case.app.state.orchestrator
    register_clarification_tool(orchestrator.tool_registry)
    orchestrator.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orchestrator.event_registry)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "document-question-uploads"))
    await artifacts.initialize()
    case.app.state.artifact_store = orchestrator.artifact_store = artifacts
    service = DocumentService(case.app.state)
    monkeypatch.setattr(document_service, "resolve_team_model_runtime", turn_service.resolve_team_model_runtime)
    case.app.state.document_service = orchestrator.document_service = service
    worker = HostWorker(case.path.parent / "document-worker")
    worker.url, worker.token = "http://document-executor", "test-only"
    monkeypatch.setattr(service, "executor", lambda: worker)
    register_document_tool(orchestrator.tool_registry, service=lambda: service)
    if not upload:
        return service, worker, None
    scope = case.fixture.scope
    attachment = await artifacts.put_bytes(
        content=_deck(),
        filename="template.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        created_by=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    with sqlite3.connect(case.path) as db:
        db.execute(
            "INSERT INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,created_at) VALUES (?,?,?,?)",
            (attachment.id, scope.workspace_id, scope.user_id, datetime.now(UTC).isoformat()),
        )
    return service, worker, attachment


@pytest.mark.parametrize(
    "answer_mode,round_count,boundary",
    [
        ("candidate", 1, "ordinary"),
        ("custom", 2, "ordinary"),
        ("skip", 1, "ordinary"),
        ("custom", 1, "duplicate_lookup"),
        ("custom", 1, "revoked"),
        ("custom", 1, "preworker_retry"),
        ("custom", 2, "new_legacy_question"),
    ],
)
async def test_empty_lookup_question_reaches_original_document_planner(http_start, monkeypatch, answer_mode, round_count, boundary):
    case = http_start
    service, worker, attachment = await prepare_document_http(case, monkeypatch)
    scope = case.fixture.scope
    original = "Use the attached PPTX as the template. Ask which audience and period to use, then prepare a draft and PDF."
    questions = ["Which audience?", "Which period?"]
    values = ["Board members", "Quarter two"]
    lead_calls, document_calls = [], []
    if boundary == "new_legacy_question":
        from bowhead_core.documents.request import DocumentScope

        document_scope = DocumentScope(team_id=scope.team_id, workspace_id=scope.workspace_id, user_id=scope.user_id)
        legacy_job_id, legacy_owner = await prepare_legacy_job(case, service, document_scope, attachment)

    def respond(request):
        body = json.loads(request.content)
        if not body.get("tools"):
            document_calls.append(body)
            message = {
                "role": "assistant",
                "content": json.dumps({"action": "ask", "reason": "comparison", "slots": ["s0"], "question": "Which comparison basis?"}),
            }
            reason = "stop"
        else:
            lead_calls.append(body)
            index = len(lead_calls) - 1
            if boundary == "duplicate_lookup" and index > 0:
                index -= 1
            if index < round_count * 2 and index % 2 == 0:
                name, arguments = "template_document_submit", {"action": "list_questions"}
            elif index < round_count * 2:
                number = index // 2
                name, arguments = (
                    "clarification_ask",
                    {"questions": [{"question": questions[number], "type": "single_select", "options": [values[number], "Unspecified"]}]},
                )
            else:
                assert index == round_count * 2, "A planning outcome must stop Lead without another paraphrase call"
                name, arguments = (
                    "template_document_submit",
                    {
                        "action": "generate",
                        "evidence": {"text": original},
                        "inputs": [
                            {"artifact_id": attachment.id, "role": "template", "evidence": {"text": "Use the attached PPTX as the template."}}
                        ],
                        "requested_outputs": ["source", "pdf"],
                    },
                )
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"index": 0, "id": f"document-call-{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                ],
            }
            reason = "tool_calls"
        usage = {"prompt_tokens": 17, "completion_tokens": 9}
        if not body.get("stream"):
            return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": reason}], "usage": usage})
        chunks = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage}]
        return httpx2.Response(
            200,
            text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://document-executor/document-runs/capabilities").respond(
            200, json={"profile": PROFILE, "image_id": IMAGE, "planner_profiles": ["pptx-grounded-plan-v5"]}
        )
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": scope.workspace_id,
                **({"session_id": "s_123456abcdef"} if boundary == "new_legacy_question" else {}),
                "message": original,
                "attachments": [attachment.id],
                "attachment_refs": [
                    {
                        "fileId": attachment.id,
                        "filename": attachment.filename,
                        "mime": attachment.mime,
                        "size": attachment.size,
                        "referenceKind": "upload",
                    }
                ],
            },
            headers={"Content-Type": "application/json"},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        original_cards = []
        decisions = []
        for index in range(round_count):
            if boundary == "new_legacy_question" and index == 1:
                retained = await service.jobs.questions_from_turn(scope=document_scope, turn_id=turn_id)
                assert retained == [{"job_id": legacy_job_id, "question": "Which comparison for the older document?"}]
                assert len(rows(case, "document_jobs")) == 1
                assert len(rows(case, "clarification_requests")) == 1
                assert rows(case, "document_dispatches") == []
                assert rows(case, "document_continuations") == []
                assert not document_calls and not worker.runs
                steps = [json.loads(row["payload_json"]) for row in rows(case, "turn_events") if row["event_type"] == "step_done"]
                assert any(legacy_job_id in json.dumps(step) for step in steps)
                generation = (await case.fixture.case.store.get_turn(turn_id)).execution_generation
                replay = await case.client.post(f"/api/v1/clarifications/{original_cards[0]}/resume_turn")
                assert replay.status_code in {200, 409}, replay.text
                from bowhead_gateway.routes.chat import _inflight_tasks

                assert not [task for task in _inflight_tasks if task.get_name().endswith(turn_id) and not task.done()]
                assert (await case.fixture.case.store.get_turn(turn_id)).execution_generation == generation == 2
                assert len(lead_calls) == 4 and rows(case, "document_continuations") == []
                original_card = await case.app.state.clarification_store.get(original_cards[0])
                assert original_card.status == "resolved"
                assert original_card.answers == [
                    {
                        "question": questions[0],
                        "source": "custom",
                        "answer": values[0],
                        "skipped": False,
                        "option_index": None,
                    }
                ]
                assert (original_card.turn_id, original_card.session_id, original_card.user_id, original_card.workspace_id) == (
                    turn_id,
                    accepted.json()["session_id"],
                    scope.user_id,
                    scope.workspace_id,
                )
                return
            assert len(rows(case, "document_jobs")) == int(boundary == "new_legacy_question"), "A question cannot admit a document"
            assert rows(case, "document_dispatches") == []
            cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
            assert len(cards) == index + 1
            card = next(row for row in cards if row["status"] == "pending")
            snapshot = json.loads(card["resume_context_json"])
            assert snapshot["__schema_version"] == (1 if boundary == "duplicate_lookup" else 2)
            if boundary != "duplicate_lookup":
                assert snapshot["question_round"]["round"] == index + 1
            assert snapshot["user_text"] == original
            original_cards.append(card["request_id"])
            value = None if answer_mode == "skip" else values[index]
            answer = {
                "question": questions[index],
                "source": answer_mode,
                "answer": value,
                "skipped": answer_mode == "skip",
                "option_index": 0 if answer_mode == "candidate" else None,
            }
            decisions.append({"question": questions[index], "answer": value})
            if boundary == "new_legacy_question":
                for state in ("planning", "rendering", "question"):
                    await service.delivery.transition(
                        scope=document_scope,
                        job_id=legacy_job_id,
                        owner=legacy_owner,
                        state=state,
                        **({"question": "Which comparison for the older document?"} if state == "question" else {}),
                    )
            if boundary == "preworker_retry":

                async def fail_before_worker(*_args, **_kwargs):
                    raise ValueError("injected_preworker_failure")

                with monkeypatch.context() as failure:
                    failure.setattr(turn_service, "spawn_turn", fail_before_worker)
                    refused = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"answers": [answer]})
                assert refused.status_code == 409, refused.text
                paused = await case.fixture.case.store.get_turn(turn_id)
                assert paused.status == "clarification_pending" and paused.execution_generation == 2
                assert len(lead_calls) == 2 and not document_calls
                assert rows(case, "document_jobs") == [] and rows(case, "document_dispatches") == []
            if boundary == "revoked":
                with sqlite3.connect(case.path) as db:
                    db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (scope.workspace_id, scope.user_id))
            resolved = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"answers": [answer]})
            if boundary == "revoked":
                assert resolved.status_code in {403, 404}, resolved.text
                assert len(lead_calls) == 2 and not document_calls and not worker.runs
                assert rows(case, "document_jobs") == [] and rows(case, "document_dispatches") == []
                retained = await case.app.state.clarification_store.get(card["request_id"])
                assert retained.status == "pending" and retained.answers is None
                return
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["resume_turn_id"] == turn_id
            await settled(turn_id)
        if boundary == "duplicate_lookup":
            assert not document_calls and not worker.runs
            jobs = rows(case, "document_jobs")
            assert len(jobs) == 1 and len(rows(case, "document_dispatches")) == 1
            assert rows(case, "document_planning_attempts") == []
            turn = await case.fixture.case.store.get_turn(turn_id)
            assert turn.status == "failed" and turn.error_kind == "document_history_unavailable"
            terminals = [json.loads(row["payload_json"]) for row in rows(case, "turn_events") if row["event_type"] == "done"]
            terminal = terminals[-1]
            assert terminal["reason_code"] == turn.error_kind
            assert terminal["diagnostics"]["retryable"] is False
            replay = await asyncio.wait_for(case.client.get(f"/api/v1/chat/stream/{turn_id}"), timeout=10)
            assert replay.status_code == 200, replay.text
            frames = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
            assert any(frame.get("diagnostics") == terminal["diagnostics"] for frame in frames)
            return
        assert len(document_calls) == 1, {
            "turn": await case.fixture.case.store.get_turn(turn_id),
            "jobs": rows(case, "document_jobs"),
            "executions": rows(case, "document_executions"),
            "steps": [row["payload_json"] for row in rows(case, "turn_events") if row["event_type"] == "step_done"],
        }
        model_input = json.loads(document_calls[0]["messages"][-1]["content"])
        assert json.loads(model_input["instruction"].rsplit("\n", 1)[1]) == {"decisions": decisions}
        jobs = rows(case, "document_jobs")
        assert len(jobs) == 1 and jobs[0]["origin_turn_id"] == turn_id
        assert len(rows(case, "document_dispatches")) == 1
        assert worker.runs == ["inventory", "apply_verify"]
        turn = await case.fixture.case.store.get_turn(turn_id)
        assert turn.status == "clarification_pending" and turn.execution_generation == round_count + 1 + (boundary == "preworker_retry")
        domain_cards = [row for row in rows(case, "clarification_requests") if row["request_id"] not in original_cards]
        assert len(domain_cards) == 1
        assert json.loads(domain_cards[0]["resume_context_json"])["__schema_version"] == 3
        before = len(lead_calls), len(document_calls), len(rows(case, "document_dispatches"))
        replay = await case.client.post(f"/api/v1/clarifications/{original_cards[-1]}/resume_turn")
        assert replay.status_code == 409, replay.text
        assert (len(lead_calls), len(document_calls), len(rows(case, "document_dispatches"))) == before


async def prepare_legacy_job(case, service, scope, attachment):
    """Admit earlier work before the current Turn, using original writers."""
    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.documents.request import DocumentProposal, InputRoleProposal, UserQuote

    text, old_turn = "Use this file as the template for the older document.", "t_legacy_document_fixture"
    session_id = "s_123456abcdef"
    await case.fixture.case.store.ensure_session(session_id=session_id, workspace_id=scope.workspace_id, user_id=scope.user_id)
    await case.fixture.case.store.record_turn(
        turn_id=old_turn,
        session_id=session_id,
        user_text=text,
        user_source=ChatTurnUserSource(
            turn_id=old_turn,
            session_id=session_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            user_text=text,
            attachment_ids=(attachment.id,),
            attachment_refs=({"fileId": attachment.id},),
            parts=None,
        ),
    )
    quote = UserQuote(start=0, text=text)
    receipt = await service.jobs.submit_from_turn(
        scope=scope,
        turn_id=old_turn,
        command_id="legacy-document-fixture",
        proposal=DocumentProposal(
            evidence=quote,
            inputs=(InputRoleProposal(artifact_id=attachment.id, role="template", evidence=quote, current_message_position=1),),
            requested_outputs=("source", "pdf"),
        ),
    )
    owner = await service.delivery.claim(scope=scope, job_id=receipt.job_id)
    assert owner is not None
    assert await case.fixture.case.store.mark_completed(
        turn_id=old_turn,
        intent=None,
        final_answer="The older request was accepted.",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0,
    )
    await case.fixture.case.store.finalize_turn(session_id=session_id, current_turn_id=old_turn, next_turn_id=None)
    return receipt.job_id, owner
