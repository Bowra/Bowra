"""In-place resume through the runner, the spawn tool and Lead (agent run limits, phase 2 §6)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents._lead_context import reset_turn_deadline, set_turn_deadline
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop, _prepare_subagent_resume
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.events import TurnEventBus
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult, SubagentRunner, register_subagent_tool
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec, build_default_registry
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse, ToolCall, ToolSchema
from test_subagent_resume_store import _capture, _db, _fernet  # noqa: F401 — the fixture seals checkpoints

from bowhead_core import agent_run_limits as rl
from bowhead_core import subagent_resume as sr

pytestmark = pytest.mark.unit

PROFILE = SubagentProfile(
    name="resume-test",
    llm_agent_type="analyst",
    tool_whitelist=("read", "subagent_return"),
    system_prompt="Read and return.",
    max_iterations=40,
    max_output_tokens=2000,
)


class _Bus(TurnEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.seen.append(event)
        await super().emit(event)

    def spawned(self) -> list[dict[str, Any]]:
        return [event.payload for event in self.seen if event.event_type == "agent_spawned"]


class _Router:
    """Reads a new key each step and returns after ``reads`` steps; answers wrap-up calls."""

    def __init__(self, reads: int) -> None:
        self.reads = reads
        self.requests: list[LLMRequest] = []

    def _next(self) -> LLMResponse:
        step = len(self.requests)
        if self.requests[-1].tool_choice == "none":
            return LLMResponse(content="Done: keys 1-2.\nRemaining: keys 3-4.")
        if step > self.reads:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id=f"r{step}", name="subagent_return", arguments={"payload": {"delivery_surface": "chat", "exec_summary": "done"}})
                ],
            )
        return LLMResponse(content="", tool_calls=[ToolCall(id=f"c{step}", name="read", arguments={"key": str(step)})])

    async def invoke(self, _agent_type: str, request: LLMRequest, **_kwargs: object) -> LLMResponse:
        self.requests.append(request)
        return self._next()

    async def stream(self, _agent_type: str, request: LLMRequest, **_kwargs: object):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        for call in self._next().tool_calls:
            yield LLMChunk(tool_call_delta=call)
        yield LLMChunk(finish_reason="tool_use")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def read(arguments: dict[str, object], _ctx: ToolExecutionContext) -> dict[str, object]:
        return {"ok": True, "value": arguments.get("key")}

    registry.register(ToolSpec(schema=ToolSchema(name="read", description="Read", input_schema={"type": "object"}), handler=read))
    register_subagent_return_tool(registry)
    return registry


def _ctx(db_path: str, turn_id: str, **extra: Any) -> ToolExecutionContext:
    store = SessionStore(db_path)
    return ToolExecutionContext(
        workspace_id="ws_1",
        member_uid="m",
        user_id="u_1",
        turn_id=turn_id,
        session_id="s_1",
        datarc=None,  # type: ignore[arg-type]
        team_id="team_1",
        session_store=store,
        **extra,
    )


async def _run(monkeypatch: pytest.MonkeyPatch, router: _Router, ctx: ToolExecutionContext, *, step_limit: int | None, **kwargs: Any):
    monkeypatch.setitem(PROFILES, PROFILE.name, PROFILE)
    limits = rl.AgentRunLimits(step_limit=step_limit, tool_call_limit=1000)
    limits_token = rl.set_run_limits(limits)
    budget_token = rl.set_tool_call_budget(rl.TurnToolCallBudget(1000))
    deadline_token = set_turn_deadline(time.monotonic() + 1800)
    bus = _Bus()
    try:
        result = await SubagentRunner(router=router, tool_registry=_registry()).run(
            profile_name=PROFILE.name, task="Read all keys", parent_turn_id=ctx.turn_id, parent_bus=bus, parent_ctx=ctx, **kwargs
        )
    finally:
        reset_turn_deadline(deadline_token)
        rl.reset_tool_call_budget(budget_token)
        rl.reset_run_limits(limits_token)
    return result, bus


def _state(db_path: str, checkpoint_id: str) -> str:
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT state FROM subagent_resume_checkpoints WHERE id=?", (checkpoint_id,)).fetchone()[0]


# ── Runner: capture and restore ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_stopped_at_its_step_limit_continues_from_its_own_transcript(tmp_path, monkeypatch) -> None:
    db_path = await _db(tmp_path)
    first, _ = await _run(monkeypatch, _Router(reads=6), _ctx(db_path, "t_1"), step_limit=3, pre_minted_subagent_id="sub_a")
    assert (first.completion, first.stop_reason) == ("partial", "step_limit")
    assert first.resume_checkpoint_id is not None

    checkpoint = await sr.claim(db_path, checkpoint_id=first.resume_checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_2")
    grant = sr.ResumeGrant(checkpoint=checkpoint, material=sr.open_material(checkpoint), instruction="Finish keys 3-4.")
    router = _Router(reads=0)
    second, bus = await _run(monkeypatch, router, _ctx(db_path, "t_2"), step_limit=3, pre_minted_subagent_id="sub_a", resume_grant=grant)

    assert second.ok and second.subagent_id == "sub_a"
    sent = router.requests[0].messages
    assert [m.content for m in sent[:2]][0] == "Read all keys"
    assert any(m.role == "tool" and m.tool_call_id == "c1" for m in sent)
    # The saved transcript ends where the run stopped; the instruction follows it.
    [at] = [i for i, m in enumerate(sent) if m.role == "user" and m.content.endswith("Finish keys 3-4.")]
    assert sent[at - 1].role == "assistant" and "Remaining: keys 3-4." in sent[at - 1].content
    assert bus.spawned()[0]["resumed_from"] == first.resume_checkpoint_id
    # The resumed run's own checkpoint supersedes the one it continued.
    assert second.resume_checkpoint_id not in (None, first.resume_checkpoint_id)
    assert _state(db_path, first.resume_checkpoint_id) == "superseded"


@pytest.mark.asyncio
async def test_changed_tools_start_fresh_from_the_saved_task_and_say_why(tmp_path, monkeypatch) -> None:
    db_path = await _db(tmp_path)
    checkpoint_id = await _capture(db_path, profile=PROFILE.name, agent_revision_id=None, tool_names=["read", "write", "subagent_return"])
    checkpoint = await sr.claim(db_path, checkpoint_id=checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_2")
    grant = sr.ResumeGrant(checkpoint=checkpoint, material=sr.open_material(checkpoint), instruction="Go on.")
    router = _Router(reads=0)
    _result, bus = await _run(monkeypatch, router, _ctx(db_path, "t_2"), step_limit=None, pre_minted_subagent_id="sub_a", resume_grant=grant)

    assert grant.refused == "tools_changed"
    first_request = router.requests[0].messages
    assert not any(m.role in ("assistant", "tool") for m in first_request)
    assert first_request[0].content.startswith("analyse sales by region")
    assert "Remaining: east and west" in first_request[0].content.replace("It reported as remaining:", "Remaining:")
    assert bus.spawned()[0]["resume_refused"] == "tools_changed"


@pytest.mark.asyncio
async def test_a_completed_run_is_saved_for_follow_ups(tmp_path, monkeypatch) -> None:
    db_path = await _db(tmp_path)
    result, _ = await _run(monkeypatch, _Router(reads=1), _ctx(db_path, "t_1"), step_limit=None)
    assert result.ok and result.resume_checkpoint_id
    [listed] = await sr.list_resumable(db_path, session_id="s_1", user_id="u_1", exclude_turn_id="t_2")
    assert (listed.completion, listed.task) == ("complete", "Read all keys")


@pytest.mark.asyncio
async def test_nothing_is_saved_when_the_feature_is_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_AGENT_RUN_LIMITS", "0")
    db_path = await _db(tmp_path)
    result, _ = await _run(monkeypatch, _Router(reads=1), _ctx(db_path, "t_1"), step_limit=None)
    assert result.ok and result.resume_checkpoint_id is None


@pytest.mark.asyncio
async def test_server_chosen_calls_run_before_the_first_model_call(tmp_path, monkeypatch) -> None:
    """The Continue path: the runner executes the given calls instead of asking the model first."""
    db_path = await _db(tmp_path)
    router = _Router(reads=0)
    forced = [ToolCall(id="call_continue_0", name="read", arguments={"key": "forced"})]
    monkeypatch.setitem(PROFILES, PROFILE.name, PROFILE)
    deadline_token = set_turn_deadline(time.monotonic() + 1800)
    try:
        await SubagentRunner(router=router, tool_registry=_registry()).run(
            profile_name=PROFILE.name,
            task="继续",
            parent_turn_id="t_2",
            parent_bus=_Bus(),
            parent_ctx=_ctx(db_path, "t_2"),
            forced_tool_calls=forced,
        )
    finally:
        reset_turn_deadline(deadline_token)
    first = router.requests[0].messages
    [at] = [i for i, m in enumerate(first) if m.tool_calls == forced]
    assert first[at + 1].role == "tool" and first[at + 1].tool_call_id == "call_continue_0" and '"forced"' in first[at + 1].content


# ── The spawn tool's resume path ────────────────────────────────────────────


@dataclass
class _CapturingRunner:
    kwargs: dict[str, Any] = field(default_factory=dict)

    async def run(self, **kwargs: Any) -> SubagentResult:
        self.kwargs = kwargs
        return SubagentResult(profile=kwargs["profile_name"], subagent_id=kwargs["pre_minted_subagent_id"], ok=True, final_answer="ok")


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


async def _spawn(db_path: str, turn_id: str, args: dict[str, Any]) -> tuple[Any, _CapturingRunner, _FakeBus]:
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner, bus = _CapturingRunner(), _FakeBus()
    ctx = _ctx(db_path, turn_id, subagent_runner=runner, parent_bus=bus)
    invocation = await registry.execute("subagent_spawn", args, ctx)
    return invocation, runner, bus


@pytest.mark.asyncio
async def test_resume_dispatches_the_saved_helper_with_its_transcript(tmp_path) -> None:
    db_path = await _db(tmp_path)
    checkpoint_id = await _capture(db_path)
    invocation, runner, bus = await _spawn(db_path, "t_2", {"resume_checkpoint_id": checkpoint_id, "profile": "kb-analyst", "task": "Finish it."})

    assert invocation.error is None, invocation.result
    grant = runner.kwargs["resume_grant"]
    assert (runner.kwargs["profile_name"], runner.kwargs["pre_minted_subagent_id"]) == ("compute", "sub_a")
    assert grant.checkpoint.id == checkpoint_id and grant.material is not None and grant.instruction == "Finish it."
    assert invocation.result["resumed_from"] == checkpoint_id
    [summary] = [e.payload for e in bus.events if e.event_type == "lead.subagent_summary"]
    assert summary["resumed_from"] == checkpoint_id
    # The run finished without saving newer work: the older partial transcript is not offered again.
    assert _state(db_path, checkpoint_id) == "superseded"


@pytest.mark.asyncio
async def test_a_failed_resume_offers_the_saved_work_again(tmp_path) -> None:
    db_path = await _db(tmp_path)
    checkpoint_id = await _capture(db_path)
    registry = build_default_registry()
    register_subagent_tool(registry)

    class _FailingRunner:
        async def run(self, **kwargs: Any) -> SubagentResult:
            return SubagentResult(
                profile=kwargs["profile_name"], subagent_id=kwargs["pre_minted_subagent_id"], ok=False, final_answer="", error="llm_error"
            )

    ctx = _ctx(db_path, "t_2", subagent_runner=_FailingRunner(), parent_bus=_FakeBus())
    await registry.execute("subagent_spawn", {"resume_checkpoint_id": checkpoint_id, "profile": "compute", "task": "Finish it."}, ctx)
    assert _state(db_path, checkpoint_id) == "live"


@pytest.mark.asyncio
async def test_a_second_stopped_helper_of_the_same_profile_still_resumes(tmp_path) -> None:
    """The same-turn partial guard is per lineage for resumes, not per profile."""
    db_path = await _db(tmp_path)
    first = await _capture(db_path)
    second = await _capture(db_path, run_subagent_id="sub_b")
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner, bus = _CapturingRunner(), _FakeBus()
    ctx = _ctx(db_path, "t_2", subagent_runner=runner, parent_bus=bus)
    ctx._run_limit_partial_profiles = {"compute"}  # type: ignore[attr-defined]
    for checkpoint_id, lineage in ((first, "sub_a"), (second, "sub_b")):
        invocation = await registry.execute("subagent_spawn", {"resume_checkpoint_id": checkpoint_id, "profile": "", "task": "go"}, ctx)
        assert invocation.result.get("error_kind") != "run_limit_partial"
        assert runner.kwargs["pre_minted_subagent_id"] == lineage


@pytest.mark.asyncio
async def test_resume_in_the_capturing_turn_is_refused_without_dispatch(tmp_path) -> None:
    db_path = await _db(tmp_path)
    checkpoint_id = await _capture(db_path)
    invocation, runner, _ = await _spawn(db_path, "t_1", {"resume_checkpoint_id": checkpoint_id, "profile": "compute", "task": "again"})
    assert invocation.result["resume_refused"] == "same_turn"
    assert runner.kwargs == {}


@pytest.mark.asyncio
async def test_expired_saved_work_hands_the_task_to_a_fresh_helper(tmp_path) -> None:
    db_path = await _db(tmp_path)
    checkpoint_id = await _capture(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE subagent_resume_checkpoints SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (checkpoint_id,))
    invocation, runner, _ = await _spawn(db_path, "t_2", {"resume_checkpoint_id": checkpoint_id, "profile": "compute", "task": "Finish it."})
    grant = runner.kwargs["resume_grant"]
    assert (grant.material, grant.refused) == (None, "expired")
    assert invocation.result["resume_refused"] == "expired"


# ── Lead: the Continue click ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_continue_click_becomes_server_chosen_resume_calls(tmp_path) -> None:
    db_path = await _db(tmp_path)
    clicked = await _capture(db_path)
    other = await _capture(db_path, run_subagent_id="sub_b", completion="complete", stop_reason=None, partial_done=None, partial_remaining=None)
    await sr.record_requests(db_path, turn_id="t_2", checkpoint_ids=[clicked])
    blocks: list[str] = []
    runtime = SimpleNamespace(parent_ctx=SimpleNamespace(session_store=SessionStore(db_path), session_id="s_1", user_id="u_1"))

    calls = await _prepare_subagent_resume(runtime, "t_2", lambda block, **_: blocks.append(block))

    assert [(c.name, c.arguments["resume_checkpoint_id"]) for c in calls] == [("subagent_spawn", clicked)]
    assert any(other in block for block in blocks) and not any(f"checkpoint_id={clicked}" in block for block in blocks)
    assert any("<continue_request>" in block for block in blocks)


@dataclass
class _LeadRunner:
    kwargs: dict[str, Any] = field(default_factory=dict)

    async def run(self, *, profile_name: str, **kwargs: Any) -> SubagentResult:
        self.kwargs = kwargs
        return SubagentResult(profile=profile_name, subagent_id="sub_lead", ok=True, final_answer="ok")


class _EmptyCatalog:
    def list_names(self):
        return []

    def list_specs(self):
        return []

    def get(self, name):
        return None


@pytest.mark.asyncio
async def test_the_lead_loop_passes_the_click_to_its_runner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    monkeypatch.setitem(
        PROFILES,
        "lead",
        SubagentProfile(name="lead", llm_agent_type="lead", tool_whitelist=("subagent_return",), system_prompt="(stub)", max_iterations=4),
    )
    db_path = await _db(tmp_path)
    clicked = await _capture(db_path)
    await sr.record_requests(db_path, turn_id="t_2", checkpoint_ids=[clicked])
    runner = _LeadRunner()
    ctx = SimpleNamespace(
        clarification_store=None,
        _m137_subagent_payloads=[],
        tool_view=None,
        user_id="u_1",
        team_id="team_1",
        session_id="s_1",
        session_store=SessionStore(db_path),
        skill_instance_store=None,
        team_skill_store=None,
        granted_team_skill_ids=None,
        skill_catalog=_EmptyCatalog(),
        execution_generation=1,
        execution_owner_id=None,
    )
    runtime = LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=_FakeBus(),
        parent_ctx=ctx,
        subagent_runner=runner,
    )
    sctx = SerializableTurnContext(user_id="u_1", workspace_id="ws_1", session_id="s_1", turn_id="t_2", role="user")

    await _lead_loop(sctx, "继续", runtime)

    forced = runner.kwargs["forced_tool_calls"]
    assert [call.arguments["resume_checkpoint_id"] for call in forced] == [clicked]
