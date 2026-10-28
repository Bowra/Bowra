"""CSV loader：读成一张表，切分沿用 XlsxLoader（表格块、每块最多 50 行、表头随块重复）。

知识库体验整治 PRD §10 Q5：数据产品用户最常见的资料是表格导出。矩阵抽取读的是入库分块，
与格式无关，CSV 入库后即可用于矩阵。

编码：先按 UTF-8（含 BOM）读，失败再按 GB18030（国内 Excel/系统导出常见 GBK）。
分隔符：逗号、制表符、分号中自动识别，识别不了按逗号。行数上限与 xlsx 相同。
"""

from __future__ import annotations

import asyncio
import csv
import io
from pathlib import Path
from typing import Any

from bowhead_core.kb.loader.xlsx_loader import MAX_TOTAL_ROWS_PER_WORKBOOK, XlsxLoader

CSV_MIME = "text/csv"
_ENCODINGS = ("utf-8-sig", "gb18030")
_DELIMITERS = ",\t;"


def _decode(raw: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _csv_reader(file_path: str):
    text = _decode(Path(file_path).read_bytes())
    sample = text[: 64 * 1024]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=_DELIMITERS)
    except csv.Error:
        dialect = csv.excel
    return csv.reader(io.StringIO(text), dialect)


def read_csv_rows(file_path: str, max_rows: int = MAX_TOTAL_ROWS_PER_WORKBOOK) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for row in _csv_reader(file_path):
        if len(rows) >= max_rows:
            break
        rows.append([cell.strip() or None for cell in row])
    return rows


def count_csv_rows(file_path: str) -> int:
    """CSV 总行数（与 read_csv_rows 的计数口径一致），用于提示超出索引上限的行数。"""
    return sum(1 for _ in _csv_reader(file_path))


class CsvLoader(XlsxLoader):
    SUPPORTED_MIMES = frozenset({CSV_MIME})
    FORMAT_LABEL = "csv"
    SINGLE_HEADER_ROW = True

    async def _read_sheets(self, file_path: str) -> list[tuple[str, list[list[Any]]]]:
        return [("CSV", await asyncio.to_thread(read_csv_rows, file_path))]

    async def _count_total_rows(self, file_path: str) -> int:
        return await asyncio.to_thread(count_csv_rows, file_path)
