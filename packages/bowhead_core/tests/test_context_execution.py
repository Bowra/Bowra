"""Owned inline compaction with real worker storage and actual Router rendering."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.context_execution import ExecutionContextView
from bowhead_core.context_store import read_context_graph_in_tx
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from bowhead_router.types import Message
from test_agent_collaboration import collab as collaboration_fixture
from test_context_maintenance import context_case as maintenance_fixture

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration


def owner(case, **changes):
    return ExecutionContextView(
        db_path=case.path,
        worker=case.worker,
        execution_id=changes.pop("execution_id", case.requester),
        lane=changes.pop("lane", "ordinary"),
        cipher=case.store.cipher,
        validate_sources=AsyncMock(),
        activity=AsyncMock(),
        **changes,
    )


async def prepare(execution, case, request=None, usage=None, **options):
    return await execution.prepare(
        request or case.request,
        router=case.router,
        agent_type="lead",
        profile="lead",
        user_pref=None,
        controller=case.args["controller"],
        current_request=8,
        canonical_count=len((request or case.request).messages),
        timeout=10,
        max_growth_tokens=12500,
        stream=True,
        record_usage=(usage if usage is not None else []).append,
        **options,
    )


async def test_model_projection_restores_without_replacing_original_continuation_messages(collab, context_case):
    execution = owner(collab)
    native = deepcopy(context_case.request.messages)
    usage = []
    result = await prepare(execution, context_case, usage=usage)
    assert result.candidate is not None and len(usage) == 1
    assert context_case.request.messages == native
    assert execution.view.project(native) == result.request.messages
    restored = owner(collab)
    await restored.restore()
    appended = [*native, Message(role="assistant", content="Confirmed prior source; next step remains open.")]
    assert restored.view.project(appended) == [*result.request.messages, appended[-1]]
    next_result = await prepare(restored, context_case, replace(context_case.request, messages=appended))
    assert next_result.candidate is None and next_result.request.messages == restored.view.project(appended)
    assert len(context_case.sent) == 1
    phases = [call.args[0]["phase"] for call in execution.activity.await_args_list]
    assert phases == ["running", "committed"]
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN")
        graph = await read_context_graph_in_tx(
            db,
            worker=collab.worker,
            execution_id=collab.requester,
            lane="ordinary",
            generation_id=execution.generation,
            cipher=collab.store.cipher,
            validate_sources=AsyncMock(),
        )
    assert any(record.messages == native for record in graph.values())


async def test_commit_failure_retains_original_model_view_and_counts_summary_usage(collab, context_case, monkeypatch):
    import bowhead_core.context_execution as module

    execution = owner(collab)
    write = module.write_context_in_tx

    async def fail_compact(*args, **kwargs):
        if kwargs.get("completion") is not None:
            raise OSError("injected_storage_failure")
        return await write(*args, **kwargs)

    monkeypatch.setattr(module, "write_context_in_tx", fail_compact)
    usage = []
    with pytest.raises(OSError, match="injected_storage_failure"):
        await prepare(execution, context_case, usage=usage)
    assert len(usage) == 1
    assert execution.view.project(context_case.request.messages) == context_case.request.messages
    restored = owner(collab)
    await restored.restore()
    assert restored.view.project(context_case.request.messages) == context_case.request.messages
    assert [call.args[0]["phase"] for call in execution.activity.await_args_list] == ["running"]


async def test_unusable_summary_trims_outgoing_input_but_restores_the_full_checkpoint(collab, context_case):
    execution = owner(collab)
    case = context_case
    messages = deepcopy(case.request.messages)
    for message in messages[:8]:
        message.content *= 5
    request = replace(case.request, messages=messages)
    native = deepcopy(messages)
    case.response.finish_reason = "length"
    usage = []
    result = await prepare(execution, case, request, usage)
    assert result.trimmed_messages > 0 and result.budget.budget.fits
    assert result.request.messages[-3:] == native[-3:]
    assert execution.view.project(messages) == native and messages == native
    assert len(case.sent) == 1 and usage == [case.response]
    assert [call.args[0]["phase"] for call in execution.activity.await_args_list] == ["running", "skipped"]
    restored = owner(collab)
    await restored.restore()
    assert restored.view.project(messages) == native
    async with (
        open_shared_ctx(collab.path) as db,
        db.execute("SELECT count(*) FROM model_context_generations WHERE turn_id=? AND kind='compact'", (collab.worker.turn_id,)) as cursor,
    ):
        assert (await cursor.fetchone())[0] == 0


async def test_revoked_original_is_rechecked_before_restored_summary_can_be_used(collab, context_case):
    execution = owner(collab)
    await prepare(execution, context_case)
    restored = owner(collab)

    async def denied(messages):
        if any(message.role == "tool" and message.tool_call_id == "c1" for message in messages):
            raise PermissionError("source_access_revoked")

    restored.validate_sources = denied
    with pytest.raises(PermissionError, match="source_access_revoked"):
        await restored.restore()
    assert restored.generation is None


async def test_accepted_cancellation_during_summary_prevents_adoption(collab, context_case, monkeypatch):
    execution = owner(collab)
    adapter = context_case.registry.get("provider")
    invoke = adapter.invoke

    async def stop_after_response(request):
        response = await invoke(request)
        async with open_shared_ctx(collab.path) as db:
            await db.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id=?", ("2026-10-29", collab.worker.turn_id))
            await db.commit()
        return response

    monkeypatch.setattr(adapter, "invoke", stop_after_response)
    usage = []
    with pytest.raises(OrdinaryContinuationUnavailable):
        await prepare(execution, context_case, usage=usage)
    assert len(usage) == 1 and len(context_case.sent) == 1
    assert execution.view.project(context_case.request.messages) == context_case.request.messages


async def test_coordination_restores_its_own_history_without_rewriting_the_accepted_business_prefix(collab, context_case):
    from bowhead_core.context_compaction import message_digest

    prefix = [Message(role="user", content="Original business request, still waiting for delegated results.")]
    key = message_digest(prefix)
    request = replace(context_case.request, messages=[*prefix, *context_case.request.messages])

    async def coordinate(view, current, current_prefix):
        return await view.prepare(
            current,
            router=context_case.router,
            agent_type="lead",
            profile="lead",
            user_pref=None,
            controller=context_case.args["controller"],
            current_request=len(current_prefix) + 8,
            canonical_count=len(current.messages),
            prefix_count=len(current_prefix),
            timeout=10,
            max_growth_tokens=2048,
            stream=False,
            record_usage=lambda response: None,
        )

    execution = owner(collab, lane="coordination", projection_key=key)
    result = await coordinate(execution, request, prefix)
    assert result.candidate is not None
    assert result.request.messages[:1] == prefix
    assert result.request.messages[1:] == execution.view.project(context_case.request.messages)
    restored = owner(collab, lane="coordination", projection_key=key)
    reused = await coordinate(restored, request, prefix)
    assert reused.request.messages == result.request.messages
    assert len(context_case.sent) == 1

    # A later suspended business batch starts another transcript projection.
    # The execution/lane retains its audit ancestry, not the previous batch's cuts.
    next_prefix = [*prefix, Message(role="assistant", content="The first delegation has now settled.")]
    later = owner(collab, lane="coordination", projection_key=message_digest(next_prefix))
    await later.restore()
    assert later.view.project(context_case.request.messages) == context_case.request.messages
    next_result = await coordinate(later, replace(request, messages=[*next_prefix, *context_case.request.messages]), next_prefix)
    assert next_result.candidate is not None
    assert next_result.request.messages[:2] == next_prefix
    ordinary = owner(collab)
    await ordinary.restore()
    assert ordinary.generation is None


async def test_fallback_recovery_measures_the_already_trimmed_input_at_its_actual_target(collab, context_case):
    from bowhead_router import AgentBinding

    case = context_case
    definition = case.registry.definition_for("provider")
    definition["models"][0]["context_policy"]["enabled"] = False
    definition["models"][0]["context_window"] = 8000
    definition["models"].append({"id": "fallback", "context_window": 32000})
    case.router.bind(AgentBinding(agent_type="lead", primary=("provider", "model"), fallback=[("provider", "fallback")]))
    case.router.set_context_windows({("provider", "model"): 8000, ("provider", "fallback"): 32000})
    execution = owner(collab)
    first = await prepare(execution, case)
    assert first.trimmed_messages > 0
    fallback = await case.router.request_context_budget("lead", first.request, target=("provider", "fallback"), reject_over_capacity=False)
    recovered = await prepare(execution, case, trigger="capacity", target=("provider", "fallback"))
    assert recovered.budget.budget.input_estimate < fallback.budget.input_estimate
    assert recovered.trimmed_messages > first.trimmed_messages
    assert execution.view.project(case.request.messages) == case.request.messages
    assert case.sent == []
