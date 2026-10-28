"""prod-bug-bash-039 #6 citation reform end-to-end tests (0.39.2 + 0.39.3).

Covers the registry + resolve + flush chokepoints land-on-prod cares about:
  - TurnCitationRegistry: register dedup, resolve OOB → None, ensure_registry
    lazy-init on frozen/free contexts.
  - _resolve_citations: `[data:art_xxx]` → `[cite:N]` rewrite registers the
    artifact + leaves `[cite:N]` orphans as `[?]` placeholder.
  - _flush_citations: emits `turn.citation_registered` per Citation in seq
    order + persists via SessionStore.record_turn_citations (best-effort).

Mirrors `[[feedback_ut_must_assert_emit_contract]]` — bus mock asserts the
exact event sequence + payload shape, not just "something was emitted".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import (
    LeadRuntime,
    _flush_citations,
    _resolve_citations,
    _strip_data_markers,
)
from bowhead_core.citations import (
    TurnCitationRegistry,
    ensure_registry,
)

# ─── TurnCitationRegistry primitives ─────────────────────────────────────


def test_registry_register_returns_1_based_seq() -> None:
    """First register → seq=1; second different artifact → seq=2."""
    r = TurnCitationRegistry()
    assert r.register("art_one") == 1
    assert r.register("art_two") == 2
    assert r.size() == 2


def test_registry_register_same_artifact_dedups_seq() -> None:
    """Same artifact_id re-registered → same seq, no new entry."""
    r = TurnCitationRegistry()
    assert r.register("art_one", label="first") == 1
    assert r.register("art_one", label="ignored on dedup") == 1
    assert r.size() == 1


def test_registry_resolve_returns_citation_for_valid_seq() -> None:
    """resolve(N) → Citation when N in range; None when out of range."""
    r = TurnCitationRegistry()
    r.register("art_one", label="L1", kind="chart_section")
    cite = r.resolve(1)
    assert cite is not None
    assert cite.artifact_id == "art_one"
    assert cite.label == "L1"
    assert cite.kind == "chart_section"
    assert r.resolve(2) is None
    assert r.resolve(0) is None
    assert r.resolve(-1) is None


def test_registry_all_citations_in_seq_order() -> None:
    """all_citations() returns insertion order (== seq order)."""
    r = TurnCitationRegistry()
    r.register("art_three")  # seq=1
    r.register("art_one")  # seq=2
    r.register("art_two")  # seq=3
    out = r.all_citations()
    assert [c.seq for c in out] == [1, 2, 3]
    assert [c.artifact_id for c in out] == ["art_three", "art_one", "art_two"]


# ─── ensure_registry lazy-init ───────────────────────────────────────────


class _StubCtx:
    pass


def test_ensure_registry_lazy_inits_on_first_call() -> None:
    """No registry → fresh; second call → same instance."""
    ctx = _StubCtx()
    r1 = ensure_registry(ctx)
    assert isinstance(r1, TurnCitationRegistry)
    r2 = ensure_registry(ctx)
    assert r1 is r2


def test_ensure_registry_tolerates_frozen_ctx() -> None:
    """Frozen ctx (slots=True etc.) → returns transient registry, no raise."""

    @dataclass(frozen=True)
    class _Frozen:
        x: int = 0

    r = ensure_registry(_Frozen())
    assert isinstance(r, TurnCitationRegistry)


# ─── _resolve_citations (BE narrative finalize) ──────────────────────────


def test_resolve_citations_rewrites_data_art_marker() -> None:
    """`[data:art_xxx]` rewrites to `[cite:N]` + registers artifact."""
    ctx = _StubCtx()
    out = _resolve_citations("总盘 [data:art_abc123] 同比", ctx)
    assert out == "总盘 [cite:1] 同比"
    r = ensure_registry(ctx)
    assert r.size() == 1
    cite = r.resolve(1)
    assert cite is not None and cite.artifact_id == "art_abc123"


def test_resolve_citations_dedup_same_artifact_same_seq() -> None:
    """Two `[data:art_xxx]` references → both → `[cite:1]` (same seq)."""
    ctx = _StubCtx()
    out = _resolve_citations(
        "首段 [data:art_a1b2] 中间 [data:art_a1b2] 末尾",
        ctx,
    )
    assert out == "首段 [cite:1] 中间 [cite:1] 末尾"
    assert ensure_registry(ctx).size() == 1


def test_resolve_citations_orphan_cite_is_stripped() -> None:
    """dogfood F1 (2026-06-16): `[cite:N]` with N > registry size → STRIPPED
    (was an inert `[?]` placeholder). [cite:N] is the chat-only marker; in a
    chat-mode turn the registry is empty so every cite orphans, and users saw
    raw `[?]` leak ("40 家门店 [?]"). Consistent with the [data:N]-orphan strip
    above; the `narrative.orphan_cite` log keeps the debug signal."""
    ctx = _StubCtx()
    out = _resolve_citations("看 [cite:99] 不存在", ctx)
    assert "[?]" not in out
    assert "[cite:" not in out
    assert "看" in out and "不存在" in out


def test_resolve_citations_none_ctx_falls_to_legacy_strip() -> None:
    """parent_ctx=None path → legacy strip (`[data:art_*]` stripped)."""
    out = _resolve_citations("总盘 [data:art_abc123] 同比", None)
    # Legacy strip removes the marker entirely.
    assert "[data:art_" not in out
    assert "[cite:" not in out


def test_resolve_citations_empty_or_none_passthrough() -> None:
    assert _resolve_citations("", _StubCtx()) == ""
    assert _resolve_citations(None, _StubCtx()) is None  # type: ignore[arg-type]


def test_resolve_citations_handles_data_int_marker_as_cite_orphan() -> None:
    """0.39.3 P0 bug A (0.39.4 polish) — prod LLM (turn t_83cc7740c011) emitted
    `[data:1]` (integer N, not `art_HEX`). With empty registry, strip silently
    (NOT `[?]` — those orphans are LLM hallucinations with no backing data;
    better UX to drop than insert confusing marker mid-sentence)."""
    ctx = _StubCtx()
    out = _resolve_citations(
        "总盘 [data:1][data:2] 同比微增 [data:3]",
        ctx,
    )
    assert "[data:" not in out, f"raw [data:N] leaked: {out!r}"
    assert "[?]" not in out, f"unexpected orphan placeholder: {out!r}"
    assert "[cite:" not in out, f"unexpected cite marker: {out!r}"
    # Body text preserved
    assert "总盘" in out and "同比微增" in out


def test_resolve_citations_data_int_resolves_when_registry_has_entry() -> None:
    """0.39.3 P0 bug A — when registry has citation seq=1, `[data:1]` should
    resolve to `[cite:1]` (clickable chip), same as `[data:art_X]` rewrite."""
    ctx = _StubCtx()
    from bowhead_core.citations import ensure_registry

    r = ensure_registry(ctx)
    r.register("art_real_chart_aaa", label="2025 GMV", kind="chart_section")
    out = _resolve_citations("营业额 165 亿 [data:1] 数据出处", ctx)
    assert out == "营业额 165 亿 [cite:1] 数据出处"


def test_strip_data_markers_also_strips_data_int() -> None:
    """0.39.3 P0 bug A — legacy 1-arg strip path also handles `[data:N]`."""
    out = _strip_data_markers("总盘 [data:1] 同比 [data:99] 增长")
    assert "[data:" not in out


# ─── 0.40.5 Bug 11: `[ref:chunk_xxx]` strip (chat narrative leak) ────────


def test_resolve_citations_strips_ref_chunk_marker() -> None:
    """Bug 11 — `[ref:chunk_<hex>]` (KB chunk_id citation Lead picks up from
    kb_search tool nudge) MUST be stripped from chat narrative. Chat
    renderer doesn't parse [ref:...] (only canvas does)."""
    ctx = _StubCtx()
    out = _resolve_citations(
        "**高新技术企业证书**[ref:chunk_a30cafb175d2c30c88387342]",
        ctx,
    )
    assert "[ref:" not in out
    assert out == "**高新技术企业证书**"


def test_resolve_citations_strips_multiple_ref_chunk_markers() -> None:
    """Prod turn t_13b24e763c23 had 3 chunk_id markers in one narrative."""
    ctx = _StubCtx()
    text = "**资产评估机构**[ref:chunk_774d87e03d62055d339340fa][ref:chunk_963947198b48944496b76333]\n- 评估机构: 北京建华信"
    out = _resolve_citations(text, ctx)
    assert "[ref:" not in out
    assert "chunk_" not in out
    assert "**资产评估机构**" in out
    assert "评估机构: 北京建华信" in out


def test_resolve_citations_strips_ref_s_alias_marker() -> None:
    """kb_search legacy schema description mentioned `[ref:s_xxx]` form —
    also strip in case LLM emits that variant."""
    ctx = _StubCtx()
    out = _resolve_citations("文档说 [ref:s_001] 营业额上升", ctx)
    assert "[ref:" not in out
    assert "文档说" in out and "营业额上升" in out


def test_resolve_citations_strips_ref_int_alias() -> None:
    """compute.md ν1.5 form `[ref:3]` (simple int alias) — also strip in
    chat-narrative path (canvas peer uses different finalize)."""
    ctx = _StubCtx()
    out = _resolve_citations("详见 [ref:3] 文档", ctx)
    assert "[ref:" not in out


def test_resolve_citations_preserves_non_ref_bracket_text() -> None:
    """Don't over-match — keep user-visible bracketed text like `[备注]`
    or markdown link `[标题](url)`."""
    ctx = _StubCtx()
    text = "[备注] 这是说明 [文档链接](https://example.com)"
    out = _resolve_citations(text, ctx)
    assert out == text  # untouched


def test_strip_data_markers_legacy_path_also_strips_ref_chunk() -> None:
    """Belt + braces — legacy 1-arg strip (no ctx) also handles ref markers
    so any code path calling _strip_data_markers without ctx is also covered."""
    out = _strip_data_markers(
        "标题[ref:chunk_a30cafb175d2c30c88387342]内容",
    )
    assert out == "标题内容"


def test_resolve_citations_strip_ref_does_not_break_cite_rewrite() -> None:
    """Make sure the new ref-strip doesn't interfere with the [data:art_xxx]
    → [cite:N] rewrite that's the primary purpose of _resolve_citations."""
    ctx = _StubCtx()
    out = _resolve_citations(
        "盘子 [data:art_abc123] 和 KB 引用 [ref:chunk_aabbccdd11223344] 同时",
        ctx,
    )
    assert "[ref:" not in out
    assert "[cite:1]" in out  # data:art was registered + rewritten
    assert "[data:" not in out


# ─── _flush_citations (SSE emit + persist chokepoint) ────────────────────


class _FakeBus:
    """Capture all emit calls in order. Asserts (event_type, payload) tuples."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, ev: Any) -> None:
        # OrchestratorEvent dataclass shape: event_type + payload + turn_id
        self.events.append((ev.event_type, ev.payload))


@dataclass
class _FakeSessionStore:
    calls: list[tuple[str, list[tuple[int, str, str | None, str]]]] = field(
        default_factory=list,
    )

    async def record_turn_citations(
        self,
        *,
        turn_id: str,
        citations: list[tuple[int, str, str | None, str]],
    ) -> None:
        self.calls.append((turn_id, list(citations)))


@dataclass
class _FakeSctx:
    turn_id: str = "t_test"


def _make_runtime(parent_ctx: Any, bus: Any, store: Any) -> LeadRuntime:
    """Minimal LeadRuntime — only fields _flush_citations touches."""
    return LeadRuntime(
        router=None,  # type: ignore[arg-type]
        tool_registry=None,  # type: ignore[arg-type]
        parent_bus=bus,
        parent_ctx=parent_ctx,
        session_store=store,
    )


@pytest.mark.asyncio
async def test_flush_citations_emits_in_seq_order_and_persists() -> None:
    """Registry with 3 citations → 3 emit calls in seq order + 1 persist call."""
    ctx = _StubCtx()
    r = ensure_registry(ctx)
    r.register("art_aaa", label="L1", kind="chart_section")
    r.register("art_bbb", label=None, kind="data_ref")
    r.register("art_ccc", label="L3", kind="tool_output")

    bus = _FakeBus()
    store = _FakeSessionStore()
    runtime = _make_runtime(ctx, bus, store)
    sctx = _FakeSctx(turn_id="t_99")
    await _flush_citations(runtime, sctx)

    # 1) 3 events, all type turn.citation_registered, in seq order.
    # 0.39.3: payload uses `citation_seq` (NOT `seq`) — bus envelope's to_sse
    # clobbers `payload.seq` with the monotonic event counter.
    assert len(bus.events) == 3
    assert all(ev[0] == "turn.citation_registered" for ev in bus.events)
    seqs = [ev[1]["citation_seq"] for ev in bus.events]
    assert seqs == [1, 2, 3]
    # Payload shape per Citation
    assert bus.events[0][1] == {
        "citation_seq": 1,
        "artifact_id": "art_aaa",
        "label": "L1",
        "kind": "chart_section",
    }
    assert bus.events[1][1]["artifact_id"] == "art_bbb"
    assert bus.events[1][1]["label"] is None
    # 2) one persist call with all 3 tuples
    assert len(store.calls) == 1
    turn_id, rows = store.calls[0]
    assert turn_id == "t_99"
    assert rows == [
        (1, "art_aaa", "L1", "chart_section"),
        (2, "art_bbb", None, "data_ref"),
        (3, "art_ccc", "L3", "tool_output"),
    ]


@pytest.mark.asyncio
async def test_flush_citations_empty_registry_noop() -> None:
    """No citations registered → no emits, no persist."""
    ctx = _StubCtx()
    ensure_registry(ctx)  # lazy-init but no register calls
    bus = _FakeBus()
    store = _FakeSessionStore()
    runtime = _make_runtime(ctx, bus, store)
    await _flush_citations(runtime, _FakeSctx())
    assert bus.events == []
    assert store.calls == []


@pytest.mark.asyncio
async def test_flush_citations_missing_registry_noop() -> None:
    """parent_ctx has no registry attached → no-op (not raise)."""
    ctx = _StubCtx()  # no ensure_registry call
    bus = _FakeBus()
    runtime = _make_runtime(ctx, bus, None)
    await _flush_citations(runtime, _FakeSctx())
    assert bus.events == []


@pytest.mark.asyncio
async def test_flush_citations_no_session_store_still_emits() -> None:
    """session_store=None → SSE still fires, persist silently skipped."""
    ctx = _StubCtx()
    ensure_registry(ctx).register("art_x")
    bus = _FakeBus()
    runtime = _make_runtime(ctx, bus, None)
    await _flush_citations(runtime, _FakeSctx())
    assert len(bus.events) == 1


@pytest.mark.asyncio
async def test_flush_citations_persist_failure_swallowed() -> None:
    """SessionStore persist raises → log+continue, SSE still fired."""
    ctx = _StubCtx()
    ensure_registry(ctx).register("art_x")

    class _BadStore:
        async def record_turn_citations(self, *, turn_id: str, citations: Any) -> None:
            raise RuntimeError("db down")

    bus = _FakeBus()
    runtime = _make_runtime(ctx, bus, _BadStore())
    # Should NOT raise
    await _flush_citations(runtime, _FakeSctx())
    assert len(bus.events) == 1


@pytest.mark.asyncio
async def test_flush_citations_order_invariant_emit_before_final_answer() -> None:
    """Emit ordering invariant: each citation event arrives via single async
    sequence. SSE delivery is in-order, so FE sees citations[] populated
    before final_answer renders [cite:N] markers — this asserts the helper
    awaits each emit serially (no gather), preserving the ordering."""
    ctx = _StubCtx()
    r = ensure_registry(ctx)
    r.register("art_1")
    r.register("art_2")

    order: list[str] = []

    class _OrderBus:
        async def emit(self, ev: Any) -> None:
            # Yield control mid-await to expose any parallel-fire race
            await asyncio.sleep(0)
            order.append(ev.payload["artifact_id"])

    runtime = _make_runtime(ctx, _OrderBus(), None)
    await _flush_citations(runtime, _FakeSctx())
    assert order == ["art_1", "art_2"]


# ── citation-audit M1-5: fix A1 in practice (strip_legacy at the chat persistence boundary) ─────────


def test_strip_data_markers_bakes_real_gen2_markers() -> None:
    """A1：旧 `_NUM_VU_MARKER_RE` 是字面量 V，真实 gen2 标记剥不掉、落库带
    raw marker。现在 chat persist 收口 bake（单位保留，不是删除数据）。"""
    out = _strip_data_markers("同比增长[num:4.18:%:src:tl_e5a0]，规模[num:5.2:亿元]。")
    assert out == "同比增长4.18%，规模5.2亿元。"
    assert "[num:" not in out


def test_strip_data_markers_covers_fact_bi_memory_families() -> None:
    """§4.6：`[fact:]/[bi:]/[memory:]`（δ provenance 族）此前 chat 面零处理
    ——Bug 11 同款 literal 泄露面，随 strip_legacy 一并闭合。"""
    out = _strip_data_markers("依据 [fact:q3_rev] 与 [bi:s_9] 及 [memory:m_1] 判断")
    assert out == "依据 与 及 判断"


def test_resolve_citations_bakes_gen2_keeps_valid_cite() -> None:
    ctx = _StubCtx()
    reg = ensure_registry(ctx)
    reg.register("art_abc123", label="总盘")
    out = _resolve_citations("总盘 [cite:1] 同比 [num:4.18:%:src:tl_x] 提升", ctx)
    assert "[cite:1]" in out  # Keep valid cite (frontend chip parsing).
    assert "[num:" not in out
    assert "4.18%" in out  # Baking preserves data rather than deleting it.


@pytest.mark.asyncio
async def test_flush_citations_high_water_mark_makes_second_flush_incremental() -> None:
    """F3 残差收口：done 咽喉 drain 与 per-branch flush 双跑——高水位保证第二次
    只发增量（无新引用=零事件），线上不再整表重发。"""
    ctx = _StubCtx()
    r = ensure_registry(ctx)
    r.register("art_aaa", label="L1", kind="data_ref")
    bus = _FakeBus()
    store = _FakeSessionStore()
    runtime = _make_runtime(ctx, bus, store)

    await _flush_citations(runtime, _FakeSctx())
    assert len(bus.events) == 1
    await _flush_citations(runtime, _FakeSctx())
    assert len(bus.events) == 1  # No new citation ⇒ no re-emission
    r.register("art_bbb", label="L2", kind="chart_section")
    await _flush_citations(runtime, _FakeSctx())
    assert len(bus.events) == 2  # Emit only the new entry.
    assert bus.events[-1][1]["citation_seq"] == 2


@pytest.mark.asyncio
async def test_bus_pre_terminal_drain_flushes_before_done() -> None:
    """F3 残差收口：任何分支忘了 flush，done 咽喉点冻结前 drain 兜底——
    citation 事件必须先于 done 进 ring，且 exactly-once（第二个 done 不重跑）。"""
    from bowhead_core.events import OrchestratorEvent, TurnEventBus

    bus = TurnEventBus()

    async def drain() -> None:
        await bus.emit(
            OrchestratorEvent(
                event_type="turn.citation_registered",
                turn_id="t_drain",
                payload={"citation_seq": 1, "artifact_id": "art_x", "label": None, "kind": "data_ref"},
            )
        )

    bus.add_pre_terminal_drain(drain)
    assert await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_drain", payload={}))
    types = [ev.event_type for ev in bus._events]
    assert types[-2:] == ["turn.citation_registered", "done"]
    # Second done: frozen bus rejects it; drain does not rerun.
    assert not await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_drain", payload={}))
    assert [ev.event_type for ev in bus._events][-2:] == ["turn.citation_registered", "done"]


@pytest.mark.asyncio
async def test_bus_pre_terminal_drain_error_never_blocks_terminal() -> None:
    from bowhead_core.events import OrchestratorEvent, TurnEventBus

    bus = TurnEventBus()

    async def bad_drain() -> None:
        raise RuntimeError("boom")

    bus.add_pre_terminal_drain(bad_drain)
    assert await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_drain2", payload={}))
    assert [ev.event_type for ev in bus._events] == ["done"]


@pytest.mark.asyncio
async def test_flush_hwm_not_advanced_on_persist_failure_so_drain_retries() -> None:
    """review F2：水位只在 emit+persist 成功后推进——瞬时 persist 失败不得把
    pending 段永久跳过（done 咽喉 drain 的重试就靠这个）。"""

    class _FlakyStore:
        def __init__(self) -> None:
            self.fail_next = True
            self.calls: list[Any] = []

        async def record_turn_citations(self, *, turn_id: str, citations: Any) -> None:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("sqlite busy")
            self.calls.append((turn_id, list(citations)))

    ctx = _StubCtx()
    r = ensure_registry(ctx)
    r.register("art_aaa", label="L1", kind="data_ref")
    bus = _FakeBus()
    store = _FlakyStore()
    runtime = _make_runtime(ctx, bus, store)

    await _flush_citations(runtime, _FakeSctx())  # Persistence fails ⇒ watermark unchanged
    assert store.calls == []
    await _flush_citations(runtime, _FakeSctx())  # Drain retry ⇒ successful backfill
    assert len(store.calls) == 1
    assert store.calls[0][1][0][1] == "art_aaa"
    # Watermark advances after success: the third call does not re-emit.
    n_events = len(bus.events)
    await _flush_citations(runtime, _FakeSctx())
    assert len(bus.events) == n_events


@pytest.mark.asyncio
async def test_bus_pre_terminal_drain_cancellation_freezes_terminal() -> None:
    """review F5：drain 里逃逸的 CancelledError 必须先冻结终态再抛——否则
    _terminal_in_progress 卡 True，bus 永远发不出终态帧。"""
    import asyncio as _asyncio

    from bowhead_core.events import OrchestratorEvent, TurnEventBus

    bus = TurnEventBus()

    async def cancelled_drain() -> None:
        raise _asyncio.CancelledError()

    bus.add_pre_terminal_drain(cancelled_drain)
    with pytest.raises(_asyncio.CancelledError):
        await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_c", payload={}))
    assert bus._terminal_frozen is True
    # Frozen bus cleanly rejects done (returns False), without hanging at the occupied gate.
    assert not await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_c", payload={}))
