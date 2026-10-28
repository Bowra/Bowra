"""Real HTTP answer, dispatch, TurnRunner and domain service on one original Turn.

Only provider/executor HTTP uses the document fixture's controlled transport;
its fixed native inventory/compiler, authorization and storage remain real.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.document_chat_runtime import worker_from_context
from bowhead_core.documents.continuation import DocumentRuntimeSnapshot
from bowhead_core.orchestrator import Orchestrator
from bowhead_core.tool_registry import build_default_registry
from bowhead_gateway.routes.chat import _inflight_tasks
from test_document_clarification_http import http_case as http_case
from test_document_clarification_http import payload
from test_document_clarification_lifecycle import pause, rows
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_turn_runner import ASK, QUESTION, start
from test_document_turn_runner import native as native

from bowhead_gateway import turn_service

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
_spawn_resume_turn = turn_service.spawn_resume_turn


@pytest.mark.parametrize(
    "ending",
    [
        "incomplete",
        pytest.param(
            "review", marks=pytest.mark.skipif(os.environ.get("BOWHEAD_DOCUMENT_NATIVE_SMOKE") != "1", reason="Requires native LibreOffice/Poppler")
        ),
    ],
)
async def test_http_resumes_exact_checkpoint_through_actual_worker(http_case, native, ctx, monkeypatch, ending):
    from bowhead_gateway.routes import chat, documents

    # This hand-built Orchestrator has no reference materializer (main.py wires one), and the
    # exact provider-call count below excludes automatic memory capture's classifier call.
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "0")
    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "0")
    router = await native.service.authorize_model(SCOPE)
    app = http_case.app
    previous = app.state.orchestrator
    orchestrator = Orchestrator(
        router=router,
        datarc=None,
        session_store=ctx.sessions,
        tool_registry=build_default_registry(),
        event_registry=previous.event_registry,
        user_store=app.state.user_store,
        clarification_store=app.state.clarification_store,
        artifact_store=ctx.artifacts,
    )
    orchestrator.document_service = native.service
    app.state.orchestrator = orchestrator
    app.state.document_service = native.service
    app.state.artifact_store = ctx.artifacts
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1/chat")
    monkeypatch.setattr(chat, "get_event_registry", lambda: orchestrator.event_registry)
    monkeypatch.setattr(turn_service, "spawn_resume_turn", _spawn_resume_turn)
    turn_service.resolve_team_model_runtime.return_value = turn_service.TeamModelRuntimeResolution(
        router=router, result="team_safe_success", reason="fixture_provider"
    )
    outcome = await start(ctx, native)
    async with (
        open_shared_ctx(ctx.path) as db,
        db.execute("SELECT runtime_snapshot_json FROM document_turn_dispatches WHERE job_id=?", (outcome.job_id,)) as cursor,
    ):
        snapshot = DocumentRuntimeSnapshot.model_validate_json((await cursor.fetchone())[0])
    store, card = await pause(
        ctx, SimpleNamespace(worker=worker_from_context(native.context), job_id=outcome.job_id, ref=outcome.checkpoint, snapshot=snapshot)
    )
    native.responses = [ASK]
    if ending == "review":
        from bowhead_core.documents.planning_resume import prepare_answer_context
        from bowhead_core.documents.pptx_plan import PlanningContext

        context_json = (await rows(ctx, "SELECT context_json FROM document_planning_attempts WHERE job_id=? AND attempt_no=1", (outcome.job_id,)))[0][
            0
        ]
        resumed = prepare_answer_context(PlanningContext(**json.loads(context_json)), question=QUESTION, answer="Quarter two", external_input_count=0)
        evidence = json.loads(resumed.context.payload)
        slot = next(item["id"] for item in evidence["slots"] if item["text"] == "Quarter one")
        fact = next(item["id"] for item in evidence["facts"] if item["path"] == "/clarification/answer")
        native.responses = [
            json.dumps(
                {
                    "action": "propose",
                    "updates": [{"slot": slot, "parts": [{"ref": fact}]}],
                    "keep": [item["id"] for item in evidence["slots"] if item["id"] != slot],
                }
            )
        ]
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0")
    try:
        response = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json=payload("Quarter two"))
        assert response.status_code == 200, response.text
        assert response.json()["resume_turn_id"] == "t_doc"
        tasks = [task for task in _inflight_tasks if task.get_name() in {"turn_resume:t_doc", "turn_dispatch_lease:t_doc"}]
        assert tasks
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=45)
        turn = await ctx.sessions.get_turn("t_doc")
        assert turn.status == ("completed" if ending == "review" else "failed") and turn.execution_generation == 2
        assert (turn.tokens_input, turn.tokens_output) == (17, 9)
        assert (await store.get(card.request_id)).answers == payload("Quarter two")["answers"]
        assert len(native.calls) == 2 and native.worker.runs.count("inventory") == 1
        assert await rows(ctx, "SELECT count(*) FROM turns") == [(1,)]
        assert await rows(ctx, "SELECT count(*) FROM clarification_requests") == [(1,)]
        assert await rows(ctx, "SELECT consumed_generation FROM clarification_document_continuations") == [(2,)]
        assert await rows(ctx, "SELECT count(*) FROM credit_ledger WHERE reason='template_document'") == [(2,)]
        assert await rows(ctx, "SELECT count(*) FROM turn_events WHERE event_type='turn.resumed'") == [(1,)]
        assert await rows(ctx, "SELECT count(*) FROM turn_events WHERE event_type='clarification_required'") == [(1,)]
        if ending == "incomplete":
            assert turn.error_kind == "document_information_incomplete"
            terminal = await rows(ctx, "SELECT payload_json FROM turn_events WHERE event_type='done' ORDER BY seq DESC LIMIT 1")
            failure = json.loads(terminal[0][0])
            assert failure["reason_code"] == "document_information_incomplete"
            assert failure["diagnostics"]["retryable"] is False
            assert failure["diagnostics"]["diagnostic_code"] == "chat_turn_failed"
            replay = await asyncio.wait_for(http_case.client.get("/api/v1/chat/stream/t_doc"), timeout=10)
            assert replay.status_code == 200, replay.text
            frames = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
            assert any(frame.get("diagnostics") == failure["diagnostics"] for frame in frames)
        if ending == "review":
            saved = await native.service.delivery.get(scope=SCOPE, job_id=outcome.job_id)
            assert saved["state"] == "review"
            assert await rows(
                ctx,
                "SELECT json_extract(payload_json,'$.job_id'),json_extract(payload_json,'$.draft_digest') FROM turn_events WHERE event_type='document.draft_open'",
            ) == [(outcome.job_id, saved["draft_digest"])]
            opened = await http_case.client.get(f"/api/v1/sessions/s_doc/documents/{outcome.job_id}")
            assert opened.status_code == 200, opened.text
            assert opened.json()["state"] == "review" and opened.json()["draft_digest"] == saved["draft_digest"]
            (ctx.path.parent / "http-native-review.json").write_text(opened.text)
            replay = await asyncio.wait_for(http_case.client.get("/api/v1/chat/stream/t_doc"), timeout=10)
            assert replay.status_code == 200, replay.text
            frames = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
            pointers = [frame for frame in frames if frame.get("job_id") == outcome.job_id]
            assert len(pointers) == 1, replay.text
            assert pointers[0]["draft_digest"] == saved["draft_digest"]
            assert (await native.service.delivery.get(scope=SCOPE, job_id=outcome.job_id))["state"] == "review"
        retry = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json=payload("A changed answer"))
        assert retry.status_code in {200, 409}, retry.text
        assert len(native.calls) == 2
        assert (await store.get(card.request_id)).answers == payload("Quarter two")["answers"]
        http_case.datarc.assert_not_awaited()
    finally:
        pending = [task for task in _inflight_tasks if task.get_name().endswith(":t_doc") and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
