"""Source ownership through original Lead/Tool/helper execution paths."""

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bowhead_bi_bridge.types import CutResult, CutToken, DownloadDataResult, MeasureMeta
from bowhead_core.agents.lead_orchestrator import _clarification_resume_context
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.datarc_selection import (
    DatarcConnectionBinding,
    DatarcPersistenceReceipt,
    accepted_datarc_selection,
    current_datarc_selection,
    ensure_datarc_run,
    fork_datarc_run,
    project_datarc_scope,
    restore_datarc_selection,
    switch_datarc_selection,
)
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import AgentBinding, LLMChunk, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, ToolCall

QUERY = "2025-09 revenue by month"
ROSTER = ({"id": "1", "name": "Source A"}, {"id": "2", "name": "Source B"})


class Data:
    def __init__(self):
        self.calls = []

    async def download_data(self, *, datarc_workspace_id, member_uid, text):
        self.calls.append((datarc_workspace_id, member_uid))
        return DownloadDataResult(columns=["source", "revenue"], rows=[{"source": datarc_workspace_id, "revenue": 123}], total_rows=1)

    async def cut_question(self, **kwargs):
        return CutResult(tokens=[CutToken(word="revenue", prop="m1", unit_idx=0)])

    async def list_measures(self, *args, **kwargs):
        return [MeasureMeta(id=1, names=["revenue"])]

    async def list_dimensions(self, *args, **kwargs):
        return []

    def measure_metadata(self, *args, **kwargs):
        return {}


class Switcher:
    def __init__(self, *, persistence="saved", fail=False):
        self.prepared = []
        self.persistence = persistence
        self.fail = fail

    async def prepare_selection(self, identifier, name, *, current):
        if self.fail:
            raise ValueError("Controlled membership failure")
        self.prepared.append(identifier)
        return replace(current, datarc_workspace_id=identifier, name=name, member_uid="member_" + identifier)

    async def save_preference(self, selection, *, remember):
        return DatarcPersistenceReceipt(self.persistence, "default" if remember else "session")


def context(*, data=None, switcher=None):
    data = data or Data()
    return ToolExecutionContext(
        team_id="team_one",
        workspace_id="ws_one",
        user_id="usr_actor",
        session_id="s_one",
        turn_id="t_" + uuid4().hex,
        datarc=data,
        active_datarc_project="1",
        active_datarc_workspace_name="Source A",
        member_uid="member_1",
        accessible_datarc_workspaces=ROSTER,
        datarc_context_switcher=switcher or Switcher(),
        datarc_binding=DatarcConnectionBinding("team_one", "dc_one", "revision_one", data),
        role="lead",
    )


def router(responses):
    class ScriptedAdapter(MockAdapter):
        async def stream(self, request):
            response = await self.invoke(request)
            if response.content:
                yield LLMChunk(delta=response.content)
            for call in response.tool_calls:
                yield LLMChunk(tool_call_delta=call)
            yield LLMChunk(finish_reason=response.finish_reason, usage=response.usage)

    adapter = ScriptedAdapter(provider_name="context-test")
    adapter.scripted_responses = list(responses)
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="context-test", type="mock")])
    providers._adapters["context-test"] = adapter
    result = Router(providers)
    for name in ("lead", "analyst"):
        result.bind(AgentBinding(agent_type=name, primary=("context-test", "mock")))
    return result


async def consume(bus):
    async for _event in bus.subscribe():
        pass


async def run_lead(ctx, responses):
    registry = build_default_registry()
    register_subagent_tool(registry)
    prior = PROFILES.get("lead")
    PROFILES["lead"] = SubagentProfile(
        name="lead",
        llm_agent_type="lead",
        tool_whitelist=("tool_switch_context", "subagent_spawn", "bi_query"),
        system_prompt="Execute the controlled test actions.",
        max_iterations=8,
    )
    PROFILES["context-test-peer"] = SubagentProfile(
        name="context-test-peer",
        llm_agent_type="analyst",
        tool_whitelist=("bi_query",),
        system_prompt="Run the requested query.",
        max_iterations=4,
    )
    runner = SubagentRunner(router=router(responses), tool_registry=registry)
    bus = TurnEventBus()
    consumer = asyncio.create_task(consume(bus))
    ctx.subagent_runner, ctx.parent_bus = runner, bus
    try:
        return await runner.run(profile_name="lead", task="Use Source B for the query.", parent_turn_id=ctx.turn_id, parent_bus=bus, parent_ctx=ctx)
    finally:
        await bus.emit(OrchestratorEvent(event_type="done", turn_id=ctx.turn_id, payload={}))
        await consumer
        PROFILES.pop("context-test-peer", None)
        if prior is None:
            PROFILES.pop("lead", None)
        else:
            PROFILES["lead"] = prior


async def test_lead_switch_publishes_to_fresh_helper_and_outer_snapshot():
    ctx = context()
    outcome = await run_lead(
        ctx,
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="switch", name="tool_switch_context", arguments={"name": "Source B"})]),
            LLMResponse(
                content="", tool_calls=[ToolCall(id="spawn", name="subagent_spawn", arguments={"profile": "context-test-peer", "task": QUERY})]
            ),
            LLMResponse(content="", tool_calls=[ToolCall(id="query", name="bi_query", arguments={"queries": [QUERY]})]),
            LLMResponse(content="The peer query has settled."),
            LLMResponse(content="The Lead has settled."),
        ],
    )
    assert outcome.ok
    assert ctx.datarc.calls == [("2", "member_2")]
    assert current_datarc_selection(ctx).datarc_workspace_id == "2"
    snapshot = _clarification_resume_context(SimpleNamespace(turn_id=ctx.turn_id, session_id=ctx.session_id), QUERY, ctx)
    assert snapshot["active_datarc_project"] == "2"
    assert snapshot["datarc_selection"]["member_uid"] == "member_2"


@pytest.mark.parametrize("other", ["bi_query", "subagent_spawn", "tool_switch_context"])
async def test_switch_batch_refuses_every_call_before_handlers(other):
    ctx = context()
    outcome = await run_lead(
        ctx,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="first", name="tool_switch_context", arguments={"name": "Source B"}),
                    ToolCall(
                        id="second", name=other, arguments={"queries": [QUERY], "name": "Source B", "profile": "context-test-peer", "task": QUERY}
                    ),
                ],
            ),
            LLMResponse(content="The batch was refused."),
        ],
    )
    assert ctx.datarc.calls == []
    assert ctx.datarc_context_switcher.prepared == []
    assert len(outcome.tool_invocations) == 2
    assert all(item.result["error_kind"] == "context_switch_batch_conflict" for item in outcome.tool_invocations)


async def test_admitted_helper_and_batch_remain_on_original_source():
    ctx = context()
    state = ensure_datarc_run(ctx)
    call = copy.copy(ctx)
    project_datarc_scope(call, state.current)
    child = fork_datarc_run(call)
    await switch_datarc_selection(ctx, "2", "Source B", remember=False)
    fresh_call = copy.copy(ctx)
    project_datarc_scope(fresh_call, state.current)
    fresh = fork_datarc_run(fresh_call)
    assert accepted_datarc_selection(call).datarc_workspace_id == "1"
    assert current_datarc_selection(child).member_uid == "member_1"
    assert current_datarc_selection(fresh).member_uid == "member_2"
    assert child.datarc_run_state is not ctx.datarc_run_state


async def test_failed_save_is_runtime_only_and_member_failure_changes_nothing():
    ctx = context(switcher=Switcher(persistence="failed"))
    result = await switch_datarc_selection(ctx, "2", "Source B", remember=True)
    assert result["effective"] == "run"
    assert result["persistence"]["status"] == "failed"
    assert current_datarc_selection(ctx).datarc_workspace_id == "2"
    ctx.datarc_context_switcher = Switcher(fail=True)
    with pytest.raises(ValueError, match="membership"):
        await switch_datarc_selection(ctx, "1", "Source A", remember=False)
    assert current_datarc_selection(ctx).datarc_workspace_id == "2"


async def test_direct_stale_switch_cannot_overwrite_new_selection():
    ctx = context()
    ensure_datarc_run(ctx)
    stale = copy.copy(ctx)
    project_datarc_scope(stale, ctx.datarc_run_state.current)
    await switch_datarc_selection(ctx, "2", "Source B", remember=False)
    result = await switch_datarc_selection(stale, "1", "Source A", remember=False)
    assert result["error_kind"] == "datarc_selection_conflict"
    assert current_datarc_selection(ctx).datarc_workspace_id == "2"


async def test_source_caches_and_semantic_guidance_are_bound_to_workspace(tmp_path):
    ctx = context()
    ctx.artifact_store = ArtifactStore(str(tmp_path / "artifacts.db"), LocalBackend(tmp_path / "blobs"))
    await ctx.artifact_store.initialize()
    handler = build_default_registry().get("bi_query").handler
    first = await handler({"queries": [QUERY]}, ctx)
    repeated = await handler({"queries": [QUERY]}, ctx)
    assert first["artifact_id"] == repeated["artifact_id"]
    await switch_datarc_selection(ctx, "2", "Source B", remember=False)
    second = await handler({"queries": [QUERY]}, ctx)
    assert first["artifact_id"] != second["artifact_id"]
    assert "_cache_hint" not in second
    assert ctx.datarc.calls == [("1", "member_1"), ("2", "member_2")]
    variant = await handler({"queries": [QUERY + " Top 10"]}, ctx)
    assert variant["_cache_hint"]["prior_artifact_id"] == second["artifact_id"]
    await switch_datarc_selection(ctx, "1", "Source A", remember=False)
    third = await handler({"queries": [QUERY]}, ctx)
    assert third["artifact_id"] == first["artifact_id"]
    assert len(ctx.datarc.calls) == 3


@pytest.mark.parametrize("change", ["connection", "revision", "member"])
async def test_exact_cache_does_not_cross_changed_binding_or_member(tmp_path, change):
    ctx = context()
    ctx.artifact_store = ArtifactStore(str(tmp_path / "artifacts.db"), LocalBackend(tmp_path / "blobs"))
    await ctx.artifact_store.initialize()
    handler = build_default_registry().get("bi_query").handler
    first = await handler({"queries": [QUERY]}, ctx)
    selection = current_datarc_selection(ctx)
    changed = (
        replace(selection, binding=replace(selection.binding, connection_id="dc_other"))
        if change == "connection"
        else replace(selection, binding=replace(selection.binding, revision="rotated"))
        if change == "revision"
        else replace(selection, member_uid="member_changed")
    )
    ctx.datarc_run_state.publish(changed)
    project_datarc_scope(ctx, ctx.datarc_run_state.current)
    second = await handler({"queries": [QUERY]}, ctx)
    assert second["artifact_id"] != first["artifact_id"]
    assert len(ctx.datarc.calls) == 2


async def test_continue_reauthorizes_original_source_without_changing_parent():
    ctx = context()
    saved = current_datarc_selection(ctx).snapshot()
    await switch_datarc_selection(ctx, "2", "Source B", remember=False)
    restored = await restore_datarc_selection(ctx, saved)
    child = fork_datarc_run(ctx, selection=restored)
    assert current_datarc_selection(child).datarc_workspace_id == "1"
    assert current_datarc_selection(ctx).datarc_workspace_id == "2"
    ctx.datarc_context_switcher = Switcher(fail=True)
    with pytest.raises(ValueError, match="membership"):
        await restore_datarc_selection(ctx, saved)


async def test_inflight_query_and_later_child_queries_keep_the_admitted_source(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class GatedData(Data):
        async def download_data(self, **kwargs):
            started.set()
            await release.wait()
            return await super().download_data(**kwargs)

    ctx = context(data=GatedData())
    name = "inflight-source-test"
    monkeypatch.setitem(
        PROFILES,
        name,
        SubagentProfile(
            name=name,
            llm_agent_type="analyst",
            tool_whitelist=("bi_query",),
            system_prompt="Run the controlled queries.",
            max_iterations=4,
        ),
    )

    async def run_peer(queries):
        responses = [
            LLMResponse(content="", tool_calls=[ToolCall(id=f"query_{index}", name="bi_query", arguments={"queries": [query]})])
            for index, query in enumerate(queries)
        ]
        responses.append(LLMResponse(content="The requested queries settled."))
        bus = TurnEventBus()
        consumer = asyncio.create_task(consume(bus))
        try:
            return await SubagentRunner(router=router(responses), tool_registry=build_default_registry()).run(
                profile_name=name,
                task=QUERY,
                parent_turn_id=ctx.turn_id,
                parent_bus=bus,
                parent_ctx=ctx,
            )
        finally:
            await bus.emit(OrchestratorEvent(event_type="done", turn_id=ctx.turn_id, payload={}))
            await consumer

    task = asyncio.create_task(run_peer([QUERY, "2025-08 revenue by month"]))
    await started.wait()
    await switch_datarc_selection(ctx, "2", "Source B", remember=False)
    release.set()
    assert (await task).ok
    assert (await run_peer([QUERY])).ok
    assert ctx.datarc.calls == [("1", "member_1"), ("1", "member_1"), ("2", "member_2")]


async def test_schema_cache_isolates_member_and_connection_with_overlapping_workspace_ids():
    from bowhead_core.datarc_schema_cache import DatarcSchemaCache, get_schema_with_cache

    class Metadata(Data):
        def __init__(self, name):
            super().__init__()
            self.name, self.reads = name, 0

        async def get_workspace(self, workspace):
            self.reads += 1
            return SimpleNamespace(name=self.name, desc="")

    cache = DatarcSchemaCache(max_entries=2)
    first, second = context(data=Metadata("First source")), context(data=Metadata("Second source"))
    second.datarc_binding = replace(second.datarc_binding, connection_id="dc_other")
    for ctx in (first, second):
        ctx.schema_cache = cache
        ensure_datarc_run(ctx)
    for ctx in (first, second, first):
        schema = await get_schema_with_cache(datarc_client=ctx.datarc, datarc_workspace_id="1", schema_cache=ctx.schema_cache)
        assert schema.workspace_name == ctx.datarc.name
    assert (first.datarc.reads, second.datarc.reads) == (1, 1)
    first.datarc_run_state.publish(replace(current_datarc_selection(first), member_uid="changed_member"))
    ensure_datarc_run(first)
    assert first.schema_cache.get("1") is None
    assert cache.stats()["size"] == 2


async def test_original_helper_capture_claim_and_continue_restore_its_source(tmp_path, monkeypatch):
    from bowhead_core.session_store import SessionStore
    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from test_subagent_resume_store import _db

    from bowhead_core import subagent_resume as sr

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=")
    path = await _db(tmp_path)
    ctx = context()
    ctx.team_id, ctx.user_id, ctx.workspace_id, ctx.session_id, ctx.turn_id = "team_1", "u_1", "ws_1", "s_1", "t_1"
    ctx.datarc_binding = replace(ctx.datarc_binding, team_id="team_1")
    ctx.session_store = SessionStore(path)
    name = "source-continue-test"
    monkeypatch.setitem(
        PROFILES,
        name,
        SubagentProfile(
            name=name, llm_agent_type="analyst", tool_whitelist=("bi_query", "subagent_return"), system_prompt="Query and finish.", max_iterations=4
        ),
    )

    async def run(responses, **kwargs):
        bus = TurnEventBus()
        consumer = asyncio.create_task(consume(bus))
        try:
            registry = build_default_registry()
            register_subagent_return_tool(registry)
            return await SubagentRunner(router=router(responses), tool_registry=registry).run(
                profile_name=name,
                task=QUERY,
                parent_turn_id=ctx.turn_id,
                parent_bus=bus,
                parent_ctx=ctx,
                **kwargs,
            )
        finally:
            await bus.emit(OrchestratorEvent(event_type="done", turn_id=ctx.turn_id, payload={}))
            await consumer

    query = LLMResponse(content="", tool_calls=[ToolCall(id="query", name="bi_query", arguments={"queries": [QUERY]})])
    finished = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="returned",
                name="subagent_return",
                arguments={"payload": {"delivery_surface": "chat", "exec_summary": "The accepted source was queried."}},
            )
        ],
    )
    first = await run([query, finished], pre_minted_subagent_id="sub_source")
    assert first.resume_checkpoint_id
    await switch_datarc_selection(ctx, "2", "Source B", remember=False)
    ctx.turn_id = "t_2"
    checkpoint = await sr.claim(path, checkpoint_id=first.resume_checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_2")
    material = sr.open_material(checkpoint, db_path=path)
    assert material.datarc_selection.datarc_workspace_id == "1"
    grant = sr.ResumeGrant(checkpoint=checkpoint, material=material, instruction="Check the remaining period.")
    second = await run([query, finished], pre_minted_subagent_id="sub_source", resume_grant=grant)
    assert second.ok
    assert ctx.datarc.calls == [("1", "member_1"), ("1", "member_1")]
    assert current_datarc_selection(ctx).datarc_workspace_id == "2"
    fresh_grant = replace(grant, refused="tools_changed")
    fresh = await run([query, finished], pre_minted_subagent_id="sub_source_fresh", resume_grant=fresh_grant)
    assert fresh.ok
    assert ctx.datarc.calls[-1] == ("2", "member_2")
    legacy = replace(grant, material=replace(material, datarc_selection=None))
    refused = await run([], pre_minted_subagent_id="sub_source", resume_grant=legacy)
    assert refused.error == "datarc_resume_scope_missing"
    owner = ctx.datarc_run_state
    owner.publish(replace(owner.current.selection, binding=replace(ctx.datarc_binding, revision="rotated")))
    refused = await run([], pre_minted_subagent_id="sub_source", resume_grant=grant)
    assert refused.error == "datarc_resume_scope_changed"


@pytest.mark.parametrize("corruption", ["future_material", "future_source", "missing_source", "unknown_field", "missing_member"])
def test_helper_material_v4_round_trips_source_and_refuses_invalid_evidence(corruption):
    import json
    import zlib

    from bowhead_router.types import Message

    from bowhead_core import subagent_resume as sr

    source = current_datarc_selection(context()).snapshot()
    packed, _ = sr.encode_material(
        messages=[Message(role="user", content=QUERY)], reference_inventory=[], max_sealed=1024 * 1024, datarc_selection=source
    )
    assert sr.decode_material(packed).datarc_selection == source
    value = json.loads(zlib.decompress(packed))
    if corruption == "future_material":
        value["schema_version"] = 5
    elif corruption == "future_source":
        value["datarc_selection"]["contract"] = "datarc-selection.v2"
    elif corruption == "missing_source":
        value.pop("datarc_selection")
    elif corruption == "unknown_field":
        value["datarc_selection"]["assumed_source"] = "another_workspace"
    else:
        value["datarc_selection"]["member_uid"] = ""
    with pytest.raises(ValueError):
        sr.decode_material(zlib.compress(json.dumps(value).encode()))
