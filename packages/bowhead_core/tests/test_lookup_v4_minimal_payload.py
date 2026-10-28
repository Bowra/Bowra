"""chat-v4 W3W4-rewrite (Option A) — UT for lookup short-path
minimal lead.final.payload emit.

When a turn finishes WITHOUT spawning a peer (`aggregated_payload` is None),
lead_orchestrator builds a minimal AnalysisPayload-shaped payload from the
Lead-self prose so FE still receives a lead.final.payload event for the
chat-style answer. Without this, lookup turns would only get final_answer
+ done, missing the v4 frame.
"""

from __future__ import annotations

from typing import Any

from bowhead_core.agents.lead_orchestrator import build_lookup_minimal_payload


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, ev: Any) -> None:  # mirrors _emit_event call shape
        self.events.append((ev.event_type, dict(ev.payload)))


class _FakeCtx:
    def __init__(self) -> None:
        self._v4_final_payload_emitted = False


def _build_minimal_lookup_payload(text_for_chat: str) -> dict:
    """Thin call into the REAL builder (was a hand-copied mirror that no
    production change could ever turn red — see the note further down)."""
    return build_lookup_minimal_payload(text_for_chat=text_for_chat, sources=[])


def test_lookup_short_path_payload_shape() -> None:
    payload = _build_minimal_lookup_payload("4 月 GMV 是 5.0M, 同比 +12%.")
    # Schema parity with full AnalysisPayload emit (FE consumer can mix).
    assert set(payload.keys()) == {
        "title",
        "exec_summary",
        "kpis",
        "insights",
        "recommendations",
        "actions",
        "caveats",
        "sources",
    }
    assert payload["exec_summary"] == "4 月 GMV 是 5.0M, 同比 +12%."
    # Slot-optional rule — empty arrays legal.
    assert payload["kpis"] == []
    assert payload["recommendations"] == []
    assert payload["actions"] == []  # CHANGE-S.6 dual-read alias


def test_lookup_payload_exec_summary_is_not_truncated() -> None:
    """A long Lead answer reaches the payload without truncation.

    The short-answer threshold is a discriminator, not a content budget. Downstream
    consumers apply their own limits without altering the payload's source text.
    """
    long_text = "x" * 2000
    payload = _build_minimal_lookup_payload(long_text)
    assert payload["exec_summary"] == long_text
    assert "…" not in payload["exec_summary"]


def test_lookup_payload_dedup_latch_skips_second() -> None:
    """If aggregated_payload path already emitted lead.final.payload,
    the lookup short-path emit must skip via parent_ctx latch."""
    ctx = _FakeCtx()
    ctx._v4_final_payload_emitted = True
    # Replicate the orchestrator's guard.
    already = getattr(ctx, "_v4_final_payload_emitted", False)
    aggregated_payload: dict | None = None
    if not already and not aggregated_payload:
        raise AssertionError("would emit but should have skipped")
    # Pass: latch held; no emit attempt.


def test_lookup_payload_only_when_no_aggregated() -> None:
    """The minimal lookup emit must NOT fire when aggregated_payload exists
    (peer was spawned → CHANGE-W4.1 path already emitted)."""
    ctx = _FakeCtx()
    aggregated_payload = {"title": "from peer", "exec_summary": "peer prose"}
    already = getattr(ctx, "_v4_final_payload_emitted", False)
    if not already and not aggregated_payload:
        raise AssertionError("would emit but should have skipped (peer path)")


def test_lookup_payload_emits_when_aggregated_none_and_text_present() -> None:
    """Positive case: aggregated_payload None + text_for_chat present + latch
    False → emit fires + latch flips."""
    ctx = _FakeCtx()
    aggregated_payload: dict | None = None
    text_for_chat = "4 月 GMV 5.0M"
    emitted: list[tuple[str, dict]] = []

    if not getattr(ctx, "_v4_final_payload_emitted", False) and not aggregated_payload:
        payload = _build_minimal_lookup_payload(text_for_chat)
        emitted.append(("lead.final.payload", payload))
        ctx._v4_final_payload_emitted = True

    assert len(emitted) == 1
    assert emitted[0][0] == "lead.final.payload"
    assert emitted[0][1]["exec_summary"] == "4 月 GMV 5.0M"
    assert ctx._v4_final_payload_emitted is True


def test_lookup_payload_actions_alias_present() -> None:
    """CHANGE-S.6: `actions` dict-level alias for `recommendations`. Lookup
    short-path payload ships with empty `actions=[]` for FE dual-read parity."""
    payload = _build_minimal_lookup_payload("x")
    assert "actions" in payload
    assert payload["actions"] == payload["recommendations"]


# ── The supplemental-emission decision itself (bug found on dev hardware, 2026-08-06) ──
#
# ⚠ The tests above **previously** exercised a copied mirror, not product code; that let the 600-character
# hard cutoff survive until 1.2.24. The mirror now calls `build_lookup_minimal_payload` directly.
# The following test checks the supplemental-emission decision itself.

from bowhead_core.agents.lead_orchestrator import _should_emit_minimal_final_payload  # noqa: E402


def test_lookup_turn_still_emits() -> None:
    """原有行为：没派 peer 的 lookup/闲聊轮照发。"""
    assert _should_emit_minimal_final_payload(already_emitted=False, aggregated_payload=None, has_inline_artifacts=False)
    assert _should_emit_minimal_final_payload(already_emitted=False, aggregated_payload={}, has_inline_artifacts=False)


def test_already_emitted_never_double_emits() -> None:
    assert not _should_emit_minimal_final_payload(already_emitted=True, aggregated_payload=None, has_inline_artifacts=True)
    assert not _should_emit_minimal_final_payload(already_emitted=True, aggregated_payload={"title": "x"}, has_inline_artifacts=True)


def test_export_turn_with_deliverable_gets_the_chip_frame() -> None:
    """★ 真机 dev t_186741818ea0 / t_cfd9a579fe2a：纯导出轮。

    payload 非空但 V4 校验必败（`missing/empty top-level: title/kpis/insights/
    recommendations` —— 导出轮本来就不是分析），fall through 到 V1 markdown；
    而下载 chip 只在 lead.final.payload 上注入 ⇒ 修复前**文件在、按钮没有**。
    """
    export_payload = {"exec_summary": "已导出 Bowhead Canvas.docx"}  # No title/kpis/insights
    assert _should_emit_minimal_final_payload(
        already_emitted=False,
        aggregated_payload=export_payload,
        has_inline_artifacts=True,
    ), "有交付物却不补发这一帧 = 用户看不到下载按钮"


def test_failed_payload_without_deliverable_is_unchanged() -> None:
    """没有交付物的失败轮**行为不变** —— 这次修复的作用域仅限"有东西要给用户"。"""
    assert not _should_emit_minimal_final_payload(
        already_emitted=False,
        aggregated_payload={"exec_summary": "分析失败"},
        has_inline_artifacts=False,
    )
