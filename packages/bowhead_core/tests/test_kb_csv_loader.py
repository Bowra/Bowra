"""知识库接受 CSV（知识库体验整治 §10 Q5）：编码、分隔符、按表格切分（表头随块重复）、端到端入库。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.embedder import EMBED_DIM
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.loader.csv_loader import CsvLoader
from bowhead_core.kb.loader.dispatch import get_loader
from bowhead_core.kb.worker import KbWorker

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_ROWS = [f"门店{i},{100 + i},华东" for i in range(120)]


def _csv(tmp_path: Path, name: str, text: str, encoding: str) -> str:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return str(path)


@pytest.mark.parametrize(
    ("encoding", "delimiter"),
    [("utf-8-sig", ","), ("gb18030", ","), ("utf-8", "\t")],
)
async def test_csv_is_chunked_like_a_table_with_header_in_every_chunk(tmp_path, encoding, delimiter) -> None:
    body = "\n".join(["门店,营业额,区域", *_ROWS]).replace(",", delimiter)
    result = await CsvLoader().load(_csv(tmp_path, "sales.csv", body, encoding))

    assert not result.metadata.get("parse_failed")
    docs = result.documents
    assert len(docs) == 3  # 120 rows / 50 per chunk.
    assert all("门店" in d.page_content and "营业额" in d.page_content for d in docs)
    assert "门店119" in docs[-1].page_content
    assert "门店0" in docs[0].page_content


def test_dispatch_routes_text_csv_to_csv_loader() -> None:
    assert isinstance(get_loader("text/csv"), CsvLoader)


@pytest.fixture
async def files_store(initialize_migrated_kb_files_database, tmp_path):
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    s = KbFilesStore(tmp_path / "kb_files.db")
    await s.initialize()
    return s


async def test_worker_indexes_csv_end_to_end(files_store, tmp_path) -> None:
    path = _csv(tmp_path, "sales.csv", "\n".join(["门店,营业额,区域", *_ROWS]), "gb18030")
    fid = await files_store.create(
        artifact_id=path,
        filename="sales.csv",
        mime_type="text/csv",
        size_bytes=1,
        scope="personal",
        scope_id="u1",
        workspace_id="ws_1",
        team_id="team_1",
        uploaded_by="u1",
    )
    added: dict[str, int] = {}

    async def _add(_file, chunks) -> None:
        added["n"] = len(chunks)

    store = MagicMock()
    store.hard_delete_file = AsyncMock(return_value=0)
    store.add_chunks = AsyncMock(side_effect=_add)
    store.count_chunks = AsyncMock(side_effect=lambda _f: added.get("n", 0))
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.1] * EMBED_DIM] * len(texts))

    await KbWorker(files_store=files_store, store=store, embedder=embedder).parse_and_index(fid)

    dto = await files_store.get(fid)
    assert (dto.parse_status, dto.parse_error_code) == ("done", None)
    assert dto.chunks_count >= 3


async def test_worker_never_indexes_the_empty_sheet_placeholder(files_store, tmp_path) -> None:
    """空表的「没找到数据」占位不是内容：不入库，文件如实标为内容为空。"""
    path = _csv(tmp_path, "empty.csv", "\n\n", "utf-8")
    fid = await files_store.create(
        artifact_id=path,
        filename="empty.csv",
        mime_type="text/csv",
        size_bytes=1,
        scope="personal",
        scope_id="u1",
        workspace_id="ws_1",
        team_id="team_1",
        uploaded_by="u1",
    )
    store = MagicMock()
    store.hard_delete_file = AsyncMock(return_value=0)
    store.add_chunks = AsyncMock()
    store.count_chunks = AsyncMock(return_value=0)
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.1] * EMBED_DIM] * len(texts))

    await KbWorker(files_store=files_store, store=store, embedder=embedder).parse_and_index(fid)

    store.add_chunks.assert_not_awaited()
    dto = await files_store.get(fid)
    assert (dto.parse_status, dto.parse_error_code) == ("failed", "empty_content")


async def test_rows_beyond_the_index_cap_are_reported_not_silently_cut(tmp_path) -> None:
    """超过行数上限（5000）不再静默截断：load metadata 记录截断，最后一个表格块带说明。"""
    from bowhead_core.kb.loader.xlsx_loader import MAX_TOTAL_ROWS_PER_WORKBOOK

    total = MAX_TOTAL_ROWS_PER_WORKBOOK + 3
    body = "\n".join(["门店,营业额", *(f"门店{i},{i}" for i in range(total - 1))])

    result = await CsvLoader().load(_csv(tmp_path, "big.csv", body, "utf-8"))

    assert result.metadata["truncated_rows"] == {"indexed": MAX_TOTAL_ROWS_PER_WORKBOOK, "total": total}
    assert f"本文件共 {total} 行，仅索引了前 {MAX_TOTAL_ROWS_PER_WORKBOOK} 行" in result.documents[-1].page_content
    assert not any("仅索引了前" in d.page_content for d in result.documents[:-1])


async def test_small_csv_has_no_truncation_record(tmp_path) -> None:
    result = await CsvLoader().load(_csv(tmp_path, "small.csv", "\n".join(["门店,营业额,区域", *_ROWS]), "utf-8"))

    assert "truncated_rows" not in result.metadata
    assert not any("仅索引了前" in d.page_content for d in result.documents)


async def test_truncation_is_exposed_on_the_file_dto_and_cleared_on_clean_reparse(files_store, tmp_path) -> None:
    from pathlib import Path

    from bowhead_core.kb.loader.xlsx_loader import MAX_TOTAL_ROWS_PER_WORKBOOK

    total = MAX_TOTAL_ROWS_PER_WORKBOOK + 10
    path = _csv(tmp_path, "big.csv", "\n".join(["门店,营业额", *(f"门店{i},{i}" for i in range(total - 1))]), "utf-8")
    fid = await files_store.create(
        artifact_id=path,
        filename="big.csv",
        mime_type="text/csv",
        size_bytes=1,
        scope="personal",
        scope_id="u1",
        workspace_id="ws_1",
        team_id="team_1",
        uploaded_by="u1",
    )
    added: dict[str, int] = {}

    async def _add(_file, chunks) -> None:
        added["n"] = len(chunks)

    store = MagicMock()
    store.hard_delete_file = AsyncMock(return_value=0)
    store.add_chunks = AsyncMock(side_effect=_add)
    store.count_chunks = AsyncMock(side_effect=lambda _f: added.get("n", 0))
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.1] * EMBED_DIM] * len(texts))
    worker = KbWorker(files_store=files_store, store=store, embedder=embedder)

    await worker.parse_and_index(fid)
    dto = await files_store.get(fid)
    assert dto.parse_status == "done"
    assert dto.truncated_rows is not None
    assert (dto.truncated_rows.indexed, dto.truncated_rows.total) == (MAX_TOTAL_ROWS_PER_WORKBOOK, total)

    # Forced reparse after replacing with small table: no truncation, old records cleared.
    Path(path).write_text("\n".join(["门店,营业额,区域", *_ROWS]), encoding="utf-8")
    await worker.parse_and_index(fid, allow_terminal=True)
    dto = await files_store.get(fid)
    assert dto.parse_status == "done"
    assert dto.truncated_rows is None


async def test_csv_header_is_exactly_the_first_row_even_when_data_is_all_text(tmp_path) -> None:
    """CSV 全是字符串：多行表头启发式会把数据行当表头（116 前实测 3 行），CSV 固定首行为表头。"""
    body = "合同号,客户,状态\nHT-001,华联超市,已签\nHT-002,王府井百货,审批中\n"
    doc = (await CsvLoader().load(_csv(tmp_path, "contracts.csv", body, "utf-8"))).documents[0]

    lines = doc.page_content.splitlines()
    assert doc.metadata["header_row_count"] == 1
    assert lines[1] == "| 合同号 | 客户 | 状态 |"
    assert lines[2].startswith("|---")
    assert "HT-001" in lines[3]


async def test_table_chunks_fit_the_splitter_so_every_indexed_chunk_keeps_the_header(tmp_path) -> None:
    """表格块渲染后不超过切分器上限：worker 不再二次切开，入库的每一片都带表头
    （116 实测 151 行 CSV 被切成 10 片、只有 4 片带表头）。"""
    from types import SimpleNamespace

    from bowhead_core.kb.splitter.paragraph_aware import DEFAULT_CHUNK_SIZE, count_tokens

    header = "日期,门店,品类,销售额(元),客流,备注"
    rows = [f"2025-09-{i % 30 + 1:02d},门店{i},生鲜,{10000 + i},{300 + i},本周促销活动带动客流明显上升需持续跟踪" for i in range(150)]
    loaded = await CsvLoader().load(_csv(tmp_path, "wide.csv", "\n".join([header, *rows]), "utf-8"))
    docs = loaded.documents

    assert len(docs) > 3
    assert all(count_tokens(d.page_content) <= DEFAULT_CHUNK_SIZE for d in docs)
    assert all("| 日期 | 门店 | 品类 |" in d.page_content for d in docs)
    assert sum(d.metadata["row_count"] for d in docs) == 150

    worker = KbWorker(files_store=MagicMock(), store=MagicMock(), embedder=MagicMock())
    indexed = await worker._split(MagicMock(mime_type="text/csv"), SimpleNamespace(documents=docs, metadata={}))
    assert len(indexed) == len(docs)
