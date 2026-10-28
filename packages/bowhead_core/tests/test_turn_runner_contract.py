"""TurnRunner contract tests — Sprint θ Phase 8c-B adapted.

This file is the **living spec** of TurnRunner's external contract. After Phase
8c-B-1 routes all turns through `lead_orchestrator.run_turn`, the contract is:

    TurnRunner.run(req) →
        emit turn_started
        delegate to lead_orchestrator.run_turn (which delegates to SubagentRunner)
        emit done with result.status / duration
        return TurnResult

Tests adapt by mocking SubagentRunner via the `fake_subagent_runner` fixture
defined in `conftest.py`. The Lead orchestrator picks up the mock via
`runtime.subagent_runner` (sourced from `orch.subagent_runner`).

Coverage:
1. Top-level event sequence (turn_started → done)
2. SubagentRunner-emitted events (tool_call, tool_result) propagate to parent_bus
3. Concurrent turn isolation (no cross-talk on shared orch)
4. Metrics accumulation across turns (lead_metrics outcome counter)
5. Back-compat re-exports (orchestrator module still exports legacy helpers)
6. State hygiene (no leaks onto orch instance)
7. Done event payload schema (turn_id / status / duration_ms / schema_version)

What was deleted (per phase-8c-b-test-adapt-strategy.md): tests for hooks,
approval queue, mid-turn session budget, thinking event, tool error recovery,
and aggressive compaction — all moved to SubagentRunner; covered by SubagentRunner
own tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.types import (
    ToolInvocation,
    TurnRequest,
    TurnStatus,
)


# ─────────────────────────────────────────────────────────────
# Shared fakes
# ─────────────────────────────────────────────────────────────
class FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._persist_before_live = True

    async def emit(self, event: Any) -> bool:
        self.events.append(event)
        return True

    @property
    def types(self) -> list[str]:
        return [e.event_type for e in self.events]


class FakeEventRegistry:
    def __init__(self) -> None:
        self.buses: dict[str, FakeBus] = {}

    async def get_or_create(self, turn_id: str) -> FakeBus:
        if turn_id not in self.buses:
            self.buses[turn_id] = FakeBus()
        return self.buses[turn_id]

    async def projection_checkpoint(self, turn_id: str):
        return await self.get_or_create(turn_id), 0


class FakeConfig:
    llm_agent_type = "lead"
    max_output_tokens = 1000


class FakeToolRegistry:
    def __init__(self, spawn_available: bool = True) -> None:
        self.spawn_available = spawn_available
        self.specs: dict[str, Any] = {}

    def to_schemas(self) -> list[Any]:
        return []

    def has(self, name: str) -> bool:
        return name == "subagent_spawn" and self.spawn_available

    def get(self, name: str) -> Any:
        return self.specs.get(name, SimpleNamespace(needs_approval=False, risk_level="low"))


def _make_fake_orch(
    *,
    config: FakeConfig | None = None,
    session_tokens_total: int = 0,
    canvas_store: Any = None,
    tool_registry: FakeToolRegistry | None = None,
    subagent_runner: Any = None,
) -> SimpleNamespace:
    """Build a minimal orch-like object for TurnRunner consumption."""
    from bowhead_core.events import OrchestratorEvent

    def _event(turn_id: str, event_type: str, payload: dict[str, Any]) -> OrchestratorEvent:
        return OrchestratorEvent(
            event_type=event_type,
            turn_id=turn_id,
            payload=payload,  # type: ignore[arg-type]
        )

    async def _ensure_session(**kw: Any) -> None:
        pass

    async def _record_turn(**kw: Any) -> Any:
        return SimpleNamespace(seq=1)

    async def _require_legacy_chat_execution(**kw: Any) -> None:
        # This storage fake contains only legacy text sources.
        pass

    async def _record_turn_attachments(**kw: Any) -> None:
        pass

    async def _pin_session_attachments(**kw: Any) -> None:
        pass

    async def _list_recent_digests(**kw: Any) -> list[dict]:
        return []

    async def _list_pinned_attachments(**kw: Any) -> list[str]:
        return []

    async def _complete_turn(**kw: Any) -> None:
        pass

    # 2026-08-04 (after 33a68fad1): done persistence uses SessionStore.mark_completed /
    # mark_failed (turn_runner._persist_turn_state). Missing fake methods make
    # _persist_done_state raise AttributeError → normal done fails → fallback,
    # final event no longer done. **True = state transition succeeded**, matching real store;
    # None becomes false via bool(), swapping false green for another false red.
    async def _mark_completed(**kw: Any) -> bool:
        return True

    async def _mark_failed(**kw: Any) -> bool:
        return True

    async def _touch_session(**kw: Any) -> None:
        pass

    async def _save_digest(**kw: Any) -> None:
        pass

    async def _get_session(sid: str) -> Any:
        return SimpleNamespace(tokens_total=session_tokens_total, title="t")

    session_store = SimpleNamespace(
        require_legacy_chat_execution=_require_legacy_chat_execution,
        ensure_session=_ensure_session,
        record_turn=_record_turn,
        record_turn_attachments=_record_turn_attachments,
        pin_session_attachments=_pin_session_attachments,
        list_recent_digests=_list_recent_digests,
        list_pinned_attachments=_list_pinned_attachments,
        complete_turn=_complete_turn,
        mark_completed=_mark_completed,
        mark_failed=_mark_failed,
        touch_session=_touch_session,
        save_digest=_save_digest,
        get_session=_get_session,
    )

    orch = SimpleNamespace(
        event_registry=FakeEventRegistry(),
        session_store=session_store,
        canvas_store=canvas_store,
        user_memory_store=None,
        artifact_store=None,
        custom_subagent_store=None,
        datarc=SimpleNamespace(),
        datarc_client=None,
        router=None,
        tool_registry=tool_registry or FakeToolRegistry(),
        config=config or FakeConfig(),
        vector_client=None,
        skill_instance_store=None,
        skill_catalog=None,
        approval_store=None,
        dreaming_light=None,
        subagent_runner=subagent_runner,
        fact_store=None,
        schema_cache=None,
        clarification_store=None,
        _event=_event,
    )
    return orch


def _req(text: str = "hi", turn_id: str | None = None) -> TurnRequest:
    return TurnRequest(
        turn_id=turn_id or f"t_{abs(hash(text)) & 0xFFFF:04x}",
        session_id="s_test",
        workspace_id="1",
        member_uid="m1",
        user_id="u1",
        text=text,
        attachments=[],
    )


# ─────────────────────────────────────────────────────────────
# 1. Top-level event sequence
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_event_sequence_happy_path(fake_subagent_runner, make_subagent_result) -> None:
    """Top-level: turn_started → done. Lead orchestrator emits done internally
    (status=succeeded mapped via SubagentResult), then turn_runner wraps with
    a final done. SSE bus closes cleanly."""
    from bowhead_core.turn_runner import TurnRunner

    fake_subagent_runner.results = [
        make_subagent_result(final_answer="hello", tokens_input=10, tokens_output=5),
    ]
    orch = _make_fake_orch(
        subagent_runner=fake_subagent_runner,
    )
    req = _req()
    await TurnRunner(orch).run(req)
    types = orch.event_registry.buses[req.turn_id].types
    assert types[0] == "turn_started"
    assert types[-1] == "done"


@pytest.mark.asyncio
async def test_selected_editor_is_projected_in_the_original_started_event(fake_subagent_runner, make_subagent_result) -> None:
    from bowhead_core.coach_canvas_target import AdmittedCoachCanvasTarget, CoachCanvasSendTarget
    from bowhead_core.turn_runner import TurnRunner

    fake_subagent_runner.results = [make_subagent_result(final_answer="Selected resource inspected")]
    orch = _make_fake_orch(subagent_runner=fake_subagent_runner)
    req = _req()
    target = CoachCanvasSendTarget.model_validate(
        {
            "schema_version": 1,
            "user_id": req.user_id,
            "team_id": "team_original",
            "workspace_id": req.workspace_id,
            "session_id": req.session_id,
            "target": {"kind": "skill", "source_kind": "user", "id": "sk_original", "revision": "sha256:" + "a" * 64},
            "dirty": False,
            "pending_write": False,
        }
    )

    async def recheck() -> dict:
        return {"private_resource_contents": "not an event payload"}

    req.coach_canvas_target = AdmittedCoachCanvasTarget(target, recheck)
    await TurnRunner(orch).run(req)
    started = orch.event_registry.buses[req.turn_id].events[0]
    assert started.event_type == "turn_started"
    assert started.payload["coach_canvas_target"] == target.model_dump()
    assert "private_resource_contents" not in str(started.payload)


@pytest.mark.asyncio
async def test_event_sequence_tool_roundtrip(fake_subagent_runner, make_subagent_result) -> None:
    """SubagentRunner-emitted events (tool_call, tool_result) propagate up via
    parent_bus. Tests verify lifecycle visibility regardless of which layer
    emits them."""
    from bowhead_core.turn_runner import TurnRunner

    # FakeSubagentRunner emits tool_call + tool_result on parent_bus before
    # returning final result — mirrors real SubagentRunner lifecycle.
    fake_subagent_runner.emit_events = [
        ("tool_call", {"tool_call_id": "tc_1", "name": "bi_ask_question", "arguments": {"text": "q"}}),
        ("tool_result", {"tool_call_id": "tc_1", "name": "bi_ask_question", "ok": True}),
    ]
    fake_subagent_runner.results = [
        make_subagent_result(
            final_answer="final",
            tool_invocations=[
                ToolInvocation(
                    tool_name="bi_ask_question",
                    arguments={"text": "q"},
                    result={"ok": True},
                    error=None,
                    started_at="",
                    ended_at="",
                    duration_ms=12,
                ),
            ],
            tokens_input=15,
            tokens_output=8,
        ),
    ]
    orch = _make_fake_orch(
        subagent_runner=fake_subagent_runner,
    )
    req = _req()
    result = await TurnRunner(orch).run(req)
    assert result.final_answer == "final"
    types = orch.event_registry.buses[req.turn_id].types
    assert "tool_call" in types
    assert "tool_result" in types
    assert types.index("tool_call") < types.index("tool_result")


# ─────────────────────────────────────────────────────────────
# 2. Concurrent isolation
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_two_concurrent_turns_do_not_share_state(
    fake_subagent_runner,
    make_subagent_result,
) -> None:
    """Two turns on same orch: independent buses, no event cross-talk."""
    import asyncio

    from bowhead_core.turn_runner import TurnRunner

    fake_subagent_runner.results = [make_subagent_result(final_answer=f"ans_{i}") for i in range(10)]
    orch = _make_fake_orch(
        subagent_runner=fake_subagent_runner,
    )
    req_a = _req("q_a", turn_id="t_aaaa")
    req_b = _req("q_b", turn_id="t_bbbb")
    runner = TurnRunner(orch)
    res_a, res_b = await asyncio.gather(runner.run(req_a), runner.run(req_b))
    assert res_a.turn_id == "t_aaaa"
    assert res_b.turn_id == "t_bbbb"
    assert "t_aaaa" in orch.event_registry.buses
    assert "t_bbbb" in orch.event_registry.buses
    events_a = orch.event_registry.buses["t_aaaa"].events
    events_b = orch.event_registry.buses["t_bbbb"].events
    for e in events_a:
        assert e.turn_id == "t_aaaa"
    for e in events_b:
        assert e.turn_id == "t_bbbb"


# ─────────────────────────────────────────────────────────────
# 3. Metrics accumulation
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_metrics_accumulate_across_multiple_turns(
    fake_subagent_runner,
    make_subagent_result,
) -> None:
    """3 turns → lead_metrics outcome counter reflects each completion."""
    from bowhead_core.turn_runner import TurnRunner

    from bowhead_core import metrics as _m

    _m.get_registry().reset()
    fake_subagent_runner.results = [make_subagent_result(final_answer=f"a{i}") for i in range(3)]
    orch = _make_fake_orch(
        subagent_runner=fake_subagent_runner,
    )
    runner = TurnRunner(orch)
    for i in range(3):
        await runner.run(_req(f"q_{i}", turn_id=f"t_{i}"))

    out = _m.get_registry().render_prometheus()
    # lead_metrics counter increments once per succeeded turn
    assert "bowhead_lead_orchestrator_outcome_total" in out
    # Just verify the line exists with succeeded outcome — don't lock to exact
    # count format (renderer may format as "3" or "3.0" depending on type)
    assert 'outcome="succeeded"' in out
    _m.get_registry().reset()


# ─────────────────────────────────────────────────────────────
# 4. Back-compat re-exports (legacy import paths)
# ─────────────────────────────────────────────────────────────
def test_orchestrator_module_still_exports_legacy_helpers() -> None:
    """契约终态（2026-07-14）：helpers 转公开命名、正式家=turn_runner；orchestrator
    只保留真正属于它的符号。re-export 兼容期结束（全仓无旧私名调用点，grep 证）。"""
    from bowhead_core.orchestrator import (
        Orchestrator,
        OrchestratorConfig,
    )
    from bowhead_core.turn_runner import (
        abbrev_thinking,
        build_cap_exceeded_summary,
        compress_old_messages,
        data_attachment_ids,
        flatten_for_summary,
        log_thinking,
        result_meta,
        safe_preview,
        subagent_backend_from_env,
        user_explicitly_wants_bi,
    )

    for fn in (
        abbrev_thinking,
        build_cap_exceeded_summary,
        compress_old_messages,
        data_attachment_ids,
        flatten_for_summary,
        log_thinking,
        result_meta,
        safe_preview,
        subagent_backend_from_env,
        user_explicitly_wants_bi,
    ):
        assert callable(fn)
    assert Orchestrator is not None
    assert OrchestratorConfig is not None


def test_turn_runner_module_exports_turnrunner_class() -> None:
    from bowhead_core.turn_runner import TurnRunner

    assert TurnRunner is not None


def test_gateway_import_path_unbroken() -> None:
    from bowhead_core import Orchestrator, TurnRequest, get_event_registry

    assert Orchestrator is not None
    assert TurnRequest is not None
    assert callable(get_event_registry)


# ─────────────────────────────────────────────────────────────
# 5. State hygiene — no leaks onto orch
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_real_orchestrator_new_instance_runs_without_attrleak(
    fake_subagent_runner,
) -> None:
    """Real Orchestrator (via __new__) running through Lead path: no attr leak."""
    from bowhead_core.orchestrator import Orchestrator
    from bowhead_core.turn_runner import TurnRunner

    orch_real = Orchestrator.__new__(Orchestrator)
    fake_orch = _make_fake_orch(
        subagent_runner=fake_subagent_runner,
    )
    for attr in (
        "event_registry",
        "session_store",
        "canvas_store",
        "user_memory_store",
        "artifact_store",
        "custom_subagent_store",
        "datarc",
        "datarc_client",
        "router",
        "tool_registry",
        "config",
        "vector_client",
        "skill_instance_store",
        "skill_catalog",
        "approval_store",
        "dreaming_light",
        "subagent_runner",
        "fact_store",
        "schema_cache",
        "clarification_store",
    ):
        setattr(orch_real, attr, getattr(fake_orch, attr))

    pre_attrs = set(dir(orch_real))
    result = await TurnRunner(orch_real).run(_req())
    assert result.status == TurnStatus.COMPLETED
    post_attrs = set(dir(orch_real))
    new_attrs = post_attrs - pre_attrs
    # historical leak — must NOT come back
    assert "_session_start_tokens" not in new_attrs


# ─────────────────────────────────────────────────────────────
# 6. Done event payload schema
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_done_event_has_expected_fields(fake_subagent_runner, make_subagent_result) -> None:
    """`done` event payload contains turn_id / status / duration_ms / schema_version.

    Detailed token accounting (tokens_input/output) is now SubagentRunner's
    concern; turn_runner's done is a top-level lifecycle marker.
    """
    from bowhead_core.turn_runner import TurnRunner

    fake_subagent_runner.results = [
        make_subagent_result(final_answer="ok", tokens_input=42, tokens_output=17),
    ]
    orch = _make_fake_orch(
        subagent_runner=fake_subagent_runner,
    )
    req = _req()
    await TurnRunner(orch).run(req)
    bus = orch.event_registry.buses[req.turn_id]
    done_events = [e for e in bus.events if e.event_type == "done"]
    assert len(done_events) >= 1
    p = done_events[-1].payload
    assert p["turn_id"] == req.turn_id
    assert p["status"] == TurnStatus.COMPLETED.value
    assert isinstance(p["duration_ms"], int)
    assert p.get("schema_version") == "0.9.0"


# ─────────────────────────────────────────────────────────────
# 8. canvas-summon: Lead prose **no longer** autosplits into canvas sections
#    (full TurnRunner.run, not direct predicate call).
# ─────────────────────────────────────────────────────────────
class _NoCanvasStore:
    """会话还没有 canvas 行 —— 纯聊天轮的真实形状。"""

    async def get_canvas_by_session(self, session_id: str) -> Any:
        return None


_LEAD_PROSE = "# 上周销售\n\n整体环比回落，主要来自华北。\n"


async def _run_and_collect(orch, req) -> list[str]:
    from bowhead_core.turn_runner import TurnRunner

    await TurnRunner(orch).run(req)
    return orch.event_registry.buses[req.turn_id].types


@pytest.mark.asyncio
async def test_lead_prose_never_reaches_canvas(fake_subagent_runner, monkeypatch) -> None:
    """★ 默认行为：Lead 的终答散文**一个 canvas 事件都不发**，而叙事流照旧。

    一条用例同时钉住两件事，因为它们是同一次改动的两半，分开写就会漏掉后半：
      · 砍干净了 —— 前端拿不到 sec_lead_*，done 时无从 reveal，结构上没有可闪的东西；
      · **没砍到叙事流** —— 第一版实现复用了 `streaming_enabled`（它同时管着 stream_cb），
        聊天气泡的逐字流式直接消失（真机 88 → 0 delta），而当时的单测只断言了
        "没有 canvas 事件"，一路绿灯。
    """
    monkeypatch.delenv("BOWHEAD_LEAD_CANVAS_AUTOCHOP", raising=False)
    fake_subagent_runner.stream_text = _LEAD_PROSE
    orch = _make_fake_orch(subagent_runner=fake_subagent_runner, canvas_store=_NoCanvasStore())
    types = await _run_and_collect(orch, _req("autochop-off"))

    # Assert **section events**, not all canvas.*: canvas.done(section_count=0) emits every turn,
    # unrelated to autosplit (lead_orchestrator closing fallback); frontend hasCanvas only counts
    # visible sections, so harmless. Asserting it absent pins unrelated behavior here.
    section_events = [t for t in types if t.startswith("canvas.section")]
    assert section_events == [], f"Lead 散文仍在进 canvas：{section_events}"
    assert "narrative.delta" in types, f"砍到了聊天气泡的流式：{types}"


@pytest.mark.asyncio
async def test_rollback_lever_restores_autochop(fake_subagent_runner, monkeypatch) -> None:
    """回滚杠杆得**真的**能回滚 —— 否则它只是个心理安慰。

    `BOWHEAD_LEAD_CANVAS_AUTOCHOP=1` → 旧行为原样回来（Lead 散文照常切段流出）。
    这条用例随杠杆一起删除（下个版本连同 canvas_composers/streaming.py）。
    """
    monkeypatch.setenv("BOWHEAD_LEAD_CANVAS_AUTOCHOP", "1")
    fake_subagent_runner.stream_text = _LEAD_PROSE
    orch = _make_fake_orch(subagent_runner=fake_subagent_runner, canvas_store=_NoCanvasStore())
    types = await _run_and_collect(orch, _req("autochop-on"))
    assert "canvas.section_streaming" in types, f"杠杆没生效：{types}"
    assert "narrative.delta" in types, types


# ─────────────────────────────────────────────────────────────
# DELETED tests (per phase-8c-b-test-adapt-strategy.md):
#
# - test_tool_error_does_not_abort_loop: SubagentRunner's loop has its own
#   error-recovery; covered by SubagentRunner own tests.
# - test_force_spawn_*: turn_runner force_spawn shortcut deleted (A2 strict).
#   Covered by `_data_attachment_ids` / `_user_explicitly_wants_bi` helper
#   tests in test_orchestrator.py.
# - test_pre_tool_call_hook_*: hooks fire at SubagentRunner level now.
# - test_session_hard_budget_breaks_mid_turn: mid-turn budget guard removed
#   from turn_runner; covered at gateway/V3 budget layer.
# - test_aggressive_compact_triggered_when_soft_budget_hit: compaction moved
#   to SubagentRunner intra-loop (Sprint η).
# - test_needs_approval_tool_is_queued_not_executed: approval queue moved to
#   SubagentRunner.
# - test_thinking_model_emits_thinking_event: thinking event emitted by
#   SubagentRunner, not turn_runner.
# ─────────────────────────────────────────────────────────────
