"""kb-doctype-config Day 1: XlsxLoader + island detection unit tests.

Tech design v1.0 §3.1 — 8 cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import openpyxl
from bowhead_core.kb.loader.xlsx_loader import (
    XlsxLoader,
    _detect_header_offset,
    _detect_islands,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_xlsx(path: Path, sheets: dict[str, list[list]]) -> None:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r, row in enumerate(rows, 1):
            for c, val in enumerate(row, 1):
                ws.cell(r, c, val)
    wb.save(path)


# ─── Pure function tests ────────────────────────


def test_detect_islands_empty_separator() -> None:
    rows = [
        ["a", "b"],
        ["1", "2"],
        [None, None],  # separator
        ["x", "y"],
        ["3", "4"],
    ]
    islands = _detect_islands(rows)
    assert len(islands) == 2
    assert len(islands[0]) == 2
    assert len(islands[1]) == 2


def test_detect_islands_strips_blank_strings() -> None:
    rows = [
        ["a"],
        ["", "  ", None],  # all blank → separator
        ["b"],
    ]
    islands = _detect_islands(rows)
    assert len(islands) == 2


def test_detect_header_offset_with_title() -> None:
    """1st row is single-cell title → header is row 1."""
    island = [
        ["无形资产摊销表", None, None],
        ["序号", "名称", "金额"],
        [1, "X", 100],
    ]
    assert _detect_header_offset(island) == 1


def test_detect_header_offset_default() -> None:
    """Multi-cell row 0 → header is row 0."""
    island = [
        ["序号", "名称", "金额"],
        [1, "X", 100],
    ]
    assert _detect_header_offset(island) == 0


# ─── Loader integration tests ──────────────────


async def test_xlsx_loader_single_sheet_single_island(tmp_path) -> None:
    path = tmp_path / "single.xlsx"
    _write_xlsx(
        path,
        {
            "Sheet1": [
                ["序号", "名称", "金额"],
                [1, "项目A", 1000],
                [2, "项目B", 2000],
            ],
        },
    )
    result = await XlsxLoader().load(str(path))
    assert result.layer_used == "L0"
    assert result.metadata.get("sheet_count") == 1
    assert result.metadata.get("island_count") == 1
    # markdown should contain header pipes
    md = result.documents[0].page_content
    assert "| 序号 | 名称 | 金额 |" in md
    assert "项目A" in md


async def test_xlsx_loader_multi_island(tmp_path) -> None:
    path = tmp_path / "multi.xlsx"
    _write_xlsx(
        path,
        {
            "Sheet1": [
                ["A", "B"],
                [1, 2],
                [None, None],  # separator
                ["X", "Y"],
                [3, 4],
                [None, None],
                ["序号", "名称", "金额"],
                [5, "P", 100],
            ],
        },
    )
    result = await XlsxLoader().load(str(path))
    assert result.metadata.get("island_count") == 3
    assert len(result.documents) == 3
    # 3rd island has the 序号/名称/金额 header
    last_md = result.documents[-1].page_content
    assert "序号" in last_md and "名称" in last_md


async def test_xlsx_loader_multi_sheet(tmp_path) -> None:
    path = tmp_path / "multi_sheet.xlsx"
    _write_xlsx(
        path,
        {
            "S1": [["a", "b"], [1, 2]],
            "S2": [["c", "d"], [3, 4]],
        },
    )
    result = await XlsxLoader().load(str(path))
    assert result.metadata.get("sheet_count") == 2
    assert result.metadata.get("island_count") == 2
    sheets_in_md = {d.metadata.get("sheet") for d in result.documents}
    assert sheets_in_md == {"S1", "S2"}


async def test_xlsx_loader_title_row(tmp_path) -> None:
    """Title row (single non-empty cell) → markdown has ## title heading."""
    path = tmp_path / "titled.xlsx"
    _write_xlsx(
        path,
        {
            "Sheet1": [
                ["无形资产摊销表", None, None, None],
                ["序号", "资产名称", "原值", "摊销年限"],
                [1, "数据中台", 530000, "10年"],
            ],
        },
    )
    result = await XlsxLoader().load(str(path))
    md = result.documents[0].page_content
    assert "## 无形资产摊销表" in md
    assert "| 序号 | 资产名称 | 原值 | 摊销年限 |" in md


async def test_xlsx_loader_corrupt_xlsx(tmp_path) -> None:
    """Non-xlsx bytes → parse_failed=True (graceful)."""
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"not_a_valid_xlsx")
    result = await XlsxLoader().load(str(path))
    assert result.metadata.get("parse_failed") is True


async def test_xlsx_loader_big_island_split_into_chunks(tmp_path, monkeypatch) -> None:
    """Big island > MAX_ROWS_PER_ISLAND_CHUNK → split into chunks, header repeated."""
    monkeypatch.setenv("BOWHEAD_KB_XLSX_ROWS_PER_CHUNK", "10")
    # Need to reimport for env to take effect. Use direct override.
    import bowhead_core.kb.loader.xlsx_loader as xlsx_mod

    monkeypatch.setattr(xlsx_mod, "MAX_ROWS_PER_ISLAND_CHUNK", 10)

    rows = [["序号", "金额"]] + [[i, i * 100] for i in range(1, 26)]  # 25 data rows
    path = tmp_path / "big.xlsx"
    _write_xlsx(path, {"Sheet1": rows})

    result = await XlsxLoader().load(str(path))
    # 25 rows / 10 per chunk = 3 chunks
    assert len(result.documents) == 3
    # all chunks share same header repeated
    for doc in result.documents:
        assert "| 序号 | 金额 |" in doc.page_content
    # Part X/3 marker present when total_chunks > 1
    assert "Part 1/3" in result.documents[0].page_content


async def test_xlsx_loader_empty_workbook(tmp_path) -> None:
    """xlsx with only blank rows → 1 placeholder doc, no parse_failed."""
    path = tmp_path / "blank.xlsx"
    wb = openpyxl.Workbook()
    wb.save(path)  # default sheet, all empty
    result = await XlsxLoader().load(str(path))
    assert not result.metadata.get("parse_failed")
    assert result.metadata.get("island_count") == 0
    assert "empty" in result.documents[0].page_content


# ─── V2 chunk_strategy tests ─────────────────


async def test_xlsx_loader_per_row_strategy(tmp_path) -> None:
    """chunk_strategy='per_row' → 1 chunk per data row + key-value content."""
    path = tmp_path / "roster.xlsx"
    _write_xlsx(
        path,
        {
            "Sheet1": [
                ["姓名", "部门", "入职日期"],
                ["张三", "GZ", "2020-01-15"],
                ["李四", "上海", "2019-06-01"],
                ["王五", "GZ", "2021-08-20"],
            ],
        },
    )
    result = await XlsxLoader(chunk_strategy="per_row").load(str(path))
    assert result.metadata.get("chunk_strategy") == "per_row"
    assert len(result.documents) == 3  # 3 data rows
    # First chunk has 张三's key-value
    md0 = result.documents[0].page_content
    assert "张三" in md0
    assert "**姓名**" in md0
    assert "**部门**: GZ" in md0
    assert "Row 1" in md0


async def test_xlsx_loader_per_table_strategy(tmp_path) -> None:
    """chunk_strategy='per_table' → 1 chunk per sheet (整 sheet 一片)."""
    path = tmp_path / "report.xlsx"
    _write_xlsx(
        path,
        {
            "资产负债表": [
                ["资产", "期末余额"],
                ["货币资金", 975022.15],
                ["应收账款", 16424140.75],
            ],
            "利润表": [
                ["项目", "本期金额"],
                ["营业收入", 4402947.76],
                ["营业成本", 39823.01],
            ],
        },
    )
    result = await XlsxLoader(chunk_strategy="per_table").load(str(path))
    assert result.metadata.get("chunk_strategy") == "per_table"
    assert len(result.documents) == 2  # 1 chunk per sheet
    assert "资产负债表" in result.documents[0].page_content
    assert "货币资金" in result.documents[0].page_content
    assert "利润表" in result.documents[1].page_content


async def test_xlsx_loader_default_per_island_unchanged(tmp_path) -> None:
    """Default constructor → per_island (backward compat)."""
    path = tmp_path / "x.xlsx"
    _write_xlsx(path, {"Sheet1": [["a", "b"], [1, 2]]})
    result = await XlsxLoader().load(str(path))
    assert result.metadata.get("chunk_strategy") == "per_island"


async def test_xlsx_loader_invalid_chunk_strategy_falls_back(tmp_path) -> None:
    """Invalid chunk_strategy → silently fall back to per_island."""
    path = tmp_path / "x.xlsx"
    _write_xlsx(path, {"Sheet1": [["a"], [1]]})
    result = await XlsxLoader(chunk_strategy="bogus").load(str(path))
    assert result.metadata.get("chunk_strategy") == "per_island"


# ─── V3 multi-row header detection tests ─────────


def test_detect_header_span_single_row() -> None:
    """Single text-heavy header row + numeric data → header span = 1."""
    from bowhead_core.kb.loader.xlsx_loader import _detect_header_span

    island = [
        ["序号", "名称", "金额"],
        [1, "项目A", 100],
        [2, "项目B", 200],
    ]
    assert _detect_header_span(island) == (0, 1)


def test_detect_header_span_two_row_with_title() -> None:
    """Title row + 2-row text header → header span (1, 3)."""
    from bowhead_core.kb.loader.xlsx_loader import _detect_header_span

    island = [
        ["收入确认清单", None, None, None],  # title
        ["业务信息", "业务信息", "金额", "金额"],  # group header (text-heavy)
        ["序号", "项目", "本期", "全年"],  # sub header (text-heavy)
        [1, "项目A", 1000, 12000],  # data (numbers)
    ]
    span = _detect_header_span(island)
    assert span == (1, 3)


async def test_xlsx_multi_row_header_renders_correctly(tmp_path) -> None:
    """Real-world 2-row header xlsx → markdown contains both header rows."""
    path = tmp_path / "multi.xlsx"
    _write_xlsx(
        path,
        {
            "Sheet1": [
                ["收入确认清单", None, None],
                ["项目信息", "项目信息", "金额"],
                ["序号", "项目名称", "本期收入"],
                [1, "datarc 订阅", 100000],
                [2, "咨询服务", 50000],
            ],
        },
    )
    result = await XlsxLoader().load(str(path))
    md = result.documents[0].page_content
    assert "## 收入确认清单" in md
    # Both header rows should appear
    assert "项目信息" in md
    assert "序号" in md
    # Data row preserved
    assert "datarc 订阅" in md
    # header_row_count should be 2 in metadata
    assert result.documents[0].metadata.get("header_row_count") == 2


async def test_workbook_over_row_cap_records_truncation(tmp_path: Path) -> None:
    """跨工作表共超出行数上限：记录截断（总行数含未读到的工作表），最后一个表格块带说明。"""
    from bowhead_core.kb.loader.xlsx_loader import MAX_TOTAL_ROWS_PER_WORKBOOK

    path = tmp_path / "big.xlsx"
    first = [["序号", "金额"], *([i, i * 10] for i in range(MAX_TOTAL_ROWS_PER_WORKBOOK - 11))]
    second = [["序号", "金额"], *([i, i] for i in range(19))]
    _write_xlsx(path, {"一月": first, "二月": second})
    total = len(first) + len(second)

    result = await XlsxLoader().load(str(path))

    assert result.metadata["truncated_rows"] == {"indexed": MAX_TOTAL_ROWS_PER_WORKBOOK, "total": total}
    assert "仅索引了前" in result.documents[-1].page_content
    assert "二月" in result.documents[-1].page_content
