"""Sprint ρ Phase A — G classifier v2 + dispatcher + counters + count_sections_for_turn.

Per tech-design r2 §1 + r1 §1.6: 6 UTs covering pure scalar helper,
flag-off byte-for-byte v1 parity, single-turn 1+4 consistency invariant,
canvas_store helper, counter wiring (mocked prometheus), and signature
scalar-only enforcement.

Per `feedback_ut_must_assert_emit_contract`: UT 3 mocks the dispatch
chokepoint to assert callsite 1 + 4 produce the same response_kind
within one turn (the PRD r2 invariant). No FakeBus needed here because
the helpers are pure-scalar; emit contract is exercised at integration
level in the dogfood phase, not at unit boundary.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from bowhead_core.agents.lead_orchestrator import (
    _CANVAS_WRITE_PREFIXES,
    _CHAT_ONLY_PEER_PROFILES,
    _compute_response_kind,
    _compute_response_kind_dispatch,
    _compute_response_kind_v2,
)

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _MockInv:
    tool_name: str
    arguments: dict[str, Any]


# ─── UT 1: 10-case truth table for v2 ────────────────────────────────────────


def test_response_kind_v2_10_cases() -> None:
    """Exhaustive truth table per tech-design r1 §1.6 / r2 §1.

    Each tuple = (label, invocations, count, prior_status, expected).
    """
    cases: list[tuple[str, list[Any] | None, int, str | None, str]] = [
        # 1. empty invocations → chat (rule 4)
        ("empty", [], 0, None, "chat"),
        ("none", None, 0, None, "chat"),
        # 2. bi.query only (read-only) → chat (rule 4 doesn't fire; rule 5 only spawn)
        ("bi-only", [_MockInv("bi_query", {})], 0, None, "chat"),
        # 3. add_chart → canvas (rule 2)
        ("canvas-add-chart", [_MockInv("add_chart", {})], 0, None, "canvas"),
        # 4. canvas_add_kpi_row → canvas
        ("canvas-add-kpi", [_MockInv("canvas_add_kpi_row", {})], 0, None, "canvas"),
        # 5/6. delivery-surface C2 (2026-08-13): spawn no longer decides the delivery surface;
        #    spawn is an execution decision (work needs tools Lead lacks), while reporting is a delivery decision.
        #    Neither causes the other (116: spawn⇒canvas turned a simple lookup into an empty report;
        #    discovery §3.2). A spawn turn with no canvas writes → chat; actual reports are covered by
        #    rules 1/2 (materialized writes) and terminal _resolve_terminal_response_kind.
        (
            "spawn-kb",
            [_MockInv("subagent_spawn", {"profile": "kb-analyst"})],
            0,
            None,
            "chat",
        ),
        (
            "spawn-compute",
            [_MockInv("subagent_spawn", {"profile": "compute"})],
            0,
            None,
            "chat",
        ),
        # 7. spawn(compute) WITH canvas writes via section count > 0 → canvas (rule 1)
        (
            "spawn-compute+count",
            [_MockInv("subagent_spawn", {"profile": "compute"})],
            3,
            None,
            "canvas",
        ),
        # 8. prior=clarification_pending, no canvas → chat (rule 3, HITL chain)
        ("hitl-chain", [_MockInv("bi_query", {})], 0, "clarification_pending", "chat"),
        # 9. prior=clarification_pending + section count > 0 → canvas (rule 1 wins)
        (
            "hitl-with-canvas",
            [_MockInv("bi_query", {})],
            1,
            "clarification_pending",
            "canvas",
        ),
        # 10. composer-only path: no invocations, count > 0 → canvas (rule 1)
        ("composer-only", [], 5, None, "canvas"),
        # 11/12. delivery-surface C2: remove old rule 5.5 (≥2 BI calls / ≥2 subqueries in a batch
        #    → canvas). The compute prompt already mandates batching for multiple metrics,
        #    so this rule meant any question with ≥2 metrics must produce a report. Regression anchor:
        #    multiple BI calls/large batches remain chat unless they actually write canvas.
        (
            "multi-bi-calls",
            [_MockInv("bi_query", {}), _MockInv("bi_query", {})],
            0,
            None,
            "chat",
        ),
        (
            "bi-batch-5-queries",
            [_MockInv("bi_query", {"queries": ["q1", "q2", "q3", "q4", "q5"]})],
            0,
            None,
            "chat",
        ),
    ]
    for label, invs, count, prior, expected in cases:
        got = _compute_response_kind_v2(
            invs,
            canvas_section_count_for_turn=count,
            prior_turn_status=prior,
        )
        assert got == expected, f"{label}: expected {expected}, got {got}"


# ─── UT 2: signature is pure scalar (no store / ctx / bus) ───────────────────


def test_response_kind_signature_scalar_only() -> None:
    """Helper signature MUST be pure scalar — no store/ctx/bus refs.

    Defensive invariant: keeps the helper unit-testable in isolation and
    cheap to call (<50ns target). If anyone smuggles a runtime dependency
    in, this test fails immediately.
    """
    sig = inspect.signature(_compute_response_kind_v2)
    forbidden = {"store", "ctx", "bus", "runtime", "registry", "router"}
    for name, _param in sig.parameters.items():
        lower = name.lower()
        for bad in forbidden:
            assert bad not in lower, f"forbidden token {bad!r} in param {name}"

    # Annotations must be scalar (int / str / None / list[Any]).
    # Build an annotations map and check expected leaf types.
    params = sig.parameters
    assert "invocations" in params
    assert "canvas_section_count_for_turn" in params
    assert "prior_turn_status" in params
    # canvas_section_count_for_turn must be int (no Object/store-bound types).
    # `from __future__ import annotations` makes annotations strings, so
    # compare by name (covers both 3.x runtimes).
    count_annot = params["canvas_section_count_for_turn"].annotation
    annot_str = count_annot if isinstance(count_annot, str) else getattr(count_annot, "__name__", repr(count_annot))
    assert annot_str == "int", f"count annot expected int, got {annot_str!r}"


# ─── UT 3: single-turn 1+4 consistency (PRD r2 invariant) ────────────────────


def test_g_classifier_single_turn_consistency() -> None:
    """Callsites 1 (pre-flush, count=0) + 4 (post-flush, count from store)
    must produce the SAME response_kind within a single turn when canvas
    writes happened — the rule 2 invocation match catches it pre-flush,
    while rule 1 section count catches it post-flush.

    Scenario: peer dispatches compute that emits add_chart × 1.

    Pre-flush (callsite 1/2/3): invocations carry the add_chart
    tool_name AND subagent_spawn(compute) → rule 2 → canvas.

    Post-flush (callsite 4): canvas_store.count_sections_for_turn returns
    1 → rule 1 → canvas. Same answer.

    Failure mode the invariant guards against: someone re-orders rules so
    that pre-flush (count=0, no canvas tool name) goes "chat" and
    post-flush (count=1) goes "canvas" — that would mean the bail emit
    says "chat" but the success emit says "canvas" for the same turn.
    """
    invs = [
        _MockInv("subagent_spawn", {"profile": "compute"}),
        _MockInv("add_chart", {"x": "y"}),
    ]
    # Callsite 1: pre-flush, proxy 0.
    rk_callsite_1 = _compute_response_kind_v2(
        invs,
        canvas_section_count_for_turn=0,
        prior_turn_status=None,
    )
    # Callsite 4: post-flush, store returned 1.
    rk_callsite_4 = _compute_response_kind_v2(
        invs,
        canvas_section_count_for_turn=1,
        prior_turn_status=None,
    )
    assert rk_callsite_1 == "canvas"
    assert rk_callsite_4 == "canvas"
    assert rk_callsite_1 == rk_callsite_4

    # Composer-only path edge case: empty invocations, count=1.
    # Pre-flush would say "chat" (rule 4 — no invs), post-flush says
    # "canvas" (rule 1). This is the documented "callsites 1+4 may
    # differ for composer-only turns" exception per tech-design r2 §0
    # P0-1: rule 1 only fires authoritative at callsite 4. The
    # invariant is that callsite 4's answer is final.
    rk_composer_pre = _compute_response_kind_v2(
        [],
        canvas_section_count_for_turn=0,
        prior_turn_status=None,
    )
    rk_composer_post = _compute_response_kind_v2(
        [],
        canvas_section_count_for_turn=1,
        prior_turn_status=None,
    )
    assert rk_composer_pre == "chat"
    assert rk_composer_post == "canvas"


# ─── UT 4: canvas_store.count_sections_for_turn ──────────────────────────────


@pytest.mark.asyncio
async def test_canvas_store_count_sections_for_turn(tmp_path: Path) -> None:
    """3 appends under turn_A + 2 under turn_B → counts 3, 2.

    Also asserts: unknown turn → 0; empty turn_id → 0.
    """
    store = CanvasStore(tmp_path / "rho.db")
    await store.initialize()
    canvas = await store.ensure_canvas(
        session_id="s_rho",
        workspace_id="w_rho",
        user_id="u_rho",
    )
    # 3 appends tied to turn_A
    for i in range(3):
        await store.append_section(
            canvas_id=canvas.id,
            kind="chart",
            payload={"i": i},
            by_turn_id="turn_A",
        )
    # 2 appends tied to turn_B
    for i in range(2):
        await store.append_section(
            canvas_id=canvas.id,
            kind="kpi_row",
            payload={"i": i},
            by_turn_id="turn_B",
        )
    # 1 append with no by_turn_id (should NOT be counted under any turn)
    await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"text": "orphan"},
    )

    assert await store.count_sections_for_turn("turn_A") == 3
    assert await store.count_sections_for_turn("turn_B") == 2
    assert await store.count_sections_for_turn("turn_missing") == 0
    assert await store.count_sections_for_turn("") == 0

    # Patches should NOT count as new sections (the helper counts only
    # `op='append'` revisions). Patch one of turn_A's sections under turn_B.
    sections = await store.list_sections(canvas.id)
    target = sections[0]  # one of turn_A's
    await store.patch_section(
        section_id=target.id,
        patch={"patched": True},
        by_turn_id="turn_B",
    )
    # turn_B's append count is still 2 (the patch is op='patch', not 'append').
    assert await store.count_sections_for_turn("turn_B") == 2

    # P0-1 (CTO review): assert the count_sections_for_turn query uses the
    # new `idx_revisions_turn` index — without it this becomes a full scan on
    # every post-flush success chokepoint. EXPLAIN QUERY PLAN's rendering
    # gives us "USING INDEX idx_revisions_turn"; we assert presence loosely
    # so a future SQLite version reformat doesn't break us.
    import aiosqlite as _aiosqlite  # local: avoid polluting module-level imports

    async with (
        _aiosqlite.connect(store._path) as db,
        db.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM canvas_revisions WHERE by_turn_id = ? AND op = 'append'",
            ("turn_A",),
        ) as cur,
    ):
        plan_rows = await cur.fetchall()
    plan_text = " | ".join(str(r) for r in plan_rows)
    assert "idx_revisions_turn" in plan_text, f"count_sections_for_turn must use idx_revisions_turn index; plan was: {plan_text}"


# ─── UT 6: flag-off byte-for-byte v1 parity ──────────────────────────────────


def test_response_kind_dispatch_flag_off_byte_for_byte_v1() -> None:
    """Flag OFF → dispatch returns EXACTLY v1's result for the same inputs,
    ignoring v2-only kwargs (canvas count + prior status). Crucial for
    rollback parity (BOWHEAD_RHO_G_NEW_CLASSIFIER=0).
    """
    cases: list[list[Any]] = [
        [],
        [_MockInv("bi_query", {})],
        [_MockInv("add_chart", {})],
        [_MockInv("subagent_spawn", {"profile": "compute"})],
        [_MockInv("subagent_spawn", {"profile": "kb-analyst"})],
        [
            _MockInv("subagent_spawn", {"profile": "compute"}),
            _MockInv("add_chart", {}),
        ],
    ]
    with patch.dict(os.environ, {"BOWHEAD_RHO_G_NEW_CLASSIFIER": "0"}):
        for invs in cases:
            v1 = _compute_response_kind(invs)
            # Even passing v2-only kwargs that WOULD flip the answer in
            # v2 (count > 0, HITL prior), flag OFF should ignore them.
            dispatched = _compute_response_kind_dispatch(
                invs,
                canvas_section_count_for_turn=99,
                prior_turn_status="clarification_pending",
            )
            assert dispatched == v1, f"flag-off parity violated: invs={invs!r} v1={v1!r} dispatched={dispatched!r}"


# ─── helper exposure sanity ─────────────────────────────────────────────────


def test_canvas_write_prefixes_and_chat_only_constants_unchanged() -> None:
    """v2 reuses the same constants v1 uses; if someone forks them ρ
    classification will drift from ν2 reality. Snapshot here so we notice.
    """
    assert "add_chart" in _CANVAS_WRITE_PREFIXES
    assert "canvas_add_kpi_row" in _CANVAS_WRITE_PREFIXES
    # fleet retrofit (2026-06-01): kb-analyst upgraded to a research analyst that
    # returns report-level AnalysisPayload → canvas-capable, removed from chat-only.
    assert "kb-analyst" not in _CHAT_ONLY_PEER_PROFILES
    assert "kb_analyst" not in _CHAT_ONLY_PEER_PROFILES
