"""Native retained-file delivery uses real storage and the ordered event writer."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from bowhead_core.artifact_presentation import NativePresentationResult, normalize_producer_declaration, present_artifacts
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import _tool_result_meta
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from test_chat_reference_storage import case  # noqa: F401
from test_subagent_step_emit_contract import _make_runner

from bowhead_router import ToolCall

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "arguments",
    [
        {"artifacts": [{"artifact_id": "art_file", "path": "/private/source"}]},
        {"artifacts": [{"artifact_id": "art_file", "title": "x" * 121}]},
        {"artifacts": [{"artifact_id": "art_file"}] * 17},
        {"artifacts": [{"artifact_id": "art_file"}], "session_id": "s_foreign"},
    ],
)
async def test_model_arguments_cannot_override_scope_paths_or_metadata_bounds(arguments):
    result = await present_artifacts(arguments, SimpleNamespace(artifact_presentation_entry=True))
    assert result["error"] == "artifact_presentation_invalid_arguments"
    assert "/private/source" not in json.dumps(result)


@pytest_asyncio.fixture
async def presentation(case):  # noqa: F811
    store = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "files"))
    sessions = SessionStore(str(case.path))
    await sessions.ensure_session(session_id="s_old", workspace_id=case.scope.workspace_id, user_id=case.scope.user_id)
    artifact = await store.put_bytes(
        content=b"retained source",
        filename="main.tex",
        mime="text/plain",
        source="python_sandbox",
        created_by=case.scope.user_id,
        session_id="s_old",
        kind="deliverable",
    )
    ctx = ToolExecutionContext(
        workspace_id=case.scope.workspace_id,
        member_uid="member",
        user_id=case.scope.user_id,
        turn_id="t_ref",
        datarc=None,
        role="lead",
        team_id=case.scope.team_id,
        session_id="s_ref",
        artifact_store=store,
        session_store=sessions,
        execution_generation=1,
        artifact_presentation_entry=True,
        artifact_presentation_call=("entry", "call_present"),
    )
    return ctx, artifact


async def _execute(ctx, bus, args, *, handler=present_artifacts):
    return await _make_runner({"present_artifacts": handler})._execute_tool(
        ToolCall(id="call_present", name="present_artifacts", arguments=args),
        ctx,
        bus,
        parent_turn_id=ctx.turn_id,
        subagent_id="entry",
        running_profile="lead",
    )


async def test_retained_file_reuse_is_durable_idempotent_and_snapshot_bounded(presentation):
    ctx, artifact = presentation
    logs = TurnLogStore(ctx.artifact_store.db_path)
    bus = TurnEventBus(log_sink=OrderedTurnLogSink(logs))
    before = await ctx.artifact_store.results_high_water(session_id=ctx.session_id, user_id=ctx.user_id)
    args = {"artifacts": [{"artifact_id": artifact.id, "title": "Editable source"}, {"artifact_id": artifact.id}]}
    result = await _execute(ctx, bus, args)
    assert result.result["ok"] is True
    events = [event for event in bus._events if event.event_type == "tool_result"]
    assert len(events) == 1
    assert events[0].persist_before_delivery
    metadata = events[0].payload["result_meta"]
    assert metadata["artifact_presentation"]["items"] == [args["artifacts"][0]]
    with sqlite3.connect(ctx.artifact_store.db_path) as db:
        stored = db.execute("SELECT payload_json FROM turn_events WHERE event_type='tool_result'").fetchone()
        assert json.loads(stored[0])["result_meta"] == metadata
        count_before = db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    again = await _execute(ctx, bus, args)
    assert again.result["ok"] is True
    assert len([event for event in bus._events if event.event_type == "tool_result"]) == 1
    changed = await _execute(ctx, bus, {"artifacts": [{"artifact_id": artifact.id, "title": "Changed"}]})
    assert changed.result["error"] == "artifact_presentation_call_conflict"
    after = await ctx.artifact_store.results_high_water(session_id=ctx.session_id, user_id=ctx.user_id)
    assert after.presentation_high_water > before.presentation_high_water
    old_page = await ctx.artifact_store.query_results_for_session(
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        snapshot=before,
        normalized_query="",
    )
    new_page = await ctx.artifact_store.query_results_for_session(
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        snapshot=after,
        normalized_query="",
    )
    assert not old_page.entries
    assert [entry.artifact.id for entry in new_page.entries] == [artifact.id]
    assert [a.id for a in await ctx.artifact_store.list_for_session(session_id=ctx.session_id, user_id=ctx.user_id)] == [artifact.id]
    with sqlite3.connect(ctx.artifact_store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == count_before


@pytest.mark.parametrize("failure", ["missing", "reject", "raise"])
async def test_receipt_failure_never_exposes_success_or_removes_bytes(presentation, failure):
    ctx, artifact = presentation
    ordered = OrderedTurnLogSink(TurnLogStore(ctx.artifact_store.db_path))

    async def sink(event):
        if event.event_type == "tool_result":
            if failure == "raise":
                raise OSError("storage unavailable")
            return False
        return await ordered(event)

    bus = TurnEventBus(log_sink=None if failure == "missing" else sink)
    with pytest.raises(TerminalDurabilityAbort):
        await _execute(ctx, bus, {"artifacts": [{"artifact_id": artifact.id}]})
    assert not any(event.event_type in {"tool_result", "step_done"} for event in bus._events)
    assert await ctx.artifact_store.backend.head(artifact.path) is not None


@pytest.mark.parametrize("denial", ["helper", "eval", "revoked", "generation", "missing_bytes", "internal", "owner", "private_draft", "mixed_batch"])
async def test_present_rejects_unavailable_scope_and_entire_batch(presentation, denial):
    ctx, artifact = presentation
    args = {"artifacts": [{"artifact_id": artifact.id}]}
    if denial == "helper":
        ctx.artifact_presentation_entry = False
    elif denial == "eval":
        ctx.eval_datarc = object()
    elif denial == "generation":
        ctx.execution_generation = 2
    elif denial == "missing_bytes":
        await ctx.artifact_store.backend.delete(artifact.path)
    elif denial == "mixed_batch":
        args["artifacts"].append({"artifact_id": "art_unavailable"})
    else:
        with sqlite3.connect(ctx.artifact_store.db_path) as db:
            if denial == "revoked":
                db.execute("UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?", (ctx.workspace_id, ctx.user_id))
            elif denial == "owner":
                db.execute("UPDATE artifacts SET created_by='another_user' WHERE id=?", (artifact.id,))
            else:
                producer = "template_document_v1" if denial == "private_draft" else "python_sandbox_stdout_v1"
                db.execute("UPDATE artifacts SET producer_kind=? WHERE id=?", (producer, artifact.id))
    result = await present_artifacts(args, ctx)
    assert result["ok"] is False
    assert not isinstance(result, NativePresentationResult)
    assert "main.tex" not in json.dumps(result)


async def test_native_metadata_survives_large_result_and_cannot_be_forged(presentation, monkeypatch):
    ctx, artifact = presentation
    monkeypatch.setenv("BOWHEAD_PHI_SSE_INLINE_MAX_BYTES", "128")

    async def native(args, scope):
        result = await present_artifacts(args, scope)
        result["diagnostic"] = "large output " * 5000
        return result

    bus = TurnEventBus(log_sink=OrderedTurnLogSink(TurnLogStore(ctx.artifact_store.db_path)))
    await _execute(ctx, bus, {"artifacts": [{"artifact_id": artifact.id}]}, handler=native)
    event = next(event for event in bus._events if event.event_type == "tool_result")
    assert event.payload["result_inline"] is None
    assert event.payload["result_meta"]["artifact_presentation"]["items"] == [{"artifact_id": artifact.id}]
    assert "artifact_presentation" not in (_tool_result_meta({"artifact_presentation": event.payload["result_meta"]["artifact_presentation"]}) or {})


async def test_failed_producer_preserves_valid_source_and_failure(presentation):
    ctx, artifact = presentation
    with sqlite3.connect(ctx.artifact_store.db_path) as db:
        db.execute("UPDATE artifacts SET created_by_turn=? WHERE id=?", (ctx.turn_id, artifact.id))
    output = {"ok": False, "error": "PDF compilation failed", "output_artifacts": [{"artifact_id": artifact.id, "name": "main.tex"}]}
    result = await normalize_producer_declaration("skill_invoke", {"name": "latex-report"}, output, ctx)
    assert isinstance(result, NativePresentationResult)
    assert result == output
    assert result.declaration["items"] == [{"artifact_id": artifact.id}]
    assert result.declaration["origin"] == "producer_declared"


async def test_future_contract_is_explicitly_unsupported_and_malformed_history_is_safe(presentation):
    from bowhead_core.artifact_presentation import PresentationContractUnsupported, project_declaration

    ctx, artifact = presentation
    result = await present_artifacts({"artifacts": [{"artifact_id": artifact.id}]}, ctx)
    metadata = result.result_metadata()
    metadata["artifact_presentation"]["contract"] = "artifact-presentation.v99"
    with pytest.raises(PresentationContractUnsupported):
        project_declaration(metadata)
    with sqlite3.connect(ctx.artifact_store.db_path) as db:
        for seq, payload in enumerate(
            ("{", '{"result_meta":{"artifact_presentation":{"items":["bad"]}}}', json.dumps({"result_meta": metadata})), 100
        ):
            db.execute(
                "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES(?,? ,?,'tool_result',?,'2026-10-28')",
                (f"evt_history_{seq}", ctx.turn_id, seq, payload),
            )
    with pytest.raises(PresentationContractUnsupported):
        await ctx.artifact_store.results_high_water(session_id=ctx.session_id, user_id=ctx.user_id)


async def test_terminal_protocol_preserves_native_receipt_metadata(tmp_path):
    from bowhead_core.events import OrchestratorEvent, emit_required_tool_result
    from test_terminal_runtime import _create_db, _runtime

    runtime = await _runtime(_create_db(tmp_path))
    metadata = {"artifact_presentation": {"contract": "artifact-presentation.v1", "origin": "explicit_tool", "items": [{"artifact_id": "art_a"}]}}
    await emit_required_tool_result(
        runtime.bus, OrchestratorEvent("tool_result", "t_1", {"call_id": "call_a", "result_meta": metadata}, persist_before_delivery=True)
    )
    with sqlite3.connect(runtime.store.db_path) as db:
        row = db.execute("SELECT payload_json FROM turn_events WHERE turn_id='t_1' AND event_type='tool_result'").fetchone()
    assert json.loads(row[0])["result_meta"] == metadata
    live = runtime.bus.snapshot()
    assert next(event for event in live if event.event_type == "tool_result").payload["result_meta"] == metadata


@pytest.mark.parametrize("entry", [False, True])
async def test_runner_uses_explicit_entry_dispatch_without_inheriting_parent_authority(monkeypatch, entry):
    from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
    from bowhead_core.tool_registry import ToolRegistry, ToolSchema, ToolSpec
    from test_subagent import _router

    from bowhead_router import LLMResponse

    observed = []

    async def probe(arguments, ctx):
        observed.append((ctx.artifact_presentation_entry, ctx.artifact_presentation_call))
        return {"ok": True}

    profile = SubagentProfile(
        name="presentation-probe", llm_agent_type="echo", tool_whitelist=("probe_entry",), system_prompt="Return the tool result."
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="probe_entry", description="Check entry", input_schema={}), handler=probe))
    router = _router(
        LLMResponse(content="", tool_calls=[ToolCall(id="call_probe", name="probe_entry", arguments={})]), LLMResponse(content="Complete.")
    )
    parent = ToolExecutionContext(
        workspace_id="ws_probe",
        member_uid="member",
        user_id="user_probe",
        turn_id="t_probe",
        datarc=None,
        role="lead",
        artifact_presentation_entry=True,
    )
    await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name,
        task="Check the entry",
        parent_turn_id=parent.turn_id,
        parent_bus=TurnEventBus(),
        parent_ctx=parent,
        artifact_presentation_entry=entry,
    )
    assert len(observed) == 1
    assert observed[0][0] is entry
    assert observed[0][1][1] == "call_probe"
    assert parent.artifact_presentation_entry is True


@pytest.mark.parametrize("probe", ["unavailable", "wrong_size"])
async def test_unknown_object_storage_probe_never_becomes_presentation_success(presentation, monkeypatch, probe):
    from bowhead_core.artifact_store import ObjectHead

    ctx, artifact = presentation

    async def head(path):
        if probe == "unavailable":
            raise RuntimeError("provider-specific failure must not reach the user")
        return ObjectHead(size=artifact.size + 1, etag=None, mime=None)

    monkeypatch.setattr(ctx.artifact_store.backend, "head", head)
    result = await present_artifacts({"artifacts": [{"artifact_id": artifact.id}]}, ctx)
    assert result["ok"] is False
    assert "provider-specific" not in json.dumps(result)
