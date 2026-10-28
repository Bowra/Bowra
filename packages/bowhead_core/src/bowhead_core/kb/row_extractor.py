"""RowExtractor — per-row LLM extract for Sprint 25 cardinality=many doc_type.

Streaming async generator that yields batches of extracted rows (NOT a list
of all 5K rows in memory at once — P1-PERF-2 防 OOM). Caller (worker
_run_per_row_extraction) iterates `async for batch in row_extractor.extract_rows(...)`
and writes each batch to KbExtractedRowsStore.create_many.

Per tech-design r2 §3.3 + Phase A Step 2 真测 lock decisions:
- Model resolution via `bowhead_core.kb.config_loader.KbModelRegistry`（team config 构建）
  → ModelSpec (provider, base_url, api_key, real_id, timeout)
- AsyncOpenAI client directly (NOT bowhead_router.invoke("lead") which is wired
  to deepseek+doubao for dev — wrong target for KB extract)
- `extra_body={"enable_thinking": False}` MANDATORY (qwen3.6-plus/flash are
  reasoning models — output token balloon to 12K+ chars without this)
- `_ROW_EXTRACT_LLM_SEMAPHORE(8)` global, asyncio.gather chunked at 4-5 wave
- Token budget guard: input > 28K (qwen-plus 32K - 4K output) → recursive halve
- max_output_tokens = batch_size * 100 (NOT fixed 4000)
- Per-cell sanitize (NFKC + control + bidi + zw + 500 char cap) — same logic
  as doc_type_classifier._sanitize_chunk_sample

Phase A Step 2 real latency (50-row batch, enable_thinking=false):
- qwen3.6-flash-row: P50 30s/call, output ~2400 tokens → Sprint 25 default
- qwen3.6-plus-row: P50 91s/call (3x slower) → fallback for high-accuracy
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_core.ssrf_guard import build_guarded_async_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


# Global LLM concurrency cap (P0-PERF-4 + Phase A Step 2 burst test validated).
# 8-concurrent qwen3.6-flash burst: all 200 OK, zero 429 on dev key.
# Env override: BOWHEAD_ROW_EXTRACT_LLM_CONCURRENCY (P1-TD-SEC-7 bound 1..32).
def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


_LLM_CONCURRENCY = _clamp(
    int(os.environ.get("BOWHEAD_ROW_EXTRACT_LLM_CONCURRENCY", "8")),
    1,
    32,
)
_ROW_EXTRACT_LLM_SEMAPHORE = asyncio.Semaphore(_LLM_CONCURRENCY)


# Token budget guard (qwen3.6 series 32K context, leave ~4K output buffer)
MAX_INPUT_TOKENS_PER_BATCH = 28_000
INTRA_FILE_BATCH_CHUNK_SIZE = 4  # half-concurrent wave size
# Sprint 26 dogfood finding: 50-row batch × multi-field × {value, confidence,
# source_cell_text} → output 25K+ chars hits max_tokens → truncated JSON.
# Reduced 50 → 25 so output stays well under the model's output token limit.
# (9-field detail-ledger row ≈300-400 output tokens; 25 rows ×400 = safe 10K tokens.)
DEFAULT_BATCH_SIZE = 25
MAX_RECURSION_DEPTH = 3


# ─── DTOs ───────────────────────────────────────────────


@dataclass
class RowExtractionBatchResult:
    """Yielded by RowExtractor.extract_rows streaming generator."""

    batch_idx: int
    rows: list[Any]  # list[RowInput] — typed lazily to avoid circular import
    total_batches: int


# ─── Exceptions ─────────────────────────────────────────


class TokenOverflowError(Exception):
    """Single batch input > MAX_INPUT_TOKENS_PER_BATCH after recursive split.

    Indicates the file has rows so long that even 1-row batches exceed the
    LLM context window. Caller marks job failed with error_code='TOKEN_OVERFLOW'.
    """


class RowExtractionError(Exception):
    """Non-transient extraction failure (parser fail / LLM permanent error /
    JSON parse fail). Caller marks job failed_retry (transient) or failed."""


# ─── Sanitize (P0-SEC-1 + P1-TD-SEC-1 parity with classifier) ──


# Bidi / zero-width chars stripped (same set as doc_type_classifier).
_BIDI_ZW_CHARS = "\u200b‌‍‎‏‪‫‬‭‮⁦⁧⁨⁩﻿"


def _sanitize_cell_text(text: str, max_chars: int = 500) -> str:
    """P0-SEC-1: NFKC + control + bidi + zero-width + char cap.

    Equivalent to doc_type_classifier._sanitize_chunk_sample logic but per-cell
    (max_chars=500 vs 1500 — cell is much smaller than chunk). UT verifies
    parity on 100 attack samples.
    """
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKC", text)
    cleaned_chars = []
    for c in text:
        # Skip C category (control) except \n \t
        cat = unicodedata.category(c)
        if cat.startswith("C") and c not in "\n\t":
            continue
        # Skip bidi / zero-width
        if c in _BIDI_ZW_CHARS:
            continue
        cleaned_chars.append(c)
    return "".join(cleaned_chars)[:max_chars]


# kb-matrix M4 FR-40: public re-export (extracted_record_store.v1_value_to_normalized
# precedent); graduation direct-storage create_many_prewrapped bypasses wrap, so variant
# source_excerpt / user_edit* strings must explicitly use same sanitize (r3 review P1-5: r2 mistook
# rows store :521 **comment** for sanitization; implementation here).
sanitize_cell_text = _sanitize_cell_text


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate for budget guard.

    Phase A Step 2 实测: 50-row prompt ~ 6700 input tokens for ~7000 chars
    Chinese-mixed content. Ratio ~0.95 token/char. We use 1.0 token/char as
    safety overestimate.
    """
    return len(text)


# ─── Prompt ─────────────────────────────────────────────


_ROW_EXTRACT_SYS_PROMPT = (
    "你是会计表格 per-row 抽取 agent. 输入是表格行数据 (row marker + cell list).\n"
    '输出 JSON 顶层 {"rows": [...]}, list 长度严格等于输入行数, 每行一个 dict.\n\n'
    "每行 dict 的 key 是字段名 (来自 schema), value 是:\n"
    '  {"value": <抽出的值或 null>, "confidence": <0-1 float>, '
    '"source_cell_text": "<原 cell 文本 verbatim, ≤200 字符>"}\n\n'
    "字段抽取规则:\n"
    "1. type=currency: value 用纯数字 (RMB 5,000.00 → 5000.00, 万元 → ×10000)\n"
    "2. type=date: value 用 ISO YYYY-MM-DD (如 '2022年1月15日' → '2022-01-15')\n"
    "3. type=number: value 用纯数字\n"
    "4. type=string: value 简洁直接不带解释\n"
    "5. 找不到值 → value: null, confidence: 0\n"
    "6. source_cell_text 必须是原表格 cell 内容 verbatim 复制, 截断 ≤200 字符\n"
    "7. 不要补全没出现的字段, 仅返回 schema 定义的字段\n"
    "8. 仅输出 JSON, 不要解释\n"
)


# ─── Helpers ────────────────────────────────────────────


def _build_user_prompt(
    batch_rows: list[Any],  # list[TableRow] — lazy import
    fields: list[Any],  # list[ExtractField]
) -> str:
    """Build user prompt with per-cell sanitize (P0-SEC-1).

    batch_rows is list[TableRow] from table_parser, fields is list[ExtractField]
    from doc_type catalog. Each row formatted as `row_<seq>: | c1 | c2 | ... |`.
    """
    # Lazy import to avoid circular
    fields_spec = [{"name": f.name, "type": f.type, "required": getattr(f, "required", False)} for f in fields]
    rows_text_lines = []
    for row in batch_rows:
        sanitized_cells = [_sanitize_cell_text(c) for c in row.cells]
        rows_text_lines.append(f"row_{row.row_seq}: " + " | ".join(sanitized_cells))
    return (
        f"字段 schema:\n{json.dumps(fields_spec, ensure_ascii=False)}\n\n"
        f"表格行 (共 {len(batch_rows)} 行):\n" + "\n".join(rows_text_lines) + "\n\n"
        f'请输出 JSON {{"rows": [...]}}, list 长度严格 = {len(batch_rows)}. JSON only.'
    )


_MARKDOWN_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _salvage_truncated_rows(raw: str) -> list[dict]:
    """Sprint 26 dogfood salvage: extract complete top-level row objects from a
    truncated `{"rows": [ {..}, {..}, {..  <CUT>` JSON.

    Balanced-brace scan starting after `"rows"` array open `[`. Collects each
    complete `{...}` object; stops at the first incomplete one. Returns parsed
    list (may be empty if even the first object is incomplete).
    """
    rows_key = raw.find('"rows"')
    if rows_key < 0:
        return []
    bracket = raw.find("[", rows_key)
    if bracket < 0:
        return []
    out: list[dict] = []
    i = bracket + 1
    n = len(raw)
    while i < n:
        # Skip whitespace + commas
        while i < n and raw[i] in " \t\r\n,":
            i += 1
        if i >= n or raw[i] == "]":
            break
        if raw[i] != "{":
            break
        # Balanced-brace scan for one object (respecting string literals)
        depth = 0
        obj_start = i
        in_str = False
        escape = False
        complete = False
        while i < n:
            ch = raw[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    complete = True
                    i += 1
                    break
            i += 1
        if not complete:
            break  # truncated object — stop
        import json as _json

        try:
            out.append(_json.loads(raw[obj_start:i]))
        except _json.JSONDecodeError:
            break
    return out


# Sprint 26 xlsx fast-path: directly read sheet rows via openpyxl, no LLM.
def _extract_xlsx_rows(
    file_path: str,
    fields: list[Any],  # list[ExtractField]
    max_rows: int,
) -> list[dict[str, Any]]:
    """Read xlsx sheet 1 → list of row dicts {row_seq, fields: {field_name: {value, confidence, source_cell_text}}}.

    Sprint 26 xlsx fast-path: skip LLM entirely (xlsx is already structured).
    Header row (row 1) → field name mapping via fuzzy match to ExtractField.name
    (exact match preferred; substring fallback). Data rows (row 2+) map cells
    to fields by header position.

    Type coerce: currency/number via _coerce_value (str → float),
    date → str (ISO if possible), else str.
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        logger.error("openpyxl not installed — xlsx fast-path unavailable")
        return []
    from bowhead_core.kb.structured_extractor import _coerce_value

    wb = load_workbook(file_path, read_only=True, data_only=True)
    sheet = wb.active
    if sheet is None or sheet.max_row < 2:
        return []

    # Row 1 = header. Build column index → field mapping.
    header_cells = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
    if not header_cells:
        return []

    field_by_name = {f.name: f for f in fields}
    col_to_field: dict[int, Any] = {}
    for col_idx, header_val in enumerate(header_cells):
        if header_val is None:
            continue
        header_str = str(header_val).strip()
        # Exact match
        if header_str in field_by_name:
            col_to_field[col_idx] = field_by_name[header_str]
            continue
        # Substring fuzzy match (合同金额 → 合同金额, 金额 → 借方金额 if no exact)
        for fname, f in field_by_name.items():
            if fname in header_str or header_str in fname:
                col_to_field[col_idx] = f
                break

    if not col_to_field:
        logger.warning("xlsx: no header cell matches catalog fields — skipping")
        return []

    rows_out: list[dict[str, Any]] = []
    for row_idx, row_cells in enumerate(
        sheet.iter_rows(min_row=2, max_row=min(sheet.max_row, max_rows + 1), values_only=True),
        start=1,
    ):
        # Build fields dict per row
        coerced: dict[str, dict[str, Any]] = {}
        for col_idx, val in enumerate(row_cells):
            f = col_to_field.get(col_idx)
            if f is None:
                continue
            source_cell_text = str(val) if val is not None else ""
            # Type coerce
            if val is None or source_cell_text == "":
                coerced[f.name] = {"value": None, "confidence": 0, "source_cell_text": ""}
            else:
                coerced_val = _coerce_value(val, f.type) if f.type in ("currency", "number") else val
                coerced[f.name] = {
                    "value": coerced_val,
                    "confidence": 1.0,
                    "source_cell_text": source_cell_text[:200],
                }
        if coerced:
            rows_out.append({"row_seq": row_idx, "fields": coerced})

    wb.close()
    return rows_out


def _parse_llm_output(raw: str, expected_len: int) -> list[dict]:
    """Parse {"rows": [...]} response from LLM. Handle fallback shapes.

    Robust parsing order:
    1. Try raw as-is
    2. Strip markdown fence ```json ... ```
    3. Try greedy match for outermost JSON object/array

    On len mismatch: emit audit + pad with empty {} OR truncate to expected_len.
    Caller (per-row processing) treats empty dict as "no fields extracted".
    """
    if not raw:
        raise RowExtractionError("LLM_INVALID_JSON: empty response")

    # Attempt 1: raw as-is
    data = None
    with contextlib.suppress(json.JSONDecodeError):
        data = json.loads(raw)

    if data is None:
        # Attempt 2: strip markdown fence
        m = _MARKDOWN_FENCE_RE.search(raw)
        if m:
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(m.group(1))

    if data is None:
        # Attempt 3: greedy outermost { ... } object match (most LLM outputs have surrounding prose).
        # Try to find the first { and last } that form a valid JSON.
        first_brace = raw.find("{")
        last_brace = raw.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(raw[first_brace : last_brace + 1])

    if data is None:
        # Attempt 4 (Sprint 26 dogfood salvage): JSON truncated mid-array (LLM hit
        # max_tokens). Extract complete row objects up to the truncation point —
        # better to keep N-1 valid rows than discard the whole batch. Parse each
        # top-level {...} object inside the "rows":[ array via balanced-brace scan.
        salvaged = _salvage_truncated_rows(raw)
        if salvaged:
            logger.warning(
                "LLM output truncated (raw len=%d) — salvaged %d/%d rows",
                len(raw),
                len(salvaged),
                expected_len,
            )
            from bowhead_core.kb.telemetry import emit_audit

            emit_audit(
                "kb.row_extract_llm_mismatch_len",
                {
                    "expected": expected_len,
                    "actual": len(salvaged),
                    "reason": "truncated_salvage",
                },
            )
            data = {"rows": salvaged}
        else:
            # Truly unparseable — log raw (truncated, error_hint enum only)
            logger.warning(
                "LLM_INVALID_JSON parse failed (raw len=%d, first 300 chars: %r)",
                len(raw),
                raw[:300],
            )
            raise RowExtractionError("LLM_INVALID_JSON: all parse attempts failed")

    if isinstance(data, dict) and "rows" in data and isinstance(data["rows"], list):
        rows = data["rows"]
    elif isinstance(data, list):
        rows = data
    else:
        raise RowExtractionError("LLM_INVALID_JSON: not list or {rows:list}")

    if len(rows) != expected_len:
        from bowhead_core.kb.telemetry import emit_audit

        emit_audit(
            "kb.row_extract_llm_mismatch_len",
            {
                "expected": expected_len,
                "actual": len(rows),
            },
        )
        rows = list(rows) + [{}] * (expected_len - len(rows)) if len(rows) < expected_len else rows[:expected_len]
    return rows


# ─── RowExtractor class ────────────────────────────────


class RowExtractor:
    """Per-row LLM extract. DI: ModelSpec (from yaml registry) + table_parser.

    Streaming generator pattern (P1-PERF-2) — caller iterates batches and
    writes to DB incrementally. Does NOT hold full 5K row list in memory.

    Sprint 25 default model: qwen3.6-flash-row (qwen/qwen3.6-flash), per Phase A
    Step 2 真测 30s/call vs plus 91s/call. Per-doc_type override via
    KbDocTypeDTO.extractor_model (e.g. high-accuracy doc_type 走 plus).
    """

    def __init__(self, table_parser: Any) -> None:
        # DI: any TableParser implementation (PdfPlumberTableParser default OR
        # TableParserDispatcher for L2 fallback support)
        self._table_parser = table_parser

    # Sprint 26 xlsx fast-path: detect xlsx mime → skip LLM, direct openpyxl read.
    # xlsx is already structured (header row + data rows), no LLM extraction
    # needed — each cell value maps directly to a field by header column.
    # Saves ~5-15 min of LLM call per xlsx file.
    _XLSX_MIMES = frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "application/octet-stream",  # some browsers don't send proper xlsx mime
        }
    )

    async def extract_rows(
        self,
        *,
        file_path: str,
        mime_type: str,
        fields: list[Any],  # list[ExtractField] from doc_type
        dedup_key_fields: list[str],  # from doc_type catalog
        model_spec: Any,  # kb.config_loader.ModelSpec (provider/base_url/api_key/real_id/timeout)
        max_rows: int = 10_000,
        batch_size: int = DEFAULT_BATCH_SIZE,
        start_batch_idx: int = 0,  # for resume
        existing_load_result: Any | None = None,
    ) -> AsyncIterator[RowExtractionBatchResult]:
        """Stream-yield batches of extracted rows.

        Pipeline:
        1. table_parser.parse(file_path) → TableParseResult.rows
        2. Apply max_rows cap
        3. Batch into batch_size chunks
        4. For each chunk_size wave: asyncio.gather batch_call within _ROW_EXTRACT_LLM_SEMAPHORE
        5. yield RowExtractionBatchResult per batch (caller writes to DB)

        Token budget guard: if estimated input > MAX_INPUT_TOKENS_PER_BATCH,
        recursive split batch into halves (max depth 3).
        """
        # Sprint 26 xlsx fast-path: skip LLM, read cells directly via openpyxl
        is_xlsx = mime_type in self._XLSX_MIMES or file_path.lower().endswith((".xlsx", ".xlsm", ".xls"))
        if is_xlsx:
            logger.info("xlsx fast-path: skip LLM, direct openpyxl read for %s", file_path)
            xlsx_rows = await asyncio.to_thread(_extract_xlsx_rows, file_path, fields, max_rows)
            if not xlsx_rows:
                logger.warning("xlsx fast-path: no rows found in %s", file_path)
                return
            # Compute dedup_key_hash + build RowInput per row, batch + yield
            from bowhead_core.kb.extracted_rows_store import (
                RowInput,
                compute_row_dedup_key_hash,
            )

            row_inputs = [
                RowInput(
                    row_seq=r["row_seq"],
                    page=None,
                    fields=r["fields"],
                    dedup_key_hash=compute_row_dedup_key_hash(
                        fields_normalized=r["fields"],
                        dedup_key_fields=dedup_key_fields,
                        row_seq=r["row_seq"],
                    ),
                )
                for r in xlsx_rows
            ]
            # Batch yield
            total_batches = (len(row_inputs) + batch_size - 1) // batch_size
            for i, batch_idx in enumerate(range(0, len(row_inputs), batch_size)):
                yield RowExtractionBatchResult(
                    batch_idx=i,
                    rows=row_inputs[batch_idx : batch_idx + batch_size],
                    total_batches=total_batches,
                )
            return

        # PDF path: parse table (CPU-bound, offloaded to thread inside parser)
        parse_result = await self._table_parser.parse(
            file_path=file_path,
            mime_type=mime_type,
            existing_load_result=existing_load_result,
        )
        table_rows = parse_result.rows

        if not table_rows:
            logger.warning("RowExtractor: no rows extracted from %s", file_path)
            return

        # 2. Apply max_rows cap (P1-SEC-12 prevents oversized-file OOM).
        if len(table_rows) > max_rows:
            from bowhead_core.kb.telemetry import emit_audit

            emit_audit(
                "kb.row_extract_truncated",
                {
                    "file_path": file_path,
                    "total_rows_parsed": len(table_rows),
                    "cap": max_rows,
                },
            )
            table_rows = table_rows[:max_rows]

        # 3. Chunk into batches
        batches = [table_rows[i : i + batch_size] for i in range(0, len(table_rows), batch_size)]
        total_batches = len(batches)

        # 4. Wave-based concurrency (chunk_size waves within global SEMAPHORE)
        for chunk_start in range(start_batch_idx, total_batches, INTRA_FILE_BATCH_CHUNK_SIZE):
            chunk_end = min(chunk_start + INTRA_FILE_BATCH_CHUNK_SIZE, total_batches)
            chunk_batches = batches[chunk_start:chunk_end]
            chunk_indices = list(range(chunk_start, chunk_end))

            # Run wave concurrently. asyncio.gather with return_exceptions=True
            # so 1 failing batch doesn't cancel the other 3 (their work is
            # already paid for cost-wise via semaphore acquire+LLM call).
            batch_results = await asyncio.gather(
                *[
                    self._run_one_batch(
                        batch=b,
                        batch_idx=idx,
                        fields=fields,
                        dedup_key_fields=dedup_key_fields,
                        model_spec=model_spec,
                    )
                    for b, idx in zip(chunk_batches, chunk_indices, strict=False)
                ],
                return_exceptions=True,
            )

            for idx, result in zip(chunk_indices, batch_results, strict=False):
                if isinstance(result, Exception):
                    # Propagate the first failing batch to caller (worker marks
                    # job failed/failed_retry based on exception type).
                    # Other 3 wave batches' work is consumed but not yielded —
                    # caller's resume from checkpoint will retry this wave.
                    raise result
                yield RowExtractionBatchResult(
                    batch_idx=idx,
                    rows=result,
                    total_batches=total_batches,
                )

    async def _run_one_batch(
        self,
        *,
        batch: list[Any],  # list[TableRow]
        batch_idx: int,
        fields: list[Any],
        dedup_key_fields: list[str],
        model_spec: Any,  # ModelSpec
        recursion_depth: int = 0,
    ) -> list[Any]:  # list[RowInput]
        """Single batch LLM call + parse + dedup_key compute.

        Token overflow → recursive halve (max depth 3, then raise).
        """
        # 1. Build prompt with per-cell sanitize
        user_prompt = _build_user_prompt(batch, fields)
        input_tokens = _estimate_tokens(user_prompt)

        # Token budget guard (P1-PERF-7)
        if input_tokens > MAX_INPUT_TOKENS_PER_BATCH:
            if recursion_depth >= MAX_RECURSION_DEPTH:
                raise TokenOverflowError(
                    f"Batch {batch_idx} input {input_tokens} tokens > {MAX_INPUT_TOKENS_PER_BATCH} after {recursion_depth} splits"
                )
            mid = max(1, len(batch) // 2)
            results_a = await self._run_one_batch(
                batch=batch[:mid],
                batch_idx=batch_idx,
                fields=fields,
                dedup_key_fields=dedup_key_fields,
                model_spec=model_spec,
                recursion_depth=recursion_depth + 1,
            )
            results_b = await self._run_one_batch(
                batch=batch[mid:],
                batch_idx=batch_idx,
                fields=fields,
                dedup_key_fields=dedup_key_fields,
                model_spec=model_spec,
                recursion_depth=recursion_depth + 1,
            )
            return results_a + results_b

        # 2. LLM call within global semaphore (P0-PERF-4)
        # Per-row token estimate: multi-field × {value, confidence, source_cell_text}
        # ≈ 300-400 tokens/row (Sprint 26 dogfood: 50-row × 7-field with
        # source_cell_text hit 25K char / ~8K+ token output → truncated).
        # P1-PERF-7: 400 token/row + floor 2K, ceiling 24K (qwen3.6 32K context
        # leaves headroom for the ~7K input prompt).
        max_output_tokens = max(2000, min(len(batch) * 400, 24000))
        async with _ROW_EXTRACT_LLM_SEMAPHORE:
            raw = await self._invoke_llm(
                user_prompt=user_prompt,
                max_output_tokens=max_output_tokens,
                model_spec=model_spec,
            )

        # 3. Parse JSON (auto pad/truncate on length mismatch)
        parsed = _parse_llm_output(raw, expected_len=len(batch))

        # 4. Per-row: type coerce + dedup_key + sanitize source_cell_text echo
        # Lazy import to avoid circular dep
        from bowhead_core.kb.extracted_rows_store import (
            RowInput,
            compute_row_dedup_key_hash,
        )
        from bowhead_core.kb.structured_extractor import _coerce_value

        results = []
        for table_row, llm_row in zip(batch, parsed, strict=False):
            # Build coerced fields dict {name: {value, confidence, source_cell_text}}
            coerced: dict[str, Any] = {}
            for f in fields:
                fdata = llm_row.get(f.name, {})
                if not isinstance(fdata, dict):
                    coerced[f.name] = {"value": None, "confidence": 0}
                    continue
                fdata = dict(fdata)
                # P1-TD-SEC-2: server-side echo sanitize source_cell_text (defense
                # in depth against LLM-reflected injection).
                if "source_cell_text" in fdata:
                    fdata["source_cell_text"] = _sanitize_cell_text(
                        str(fdata["source_cell_text"]),
                        max_chars=200,
                    )
                # source_snippet alias for v2 wrap (extracted_record_store.wrap_extractor_result_to_variants
                # reads source_snippet). Map source_cell_text → source_snippet.
                if "source_cell_text" in fdata and "source_snippet" not in fdata:
                    fdata["source_snippet"] = fdata["source_cell_text"]
                # Type coerce value
                if "value" in fdata and fdata["value"] is not None:
                    fdata["value"] = _coerce_value(fdata["value"], f.type)
                coerced[f.name] = fdata

            # Compute dedup_key_hash (row_seq tie-breaker per P1-ENG-3)
            dedup_hash = compute_row_dedup_key_hash(
                fields_normalized=coerced,
                dedup_key_fields=dedup_key_fields,
                row_seq=table_row.row_seq,
            )
            results.append(
                RowInput(
                    row_seq=table_row.row_seq,
                    page=table_row.page,
                    fields=coerced,
                    dedup_key_hash=dedup_hash,
                )
            )
        return results

    async def _invoke_llm(
        self,
        *,
        user_prompt: str,
        max_output_tokens: int,
        model_spec: Any,  # ModelSpec
    ) -> str:
        """Invoke LLM via AsyncOpenAI client + spec.

        Sprint 25 mandates `extra_body={"enable_thinking": False}` — qwen3.6
        models are reasoning models, output token balloons without this.
        Phase A Step 2 实测 confirms this is required for usable latency.
        """
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RowExtractionError(f"openai SDK not installed (Sprint 25 dep): {exc}") from exc

        if not model_spec.api_key:
            raise RowExtractionError(f"PARSE_FAILED: ModelSpec api_key empty for {model_spec.name}. Set BOWHEAD_MODEL_ROUTER_API_KEY env.")

        guarded_http_client = None
        client_kwargs: dict[str, Any] = {
            "api_key": model_spec.api_key,
            "base_url": model_spec.base_url,
            "timeout": float(model_spec.timeout_sec),
        }
        if getattr(model_spec, "ssrf_guard", False):
            guarded_http_client = build_guarded_async_client(
                timeout=float(model_spec.timeout_sec),
            )
            client_kwargs["http_client"] = guarded_http_client
        client = AsyncOpenAI(**client_kwargs)

        try:
            resp = await client.chat.completions.create(
                model=model_spec.real_id,  # e.g. "qwen/qwen3.6-flash"
                messages=[
                    {"role": "system", "content": _ROW_EXTRACT_SYS_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_output_tokens,
                temperature=0.0,
                response_format={"type": "json_object"},
                # Sprint 25 P0: qwen3.6-plus/flash are reasoning models.
                # WITHOUT this, output token balloons to 12K+ chars + 91s/call.
                # qwen3-max doesn't reason (param is no-op).
                extra_body={"enable_thinking": False},
            )
        except TimeoutError as e:
            raise RowExtractionError(f"LLM_TIMEOUT: {type(e).__name__}") from e
        except Exception as e:
            # Don't leak LLM raw exception body to error_hint (P0-SEC-4).
            # error_code/hint mapping is the caller's job (worker maps to enum).
            raise RowExtractionError(f"LLM_INVOKE_FAILED: {type(e).__name__}") from e
        finally:
            if guarded_http_client is not None:
                await guarded_http_client.aclose()

        if not resp.choices:
            raise RowExtractionError("LLM_INVALID_JSON: empty choices")
        content = resp.choices[0].message.content
        return (content or "").strip()
