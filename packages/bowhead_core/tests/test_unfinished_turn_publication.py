"""A Lead Turn that fails, or is stopped, inside a Tool batch publishes its native transcript.

The scenario is an interrupted task: the Lead read the schema, then delegated and searched in
parallel. The search finished, the helper was still running, and the layer waiting on the
batch ended the run.
"""

import asyncio
from copy import deepcopy

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_collaboration_runtime import CollaborationCall, CollaborationRuntime
from bowhead_core.context_assembly import incomplete_turn_marker
from bowhead_core.context_history import ConversationContext, load_conversation_context
from bowhead_core.context_ownership import StoppedTurnOwner, require_context_owner_in_tx
from bowhead_core.context_publication import ContextPublication
from bowhead_core.events import TurnEventBus
from bowhead_core.ordinary_lead_continuation import OrdinaryWorkerUnavailable
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_core.types import ToolInvocation
from bowhead_core.unfinished_turn_context import open_delegations
from bowhead_router.types import LLMResponse, TokenUsage, ToolCall
from cryptography.fernet import Fernet
from test_agent_collaboration import collab as collaboration_fixture
from test_context_history import next_context
from test_context_maintenance import context_case as maintenance_fixture

from bowhead_router import AgentBinding

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration

DOWNLOAD = "BIQueryFailedError: download_data failed (errcode=0 type=unknown): 下载数据失败, 请检查 query 日志."
SEARCH_RESULT = '{"ok": true, "rows": 3}'


def helper_result(status: str) -> str:
    return f"{status}｜子工具 4 次｜没有回执（被中止前是否已写入画布未知，以画布现状为准）｜Helper tool failures: bi_query ×3: {DOWNLOAD}"


class LeadRun:
    """One Lead run that reaches an open batch: a running helper and a finished search."""

    def __init__(self, collab, case, monkeypatch, *, fail: bool):
        self.collab, self.worker = collab, collab.worker
        case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = False
        case.router.bind(AgentBinding(agent_type="analyst", primary=("provider", "model")))
        adapter = case.registry.get("provider")
        self.task = "Inspect the workspace and write an HTML analysis. " * 15
        replies = [
            LLMResponse(
                content="Reading the schema first.",
                tool_calls=[ToolCall(id="c1", name="bi_schema", arguments={})],
                finish_reason="tool_calls",
                usage=TokenUsage(input_tokens=10, output_tokens=5, cached_tokens=0),
            ),
            LLMResponse(
                content="Delegating the data work and searching in parallel.",
                tool_calls=[
                    ToolCall(id="c2", name="subagent_spawn", arguments={"profile": "compute", "task": self.task}),
                    ToolCall(id="c3", name="web_search", arguments={"query": "hcp"}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(input_tokens=10, output_tokens=5, cached_tokens=0),
            ),
        ]
        self.sent = []

        async def invoke(request):
            adapter._build_body(request, stream=False)
            self.sent.append(deepcopy(request))
            return replies[len(self.sent) - 1]

        monkeypatch.setattr(adapter, "invoke", invoke)
        self.bus = TurnEventBus(log_sink=OrderedTurnLogSink(TurnLogStore(collab.path)))
        self.runner = SubagentRunner(router=case.router, tool_registry=ToolRegistry(), backend="legacy")
        bus = self.bus

        async def execute(tc, ctx, bus_, turn_id, subagent_id, **kwargs):
            if tc.id == "c2":
                # The dispatch records the helper as open; it runs four Tools, three of which fail.
                record = open_delegations(bus)
                record.open("c2", profile="compute", subagent_id="sub_helper")
                record.observe("sub_helper", "bi_schema", None)
                for _ in range(3):
                    record.observe("sub_helper", "bi_query", [DOWNLOAD])
                await asyncio.sleep(3600)
            return ToolInvocation(tool_name=tc.name, arguments=tc.arguments, call_id=tc.id, result={"ok": True, "rows": 3})

        monkeypatch.setattr(self.runner, "_execute_tool", execute)
        import bowhead_core.subagent as subagent_module

        names = subagent_module.effective_tool_names
        monkeypatch.setattr(subagent_module, "effective_tool_names", lambda *args, **kwargs: {*names(*args, **kwargs), "bi_schema", "web_search"})

        async def while_suspended(self_, work, *, execution_id, coordinate, user_input=None):
            batch = asyncio.ensure_future(work)
            try:
                await asyncio.sleep(0.05)
                if fail:
                    raise RuntimeError("context_lane_boundary_invalid")
                await asyncio.Event().wait()  # the Lead keeps waiting until the user stops the Turn
            finally:
                batch.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await batch

        monkeypatch.setattr(CollaborationRuntime, "while_suspended", while_suspended)
        worker = self.worker
        self.store = SessionStore(collab.path)
        self.ctx = ToolExecutionContext(
            workspace_id=worker.workspace_id,
            member_uid=worker.user_id,
            user_id=worker.user_id,
            turn_id=worker.turn_id,
            datarc=None,
            team_id=worker.team_id,
            session_id=worker.session_id,
            execution_owner_id=worker.owner_id,
            execution_generation=worker.generation,
            session_store=self.store,
            role="lead",
            subagent_runner=self.runner,
            parent_bus=self.bus,
            collaboration_runtime=CollaborationRuntime(collab.store, worker, authorize=collab.authorize),
            collaboration_call=CollaborationCall(collab.identity(collab.lead, step="spawn"), None),
        )
        self.history = deepcopy(case.request.messages[:8])
        self.ctx._model_context_history = ConversationContext(None, 0, self.history, 0)
        # Production settles the root Lead execution before the Turn's terminal write.
        self.ctx._model_context_lead_binding = collab.executions[collab.lead]

    def start(self) -> asyncio.Future:
        return asyncio.ensure_future(
            self.runner.run(
                profile_name="lead",
                task="Look at the data and write an analysis.",
                parent_turn_id=self.worker.turn_id,
                parent_ctx=self.ctx,
                parent_bus=self.bus,
                exact_binding=self.collab.executions[self.collab.lead],
                history=self.history,
            )
        )

    async def fail(self) -> None:
        with pytest.raises(RuntimeError, match="context_lane_boundary_invalid"):
            await self.start()
        assert len(self.sent) == 2

    async def stop(self) -> None:
        """The Stop request commits the cancelled status itself, then the worker is cancelled."""
        run = self.start()
        await asyncio.sleep(0.3)
        worker = self.worker
        assert (
            await self.store.request_cancel_turn(
                team_id=worker.team_id,
                user_id=worker.user_id,
                cancel_request_id="cancel_1",
                workspace_id=worker.workspace_id,
                turn_id=worker.turn_id,
                expected_execution_generation=worker.generation,
            )
            == "cancelled"
        )
        run.cancel("durable_user_cancel")
        with pytest.raises(asyncio.CancelledError):
            await run

    async def publication(self, status: str, *, owner=None) -> ContextPublication:
        publication = await self.bus._unfinished_context_publication(status, owner=owner)
        assert isinstance(publication, ContextPublication)
        return publication

    async def next_turn_history(self, following=None):
        return await load_conversation_context(following or await next_context(self.collab))

    def assert_native(self, restored, *, status: str, marker: str) -> None:
        assert restored.generation_id and restored.through_seq == 1
        assert restored.messages[:8] == self.history
        tail = restored.messages[8:]
        assert [message.role for message in tail] == ["user", "assistant", "tool", "assistant", "tool", "tool", "assistant"]
        # The Lead's own call and its result.
        assert tail[1].tool_calls[0].id == "c1" and tail[2].content == SEARCH_RESULT
        # The delegation keeps the full task; the helper that never returned reports its failures.
        spawn = tail[3].tool_calls[0]
        assert spawn.name == "subagent_spawn" and spawn.arguments["task"] == self.task
        assert (tail[4].tool_call_id, tail[4].content) == ("c2", helper_result(status))
        # The search finished before the ending and keeps its real result.
        assert (tail[5].tool_call_id, tail[5].content) == ("c3", SEARCH_RESULT)
        assert tail[6].content == marker
        assert restored.incomplete_turns == 1


async def lead_run(collab, context_case, monkeypatch, *, fail: bool) -> LeadRun:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    return LeadRun(collab, context_case, monkeypatch, fail=fail)


@pytest.mark.parametrize("status", ["failed", "aborted"])
async def test_a_turn_the_worker_ends_publishes_its_repaired_transcript(collab, context_case, monkeypatch, status):
    run = await lead_run(collab, context_case, monkeypatch, fail=True)
    await run.fail()
    publication = await run.publication(status)
    if status == "failed":
        assert await run.store.mark_failed(
            turn_id=run.worker.turn_id,
            worker=run.worker,
            error="lead_internal_error",
            error_kind="lead_internal_error",
            context_publication=publication,
        )
        marker = incomplete_turn_marker("failed", "lead_internal_error")
    else:
        assert await run.store.mark_aborted(turn_id=run.worker.turn_id, worker=run.worker, context_publication=publication)
        marker = incomplete_turn_marker("aborted")
    assert (await run.store.get_turn(run.worker.turn_id)).status == status
    await run.store.finalize_turn(session_id=run.worker.session_id, current_turn_id=run.worker.turn_id, next_turn_id=None)
    run.assert_native(await run.next_turn_history(), status=status, marker=marker)


async def test_a_publication_that_cannot_be_stored_never_blocks_the_failed_status(collab, context_case, monkeypatch):
    run = await lead_run(collab, context_case, monkeypatch, fail=True)
    await run.fail()
    publication = await run.publication("failed")

    async def refuse(self, final_answer):
        raise RuntimeError("knowledge_source_changed")

    monkeypatch.setattr(ContextPublication, "verify_authority", refuse)
    assert await run.store.mark_failed(
        turn_id=run.worker.turn_id, worker=run.worker, error="lead_internal_error", error_kind="lead_internal_error", context_publication=publication
    )
    assert (await run.store.get_turn(run.worker.turn_id)).status == "failed"
    await run.store.finalize_turn(session_id=run.worker.session_id, current_turn_id=run.worker.turn_id, next_turn_id=None)
    restored = await run.next_turn_history()
    # Rebuilt from events: the request and the marker.
    assert restored.generation_id is None
    assert [message.role for message in restored.messages] == ["user", "assistant"]
    assert restored.messages[-1].content == incomplete_turn_marker("failed", "lead_internal_error")


async def test_a_stopped_turn_is_published_by_the_stopped_owner_only(collab, context_case, monkeypatch):
    run = await lead_run(collab, context_case, monkeypatch, fail=False)
    await run.stop()
    turn_id = run.worker.turn_id
    assert (await run.store.get_turn(turn_id)).status == "cancelled"
    # The live worker lost the Turn to the Stop request: its write is refused and publishes nothing.
    live = await run.publication("cancelled")
    assert not await run.store.mark_cancelled(turn_id=turn_id, worker=run.worker, error_kind="user_cancel", context_publication=live)
    assert not await run.store.publish_stopped_turn_context(turn_id=turn_id, context_publication=live)
    async with (
        open_shared_ctx(collab.path) as db,
        db.execute("SELECT COUNT(*) FROM model_context_conversation_heads WHERE session_id=?", (run.worker.session_id,)) as cursor,
    ):
        assert (await cursor.fetchone())[0] == 0
    stopped = StoppedTurnOwner(**run.worker.model_dump())
    publication = await run.publication("cancelled", owner=stopped)
    assert not await run.store.publish_stopped_turn_context(turn_id="t_other", context_publication=publication)
    assert await run.store.publish_stopped_turn_context(turn_id=turn_id, context_publication=publication)
    run.assert_native(await run.next_turn_history(), status="cancelled", marker=incomplete_turn_marker("cancelled"))


async def test_a_stopped_turn_with_a_successor_is_not_published(collab, context_case, monkeypatch):
    run = await lead_run(collab, context_case, monkeypatch, fail=False)
    await run.stop()
    following = await next_context(collab)
    publication = await run.publication("cancelled", owner=StoppedTurnOwner(**run.worker.model_dump()))
    assert not await run.store.publish_stopped_turn_context(turn_id=run.worker.turn_id, context_publication=publication)
    restored = await run.next_turn_history(following)
    assert restored.generation_id is None
    assert [message.role for message in restored.messages] == ["user", "assistant"]
    assert restored.messages[-1].content == incomplete_turn_marker("cancelled")


async def test_the_stopped_owner_is_admitted_only_for_the_turn_the_user_stopped(collab, context_case, monkeypatch):
    run = await lead_run(collab, context_case, monkeypatch, fail=False)
    stopped = StoppedTurnOwner(**run.worker.model_dump())

    async def admitted(owner) -> bool:
        async with open_shared_ctx(collab.path) as db:
            await db.execute("BEGIN")
            try:
                await require_context_owner_in_tx(db, worker=owner)
                return True
            except OrdinaryWorkerUnavailable:
                return False
            finally:
                await db.rollback()

    # A running Turn belongs to its live worker.
    assert await admitted(run.worker) and not await admitted(stopped)
    await run.stop()
    assert await admitted(stopped) and not await admitted(run.worker)
    # Another worker, another execution generation and another user are refused.
    assert not await admitted(stopped.model_copy(update={"owner_id": "another-worker"}))
    assert not await admitted(stopped.model_copy(update={"generation": stopped.generation + 1}))
    assert not await admitted(stopped.model_copy(update={"user_id": "usr_other"}))
    # A Turn that ended cancelled for another reason is not a Turn the user stopped.
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE turns SET error_kind='aborted' WHERE id=?", (run.worker.turn_id,))
        await db.commit()
    assert not await admitted(stopped)
