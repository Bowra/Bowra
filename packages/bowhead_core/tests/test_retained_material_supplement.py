"""Old encrypted context heads recover bounded native pointers at model send."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import jwt
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agents.report_tools import _write_report, register_report_tools
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.context_history import load_conversation_context
from bowhead_core.context_store import publish_conversation_context_in_tx
from bowhead_core.result_material_history import (
    MAX_RETAINED_TURNS,
    SUPPLEMENT_NAME,
    authorize_material_supplement,
    load_retained_material_supplement,
)
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.report_read_access import ReportReadAccess
from bowhead_router.types import LLMResponse, Message, ToolCall
from cryptography.fernet import Fernet
from test_agent_collaboration import collab as collab
from test_context_history import next_context
from test_context_store import readable, write

pytestmark = pytest.mark.integration


@pytest.fixture
async def retained_case(collab, monkeypatch, tmp_path, request):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", key)
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "synthetic-retained-material-secret")
    monkeypatch.delenv("FEATURE_CONTEXT_MATERIAL_RECOVERY", raising=False)
    collab.store.cipher = ReferenceCipher(key)
    worker = collab.worker
    options = getattr(request, "param", {})
    older_turns = options.get("older_turns", 0)
    if older_turns:
        async with open_shared_ctx(collab.path) as db:
            await db.execute("UPDATE turns SET seq=? WHERE id=?", (older_turns + 1, worker.turn_id))
            await db.executemany(
                "INSERT INTO turns(id,session_id,seq,user_text,status,final_answer,started_at) VALUES (?,?,?,?,?,?,?)",
                [
                    (f"t_older_{i}", worker.session_id, i + 1, "Earlier request", "completed", "Earlier answer", "2026-11-01")
                    for i in range(older_turns)
                ],
            )
            await db.commit()
    sessions, canvases = SessionStore(collab.path), CanvasStore(collab.path)
    artifacts = ArtifactStore(db_path=collab.path, backend=LocalBackend(tmp_path / "artifacts"))
    await canvases.initialize()
    await artifacts.initialize()
    canvas = await canvases.ensure_canvas(session_id=worker.session_id, workspace_id=worker.workspace_id, user_id=worker.user_id)
    source = SimpleNamespace(
        canvas_store=canvases,
        session_id=worker.session_id,
        workspace_id=worker.workspace_id,
        user_id=worker.user_id,
        turn_id=worker.turn_id,
        parent_bus=SimpleNamespace(),
    )
    saved = await _write_report({"content": "Confirm the missing data before choosing an allocation."}, source)
    for index in range(options.get("sections", 1) - 1):
        await _write_report({"content": f"Additional saved finding {index}."}, source)
    artifact = await artifacts.put_bytes(
        content=b"segment,count\nA,17\nB,24\n",
        filename="saved-counts.csv",
        mime="text/csv",
        source="sandbox",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        created_by_turn=worker.turn_id,
    )
    logs = TurnLogStore(collab.path)
    await logs.record(turn_id=worker.turn_id, event_type="agent_spawned", payload={"subagent_id": "compute", "profile": "compute"})
    await logs.record(
        turn_id=worker.turn_id,
        event_type="tool_result",
        payload={
            "subagent_id": "compute",
            "call_id": "python",
            "tool_name": "python_sandbox",
            "result_meta": {"output_artifacts": [{"artifact_id": artifact.id}]},
        },
    )
    section = next(section for section in await canvases.list_sections(canvas.id) if section.id == saved["section_id"])
    for saved_section in await canvases.list_sections(canvas.id):
        await logs.record(
            turn_id=worker.turn_id,
            event_type="canvas.section_added",
            payload={"canvas_id": canvas.id, "section": saved_section.to_report_section()},
        )
    original = [Message("user", "Analyze the synthetic counts."), Message("assistant", "The analysis and file are saved.")]
    generation = await write(collab, execution_id=collab.lead, messages=original)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await publish_conversation_context_in_tx(
            db,
            worker=worker,
            execution_id=collab.lead,
            generation_id=generation,
            expected_conversation_generation=None,
            cipher=collab.store.cipher,
            validate_sources=readable,
        )
        await db.commit()
    assert await sessions.mark_completed(
        worker=worker,
        turn_id=worker.turn_id,
        intent="chat",
        final_answer=original[-1].content,
        tokens_input=1,
        tokens_output=1,
        cost_usd=0,
    )
    await sessions.finalize_turn(session_id=worker.session_id, current_turn_id=worker.turn_id, next_turn_id=None)
    ctx = await next_context(collab)
    token = jwt.encode({"sub": worker.user_id}, "synthetic-retained-material-secret", algorithm="HS256")
    request = SimpleNamespace(
        headers={"Authorization": "Bearer " + token},
        cookies={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_store=UserStore(collab.path),
                session_store=sessions,
                canvas_store=canvases,
                artifact_store=artifacts,
                artifact_fetch_secret="synthetic-secret",
                turn_log_store=logs,
                access_control_store=None,
            )
        ),
    )
    ctx.canvas_store, ctx.artifact_store = canvases, artifacts
    ctx.report_read_access = ReportReadAccess(request, ctx.session_id, ctx.workspace_id, ctx.team_id, ctx.user_id)
    ctx._model_context_history = await load_conversation_context(ctx)
    assert ctx._model_context_history.messages == original
    assert section.id not in str(ctx._model_context_history.messages) and artifact.id not in str(ctx._model_context_history.messages)
    return SimpleNamespace(ctx=ctx, section=section, artifact=artifact, canvas=canvas, original=original, collab=collab)


async def protected_state(case):
    async with open_shared_ctx(case.collab.path) as db:
        async with db.execute("SELECT id,snapshot_json FROM model_context_generations ORDER BY id") as cursor:
            snapshots = await cursor.fetchall()
        async with db.execute("SELECT digest,ciphertext FROM model_context_segments ORDER BY digest") as cursor:
            segments = await cursor.fetchall()
    return snapshots, segments, await case.ctx.canvas_store.read_canvas_projection_state(case.canvas.id)


async def test_retained_head_recovers_both_native_kinds_without_rewriting_or_accumulating(retained_case):
    case = retained_case
    before = await protected_state(case)
    first = await load_retained_material_supplement(case.ctx, readers=("read_report",))
    second = await load_retained_material_supplement(case.ctx, readers=("read_report",))
    assert first is not None and first == second
    refs = json.loads(first.message.content)["material_refs"]
    assert {item["kind"] for item in refs["items"]} == {"canvas_section", "artifact"}
    assert case.section.id in first.message.content and case.artifact.id in first.message.content
    assert "Confirm the missing data" not in first.message.content and "A,17" not in first.message.content
    assert refs["content_in_result"] == "omitted" and refs["coverage"]["references"] == "unknown"
    assert refs["reader"]["tool"] == "read_report"
    no_reader = await load_retained_material_supplement(case.ctx, readers=())
    assert "reader" not in json.loads(no_reader.message.content)["material_refs"]
    assert await authorize_material_supplement(case.ctx, first)
    assert case.ctx._model_context_history.messages == case.original
    assert await protected_state(case) == before


@pytest.mark.parametrize("retained_case", [{"sections": 48, "older_turns": MAX_RETAINED_TURNS + 4}], indirect=True)
async def test_both_history_scan_and_complete_request_appendix_are_bounded(retained_case, monkeypatch):
    from bowhead_core import result_material_history

    case = retained_case
    scanned = []
    original_load = result_material_history.load_material_history

    async def observe(ctx, turn_ids):
        scanned.extend(turn_ids)
        return await original_load(ctx, turn_ids)

    monkeypatch.setattr(result_material_history, "load_material_history", observe)
    note = await load_retained_material_supplement(case.ctx, readers=("read_report",))
    assert note is not None and len(note.message.content.encode()) <= 8192
    assert len(note.references.items) <= 32
    assert scanned[0] == case.collab.worker.turn_id and len(scanned) == MAX_RETAINED_TURNS
    assert "t_older_0" not in scanned
    assert note.references.coverage.references == "unknown"
    assert "history_scan_budget" in note.references.coverage.reasons


async def test_event_only_history_does_not_get_an_extra_retained_appendix(retained_case):
    ctx = retained_case.ctx
    ctx._model_context_history = replace(ctx._model_context_history, generation_id=None)
    assert await load_retained_material_supplement(ctx, readers=("read_report",)) is None


@pytest.mark.parametrize("change", ["report_deleted", "artifact_deleted", "membership", "scope", "disabled"])
async def test_current_authority_can_remove_prepared_pointer_disclosure(retained_case, monkeypatch, change):
    case = retained_case
    prepared = await load_retained_material_supplement(case.ctx, readers=())
    assert prepared is not None
    if change == "artifact_deleted":
        await case.ctx.artifact_store.soft_delete(case.artifact.id, user_id=case.ctx.user_id)
    elif change in {"report_deleted", "membership"}:
        async with open_shared_ctx(case.collab.path) as db:
            if change == "report_deleted":
                await db.execute("UPDATE canvas_sections SET quarantined=1 WHERE id=?", (case.section.id,))
            else:
                await db.execute(
                    "UPDATE workspace_members SET status='inactive' WHERE workspace_id=? AND user_id=?", (case.ctx.workspace_id, case.ctx.user_id)
                )
            await db.commit()
    elif change == "scope":
        case.ctx.workspace_id = "ws_other"
    else:
        monkeypatch.setenv("FEATURE_CONTEXT_MATERIAL_RECOVERY", "0")
    assert not await authorize_material_supplement(case.ctx, prepared)
    refreshed = await load_retained_material_supplement(case.ctx, readers=())
    if change in {"membership", "scope", "disabled"}:
        assert refreshed is None
    else:
        removed = case.artifact.id if change == "artifact_deleted" else case.section.id
        assert removed not in refreshed.message.content


@pytest.mark.parametrize("mode", ["report", "artifact", "disabled", "revoked_before_send"])
async def test_actual_lead_requests_receive_current_supplement_only(retained_case, monkeypatch, mode):
    import asyncio

    from bowhead_core.agents import AgentRegistry
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.events import OrchestratorEvent, TurnEventBus
    from bowhead_core.subagent import PROFILES, SubagentRunner
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

    case, source = retained_case, retained_case.ctx
    monkeypatch.setitem(PROFILES, "lead", manifest_to_profile(AgentRegistry.bootstrap().get("lead")))
    if mode == "disabled":
        monkeypatch.setenv("FEATURE_CONTEXT_MATERIAL_RECOVERY", "0")
    if mode == "revoked_before_send":
        from bowhead_core import result_material_history

        original_load = result_material_history.load_retained_material_supplement

        async def revoke_after_preparation(ctx, **kwargs):
            prepared = await original_load(ctx, **kwargs)
            await ctx.artifact_store.soft_delete(case.artifact.id, user_id=ctx.user_id)
            return prepared

        monkeypatch.setattr(result_material_history, "load_retained_material_supplement", revoke_after_preparation)

    class Router:
        def __init__(self):
            self.requests = []

        def context_window_for(self, *args):
            return 65536

        async def invoke(self, agent_type, request, **kwargs):
            self.requests.append(deepcopy(request))
            if mode == "report" and len(self.requests) == 1:
                return LLMResponse(content="", tool_calls=[ToolCall("read", "read_report", {"section_ids": [case.section.id]})])
            return LLMResponse(content="The requested saved material is available.")

    ctx = ToolExecutionContext(
        workspace_id=source.workspace_id,
        member_uid="synthetic",
        user_id=source.user_id,
        team_id=source.team_id,
        turn_id=source.turn_id,
        session_id=source.session_id,
        datarc=None,
        role="lead",
        canvas_store=source.canvas_store,
        artifact_store=source.artifact_store,
        session_store=source.session_store,
        report_read_access=source.report_read_access,
    )
    ctx._model_context_history = source._model_context_history
    history = deepcopy(source._model_context_history.messages)
    registry = build_default_registry()
    register_report_tools(registry)
    router, bus = Router(), TurnEventBus()

    async def consume():
        async for _ in bus.subscribe():
            pass

    consumer = asyncio.create_task(consume())
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name="lead",
        task="Inspect the saved result.",
        history=history,
        parent_turn_id=source.turn_id,
        parent_bus=bus,
        parent_ctx=ctx,
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id=source.turn_id, payload={}))
    await consumer
    assert result.ok, result
    assert router.requests
    for request in router.requests:
        notes = [message for message in request.messages if message.name == SUPPLEMENT_NAME]
        if mode in {"disabled", "revoked_before_send"}:
            assert notes == []
        else:
            assert len(notes) == 1
            assert case.section.id in notes[0].content and case.artifact.id in notes[0].content
    if mode == "report":
        assert len(router.requests) == 2
        assert any(message.role == "tool" and "Confirm the missing data" in message.content for message in router.requests[-1].messages)
    assert history == case.original and source._model_context_history.messages == case.original
