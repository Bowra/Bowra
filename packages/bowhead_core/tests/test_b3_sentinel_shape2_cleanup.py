"""bi-hitl-cleanup P3 / B3 tests — Shape-2 sentinel auto-emit cleanup.

Per tech-design §P3 (砍 Shape-2 only, 保留 Shape-1 + CH2):
- I1: subagent.py no longer has lead-only sentinel break block
- I2: lead_orchestrator.py CH2 path collapsed (no `else: single-sentinel` branch)
- I3: CH2 merge with N=1 emits with multi_select=False (preserves single-card UX)
- I4: Shape-1 N=1 hits the double-write guard (no _ch2_store.put call)
- I5: `is_clarification_sentinel` function kept (backward-compat for Shape-2 detection
      if any unexpected source re-emerges)
- Shape-1 (clarification_ask) path: tool handler still writes store.put (line 215)
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SUBAGENT_PY = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "subagent.py"
LEAD_ORCH_PY = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "agents" / "lead_orchestrator.py"
CLARIFICATION_TOOL_PY = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "agents" / "clarification_tool.py"


# ── I1: subagent.py lead-only sentinel break gone ───────────────────────


def test_b3_subagent_no_lead_only_sentinel_break_block() -> None:
    """Old `if profile.name == "lead": ... sentinel_break_after_invocation`
    block deleted from subagent.py."""
    src = SUBAGENT_PY.read_text()
    # The unique fingerprint log msg from the deleted block
    assert "sentinel_break_after_invocation" not in src, (
        "subagent.py still has sentinel_break_after_invocation log — B3 break block not fully deleted"
    )


def test_b3_subagent_sentinel_break_revived_exactly_once() -> None:
    """B3 曾删除 subagent 的 shape-2 sentinel break（当时无 Shape-2 生产者）。

    批2 (workspace-space-split PRD §3.3b) 契约演进：c4 重新引入合法 Shape-2
    生产者（tool_context_select dispatch 闸 sentinel），break **复活**——sentinel
    必须就地终止 loop（回喂 LLM = act-first 模型双问/自选，dogfood t_102a292a65b0
    实锤）。pin：调用点恰 1 处（防散播），且与批2 注释共现。

    Two later reviewed consumers only *read* the sentinel and do not add a loop break:
    d48d6c87b prepares native ordinary questions before publication, and 16e44bbc5 keeps a
    collaboration open while Lead pauses for clarification. Pin all three exact call sites.
    """
    src = SUBAGENT_PY.read_text()
    loop_break = 'any(is_clarification_sentinel(getattr(_inv, "result", None)) for _inv in invocations)'
    ordinary_question = "if ordinary_state is None or not is_clarification_sentinel(inv.result):"
    collaboration_pause = "any(is_clarification_sentinel(inv.result) for inv in invocations)"
    n = src.count("is_clarification_sentinel(")
    assert n == 3, (
        f"subagent.py has {n} is_clarification_sentinel call sites; only the batch-2 loop break and the two reviewed read-only checks are allowed"
    )
    for site in (loop_break, ordinary_question, collaboration_pause):
        assert src.count(site) == 1, site
    idx = src.index(loop_break)
    assert "批2" in src[max(0, idx - 800) : idx], "sentinel break 调用点缺批2 上下文注释——请勿在别处新增调用"


def test_b3_subagent_import_of_is_clarification_sentinel_revived() -> None:
    """批2: import 随 loop-break 复活（见上一测试的演进说明）。pin 恰 1 行."""
    src = SUBAGENT_PY.read_text()
    assert src.count("from bowhead_core.agents.clarification_tool import is_clarification_sentinel") == 1


# ── I2: lead_orchestrator.py CH2 collapse ──────────────────────────────


def test_b3_lead_orchestrator_no_else_single_sentinel_branch() -> None:
    """The `else:` branch (single-sentinel path with A1's Shape-2 store.put
    back-fill) deleted; CH2 merge handles both N=1 and N≥2 uniformly."""
    src = LEAD_ORCH_PY.read_text()
    # The unique signature log of the deleted single-sentinel branch
    assert 'logger.info(\n                "lead_loop clarification_sentinel turn_id' not in src, (
        "lead_orchestrator.py still has old single-sentinel else-branch log"
    )
    # And the A1 patch log msg should also be gone
    assert "single_sentinel_shape2 store.put failed" not in src


def test_b3_lead_orchestrator_ch2_condition_collapsed() -> None:
    """The `if len(_ch2_sentinels) >= 2:` collapsed to `if _ch2_sentinels:`."""
    src = LEAD_ORCH_PY.read_text()
    assert "if len(_ch2_sentinels) >= 2:" not in src, "lead_orchestrator.py still gates on `>= 2` — B3 not done"
    # The new condition exists
    assert "if _ch2_sentinels:" in src


# ── I3: multi_select toggles by count, single-card UX preserved ─────────


def test_b3_multi_select_toggles_by_count() -> None:
    """multi_select=True when N≥2, False when N=1 (preserves V4ClarificationCard
    single-card UX for one-question case)."""
    src = LEAD_ORCH_PY.read_text()
    # The conditional assignment expression appears
    assert "_multi_select = _n_sentinels >= 2" in src
    # The store.put and SSE emit both use `_multi_select` (not hardcoded True)
    # Find ch2 store.put block (only the CH2 merge one — single-sentinel path gone)
    assert "multi_select=_multi_select" in src
    # SSE emit too
    assert '"multi_select": _multi_select' in src


# ── I4: Shape-1 N=1 double-write guard ───────────────────────────────


def test_b3_double_write_guard_present() -> None:
    """When all sentinels are Shape-1 AND N=1, skip the merge-path store.put
    (Shape-1 tool already wrote)."""
    src = LEAD_ORCH_PY.read_text()
    # Guard variable
    assert "_all_shape1" in src
    assert "_skip_store_put" in src
    # The actual skip condition
    assert "_all_shape1 and _n_sentinels == 1" in src
    # The guard gates the store.put (`if _ch2_store is not None and not _skip_store_put`)
    assert "not _skip_store_put" in src
    # And the new log msg
    assert "ch2_merge_skip_double_write_put" in src


def test_b3_shape1_sentinel_pending_flag_used_for_guard() -> None:
    """Double-write guard reads `_clarification_pending` (Shape-1 marker) via
    the SENTINEL_CLARIFICATION_PENDING constant import."""
    src = LEAD_ORCH_PY.read_text()
    assert "SENTINEL_CLARIFICATION_PENDING" in src
    # And it's specifically used to detect Shape-1
    assert "get(SENTINEL_CLARIFICATION_PENDING) is True" in src


# ── I5: is_clarification_sentinel still exists in clarification_tool ───


def test_b3_is_clarification_sentinel_function_kept() -> None:
    """The detector function stays (Shape-1 Lead-side detection elsewhere may
    rely on it, e.g. CH2 collection); Shape-2 branch becomes 永远 False post-B2."""
    src = CLARIFICATION_TOOL_PY.read_text()
    assert "def is_clarification_sentinel(" in src
    # Shape-1 detector code still present
    assert "SENTINEL_CLARIFICATION_PENDING" in src
    # Shape-2 branch also still present (backward-compat for any future caller)
    assert 'error_kind") == "needs_clarification"' in src


# ── Shape-1 backbone unchanged ──────────────────────────────────────────


def test_b3_clarification_tool_store_put_still_at_line_215_ish() -> None:
    """Shape-1 backbone: clarification_ask tool handler still writes store.put
    (line ~215 ish — verified post-B3 it's still there, possibly drifted by ±5)."""
    src = CLARIFICATION_TOOL_PY.read_text()
    # The store.put call site identified by surrounding token usage
    assert "await store.put(" in src or "store.put(" in src, "clarification_tool.py: no store.put call — Shape-1 backbone broken"
