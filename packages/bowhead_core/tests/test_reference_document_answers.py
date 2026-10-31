"""Real auth, stores, native workers and confirmation with controlled models/rendering."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import respx
import test_reference_http_start as reference_http
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.chat_references import InputDocument
from bowhead_core.documents.request import DocumentProposal, DocumentScope, InputRoleProposal, UserQuote
from test_document_authoring_turn import authored as authored
from test_document_authoring_turn import call, edits
from test_document_turn_runner import make_native
from test_reference_assigned_read_question import http_start as http_start
from test_reference_assigned_read_question import model_reply
from test_reference_assigned_read_question import original_receiver_case as original_receiver_case
from test_reference_assigned_read_question import receiver_case as receiver_case
from test_reference_assigned_read_question import start_case as start_case
from test_reference_lead_mcp import make_mcp_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("original_receiver_case", ["admission"], indirect=True),
    pytest.mark.parametrize("baseline", ["document_plugin"]),
    pytest.mark.parametrize("read_kind", ["skill", "mcp"]),
]


@pytest.fixture
async def submitted(http_start, read_kind, monkeypatch):
    http_start.document_mcp = None
    if read_kind == "mcp":
        http_start.document_mcp = await make_mcp_case(http_start, monkeypatch, document_tool=True, sequence=http_start.fixture.question_revision + 1)
        result = http_start.document_mcp.body
    else:
        result = reference_http.payload(http_start, skills=False)
        sequence = http_start.fixture.question_revision
        selected = next(node["resource"] for node in result["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "agent")
        selected.update(version=f"arv_exact_{sequence}", release_id=f"arl_exact_{sequence}")
    result["input_document"]["nodes"].append({"type": "text", "text": " Use the pinned template to prepare a PowerPoint for the confirmed quarter."})
    result["message"] = InputDocument.model_validate(result["input_document"]).display_text()
    result["session_id"] = "s_reference_document"
    return result


@pytest.fixture
async def native(http_start, submitted, monkeypatch, tmp_path):
    from bowhead_core.skill_bridge import register_skill_tools
    from bowhead_core.skill_read_resolution import build_reference_skill_sources
    from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata

    from bowhead_skill import SkillCatalog, SkillRuntime
    from bowhead_skill.catalog import builtin_catalog_dir

    case = http_start
    scope = DocumentScope(**{key: getattr(case.fixture.scope, key) for key in ("team_id", "user_id", "workspace_id")})
    artifacts = ArtifactStore(db_path=case.path, backend=LocalBackend(tmp_path / "document-files"))
    artifact = await artifacts.put_bytes(
        content=b"Synthetic template", filename="template.pptx", mime="application/octet-stream", created_by=scope.user_id
    )
    sessions = case.fixture.case.store
    await sessions.ensure_session(session_id=submitted["session_id"], user_id=scope.user_id, workspace_id=scope.workspace_id)
    await sessions.pin_session_attachments(session_id=submitted["session_id"], artifact_ids=[artifact.id])
    quote = UserQuote(start=0, text=submitted["message"])
    domain = SimpleNamespace(
        path=case.path,
        sessions=sessions,
        artifacts=artifacts,
        artifact=artifact,
        proposal=DocumentProposal(
            evidence=quote, inputs=(InputRoleProposal(artifact_id=artifact.id, role="template", evidence=quote),), requested_outputs=("source", "pdf")
        ),
    )
    result = await make_native(domain, monkeypatch, tmp_path, scope=scope, session_id=submitted["session_id"], claim=False)
    result.domain, result.scope = domain, scope
    orchestrator = case.app.state.orchestrator
    case.app.state.artifact_store = orchestrator.artifact_store = artifacts
    case.app.state.document_service = orchestrator.document_service = result.service
    from bowhead_core.documents.tool import register_document_tool

    register_document_tool(orchestrator.tool_registry, service=lambda: result.service)
    root = builtin_catalog_dir()
    register_skill_tools(orchestrator.tool_registry, SkillCatalog(roots=[root]), SkillRuntime())
    orchestrator.reference_skill_sources_factory = lambda selected_scope: build_reference_skill_sources(
        case.path, scope=selected_scope, builtin_index=build_builtin_skill_metadata, builtin_root=root
    )
    return result


async def assert_answer_boundaries(db, *, worker, job_id, monkeypatch):
    from bowhead_core.clarification_rounds import read_history
    from bowhead_core.document_clarification_lifecycle import prior_document_answers_in_tx
    from bowhead_core.documents import reference_answers as reader

    cards = await read_history(db, turn_id=worker.turn_id, user_id=worker.user_id, workspace_id=worker.workspace_id, session_id=worker.session_id)
    with pytest.raises(ValueError):
        await prior_document_answers_in_tx(db, worker=worker)
    with pytest.raises(ValueError):
        await prior_document_answers_in_tx(db, worker=worker, job_id="djob_foreign")
    for field, value in {
        "team_id": "team_foreign",
        "user_id": "usr_foreign",
        "workspace_id": "ws_foreign",
        "session_id": "s_foreign",
        "turn_id": "t_foreign",
        "generation": 1,
        "owner_id": "foreign-owner",
    }.items():
        with pytest.raises(ValueError):
            await prior_document_answers_in_tx(db, worker=worker.model_copy(update={field: value}), job_id=job_id)
    for selected in ([], [*cards, *cards]):
        with pytest.raises(ValueError):
            await reader.read_reference_document_answers_in_tx(db, worker=worker, job_id=job_id, cards=selected)
    original_row = reader._row
    for target, field, value in (
        ("job", "source_hash", "0" * 64),
        ("job", "user_source_sha256", "1" * 64),
        ("job", "creation_generation", 1),
        ("job", "owner_id", "foreign-owner"),
        ("attempt", "clarification_id", "foreign-card"),
        ("attempt", "execution_id", "aex_foreign"),
        ("attempt", "generation", 1),
        ("attempt", "owner_id", "foreign-owner"),
        ("attempt", None, None),
    ):

        async def changed_row(connection, query, params=(), *, target=target, field=field, value=value):
            row = await original_row(connection, query, params)
            matches = "FROM document_jobs j" in query if target == "job" else "FROM chat_reference_lead_postdispatch_attempts" in query
            if not matches:
                return row
            return None if field is None else {**dict(row), field: value}

        with monkeypatch.context() as patch:
            patch.setattr(reader, "_row", changed_row)
            with pytest.raises(ValueError):
                await reader.read_reference_document_answers_in_tx(db, worker=worker, job_id=job_id, cards=cards)
    from unittest.mock import AsyncMock

    for name, value in (("read_pause", None), ("document_job_caller_in_tx", "aex_foreign"), ("_read_chat_turn_user_source_in_tx", None)):
        with monkeypatch.context() as patch:
            patch.setattr(reader, name, AsyncMock(return_value=value))
            with pytest.raises(ValueError):
                await reader.read_reference_document_answers_in_tx(db, worker=worker, job_id=job_id, cards=cards)


@pytest.mark.parametrize("answer_mode", ["scalar", "typed"])
@pytest.mark.parametrize("ending", ["review", "native_question"])
async def test_answered_reference_submits_original_native_job(http_start, submitted, authored, monkeypatch, answer_mode, ending, read_kind):
    await run_document_case(http_start, submitted, authored, monkeypatch, answer_mode=answer_mode, ending=ending, read_kind=read_kind)


@pytest.mark.parametrize("tail_mode", ["plain", "reference"])
async def test_native_resume_drains_accepted_follow_up_without_inheriting_its_checkpoint(
    http_start, submitted, authored, monkeypatch, read_kind, tail_mode
):
    await run_document_case(
        http_start, submitted, authored, monkeypatch, answer_mode="scalar", ending="native_question", read_kind=read_kind, tail_mode=tail_mode
    )


async def test_native_terminal_recovers_original_done_without_reexecuting(http_start, submitted, authored, monkeypatch, read_kind):
    await run_document_case(
        http_start, submitted, authored, monkeypatch, answer_mode="typed", ending="native_question", read_kind=read_kind, tail_mode="recovery"
    )


async def run_document_case(http_start, submitted, authored, monkeypatch, *, answer_mode, ending, read_kind, tail_mode=None):
    from bowhead_gateway.routes import clarifications, documents

    from bowhead_core import events

    case = http_start
    buffered_tail = tail_mode in {"plain", "reference"}
    reference_http.enable_lead_writer(case, monkeypatch)
    if tail_mode is not None:
        # The native fixture configures a separate test provider. Fresh child
        # Lead calls use that config through the existing controlled adapter.
        adapters = case.app.state.orchestrator.router._registry
        monkeypatch.setitem(adapters.adapters, "team", adapters.get("primary"))
        for model in ("test", "fast-test"):
            monkeypatch.setitem(case.app.state.orchestrator.router._context_windows, ("team", model), 1_000_000)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    case.app.include_router(documents.router, prefix="/api/v1")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    authored.responses = [call("document_read", {"source": "data-0"}, 0)] + (
        [call("clarification_ask", {"questions": [{"question": "Which comparison period?"}]}, 1)]
        if ending == "native_question"
        else edits(authored.domain)
    )
    requests = []
    child_started, release_child = asyncio.Event(), asyncio.Event()

    async def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) > 4 and buffered_tail:
            child_started.set()
            await asyncio.wait_for(release_child.wait(), 60)
            body = requests[-1]
            if any(tool["function"]["name"] == "subagent_spawn" for tool in body.get("tools", [])) and not reference_http.lead_has_dispatched(body):
                return model_reply(
                    request,
                    name="subagent_spawn",
                    arguments={
                        "profile": case.fixture.case.args["reference_start"].profile_name,
                        "task": "Summarize the confirmed synthetic changes.",
                        "delegation_role": "primary",
                    },
                )
            return reference_http.answer_response(request)
        if len(requests) == 1:
            return model_reply(
                request,
                name="subagent_spawn",
                arguments={
                    "profile": case.fixture.case.args["reference_start"].profile_name,
                    "task": "Read the document method, ask which quarter, then use the native document Tool.",
                    "delegation_role": "primary",
                },
            )
        if len(requests) == 2:
            if case.document_mcp is not None:
                return model_reply(request, name=case.document_mcp.read.id, arguments={"year": 2025})
            return model_reply(request, name="skill_read", arguments={"name": "pptx-authoring"})
        if len(requests) == 3:
            return model_reply(
                request,
                name="subagent_return",
                arguments={
                    "payload": {
                        "error": {
                            "kind": "needs_user_input",
                            "message": "The quarter is required.",
                            "clarification_payload": {"questions": [{"question": "Which quarter?", "type": "free_text", "options": []}]},
                        }
                    }
                },
            )
        assert len(requests) == 4, "The original Agent must not be dispatched again"
        return model_reply(request, name="template_document_submit", arguments=authored.arguments)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=submitted)
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await reference_http.settled(turn_id)
        cards = [row for row in reference_http.rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1
        assert (await authored.domain.sessions.get_turn(turn_id)).status == "clarification_pending"
        resolution = (
            {"chosen_value": "2026-Q3"}
            if answer_mode == "scalar"
            else {"answers": [{"question": "Which quarter?", "source": "custom", "answer": "2026-Q3", "skipped": False, "option_index": None}]}
        )
        resolved = await case.client.post(f"/api/v1/clarifications/{cards[0]['request_id']}/resolve", json=resolution)
        assert resolved.status_code == 200, resolved.text
        await reference_http.settled(turn_id)
        turn = await authored.domain.sessions.get_turn(turn_id)
        assert turn.status == ("clarification_pending" if ending == "native_question" else "completed"), turn.error_kind
        jobs = [row for row in reference_http.rows(case, "document_jobs") if row["origin_turn_id"] == turn_id]
        assert len(jobs) == 1
        job_id = jobs[0]["id"]
        assert len(requests) == 4
        assert authored.calls, "The native author must receive the canonical earlier answer"
        assert any("2026-Q3" in json.dumps(private_call) for private_call in authored.calls)
        assert len([row for row in reference_http.rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]) == 1
        tool_events = [
            json.loads(row["payload_json"])
            for row in reference_http.rows(case, "turn_events")
            if row["turn_id"] == turn_id and row["event_type"] == "tool_result"
        ]
        assert sum(event.get("tool_name") == "skill_read" for event in tool_events) == (1 if read_kind == "skill" else 0)
        if case.document_mcp is not None:
            assert len(case.document_mcp.calls) == 1
        if ending == "native_question":
            pending = [row for row in reference_http.rows(case, "clarification_requests") if row["turn_id"] == turn_id and row["status"] == "pending"]
            assert len(pending) == 1 and pending[0]["request_id"] != cards[0]["request_id"]
            await authored.domain.sessions.recover_expired_chat_spawn_dispatches()
            assert await authored.domain.sessions.is_turn_resume_ready(turn_id)
            authored.responses = edits(authored.domain)
            started, release = asyncio.Event(), asyncio.Event()
            dispatch = authored.authoring_worker._dispatch

            async def wait_for_follow_up(*args, **kwargs):
                started.set()
                await asyncio.wait_for(release.wait(), 60)
                return await dispatch(*args, **kwargs)

            if buffered_tail:
                monkeypatch.setattr(authored.authoring_worker, "_dispatch", wait_for_follow_up)
            if tail_mode == "recovery":
                materialize = authored.domain.sessions.materialize_steer_follow_up

                async def interrupted_tail(**fields):
                    if fields["current_turn_id"] == turn_id:
                        raise RuntimeError("Synthetic native terminal-tail interruption")
                    return await materialize(**fields)

                monkeypatch.setattr(authored.domain.sessions, "materialize_steer_follow_up", interrupted_tail)
            answered = await case.client.post(
                f"/api/v1/clarifications/{pending[0]['request_id']}/resolve",
                json={
                    "answers": [
                        {
                            "question": "Which comparison period?",
                            "source": "custom",
                            "answer": "Compare with the previous quarter",
                            "skipped": False,
                            "option_index": None,
                        }
                    ]
                },
            )
            assert answered.status_code == 200, answered.text
            if buffered_tail:
                try:
                    await asyncio.wait_for(started.wait(), 60)
                    queued = copy.deepcopy(submitted)
                    queued.update(
                        client_submission_id=str(uuid4()),
                        submission_kind="steer",
                        expected_parent_turn_id=turn_id,
                        expected_execution_generation=3,
                    )
                    if tail_mode == "reference":
                        selected = next(node for node in queued["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "agent")
                        queued["input_document"]["nodes"] = [{"type": "text", "text": "Summarize the confirmed synthetic changes. "}, selected]
                        queued["message"] = InputDocument.model_validate(queued["input_document"]).display_text()
                    else:
                        queued.pop("input_document")
                        queued.update(submission_contract_version="chat-submit.v1", model_control_batch="a")
                        queued["message"] = "Summarize the confirmed synthetic changes."
                    headers = {"content-type": "application/json"} if tail_mode == "plain" else {}
                    buffered = await case.client.post("/api/v1/chat/send", json=queued, headers=headers)
                    assert buffered.status_code == 200 and buffered.json()["status"] == "buffered", buffered.text
                    replay = await case.client.post("/api/v1/chat/send", json=queued, headers=headers)
                    assert replay.status_code == 200 and replay.json() == buffered.json()
                    assert len(requests) == 4
                finally:
                    release.set()
            if tail_mode == "recovery":
                with pytest.raises(asyncio.CancelledError, match="dispatch_owner_lost"):
                    await reference_http.settled(turn_id)
                assert turn_id in await authored.domain.sessions.recover_expired_chat_spawn_dispatches()
            else:
                await reference_http.settled(turn_id)
            assert (await authored.domain.sessions.get_turn(turn_id)).status == "completed"
            if not buffered_tail:
                assert len(requests) == 4
            else:
                relation = await authored.domain.sessions.get_chat_follow_up(parent_turn_id=turn_id)
                assert relation is not None
                child_id = relation[0]
                try:
                    await asyncio.wait_for(child_started.wait(), 60)
                    further = {
                        "workspace_id": submitted["workspace_id"],
                        "session_id": submitted["session_id"],
                        "message": "A final synthetic note.",
                        "client_submission_id": str(uuid4()),
                        "submission_kind": "steer",
                        "submission_contract_version": "chat-submit.v1",
                        "model_control_batch": "a",
                        "expected_parent_turn_id": child_id,
                        "expected_execution_generation": 1,
                    }
                    buffered = await case.client.post("/api/v1/chat/send", json=further, headers={"content-type": "application/json"})
                    assert buffered.status_code == 200 and buffered.json()["status"] == "buffered", buffered.text
                    session_before = next(row for row in reference_http.rows(case, "sessions") if row["id"] == submitted["session_id"])
                    assert session_before["active_turn_id"] == child_id and json.loads(session_before["steer_buffer_json"])
                    parent_dispatch = next(row for row in reference_http.rows(case, "chat_turn_spawn_dispatch") if row["turn_id"] == turn_id)
                    owner = parent_dispatch["owner_id"]
                    assert await authored.domain.sessions.heartbeat_chat_spawn_dispatch(turn_id=turn_id, owner_id=owner) == "lost"
                    assert not await authored.domain.sessions.mark_chat_spawn_dispatch_terminal(turn_id=turn_id, owner_id=owner)
                    assert (
                        await authored.domain.sessions.materialize_steer_follow_up(
                            session_id=submitted["session_id"], current_turn_id=turn_id, next_turn_id="t_stale_parent_child"
                        )
                        == []
                    )
                    session_after = next(row for row in reference_http.rows(case, "sessions") if row["id"] == submitted["session_id"])
                    assert session_after == session_before
                finally:
                    release_child.set()
                await reference_http.settled(child_id)
                child = await authored.domain.sessions.get_turn(child_id)
                assert child.status == "completed", child.error_kind
                assert child.execution_generation == 1
                final_relation = await authored.domain.sessions.get_chat_follow_up(parent_turn_id=child_id)
                assert final_relation is not None
                await reference_http.settled(final_relation[0])
                assert (await authored.domain.sessions.get_turn(final_relation[0])).status == "completed"
                assert len(reference_http.rows(case, "document_jobs")) == 1
            done = [row for row in reference_http.rows(case, "turn_events") if row["turn_id"] == turn_id and row["event_type"] == "done"]
            assert [json.loads(row["payload_json"])["status"] for row in done] == ["clarification_pending", "clarification_pending", "completed"]
            parent_dispatch = next(row for row in reference_http.rows(case, "chat_turn_spawn_dispatch") if row["turn_id"] == turn_id)
            # Recovery fences the failed worker while retaining the Turn's
            # completed result and its reconstructed original done event.
            assert parent_dispatch["state"] == ("failed" if tail_mode == "recovery" else "terminal")
        draft = await authored.service.delivery.get(scope=authored.scope, job_id=job_id)
        assert draft["state"] == "review", draft
        from bowhead_core.chat_reference_lead_postdispatch import answer, read_pause
        from bowhead_core.document_chat_continuation import DocumentTurnOwner
        from bowhead_core.document_clarification_lifecycle import prior_document_answers_in_tx
        from bowhead_core.documents.planning_instruction import document_instruction_in_tx

        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            dispatch = await (
                await db.execute("SELECT creation_generation,owner_id FROM document_turn_dispatches WHERE job_id=?", (job_id,))
            ).fetchone()
            worker = DocumentTurnOwner(
                **authored.scope.model_dump(), session_id=submitted["session_id"], turn_id=turn_id, generation=dispatch[0], owner_id=dispatch[1]
            )
            original = await read_pause(db, turn_id)
            canonical = json.loads(answer(original))
            retained = await prior_document_answers_in_tx(db, worker=worker, job_id=job_id)
            assert retained == [{"question": original["rationale"], "answer": canonical}]
            instruction = await document_instruction_in_tx(db, worker=worker, job_id=job_id)
            assert json.loads(instruction.history.content) == {"answers": retained}
            assert draft["clarification_evidence_sources"] == [
                {"source_name": instruction.history.name, "source_sha256": hashlib.sha256(instruction.history.content).hexdigest()}
            ]
            if ending == "review" and answer_mode == "typed" and read_kind == "skill":
                await assert_answer_boundaries(db, worker=worker, job_id=job_id, monkeypatch=monkeypatch)
            await db.rollback()
        confirmed = await case.client.post(
            f"/api/v1/documents/{job_id}/confirm", json={"command_id": "confirm-reference-document", "draft_digest": draft["draft_digest"]}
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["state"] == "published"
        assert (
            await case.client.post(
                f"/api/v1/documents/{job_id}/confirm", json={"command_id": "confirm-reference-document", "draft_digest": draft["draft_digest"]}
            )
        ).json() == confirmed.json()
        if not buffered_tail:
            assert len(requests) == 4
        if case.document_mcp is not None:
            assert len(case.document_mcp.calls) == 1
        session = next(row for row in reference_http.rows(case, "sessions") if row["id"] == submitted["session_id"])
        assert session["active_turn_id"] is None and json.loads(session["steer_buffer_json"]) == []
        if ending == "review" and answer_mode == "typed" and read_kind == "skill":
            private_calls = len(authored.calls)
            async with open_shared_ctx(case.path) as db:
                await db.execute(
                    "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (authored.scope.workspace_id, authored.scope.user_id)
                )
                await db.execute("DELETE FROM team_members WHERE team_id=? AND user_id=?", (authored.scope.team_id, authored.scope.user_id))
                await db.commit()
            denied = await case.client.get(f"/api/v1/sessions/{submitted['session_id']}/documents/{job_id}")
            assert denied.status_code in {403, 404}, denied.text
            denied = await case.client.post(
                f"/api/v1/documents/{job_id}/confirm", json={"command_id": "confirm-reference-document", "draft_digest": draft["draft_digest"]}
            )
            assert denied.status_code in {403, 404}, denied.text
            assert len(authored.calls) == private_calls and len(requests) == 4
