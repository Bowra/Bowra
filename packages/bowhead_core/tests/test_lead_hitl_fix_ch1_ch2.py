"""Batched clarification merging and tool-discriminator contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.clarification_tool import make_clarification_sentinel
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult
from bowhead_core.types import ToolInvocation, TurnStatus

# ─────────────────────────────────────────────────────────────
# Shared fakes
# ─────────────────────────────────────────────────────────────


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    @property
    def types(self) -> list[str]:
        return [e.event_type for e in self.events]

    def find(self, event_type: str) -> Any | None:
        for e in self.events:
            if e.event_type == event_type:
                return e
        return None

    def find_all(self, event_type: str) -> list[Any]:
        return [e for e in self.events if e.event_type == event_type]


@dataclass
class _FakeSubagentRunner:
    """Minimal SubagentRunner stand-in returning a pre-seeded SubagentResult."""

    result: SubagentResult | None = None

    async def run(
        self,
        *,
        profile_name: str,
        task: str,
        parent_turn_id: str,
        parent_bus: Any,
        parent_ctx: Any,
        extra_instructions: str | None = None,
        state: Any = None,
        stream_callback: Any = None,
        stream_on_tool_call: Any = None,
        **_kwargs: Any,
    ) -> SubagentResult:
        if self.result is not None:
            return self.result
        return SubagentResult(
            profile=profile_name,
            subagent_id="sub_test",
            ok=True,
            final_answer="(default)",
            tokens_input=0,
            tokens_output=0,
        )


def _make_sctx(turn_id: str = "t_test", user_text: str = "hi") -> SerializableTurnContext:
    return SerializableTurnContext(
        user_id="u1",
        workspace_id="ws1",
        session_id="s1",
        turn_id=turn_id,
        role="user",
    )


def _make_ctx(clarification_store: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        clarification_store=clarification_store,
        _m137_subagent_payloads=[],
        execution_generation=1,
        execution_owner_id=None,
    )


@pytest.fixture(autouse=True)
def _ensure_lead_profile():
    """Ensure stub 'lead' profile is in PROFILES (autouse conftest already
    handles this, but we belt-and-suspenders for explicit clarity)."""
    pre_existing = "lead" in PROFILES
    if not pre_existing:
        PROFILES["lead"] = SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            tool_whitelist=(
                "subagent_return",
                "bi_ask_question",
                "clarification_ask",
            ),
            system_prompt="(stub)",
            max_iterations=4,
        )
    try:
        yield
    finally:
        if not pre_existing:
            PROFILES.pop("lead", None)


@pytest.fixture
def _stream_env(monkeypatch):
    """Default streaming ON (prod default) but disable LeadDispatchGuard
    (BOWHEAD_LEAD_POST_PEER_BREAK=0) to skip the post-peer guard wiring that
    requires a populated `_m137_subagent_payloads` ref. The
    `strip_response_kind_markers` import lives inside the
    `if streaming_enabled:` block (line ~1035) and Python's scope analysis
    makes it function-local — turning streaming OFF would render that name
    unbound at the post-loop callsite (~line 2126). Pre-existing module
    shadow; we don't fix here, just don't trigger it."""
    monkeypatch.setenv("BOWHEAD_STREAMING_LEAD", "1")
    monkeypatch.setenv("BOWHEAD_LEAD_POST_PEER_BREAK", "0")


# ═════════════════════════════════════════════════════════════
# CH1 — task_plan Lead pre-gate
# ═════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════
# CH2 — batched HITL sentinel merge
# ═════════════════════════════════════════════════════════════


def _inv(tool_name: str, result_dict: dict) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool_name,
        arguments={},
        result=result_dict,
    )


@pytest.mark.asyncio
async def test_ch2_two_sentinels_merge_into_single_emit(
    monkeypatch,
    _stream_env,
):
    """≥2 sentinels in tool_invocations → ONE merged clarification_required
    event with combined candidates + multi_select=True. ClarificationStore.put
    invoked once."""
    bus = _FakeBus()

    # Real store so we can assert put was called once with merged candidates.
    import tempfile

    from bowhead_core.clarification_store import ClarificationStore

    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        ctx = _make_ctx(clarification_store=store)

        s1 = make_clarification_sentinel(
            candidates=["营收", "销量"],
            fallback_text="metric 不明",
            aborted_turn_id="t_ch2_merge",
        )
        s2 = make_clarification_sentinel(
            candidates=["上月", "本月"],
            fallback_text="time 不明",
            aborted_turn_id="t_ch2_merge",
        )
        sub_result = SubagentResult(
            profile="lead",
            subagent_id="sub_m",
            ok=True,
            final_answer="(not used)",
            tokens_input=10,
            tokens_output=5,
            tool_invocations=[
                _inv("clarification_ask", s1),
                _inv("clarification_ask", s2),
            ],
        )
        runner = _FakeSubagentRunner(result=sub_result)
        runtime = LeadRuntime(
            router=SimpleNamespace(),
            tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
            parent_bus=bus,
            parent_ctx=ctx,
            subagent_runner=runner,
        )
        sctx = _make_sctx(turn_id="t_ch2_merge")
        result = await _lead_loop(sctx, "compound q", runtime)

        # Exactly ONE clarification_required event.
        clar_events = bus.find_all("clarification_required")
        assert len(clar_events) == 1, f"expected 1 merged event, got {len(clar_events)}"
        payload = clar_events[0].payload
        # Combined candidates (order-preserving, no dups).
        assert payload["candidates"] == ["营收", "销量", "上月", "本月"]
        # multi_select=True flag set.
        assert payload.get("multi_select") is True
        # ClarificationStore row written (kind=ambiguous_turn, multi_select=True).
        pending = await store.get_pending_for_turn("t_ch2_merge")
        assert len(pending) == 1, "store.put must be called exactly once"
        assert pending[0].kind == "ambiguous_turn"
        assert pending[0].multi_select is True
        labels = [c.label for c in pending[0].candidates]
        assert labels == ["营收", "销量", "上月", "本月"]
        # clarification_id in event matches the persisted row.
        assert payload["clarification_id"] == pending[0].request_id
        # CLARIFICATION_PENDING TurnResult.
        assert result.status == TurnStatus.CLARIFICATION_PENDING


@pytest.mark.asyncio
async def test_ch2_single_sentinel_preserves_existing_behavior(
    monkeypatch,
    _stream_env,
):
    """1 sentinel → merge path (post bi-hitl-cleanup B3 collapse). multi_select
    is False for N=1. Store.put NOT called here because FakeSubagentRunner
    bypasses the real clarification_ask handler (which would normally write).

    Pre-B3 contract (multi_select NOT in payload, separate single-emit branch)
    was intentionally retired — see lead_orchestrator.py:1496-1503 comment.

    2026-09-04 HITL 审计 §5：这个哨兵没有 `store_persisted=True`（FakeSubagentRunner
    绕过了真 handler），所以它证明不了背后有行 —— CH2 会补写一次，发出去的
    clarification_id 是补写那一行的 id。此前这里断言的是「原样透传哨兵 id + 不写库」，
    那正是「发一张点了必 404 的卡」的形状，只是被当成 harness 事实钉住了。
    """
    bus = _FakeBus()

    import tempfile

    from bowhead_core.clarification_store import ClarificationStore

    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        ctx = _make_ctx(clarification_store=store)

        s1 = make_clarification_sentinel(
            candidates=["营收", "销量", "GMV"],
            fallback_text="想看哪个指标?",
            aborted_turn_id="t_ch2_single",
            clarification_id="clar_single_xyz",
        )
        sub_result = SubagentResult(
            profile="lead",
            subagent_id="sub_s",
            ok=True,
            final_answer="(not used)",
            tokens_input=10,
            tokens_output=5,
            tool_invocations=[_inv("clarification_ask", s1)],
        )
        runner = _FakeSubagentRunner(result=sub_result)
        runtime = LeadRuntime(
            router=SimpleNamespace(),
            tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
            parent_bus=bus,
            parent_ctx=ctx,
            subagent_runner=runner,
        )
        sctx = _make_sctx(turn_id="t_ch2_single")
        await _lead_loop(sctx, "ambiguous q", runtime)

        clar_events = bus.find_all("clarification_required")
        assert len(clar_events) == 1
        payload = clar_events[0].payload
        # Single-path candidates preserved.
        assert payload["candidates"] == ["营收", "销量", "GMV"]
        # Post-B3 collapse: multi_select is False for N=1 (was absent pre-B3).
        assert payload.get("multi_select") is False
        # Audit §5: sentinel lacks proof of a row → CH2 persists it; emitted ID must reference an actual row.
        pending = await store.get_pending_for_turn("t_ch2_single")
        assert len(pending) == 1
        assert payload["clarification_id"] == pending[0].request_id
        assert payload["clarification_id"] != "clar_single_xyz"


@pytest.mark.asyncio
async def test_ch2_dedup_same_candidate_string_from_two_sentinels(
    monkeypatch,
    _stream_env,
):
    """Dedup: when two sentinels share a candidate string, merged list keeps
    only the first occurrence."""
    bus = _FakeBus()
    import tempfile

    from bowhead_core.clarification_store import ClarificationStore

    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        ctx = _make_ctx(clarification_store=store)

        s1 = make_clarification_sentinel(
            candidates=["GMV", "销量"],
            fallback_text=None,
            aborted_turn_id="t_dedup",
        )
        s2 = make_clarification_sentinel(
            # "销量" already in s1 — should NOT appear twice.
            candidates=["销量", "营收"],
            fallback_text=None,
            aborted_turn_id="t_dedup",
        )
        sub_result = SubagentResult(
            profile="lead",
            subagent_id="sub_d",
            ok=True,
            final_answer="(not used)",
            tokens_input=10,
            tokens_output=5,
            tool_invocations=[
                _inv("clarification_ask", s1),
                _inv("clarification_ask", s2),
            ],
        )
        runner = _FakeSubagentRunner(result=sub_result)
        runtime = LeadRuntime(
            router=SimpleNamespace(),
            tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
            parent_bus=bus,
            parent_ctx=ctx,
            subagent_runner=runner,
        )
        sctx = _make_sctx(turn_id="t_dedup")
        await _lead_loop(sctx, "compound q", runtime)

        payload = bus.find("clarification_required").payload
        # First occurrence preserved, no dup.
        assert payload["candidates"] == ["GMV", "销量", "营收"]
        # Store row dedup too.
        pending = await store.get_pending_for_turn("t_dedup")
        assert len(pending) == 1
        assert [c.label for c in pending[0].candidates] == ["GMV", "销量", "营收"]


@pytest.mark.asyncio
async def test_ch2_zero_sentinels_no_clarification_emit(
    monkeypatch,
    _stream_env,
):
    """0 sentinels → no clarification_required event. Lead falls through to
    final_answer composition (existing path)."""
    bus = _FakeBus()
    ctx = _make_ctx()
    # No sentinel — just a normal tool result.
    sub_result = SubagentResult(
        profile="lead",
        subagent_id="sub_n",
        ok=True,
        final_answer="normal answer",
        tokens_input=10,
        tokens_output=5,
        tool_invocations=[
            _inv("bi_ask_question", {"ok": True, "rows": []}),
        ],
    )
    runner = _FakeSubagentRunner(result=sub_result)
    runtime = LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        subagent_runner=runner,
    )
    sctx = _make_sctx(turn_id="t_no_sent")
    result = await _lead_loop(sctx, "simple q", runtime)

    assert bus.find_all("clarification_required") == []
    assert result.final_answer == "normal answer"


class _StreamingRunner(_FakeSubagentRunner):
    """Streams two LLM calls (the first continues with a tool) before returning."""

    calls: list[list[str]] | None = None

    async def run(self, *, stream_callback: Any = None, stream_on_tool_call: Any = None, parent_bus: Any = None, **kwargs: Any) -> SubagentResult:
        assert stream_callback is not None and stream_on_tool_call is not None
        for index, chunks in enumerate(self.calls or []):
            for chunk in chunks:
                await stream_callback(chunk)
            if index < len(self.calls or []) - 1:
                await stream_on_tool_call()
                await parent_bus.emit(SimpleNamespace(event_type="tool_boundary", payload={}))
        return await super().run(stream_callback=stream_callback, stream_on_tool_call=stream_on_tool_call, parent_bus=parent_bus, **kwargs)


@pytest.mark.asyncio
async def test_held_marker_tails_are_flushed_at_call_boundary_and_turn_end(monkeypatch, _stream_env) -> None:
    """Text the marker filter holds (`<`, `_kind`) used to be lost when a call ended there."""
    monkeypatch.delenv("BOWHEAD_PSI_HITL_TASK_PLAN_ENABLED", raising=False)
    bus = _FakeBus()
    runner = _StreamingRunner(
        result=SubagentResult(profile="lead", subagent_id="sub_s", ok=True, final_answer="done", tokens_input=1, tokens_output=1),
    )
    first = "<response_kind>chat</response_kind><headline>H</headline>check if a <"
    second = "<response_kind>chat</response_kind>then name it value _kind"
    runner.calls = [[first[i : i + 3] for i in range(0, len(first), 3)], [second[i : i + 4] for i in range(0, len(second), 4)]]
    runtime = LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=_make_ctx(),
        subagent_runner=runner,
    )
    await _lead_loop(_make_sctx(turn_id="t_flush"), "simple q", runtime)

    sequence = [
        "|" if event.event_type == "tool_boundary" else event.payload.get("delta", "")
        for event in bus.events
        if event.event_type in {"narrative.delta", "tool_boundary"}
    ]
    assert "".join(sequence) == "check if a <|then name it value _kind"


@pytest.mark.asyncio
async def test_final_answer_has_no_blank_lines_left_by_stripped_markers(
    monkeypatch,
    _stream_env,
):
    """Markers on their own lines used to leave "\\n\\n\\n\\n" before the answer."""
    monkeypatch.delenv("BOWHEAD_PSI_HITL_TASK_PLAN_ENABLED", raising=False)
    bus = _FakeBus()
    sub_result = SubagentResult(
        profile="lead",
        subagent_id="sub_markers",
        ok=True,
        final_answer="<response_kind>chat</response_kind>\n\n<headline>结论</headline>\n\n第一段。\n\n```\na = 1\n\n\nb = 2\n```",
        tokens_input=10,
        tokens_output=5,
        tool_invocations=[_inv("bi_ask_question", {"ok": True, "rows": []})],
    )
    runtime = LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=_make_ctx(),
        subagent_runner=_FakeSubagentRunner(result=sub_result),
    )
    result = await _lead_loop(_make_sctx(turn_id="t_markers"), "simple q", runtime)

    # Only the marker lines go; blank lines inside the code block stay.
    assert result.final_answer == "第一段。\n\n```\na = 1\n\n\nb = 2\n```"
    for event in bus.find_all("final_answer"):
        text = str(event.payload.get("text") or event.payload.get("final_answer") or "")
        assert not text.startswith("\n")
        assert "<headline>" not in text


@pytest.mark.asyncio
async def test_ch2_tool_context_sentinel_forwards_tool_discriminator(
    monkeypatch,
    _stream_env,
):
    """connector-context T1#6 emit-contract pin: shape-2 tool_context_select
    sentinel 的 connector 判别符经 CH2 merge 后 ①clarification_required SSE
    payload 带 tool ②store 行落 tool 列（修前两处皆丢，红）."""
    bus = _FakeBus()

    import tempfile

    from bowhead_core.clarification_store import ClarificationStore

    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        ctx = _make_ctx(clarification_store=store)

        shape2 = {
            "ok": False,
            "error_kind": "needs_clarification",
            "clarification": {
                "kind": "tool_context_select",
                "tool": "datarc",
                "prompt": "你在北极九章有 2 个可访问的工作区，这次查询用哪个？",
                "options": [
                    {"value": "42", "label": "零售集团"},
                    {"value": "7", "label": "华东大区"},
                ],
                "remember_option": True,
            },
            "rows": [],
        }
        sub_result = SubagentResult(
            profile="lead",
            subagent_id="sub_m",
            ok=True,
            final_answer="(not used)",
            tokens_input=10,
            tokens_output=5,
            tool_invocations=[_inv("bi_query", shape2)],
        )
        runner = _FakeSubagentRunner(result=sub_result)
        runtime = LeadRuntime(
            router=SimpleNamespace(),
            tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
            parent_bus=bus,
            parent_ctx=ctx,
            subagent_runner=runner,
        )
        sctx = _make_sctx(turn_id="t_ch2_tool")
        result = await _lead_loop(sctx, "今年毛利", runtime)

        clar_events = bus.find_all("clarification_required")
        assert len(clar_events) == 1
        payload = clar_events[0].payload
        assert payload["tool"] == "datarc"
        assert payload["kind"] == "tool_context_select"
        pending = await store.get_pending_for_turn("t_ch2_tool")
        assert len(pending) == 1
        assert pending[0].tool == "datarc"
        assert pending[0].kind == "tool_context_select"
        assert result.status == TurnStatus.CLARIFICATION_PENDING


@pytest.mark.asyncio
async def test_ch2_generic_sentinel_tool_is_none(
    monkeypatch,
    _stream_env,
):
    """generic shape-1 sentinel 路：payload/store 行 tool 均为 None（additive 零行为）."""
    bus = _FakeBus()

    import tempfile

    from bowhead_core.clarification_store import ClarificationStore

    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        ctx = _make_ctx(clarification_store=store)

        s1 = make_clarification_sentinel(
            candidates=["营收", "销量"],
            fallback_text="metric 不明",
            aborted_turn_id="t_ch2_generic_tool",
        )
        s2 = make_clarification_sentinel(
            candidates=["上月", "本月"],
            fallback_text="time 不明",
            aborted_turn_id="t_ch2_generic_tool",
        )
        sub_result = SubagentResult(
            profile="lead",
            subagent_id="sub_m",
            ok=True,
            final_answer="(not used)",
            tokens_input=10,
            tokens_output=5,
            tool_invocations=[
                _inv("clarification_ask", s1),
                _inv("clarification_ask", s2),
            ],
        )
        runner = _FakeSubagentRunner(result=sub_result)
        runtime = LeadRuntime(
            router=SimpleNamespace(),
            tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
            parent_bus=bus,
            parent_ctx=ctx,
            subagent_runner=runner,
        )
        sctx = _make_sctx(turn_id="t_ch2_generic_tool")
        await _lead_loop(sctx, "compound q", runtime)

        clar_events = bus.find_all("clarification_required")
        assert len(clar_events) == 1
        assert clar_events[0].payload["tool"] is None
        pending = await store.get_pending_for_turn("t_ch2_generic_tool")
        assert len(pending) == 1
        assert pending[0].tool is None
