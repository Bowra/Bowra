"""Table parser for Sprint 25 per-row extraction.

Adapter on top of pdfplumber (native PDF) + L2 vision markdown (scan-only PDF).
Outputs list[TableRow] for RowExtractor to batch + LLM extract.

Per tech-design r2 §3.4 + PRD r3 §3.2 Phase B. Phase A spike Step 1 validated
pdfplumber as default parser (synthetic 0.027s/page, header detect ✓, subtotal
skip ✓). Real corpus edge case (cross-page row, merged cell) handled here.

Cross-page row continuation per IntelliVault chunker.py:156-204 pattern, but
header detect uses **finance keyword whitelist** (NOT "no digit" heuristic which
fails on "2022年" / "期末数").
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# Finance domain header keyword whitelist (P1-PM-7 r1).
# Replaces IntelliVault's "no digit" heuristic which fails for cells like
# "2022年" / "期末数" / "Q1借方".
FINANCE_HEADER_KEYWORDS = frozenset(
    {
        # Detail ledger / journal.
        "凭证号",
        "日期",
        "摘要",
        "科目编码",
        "科目名称",
        "借方金额",
        "贷方金额",
        "借方",
        "贷方",
        "币种",
        "方向",
        # Activity table.
        "会计期间",
        "期初余额",
        "期末余额",
        "本期发生",
        "本期借方发生",
        "本期贷方发生",
        "期初借方",
        "期初贷方",
        "期末借方",
        "期末贷方",
        # General ledger.
        "本期借方",
        "本期贷方",
        # Income statement (cardinality=one; parser still detects headers for consistency).
        "报告期",
        "营业收入",
        "营业成本",
        "管理费用",
        "销售费用",
        "财务费用",
        "营业利润",
        "利润总额",
        "净利润",
    }
)


# Subtotal row keywords (skip during data row collection)
SUBTOTAL_KEYWORDS = frozenset(
    {
        "小计",
        "合计",
        "本页合计",
        "本期合计",
        "累计",
        "本年累计",
        "本月合计",
        "年度合计",
        "季度合计",
    }
)


# ─── DTOs ────────────────────────────────────────────────


@dataclass
class TableRow:
    """Logical table row (cross-page joined, header skipped, subtotal skipped).

    row_seq is 1-indexed within source file. page is the source page (1-indexed)
    for citation in chat (`[ref:row:rec_xxx]` resolves to `第N页第M行`).
    cells is the raw string list from parser — RowExtractor sanitizes per-cell
    before LLM prompt (P0-SEC-1).
    """

    row_seq: int
    page: int | None
    cells: list[str]


@dataclass
class TableParseResult:
    """Aggregated parser output for a single file."""

    rows: list[TableRow]
    total_pages: int
    header_detected: bool
    subtotal_rows_skipped: int
    cross_page_join_pairs: int
    parser_name: str


# ─── Parser interface ────────────────────────────────────


class TableParser(Protocol):
    """Abstract parser. Phase B implementations: pdfplumber (default native),
    vision-markdown (L2 fallback for scan-only PDF)."""

    async def parse(
        self,
        *,
        file_path: str,
        mime_type: str,
        existing_load_result: Any | None = None,
    ) -> TableParseResult: ...


# ─── pdfplumber implementation (default for native PDF) ──


class PdfPlumberTableParser:
    """Default parser per Phase A spike (synthetic ~0.027s/page).

    Real-corpus edge cases handled:
    - **Cross-page row continuation**: data row split across page boundary →
      detected by cell-count match + page gap ≤ 1, last row of page N merged
      with first row of page N+1 (if not header)
    - **Repeated page headers**: detect by FINANCE_HEADER_KEYWORDS match ≥2 cells
    - **Subtotal rows**: detected by SUBTOTAL_KEYWORDS, skipped from data rows
    """

    async def parse(
        self,
        *,
        file_path: str,
        mime_type: str,
        existing_load_result: Any | None = None,
    ) -> TableParseResult:
        # CPU-bound — offload to thread pool
        return await asyncio.to_thread(self._extract_sync, file_path)

    def _extract_sync(self, file_path: str) -> TableParseResult:
        try:
            import pdfplumber
        except ImportError as exc:
            raise RuntimeError("pdfplumber not installed. Run: uv add pdfplumber") from exc

        rows: list[TableRow] = []
        page_count = 0
        header_detected_global = False
        subtotal_skipped = 0
        cross_page_pairs = 0
        prev_header_cells: list[str] | None = None
        row_seq = 0
        last_page_row: tuple[int, list[str]] | None = None  # (page_idx, cells) for cross-page join

        with pdfplumber.open(file_path) as pdf:
            page_count = len(pdf.pages)
            for page_idx, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables() or []
                for table in tables:
                    if not table:
                        continue

                    # Determine header start
                    start_idx = 0
                    first_row_cells = [str(c or "") for c in table[0]]
                    if not header_detected_global:
                        # First page first table — try to detect header
                        if self._is_header_row(first_row_cells):
                            prev_header_cells = first_row_cells
                            header_detected_global = True
                            start_idx = 1
                    # Subsequent table — skip repeated header
                    elif prev_header_cells and self._is_header_match(first_row_cells, prev_header_cells):
                        start_idx = 1

                    # Process data rows
                    table_rows = table[start_idx:]
                    for cell_list in table_rows:
                        cells = [str(c or "") for c in cell_list]

                        # Skip subtotal row
                        if self._is_subtotal_row(cells):
                            subtotal_skipped += 1
                            continue

                        # Cross-page join: if cell_count mismatch (likely
                        # continuation of last page's row) AND last_page_row exists
                        # within 1-page gap, attempt-merge
                        if (
                            last_page_row is not None
                            and prev_header_cells is not None
                            and len(cells) != len(prev_header_cells)
                            and len(last_page_row[1]) + len(cells) <= len(prev_header_cells) + 1
                            and page_idx - last_page_row[0] <= 1
                        ):
                            # Merge: pad last_page_row.cells + cells = full row
                            merged = last_page_row[1] + cells
                            if len(merged) <= len(prev_header_cells):
                                # Pad to header length with empty strings
                                merged += [""] * (len(prev_header_cells) - len(merged))
                                # Replace last appended row with merged
                                if rows and rows[-1].row_seq == row_seq:
                                    rows[-1] = TableRow(
                                        row_seq=row_seq,
                                        page=last_page_row[0],
                                        cells=merged[: len(prev_header_cells)],
                                    )
                                    cross_page_pairs += 1
                                    last_page_row = None
                                    continue

                        row_seq += 1
                        rows.append(
                            TableRow(
                                row_seq=row_seq,
                                page=page_idx,
                                cells=cells,
                            )
                        )
                        last_page_row = (page_idx, cells)

        return TableParseResult(
            rows=rows,
            total_pages=page_count,
            header_detected=header_detected_global,
            subtotal_rows_skipped=subtotal_skipped,
            cross_page_join_pairs=cross_page_pairs,
            parser_name="pdfplumber",
        )

    def _is_header_row(self, cells: list[str]) -> bool:
        """Detect header by finance keyword count.

        Threshold: ≥2 cells match FINANCE_HEADER_KEYWORDS (substring).
        Note: IntelliVault uses "no digit" but that fails on "2022年" header.
        """
        cells_text = [c.strip() for c in cells if c]
        if not cells_text:
            return False
        finance_hits = sum(1 for c in cells_text if any(kw in c for kw in FINANCE_HEADER_KEYWORDS))
        return finance_hits >= 2

    def _is_header_match(self, candidate: list[str], header: list[str]) -> bool:
        """Match by cell count + at least 2 finance keyword cells in candidate."""
        if len(candidate) != len(header):
            return False
        return self._is_header_row(candidate)

    def _is_subtotal_row(self, cells: list[str]) -> bool:
        """Detect subtotal/合计 row to skip."""
        text = " ".join(str(c or "") for c in cells)
        return any(kw in text for kw in SUBTOTAL_KEYWORDS)


# ─── L2 vision markdown parser (scan-only PDF fallback) ──


class VisionMarkdownTableParser:
    """Parse L2 vision LLM markdown table output to TableRow list.

    Only invoked when existing_load_result.layer_used == 'L2' (PdfLoader fell
    back to qwen3-vl-plus for scan-only PDF). Markdown table format:
        | col1 | col2 | col3 |
        | --- | --- | --- |
        | val1 | val2 | val3 |

    Per page joined as "## Page N\\n\\n<markdown>" (see PdfLoader._vision_l2).
    """

    # Markdown table row regex: `| ... | ... |`
    _ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
    # Separator row regex: `| --- | --- |`
    _SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
    # Page marker: `## Page N`
    _PAGE_RE = re.compile(r"^##\s*Page\s+(\d+)", re.IGNORECASE)

    async def parse(
        self,
        *,
        file_path: str,
        mime_type: str,
        existing_load_result: Any | None = None,
    ) -> TableParseResult:
        if existing_load_result is None:
            raise ValueError("VisionMarkdownTableParser requires existing_load_result from L2 vision pipeline")
        layer_used = getattr(existing_load_result, "layer_used", None)
        if layer_used != "L2":
            raise ValueError(f"VisionMarkdownTableParser requires layer_used='L2', got {layer_used!r}")

        documents = getattr(existing_load_result, "documents", [])
        markdown = "\n\n".join(d.page_content for d in documents if getattr(d, "page_content", None))
        return self._parse_markdown_sync(markdown)

    def _parse_markdown_sync(self, md: str) -> TableParseResult:
        rows: list[TableRow] = []
        current_page = None
        page_count = 0
        in_table = False
        prev_header_cells: list[str] | None = None
        header_detected = False
        subtotal_skipped = 0
        row_seq = 0

        for line in md.splitlines():
            # Track page
            page_match = self._PAGE_RE.match(line)
            if page_match:
                current_page = int(page_match.group(1))
                page_count = max(page_count, current_page)
                in_table = False  # Reset on page boundary
                continue

            # Separator row → next line is data (or this is between header and data)
            if self._SEP_RE.match(line):
                in_table = True
                continue

            # Markdown table row
            row_match = self._ROW_RE.match(line)
            if not row_match:
                in_table = False
                continue

            cells_raw = row_match.group(1).split("|")
            cells = [c.strip() for c in cells_raw]

            # First row of a table is the header
            if not in_table:
                # Could be header (next line should be separator)
                if not header_detected:
                    if self._is_finance_header(cells):
                        prev_header_cells = cells
                        header_detected = True
                    continue
                # New table — repeated header on this page
                if prev_header_cells and len(cells) == len(prev_header_cells):
                    continue
                # Fall through — treat as data
                in_table = True

            # Data row
            cells_text = " ".join(cells)
            if any(kw in cells_text for kw in SUBTOTAL_KEYWORDS):
                subtotal_skipped += 1
                continue

            row_seq += 1
            rows.append(
                TableRow(
                    row_seq=row_seq,
                    page=current_page,
                    cells=cells,
                )
            )

        return TableParseResult(
            rows=rows,
            total_pages=page_count or 1,
            header_detected=header_detected,
            subtotal_rows_skipped=subtotal_skipped,
            cross_page_join_pairs=0,  # vision markdown loses pdfplumber-level page join
            parser_name="vision_markdown",
        )

    def _is_finance_header(self, cells: list[str]) -> bool:
        cells_text = [c.strip() for c in cells if c]
        finance_hits = sum(1 for c in cells_text if any(kw in c for kw in FINANCE_HEADER_KEYWORDS))
        return finance_hits >= 2


# ─── Dispatcher ──────────────────────────────────────────


class TableParserDispatcher:
    """Dispatch by mime + load_result.layer_used (P0-TD-ENG-6 resume support).

    Default path: native PDF → PdfPlumberTableParser
    L2 fallback: scan-only PDF → VisionMarkdownTableParser (load_result.layer_used='L2')
    """

    def __init__(self) -> None:
        self._pdfplumber = PdfPlumberTableParser()
        self._vision = VisionMarkdownTableParser()

    async def parse(
        self,
        *,
        file_path: str,
        mime_type: str,
        existing_load_result: Any | None = None,
    ) -> TableParseResult:
        if existing_load_result is not None:
            layer_used = getattr(existing_load_result, "layer_used", None)
            if layer_used == "L2":
                return await self._vision.parse(
                    file_path=file_path,
                    mime_type=mime_type,
                    existing_load_result=existing_load_result,
                )

        # Default: native PDF
        return await self._pdfplumber.parse(
            file_path=file_path,
            mime_type=mime_type,
            existing_load_result=existing_load_result,
        )
