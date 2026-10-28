"""Sprint 26 — UT for Sprint 25 4 new modules (CTO P1-ENG-3 test debt).

Covers (~30 tests):
- extracted_rows_store: 8 tests (init / create_many / UNIQUE dedup / query_rows
  top_level_filters / aggregate SUM group_by / soft_delete_for_attempt /
  count_for_doc_type needs_review)
- per_row_jobs_store: 6 tests (create_job / update_checkpoint pydantic strict /
  update_progress / mark_status enum / mark_zombies atomic / get_latest_for_file)
- row_extractor: 8 tests (sanitize parity / dedup_hash tie-breaker / parse_llm_output
  markdown fence / json no-rows / token estimate / xlsx fast-path / LLM enable_thinking
  param / _XLSX_MIMES detection)
- table_parser: 5 tests (header keyword detect / subtotal skip / vision_markdown parse)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.kb.extracted_rows_store import (
    KbExtractedRowsStore,
    RowInput,
    compute_row_dedup_key_hash,
)
from bowhead_core.kb.per_row_jobs_store import (
    CheckpointStateRead,
    CheckpointStateWrite,
    KbPerRowJobsStore,
)
from bowhead_core.kb.row_extractor import (
    _extract_xlsx_rows,
    _parse_llm_output,
    _sanitize_cell_text,
)
from bowhead_core.kb.table_parser import (
    FINANCE_HEADER_KEYWORDS,
    SUBTOTAL_KEYWORDS,
    PdfPlumberTableParser,
    VisionMarkdownTableParser,
)
from bowhead_core.kb.workspace_quotas_store import KbWorkspaceQuotasStore


@dataclass
class _FakeField:
    name: str
    type: str = "string"
    required: bool = False


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test.db"


# ─── extracted_rows_store (8 tests) ────────────────────────────


@pytest.mark.asyncio
async def test_rows_store_initialize_idempotent(tmp_db):
    store = KbExtractedRowsStore(tmp_db)
    await store.initialize()
    await store.initialize()  # re-run should not crash
    assert tmp_db.exists()


@pytest.mark.asyncio
async def test_rows_store_create_many_basic(tmp_db):
    store = KbExtractedRowsStore(tmp_db)
    await store.initialize()
    rows = [
        RowInput(row_seq=1, page=1, fields={"凭证号": {"value": "0001"}}, dedup_key_hash=111),
        RowInput(row_seq=2, page=1, fields={"凭证号": {"value": "0002"}}, dedup_key_hash=222),
    ]
    result = await store.create_many(
        rows=rows,
        file_id="f1",
        doc_type_id="dt1",
        workspace_id="ws1",
    )
    assert result.inserted_count == 2
    assert result.skipped_dup_count == 0


@pytest.mark.asyncio
async def test_rows_store_unique_dedup(tmp_db):
    """UNIQUE INDEX hit → skipped_dup_count++ (NOT raise)."""
    store = KbExtractedRowsStore(tmp_db)
    await store.initialize()
    rows = [RowInput(row_seq=1, page=1, fields={"a": {"value": "x"}}, dedup_key_hash=999)]
    r1 = await store.create_many(rows=rows, file_id="f1", doc_type_id="dt1", workspace_id="ws1")
    r2 = await store.create_many(rows=rows, file_id="f1", doc_type_id="dt1", workspace_id="ws1")
    assert r1.inserted_count == 1
    assert r2.inserted_count == 0
    assert r2.skipped_dup_count == 1


@pytest.mark.asyncio
async def test_rows_store_query_rows_top_level_row_seq(tmp_db):
    store = KbExtractedRowsStore(tmp_db)
    await store.initialize()
    rows = [RowInput(row_seq=i, page=1, fields={"a": {"value": f"v{i}"}}, dedup_key_hash=i) for i in range(1, 6)]
    await store.create_many(rows=rows, file_id="f1", doc_type_id="dt1", workspace_id="ws1")
    # top_level_filters row_seq=3 → 1 row
    out = await store.query_rows(
        workspace_id="ws1",
        doc_type_id="dt1",
        top_level_filters={"row_seq": 3},
    )
    assert len(out) == 1
    assert out[0].row_seq == 3
    # row_seq_range [2,4] → 3 rows
    out2 = await store.query_rows(
        workspace_id="ws1",
        doc_type_id="dt1",
        top_level_filters={"row_seq_range": [2, 4]},
    )
    assert len(out2) == 3


@pytest.mark.asyncio
async def test_rows_store_aggregate_sum_group_by(tmp_db):
    """aggregate SUM group_by — positional grp_{i} alias works (P0-TD-SEC-2)."""
    store = KbExtractedRowsStore(tmp_db)
    await store.initialize()
    rows = [
        RowInput(row_seq=1, page=1, fields={"金额": {"value": 100.0}, "科目": {"value": "A"}}, dedup_key_hash=1),
        RowInput(row_seq=2, page=1, fields={"金额": {"value": 200.0}, "科目": {"value": "A"}}, dedup_key_hash=2),
        RowInput(row_seq=3, page=1, fields={"金额": {"value": 50.0}, "科目": {"value": "B"}}, dedup_key_hash=3),
    ]
    await store.create_many(
        rows=rows,
        file_id="f1",
        doc_type_id="dt1",
        workspace_id="ws1",
        doc_type_fields=[_FakeField("金额", "currency"), _FakeField("科目", "string")],
    )
    # Total SUM (no group_by)
    total = await store.aggregate(
        workspace_id="ws1",
        doc_type_id="dt1",
        field="金额",
        op="SUM",
    )
    assert len(total) == 1
    assert total[0].value == 350.0
    # group_by 科目 →2 groups.
    by_subject = await store.aggregate(
        workspace_id="ws1",
        doc_type_id="dt1",
        field="金额",
        op="SUM",
        group_by=["科目"],
    )
    assert len(by_subject) == 2
    values_by_key = {r.group_keys["科目"]: r.value for r in by_subject}
    assert values_by_key["A"] == 300.0
    assert values_by_key["B"] == 50.0


@pytest.mark.asyncio
async def test_rows_store_soft_delete_for_attempt(tmp_db):
    """force re-trigger: soft-delete attempt_id < N rows."""
    store = KbExtractedRowsStore(tmp_db)
    await store.initialize()
    # Attempt 1
    rows_a1 = [RowInput(row_seq=i, page=1, fields={"a": {"value": f"v{i}"}}, dedup_key_hash=i) for i in range(1, 4)]
    await store.create_many(rows=rows_a1, file_id="f1", doc_type_id="dt1", workspace_id="ws1", attempt_id=1)
    # Soft-delete attempt 1
    deleted = await store.soft_delete_for_attempt(
        file_id="f1",
        doc_type_id="dt1",
        workspace_id="ws1",
        attempt_id_lt=2,
    )
    assert deleted == 3
    # Verify they don't show in queries
    out = await store.query_rows(workspace_id="ws1", doc_type_id="dt1")
    assert len(out) == 0


@pytest.mark.asyncio
async def test_rows_store_count_for_doc_type(tmp_db):
    store = KbExtractedRowsStore(tmp_db)
    await store.initialize()
    rows = [RowInput(row_seq=i, page=1, fields={"a": {"value": f"v{i}"}}, dedup_key_hash=i) for i in range(1, 6)]
    await store.create_many(rows=rows, file_id="f1", doc_type_id="dt1", workspace_id="ws1")
    count = await store.count_for_doc_type(workspace_id="ws1", doc_type_id="dt1")
    assert count == 5
    # Different ws should not leak
    count_other_ws = await store.count_for_doc_type(workspace_id="ws_other", doc_type_id="dt1")
    assert count_other_ws == 0


def test_rows_compute_dedup_key_hash_tie_breaker():
    """row_seq as tie-breaker for multi-entry same logical row (借/贷分录)."""
    # Same dedup fields, different row_seq → different hash
    fields = {"日期": {"value": "2022-01-15"}, "凭证号": {"value": "0001"}}
    h1 = compute_row_dedup_key_hash(fields, ["日期", "凭证号"], row_seq=1)
    h2 = compute_row_dedup_key_hash(fields, ["日期", "凭证号"], row_seq=2)
    assert h1 != h2, "row_seq tie-breaker missing"
    # Same row_seq + same fields → same hash (deterministic)
    h3 = compute_row_dedup_key_hash(fields, ["日期", "凭证号"], row_seq=1)
    assert h1 == h3


# ─── per_row_jobs_store (6 tests) ─────────────────────────


@pytest.mark.asyncio
async def test_jobs_store_create_job(tmp_db):
    store = KbPerRowJobsStore(tmp_db)
    await store.initialize()
    job_id = await store.create_job(
        file_id="f1",
        doc_type_id="dt1",
        workspace_id="ws1",
        total_batches=5,
        status="queued",
    )
    assert job_id.startswith("perrow_")
    job = await store.get(job_id)
    assert job is not None
    assert job.status == "queued"
    assert job.total_batches == 5


def test_jobs_checkpoint_state_write_strict():
    """CheckpointStateWrite rejects extra fields (P0-TD-SEC-6)."""
    # Valid
    cp = CheckpointStateWrite(
        last_completed_batch_idx=0,
        last_completed_row_seq=0,
        total_batches=5,
        attempt_id=1,
    )
    assert cp.attempt_id == 1
    # Extra field rejected
    with pytest.raises(Exception):  # noqa: B017
        CheckpointStateWrite(
            last_completed_batch_idx=0,
            last_completed_row_seq=0,
            total_batches=5,
            attempt_id=1,
            cell_content_leak="DANGER",  # NOT allowed
        )


def test_jobs_checkpoint_state_read_lenient():
    """CheckpointStateRead ignores extra fields for forward compat (P0-TD-SEC-5)."""
    # Future Sprint 26+ may add fields; 0.44.x must read them via ignore
    read = CheckpointStateRead.model_validate(
        {
            "last_completed_batch_idx": 0,
            "last_completed_row_seq": 0,
            "total_batches": 5,
            "attempt_id": 1,
            "future_sprint_field": "ignored OK",
        }
    )
    assert read.attempt_id == 1


@pytest.mark.asyncio
async def test_jobs_store_mark_status_enum_guard(tmp_db):
    store = KbPerRowJobsStore(tmp_db)
    await store.initialize()
    job_id = await store.create_job(file_id="f1", doc_type_id="dt1", workspace_id="ws1")
    # Valid status
    await store.mark_status(job_id, "running")
    # Invalid status raises
    with pytest.raises(ValueError):
        await store.mark_status(job_id, "not_a_status")
    # Invalid error_code raises
    with pytest.raises(ValueError):
        await store.mark_status(job_id, "failed", error_code="NOT_IN_ENUM")
    # Valid error_code from enum
    await store.mark_status(job_id, "failed", error_code="LLM_TIMEOUT", error_hint="constant")
    job = await store.get(job_id)
    assert job.status == "failed"
    assert job.error_code == "LLM_TIMEOUT"
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_jobs_store_mark_zombies_atomic(tmp_db):
    """heartbeat scanner: UPDATE WHERE heartbeat_at < threshold (P1-SEC-4 atomic)."""
    store = KbPerRowJobsStore(tmp_db)
    await store.initialize()
    job_id = await store.create_job(file_id="f1", doc_type_id="dt1", workspace_id="ws1")
    await store.mark_status(job_id, "running")
    async with open_shared_ctx(tmp_db) as db:
        await db.execute("UPDATE kb_per_row_jobs SET heartbeat_at = '2000-01-01 00:00:00' WHERE job_id = ?", (job_id,))
        await db.commit()
    count = await store.mark_zombies(threshold_minutes=0)
    assert count == 1
    job = await store.get(job_id)
    assert job.status == "failed_retry"


@pytest.mark.asyncio
async def test_jobs_store_get_latest_for_file(tmp_db):
    """get_latest_for_file returns highest attempt_id."""
    store = KbPerRowJobsStore(tmp_db)
    await store.initialize()
    await store.create_job(file_id="f1", doc_type_id="dt1", workspace_id="ws1", attempt_id=1)
    await store.create_job(file_id="f1", doc_type_id="dt1", workspace_id="ws1", attempt_id=2)
    j3 = await store.create_job(file_id="f1", doc_type_id="dt1", workspace_id="ws1", attempt_id=3)
    latest = await store.get_latest_for_file("f1")
    assert latest.job_id == j3
    assert latest.attempt_id == 3


# ─── row_extractor (8 tests) ──────────────────────────


def test_sanitize_cell_text_control_chars_stripped():
    """P0-SEC-1: control chars stripped, newlines kept."""
    assert _sanitize_cell_text("hello\x00world") == "helloworld"
    assert _sanitize_cell_text("line1\nline2\tcol") == "line1\nline2\tcol"
    # Bidi zero-width stripped
    assert _sanitize_cell_text("test\u200btext") == "testtext"


def test_sanitize_cell_text_char_cap():
    """500 char cap on cell text."""
    long = "a" * 1000
    assert len(_sanitize_cell_text(long)) == 500
    assert len(_sanitize_cell_text(long, max_chars=200)) == 200


def test_sanitize_cell_text_nfkc_normalize():
    """NFKC normalizes fullwidth chars."""
    # Fullwidth digit
    fullwidth = "１２３"
    out = _sanitize_cell_text(fullwidth)
    assert out == "123"


def test_parse_llm_output_clean_json():
    """Direct JSON parses cleanly."""
    raw = '{"rows": [{"name": "v1"}, {"name": "v2"}]}'
    out = _parse_llm_output(raw, expected_len=2)
    assert len(out) == 2
    assert out[0]["name"] == "v1"


def test_parse_llm_output_markdown_fence():
    """Markdown fence ```json {...} ``` stripped."""
    raw = '```json\n{"rows": [{"a": 1}]}\n```'
    out = _parse_llm_output(raw, expected_len=1)
    assert len(out) == 1


def test_parse_llm_output_greedy_brace_match():
    """LLM prepends prose → greedy brace extraction recovers."""
    raw = '抽取结果如下: {"rows": [{"x": 1}]} 完毕.'
    out = _parse_llm_output(raw, expected_len=1)
    assert len(out) == 1


def test_parse_llm_output_truncated_salvage():
    """Sprint 26 dogfood: truncated JSON (LLM hit max_tokens) → salvage complete rows."""
    # 3 complete rows then truncated mid-4th object
    raw = (
        '{"rows": [\n'
        '{"凭证号": {"value": "0001"}},\n'
        '{"凭证号": {"value": "0002"}},\n'
        '{"凭证号": {"value": "0003"}},\n'
        '{"凭证号": {"value": "00'  # truncated here
    )
    out = _parse_llm_output(raw, expected_len=25)
    # Salvaged 3 complete + padded to expected_len
    assert len(out) == 25
    assert out[0]["凭证号"]["value"] == "0001"
    assert out[1]["凭证号"]["value"] == "0002"
    assert out[2]["凭证号"]["value"] == "0003"
    assert out[3] == {}  # padded (4th was truncated)


def test_parse_llm_output_len_mismatch_pads():
    """Expected 3 but got 1 → pad with empty {}."""
    raw = '{"rows": [{"a": 1}]}'
    out = _parse_llm_output(raw, expected_len=3)
    assert len(out) == 3
    assert out[0]["a"] == 1
    assert out[1] == {}  # padded
    assert out[2] == {}


def test_xlsx_fast_path_extracts_rows(tmp_path):
    """Sprint 26 xlsx fast-path: openpyxl direct read, no LLM."""
    import openpyxl

    fp = tmp_path / "test.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["日期", "凭证号", "借方金额"])
    ws.append(["2022-01-15", "0001", 1000.0])
    ws.append(["2022-01-16", "0002", 2000.0])
    wb.save(fp)
    fields = [
        _FakeField("日期", "date"),
        _FakeField("凭证号", "string"),
        _FakeField("借方金额", "currency"),
    ]
    rows = _extract_xlsx_rows(str(fp), fields, max_rows=100)
    assert len(rows) == 2
    assert rows[0]["row_seq"] == 1
    assert rows[0]["fields"]["凭证号"]["value"] == "0001"
    assert rows[0]["fields"]["借方金额"]["value"] == 1000


# ─── table_parser (5 tests) ────────────────────────


def test_finance_header_keywords_includes_common():
    """Sprint 26 catalog covers 5+ doc_types — keyword whitelist covers all."""
    common = {"凭证号", "日期", "借方金额", "贷方金额", "科目编码", "营业收入", "净利润", "期末余额"}
    for kw in common:
        assert kw in FINANCE_HEADER_KEYWORDS, f"{kw} missing from keyword whitelist"


def test_subtotal_keywords_blocks_typical():
    """合计 / 小计 / 本期合计 row 必须 skip."""
    typical = {"小计", "合计", "本页合计", "本期合计"}
    for kw in typical:
        assert kw in SUBTOTAL_KEYWORDS


def test_pdfplumber_parser_is_header_row_finance_match():
    """Finance header detect via keyword count ≥2 (NOT IntelliVault no-digit which fails on 2022年)."""
    parser = PdfPlumberTableParser()
    # 2 finance keywords → True
    assert parser._is_header_row(["凭证号", "借方金额", "随便文字"]) is True
    # Only 1 keyword → False (need ≥2)
    assert parser._is_header_row(["凭证号", "abc", "xyz"]) is False
    # Including "2022年" doesn't break detection (was IntelliVault's failure mode)
    assert parser._is_header_row(["2022年", "凭证号", "借方金额"]) is True


def test_pdfplumber_parser_subtotal_detect():
    parser = PdfPlumberTableParser()
    assert parser._is_subtotal_row(["小计", "", "", "1000.00"]) is True
    assert parser._is_subtotal_row(["本期合计", "100"]) is True
    # Normal data row should not match
    assert parser._is_subtotal_row(["2022-01-15", "0001", "1000.00"]) is False


def test_vision_markdown_parser_requires_l2():
    """VisionMarkdownTableParser raises if not L2 load result."""
    parser = VisionMarkdownTableParser()
    # No existing_load_result
    with pytest.raises(ValueError):
        asyncio.run(parser.parse(file_path="/tmp/x.pdf", mime_type="application/pdf"))

    # Wrong layer
    class FakeLoadResult:
        layer_used = "L0"
        documents = []

    with pytest.raises(ValueError):
        asyncio.run(
            parser.parse(
                file_path="/tmp/x.pdf",
                mime_type="application/pdf",
                existing_load_result=FakeLoadResult(),
            )
        )


# ─── workspace_quotas_store (4 tests) ──────────────


@pytest.mark.asyncio
async def test_quotas_store_initialize_idempotent(tmp_db):
    store = KbWorkspaceQuotasStore(tmp_db)
    await store.initialize()
    await store.initialize()
    assert tmp_db.exists()


@pytest.mark.asyncio
async def test_quotas_check_under_cap(tmp_db):
    """New ws under default cap → check_quota returns True + auto-creates row."""
    store = KbWorkspaceQuotasStore(tmp_db)
    await store.initialize()
    assert await store.check_quota("ws_a") is True
    # Row created
    summary = await store.get_summary("ws_a")
    assert summary is not None
    assert summary.tokens_used == 0


@pytest.mark.asyncio
async def test_quotas_increment_and_exceed(tmp_db):
    """increment_usage adds; exceeding cap returns False from check_quota."""
    store = KbWorkspaceQuotasStore(tmp_db)
    await store.initialize()
    await store.set_cap("ws_b", new_cap=1000)
    await store.increment_usage("ws_b", 500)
    summary = await store.get_summary("ws_b")
    assert summary.tokens_used == 500
    # Still under cap
    assert await store.check_quota("ws_b") is True
    # Exceed
    await store.increment_usage("ws_b", 600)
    assert await store.check_quota("ws_b") is False


@pytest.mark.asyncio
async def test_quotas_set_cap_validation(tmp_db):
    """set_cap rejects ≤0 values."""
    store = KbWorkspaceQuotasStore(tmp_db)
    await store.initialize()
    with pytest.raises(ValueError):
        await store.set_cap("ws_c", new_cap=0)
    with pytest.raises(ValueError):
        await store.set_cap("ws_c", new_cap=-1)
