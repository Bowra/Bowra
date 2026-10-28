"""批 C (report-edit-audit P2-1) — 标题/eyebrow 词边界截断."""

from __future__ import annotations

from bowhead_core.report_writer_loop import _truncate_heading


def test_short_titles_pass_through() -> None:
    assert _truncate_heading("2026 年报告") == "2026 年报告"
    assert _truncate_heading("a" * 160) == "a" * 160


def test_english_truncates_at_word_boundary_with_ellipsis() -> None:
    # Observed bug: [:80] cut "Traffic" to "Traffi" without ellipsis.
    title = ("2026 Jan-Jul Multi-Dimensional Performance Report covering Revenue Profit Rate " * 3).strip()
    out = _truncate_heading(title)
    assert out.endswith("…")
    assert len(out) <= 161
    # Word boundary: strip ellipsis, ends complete word (next original character space).
    stem = out[:-1]
    assert title.startswith(stem)
    assert title[len(stem)] == " "


def test_cjk_without_spaces_hard_cuts_with_ellipsis() -> None:
    title = "季" * 200
    out = _truncate_heading(title)
    assert out == "季" * 160 + "…"


def test_strips_whitespace() -> None:
    assert _truncate_heading("  标题  ") == "标题"
