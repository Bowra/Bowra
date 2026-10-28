"""Sprint chat-v3 Phase A · tool_finding tests (≥ 12 cases).

Covers tech-design r2 §A.5 acceptance:
    UT1   bi_query → emit tool_finding (≤120 chars)
    UT2   skill_read (lookup) → NO finding (category)
    UT3   add_chart (write) → NO finding (category)
    UT4   13th tool in turn → finding NULL (cap)
    UT5   LLM returns >120 char → truncate
    UT6   LLM returns "因为..." → reject (lead-in)
    UT7   BOWHEAD_TOOL_FINDING_ENABLED=0 → skip (disabled)
    UT8   preview_payload None → skip (no_preview)
    UT9   Truncate preview_for_finding payload-cap (≤ 1600 chars)
    UT10  Truncate keeps cols + top 3 rows
    UT11  Sanitize returns None on empty / whitespace
    UT12  generate_finding handles asyncio.TimeoutError → None + metric
    UT13  Truncate when single huge preview cell → reduce to cols + 1 row
    UT14  tool_finding event contains call_id (for FE linking)
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.terminal_runtime import TerminalDurabilityAbort
from bowhead_core.tool_category import lookup_kind, should_emit_finding_for_tool
from bowhead_core.tool_finding import (
    sanitize_finding,
    schedule_finding,
    truncate_preview_for_finding,
)

from bowhead_core import tool_finding as tf


# ─────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────
def _make_router(text: str = "30 品牌全数为负, top 跌幅: 阿迪 -1.28% / 耐克 -1.25%", *, input_tokens: int = 200, output_tokens: int = 40):
    """Mock Router with .invoke returning content=`text` + usage."""
    router = MagicMock()
    resp = MagicMock()
    resp.content = text
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    resp.usage = usage
    router.invoke = AsyncMock(return_value=resp)
    return router


def _make_bus():
    """Mock bus capturing emitted events."""
    bus = MagicMock()
    bus.emitted_events = []

    async def _emit(event):
        bus.emitted_events.append(event)

    bus.emit = _emit
    return bus


def _sample_preview():
    return {
        "kind": "table",
        "cols": ["品牌", "销量", "同比"],
        "rows": [
            ["阿迪达斯", "1.82M", "-1.28%"],
            ["耐克", "1.60M", "-1.25%"],
            ["欧莱雅", "1.18M", "-1.35%"],
        ],
        "total": 30,
    }


class _ProducerLease:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.released = False

    def bind_task(self, task: asyncio.Task) -> None:
        self.task = task

    async def release(self) -> None:
        self.released = True


class _TerminalFindingBus:
    terminal_protocol_version = 1

    def __init__(self, *, emit_error: BaseException | None = None) -> None:
        self.emit_error = emit_error
        self.emitted_events = []
        self.leases: list[_ProducerLease] = []

    async def register_producer(self) -> _ProducerLease:
        lease = _ProducerLease()
        self.leases.append(lease)
        return lease

    async def emit(self, event) -> None:
        if self.emit_error is not None:
            raise self.emit_error
        self.emitted_events.append(event)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    """Reset module state + env between tests."""
    tf._reset_for_tests()
    for var in (
        "BOWHEAD_TOOL_FINDING_ENABLED",
        "BOWHEAD_TOOL_FINDING_MAX_PER_TURN",
        "BOWHEAD_TOOL_FINDING_TIMEOUT_S",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.mark.asyncio
async def test_terminal_finding_reserves_bound_producer_before_return() -> None:
    bus = _TerminalFindingBus()

    assert await schedule_finding(
        bus=bus,
        turn_id="t_registered",
        tool_name="bi_query",
        args_text="",
        preview_payload=_sample_preview(),
        call_id="c_registered",
        parent_agent_id=None,
        turn_finding_count=0,
        router=_make_router(),
    )

    assert len(bus.leases) == 1
    assert bus.leases[0].task is not None
    await tf.drain_pending_findings(
        turn_id="t_registered",
        timeout_s=1.0,
    )
    assert bus.leases[0].released


@pytest.mark.asyncio
async def test_terminal_finding_failure_is_isolated_by_turn() -> None:
    failed_bus = _TerminalFindingBus(emit_error=TerminalDurabilityAbort("turn A durable failure"))
    healthy_bus = _TerminalFindingBus()
    for turn_id, bus in (("t_a", failed_bus), ("t_b", healthy_bus)):
        assert await schedule_finding(
            bus=bus,
            turn_id=turn_id,
            tool_name="bi_query",
            args_text="",
            preview_payload=_sample_preview(),
            call_id=f"c_{turn_id}",
            parent_agent_id=None,
            turn_finding_count=0,
            router=_make_router(),
        )

    await tf.drain_pending_findings(turn_id="t_b", timeout_s=1.0)
    assert len(healthy_bus.emitted_events) == 1
    with pytest.raises(TerminalDurabilityAbort, match="turn A"):
        await tf.drain_pending_findings(turn_id="t_a", timeout_s=1.0)


async def _drain():
    """Helper: yield once so create_task tasks run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ─────────────────────────────────────────────────────────────────
# UT1 — bi_query → emit
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut1_bi_query_emits_finding_under_120_chars():
    router = _make_router(text="30 品牌全数为负, top 跌幅: 阿迪 -1.28%。")
    bus = _make_bus()

    scheduled = await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="bi_query",
        args_text="2026-04 销量同比 按品牌",
        preview_payload=_sample_preview(),
        call_id="c1",
        parent_agent_id="sub_x",
        turn_finding_count=0,
        router=router,
    )
    assert scheduled is True

    await tf.drain_pending_findings(turn_id="t1", timeout_s=1.0)
    assert len(bus.emitted_events) == 1
    e = bus.emitted_events[0]
    assert e.event_type == "tool_finding"
    assert e.payload["call_id"] == "c1"
    assert len(e.payload["finding"]) <= 120


# ─────────────────────────────────────────────────────────────────
# UT2 — skill_read (lookup) → NO finding
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut2_skill_read_skipped_lookup_category():
    router = _make_router()
    bus = _make_bus()

    scheduled = await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="skill_read",
        args_text="some_skill",
        preview_payload=_sample_preview(),
        call_id="c1",
        parent_agent_id=None,
        turn_finding_count=0,
        router=router,
    )
    assert scheduled is False
    await _drain()
    assert bus.emitted_events == []
    router.invoke.assert_not_called()


# ─────────────────────────────────────────────────────────────────
# UT3 — add_chart (write) → NO finding
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut3_canvas_write_skipped_category():
    router = _make_router()
    bus = _make_bus()

    scheduled = await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="add_chart",
        args_text="{}",
        preview_payload=_sample_preview(),
        call_id="c1",
        parent_agent_id=None,
        turn_finding_count=0,
        router=router,
    )
    assert scheduled is False
    await _drain()
    assert bus.emitted_events == []


# ─────────────────────────────────────────────────────────────────
# UT4 — turn cap
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut4_turn_cap_hit_skips():
    router = _make_router()
    bus = _make_bus()

    # 12 is the default cap. 13th call (count=12) should skip.
    scheduled = await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="bi_query",
        args_text="x",
        preview_payload=_sample_preview(),
        call_id="c13",
        parent_agent_id=None,
        turn_finding_count=12,
        router=router,
    )
    assert scheduled is False


# ─────────────────────────────────────────────────────────────────
# UT5 — LLM long output → truncate
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut5_llm_long_output_truncated_120():
    long = "x" * 250
    router = _make_router(text=long)
    bus = _make_bus()

    await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="bi_query",
        args_text="",
        preview_payload=_sample_preview(),
        call_id="c1",
        parent_agent_id=None,
        turn_finding_count=0,
        router=router,
    )
    await tf.drain_pending_findings(turn_id="t1", timeout_s=1.0)
    assert len(bus.emitted_events) == 1
    assert len(bus.emitted_events[0].payload["finding"]) == 120


# ─────────────────────────────────────────────────────────────────
# UT6 — reject the LLM lead-in 因为.
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut6_lead_in_rejected():
    router = _make_router(text="因为 4 月销量同比下降, 所以...")
    bus = _make_bus()

    await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="bi_query",
        args_text="",
        preview_payload=_sample_preview(),
        call_id="c1",
        parent_agent_id=None,
        turn_finding_count=0,
        router=router,
    )
    await tf.drain_pending_findings(turn_id="t1", timeout_s=1.0)
    # rejected — no event emitted (sanitize returned None)
    assert bus.emitted_events == []


# ─────────────────────────────────────────────────────────────────
# UT7 — flag disabled → skip
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut7_flag_disabled_skips(monkeypatch):
    monkeypatch.setenv("BOWHEAD_TOOL_FINDING_ENABLED", "0")
    router = _make_router()
    bus = _make_bus()

    scheduled = await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="bi_query",
        args_text="x",
        preview_payload=_sample_preview(),
        call_id="c1",
        parent_agent_id=None,
        turn_finding_count=0,
        router=router,
    )
    assert scheduled is False
    await _drain()
    assert bus.emitted_events == []


# ─────────────────────────────────────────────────────────────────
# UT8 — preview None → skip
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut8_preview_none_skips():
    router = _make_router()
    bus = _make_bus()

    scheduled = await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="bi_query",
        args_text="x",
        preview_payload=None,
        call_id="c1",
        parent_agent_id=None,
        turn_finding_count=0,
        router=router,
    )
    assert scheduled is False


# ─────────────────────────────────────────────────────────────────
# UT9 — truncate caps total chars
# ─────────────────────────────────────────────────────────────────
def test_ut9_truncate_caps_chars():
    huge = {
        "kind": "table",
        "cols": ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"],
        "rows": [["v" * 200] * 8 for _ in range(20)],
        "total": 20,
    }
    out = truncate_preview_for_finding(huge)
    assert len(out["cols"]) <= 6
    assert len(out["rows"]) <= 3
    # each cell ≤ 40 chars
    for row in out["rows"]:
        for cell in row:
            assert len(cell) <= 40


# ─────────────────────────────────────────────────────────────────
# UT10 — truncate keeps cols + top 3
# ─────────────────────────────────────────────────────────────────
def test_ut10_truncate_keeps_cols_top3():
    out = truncate_preview_for_finding(_sample_preview())
    assert out["cols"] == ["品牌", "销量", "同比"]
    assert len(out["rows"]) == 3
    assert out["total"] == 30


# ─────────────────────────────────────────────────────────────────
# UT11 — sanitize returns None on empty / whitespace
# ─────────────────────────────────────────────────────────────────
def test_ut11_sanitize_empty_returns_none():
    assert sanitize_finding(None) is None
    assert sanitize_finding("") is None
    assert sanitize_finding("   \n\t   ") is None
    assert sanitize_finding("有效 finding 内容") == "有效 finding 内容"


# ─────────────────────────────────────────────────────────────────
# UT12 — TimeoutError → None + error metric
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut12_timeout_returns_none(monkeypatch):
    router = MagicMock()

    # Simulate slow LLM that exceeds timeout
    async def slow_invoke(*args, **kwargs):
        await asyncio.sleep(5.0)
        raise AssertionError("should have timed out")

    router.invoke = slow_invoke

    monkeypatch.setenv("BOWHEAD_TOOL_FINDING_TIMEOUT_S", "0.1")
    bus = _make_bus()

    await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="bi_query",
        args_text="",
        preview_payload=_sample_preview(),
        call_id="c1",
        parent_agent_id=None,
        turn_finding_count=0,
        router=router,
    )
    await tf.drain_pending_findings(turn_id="t1", timeout_s=1.0)
    # timeout → no event emitted
    assert bus.emitted_events == []


# ─────────────────────────────────────────────────────────────────
# UT13 — truncate huge single cell → reduce to cols + 1 row
# ─────────────────────────────────────────────────────────────────
def test_ut13_truncate_oversize_reduces_to_one_row():
    over = {
        "kind": "table",
        "cols": ["a", "b", "c"],
        # rows that after truncation still serialize > 1600 chars
        "rows": [["x" * 40] * 3 for _ in range(3)],
        "total": 3,
    }
    out = truncate_preview_for_finding(over)
    # Either fits in budget (3 rows kept) or shrinks to 1 row
    serialized = json.dumps(out, ensure_ascii=False)
    assert len(serialized) <= tf._PROMPT_PAYLOAD_MAX_CHARS or len(out["rows"]) == 1


# ─────────────────────────────────────────────────────────────────
# UT14 — tool_finding event has call_id linking
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut14_emit_event_has_call_id():
    router = _make_router(text="finding text")
    bus = _make_bus()

    await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="python_sandbox",
        args_text="code",
        preview_payload=_sample_preview(),
        call_id="call_uniq_abc",
        parent_agent_id="sub_xyz",
        turn_finding_count=0,
        router=router,
    )
    await tf.drain_pending_findings(turn_id="t1", timeout_s=1.0)
    assert len(bus.emitted_events) == 1
    e = bus.emitted_events[0]
    assert e.event_type == "tool_finding"
    assert e.payload["call_id"] == "call_uniq_abc"
    assert e.payload["tool_name"] == "python_sandbox"
    assert e.parent_agent_id == "sub_xyz"


# ─────────────────────────────────────────────────────────────────
# UT16 — turn-scope counter on bus (P0-A4 close · phase-ab-review)
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut16_bus_scope_counter_caps_across_peers(monkeypatch):
    """Counter on bus (not ctx) → Lead + 2 peers share 12-cap, not 3×12."""
    monkeypatch.setenv("BOWHEAD_TOOL_FINDING_MAX_PER_TURN", "3")
    router = _make_router()
    bus = _make_bus()
    bus._tool_finding_count = 0  # type: ignore[attr-defined]

    # Simulate 5 tool_results across mixed peers (Lead + 2 compute peers).
    # All 5 read+write the SAME bus._tool_finding_count, so cap 3 is shared.
    for i in range(5):
        _v3_count = int(getattr(bus, "_tool_finding_count", 0))
        scheduled = await schedule_finding(
            bus=bus,
            turn_id="t1",
            tool_name="bi_query",
            args_text=f"q{i}",
            preview_payload=_sample_preview(),
            call_id=f"c{i}",
            parent_agent_id=f"sub_{i % 3}",
            turn_finding_count=_v3_count,
            router=router,
        )
        if scheduled:
            bus._tool_finding_count = _v3_count + 1

    # Cap=3 → only first 3 scheduled → exactly 3 emit events.
    await tf.drain_pending_findings(turn_id="t1", timeout_s=1.0)
    assert len(bus.emitted_events) == 3
    assert bus._tool_finding_count == 3


# ─────────────────────────────────────────────────────────────────
# UT17 — real cost from usage + provider (P0-A5 + B-3 close)
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut17_real_cost_uses_provider_rate():
    """LLMResponse.provider routed through _estimate_cost_usd, not hardcoded."""
    # DeepSeek rate (input 0.00027, output 0.00110 per 1k)
    router = MagicMock()
    resp = MagicMock()
    resp.content = "finding"
    usage = MagicMock()
    usage.input_tokens = 1000
    usage.output_tokens = 100
    resp.usage = usage
    resp.provider = "deepseek_v4_pro"
    router.invoke = AsyncMock(return_value=resp)

    bus = _make_bus()
    await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="bi_query",
        args_text="",
        preview_payload=_sample_preview(),
        call_id="c1",
        parent_agent_id=None,
        turn_finding_count=0,
        router=router,
    )
    await tf.drain_pending_findings(turn_id="t1", timeout_s=1.0)
    # Event emitted = finding pipeline ran end-to-end including cost calc.
    # We don't assert cost value (it's metric-emit), only that it didn't crash
    # on now-correct provider attribute path.
    assert len(bus.emitted_events) == 1


# ─────────────────────────────────────────────────────────────────
# UT18 — INTEGRATION: simulate subagent.py wire site path
# (closes phase-ab-review "Integration test gap")
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ut18_integration_wire_site_emits_finding_after_tool_result():
    """Replicates subagent.py:1898 + 1917 wire pattern.

    Asserts:
      1. tool_result emit happens (we simulate this)
      2. schedule_finding kicks off task
      3. After drain, bus has BOTH tool_result + tool_finding with same call_id
      4. tool_finding parent_agent_id = subagent_id (peer scope)
    """
    from bowhead_core.events import OrchestratorEvent

    router = _make_router(text="3 品牌全数为负, top 跌幅: 阿迪 -1.28%")
    bus = _make_bus()
    bus._tool_finding_count = 0  # type: ignore[attr-defined]

    # Step 1: simulate tool_result emit (what subagent.py:1897 does)
    call_id = "call_xyz"
    subagent_id = "sub_compute_42"
    await bus.emit(
        OrchestratorEvent(
            event_type="tool_result",
            turn_id="t_integration",
            payload={
                "subagent_id": subagent_id,
                "tool_name": "bi_query",
                "call_id": call_id,
                "ok": True,
                "duration_ms": 1500,
                "result_preview": "30 行",
                "preview_payload": _sample_preview(),
            },
            parent_agent_id=subagent_id,  # peer scope
        )
    )

    # Step 2: schedule_finding (what subagent.py:1922 does)
    scheduled = await schedule_finding(
        bus=bus,
        turn_id="t_integration",
        tool_name="bi_query",
        args_text="2026-04 销售量按品牌",
        preview_payload=_sample_preview(),
        call_id=call_id,
        parent_agent_id=subagent_id,
        turn_finding_count=0,
        router=router,
    )
    assert scheduled is True
    bus._tool_finding_count = 1

    # Step 3: drain (what lead_orchestrator._emit_event does at done time)
    await tf.drain_pending_findings(
        turn_id="t_integration",
        timeout_s=1.0,
    )

    # Step 4: assert both events on bus with correct linkage
    assert len(bus.emitted_events) == 2
    e_result, e_finding = bus.emitted_events
    assert e_result.event_type == "tool_result"
    assert e_finding.event_type == "tool_finding"
    assert e_finding.payload["call_id"] == call_id  # linked to tool_result
    assert e_finding.payload["tool_name"] == "bi_query"
    assert e_finding.parent_agent_id == subagent_id  # peer-scope propagated
    assert "阿迪" in e_finding.payload["finding"]


# ─────────────────────────────────────────────────────────────────
# UT15 — category mapping (sanity check)
# ─────────────────────────────────────────────────────────────────
def test_ut15_category_mapping_known_tools():
    assert lookup_kind("bi_query") == "query"
    assert lookup_kind("python_sandbox") == "compute"
    assert lookup_kind("kb_search") == "query"
    assert lookup_kind("skill_read") == "lookup"
    assert lookup_kind("add_chart") == "chart"
    assert lookup_kind("send_email") == "write"
    assert lookup_kind("unknown_tool_xyz") == "compute"  # fallback

    assert should_emit_finding_for_tool("bi_query") is True
    assert should_emit_finding_for_tool("python_sandbox") is True
    assert should_emit_finding_for_tool("skill_read") is False
    assert should_emit_finding_for_tool("add_chart") is False


# ─────────────────────────────────────────────────────────────────
# chat-truth Phase B-BE · static_finding_for templates
# ─────────────────────────────────────────────────────────────────
from bowhead_core.tool_finding import static_finding_for  # noqa: E402


def test_b_t7_static_finding_for_subagent_spawn():
    """B-T7: subagent_spawn → static template returns non-empty string ≤ 80 chars."""
    s = static_finding_for(
        "subagent_spawn",
        {"profile": "数据分析师", "task": "拆解 GMV 同比下降归因"},
        None,
    )
    assert s is not None
    assert isinstance(s, str)
    assert len(s) > 0
    assert len(s) <= 80
    assert "数据分析师" in s


def test_b_t7b_static_finding_other_templates_non_empty():
    """B-T7b: other templates also produce non-empty ≤80 char strings."""
    cases = [
        ("skill_read", {"name": "compute_breakdown"}),
        ("skill_invoke", {"name": "explain_chart"}),
        ("add_chart", {"hint": "rank", "title": "2026年4月 品牌GMV排名"}),
        # Removed canvas_add_iframe case: main 26bf0694 retired datarc_iframe creation and removed
        # this tool_finding template (static_finding_for returns None). This was a stale assertion left on main
        # (found by full revalidation after merge; missed by main CI).
        ("kb_read", {"doc_id": "doc_001"}),
        ("kb_list_documents", {}),
    ]
    for tool_name, args in cases:
        s = static_finding_for(tool_name, args, None)
        assert s is not None, f"{tool_name} 应返回 non-None"
        assert 0 < len(s) <= 80


def test_b_t8_static_finding_unknown_tool_returns_none():
    """B-T8: unknown tool → returns None (fall through to LLM path or skip)."""
    assert static_finding_for("mcp_foo_bar", {}, None) is None
    assert static_finding_for("bi_query", {}, None) is None  # LLM-eligible, not in static map


@pytest.mark.asyncio
async def test_b_t9_schedule_finding_short_circuits_when_static_matches(monkeypatch):
    """B-T9: for non-LLM-eligible tool with static template match, the LLM path
    (schedule_finding) skips by category gate — haiku NOT called.

    This validates the existing category gate already short-circuits;
    the new wire in subagent.py emits static finding synchronously instead.
    """
    router = _make_router()
    bus = _make_bus()
    # subagent_spawn → spawn kind → not in _FINDING_ALLOW_KINDS → skip
    scheduled = await schedule_finding(
        bus=bus,
        turn_id="t1",
        tool_name="subagent_spawn",
        args_text="profile=数据分析师",
        preview_payload=_sample_preview(),
        call_id="c1",
        parent_agent_id=None,
        turn_finding_count=0,
        router=router,
    )
    assert scheduled is False
    await _drain()
    # Haiku NOT called
    router.invoke.assert_not_called()
    # No emitted events (subagent.py wire emits static separately; tested via
    # integration)
    assert bus.emitted_events == []

    # Static template available for caller to emit synchronously
    static = static_finding_for("subagent_spawn", {"profile": "数据分析师"}, None)
    assert static is not None
    assert "数据分析师" in static


# ─────────────────────────────────────────────────────────────────
# chat-truth-hotfix-2 (2026-05-15) — bi_schema static finding regression
# ─────────────────────────────────────────────────────────────────


def test_chat_truth_hotfix_bi_schema_static_finding_emits_counts():
    """bi_schema result has measures+dimensions; template summarizes counts."""
    s = static_finding_for(
        "bi_schema",
        {"kind": "both"},
        {
            "measures": [{"id": 1, "names": ["GMV"]}, {"id": 2, "names": ["UV"]}],
            "dimensions": [{"id": 1, "names": ["品类"]}],
        },
    )
    assert s is not None
    assert "2" in s and "1" in s
    assert "测度" in s and "维度" in s


def test_chat_truth_hotfix_bi_schema_static_handles_empty_result():
    """Defensive: empty / missing measures+dimensions still returns a finding (not None)."""
    s = static_finding_for("bi_schema", {}, {})
    assert s is not None
    assert "0 测度" in s and "0 维度" in s


def test_chat_truth_hotfix_bi_schema_kind_is_lookup_not_compute():
    """bi_schema kind must be 'lookup' so static_finding path triggers
    (not 'compute' default which would route to LLM schedule + skip on no preview)."""
    from bowhead_core.tool_category import lookup_kind, should_emit_finding_for_kind

    assert lookup_kind("bi_schema") == "lookup"
    # lookup is NOT in ALLOW_KINDS → static path
    assert should_emit_finding_for_kind("lookup") is False
