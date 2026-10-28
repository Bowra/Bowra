"""Shared xlsx loading utility — used by both attachments_injector (inline 30-row preview)
and kb/loader/xlsx_loader (full KB indexing).

Bowhead 已存在 3 条 xlsx 路径, 本 util 统一最底层 openpyxl 读取约定:
1. **bowhead_skill/catalog/xlsx**: 用户 **interactive 分析** 走 python_sandbox + pandas
   (聚合 / 透视 / 公式). KB index 不走这条.
2. **attachments_injector._extract_xlsx**: 用户 **inline attach** xlsx 时, 前 30 行
   TSV 渲染塞进 prompt (走 read_xlsx_rows 跟本 util). 不索引.
3. **kb/loader/xlsx_loader**: 静态 **KB indexing** (整 xlsx → markdown chunks → chromadb).
   走 read_xlsx_rows 跟本 util + 加 island detection / 拆 chunk.

复杂聚合 query (SUM / COUNT / GROUP BY) → KB 检索给 chunk + Lead 走 python_sandbox skill
做真聚合, 跟现 prod flow 一致. 不在 KB 重复造轮子.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def read_xlsx_rows_from_path(file_path: str, max_total_rows: int = 5000) -> list[tuple[str, list[list[Any]]]]:
    """Load xlsx file → list of (sheet_name, rows). Sync (caller wraps in to_thread).

    每 sheet 一个 (name, rows) tuple. rows 是 list[list[Any]] (含 None for empty cells).
    Caller 自己 detect island / pad rows / render markdown.

    - data_only=True: 公式 → cached value (没 cache 则 None, 用户 Excel 打开 save 后再 upload)
    - read_only=True: 流式读, 内存友好
    - max_total_rows: 跨 sheet 总行 cap (memory + cost guard, 默 5000)
    """
    import openpyxl

    wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    out: list[tuple[str, list[list[Any]]]] = []
    total = 0
    try:
        for sheet_name in wb.sheetnames:
            if total >= max_total_rows:
                logger.warning(
                    "xlsx_utils: row cap %d reached, dropping remaining sheets",
                    max_total_rows,
                )
                break
            ws = wb[sheet_name]
            rows: list[list[Any]] = []
            for row in ws.iter_rows(values_only=True):
                if total >= max_total_rows:
                    break
                rows.append(list(row))
                total += 1
            out.append((sheet_name, rows))
    finally:
        wb.close()
    return out


def read_xlsx_rows_from_bytes(data: bytes, max_total_rows: int = 5000) -> list[tuple[str, list[list[Any]]]]:
    """Same as read_xlsx_rows_from_path but takes bytes (in-memory upload)."""
    import io

    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    out: list[tuple[str, list[list[Any]]]] = []
    total = 0
    try:
        for sheet_name in wb.sheetnames:
            if total >= max_total_rows:
                break
            ws = wb[sheet_name]
            rows: list[list[Any]] = []
            for row in ws.iter_rows(values_only=True):
                if total >= max_total_rows:
                    break
                rows.append(list(row))
                total += 1
            out.append((sheet_name, rows))
    finally:
        wb.close()
    return out


def read_xls_rows_from_path(file_path: str, max_total_rows: int = 5000) -> list[tuple[str, list[list[Any]]]]:
    """Legacy .xls (Excel 97-2003 / WPS) reader via xlrd<2.0.

    财税尽调财务报表 (资产负债表 / 利润表 / 现金流量表) 多 .xls. openpyxl 不支持.
    xlrd 2.0+ 砍了 .xls, 我们 pin xlrd<2.0.

    Returns same shape as read_xlsx_rows_from_path: [(sheet_name, rows)].
    """
    import xlrd

    wb = xlrd.open_workbook(file_path, on_demand=True)
    out: list[tuple[str, list[list[Any]]]] = []
    total = 0
    try:
        for sheet_name in wb.sheet_names():
            if total >= max_total_rows:
                logger.warning(
                    "xlsx_utils: row cap %d reached, dropping remaining sheets",
                    max_total_rows,
                )
                break
            ws = wb.sheet_by_name(sheet_name)
            rows: list[list[Any]] = []
            for r in range(ws.nrows):
                if total >= max_total_rows:
                    break
                row = [ws.cell_value(r, c) for c in range(ws.ncols)]
                # xlrd empty cells return "", not None; align openpyxl.
                row = [None if v == "" else v for v in row]
                rows.append(row)
                total += 1
            out.append((sheet_name, rows))
            wb.unload_sheet(sheet_name)
    finally:
        wb.release_resources()
    return out


def read_excel_rows_from_path(file_path: str, max_total_rows: int = 5000) -> list[tuple[str, list[list[Any]]]]:
    """Universal Excel reader. Dispatches by file extension.

    .xlsx / .xlsm → openpyxl (read_xlsx_rows_from_path)
    .xls → xlrd<2.0 (read_xls_rows_from_path)
    """
    p = file_path.lower()
    if p.endswith((".xlsx", ".xlsm")):
        return read_xlsx_rows_from_path(file_path, max_total_rows)
    if p.endswith(".xls"):
        return read_xls_rows_from_path(file_path, max_total_rows)
    # default to xlsx (most common)
    return read_xlsx_rows_from_path(file_path, max_total_rows)


def count_excel_rows_from_path(file_path: str) -> int:
    """工作簿所有工作表的总行数（与读取时的行计数口径一致：含空行）。Sync。

    只在读取触到行数上限时调用，用来告诉用户「共 N 行、只索引了前 M 行」。.xlsx 优先用
    工作表的尺寸信息（不逐行解析）；缺尺寸时流式数行。.xls 用 xlrd 的 nrows。
    """
    if file_path.lower().endswith(".xls"):
        import xlrd

        wb = xlrd.open_workbook(file_path, on_demand=True)
        try:
            total = 0
            for sheet_name in wb.sheet_names():
                total += wb.sheet_by_name(sheet_name).nrows
                wb.unload_sheet(sheet_name)
            return total
        finally:
            wb.release_resources()

    import openpyxl

    wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    try:
        total = 0
        for ws in wb.worksheets:
            n = ws.max_row
            if n is None:
                n = sum(1 for _ in ws.iter_rows(values_only=True))
            total += n
        return total
    finally:
        wb.close()


def is_empty_cell(v: Any) -> bool:
    """Cell empty check shared by both xlsx paths."""
    if v is None:
        return True
    return bool(isinstance(v, str) and not v.strip())


def row_is_empty(row: list[Any]) -> bool:
    return all(is_empty_cell(c) for c in row)
