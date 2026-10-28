"""kb-knowledge-tables sprint Day 4: KbWorker structured extraction integration test.

Validates the new path: file.doc_type_id → after parse + index, run StructuredExtractor →
save KbExtractedRecord + bump KbDocType.record_count.

Mocks: embedder + chromadb store + LLM router (no real qwen call).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.doc_type_store import ExtractField, KbDocTypeStore
from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.structured_extractor import StructuredExtractor
from bowhead_core.kb.worker import KbWorker

if TYPE_CHECKING:
    from pathlib import Path

# ─── helpers ─────────


class _StubEmbedder:
    """Returns deterministic 4-dim embeddings for any text batch."""

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _StubChromaStore:
    """In-memory chunks store; just counts what was added."""

    def __init__(self) -> None:
        self._added: list = []

    async def add_chunks(self, file, chunks):  # type: ignore[no-untyped-def]
        self._added.extend(chunks)

    async def count_chunks(self, file) -> int:  # type: ignore[no-untyped-def]
        return len(self._added)

    async def hard_delete_file(self, file) -> int:  # type: ignore[no-untyped-def]
        n = len(self._added)
        self._added = []
        return n


def _mock_llm_router(content: str) -> object:
    resp = MagicMock(content=content)
    r = MagicMock()
    r.invoke = AsyncMock(return_value=resp)
    return r


# ─── tests ─────────


@pytest.mark.asyncio
async def test_worker_runs_extraction_and_saves_record(initialize_migrated_kb_files_database, tmp_path: Path) -> None:
    # Set up KB stores.
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    rec_store = KbExtractedRecordStore(tmp_path / "rec.db")
    await rec_store.initialize()

    # 1) Create doc_type with 2 fields.
    dt_id = await dt_store.create(
        workspace_id="ws_a",
        name="销售合同",
        created_by="u1",
        fields=[
            ExtractField(name="合同金额", type="currency", description="合同总价"),
            ExtractField(name="起始日", type="date", description="合同生效日期"),
        ],
    )

    # 2) Write a fake markdown file with contract text.
    contract_path = tmp_path / "contract.md"
    contract_path.write_text(
        "# 销售合同\n\n第一条 合同金额\n本协议合同总金额为人民币伍佰万元 (RMB 5,000,000), 含税.\n\n第二条 期限\n本合同自2024年1月15日起生效.\n",
        encoding="utf-8",
    )

    # 3) Register file in kb_files store with doc_type_id binding.
    file_id = await files_store.create(
        artifact_id=str(contract_path),
        filename="contract.md",
        mime_type="text/markdown",
        size_bytes=contract_path.stat().st_size,
        scope="workspace",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u1",
        doc_type_id=dt_id,
    )

    # 4) Build extractor with mock LLM that returns proper extracted JSON.
    llm_out = json.dumps(
        {
            "合同金额": {
                "value": "5000000",
                "source_snippet": "合同总金额为人民币伍佰万元",
                "source_page": 1,
                "confidence": 0.95,
            },
            "起始日": {
                "value": "2024-01-15",
                "source_snippet": "本合同自2024年1月15日起生效",
                "source_page": 1,
                "confidence": 0.9,
            },
        },
        ensure_ascii=False,
    )
    extractor = StructuredExtractor(router=_mock_llm_router(llm_out))

    # 5) Run worker.
    worker = KbWorker(
        files_store=files_store,
        store=_StubChromaStore(),  # type: ignore[arg-type]
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
        artifact_fetcher=None,  # tests mode: artifact_id is the path
        doc_type_store=dt_store,
        extracted_record_store=rec_store,
        structured_extractor=extractor,
    )
    await worker.parse_and_index(file_id)

    # 6) Verify record was saved.
    records = await rec_store.list_for_doc_type(
        workspace_id="ws_a",
        doc_type_id=dt_id,
    )
    assert len(records) == 1
    rec = records[0]
    assert rec.file_id == file_id
    # v2 shape: variant array; primary variant carries normalized.amount/value.
    # Step 5 wires worker → create(doc_type_fields=doc_type.fields), so currency
    # fields get proper {amount, currency} shape (no longer degraded).
    amt_norm = rec.fields["合同金额"][0]["normalized"]
    assert amt_norm == {"amount": "5000000", "currency": None}, f"expected currency-coerced amount shape, got {amt_norm!r}"
    date_norm = rec.fields["起始日"][0]["normalized"]
    assert date_norm == {"value": "2024-01-15"}
    assert rec.verify_status == "ready"  # all conf > 0.5
    assert rec.extractor_model == "qwen-plus"

    # 7) Verify doc_type.record_count bumped.
    dt = await dt_store.get(dt_id)
    assert dt is not None
    assert dt.record_count == 1


@pytest.mark.asyncio
async def test_worker_skips_extraction_without_doc_type(initialize_migrated_kb_files_database, tmp_path: Path) -> None:
    """File without doc_type_id → no extraction, parse still completes."""
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    rec_store = KbExtractedRecordStore(tmp_path / "rec.db")
    await rec_store.initialize()

    contract_path = tmp_path / "doc.md"
    contract_path.write_text("plain content", encoding="utf-8")
    file_id = await files_store.create(
        artifact_id=str(contract_path),
        filename="doc.md",
        mime_type="text/markdown",
        size_bytes=contract_path.stat().st_size,
        scope="workspace",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u1",
        doc_type_id=None,
    )

    extractor = StructuredExtractor(router=_mock_llm_router("{}"))
    worker = KbWorker(
        files_store=files_store,
        store=_StubChromaStore(),  # type: ignore[arg-type]
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
        artifact_fetcher=None,
        extracted_record_store=rec_store,
        structured_extractor=extractor,
    )
    await worker.parse_and_index(file_id)

    # No records created.
    records = await rec_store.list_for_file(file_id)
    assert len(records) == 0
    # File still went to done.
    file = await files_store.get(file_id)
    assert file is not None
    assert file.parse_status == "done"


@pytest.mark.asyncio
async def test_worker_low_confidence_marks_needs_review(initialize_migrated_kb_files_database, tmp_path: Path) -> None:
    """Field confidence < 0.5 → record.verify_status = needs_review."""
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    rec_store = KbExtractedRecordStore(tmp_path / "rec.db")
    await rec_store.initialize()

    dt_id = await dt_store.create(
        workspace_id="ws_a",
        name="X",
        created_by="u1",
        fields=[ExtractField(name="X", type="string")],
    )
    contract_path = tmp_path / "doc.md"
    contract_path.write_text("uncertain text...", encoding="utf-8")
    file_id = await files_store.create(
        artifact_id=str(contract_path),
        filename="doc.md",
        mime_type="text/markdown",
        size_bytes=contract_path.stat().st_size,
        scope="workspace",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u1",
        doc_type_id=dt_id,
    )

    # LLM returns low-confidence answer
    llm_out = json.dumps(
        {
            "X": {"value": "maybe", "source_snippet": "", "confidence": 0.3},
        },
        ensure_ascii=False,
    )
    extractor = StructuredExtractor(router=_mock_llm_router(llm_out))
    worker = KbWorker(
        files_store=files_store,
        store=_StubChromaStore(),  # type: ignore[arg-type]
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
        artifact_fetcher=None,
        doc_type_store=dt_store,
        extracted_record_store=rec_store,
        structured_extractor=extractor,
    )
    await worker.parse_and_index(file_id)

    records = await rec_store.list_for_doc_type(
        workspace_id="ws_a",
        doc_type_id=dt_id,
    )
    assert len(records) == 1
    assert records[0].verify_status == "needs_review"


@pytest.mark.asyncio
async def test_worker_extraction_failure_does_not_fail_parse(initialize_migrated_kb_files_database, tmp_path: Path) -> None:
    """Extractor crash → parse still done, no record. No exception bubbled."""
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    rec_store = KbExtractedRecordStore(tmp_path / "rec.db")
    await rec_store.initialize()

    dt_id = await dt_store.create(
        workspace_id="ws_a",
        name="X",
        created_by="u1",
        fields=[ExtractField(name="X", type="string")],
    )
    contract_path = tmp_path / "doc.md"
    contract_path.write_text("text", encoding="utf-8")
    file_id = await files_store.create(
        artifact_id=str(contract_path),
        filename="doc.md",
        mime_type="text/markdown",
        size_bytes=contract_path.stat().st_size,
        scope="workspace",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u1",
        doc_type_id=dt_id,
    )

    # extractor whose .extract() raises
    bad_extractor = MagicMock()
    bad_extractor.extract = AsyncMock(side_effect=RuntimeError("boom"))

    worker = KbWorker(
        files_store=files_store,
        store=_StubChromaStore(),  # type: ignore[arg-type]
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
        artifact_fetcher=None,
        doc_type_store=dt_store,
        extracted_record_store=rec_store,
        structured_extractor=bad_extractor,
    )
    await worker.parse_and_index(file_id)

    # parse_status = done (chunks did finish), but no record.
    file = await files_store.get(file_id)
    assert file is not None
    assert file.parse_status == "done"
    records = await rec_store.list_for_file(file_id)
    assert len(records) == 0
    # record_count stays 0.
    dt = await dt_store.get(dt_id)
    assert dt is not None
    assert dt.record_count == 0


@pytest.mark.asyncio
async def test_worker_doc_type_soft_deleted_skips(initialize_migrated_kb_files_database, tmp_path: Path) -> None:
    """File binds doc_type but doc_type was soft-deleted → skip extraction (no crash)."""
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    rec_store = KbExtractedRecordStore(tmp_path / "rec.db")
    await rec_store.initialize()

    dt_id = await dt_store.create(
        workspace_id="ws_a",
        name="X",
        created_by="u1",
        fields=[ExtractField(name="X", type="string")],
    )
    contract_path = tmp_path / "doc.md"
    contract_path.write_text("x", encoding="utf-8")
    file_id = await files_store.create(
        artifact_id=str(contract_path),
        filename="doc.md",
        mime_type="text/markdown",
        size_bytes=contract_path.stat().st_size,
        scope="workspace",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u1",
        doc_type_id=dt_id,
    )
    # Soft-delete the doc_type before file is indexed.
    await dt_store.soft_delete(dt_id)

    extractor = StructuredExtractor(router=_mock_llm_router("{}"))
    worker = KbWorker(
        files_store=files_store,
        store=_StubChromaStore(),  # type: ignore[arg-type]
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
        artifact_fetcher=None,
        doc_type_store=dt_store,
        extracted_record_store=rec_store,
        structured_extractor=extractor,
    )
    await worker.parse_and_index(file_id)

    file = await files_store.get(file_id)
    assert file is not None
    assert file.parse_status == "done"
    records = await rec_store.list_for_file(file_id)
    assert len(records) == 0
