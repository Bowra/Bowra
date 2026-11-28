"""Document matrix step 1 (S5): how one snapshot cell becomes a spreadsheet value and a status.

The route-level workbook (sheets, fills, hidden rows, formulas, language) is covered by
``packages/bowhead_gateway/tests/test_kb_matrix_export.py``; this file pins the per-cell rules.
"""

from __future__ import annotations

from datetime import date

import pytest
from bowhead_core.kb.matrix_export import MAX_CELL_TEXT_CHARS, _location_label, answer_value, cell_status, export_copy, sheet_text

EN = export_copy("en-US")


def _cell(normalized, *, status: str = "done", raw=None, **flags) -> dict:
    return {"status": status, "variants": [{"is_primary": True, "normalized": normalized, "raw": raw}], **flags}


@pytest.mark.parametrize(
    ("column_type", "normalized", "raw", "expected"),
    [
        ("currency", {"amount": "286000", "currency": None}, None, 286000),
        ("number", {"amount": "12.5", "currency": None}, None, 12.5),
        # Excel keeps 15 significant digits: a longer number stays text rather than change a digit.
        ("number", {"amount": "1234567890123456789", "currency": None}, None, "1234567890123456789"),
        ("number", {"value": "1,024"}, None, 1024),
        ("date", {"value": "2026-03-01"}, None, date(2026, 3, 1)),
        # Spreadsheet dates start in 1900; earlier ones and free text stay text.
        ("date", {"value": "1899-12-31"}, None, "1899-12-31"),
        ("boolean", {"flag": False}, None, False),
        ("select", {"choice": "Active"}, None, "Active"),
        ("multi_select", {"choices": ["IT", "HR"]}, None, "IT, HR"),
        ("number", {"raw_string": "about 5"}, None, "about 5"),
        # Kept as text: no normalized value, the original text in raw.
        ("number", None, "roughly 5 million", "roughly 5 million"),
        ("question", None, {"k": 1}, '{"k": 1}'),
        ("question", None, None, None),
    ],
)
def test_answers_are_typed_by_their_column(column_type, normalized, raw, expected) -> None:
    value = answer_value(_cell(normalized, raw=raw), {"type": column_type}, EN)
    assert value == expected
    assert type(value) is type(expected)


def test_only_finished_cells_have_an_answer() -> None:
    for status in ("queued", "running", "failed"):
        assert answer_value(_cell({"value": "old"}, status=status), {"type": "question"}, EN) is None
    assert answer_value(None, {"type": "question"}, EN) is None
    assert answer_value(_cell({"raw_string": "x"}, status="needs_review"), {"type": "number"}, EN) == "x"


def test_status_takes_the_first_that_applies() -> None:
    assert cell_status(_cell({"value": "v"}), row_missing=True) == "unavailable"
    assert cell_status(None, row_missing=False) == "not_run"
    assert cell_status(_cell(None, status="queued"), row_missing=False) == "running"
    assert cell_status(_cell(None, status="failed", verified=True), row_missing=False) == "failed"
    assert cell_status(_cell({"value": "v"}, stale=True, verified=True), row_missing=False) == "stale"
    assert cell_status(_cell({"value": "v"}, edited=True, verified=True), row_missing=False) == "verified"
    assert cell_status(_cell({"value": "v"}, edited=True), row_missing=False) == "edited"
    assert cell_status(_cell({"raw_string": "v"}, status="needs_review"), row_missing=False) == "needs_review"
    assert cell_status(_cell({"value": "v"}), row_missing=False) == "not_verified"


def test_sheet_text_drops_illegal_characters_and_cuts_with_a_marker() -> None:
    assert sheet_text("a\x00b\x1fc", EN) == "abc"
    cut = sheet_text("x" * (MAX_CELL_TEXT_CHARS + 1), EN)
    assert len(cut) == MAX_CELL_TEXT_CHARS and cut.endswith(EN["truncated"])
    assert sheet_text("x" * MAX_CELL_TEXT_CHARS, EN) == "x" * MAX_CELL_TEXT_CHARS


def test_unknown_languages_get_english_and_every_language_has_every_word() -> None:
    assert export_copy("ja-JP") is export_copy("en-US")
    keys = set(export_copy("en-US"))
    for language in ("zh-CN", "de-DE", "fr-FR"):
        assert set(export_copy(language)) == keys, language


@pytest.mark.parametrize(
    ("variant", "language", "expected"),
    [
        ({"page": 5, "chunk_index": 2}, "en-US", "Page 5"),
        ({"page": None, "chunk_index": 3}, "en-US", "Paragraph 3"),
        ({"chunk_index": 3}, "zh-CN", "第 3 段"),
        ({"chunk_index": 3}, "de-DE", "Absatz 3"),
        ({"chunk_index": 3}, "fr-FR", "Paragraphe 3"),
        ({"page": True, "chunk_index": 0}, "en-US", None),
        ({}, "en-US", None),
        (None, "en-US", None),
    ],
)
def test_location_prefers_the_page_then_the_paragraph(variant, language, expected) -> None:
    assert _location_label(export_copy(language), variant) == expected
