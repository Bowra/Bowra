"""Authorized report reads retain historical observations without repeated effects."""

import json
from types import SimpleNamespace

import jwt
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agents.report_tools import _read_report
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.context_dependencies import ContextSourceObservations, capture_context_dependencies, validate_context_dependencies
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.report_read_access import ReportReadAccess

pytestmark = pytest.mark.integration


@pytest.fixture
async def report_case(tmp_path, initialize_migrated_core_database, monkeypatch):
    monkeypatch.setenv("FEATURE_CONTEXT_MATERIAL_RECOVERY", "1")
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "synthetic-report-recovery-secret")
    path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(path)
    users, sessions, canvases = UserStore(path), SessionStore(path), CanvasStore(path)
    await users.initialize()
    await sessions.initialize()
    await canvases.initialize()
    team = await users.create_team(name="Recovery", max_seats=5)
    user = await users.create_user(team_id=team.id, email="recovery@example.com")
    space = await users.create_local_workspace(team_id=team.id, name="Recovery", creator_user_id=user.id)
    session = await sessions.ensure_session(session_id="s_recovery", workspace_id=space, user_id=user.id)
    await sessions.record_turn(turn_id="t_recovery", session_id=session.id, user_text="Inspect the saved report")
    canvas = await canvases.ensure_canvas(session_id=session.id, workspace_id=space, user_id=user.id)
    section = await canvases.append_section(
        canvas_id=canvas.id, kind="narrative", payload={"markdown": "Collect missing data before choosing a priority."}, by_turn_id="t_recovery"
    )
    token = jwt.encode({"sub": user.id}, "synthetic-report-recovery-secret", algorithm="HS256")
    request = SimpleNamespace(
        headers={"Authorization": "Bearer " + token},
        cookies={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_store=users,
                session_store=sessions,
                canvas_store=canvases,
                artifact_store=None,
                artifact_fetch_secret="synthetic-secret",
                turn_log_store=TurnLogStore(path),
                access_control_store=None,
            )
        ),
    )
    access = ReportReadAccess(request, session.id, space, team.id, user.id)
    ctx = SimpleNamespace(
        session_store=sessions,
        canvas_store=canvases,
        session_id=session.id,
        workspace_id=space,
        team_id=team.id,
        user_id=user.id,
        turn_id="t_recovery",
        role="lead",
        report_read_access=access,
        ordinary_lead_state=None,
        ordinary_call_identity=("lead", "read"),
        context_source_observations=ContextSourceObservations(),
        knowledge_source_uses={},
    )
    return ctx, section, canvas, request


async def test_direct_read_and_retained_dependency_survive_normal_edit_but_not_revocation(report_case):
    ctx, section, canvas, request = report_case
    result = await _read_report({"section_ids": [section.id]}, ctx)
    assert result.get("ok"), result
    assert result["sections"][0]["content"] == "Collect missing data before choosing a priority."
    dependencies = capture_context_dependencies(ctx)
    assert len(dependencies) == 1 and "report_read" in dependencies[0].content
    await validate_context_dependencies(ctx, dependencies)
    await ctx.canvas_store.patch_section(section_id=section.id, patch={"markdown": "A later revised conclusion."}, expected_rev=section.rev)
    await validate_context_dependencies(ctx, dependencies)
    assert "Collect missing data" in result["sections"][0]["content"]
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("UPDATE workspace_members SET status='inactive' WHERE workspace_id=? AND user_id=?", (ctx.workspace_id, ctx.user_id))
        await db.commit()
    with pytest.raises(OrdinaryContinuationUnavailable):
        await validate_context_dependencies(ctx, dependencies)


async def test_outline_selection_unknown_ids_and_no_mutations(report_case):
    ctx, section, canvas, request = report_case
    before = await ctx.canvas_store.read_canvas_projection_state(canvas.id)
    outline = await _read_report({}, ctx)
    assert outline.get("ok"), outline
    assert "sections" not in outline and outline["coverage"]["view"] == "outline"
    full = await _read_report({"section_ids": [section.id, section.id]}, ctx)
    assert len(full["sections"]) == 1
    missing = await _read_report({"section_ids": [section.id, "sec_missing"]}, ctx)
    assert missing["error"] == "not_found_or_unavailable" and "sections" not in missing
    assert await ctx.canvas_store.read_canvas_projection_state(canvas.id) == before
    ctx.material_read_token_budget = 1
    oversized = await _read_report({"include_full": True}, ctx)
    assert oversized["error"] == "report_read_too_large"


async def test_reconstruction_uses_persisted_section_ids_and_checks_current_access(report_case):
    from bowhead_core.context_assembly import build_turn_history
    from bowhead_core.result_material_history import load_material_history

    ctx, section, canvas, request = report_case
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("UPDATE turns SET status='completed',final_answer='Report saved.' WHERE id=?", (ctx.turn_id,))
        await db.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
            (
                "evt_recovery",
                ctx.turn_id,
                1,
                "canvas.section_added",
                json.dumps({"canvas_id": canvas.id, "section": section.to_report_section()}),
                "2026-11-05T00:00:00Z",
            ),
        )
        await db.commit()
    await ctx.session_store.finalize_turn(session_id=ctx.session_id, current_turn_id=ctx.turn_id, next_turn_id=None)
    await ctx.session_store.record_turn(turn_id="t_next", session_id=ctx.session_id, user_text="What was concluded?")
    ctx.turn_id = "t_next"
    metadata = await load_material_history(ctx, ["t_recovery"])
    assert metadata["t_recovery"]["items"][0]["locator"]["section_id"] == section.id
    turns = await ctx.session_store.list_turns(session_id=ctx.session_id)
    history = build_turn_history(turns, exclude_turn_id=ctx.turn_id, budget_tokens=0, material_refs=metadata)
    assert section.id in history.messages[-1]["content"] and "Collect missing data" not in history.messages[-1]["content"]
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("UPDATE canvas_sections SET quarantined=1 WHERE id=?", (section.id,))
        await db.commit()
    unavailable = await load_material_history(ctx, ["t_recovery"])
    assert unavailable["t_recovery"]["items"] == []


async def test_browser_projection_does_not_release_internal_pointer_metadata(report_case):
    from bowhead_gateway.source_access_projection import project_event_payloads_for_viewer, project_live_event_payload_for_viewer

    ctx, section, canvas, request = report_case
    payload = {"result_meta": {"count": 1, "material_refs": {"private": "source"}}}
    live = await project_live_event_payload_for_viewer(request, ctx.turn_id, "tool_result", payload)
    replay = await project_event_payloads_for_viewer(request, ctx.turn_id, [("tool_result", payload)])
    assert live == replay[0] == {"result_meta": {"count": 1}}
    assert "material_refs" in payload["result_meta"]
    legacy = {
        "tool_name": "read_report",
        "ok": True,
        "result_inline": {"sections": [{"content": "Protected original body"}]},
        "result_preview": "Protected original body",
        "output_preview": "Protected original body",
    }
    current = await project_live_event_payload_for_viewer(request, ctx.turn_id, "tool_result", legacy)
    replay = await project_event_payloads_for_viewer(request, ctx.turn_id, [("tool_result", legacy)])
    assert current == replay[0] and "Protected original body" not in json.dumps(current)
    assert legacy["result_inline"]["sections"][0]["content"] == "Protected original body"


async def test_python_chart_read_uses_original_source_authority_and_retains_revocation(report_case):
    ctx, section, canvas, request = report_case
    identity = "art_123456789abc"

    class Artifacts:
        available = True

        async def get_many(self, ids, *, include_deleted=False):
            return [
                SimpleNamespace(id=item, status="ready", created_by=ctx.user_id, workspace_id=ctx.workspace_id)
                for item in ids
                if item == identity and self.available
            ]

    artifacts = Artifacts()
    request.app.state.artifact_store = artifacts
    chart = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={
            "spec": {
                "kind": "table",
                "title": "Coverage",
                "data_artifact_id": identity,
                "source_meta": {"id": identity, "artifact_id": identity, "tool": "python_sandbox"},
                "columns": [{"key": "decile", "header": "Decile", "type": "numeric"}],
                "rows": [{"decile": 9}],
            },
            "data_source": {"tool": "datarc", "context_id": "1", "context_name": "Synthetic"},
        },
    )
    result = await _read_report({"section_ids": [chart.id]}, ctx)
    assert result.get("ok"), result
    assert chart.id in {row["section_id"] for row in result["sections"]}
    dependencies = capture_context_dependencies(ctx)
    await validate_context_dependencies(ctx, dependencies)
    artifacts.available = False
    with pytest.raises(OrdinaryContinuationUnavailable):
        await validate_context_dependencies(ctx, dependencies)
    unavailable = await _read_report({"section_ids": [chart.id]}, ctx)
    assert unavailable["error"] == "not_found_or_unavailable"
    outline = await _read_report({}, ctx)
    assert outline["ok"] and outline["coverage"]["complete"] is False
    assert chart.id not in {row["section_id"] for row in outline["outline"]}


async def test_cross_scope_and_concurrent_changes_never_return_body(report_case, monkeypatch):
    ctx, section, canvas, request = report_case
    ctx.session_id = "s_other"
    assert (await _read_report({"include_full": True}, ctx))["error"] == "not_found_or_unavailable"
    ctx.session_id = canvas.session_id
    original = ctx.canvas_store.read_canvas_projection_state
    calls = 0

    async def racing_snapshot(identity):
        nonlocal calls
        calls += 1
        if calls == 2:
            await ctx.canvas_store.patch_section(section_id=section.id, patch={"markdown": "Changed concurrently"}, expected_rev=section.rev)
        return await original(identity)

    monkeypatch.setattr(ctx.canvas_store, "read_canvas_projection_state", racing_snapshot)
    result = await _read_report({"include_full": True}, ctx)
    assert result["error"] == "report_content_changed" and "sections" not in result


@pytest.mark.parametrize("mode", ["default", "enabled", "disabled", "revoked_before_dispatch"])
@pytest.mark.parametrize("context_window", [None, 65536])
async def test_lead_model_request_can_read_saved_report_without_creating_content(report_case, monkeypatch, mode, context_window):
    import asyncio

    from bowhead_core.events import OrchestratorEvent, TurnEventBus
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

    from bowhead_router import LLMResponse, ToolCall

    source, section, canvas, request = report_case
    admitted = mode != "disabled"
    from bowhead_core.agents import AgentRegistry
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.subagent import PROFILES

    monkeypatch.setitem(PROFILES, "lead", manifest_to_profile(AgentRegistry.bootstrap().get("lead")))
    if mode == "default":
        monkeypatch.delenv("FEATURE_CONTEXT_MATERIAL_RECOVERY", raising=False)
    else:
        monkeypatch.setenv("FEATURE_CONTEXT_MATERIAL_RECOVERY", "1" if admitted else "0")

    if mode == "revoked_before_dispatch":
        original_read = ReportReadAccess.read

        async def revoke_after_read(self, arguments, ctx):
            result = await original_read(self, arguments, ctx)
            async with open_shared_ctx(source.session_store.db_path) as db:
                await db.execute(
                    "UPDATE workspace_members SET status='inactive' WHERE workspace_id=? AND user_id=?", (source.workspace_id, source.user_id)
                )
                await db.commit()
            return result

        monkeypatch.setattr(ReportReadAccess, "read", revoke_after_read)

    class Router:
        requests = []

        def context_window_for(self, agent_type, profile_name, user_pref):
            return context_window

        async def invoke(self, agent_type, request, **kwargs):
            self.requests.append(request)
            if admitted and len(self.requests) == 1:
                return LLMResponse(content="", tool_calls=[ToolCall("read", "read_report", {"section_ids": [section.id]})])
            return LLMResponse(content="The saved report says to collect missing data before choosing a priority.")

    router = Router()
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
        report_read_access=source.report_read_access,
    )
    before = await source.canvas_store.read_canvas_projection_state(canvas.id)
    from bowhead_core.agents.report_tools import register_report_tools

    registry = build_default_registry()
    register_report_tools(registry)
    bus = TurnEventBus()
    events = []

    async def consume():
        async for event in bus.subscribe():
            events.append(event)

    consumer = asyncio.create_task(consume())
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name="lead",
        task="What conclusion was saved?",
        parent_turn_id=source.turn_id,
        parent_bus=bus,
        parent_ctx=ctx,
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id=source.turn_id, payload={}))
    await consumer
    for event in events:
        if event.event_type in {"tool_result", "step_done"} and event.payload.get("tool_name") == "read_report":
            assert "Collect missing data before choosing a priority." not in json.dumps(event.payload)
    if mode == "revoked_before_dispatch":
        assert not result.ok and len(router.requests) == 1
        assert await source.canvas_store.read_canvas_projection_state(canvas.id) == before
        return
    assert result.ok, result
    assert ("read_report" in {schema.name for schema in router.requests[0].tools}) == admitted
    if not admitted:
        assert await source.canvas_store.read_canvas_projection_state(canvas.id) == before
        return
    tool_messages = [message for message in router.requests[1].messages if message.role == "tool" and message.name == "read_report"]
    assert len(tool_messages) == 1 and "Collect missing data before choosing a priority." in tool_messages[0].content
    from bowhead_core.subagent import _public_tool_result

    public, _ = _public_tool_result("read_report", json.loads(tool_messages[0].content))
    assert public == {"ok": True, "report_read": True}

    assert await source.canvas_store.read_canvas_projection_state(canvas.id) == before


async def test_knowledge_source_revocation_invalidates_retained_text(report_case, monkeypatch):
    from bowhead_core.source_attribution import KbFileAccess

    from bowhead_gateway import kb_citation_access

    ctx, section, canvas, request = report_case
    allowed = True

    async def judge(identity):
        return KbFileAccess(state="readable" if allowed else "forbidden")

    monkeypatch.setattr(kb_citation_access, "kb_file_access_for_user", lambda app, user, **kwargs: judge)
    await ctx.canvas_store.patch_section(
        section_id=section.id, expected_rev=section.rev, patch={"source_refs": ["kb:file_synthetic:chunk_synthetic"]}
    )
    result = await _read_report({"section_ids": [section.id]}, ctx)
    assert result.get("ok"), result
    dependencies = capture_context_dependencies(ctx)
    allowed = False
    with pytest.raises(OrdinaryContinuationUnavailable):
        await validate_context_dependencies(ctx, dependencies)
    assert (await _read_report({"section_ids": [section.id]}, ctx))["error"] == "not_found_or_unavailable"


async def test_persisted_native_receipts_deduplicate_and_reject_foreign_origins(report_case):
    from bowhead_core.result_material_history import load_material_history
    from bowhead_core.result_materials import capture_material_references
    from bowhead_core.types import ToolInvocation

    ctx, section, canvas, request = report_case
    invocation = ToolInvocation("write_report", {}, call_id="write", result={"ok": True, "section_id": section.id, "rev": section.rev})
    await capture_material_references(invocation, ctx, turn_id=ctx.turn_id, producer_id="writer")
    native = invocation.material_refs.model_dump(exclude_none=True)
    logs = request.app.state.turn_log_store
    for event_type, payload in [
        ("agent_spawned", {"subagent_id": "writer", "profile": "narrative"}),
        ("tool_result", {"subagent_id": "writer", "call_id": "write", "tool_name": "write_report", "result_meta": {"material_refs": native}}),
        ("lead.subagent_summary", {"subagent_id": "writer", "material_refs": native}),
    ]:
        await logs.record(turn_id=ctx.turn_id, event_type=event_type, payload=payload)
    foreign = json.loads(json.dumps(native))
    foreign["items"][0]["origin"]["turn_id"] = "t_foreign"
    await logs.record(
        turn_id=ctx.turn_id,
        event_type="tool_result",
        payload={"subagent_id": "writer", "call_id": "other", "tool_name": "write_report", "result_meta": {"material_refs": foreign}},
    )
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("UPDATE turns SET status='completed' WHERE id=?", (ctx.turn_id,))
        await db.commit()
    await ctx.session_store.finalize_turn(session_id=ctx.session_id, current_turn_id=ctx.turn_id, next_turn_id=None)
    await ctx.session_store.record_turn(turn_id="t_followup", session_id=ctx.session_id, user_text="Inspect the saved conclusion")
    ctx.turn_id = "t_followup"
    metadata = (await load_material_history(ctx, ["t_recovery"]))["t_recovery"]
    assert len(metadata["items"]) == 1
    assert metadata["items"][0]["origin"]["call_id"] == "write"
    assert metadata["coverage"]["references"] == "unknown"


@pytest.mark.parametrize(
    "tool,arguments,result",
    [
        ("python_exec", {}, {"output_artifacts": [{"artifact_id": "art_legacy"}]}),
        ("skill_invoke", {"name": "python_sandbox"}, {"output_artifacts": [{"artifact_id": "art_legacy"}]}),
        ("skill_invoke", {"name": "synthetic_skill"}, {"output_artifacts": [{"artifact_id": "art_legacy"}]}),
    ],
)
async def test_legacy_artifact_history_reads_native_dispatch_event_names(report_case, monkeypatch, tool, arguments, result):
    import asyncio

    from bowhead_core.events import OrchestratorEvent, TurnEventBus
    from bowhead_core.result_material_history import load_material_history
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

    from bowhead_router import ToolCall, ToolSchema

    ctx, _, _, request = report_case

    class Artifacts:
        available = True

        async def get_many(self, ids, *, include_deleted=False):
            return [await self.get(identity) for identity in ids] if self.available else []

        async def get(self, identity, **kwargs):
            return SimpleNamespace(id=identity, status="ready", created_by=ctx.user_id, workspace_id=ctx.workspace_id) if self.available else None

    artifacts = Artifacts()
    request.app.state.artifact_store = artifacts

    async def handler(args, execution_ctx):
        return result

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name=tool, description="Synthetic producer", input_schema={}), handler=handler))
    runner = SubagentRunner(router=SimpleNamespace(), tool_registry=registry)
    execution_ctx = ToolExecutionContext(
        workspace_id=ctx.workspace_id, member_uid="synthetic", user_id=ctx.user_id, turn_id=ctx.turn_id, datarc=None, role="lead"
    )
    bus = TurnEventBus()
    events = []

    async def consume():
        async for event in bus.subscribe():
            events.append(event)

    # Disabled admission reproduces the original event shape without new receipts.
    monkeypatch.setenv("FEATURE_CONTEXT_MATERIAL_RECOVERY", "0")
    consumer = asyncio.create_task(consume())
    await runner._execute_tool(
        ToolCall("legacy", tool, arguments), execution_ctx, bus, parent_turn_id=ctx.turn_id, subagent_id="compute", running_profile="compute"
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id=ctx.turn_id, payload={}))
    await consumer
    logs = request.app.state.turn_log_store
    await logs.record(turn_id=ctx.turn_id, event_type="agent_spawned", payload={"subagent_id": "compute", "profile": "compute"})
    for event in events:
        if event.event_type in {"tool_result", "step_done"}:
            assert "material_refs" not in (event.payload.get("result_meta") or {})
            await logs.record(turn_id=ctx.turn_id, event_type=event.event_type, payload=event.payload)
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("UPDATE turns SET status='completed' WHERE id=?", (ctx.turn_id,))
        await db.commit()
    await ctx.session_store.finalize_turn(session_id=ctx.session_id, current_turn_id=ctx.turn_id, next_turn_id=None)
    await ctx.session_store.record_turn(turn_id="t_followup", session_id=ctx.session_id, user_text="Inspect the saved result")
    ctx.turn_id = "t_followup"
    monkeypatch.setenv("FEATURE_CONTEXT_MATERIAL_RECOVERY", "1")
    metadata = (await load_material_history(ctx, ["t_recovery"]))["t_recovery"]
    assert [item["locator"]["artifact_id"] for item in metadata["items"]] == ["art_legacy"]
    assert metadata["coverage"]["references"] == "unknown"
    artifacts.available = False
    assert (await load_material_history(ctx, ["t_recovery"]))["t_recovery"]["items"] == []


@pytest.mark.parametrize("known_revision", [False, True])
async def test_canvas_mirrors_preserve_native_artifact_budget(report_case, known_revision):
    from bowhead_core.result_material_history import load_material_history
    from bowhead_core.result_materials import capture_material_references
    from bowhead_core.types import ToolInvocation

    ctx, section, canvas, request = report_case
    sections = [section]
    for i in range(16):
        sections.append(
            await ctx.canvas_store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": f"Section {i}"}, by_turn_id=ctx.turn_id)
        )
    inv = ToolInvocation("write_report", {}, call_id="write", result={"ok": True, "section_ids": [s.id for s in sections]})
    await capture_material_references(inv, ctx, turn_id=ctx.turn_id, producer_id="writer")
    assert len(inv.material_refs.items) == 17
    if known_revision:
        inv.material_refs = inv.material_refs.model_copy(
            update={
                "items": [
                    item.model_copy(update={"observed_version": {"rev": sections[index].rev}}) for index, item in enumerate(inv.material_refs.items)
                ]
            }
        )
    logs = request.app.state.turn_log_store
    await logs.record(turn_id=ctx.turn_id, event_type="agent_spawned", payload={"subagent_id": "writer", "profile": "narrative"})
    for s in sections:
        await logs.record(turn_id=ctx.turn_id, event_type="canvas.section_added", payload={"canvas_id": canvas.id, "section": s.to_report_section()})
    await logs.record(
        turn_id=ctx.turn_id,
        event_type="tool_result",
        payload={
            "subagent_id": "writer",
            "call_id": "write",
            "tool_name": "write_report",
            "result_meta": {"material_refs": inv.material_refs.model_dump(exclude_none=True)},
        },
    )
    updated = await ctx.canvas_store.patch_section(
        section_id=section.id, expected_rev=section.rev, patch={"payload": {"markdown": "Revised conclusion"}}
    )
    await logs.record(
        turn_id=ctx.turn_id, event_type="canvas.section_patched", payload={"canvas_id": canvas.id, "section": updated.to_report_section()}
    )
    from pathlib import Path

    from bowhead_core.artifact_store import ArtifactStore, LocalBackend

    artifacts = ArtifactStore(ctx.session_store.db_path, LocalBackend(Path(ctx.session_store.db_path).parent / "blobs"))
    await artifacts.initialize()
    ctx.artifact_store = artifacts
    request.app.state.artifact_store = artifacts
    files = [
        await artifacts.put_bytes(
            content=b"1",
            filename=f"data{i}.csv",
            mime="text/csv",
            source="python_sandbox",
            created_by=ctx.user_id,
            workspace_id=ctx.workspace_id,
            session_id=ctx.session_id,
        )
        for i in range(3)
    ]
    py = ToolInvocation("python_exec", {}, call_id="python", result={"output_artifacts": [{"artifact_id": a.id} for a in files]})
    await capture_material_references(py, ctx, turn_id=ctx.turn_id, producer_id="writer")
    assert len(py.material_refs.items) == 3
    await logs.record(
        turn_id=ctx.turn_id,
        event_type="tool_result",
        payload={
            "subagent_id": "writer",
            "call_id": "python",
            "tool_name": "python_sandbox",
            "result_meta": {"material_refs": py.material_refs.model_dump(exclude_none=True)},
        },
    )
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("UPDATE turns SET status='completed' WHERE id=?", (ctx.turn_id,))
        await db.commit()
    await ctx.session_store.finalize_turn(session_id=ctx.session_id, current_turn_id=ctx.turn_id, next_turn_id=None)
    await ctx.session_store.record_turn(turn_id="t_followup", session_id=ctx.session_id, user_text="Read")
    ctx.turn_id = "t_followup"
    data = (await load_material_history(ctx, ["t_recovery"]))["t_recovery"]
    unique = {item["locator"]["section_id"] for item in data["items"] if item["kind"] == "canvas_section"}
    recovered_files = {i["locator"]["artifact_id"] for i in data["items"] if i["kind"] == "artifact"}

    assert unique == {s.id for s in sections}
    assert recovered_files == {a.id for a in files}
    assert all(item["origin"]["kind"] == "tool" for item in data["items"][:20])
    if known_revision:
        assert len(data["items"]) == 21
        assert data["items"][-1]["observed_version"] == {"rev": updated.rev}
        assert data["coverage"]["references"] == "complete"
    else:
        assert data["coverage"]["references"] == "unknown"


@pytest.mark.parametrize("file_scope", ["team", "space", "personal"])
async def test_report_source_uses_bound_team_not_home_team(report_case, monkeypatch, file_scope):
    from bowhead_gateway.kb_citation_access import kb_file_access_for_user

    ctx, section, _canvas, request = report_case
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1")
    source_scope_id = {"team": ctx.team_id, "space": ctx.workspace_id, "personal": ctx.user_id}[file_scope]
    original_scope_id = source_scope_id

    async def get_file(identity):
        return SimpleNamespace(
            id=identity,
            scope=file_scope,
            scope_id=source_scope_id,
            filename="synthetic.txt",
            soft_deleted_at=None,
        )

    request.app.state.kb_files_store = SimpleNamespace(get=get_file)
    await ctx.canvas_store.patch_section(
        section_id=section.id, expected_rev=section.rev, patch={"source_refs": ["kb:file_synthetic:chunk_synthetic"]}
    )
    before = await _read_report({"section_ids": [section.id]}, ctx)
    assert before.get("ok"), before
    other = await request.app.state.user_store.create_team(name="Home Team", max_seats=5)
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES('membership_home',?,?,'member','active','t')",
            (ctx.user_id, other.id),
        )
        await db.execute("UPDATE users SET team_id=?,active_team_id=? WHERE id=?", (other.id, ctx.team_id, ctx.user_id))
        await db.commit()
    assert await ctx.report_read_access._canvas() is not None
    after = await _read_report({"section_ids": [section.id]}, ctx)

    assert after.get("ok"), after
    judge = kb_file_access_for_user(request.app, await request.app.state.user_store.get_user(ctx.user_id), expected_team_id=ctx.team_id)
    assert (await judge("file_synthetic")).state == "readable"
    other_space = await request.app.state.user_store.create_local_workspace(team_id=other.id, name="Other", creator_user_id=ctx.user_id)
    source_scope_id = {"team": other.id, "space": other_space, "personal": "u_other"}[file_scope]
    assert (await judge("file_synthetic")).state == "forbidden"
    source_scope_id = original_scope_id
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("UPDATE users SET active_team_id=? WHERE id=?", (other.id, ctx.user_id))
        await db.commit()
    assert (await _read_report({"section_ids": [section.id]}, ctx)).get("ok")
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("DELETE FROM team_members WHERE user_id=? AND team_id=?", (ctx.user_id, ctx.team_id))
        await db.commit()
    assert (await judge("file_synthetic")).state == "forbidden"
    assert (await _read_report({"section_ids": [section.id]}, ctx))["error"] == "report_reader_unavailable"


async def test_actor_bound_chart_projection_survives_http_credentials_but_rechecks_scope(report_case):
    ctx, _, canvas, request = report_case
    from bowhead_core.chart.authoring_tools import ChartProjectionUnavailable
    from bowhead_gateway.chart_authoring_projection import project_chart_for_authoring
    from fastapi import HTTPException

    artifact_id = "art_123456789abc"

    class Artifacts:
        available = True

        async def get_many(self, ids, *, include_deleted=False):
            return (
                [SimpleNamespace(id=item, created_by=ctx.user_id, workspace_id=ctx.workspace_id, status="ready") for item in ids]
                if self.available
                else []
            )

    request.app.state.artifact_store = Artifacts()
    chart = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={
            "spec": {
                "kind": "bar",
                "title": "Synthetic counts",
                "data_artifact_id": artifact_id,
                "source_meta": {"id": artifact_id, "artifact_id": artifact_id, "tool": "python_sandbox"},
                "columns": [{"key": "label", "header": "Label", "type": "categorical"}, {"key": "value", "header": "Value", "type": "numeric"}],
                "rows": [{"label": "A", "value": 25}],
                "row_count": 1,
            },
            "data_source": {"tool": "datarc", "context_id": "1", "context_name": "Synthetic context"},
        },
    )
    request.headers = {}
    request.app.state.settings = SimpleNamespace(auth_mode="prod")
    access = ctx.report_read_access
    with pytest.raises(HTTPException) as denied:
        await project_chart_for_authoring(request, ctx.session_id, chart)
    assert denied.value.status_code == 401
    projected = await access.project_chart(chart)
    assert projected["spec"]["rows"] == [{"label": "A", "value": 25}]
    request.app.state.artifact_store.available = False
    with pytest.raises(ChartProjectionUnavailable) as denied_source:
        await access.project_chart(chart)
    assert denied_source.value.reason == "source_access_denied"
    request.app.state.artifact_store.available = True
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("UPDATE workspace_members SET status='inactive' WHERE workspace_id=? AND user_id=?", (ctx.workspace_id, ctx.user_id))
        await db.commit()
    with pytest.raises(ChartProjectionUnavailable) as revoked:
        await access.project_chart(chart)
    assert revoked.value.reason == "scope_unavailable"
    assert isinstance(revoked.value.__cause__, ValueError) and "reference_scope_unavailable" in str(revoked.value.__cause__)


async def test_chart_palette_reuses_effective_style_and_offers_a_named_green(report_case, monkeypatch):
    ctx, _, _, request = report_case
    from bowhead_core.chart_style_read_service import ChartStyleReadService
    from bowhead_core.team_settings_store import TeamSettingsStore
    from bowhead_core.user_settings_store import UserSettingsStore

    state = request.app.state
    path = ctx.session_store.db_path
    state.chart_style_read_service = ChartStyleReadService(path)
    state.team_settings_store = TeamSettingsStore(path)
    state.user_settings_store = UserSettingsStore(path)
    options = await ctx.report_read_access.chart_palette()
    assert any(option["family"] == "green" for option in options)
    assert [option["index"] for option in options] == list(range(6))
    assert all(option["light"].startswith("#") and option["dark"].startswith("#") for option in options)

    from bowhead_gateway.routes import theme

    original = theme.resolve_chart_style_v2_for_current_theme

    async def resolve(request, **kwargs):
        assert kwargs["user"].team_id == ctx.team_id
        assert kwargs["user"].active_team_id == ctx.team_id
        return await original(request, **kwargs)

    monkeypatch.setattr(theme, "resolve_chart_style_v2_for_current_theme", resolve)
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1")
    other = await state.user_store.create_team(name="Other home team", max_seats=5)
    async with open_shared_ctx(path) as db:
        await db.execute("UPDATE users SET team_id=? WHERE id=?", (other.id, ctx.user_id))
        await db.commit()
    assert await ctx.report_read_access.chart_palette() == options
