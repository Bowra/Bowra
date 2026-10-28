"""Unit tests for im/turn_render.py — the event->NeutralBlock state machine (teams T3)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.source_linkage_presentation import SourceLinkagePresentation
from bowhead_gateway.im.blocks import (
    Caveat,
    ErrorBlock,
    ImageBlock,
    KpiRow,
    ReportLifecycleLine,
    SourceLinkageLine,
    SourcesBlock,
    TableBlock,
    TextBlock,
)
from bowhead_gateway.im.turn_render import (
    AgentAttribution,
    NeutralTurnState,
    apply_event,
    refresh_report_lifecycle,
    render_blocks,
)


@dataclass
class _Ev:
    event_type: str
    payload: dict[str, Any]


def _drive(turn_id: str, events: list[_Ev]) -> NeutralTurnState:
    state = NeutralTurnState(turn_id=turn_id)
    for ev in events:
        apply_event(state, ev)
    return state


def _kinds(blocks: list[Any]) -> list[str]:
    return [b.kind for b in blocks]


# ─── apply_event ─────────────────────────────────────────────────────────────


def test_canvas_opened_and_sections_accumulate_in_order() -> None:
    state = _drive(
        "t1",
        [
            _Ev("canvas.opened", {"canvas_id": "c1", "title": "Report"}),
            _Ev("canvas.section_added", {"section": {"id": "s1", "kind": "narrative", "markdown": "a"}}),
            _Ev("canvas.section_added", {"section": {"id": "s2", "kind": "kpi_row", "cells": []}}),
            _Ev("canvas.section_done", {"section_id": "s1", "payload": {"id": "s1", "kind": "narrative", "markdown": "final a"}}),
        ],
    )
    assert state.canvas_id == "c1" and state.title == "Report"
    assert state.section_order == ["s1", "s2"]
    assert state.sections["s1"]["markdown"] == "final a"  # section_done updated it


def test_section_removed_and_reordered() -> None:
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_added", {"section": {"id": "s1", "kind": "narrative", "markdown": "a"}}),
            _Ev("canvas.section_added", {"section": {"id": "s2", "kind": "narrative", "markdown": "b"}}),
            _Ev("canvas.section_added", {"section": {"id": "s3", "kind": "narrative", "markdown": "c"}}),
            _Ev("canvas.reordered", {"order": ["s3", "s1", "s2"]}),
            _Ev("canvas.section_removed", {"section_id": "s1"}),
        ],
    )
    assert state.section_order == ["s3", "s2"]
    assert "s1" not in state.sections


def test_section_done_merge_preserves_kind_from_added() -> None:
    # Real emit: section_added carries id+kind (to_report_section); section_done's inner payload
    # often omits kind. Merge (not replace) must keep the kind so trust classification still works.
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_added", {"section": {"id": "s1", "kind": "sources", "entries": [{"label": "a"}]}}),
            _Ev("canvas.section_done", {"section_id": "s1", "payload": {"entries": [{"label": "a"}, {"label": "b"}]}}),
        ],
    )
    sec = state.sections["s1"]
    assert sec["kind"] == "sources"  # preserved from section_added (not lost on done)
    assert len(sec["entries"]) == 2  # final payload merged in


def test_section_done_uses_top_level_kind_when_no_prior_add() -> None:
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_done", {"section_id": "s9", "kind": "narrative", "payload": {"markdown": "x"}}),
        ],
    )
    assert state.sections["s9"]["kind"] == "narrative"
    assert state.sections["s9"]["id"] == "s9"


def test_citation_final_progress_terminal() -> None:
    state = _drive(
        "t1",
        [
            _Ev("turn.citation_registered", {"citation_seq": 1, "artifact_id": "a1", "label": "L", "kind": "data_ref"}),
            _Ev("headline", {"text": "thinking"}),
            _Ev("step_progress", {"title": "running query"}),
            _Ev("final_answer", {"text": "hi"}),
            _Ev("done", {"status": "completed"}),
        ],
    )
    assert state.citations[1] == {"kind": "data_ref"}
    assert state.status_text == "running query"  # latest progress wins
    assert state.final_text == "hi"
    assert state.terminal is True and state.terminal_status == "completed"


def test_unknown_event_ignored() -> None:
    state = _drive("t1", [_Ev("some.unknown.event", {"x": 1})])
    assert state.section_order == [] and state.final_text is None


def test_agent_attribution_folds_first_nonempty_across_live_and_replay_shapes() -> None:
    state = _drive(
        "t1",
        [
            _Ev(
                "peer_dispatched",
                {
                    "spawn_id": "sub_1",
                    "agent_id": "osa_sales",
                    "agent_revision_id": "arv_1",
                    "agent_execution_id": "aex_1",
                },
            ),
            _Ev(
                "agent_spawned",
                {
                    "subagent_id": "sub_1",
                    "agent_id": "osa_conflict",
                    "agent_release_id": "arl_1",
                    "display_name_snapshot": "销售专家",
                },
            ),
            _Ev("agent_spawned", {"subagent_id": "legacy", "profile": "compute"}),
        ],
    )
    assert state.agent_attribution == {
        "sub_1": AgentAttribution(
            agent_id="osa_sales",
            agent_revision_id="arv_1",
            agent_release_id="arl_1",
            agent_execution_id="aex_1",
            display_name_snapshot="销售专家",
        )
    }


@pytest.mark.parametrize(
    "event_type",
    ["turn.effect_disclosure_settled", "turn.llm_invocation"],
)
def test_reader_reserved_effect_events_are_not_business_blocks(
    event_type: str,
) -> None:
    state = _drive("t1", [_Ev(event_type, {"protocol": 2})])
    assert state == NeutralTurnState(turn_id="t1")


def test_terminal_fold_renders_localized_error_and_safe_diagnostics() -> None:
    state = _drive(
        "t1",
        [
            _Ev(
                "turn_failed",
                {
                    "reason": "wallclock_exceeded",
                    "diagnostics": {
                        "schema_version": 1,
                        "disclosure": "authenticated",
                        "diagnostic_code": "chat_turn_failed",
                        "occurrence_id": "0123456789abcdef0123456789abcdef",
                        "turn_id": "t1",
                        "safe_detail": "phase=chat; failure_class=timeout",
                        "retryable": True,
                    },
                },
            )
        ],
    )
    assert state.terminal is True
    assert state.terminal_status == "wallclock_exceeded"
    blocks = render_blocks(state, locale="en-US")
    assert len(blocks) == 1
    error = blocks[0]
    assert isinstance(error, ErrorBlock)
    assert error.message == "This turn reached its time limit. Try narrowing the scope or splitting the work into separate requests."
    assert error.code == "wallclock_exceeded"
    assert error.occurrence_id == "0123456789abcdef0123456789abcdef"
    assert error.turn_id == "t1"
    assert error.safe_detail == "phase=chat; failure_class=timeout"
    assert error.retryable is True


def test_terminal_fold_rejects_raw_reason_and_untrusted_diagnostics() -> None:
    secret = "HTTP 500 password=raw-secret"
    state = _drive(
        "t1",
        [
            _Ev(
                "turn_failed",
                {
                    "reason": secret,
                    "diagnostics": {
                        "schema_version": 1,
                        "disclosure": "authenticated",
                        "diagnostic_code": "chat_turn_failed",
                        "occurrence_id": "0123456789abcdef0123456789abcdef",
                        "turn_id": "another-turn",
                        "safe_detail": secret,
                    },
                },
            )
        ],
    )

    error = render_blocks(state, locale="zh-CN")[0]
    assert isinstance(error, ErrorBlock)
    assert error.code == "lead_internal_error"
    assert error.message == "本轮处理时发生系统错误，请稍后重试。"
    assert error.occurrence_id is None
    assert error.safe_detail is None
    assert secret not in repr(error)


def test_terminal_fold_accepts_typed_llm_failure_without_provider_prose() -> None:
    state = _drive(
        "t1",
        [
            _Ev(
                "turn_failed",
                {
                    "reason": "llm_provider_unavailable",
                    "diagnostics": {
                        "schema_version": 1,
                        "disclosure": "authenticated",
                        "diagnostic_code": "chat_llm_failed",
                        "occurrence_id": "0123456789abcdef0123456789abcdef",
                        "turn_id": "t1",
                        "safe_detail": (
                            "phase=invoke; failure_class=upstream; "
                            "provider_family=openai_compatible; http_status=500; "
                            "stream_state=pre_visible; fallback=failed; attempts=3"
                        ),
                        "retryable": True,
                    },
                },
            )
        ],
    )

    error = render_blocks(state, locale="en-US")[0]
    assert isinstance(error, ErrorBlock)
    assert error.code == "llm_provider_unavailable"
    assert error.message == "The model service is temporarily unavailable. This turn could not finish. Try again later."
    assert error.safe_detail == (
        "phase=invoke; failure_class=upstream; provider_family=openai_compatible; "
        "http_status=500; stream_state=pre_visible; fallback=failed; attempts=3"
    )


def test_user_cancel_is_localized_without_error_disclosure() -> None:
    state = _drive(
        "t1",
        [
            _Ev(
                "turn_cancelled",
                {
                    "turn_id": "t1",
                    "duration_ms": 25,
                },
            )
        ],
    )

    blocks = render_blocks(state, locale="en-US")
    assert blocks == [TextBlock(markdown="Cancelled.")]
    assert state.failure_code is None


# ─── render_blocks: chat mode ────────────────────────────────────────────────


def test_chat_mode_renders_single_resolved_textblock_no_trust() -> None:
    state = _drive(
        "t1",
        [
            _Ev("final_answer", {"text": "see [cite:1] and [num:5:count]"}),
            _Ev("done", {"status": "completed"}),
        ],
    )
    blocks = render_blocks(state)
    assert _kinds(blocks) == ["text"]
    assert isinstance(blocks[0], TextBlock)
    assert "[1]" in blocks[0].markdown and "5" in blocks[0].markdown
    assert "[cite:" not in blocks[0].markdown  # R-1: no raw marker


def test_progress_line_while_live_then_gone_at_terminal() -> None:
    live = _drive("t1", [_Ev("headline", {"text": "thinking"})])
    blocks = render_blocks(live)
    assert _kinds(blocks) == ["text"] and blocks[0].markdown.startswith("⏳")

    done = _drive(
        "t1",
        [
            _Ev("headline", {"text": "thinking"}),
            _Ev("final_answer", {"text": "answer"}),
            _Ev("done", {}),
        ],
    )
    blocks2 = render_blocks(done)
    assert _kinds(blocks2) == ["text"] and blocks2[0].markdown == "answer"  # no ⏳ at terminal


# ─── render_blocks: report mode ──────────────────────────────────────────────


def _report_state() -> NeutralTurnState:
    return _drive(
        "t1",
        [
            _Ev("canvas.opened", {"canvas_id": "c1"}),
            _Ev(
                "canvas.section_added",
                {
                    "section": {
                        "id": "s1",
                        "kind": "narrative",
                        "markdown": "营收 34.71万元 [1] 增长",
                        "unverified_numbers": ["毛估 X"],
                    }
                },
            ),
            _Ev(
                "canvas.section_added",
                {
                    "section": {
                        "id": "s2",
                        "kind": "kpi_row",
                        "cells": [{"lbl": "GMV", "val": "100 [2]", "sub": "+5%"}],
                    }
                },
            ),
            _Ev(
                "canvas.section_added",
                {
                    "section": {
                        "id": "s3",
                        "kind": "data_table",
                        "caption": "明细",
                        "columns": [{"key": "a", "header": "A"}, {"key": "b", "header": "B"}],
                        "rows": [{"a": "x", "b": 5}, {"a": "y", "b": {"delta": "+3", "pos": True}}],
                    }
                },
            ),
            _Ev(
                "canvas.section_added",
                {
                    "section": {
                        "id": "s4",
                        "kind": "sources",
                        "entries": [{"label": "src1"}, {"label": "src2"}],
                    }
                },
            ),
            _Ev("done", {"status": "completed"}),
        ],
    )


def test_report_mode_trust_caveat_and_section_blocks() -> None:
    blocks = render_blocks(_report_state())
    kinds = _kinds(blocks)
    # source-linkage line first, caveat next, then sections in order
    assert kinds[0] == "source_linkage"
    assert "caveat" in kinds
    assert "kpi" in kinds and "table" in kinds and "sources" in kinds

    linkage = blocks[0]
    assert isinstance(linkage, SourceLinkageLine)
    assert linkage.presentation == SourceLinkagePresentation(
        citation_count=2,
        figure_review_count=1,
        source_count=2,
    )

    caveat = next(b for b in blocks if isinstance(b, Caveat))
    assert caveat.items == ("毛估 X",)

    narrative = next(b for b in blocks if isinstance(b, TextBlock))
    # citation-audit M1-4: per-number ✓ retired with gen2 (source-linkage summary carries neutral semantics);
    # preserve [N] unchanged (SourcesBlock reverse lookup), with no marker syntax leaked.
    assert "[1]" in narrative.markdown and "[num:" not in narrative.markdown

    table = next(b for b in blocks if isinstance(b, TableBlock))
    assert table.columns == ("A", "B")
    assert table.rows[0] == ("x", "5")
    assert table.rows[1] == ("y", "+3")  # delta cell stringified

    kpi = next(b for b in blocks if isinstance(b, KpiRow))
    # M1-4: KPI per-number ✓ likewise retired with gen2; pass value text through unchanged.
    assert kpi.cells[0].label == "GMV" and kpi.cells[0].value == "100 [2]"

    sources = next(b for b in blocks if isinstance(b, SourcesBlock))
    assert [e.n for e in sources.entries] == [1, 2]


def test_image_section_needs_resolver() -> None:
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_added", {"section": {"id": "i1", "kind": "image", "artifact_id": "art_1", "caption": "fig"}}),
            _Ev("done", {}),
        ],
    )
    # no resolver → image skipped (still no markers, but trust may be silent → empty)
    assert all(not isinstance(b, ImageBlock) for b in render_blocks(state))
    # with resolver → ImageBlock
    blocks = render_blocks(state, image_url_for=lambda aid: f"https://cdn/{aid}.png")
    img = next(b for b in blocks if isinstance(b, ImageBlock))
    assert img.url == "https://cdn/art_1.png" and img.caption == "fig"


def test_section_kind_heading_renders() -> None:
    # the streaming narrator emits headings as kind="section" with the title under h1/h2/h3 (review P1)
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_added", {"section": {"id": "s1", "kind": "section", "h2": "营收分析"}}),
            _Ev("canvas.section_added", {"section": {"id": "s2", "kind": "narrative", "markdown": "正文"}}),
            _Ev("done", {}),
        ],
    )
    blocks = render_blocks(state)
    headings = [b.markdown for b in blocks if isinstance(b, TextBlock)]
    assert any("营收分析" in h for h in headings)
    assert any(h.startswith("##") for h in headings)  # h2 → "## "


def test_data_table_includes_total_row() -> None:
    state = _drive(
        "t1",
        [
            _Ev(
                "canvas.section_added",
                {
                    "section": {
                        "id": "t",
                        "kind": "data_table",
                        "caption": "明细",
                        "columns": [{"key": "a", "header": "A"}, {"key": "b", "header": "B"}],
                        "rows": [{"a": "华东", "b": 6}],
                        "totalRow": {"a": "合计", "b": 10},
                    }
                },
            ),
            _Ev("done", {}),
        ],
    )
    table = next(b for b in render_blocks(state) if isinstance(b, TableBlock))
    assert table.rows[0] == ("华东", "6")
    assert table.rows[-1] == ("合计", "10")  # totalRow appended (review P1)


def test_unknown_section_kind_text_fallback() -> None:
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_added", {"section": {"id": "u1", "kind": "weird_widget", "markdown": "fallback [num:7:count] text"}}),
            _Ev("done", {}),
        ],
    )
    blocks = render_blocks(state)
    txt = next(b for b in blocks if isinstance(b, TextBlock))
    assert "7" in txt.markdown and "[num:" not in txt.markdown


# ─── authoritative trust from canvas.done (teams trust-fix) ──────────────────


def test_authoritative_trust_from_canvas_done_overrides_fold() -> None:
    # The real dogfood bug: the narrative carrying [N] refs was dropped from the lossy fold, so the
    # fold would compute 0/0. canvas.done carries the compose-time authoritative trust → render it.
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_added", {"section": {"id": "s4", "kind": "sources", "entries": [{"label": "a"}, {"label": "b"}]}}),
            # NOTE: no narrative section reaches the fold (dropped) → fold trust would be 0 verified
            _Ev("canvas.done", {"trust": {"verified": 26, "unverified": 0, "sources": 19, "verified_pct": 100}}),
            _Ev("done", {}),
        ],
    )
    linkage = next(b for b in render_blocks(state) if isinstance(b, SourceLinkageLine))
    assert linkage.presentation == SourceLinkagePresentation(
        citation_count=26,
        figure_review_count=0,
        source_count=19,
    )


def test_trust_falls_back_to_fold_without_canvas_done() -> None:
    # back-compat: no canvas.done trust (older composer) → re-derive from the folded sections.
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_added", {"section": {"id": "s1", "kind": "narrative", "markdown": "营收 100元 [1] 增长"}}),
            _Ev("canvas.section_added", {"section": {"id": "s4", "kind": "sources", "entries": [{"label": "a"}]}}),
            _Ev("done", {}),
        ],
    )
    linkage = next(b for b in render_blocks(state) if isinstance(b, SourceLinkageLine))
    assert linkage.presentation.citation_count == 1  # fold counted the valid [N]
    assert linkage.presentation.figure_review_count == 0


def test_malformed_canvas_done_trust_does_not_regain_certainty_from_lossy_fold() -> None:
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_added", {"section": {"id": "s4", "kind": "sources", "entries": [{"label": "a"}]}}),
            _Ev("canvas.done", {"trust": {"verified": "oops"}}),
            _Ev("done", {}),
        ],
    )
    assert not any(isinstance(block, SourceLinkageLine) for block in render_blocks(state))


def test_partial_authoritative_source_linkage_preserves_valid_siblings() -> None:
    state = _drive(
        "t1",
        [
            _Ev("canvas.section_added", {"section": {"id": "s1", "kind": "narrative", "markdown": "x"}}),
            _Ev("canvas.done", {"trust": {"verified": "oops", "unverified": 2, "sources": True, "verified_pct": 100}}),
            _Ev("done", {}),
        ],
    )
    linkage = next(block for block in render_blocks(state) if isinstance(block, SourceLinkageLine))
    assert linkage.presentation == SourceLinkagePresentation(
        citation_count=None,
        figure_review_count=2,
        source_count=None,
    )


def test_lifecycle_is_a_separate_finite_block() -> None:
    state = _report_state()
    state.lifecycle = "verified"
    blocks = render_blocks(state, locale="en-US")
    linkage_index = next(i for i, block in enumerate(blocks) if isinstance(block, SourceLinkageLine))
    lifecycle_index = next(i for i, block in enumerate(blocks) if isinstance(block, ReportLifecycleLine))
    assert lifecycle_index == linkage_index + 1
    assert blocks[lifecycle_index].effective_status == "verified"

    state.lifecycle = "attacker-controlled"
    assert not any(isinstance(block, ReportLifecycleLine) for block in render_blocks(state))


@pytest.mark.anyio
async def test_refresh_lifecycle_distinguishes_quarantine_and_fails_closed() -> None:
    class Store:
        async def get_canvas(self, canvas_id: str):
            return SimpleNamespace(status="verified")

        async def count_quarantined(self, canvas_id: str) -> int:
            return 1

    state = NeutralTurnState(turn_id="t1", canvas_id="c1")
    await refresh_report_lifecycle(state, Store())
    assert state.lifecycle == "quarantined_stale"

    class BrokenStore:
        async def get_canvas(self, canvas_id: str):
            raise RuntimeError("private failure")

    await refresh_report_lifecycle(state, BrokenStore())
    assert state.lifecycle is None


def test_canvas_section_patched_upserts_and_skips_failures() -> None:
    # canvas-comprehensive WP-4 (B2/B3): write_report UPDATE + sources-repatch emit
    # canvas.section_patched (not section_added). The mirror MUST upsert by id or the
    # IM card silently stops reflecting report updates / [N] source-number fixes.
    state = _drive(
        "t1",
        [
            _Ev("canvas.opened", {"canvas_id": "c1", "title": "R"}),
            _Ev("canvas.section_added", {"section": {"id": "s1", "kind": "narrative", "markdown": "v1"}}),
            # update path patches s1 with the full section (late-join safe) → merge
            _Ev(
                "canvas.section_patched",
                {"section_id": "s1", "kind": "narrative", "patch": {"id": "s1", "kind": "narrative", "markdown": "v2"}, "rev": 2},
            ),
            # late-join: patched arrives for a section the mirror never saw → insert
            _Ev(
                "canvas.section_patched",
                {"section_id": "s9", "kind": "sources", "patch": {"id": "s9", "kind": "sources", "entries": [{"n": 1}]}, "rev": 1},
            ),
            # failure pill (ok=False) → skipped, no data
            _Ev("canvas.section_patched", {"section_id": "s1", "ok": False, "error": "boom"}),
        ],
    )
    assert state.sections["s1"]["markdown"] == "v2"  # merged update reflected
    assert state.sections["s9"]["kind"] == "sources"  # late-join inserted
    assert state.section_order == ["s1", "s9"]  # order preserved, s9 appended


# ─── canvas-update-turn B1-6: revise-turn mirror ─────────────────────────────


def test_revise_turn_title_line_and_seq_insertion() -> None:
    """opened(revised) + two patched prior-turn sections arriving out of seq order ⇒ leading
    「《标题》已更新」 line, sections placed by canvas seq; a this-turn section_added keeps arrival order."""
    state = _drive(
        "t2",
        [
            _Ev("canvas.section_added", {"section": {"id": "src", "kind": "sources", "entries": []}, "seq": 9}),
            _Ev("canvas.opened", {"canvas_id": "c1", "session_id": "s1", "title": "全年销售分析", "revised": True}),
            _Ev(
                "canvas.section_patched",
                {"section_id": "n2", "kind": "narrative", "patch": {"id": "n2", "kind": "narrative", "markdown": "二"}, "rev": 2, "seq": 6},
            ),
            _Ev(
                "canvas.section_patched",
                {"section_id": "n1", "kind": "narrative", "patch": {"id": "n1", "kind": "narrative", "markdown": "一"}, "rev": 2, "seq": 4},
            ),
            _Ev(
                "canvas.section_patched",
                {"section_id": "noseq", "kind": "narrative", "patch": {"id": "noseq", "kind": "narrative", "markdown": "?"}, "rev": 2},
            ),
        ],
    )
    assert state.revised is True
    assert state.section_order == ["n1", "n2", "src", "noseq"]  # by seq; no-seq appends
    blocks = render_blocks(state, locale="zh-CN")
    texts = [b.markdown for b in blocks if isinstance(b, TextBlock)]
    assert texts[0] == "**《全年销售分析》已更新**" and texts[1:3] == ["一", "二"]
    en = render_blocks(state, locale="en-US")
    assert next(b.markdown for b in en if isinstance(b, TextBlock)) == "**“全年销售分析” updated**"
    de = render_blocks(state, locale="de-DE")
    assert next(b.markdown for b in de if isinstance(b, TextBlock)) == "**„全年销售分析“ wurde aktualisiert**"
    fr = render_blocks(state, locale="fr-FR")
    assert next(b.markdown for b in fr if isinstance(b, TextBlock)) == "**« 全年销售分析 » a été mis à jour**"


def test_revise_title_line_absent_when_card_has_h1_or_not_revised() -> None:
    with_h1 = _drive(
        "t2",
        [
            _Ev("canvas.opened", {"canvas_id": "c1", "title": "T", "revised": True}),
            _Ev("canvas.section_added", {"section": {"id": "h", "kind": "h1", "text": "T"}, "seq": 1}),
        ],
    )
    assert not any(isinstance(b, TextBlock) and "已更新" in b.markdown for b in render_blocks(with_h1))
    plain = _drive(
        "t1",
        [
            _Ev("canvas.opened", {"canvas_id": "c1", "title": "T"}),
            _Ev(
                "canvas.section_patched",
                {"section_id": "n1", "kind": "narrative", "patch": {"id": "n1", "kind": "narrative", "markdown": "x"}, "rev": 2, "seq": 4},
            ),
        ],
    )
    assert plain.revised is False and not any(isinstance(b, TextBlock) and "已更新" in b.markdown for b in render_blocks(plain))


def test_patched_after_reordered_does_not_override_reorder() -> None:
    state = _drive(
        "t2",
        [
            _Ev("canvas.section_added", {"section": {"id": "a", "kind": "narrative", "markdown": "a"}, "seq": 5}),
            _Ev("canvas.section_added", {"section": {"id": "b", "kind": "narrative", "markdown": "b"}, "seq": 6}),
            _Ev("canvas.reordered", {"order": ["b", "a"]}),
            _Ev(
                "canvas.section_patched",
                {"section_id": "p", "kind": "narrative", "patch": {"id": "p", "kind": "narrative", "markdown": "p"}, "rev": 2, "seq": 1},
            ),
        ],
    )
    assert state.section_order == ["b", "a", "p"]  # reordered order wins; seq-1 section appends


def test_ordinary_report_event_stream_order_unchanged() -> None:
    """Replay of a normal new-report stream (added first, then patched/done) ⇒ arrival order, byte-identical."""
    events = [
        _Ev("canvas.opened", {"canvas_id": "c1", "title": "R"}),
        _Ev("canvas.section_added", {"section": {"id": "h", "kind": "h1", "text": "R"}, "seq": 2}),
        _Ev("canvas.section_added", {"section": {"id": "e", "kind": "eyebrow", "text": "x"}, "seq": 1}),
        _Ev("canvas.section_added", {"section": {"id": "n", "kind": "narrative", "markdown": "n"}, "seq": 7}),
        _Ev("canvas.section_added", {"section": {"id": "c", "kind": "chart", "spec": {}}, "seq": 3}),
        _Ev(
            "canvas.section_patched",
            {"section_id": "n", "kind": "narrative", "patch": {"id": "n", "kind": "narrative", "markdown": "n2"}, "rev": 2, "seq": 7},
        ),
        _Ev("canvas.section_done", {"section_id": "c", "payload": {"spec": {"title": "t"}}}),
    ]
    assert _drive("t1", events).section_order == ["h", "e", "n", "c"]


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en-US", "The question was not created"),
        ("zh-CN", "这次问题没有创建"),
        ("de-DE", "Die Frage wurde nicht erstellt"),
        ("fr-FR", "La question n’a pas été créée"),
    ],
)
def test_native_capture_failure_keeps_stage_and_correlation_in_im(locale: str, expected: str) -> None:
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
    from bowhead_core.published_diagnostics import get_or_build_chat_turn_failure

    failure = get_or_build_chat_turn_failure(
        carrier=OrdinaryContinuationUnavailable(stage="clarification_capture"), reason_code="clarification_resume_unavailable", turn_id="t_capture"
    )
    state = _drive("t_capture", [_Ev("turn_failed", {"reason": failure.reason_code, "diagnostics": failure.diagnostics})])
    error = render_blocks(state, locale=locale)[0]
    assert isinstance(error, ErrorBlock)
    assert expected in error.message
    assert error.code == "clarification_resume_unavailable"
    assert error.safe_detail == "phase=clarification_capture; failure_class=configuration"
    assert error.occurrence_id == failure.occurrence_id and error.turn_id == "t_capture"
    assert error.retryable is False


def test_im_does_not_infer_clarification_stage_from_another_parent() -> None:
    state = _drive(
        "t_other",
        [
            _Ev(
                "turn_failed",
                {
                    "reason": "lead_internal_error",
                    "diagnostics": {
                        "schema_version": 1,
                        "disclosure": "authenticated",
                        "diagnostic_code": "chat_turn_failed",
                        "turn_id": "t_other",
                        "safe_detail": "phase=clarification_capture; failure_class=configuration",
                        "retryable": False,
                    },
                },
            )
        ],
    )
    error = render_blocks(state, locale="en-US")[0]
    assert isinstance(error, ErrorBlock)
    assert error.message == "A system error interrupted this turn. Please try again later."
    assert error.safe_detail is None
