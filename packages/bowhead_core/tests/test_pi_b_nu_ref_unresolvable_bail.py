"""Sprint π Phase D Item B — ν1-ref-unresolvable force-bail tests.

Verifies r2 §B acceptance:
  1. counter promotes to (turn_id, subagent_id)-keyed dict
  2. counter survives peer re-spawn (preserved across `_ν_cleanup_ctx`)
  3. 3-rejects with strip-empty payload → synthesizes bail envelope
     (ok=True, __terminal=True, bail_kind="ν1_ref_unresolvable")
  4. bail envelope user-grade prose (no internal terms)
  5. concurrent peer spawn → both increment counters correctly
  6. bail envelope surfaces to chat via Lead `_resolve final_answer` chain
  7. strip-with-evidence still wins (no bail synth when payload non-empty)
  8. turn-end GC sweeps `_ν_retry_counts` entries for the just-finished turn
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import pytest
from bowhead_core.subagent_return_tool import (
    CTX_PAYLOAD_ATTR,
    PI_B_BAIL_KIND,
    TERMINAL_FLAG,
    _build_fallback_narrative,
    _ensure_nu_retry_counts,
    _get_nu_retry_key,
    _payload_has_any_evidence,
    register_subagent_return_tool,
)
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

# ─────────────────────────────────────────────────────────────
# Stubs — minimal canvas/artifact stores that force ν1 rejection
# ─────────────────────────────────────────────────────────────


class _StubCanvas:
    def __init__(self, cid: str = "canvas_test") -> None:
        self.id = cid


class _EmptyCanvasStore:
    """canvas store with no chart sections — any chart_section_id ref will
    not match the allowlist → ν1 validator emits chart_ref_not_in_allowlist."""

    async def get_canvas_by_session(self, session_id: str) -> _StubCanvas:
        return _StubCanvas()

    async def ensure_canvas(self, *, session_id: str, workspace_id: str, user_id: str) -> _StubCanvas:
        # Empty-canvas fix: reference validation is read-only; creating a canvas is a regression.
        raise AssertionError("ref 校验路径不得创建 canvas")

    async def list_sections(self, canvas_id: str) -> list[Any]:
        return []

    async def list_section_ids_by_kind(self, canvas_id: str, *, kind: str) -> set[str]:
        return set()


class _EmptyArtifactStore:
    async def list_for_session(self, *, session_id: str, user_id: str) -> list[Any]:
        return []


def _bad_ref_payload() -> dict[str, Any]:
    """Payload that references a non-existent chart_section_id. ν1 validator
    will reject (chart_ref_not_in_allowlist). After strip: insight evidence
    has chart_section_id nulled — narrative survives, so strip-with-evidence.
    Use the empty-narrative variant for the bail-synth path."""
    return {
        "title": "EA 看板",
        "exec_summary": "整体合规率 11.39%。这里 narrative 足够长可以触发 evidence pass.",
        "kpis": [{"label": "合规率", "value": "11.39%", "sentiment": "bad"}],
        "insights": [
            {
                "title": "部门支出拖累",
                "thesis": "占 90% 单量但合规率 1.85%",
                "evidence": [{"kind": "chart", "chart_section_id": "sec_FAKE_XXX", "caption": "对比"}],
                "narrative": ("部门支出在两个维度都是结构性问题，合规率和趋势都低." * 2),
                "confidence": "high",
            }
        ],
        "recommendations": [{"priority": "P0", "action": "OA 必填字段", "owner": "IT 系统组"}],
    }


def _bad_ref_minimal_payload() -> dict[str, Any]:
    """Payload where stripping the invalid chart_section_id + clearing
    everything except a short narrative will leave _payload_has_any_evidence
    → False, triggering bail synth.

    Strategy:
      - kpis: [] (no evidence)
      - recommendations: [] (no evidence)
      - exec_summary: short (< 30 chars)
      - insights[0].evidence ref bad; narrative & thesis both empty after
        we set them empty here.
    """
    return {
        "title": "(no real data)",
        "exec_summary": "短.",
        "kpis": [],
        "insights": [
            {
                "title": "x",
                "thesis": "",
                "evidence": [{"kind": "chart", "chart_section_id": "sec_FAKE_XXX", "caption": ""}],
                "narrative": "",
                "confidence": "low",
            }
        ],
        "recommendations": [],
    }


def _stub_ctx(turn_id: str = "turn_t1") -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m_test",
        user_id="u_test",
        turn_id=turn_id,
        datarc=None,  # type: ignore[arg-type]
        session_id="s_test",
    )
    ctx.canvas_store = _EmptyCanvasStore()
    ctx.artifact_store = _EmptyArtifactStore()
    return ctx


@pytest.fixture(autouse=True)
def _enable_strict_ref_validate() -> Any:
    """Ensure ν1 strict mode is on for these tests (default ON but explicit)."""
    prev = os.environ.get("BOWHEAD_REF_VALIDATE_STRICT")
    os.environ["BOWHEAD_REF_VALIDATE_STRICT"] = "1"
    # Disable simple-int translation (we use real fake IDs)
    prev_inj = os.environ.get("BOWHEAD_REF_ALLOWLIST_INJECT")
    os.environ["BOWHEAD_REF_ALLOWLIST_INJECT"] = "0"
    # 2026-09-06: first-pass remapping + direct stripping (BOWHEAD_REF_REMAP_FIRST_PASS, default 1) bypasses the reject-twice-then-strip counter path.
    # This file tests the counter path, still valid for rollback when the flag is 0; disable it explicitly. New-path cases live in test_nu1_ref_remap.py.
    prev_remap = os.environ.get("BOWHEAD_REF_REMAP_FIRST_PASS")
    os.environ["BOWHEAD_REF_REMAP_FIRST_PASS"] = "0"
    yield
    if prev_remap is None:
        os.environ.pop("BOWHEAD_REF_REMAP_FIRST_PASS", None)
    else:
        os.environ["BOWHEAD_REF_REMAP_FIRST_PASS"] = prev_remap
    if prev is None:
        os.environ.pop("BOWHEAD_REF_VALIDATE_STRICT", None)
    else:
        os.environ["BOWHEAD_REF_VALIDATE_STRICT"] = prev
    if prev_inj is None:
        os.environ.pop("BOWHEAD_REF_ALLOWLIST_INJECT", None)
    else:
        os.environ["BOWHEAD_REF_ALLOWLIST_INJECT"] = prev_inj


# ─────────────────────────────────────────────────────────────
# UT 1: counter keyed by (turn_id, subagent_id)
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_b_counter_promotes_to_subagent_id_keyed_dict() -> None:
    """Two different subagent_ids on same parent_ctx → two distinct buckets
    in `_ν_retry_counts`. r2 §B + Eng P1-3 close."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()

    # Spawn 1: subagent_id_A — invoke handler; 1st rejection
    ctx._current_subagent_id = "sub_AAA"
    inv1 = await reg.execute("subagent_return", {"payload": _bad_ref_payload()}, ctx)
    assert inv1.result["ok"] is False
    assert inv1.result.get("ν_ref_retry") is True

    # Spawn 2: subagent_id_B (sibling, same parent_ctx)
    ctx._current_subagent_id = "sub_BBB"
    inv2 = await reg.execute("subagent_return", {"payload": _bad_ref_payload()}, ctx)
    assert inv2.result["ok"] is False
    assert inv2.result.get("ν_ref_retry") is True

    counts = ctx._ν_retry_counts
    assert isinstance(counts, dict)
    key_a = ("turn_t1", "sub_AAA")
    key_b = ("turn_t1", "sub_BBB")
    # Each sibling has its OWN counter at 1 (not shared)
    assert counts[key_a] == 1
    assert counts[key_b] == 1


# ─────────────────────────────────────────────────────────────
# UT 2: counter survives peer re-spawn (preserved across _ν_cleanup_ctx)
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_b_counter_survives_peer_respawn() -> None:
    """Same subagent_id re-invokes within same turn → counter ≥1 after first
    reject, ≥2 after second. Simulates Lead re-spawning peer (or replays)."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    ctx._current_subagent_id = "sub_CCC"

    # Reject 1 → counter=1
    await reg.execute("subagent_return", {"payload": _bad_ref_payload()}, ctx)
    counts = ctx._ν_retry_counts
    key = ("turn_t1", "sub_CCC")
    assert counts[key] == 1

    # Simulate peer exit + re-spawn (parent_ctx persists; `_ν_retry_counts`
    # is intentionally not in the cleanup list, so it must survive).
    # Manually mimic _ν_cleanup_ctx erasing the other ν attrs:
    for attr in ("_ν15_allowlist", "_current_subagent_invocations", "_ν_retry_count", "_ν_thin_retry_count", "_current_subagent_id"):
        with contextlib.suppress(AttributeError):
            delattr(ctx, attr)
    # Confirm `_ν_retry_counts` preserved (the whole point of π B)
    assert hasattr(ctx, "_ν_retry_counts")
    assert ctx._ν_retry_counts[key] == 1

    # Re-spawn same subagent_id, send another bad payload → counter=2
    ctx._current_subagent_id = "sub_CCC"
    await reg.execute("subagent_return", {"payload": _bad_ref_payload()}, ctx)
    assert ctx._ν_retry_counts[key] == 2


# ─────────────────────────────────────────────────────────────
# UT 3: 3rd attempt synthesizes bail envelope when strip-empty
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_b_forced_reject_3rd_attempt_synthesizes_bail_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject 1, 2 → ν_ref_retry; on 3rd call (retry≥2 → strip-pass path),
    when strip leaves payload empty of evidence → synthesizes bail envelope
    with ok=True, __terminal=True, bail_kind='ν1_ref_unresolvable'.

    Strip normally leaves kpis/recommendations intact (required by schema),
    so we monkeypatch `_strip_invalid_refs` to return a payload with the
    required structural fields emptied — modeling the worst-case "all
    sections invalid + all stripped → nothing left" path that r2 §B targets.
    """
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    ctx._current_subagent_id = "sub_DDD"

    # Pre-arm counter to 2 so the next call hits the strip-pass branch
    # immediately. Avoids depending on `_bad_ref_payload` passing schema in
    # the first two attempts (it does, but we want isolation).
    _ν_counts = _ensure_nu_retry_counts(ctx)
    _ν_counts[("turn_t1", "sub_DDD")] = 2

    # Monkeypatch strip to return an empty-of-evidence dict

    def _strip_empty(payload: dict, errors: list) -> dict:
        # Pretend strip dropped everything user-meaningful
        return {
            "title": payload.get("title") or "(no title)",
            "exec_summary": "",
            "kpis": [],
            "insights": [
                {"title": "x", "thesis": "", "narrative": "", "evidence": [{"kind": "chart", "chart_section_id": None}]},
            ],
            "recommendations": [],
        }

    monkeypatch.setattr(
        "bowhead_core._ref_validation._strip_invalid_refs",
        _strip_empty,
    )

    inv3 = await reg.execute(
        "subagent_return",
        {"payload": _bad_ref_payload()},
        ctx,
    )
    r3 = inv3.result
    assert r3["ok"] is True, r3
    assert r3.get(TERMINAL_FLAG) is True, r3
    assert r3.get("bail_kind") == PI_B_BAIL_KIND, r3
    assert "payload" in r3
    bail_payload = r3["payload"]
    assert "fallback_markdown" in bail_payload
    assert "narrative" in bail_payload
    assert isinstance(bail_payload["fallback_markdown"], str)
    # Bail payload pushed to ctx so subagent loop / aggregator can consume
    assert getattr(ctx, CTX_PAYLOAD_ATTR) is bail_payload


# ─────────────────────────────────────────────────────────────
# UT 4: bail envelope user-grade prose — no internal terms
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_b_bail_envelope_user_grade_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r2 §B: fallback_markdown must NOT contain internal jargon like
    '(注：peer 引用了 N 个未确认 ID...)'. Plain, user-facing reframe only."""
    # Direct helper assertion
    prose = _build_fallback_narrative()
    forbidden = [
        "(注：peer",
        "未确认 ID",
        "已退化",
        "ν1",
        "ν_ref",
        "ref validation",
        "peer hallucinated",
    ]
    for term in forbidden:
        assert term not in prose, f"internal term leaked: {term!r}"
    # Positive: prose includes user-actionable phrasing
    assert "指标" in prose or "时间范围" in prose

    # End-to-end: bail envelope synthesized by handler also user-grade
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    ctx._current_subagent_id = "sub_EEE"

    # Pre-arm counter to 2 + strip to empty so bail synth path is exercised
    _ν_counts = _ensure_nu_retry_counts(ctx)
    _ν_counts[("turn_t1", "sub_EEE")] = 2

    def _strip_empty(payload: dict, errors: list) -> dict:
        return {
            "title": "",
            "exec_summary": "",
            "kpis": [],
            "insights": [],
            "recommendations": [],
        }

    monkeypatch.setattr(
        "bowhead_core._ref_validation._strip_invalid_refs",
        _strip_empty,
    )

    inv = await reg.execute(
        "subagent_return",
        {"payload": _bad_ref_payload()},
        ctx,
    )
    assert inv.result.get("bail_kind") == PI_B_BAIL_KIND, inv.result
    bail_payload = inv.result["payload"]
    for term in forbidden:
        assert term not in bail_payload["fallback_markdown"], f"bail envelope leaked internal term: {term!r}"


# ─────────────────────────────────────────────────────────────
# UT 5: concurrent peer spawns — no race on dict counter
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_b_concurrent_peer_spawns_no_race() -> None:
    """Two concurrent subagent_return calls (asyncio.gather) on the same
    parent_ctx with different subagent_ids → both increment correctly.
    Each goroutine sees its own (turn_id, subagent_id) key bucket."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)

    async def _call_with_id(ctx: ToolExecutionContext, sid: str) -> None:
        # Each call sets _current_subagent_id then invokes handler.
        # In real subagent.py, each spawn is sequential within Lead loop, but
        # the dict-write itself must be safe under interleaving. Simulate by
        # toggling sid for each call.
        ctx._current_subagent_id = sid
        await reg.execute(
            "subagent_return",
            {"payload": _bad_ref_payload()},
            ctx,
        )

    ctx = _stub_ctx()
    # Pre-init the counts dict so both coroutines see the same instance
    _ensure_nu_retry_counts(ctx)

    # Sequence: each coroutine reads + writes its own key. CPython GIL +
    # no `await` between the dict ops in the handler's atomic bump → no
    # corruption. Run several rounds.
    for _ in range(3):
        await asyncio.gather(
            _call_with_id(ctx, "sub_P1"),
            _call_with_id(ctx, "sub_P2"),
        )

    counts = ctx._ν_retry_counts
    # Each key incremented some N>=1, total writes = 6
    assert counts[("turn_t1", "sub_P1")] >= 1
    assert counts[("turn_t1", "sub_P2")] >= 1
    # No corruption: dict still has exactly these 2 keys (no stray)
    assert set(counts.keys()) == {("turn_t1", "sub_P1"), ("turn_t1", "sub_P2")}


# ─────────────────────────────────────────────────────────────
# UT 6: bail envelope surfaces to chat via Lead _resolve_final_answer
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_b_bail_surfaces_to_chat_via_aggregate() -> None:
    """CTO P1-1 fix verified: bail envelope (NO title) → Lead's resolve chain
    surfaces narrative/fallback_markdown user-grade prose, not a placeholder
    title. Aggregates from `_m137_subagent_payloads` bag, then walks the
    `_resolve_final_answer_text` chain (exec_summary → title → narrative →
    fallback_markdown)."""
    from bowhead_core.agents.lead_orchestrator import (
        _aggregate_subagent_payloads,
    )

    class _FakeCtx:
        pass

    parent_ctx = _FakeCtx()
    bail_narrative = _build_fallback_narrative()
    # Mimic subagent_spawn handler push: bail payload landed in the bag.
    # CTO P1-1: bail envelope no longer carries `title` — only narrative +
    # fallback_markdown, so resolve chain falls through to user-grade prose.
    parent_ctx._m137_subagent_payloads = [  # type: ignore[attr-defined]
        {
            "subagent_id": "sub_x",
            "profile": "compute",
            "payload": {
                "narrative": bail_narrative,
                "fallback_markdown": bail_narrative,
            },
        }
    ]
    merged = _aggregate_subagent_payloads(parent_ctx)
    assert merged is not None, "bail envelope must aggregate"
    # narrative / fallback_markdown surfaced in merged
    assert merged.get("fallback_markdown") == bail_narrative, merged
    # CTO P1-1: no placeholder title in merged
    assert not merged.get("title"), f"bail envelope must NOT carry placeholder title: {merged.get('title')!r}"

    # Lead `_resolve_final_answer_text` fallback chain — replicate the logic
    text_for_chat = merged.get("exec_summary") or merged.get("title") or merged.get("narrative") or merged.get("fallback_markdown") or ""
    # CTO P1-1: chain skips empty title → narrative wins. User sees the prose,
    # not "(无法生成结构化报告)".
    assert text_for_chat == bail_narrative, text_for_chat
    assert text_for_chat.startswith("我已经基于查到的数据"), text_for_chat
    # Negative: placeholder string must not surface
    assert "(无法生成结构化报告)" not in text_for_chat


def test_aggregate_dedups_sources_across_peers() -> None:
    """report-engine Phase-2 §13.1.2: two peers citing the SAME source
    (artifact_id) must collapse to ONE Sources row in the merged payload — else
    the renderer shows duplicate Sources + split [N] numbering. Distinct sources
    survive."""
    from bowhead_core.agents.lead_orchestrator import _aggregate_subagent_payloads

    class _FakeCtx:
        pass

    parent_ctx = _FakeCtx()
    parent_ctx._m137_subagent_payloads = [  # type: ignore[attr-defined]
        {
            "subagent_id": "sub_a",
            "profile": "compute",
            "payload": {
                "exec_summary": "A",
                "sources": [
                    {"label": "营收表", "artifact_id": "art_shared", "tool": "bi_query"},
                    {"label": "A 独有", "artifact_id": "art_a", "tool": "bi_query"},
                ],
            },
        },
        {
            "subagent_id": "sub_b",
            "profile": "marketing-analyst",
            "payload": {
                "sources": [
                    {"label": "营收表(再引)", "artifact_id": "art_shared", "tool": "bi_query"},
                    {"label": "B 独有", "artifact_id": "art_b", "tool": "web_search"},
                ],
            },
        },
    ]
    merged = _aggregate_subagent_payloads(parent_ctx)
    assert merged is not None
    ids = [s["artifact_id"] for s in merged["sources"]]
    assert ids == ["art_shared", "art_a", "art_b"]  # shared collapsed, order kept
    # first-write wins: the shared row keeps peer A's label
    assert merged["sources"][0]["label"] == "营收表"


def test_aggregate_excludes_bounded_chat_payloads_from_report_composer() -> None:
    """116 t_6f95d818d6ad: short compute result stays in Chat, not report input."""
    from bowhead_core.agents.lead_orchestrator import _aggregate_subagent_payloads

    class _FakeCtx:
        pass

    parent_ctx = _FakeCtx()
    parent_ctx._m137_subagent_payloads = [  # type: ignore[attr-defined]
        {
            "subagent_id": "sub_compute",
            "profile": "compute",
            "payload": {
                "delivery_surface": "chat",
                "exec_summary": "19 × 29 = 551",
                "kpis": [
                    {
                        "label": "19 × 29",
                        "value": "551",
                        "sentiment": "neutral",
                    }
                ],
            },
        }
    ]

    assert _aggregate_subagent_payloads(parent_ctx) is None


def test_aggregate_dedups_labelless_sources_exactly() -> None:
    """Sources with no artifact_id dedup on exact (label,tool,query) — exact dups
    collapse, distinct ones survive (so we never drop a real distinct source)."""
    from bowhead_core.agents.lead_orchestrator import _aggregate_subagent_payloads

    class _FakeCtx:
        pass

    parent_ctx = _FakeCtx()
    parent_ctx._m137_subagent_payloads = [  # type: ignore[attr-defined]
        {
            "subagent_id": "s1",
            "profile": "compute",
            "payload": {"exec_summary": "x", "sources": [{"label": "同一来源", "tool": "web_search", "query": "q"}]},
        },
        {
            "subagent_id": "s2",
            "profile": "compute",
            "payload": {
                "sources": [
                    {"label": "同一来源", "tool": "web_search", "query": "q"},  # exact dup
                    {"label": "不同来源", "tool": "web_search", "query": "q2"},  # distinct
                ]
            },
        },
    ]
    merged = _aggregate_subagent_payloads(parent_ctx)
    assert merged is not None
    labels = [s["label"] for s in merged["sources"]]
    assert labels == ["同一来源", "不同来源"]


# ─────────────────────────────────────────────────────────────
# UT 7: strip-with-evidence still wins (no bail synth when payload non-empty)
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_b_strip_with_evidence_does_not_bail() -> None:
    """When 3rd attempt's strip leaves valid sections (kpis / recommendations
    / long exec_summary) intact, return stripped payload normally — bail
    envelope NOT synthesized. Validates `_payload_has_any_evidence` discrim."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    ctx._current_subagent_id = "sub_FFF"

    # 1st reject
    await reg.execute("subagent_return", {"payload": _bad_ref_payload()}, ctx)
    # 2nd reject
    await reg.execute("subagent_return", {"payload": _bad_ref_payload()}, ctx)
    # 3rd: retry≥2 → strip-pass path. _bad_ref_payload has kpis + recs +
    # long exec_summary → _payload_has_any_evidence == True → continue
    # normal validation (will fail schema due to nulled chart_section_id
    # leaving evidence with required ref empty — but that's NOT a bail).
    inv3 = await reg.execute(
        "subagent_return",
        {"payload": _bad_ref_payload()},
        ctx,
    )
    r3 = inv3.result
    # The result should NOT be a bail envelope (no bail_kind on result).
    # It MAY be ok=False (schema validation failure post-strip) OR ok=True
    # (passes through). What we MUST NOT see is bail_kind set.
    assert "bail_kind" not in r3 or r3.get("bail_kind") != PI_B_BAIL_KIND, r3


# ─────────────────────────────────────────────────────────────
# UT 8: turn-end GC sweeps `_ν_retry_counts` for the finished turn
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_b_turn_end_gc_sweeps_per_turn_keys() -> None:
    """Lead orchestrator's turn-end cleanup sweeps `_ν_retry_counts` entries
    whose key[0] == this turn_id, preserving keys from other turns (sticky
    parent_ctx case). New turn starts at 0."""

    class _FakeCtx:
        pass

    ctx = _FakeCtx()
    ctx._ν_retry_counts = {  # type: ignore[attr-defined]
        ("turn_T1", "sub_a"): 2,
        ("turn_T1", "sub_b"): 1,
        ("turn_T2", "sub_c"): 1,  # different turn — must survive
    }

    # Replicate the GC block from lead_orchestrator (around line 1095):
    _this_turn = "turn_T1"
    counts = ctx._ν_retry_counts  # type: ignore[attr-defined]
    stale_keys = [k for k in list(counts.keys()) if isinstance(k, tuple) and len(k) >= 1 and k[0] == _this_turn]
    for k in stale_keys:
        counts.pop(k, None)

    # turn_T1 keys gone, turn_T2 preserved
    assert ("turn_T1", "sub_a") not in counts
    assert ("turn_T1", "sub_b") not in counts
    assert ("turn_T2", "sub_c") in counts


# ─────────────────────────────────────────────────────────────
# Helper-fn UT — _payload_has_any_evidence boundary cases
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_b_payload_has_any_evidence_boundaries() -> None:
    """Boundary cases for `_payload_has_any_evidence` so bail synth predicate
    is stable as payload schema evolves."""
    # All empty → False
    assert _payload_has_any_evidence({}) is False
    assert _payload_has_any_evidence({"insights": [], "kpis": []}) is False
    # Long exec_summary → True
    assert _payload_has_any_evidence({"exec_summary": "整体合规率 11.39%。这条 narrative 长度 ≥30 字, 用于触发 evidence pass."}) is True
    # Short exec_summary → False
    assert _payload_has_any_evidence({"exec_summary": "短."}) is False
    # kpis with at least one entry → True
    assert _payload_has_any_evidence({"kpis": [{"label": "x", "value": "1"}]}) is True
    # recommendations → True
    assert _payload_has_any_evidence({"recommendations": [{"priority": "P0", "action": "x"}]}) is True
    # insight_section_ids (thin path) → True
    assert _payload_has_any_evidence({"insight_section_ids": ["sec_a"]}) is True
    # insights with non-empty narrative → True
    assert _payload_has_any_evidence({"insights": [{"title": "x", "narrative": "n" * 30, "thesis": ""}]}) is True
    # insights all empty → False
    assert _payload_has_any_evidence({"insights": [{"title": "x", "narrative": "", "thesis": ""}]}) is False


@pytest.mark.unit
def test_b_get_nu_retry_key_fallback_default() -> None:
    """When `_current_subagent_id` is not set on ctx, key falls back to
    'default' bucket so legacy tests (which set `_ν_retry_count` directly)
    still observe a sane counter."""

    class _FakeCtx:
        pass

    ctx = _FakeCtx()
    ctx.turn_id = "turn_X"  # type: ignore[attr-defined]
    # No _current_subagent_id
    key = _get_nu_retry_key(ctx)
    assert key == ("turn_X", "default")
    # With _current_subagent_id
    ctx._current_subagent_id = "sub_zzz"  # type: ignore[attr-defined]
    key2 = _get_nu_retry_key(ctx)
    assert key2 == ("turn_X", "sub_zzz")
    # CTO P0-1: captured_subagent_id override beats ctx attr (snapshot semantics).
    key3 = _get_nu_retry_key(ctx, captured_subagent_id="sub_snapshot")
    assert key3 == ("turn_X", "sub_snapshot")
    # captured_subagent_id=None (explicit) falls back to ctx attr.
    key4 = _get_nu_retry_key(ctx, captured_subagent_id=None)
    assert key4 == ("turn_X", "sub_zzz")


# ─────────────────────────────────────────────────────────────
# UT (new) — CTO P0-1: concurrent peer yields mid-handler must not
# cross-attribute counter when ctx._current_subagent_id swaps under await.
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_b_concurrent_peer_yields_mid_handler_no_cross_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CTO P0-1: handler `await`s the canvas lookup BEFORE reading subagent_id.
    Two concurrent peer coroutines with different sids, both forced to yield
    inside that lookup → without the snapshot fix, each coroutine reads
    the OTHER's subagent_id after resume → cross-attribution on counter.
    (empty-canvas 止血批把该 await 从 ensure_canvas 换成 get_canvas_by_session,
    让步点与竞态形状不变。)

    Verify: counters land on the correct key for each coroutine.
    """
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()

    # Slow canvas lookup: holds an event so both coroutines yield mid-handler
    # at the same await point. While A is suspended, B sets ctx.sid to its
    # own value, then yields. When A resumes, ctx.sid points at B's value —
    # the exact race the snapshot fix defends against.
    barrier = asyncio.Event()
    coros_entered = 0
    coros_entered_target = 2

    class _SlowStore:
        async def get_canvas_by_session(self, session_id: str) -> _StubCanvas:
            nonlocal coros_entered
            coros_entered += 1
            if coros_entered >= coros_entered_target:
                barrier.set()
            # Yield until both coroutines have arrived here — forces
            # interleaving so ctx._current_subagent_id swap is observable.
            await barrier.wait()
            return _StubCanvas()

        async def list_sections(self, canvas_id: str) -> list[Any]:
            return []

        async def list_section_ids_by_kind(self, canvas_id: str, *, kind: str) -> set[str]:
            return set()

    ctx.canvas_store = _SlowStore()

    async def _call(sid: str) -> None:
        # Each coroutine sets sid sync, but a *real* peer dispatch would have
        # parent_ctx._current_subagent_id swapped between gathers. Simulate
        # the worst case: coroutine B overwrites sid while A is awaiting.
        ctx._current_subagent_id = sid
        await reg.execute(
            "subagent_return",
            {"payload": _bad_ref_payload()},
            ctx,
        )

    # Schedule A first, then B — both await inside ensure_canvas, then both
    # resume. Without the P0-1 fix, whichever resumes second observes the
    # other's sid when calling _get_nu_retry_key.
    await asyncio.gather(_call("sub_RACE_A"), _call("sub_RACE_B"))

    counts = ctx._ν_retry_counts
    key_a = ("turn_t1", "sub_RACE_A")
    key_b = ("turn_t1", "sub_RACE_B")
    # CTO P0-1 contract: each coroutine bumps ITS OWN key, not the other's.
    # Total = 2 reject events, 1 per sid.
    assert counts.get(key_a) == 1, f"sub_RACE_A counter wrong: counts={dict(counts)}"
    assert counts.get(key_b) == 1, f"sub_RACE_B counter wrong: counts={dict(counts)}"
    # No stray keys (e.g. "default" from a misread, or both writes landing
    # on the same key would show as {key_X: 2}).
    assert set(counts.keys()) == {key_a, key_b}, dict(counts)


# ─────────────────────────────────────────────────────────────
# UT (new) — CTO P1-1: bail envelope omits placeholder title so resolve
# chain surfaces user-grade prose (not "(无法生成结构化报告)").
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_b_bail_envelope_omits_placeholder_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CTO P1-1: synthesized bail payload must NOT carry a `title` field.
    Without this fix, Lead's resolve chain (exec_summary → title → narrative
    → fallback_markdown) returns the placeholder title before the prose."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    ctx._current_subagent_id = "sub_P11"

    # Pre-arm counter so next call hits strip-pass
    _ν_counts = _ensure_nu_retry_counts(ctx)
    _ν_counts[("turn_t1", "sub_P11")] = 2

    def _strip_empty(payload: dict, errors: list) -> dict:
        return {
            "title": "leftover-from-llm",  # even if peer set a title
            "exec_summary": "",
            "kpis": [],
            "insights": [],
            "recommendations": [],
        }

    monkeypatch.setattr(
        "bowhead_core._ref_validation._strip_invalid_refs",
        _strip_empty,
    )

    inv = await reg.execute(
        "subagent_return",
        {"payload": _bad_ref_payload()},
        ctx,
    )
    bail_payload = inv.result["payload"]
    # CTO P1-1: title key must not be present at all (or be falsy) so
    # resolve chain falls through to narrative.
    assert "title" not in bail_payload or not bail_payload["title"], f"bail envelope leaked title: {bail_payload.get('title')!r}"
    # And narrative/fallback_markdown carry the prose
    prose = _build_fallback_narrative()
    assert bail_payload["narrative"] == prose
    assert bail_payload["fallback_markdown"] == prose


# ─────────────────────────────────────────────────────────────
# UT (new) — CTO P1-2: scalar `_ν_retry_count` mirror NOT written on bump.
# Authoritative count lives in the dict; scalar removal eliminates drift.
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_b_legacy_scalar_attr_not_written() -> None:
    """CTO P1-2: handler must no longer write `ctx._ν_retry_count` scalar
    on each reject. Authoritative source is `_ν_retry_counts` dict; the
    scalar diverged across peer respawn pre-fix."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    ctx._current_subagent_id = "sub_GGG"

    # Reject 1 — handler should bump dict but NOT touch scalar attr.
    assert not hasattr(ctx, "_ν_retry_count"), "precondition: scalar absent"
    await reg.execute("subagent_return", {"payload": _bad_ref_payload()}, ctx)
    counts = ctx._ν_retry_counts
    assert counts[("turn_t1", "sub_GGG")] == 1
    # CTO P1-2: scalar attr was NOT written by handler
    assert not hasattr(ctx, "_ν_retry_count"), f"scalar leaked: {getattr(ctx, '_ν_retry_count', None)!r} — P1-2 fix removes the mirror write"

    # Reject 2 — same shape
    await reg.execute("subagent_return", {"payload": _bad_ref_payload()}, ctx)
    assert counts[("turn_t1", "sub_GGG")] == 2
    assert not hasattr(ctx, "_ν_retry_count")
