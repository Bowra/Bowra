"""Provenance path B (kb-optimization) — chunk_id backfill for doc-table fields.

The structured extractor works on concatenated full_text and has no chunk_id.
``bind_field_sources`` recovers each field's origin chunk by fuzzy-matching its
``source_snippet`` against the file's indexed chunks (reusing the same
whitespace-insensitive normalization as citation grounding). ``wrap_extractor_
result_to_variants`` then persists the recovered chunk_id (was hardcoded None).
Pages derived from the same match are covered in ``test_kb_source_location.py``.

Surfaced by the KB retrieval eval / provenance spike:
docs/sprints/kb-optimization/spike-trustworthiness.md §3.
"""

from __future__ import annotations

from bowhead_core.kb.extracted_record_store import wrap_extractor_result_to_variants
from bowhead_core.kb.source_location import SourceChunk, SourceIndex, bind_field_sources

_CHUNKS = SourceIndex(
    [
        SourceChunk("chunk_aaa", "第一章 合同总则。本合同由甲乙双方签订。", 1),
        SourceChunk("chunk_bbb", "合同金额为人民币 100 万元整，付款方式为季度支付。", 2),
        SourceChunk("chunk_ccc", "违约责任：任一方违约需支付未履行金额 5% 的违约金。", 3),
    ],
    "text/markdown",
)


def test_backfill_matches_field_to_origin_chunk() -> None:
    fields = {
        "合同金额": {"value": "100万", "source_snippet": "合同金额为人民币 100 万元整", "confidence": 0.9},
        "违约金比例": {"value": "5%", "source_snippet": "支付未履行金额 5% 的违约金", "confidence": 0.8},
    }
    bound = bind_field_sources(fields, _CHUNKS)
    assert bound == 2
    assert fields["合同金额"]["chunk_id"] == "chunk_bbb"
    assert fields["违约金比例"]["chunk_id"] == "chunk_ccc"


def test_backfill_whitespace_insensitive() -> None:
    # snippet has different whitespace than the chunk — must still match.
    fields = {"f": {"value": "x", "source_snippet": "合同金额 为人民币100万元 整"}}
    bound = bind_field_sources(fields, _CHUNKS)
    assert bound == 1
    assert fields["f"]["chunk_id"] == "chunk_bbb"


def test_backfill_no_match_binds_nothing() -> None:
    fields = {"f": {"value": "x", "source_snippet": "这段文字在任何 chunk 里都不存在的内容", "chunk_id": "chunk_model"}}
    bound = bind_field_sources(fields, _CHUNKS)
    assert bound == 0
    assert fields["f"]["chunk_id"] is None  # A chunk ID the model wrote is never kept.


def test_backfill_empty_snippet_or_no_chunks() -> None:
    assert bind_field_sources({"f": {"value": "x", "source_snippet": ""}}, _CHUNKS) == 0
    assert bind_field_sources({"f": {"value": "x"}}, _CHUNKS) == 0  # no snippet key
    assert bind_field_sources({"f": {"value": "x", "source_snippet": "合同金额"}}, SourceIndex([], "text/markdown")) == 0  # no chunks


def test_wrap_persists_backfilled_chunk_id() -> None:
    """The fresh-extract wrapper now reads chunk_id from the field (was None)."""
    fields = {"合同金额": {"value": "100万", "source_snippet": "合同金额为人民币 100 万元整", "chunk_id": "chunk_bbb"}}
    variants = wrap_extractor_result_to_variants(fields)
    v = variants["合同金额"][0]
    assert v["is_primary"] is True
    assert v["chunk_id"] == "chunk_bbb"
    assert v["source_excerpt"] == "合同金额为人民币 100 万元整"


def test_wrap_chunk_id_stays_none_without_backfill() -> None:
    """No chunk_id in field (backfill found no match) → variant chunk_id None (unchanged)."""
    fields = {"合同金额": {"value": "100万", "source_snippet": "合同金额为人民币 100 万元整"}}
    variants = wrap_extractor_result_to_variants(fields)
    assert variants["合同金额"][0]["chunk_id"] is None
