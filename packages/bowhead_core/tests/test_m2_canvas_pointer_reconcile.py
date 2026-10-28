"""M2/2a (report-pipeline-reliability): kill the phantom "报告已生成 / 见画布" claim.

The Lead prompt no longer emits the pointer inline (best-effort); code appends it
only when a report surfaced, and the done-branch RETRACTS it via a corrective
final_answer when the turn ends clarification_pending / no report surfaced. These
tests cover the deterministic net: the pointer regex + `_reconcile_canvas_pointer`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.agents import lead_orchestrator as lo
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus


def test_canvas_pointer_regex_corpus() -> None:
    claims, strip = lo._claims_canvas_pointer, lo._strip_canvas_pointer
    for t in [
        "分析完成。完整报告见画布。",
        "报告已生成，见 canvas。",
        "2026 报告已生成，详见画布。",
        "报告见画布",
        "见画布",
        "报告已生成，见画布",  # two-clause phantom (review P1: clause-bounding left "报告已生成")
        "报告已生成，请见画布查看详情",  # pointer mid-clause
        "见\ncanvas",  # newline-straddling EN pointer (review P2-a: \n is a clause separator)
    ]:
        assert claims(t), f"should claim pointer: {t!r}"
        s = strip(t)
        # BOTH the pointer word AND the report-exists CLAIM must be gone. (review P2: the
        # old test only checked the pointer word — it masked a residual "报告已生成".)
        assert "见画布" not in s and "canvas" not in s.lower(), f"strip left pointer: {t!r} -> {s!r}"
        assert "报告已生成" not in s and "报告已完成" not in s, f"strip left claim: {t!r} -> {s!r}"
    # NEGATIVES — topical / incidental 画布 must NOT be treated as a pointer (no data loss)
    for t in [
        "本季度营收增长 3%。",
        "",
        None,
        "见画布科技公司本季度营收增长20%。",  # 见画布 as a company name (not clause-final)
        "从图中可见画布纹理清晰。",  # 可见画布 = "visible canvas" (lookbehind excludes 可)
    ]:
        assert not claims(t), f"should NOT claim: {t!r}"
    # PRESERVATION — comma-joined findings must SURVIVE the strip (review P1: was data loss)
    assert strip("营收增长3%，完整报告见画布") == "营收增长3%"
    _got = strip("营收 +3%，详见画布，利润率 12%")
    assert "营收" in _got and "利润率" in _got, f"findings destroyed: {_got!r}"
    assert strip("营收 +3%，利润率 12%。完整报告见画布。").startswith("营收 +3%")
    # INTERLEAVED claim,finding,pointer — finding survives; claim + pointer both gone (review P1)
    _mid = strip("报告已生成，营收+3%，见画布")
    assert "营收" in _mid and "报告已生成" not in _mid and "见画布" not in _mid, f"interleaved: {_mid!r}"


def test_canvas_pointer_non_jian_forms_deliberately_kept() -> None:
    """WP-6 broadening was REVERTED (review P1): non-"见" forms like "在画布(中|上|里)" /
    "查看/打开画布" are indistinguishable from real 画布-domain data (design/whiteboard/art/
    canvas-analytics), and whole-clause strip would silently delete the chat summary's data.
    This LOCKS the decision — these forms must be KEPT, so a future dev doesn't re-add the
    data-losing broadening. The residual gap is accepted (soft prompt + append + reconcile)."""
    claims, strip = lo._claims_canvas_pointer, lo._strip_canvas_pointer
    for t in [
        "分析显示利润在画布上涨15%。",  # 在画布**上**涨 — 上 is verb, not a location marker
        "用户在画布中的平均停留时长为8分钟。",  # canvas-product analytics — real data
        "颜料在画布上的附着力较强。",  # textile/art domain — real data
        "用户平均每天打开画布12次。",  # usage metric — real data
        "请查看画布中的哪部分数据异常？",  # a clarifying question, not a report pointer
        "画布行业本季度营收增长20%。",  # industry term
    ]:
        assert not claims(t), f"must NOT be treated as pointer (would lose data): {t!r}"
        assert strip(t).strip() == t.strip(), f"must keep verbatim: {t!r} -> {strip(t)!r}"
    # The "见画布" idiom is still caught (that's the reliable pointer form the LLM appends).
    assert claims("完整报告见画布。")
    assert strip("营收增长3%，完整报告见画布") == "营收增长3%"


def test_deterministic_append_never_doubles_pointer() -> None:
    # mirrors the _lead_loop append: strip any inline LLM pointer, then append one.
    # (dogfood caught qwen re-emitting 见画布 inline → would double without the strip.)
    strip = lo._strip_canvas_pointer
    for llm_text in [
        "各品类分析完成，完整报告见画布。",  # LLM already claimed it inline
        "各品类分析完成。",  # LLM did not
        "营收 +3%，详见画布，利润率 12%",  # pointer mid-sentence
    ]:
        composed = strip(llm_text).rstrip() + "\n\n完整报告见画布。"
        assert composed.count("见画布") == 1, f"doubled: {composed!r}"
        assert composed.endswith("完整报告见画布。")


class _Bus:
    def __init__(self, **attrs) -> None:
        self.events: list = []
        for k, v in attrs.items():
            setattr(self, k, v)

    async def emit(self, ev) -> None:
        self.events.append(ev)


def _finals(bus: _Bus) -> list:
    return [e for e in bus.events if getattr(e, "event_type", None) == "final_answer"]


async def test_reconcile_fires_on_clarification_pending() -> None:
    bus = _Bus(_canvas_pointer_claimed=True, _lead_chat_text="需要更多信息。完整报告见画布。")
    fired = await lo._reconcile_canvas_pointer(bus, "t1", "clarification_pending", False)
    assert fired
    fa = _finals(bus)
    assert len(fa) == 1
    assert fa[0].payload["reason_code"] == "canvas_pointer_reconciled"
    assert "见画布" not in fa[0].payload["text"]
    assert fa[0].payload["text"].startswith("需要更多信息")  # real prose preserved
    assert bus._canvas_pointer_claimed is False  # idempotency flag cleared


async def test_reconcile_fires_when_no_report_surfaced() -> None:
    bus = _Bus(_canvas_pointer_claimed=True, _lead_chat_text="分析完成。完整报告见画布。")
    fired = await lo._reconcile_canvas_pointer(bus, "t1", "completed", False)  # report_kept False
    assert fired
    assert "见画布" not in _finals(bus)[0].payload["text"]


async def test_reconcile_noop_when_report_kept() -> None:
    # a genuine report turn: pointer claimed AND report kept → the pointer is honest
    bus = _Bus(_canvas_pointer_claimed=True, _lead_chat_text="完整报告见画布。")
    fired = await lo._reconcile_canvas_pointer(bus, "t1", "completed", True)
    assert not fired and _finals(bus) == []


async def test_reconcile_noop_when_no_pointer_claimed() -> None:
    bus = _Bus(_canvas_pointer_claimed=False)
    fired = await lo._reconcile_canvas_pointer(bus, "t1", "clarification_pending", False)
    assert not fired and _finals(bus) == []


async def test_reconcile_idempotent_on_done_reemit() -> None:
    # D6-hidden completed path (empty strip → fallback fires; NOT the clarification skip)
    bus = _Bus(_canvas_pointer_claimed=True, _lead_chat_text="完整报告见画布。")
    assert await lo._reconcile_canvas_pointer(bus, "t1", "completed", False)
    # a done re-emit calls it again — flag was cleared → no 2nd corrective
    assert not await lo._reconcile_canvas_pointer(bus, "t1", "completed", False)
    assert len(_finals(bus)) == 1


async def test_reconcile_fallback_text_when_only_pointer() -> None:
    # completed + prose was only the pointer → strips empty → safe fallback text
    from bowhead_core.effective_language import PIN_ATTR

    bus = _Bus(_canvas_pointer_claimed=True, _lead_chat_text="完整报告见画布。", **{PIN_ATTR: "zh-CN"})
    assert await lo._reconcile_canvas_pointer(bus, "t1", "completed", False)
    assert _finals(bus)[0].payload["text"] == "本次未生成画布报告。"
    # Without a pinned turn language the fallback text is English.
    from bowhead_core.report_chrome_text import chrome_text

    unpinned = _Bus(_canvas_pointer_claimed=True, _lead_chat_text="完整报告见画布。")
    assert await lo._reconcile_canvas_pointer(unpinned, "t1", "completed", False)
    assert _finals(unpinned)[0].payload["text"] == chrome_text("no_report_this_turn", "en-US")


async def test_reconcile_skips_clarification_when_stripped_empty() -> None:
    # review P2: on the clarification path an empty→fallback bubble would contradict the
    # ClarificationCard → skip (no corrective emitted) but still clear the flag.
    bus = _Bus(_canvas_pointer_claimed=True, _lead_chat_text="完整报告见画布。")
    fired = await lo._reconcile_canvas_pointer(bus, "t1", "clarification_pending", False)
    assert not fired and _finals(bus) == []
    assert bus._canvas_pointer_claimed is False


async def test_reconcile_updates_persisted_snapshot() -> None:
    # review P1: the corrective must ALSO land in _pi_turn_state, else F5 re-hydrates the
    # phantom from the persisted final_answer column (render≠persist regression).
    bus = _Bus(
        _canvas_pointer_claimed=True,
        _lead_chat_text="分析完成。完整报告见画布。",
        _pi_turn_state={"final_answer": "分析完成。完整报告见画布。"},
    )
    assert await lo._reconcile_canvas_pointer(bus, "t1", "completed", False)
    assert "见画布" not in bus._pi_turn_state["final_answer"]  # persisted value corrected
    assert bus._pi_turn_state["final_answer"] == _finals(bus)[0].payload["text"]  # persist == emit


@pytest.mark.parametrize("already_emitted", [False, True])
async def test_done_persists_exact_preterminal_pointer_correction(
    already_emitted: bool,
) -> None:
    class _EmptyCanvasStore:
        async def list_sections_appended_by_turn(self, _turn_id: str) -> list:
            return []

        async def count_sections_for_turn(self, _turn_id: str) -> int:
            return 0

    persisted: list[dict] = []

    async def persist(**kwargs) -> bool:
        persisted.append(dict(kwargs))
        return True

    phantom = "分析完成。完整报告见画布。"
    bus = _Bus(
        _canvas_pointer_claimed=True,
        _lead_chat_text=phantom,
        _pi_turn_state={"final_answer": phantom},
        _pi_persist_turn_state=persist,
        _final_answer_emitted=already_emitted,
        _done_emitted=False,
        _d6_canvas_store=_EmptyCanvasStore(),
        _d6_turn_id="t_pointer_done",
    )

    await lo._emit_event(
        bus,
        "t_pointer_done",
        "done",
        {"turn_id": "t_pointer_done", "status": "completed"},
    )

    finals = _finals(bus)
    assert len(finals) == 1
    corrected = finals[0].payload["text"]
    assert corrected == "分析完成。"
    assert persisted == [
        {
            "turn_id": "t_pointer_done",
            "status_value": "completed",
            "intent": None,
            "final_answer": corrected,
            "tokens_input": 0,
            "tokens_output": 0,
            "cost_usd": 0.0,
            "error": None,
            "error_kind": None,
        }
    ]
    done = next(event for event in bus.events if event.event_type == "done")
    assert "final_answer" not in done.payload


async def test_lost_terminal_cas_has_no_pointer_or_canvas_side_effects() -> None:
    class _ChatModeCanvasStore:
        def __init__(self) -> None:
            self.hide_calls = 0

        async def list_sections_appended_by_turn(self, _turn_id: str) -> list:
            return [SimpleNamespace(id="sec_1", kind="data_table", payload={})]

        async def mark_turn_hidden_for_chat_mode(
            self,
            _turn_id: str,
            _rationale: str,
        ) -> list[str]:
            self.hide_calls += 1
            return ["sec_1"]

    store = _ChatModeCanvasStore()
    persisted: list[dict] = []

    async def lose_cas(**kwargs) -> bool:
        persisted.append(dict(kwargs))
        return False

    phantom = "分析完成。完整报告见画布。"
    state = {"final_answer": phantom}
    bus = _Bus(
        _canvas_pointer_claimed=True,
        _lead_chat_text=phantom,
        _pi_turn_state=state,
        _pi_persist_turn_state=lose_cas,
        _final_answer_emitted=True,
        _done_emitted=False,
        _d6_canvas_store=store,
        _d6_turn_id="t_stop_won",
    )

    await lo._emit_event(
        bus,
        "t_stop_won",
        "done",
        {"turn_id": "t_stop_won", "status": "completed"},
    )

    assert persisted[0]["final_answer"] == "分析完成。"
    assert state["final_answer"] == phantom
    assert bus.events == []
    assert store.hide_calls == 0
    assert bus._canvas_pointer_claimed is True
    assert not getattr(bus, "_d6_hide_decided", False)


async def test_terminal_surface_read_retries_once_without_duplicate_decision() -> None:
    class _TransientCanvasStore:
        def __init__(self) -> None:
            self.calls = 0

        async def list_sections_appended_by_turn(self, _turn_id: str) -> list:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient read")
            return []

    store = _TransientCanvasStore()
    bus = _Bus(_d6_canvas_store=store, _d6_turn_id="t_retry")

    assert await lo._resolve_terminal_response_kind(bus, "t_retry", "canvas") == "chat"
    assert store.calls == 2
    assert bus.events == []
    assert not getattr(bus, "_d6_hide_decided", False)


async def test_terminal_artifact_read_retries_once_then_keeps_deliverable() -> None:
    class _EmptyCanvasStore:
        async def list_sections_appended_by_turn(self, _turn_id: str) -> list:
            return []

    class _TransientArtifactStore:
        def __init__(self) -> None:
            self.calls = 0

        async def turn_has_deliverable(self, _turn_id: str) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient artifact read")
            return True

    artifacts = _TransientArtifactStore()
    bus = _Bus(
        _d6_canvas_store=_EmptyCanvasStore(),
        _d6_artifact_store=artifacts,
        _d6_turn_id="t_artifact_retry",
    )

    assert (
        await lo._resolve_terminal_response_kind(
            bus,
            "t_artifact_retry",
            "chat",
        )
        == "canvas"
    )
    assert artifacts.calls == 2
    assert bus._terminal_report_kept is True


async def test_terminal_surface_failure_is_not_a_durability_failure() -> None:
    class _BrokenCanvasStore:
        async def list_sections_appended_by_turn(self, _turn_id: str) -> list:
            raise RuntimeError("canvas temporarily unavailable")

    bus = _Bus(
        _d6_canvas_store=_BrokenCanvasStore(),
        _d6_turn_id="t_surface_unavailable",
        _persist_before_live=True,
    )

    assert (
        await lo._resolve_terminal_response_kind(
            bus,
            "t_surface_unavailable",
            "canvas",
        )
        == "canvas"
    )
    assert bus._terminal_report_kept is True
    assert not hasattr(bus, "_fatal_error")


async def test_terminal_artifact_failure_preserves_fallback_without_fatal() -> None:
    class _EmptyCanvasStore:
        async def list_sections_appended_by_turn(self, _turn_id: str) -> list:
            return []

    class _BrokenArtifactStore:
        def __init__(self) -> None:
            self.calls = 0

        async def turn_has_deliverable(self, _turn_id: str) -> bool:
            self.calls += 1
            raise RuntimeError("artifact temporarily unavailable")

    artifacts = _BrokenArtifactStore()
    bus = _Bus(
        _d6_canvas_store=_EmptyCanvasStore(),
        _d6_artifact_store=artifacts,
        _d6_turn_id="t_artifact_unavailable",
        _persist_before_live=True,
    )

    assert (
        await lo._resolve_terminal_response_kind(
            bus,
            "t_artifact_unavailable",
            "canvas",
        )
        == "canvas"
    )
    assert artifacts.calls == 2
    assert bus._terminal_report_kept is True
    assert not hasattr(bus, "_fatal_error")


@pytest.mark.parametrize(
    ("fallback", "report_kept"),
    [("chat", False), ("canvas", True)],
)
async def test_missing_surface_stores_preserve_fallback_and_pointer_fact(
    fallback: str,
    report_kept: bool,
) -> None:
    bus = _Bus()

    assert await lo._resolve_terminal_response_kind(bus, "t_no_store", fallback) == fallback
    assert bus._terminal_report_kept is report_kept


async def test_hide_failure_keeps_sections_and_publishes_canvas() -> None:
    class _BrokenHideStore:
        def __init__(self) -> None:
            self.hide_calls = 0

        async def list_sections_appended_by_turn(self, _turn_id: str) -> list:
            return [SimpleNamespace(id="sec_visible", kind="data_table", payload={})]

        async def mark_turn_hidden_for_chat_mode(
            self,
            _turn_id: str,
            _rationale: str,
        ) -> list[str]:
            self.hide_calls += 1
            raise RuntimeError("hide unavailable")

    persisted: list[dict] = []

    async def persist(**kwargs) -> bool:
        persisted.append(dict(kwargs))
        return True

    store = _BrokenHideStore()
    phantom = "分析完成。完整报告见画布。"
    state = {"final_answer": phantom}
    bus = _Bus(
        _d6_canvas_store=store,
        _d6_turn_id="t_hide_fallback",
        _pi_persist_turn_state=persist,
        _pi_turn_state=state,
        _final_answer_emitted=True,
        _canvas_pointer_claimed=True,
        _lead_chat_text=phantom,
    )

    await lo._emit_event(
        bus,
        "t_hide_fallback",
        "done",
        {"turn_id": "t_hide_fallback", "status": "completed"},
    )

    done = next(event for event in bus.events if event.event_type == "done")
    corrective = _finals(bus)
    assert done.payload["response_kind"] == "canvas"
    assert len(corrective) == 1
    assert corrective[0].payload["text"] == "分析完成。"
    assert persisted[0]["final_answer"] == corrective[0].payload["text"]
    assert state["final_answer"] == corrective[0].payload["text"]
    assert store.hide_calls == 2
    assert bus._terminal_report_kept is True
    assert not [event for event in bus.events if event.event_type == "canvas.sections_hidden"]


async def test_silent_fallback_lost_cas_has_no_state_or_wire_side_effects() -> None:
    async def lose_cas(**_kwargs) -> bool:
        return False

    state = {"final_answer": ""}
    bus = _Bus(
        _pi_turn_state=state,
        _pi_persist_turn_state=lose_cas,
        _final_answer_emitted=False,
        _done_emitted=False,
    )

    await lo._emit_event(
        bus,
        "t_silent_stop_won",
        "done",
        {"turn_id": "t_silent_stop_won", "status": "completed"},
    )

    assert state == {"final_answer": ""}
    assert bus.events == []
    assert bus._done_emitted is False


async def test_post_cas_first_wire_failure_occurs_after_hide() -> None:
    class _ChatModeCanvasStore:
        def __init__(self) -> None:
            self.hidden = False

        async def list_sections_appended_by_turn(self, _turn_id: str) -> list:
            return [SimpleNamespace(id="sec_1", kind="data_table", payload={})]

        async def mark_turn_hidden_for_chat_mode(
            self,
            _turn_id: str,
            _rationale: str,
        ) -> list[str]:
            self.hidden = True
            return ["sec_1"]

    async def persist(**_kwargs) -> bool:
        return True

    async def fail_first_wire(_event) -> None:
        raise RuntimeError("event store unavailable")

    store = _ChatModeCanvasStore()
    bus = TurnEventBus(log_sink=fail_first_wire, persist_before_live=True)
    bus._d6_canvas_store = store  # type: ignore[attr-defined]
    bus._d6_turn_id = "t_hide_before_wire_failure"  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = persist  # type: ignore[attr-defined]
    bus._pi_turn_state = {}  # type: ignore[attr-defined]
    bus._final_answer_emitted = True  # type: ignore[attr-defined]

    with pytest.raises(TerminalDurabilityAbort):
        await lo._emit_event(
            bus,
            "t_hide_before_wire_failure",
            "done",
            {"turn_id": "t_hide_before_wire_failure", "status": "completed"},
        )

    assert store.hidden is True
    assert bus.snapshot() == []
