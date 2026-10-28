"""0.39.8 #4 — `_build_bi_fallback_sections` data_table emit shape.

Pre-0.39.8: composer emitted `columns: list[str]` + `rows: list[list[Any]]`
but FE `data-table.tsx` expects `columns: TableCol[]` (objects with key/
header/numeric) + `rows: TableRow[]` (records keyed by column name). Result:
every cell rendered as `—` em-dash on prod (user-reported, session
s_c788a7a47b turn t_fcc951f042c5). Fix at canvas_composer.py:1340.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bowhead_core.canvas_composer import _build_bi_fallback_sections


@dataclass
class _Inv:
    tool_name: str
    result: dict[str, Any]


def test_data_table_emits_objectified_columns() -> None:
    inv = _Inv(
        tool_name="bi_query",
        result={
            "results": [
                {
                    "ok": True,
                    "query_text": "医院等级 有哪些值",
                    "sample_rows": [
                        {"医院等级": "Tier 3", "销售数量达成率": 0.97},
                        {"医院等级": "Tier 1", "销售数量达成率": 1.05},
                    ],
                }
            ],
        },
    )
    sections = _build_bi_fallback_sections([inv])
    tables = [s for s in sections if s["kind"] == "data_table"]
    assert len(tables) == 1
    table = tables[0]
    # New shape: columns are dicts with key/header, NOT bare strings
    assert isinstance(table["columns"], list)
    assert all(isinstance(c, dict) for c in table["columns"])
    assert table["columns"][0] == {"key": "医院等级", "header": "医院等级", "numeric": False}
    assert table["columns"][1] == {"key": "销售数量达成率", "header": "销售数量达成率", "numeric": True}


def test_data_table_emits_keyed_rows() -> None:
    inv = _Inv(
        tool_name="bi_query",
        result={
            "results": [
                {
                    "ok": True,
                    "query_text": "test",
                    "sample_rows": [
                        {"col_a": "x", "col_b": 1},
                        {"col_a": "y", "col_b": 2},
                    ],
                }
            ],
        },
    )
    sections = _build_bi_fallback_sections([inv])
    table = next(s for s in sections if s["kind"] == "data_table")
    # New shape: rows are records keyed by column name, NOT array-of-arrays
    assert isinstance(table["rows"], list)
    assert all(isinstance(r, dict) for r in table["rows"])
    assert table["rows"][0] == {"col_a": "x", "col_b": 1}
    assert table["rows"][1] == {"col_a": "y", "col_b": 2}


def test_data_table_numeric_detection_skips_empty_first_value() -> None:
    """First row's first column value is empty string — detection should peek
    later rows for the numeric signal (real prod case from sec_00f08ec49602)."""
    inv = _Inv(
        tool_name="bi_query",
        result={
            "results": [
                {
                    "ok": True,
                    "query_text": "test",
                    "sample_rows": [
                        {"col_a": "", "col_b": 9905},
                        {"col_a": "Tier 1", "col_b": 1024},
                    ],
                }
            ],
        },
    )
    sections = _build_bi_fallback_sections([inv])
    table = next(s for s in sections if s["kind"] == "data_table")
    # col_a starts with "" (string) → not numeric
    # col_b first value 9905 → numeric
    by_key = {c["key"]: c for c in table["columns"]}
    assert by_key["col_a"]["numeric"] is False
    assert by_key["col_b"]["numeric"] is True


def test_data_table_empty_rows_skipped() -> None:
    inv = _Inv(
        tool_name="bi_query",
        result={"results": [{"ok": True, "sample_rows": []}]},
    )
    sections = _build_bi_fallback_sections([inv])
    assert all(s["kind"] != "data_table" for s in sections)


def test_data_table_no_invocations_returns_empty() -> None:
    assert _build_bi_fallback_sections(None) == []
    assert _build_bi_fallback_sections([]) == []
