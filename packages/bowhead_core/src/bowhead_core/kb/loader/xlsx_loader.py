"""XLSX loader (openpyxl + island detection + markdown render).

kb-doctype-config sprint v1.0 §2.1-2.4.

V1: 整 sheet 转 markdown table per island. 中国式表头 (空行分隔多 table) 处理.
V2 (defer): doc_type-aware (e.g. 花名册 → per-row chunk via doc_type config).

底层 openpyxl 读取共用 `bowhead_core.kb.xlsx_utils.read_xlsx_rows_from_path`, 跟
attachments_injector._extract_xlsx 同一约定 (data_only=True / read_only=True / row cap).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from bowhead_core.kb.document import KbDocument
from bowhead_core.kb.loader.base import BaseLoader, LoadResult
from bowhead_core.kb.xlsx_utils import (
    count_excel_rows_from_path,
    read_excel_rows_from_path,
)
from bowhead_core.kb.xlsx_utils import (
    is_empty_cell as _is_empty_cell,
)
from bowhead_core.kb.xlsx_utils import (
    row_is_empty as _row_is_empty,
)

logger = logging.getLogger(__name__)


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Legacy Excel 97-2003 (.xls). Financial/tax due-diligence statements often use .xls (WPS / old Excel)
XLS_MIME = "application/vnd.ms-excel"

# V1 caps (per island / per workbook)—V2 chunk strategy switches to per row
MAX_ROWS_PER_ISLAND_CHUNK = int(os.environ.get("BOWHEAD_KB_XLSX_ROWS_PER_CHUNK", "50"))
"""Round 2 P0 fix: split big island into 50-row chunks, repeat header每 chunk."""

MAX_TOTAL_ROWS_PER_WORKBOOK = int(os.environ.get("BOWHEAD_KB_XLSX_MAX_ROWS", "5000"))
"""Cap on total rows across all sheets (memory + cost guard)."""


def truncation_note(indexed: int, total: int) -> str:
    """超出行数上限时附在最后一个表格块末尾的说明，让对话回答能如实说明只索引了前 N 行。"""
    return f"（注：本文件共 {total} 行，仅索引了前 {indexed} 行，其余行未进入知识库）"


def _count_nonempty(row: list[Any]) -> int:
    return sum(1 for c in row if not _is_empty_cell(c))


def _detect_islands(rows: list[list[Any]]) -> list[list[list[Any]]]:
    """Split rows into islands separated by all-empty rows.

    Returns list of islands. Each island is a list of (non-empty) rows.
    """
    islands: list[list[list[Any]]] = []
    current: list[list[Any]] = []
    for row in rows:
        if _row_is_empty(row):
            if current:
                islands.append(current)
                current = []
            continue
        current.append(row)
    if current:
        islands.append(current)
    return islands


def _detect_header_offset(island: list[list[Any]]) -> int:
    """Returns the row index (within island) that is the header row.

    Heuristics:
    - 1st row 仅 1 non-empty cell → 标题, header is row 1
    - else 1st row is header
    """
    if not island:
        return 0
    if _count_nonempty(island[0]) == 1 and len(island) >= 2:
        return 1
    return 0


def _is_text_heavy_row(row: list[Any]) -> bool:
    """A row is "text-heavy" if non-empty cells are mostly strings (not numbers).

    Used to detect multi-row header (中国式表头 e.g. group cell merged + sub headers row).
    """
    cells = [c for c in row if not _is_empty_cell(c)]
    if not cells:
        return False
    text_count = sum(
        1
        for c in cells
        if isinstance(c, str) or (isinstance(c, (int, float)) and False)  # numbers always count as non-text
    )
    return text_count / len(cells) >= 0.7


def _detect_header_span(island: list[list[Any]]) -> tuple[int, int]:
    """[V3] Detect multi-row header span.

    Returns (header_start, header_end_exclusive).
    - title row (single cell) at row 0 → header starts row 1
    - header rows: consecutive text-heavy rows from start (max 3 rows for stability)
    - data starts after.

    财税尽调 e.g. 收入确认清单 row 0 = group cells (本期 / 上期 / 全年合并),
    row 1 = sub-headers (序号/金额/税率), row 2+ = data.
    """
    if not island:
        return (0, 0)
    start = 1 if (_count_nonempty(island[0]) == 1 and len(island) >= 2) else 0
    end = start
    while end < len(island) and end - start < 3:
        if not _is_text_heavy_row(island[end]):
            break
        end += 1
    if end == start:
        end = start + 1  # at least 1 header row
    return (start, end)


def _cell_to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v).replace("|", "\\|").replace("\n", " ").strip()


def _render_island_chunk(
    rows: list[list[Any]],
    *,
    sheet_name: str,
    island_idx: int,
    chunk_idx: int,
    total_chunks: int,
    title: str | None,
    header_rows: list[list[Any]] | None = None,
    header: list[Any] | None = None,
) -> str:
    """Render 1 chunk (subset of rows) as markdown.

    Supports multi-row header (V3): pass header_rows for multi-row, OR header for legacy single-row.

    chunk header structure:
      # Sheet: <sheet_name> · Block <island_idx+1>[ · Part <chunk_idx+1>/<total_chunks>]
      [## <title>]
      | h1 | h2 | ... |          ← multi rows (each header_row 1 line)
      |----|----|-----|
      | data... |
    """
    parts = [
        f"# Sheet: {sheet_name} · Block {island_idx + 1}" + (f" · Part {chunk_idx + 1}/{total_chunks}" if total_chunks > 1 else ""),
    ]
    if title:
        parts.append(f"## {title}")

    # Header rendering: prefer multi-row header_rows over single-row header
    effective_header_rows: list[list[Any]] = []
    if header_rows:
        effective_header_rows = header_rows
    elif header:
        effective_header_rows = [header]

    n_cols = max((len(r) for r in effective_header_rows), default=0)
    if effective_header_rows:
        for hr in effective_header_rows:
            padded = list(hr) + [None] * max(0, n_cols - len(hr))
            parts.append("| " + " | ".join(_cell_to_str(c) for c in padded[:n_cols]) + " |")
        parts.append("|" + "|".join("---" for _ in range(n_cols)) + "|")

    for row in rows:
        if effective_header_rows:
            padded = list(row) + [None] * max(0, n_cols - len(row))
            padded = padded[:n_cols]
        else:
            padded = row
        parts.append("| " + " | ".join(_cell_to_str(c) for c in padded) + " |")

    return "\n".join(parts)


def _group_rows_within_budget(
    data_rows: list[list[Any]],
    *,
    header_rows: list[list[Any]],
    sheet_name: str,
    island_idx: int,
    title: str | None,
) -> list[list[list[Any]]]:
    """按行分组：每组 ≤ MAX_ROWS_PER_ISLAND_CHUNK 行，且渲染后（含标题与表头）不超过切分器的 chunk 上限。

    此前只按 50 行切，一块常超过 800 token，worker 的段落切分器再把它拦腰切开，后半段没有表头
    （116 实测 151 行 CSV：4 块被切成 10 片，只有 4 片带表头）。超长的单行独占一组。
    """
    from bowhead_core.kb.splitter.paragraph_aware import DEFAULT_CHUNK_SIZE, count_tokens

    prefix = _render_island_chunk(
        rows=[],
        sheet_name=sheet_name,
        island_idx=island_idx,
        chunk_idx=98,
        total_chunks=99,
        title=title,
        header_rows=header_rows,
    )
    budget = DEFAULT_CHUNK_SIZE - count_tokens(prefix)
    n_cols = max((len(r) for r in header_rows), default=0)
    groups: list[list[list[Any]]] = []
    current: list[list[Any]] = []
    used = 0
    for row in data_rows:
        cells = list(row)[:n_cols] if n_cols else list(row)
        cost = count_tokens("| " + " | ".join(_cell_to_str(c) for c in cells) + " |") + 1
        if current and (len(current) >= MAX_ROWS_PER_ISLAND_CHUNK or used + cost > budget):
            groups.append(current)
            current, used = [], 0
        current.append(row)
        used += cost
    if current:
        groups.append(current)
    return groups


def _split_island_to_chunks(
    island: list[list[Any]],
    sheet_name: str,
    island_idx: int,
    *,
    single_header: bool = False,
) -> list[KbDocument]:
    """Split big island into chunks of ≤ MAX_ROWS_PER_ISLAND_CHUNK rows. Repeat header每 chunk.

    V3: 走 _detect_header_span (multi-row header support).
    """
    if not island:
        return []

    # CSV has no types (all strings): multirow header heuristics mistake data for headers; CSV also has no merged-cell multirow headers.
    header_start, header_end = (0, 1) if single_header else _detect_header_span(island)
    title: str | None = None
    if header_start == 1:
        # row 0 is title (single cell); pull text
        title_cells = [c for c in island[0] if not _is_empty_cell(c)]
        title = _cell_to_str(title_cells[0]) if title_cells else None

    header_rows = island[header_start:header_end] if header_end > header_start else []
    data_rows = island[header_end:] if header_rows else island

    if not data_rows:
        md = _render_island_chunk(
            rows=[],
            sheet_name=sheet_name,
            island_idx=island_idx,
            chunk_idx=0,
            total_chunks=1,
            title=title,
            header_rows=header_rows,
        )
        return [
            KbDocument(
                page_content=md,
                metadata={
                    "sheet": sheet_name,
                    "island": island_idx,
                    "page": None,
                    "chunk_part": 0,
                    "chunk_total": 1,
                    "row_count": 0,
                    "header_row_count": len(header_rows),
                },
            )
        ]

    groups = _group_rows_within_budget(data_rows, header_rows=header_rows, sheet_name=sheet_name, island_idx=island_idx, title=title)
    total_chunks = len(groups)
    docs: list[KbDocument] = []
    for ci, sub in enumerate(groups):
        md = _render_island_chunk(
            rows=sub,
            sheet_name=sheet_name,
            island_idx=island_idx,
            chunk_idx=ci,
            total_chunks=total_chunks,
            title=title,
            header_rows=header_rows,
        )
        docs.append(
            KbDocument(
                page_content=md,
                metadata={
                    "sheet": sheet_name,
                    "island": island_idx,
                    "page": None,
                    "chunk_part": ci,
                    "chunk_total": total_chunks,
                    "row_count": len(sub),
                    "header_row_count": len(header_rows),
                },
            )
        )
    return docs


def _split_island_per_row(
    island: list[list[Any]],
    sheet_name: str,
    island_idx: int,
) -> list[KbDocument]:
    """[V2] per-row chunking: 1 data row → 1 chunk + header 上下文.

    适用员工花名册 / 合同台账 类 — 用户问 "GZ 部门的人" → retrieve 命中具体 row.
    """
    if not island:
        return []
    header_offset = _detect_header_offset(island)
    title: str | None = None
    if header_offset == 1:
        cells = [c for c in island[0] if not _is_empty_cell(c)]
        title = _cell_to_str(cells[0]) if cells else None
    header_row = island[header_offset] if header_offset < len(island) else None
    data_rows = island[header_offset + 1 :] if header_row else island

    docs: list[KbDocument] = []
    header_strs = [_cell_to_str(c) for c in header_row] if header_row else []
    for row_idx, row in enumerate(data_rows):
        # render row as key-value pairs (for retrieval semantic matching)
        kv_lines: list[str] = []
        for ci, cell in enumerate(row):
            if _is_empty_cell(cell):
                continue
            key = header_strs[ci] if ci < len(header_strs) else f"col_{ci + 1}"
            kv_lines.append(f"- **{key}**: {_cell_to_str(cell)}")
        if not kv_lines:
            continue  # all-empty row, skip

        header_block = f"# Sheet: {sheet_name} · Block {island_idx + 1} · Row {row_idx + 1}\n"
        if title:
            header_block += f"## {title}\n"
        content = header_block + "\n".join(kv_lines)
        docs.append(
            KbDocument(
                page_content=content,
                metadata={
                    "sheet": sheet_name,
                    "island": island_idx,
                    "row": row_idx,
                    "page": None,
                    "chunk_strategy": "per_row",
                },
            )
        )
    return docs


def _split_sheet_per_table(
    rows: list[list[Any]],
    sheet_name: str,
) -> list[KbDocument]:
    """[V2] per-table chunking: 整 sheet 1 chunk (财务报表 类, 全 sheet 关联紧).

    Cap: row_count > MAX_ROWS_PER_ISLAND_CHUNK 时退回 per_island 拆分 (防 prompt budget 爆).
    """
    non_empty = [r for r in rows if not _row_is_empty(r)]
    if not non_empty:
        return []
    if len(non_empty) > MAX_ROWS_PER_ISLAND_CHUNK:
        # Fallback: use per_island detection so we still chunk
        return _split_island_to_chunks(non_empty, sheet_name, 0)

    parts = [f"# Sheet: {sheet_name}"]
    for row in non_empty:
        parts.append("| " + " | ".join(_cell_to_str(c) for c in row) + " |")
    return [
        KbDocument(
            page_content="\n".join(parts),
            metadata={
                "sheet": sheet_name,
                "page": None,
                "chunk_strategy": "per_table",
            },
        )
    ]


class XlsxLoader(BaseLoader):
    """xlsx + xls loader. L0 only (no LLM). doc_type-aware chunking optional.

    V2 chunk_strategy (走 doc_type config):
    - "per_island" (默认): 1 island → 1 chunk (or split by 50 row cap)
    - "per_row": 员工花名册 / 合同台账 类 — 1 row 1 chunk + 含 header 上下文
    - "per_table": 1 sheet 全表 → 1 chunk (财务报表 类)

    复杂聚合 query (SUM / COUNT / GROUP BY) → 用户走 python_sandbox skill (bowhead_skill
    catalog/xlsx 已 ship), 不在 KB 重复造. KB 只负责 chunked indexing 给 retrieval.
    """

    SUPPORTED_MIMES = frozenset({XLSX_MIME, XLS_MIME})
    # Prefix label for parse-failure placeholder chunks (filtered by prefix on ingestion/retrieval; see document.PLACEHOLDER_PREFIXES)
    FORMAT_LABEL = "xlsx"
    # Header fixed to first row (CSV); False uses multirow header heuristics (xlsx numbers have types, making heuristics viable)
    SINGLE_HEADER_ROW = False

    def __init__(
        self,
        *,
        chunk_strategy: str = "per_island",
        doc_type_columns: list[dict] | None = None,
    ) -> None:
        if chunk_strategy not in ("per_island", "per_row", "per_table"):
            chunk_strategy = "per_island"
        self._chunk_strategy = chunk_strategy
        self._doc_type_columns = doc_type_columns or []

    async def _read_sheets(self, file_path: str) -> list[tuple[str, list[list[Any]]]]:
        """[(工作表名, 行)]。read_excel_rows_from_path 按扩展名分发（.xlsx/.xlsm 走 openpyxl，
        .xls 走 xlrd<2.0；财税财务报表多 .xls）。子类（CSV）覆盖此方法，切分沿用本类。"""
        return await asyncio.to_thread(read_excel_rows_from_path, file_path, MAX_TOTAL_ROWS_PER_WORKBOOK)

    async def _count_total_rows(self, file_path: str) -> int:
        """读取触到行数上限时才调用：全部工作表的总行数（口径与读取一致）。"""
        return await asyncio.to_thread(count_excel_rows_from_path, file_path)

    async def _truncation(self, file_path: str, sheets: list[tuple[str, list[list[Any]]]]) -> dict[str, int] | None:
        """行数上限截断信息 ``{"indexed": M, "total": N}``；未截断返回 None。计数失败不影响入库。"""
        indexed = sum(len(rows) for _, rows in sheets)
        if indexed < MAX_TOTAL_ROWS_PER_WORKBOOK:
            return None
        try:
            total = await self._count_total_rows(file_path)
        except Exception as exc:
            logger.warning("%s_loader: row count failed: %s", self.FORMAT_LABEL, exc)
            return None
        if total <= indexed:
            return None
        logger.info("%s_loader: row cap %d reached, total rows %d", self.FORMAT_LABEL, indexed, total)
        return {"indexed": indexed, "total": total}

    async def load(self, file_path: str, *, use_llm: bool = False) -> LoadResult:
        t0 = time.time()

        try:
            sheets = await self._read_sheets(file_path)
        except Exception as exc:
            logger.warning("%s_loader: load failed: %s", self.FORMAT_LABEL, exc)
            return LoadResult(
                documents=[
                    KbDocument(
                        page_content=f"({self.FORMAT_LABEL} parse failed: {exc})",
                        metadata={"page": None, "parse_failed": True},
                    )
                ],
                layer_used="L0",
                duration_ms=int((time.time() - t0) * 1000),
                metadata={"parse_failed": True, "reason": str(exc)},
            )

        docs: list[KbDocument] = []
        total_islands = 0
        for sheet_name, rows in sheets:
            if self._chunk_strategy == "per_table":
                # Whole sheet 1 chunk (financial statements: table information interrelated)
                table_docs = _split_sheet_per_table(rows, sheet_name)
                docs.extend(table_docs)
                total_islands += 1 if table_docs else 0
                continue
            islands = _detect_islands(rows)
            for idx, island in enumerate(islands):
                if self._chunk_strategy == "per_row":
                    # 1 row 1 chunk (employee rosters / contract ledgers)
                    docs.extend(_split_island_per_row(island, sheet_name, idx))
                else:
                    # default per_island
                    docs.extend(_split_island_to_chunks(island, sheet_name, idx, single_header=self.SINGLE_HEADER_ROW))
                total_islands += 1

        if not docs:
            return LoadResult(
                documents=[
                    KbDocument(
                        page_content="(xlsx empty — 没找到数据 island)",
                        metadata={"page": None},
                    )
                ],
                layer_used="L0",
                duration_ms=int((time.time() - t0) * 1000),
                metadata={"sheet_count": len(sheets), "island_count": 0},
            )

        metadata: dict[str, Any] = {
            "sheet_count": len(sheets),
            "island_count": total_islands,
            "chunk_count": len(docs),
            "chunk_strategy": self._chunk_strategy,
        }
        truncated = await self._truncation(file_path, sheets)
        if truncated is not None:
            # Truncation occurred in the last sheet read: append notice to the end of its last table chunk
            metadata["truncated_rows"] = truncated
            last = docs[-1]
            last.page_content = f"{last.page_content}\n{truncation_note(truncated['indexed'], truncated['total'])}"
        return LoadResult(
            documents=docs,
            layer_used="L0",
            duration_ms=int((time.time() - t0) * 1000),
            metadata=metadata,
        )
