"""Document matrix step 1, S7: pages come from the file, never from the extraction model.

Rules (tech-design-step1.md §7): PDF pages from the loader's ``## Page N`` markers, carried across
chunks in ``chunk_index`` order; PPTX and images from the chunk metadata page; DOCX, XLSX,
Markdown and other types get no page (the UI shows the paragraph from ``chunk_index``);
unverified quotes get nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_core.kb.loader.pptx_loader import PPTX_MIME
from bowhead_core.kb.source_location import SourceChunk, SourceIndex, SourceLocation, bind_field_sources

if TYPE_CHECKING:
    from pathlib import Path

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Three chunks of a four-page PDF as the paragraph splitter leaves them: page 2 runs on into
# chunk 2, which has no marker of its own; chunk 3 starts mid-page 3 and then opens page 4.
_PDF_CHUNKS = [
    SourceChunk("chk_1", "## Page 1\n\n甲方：北京某科技有限公司\n\n## Page 2\n\n第一条 合同总金额为人民币伍佰万元。", 1),
    SourceChunk("chk_2", "第二条 付款方式为分三期支付，\n首期于签约后十日内支付。\n\n## Page 3\n\n第三条 交付地点为乙方仓库", 2),
    SourceChunk("chk_3", "，运输费用由甲方承担。\n\n## Page 4\n\n第四条 违约金为合同总额的百分之五。", 3),
]


def _pdf(chunks: list[SourceChunk] | None = None) -> SourceIndex:
    return SourceIndex(chunks if chunks is not None else _PDF_CHUNKS, PDF)


def test_pdf_page_is_the_last_marker_before_the_excerpt() -> None:
    assert _pdf().locate("合同总金额为人民币伍佰万元") == SourceLocation("chk_1", 1, 2)
    assert _pdf().locate("甲方：北京某科技有限公司") == SourceLocation("chk_1", 1, 1)
    assert _pdf().locate("违约金为合同总额的百分之五") == SourceLocation("chk_3", 3, 4)


def test_pdf_page_carries_across_chunks_in_chunk_index_order() -> None:
    # Chunk 2 has no marker before the excerpt: the page is chunk 1's last marker.
    assert _pdf().locate("付款方式为分三期支付") == SourceLocation("chk_2", 2, 2)
    # The carried page follows chunk_index, not the order the store returned the chunks in.
    shuffled = [_PDF_CHUNKS[2], _PDF_CHUNKS[0], _PDF_CHUNKS[1]]
    assert _pdf(shuffled).locate("付款方式为分三期支付") == SourceLocation("chk_2", 2, 2)
    assert _pdf(shuffled).locate("运输费用由甲方承担") == SourceLocation("chk_3", 3, 3)


def test_pdf_excerpt_matches_whitespace_insensitively() -> None:
    # PDF text breaks lines inside sentences; the model quotes without the break.
    assert _pdf().locate("首期于签约后 十日内支付") == SourceLocation("chk_2", 2, 2)
    assert _pdf().locate("分三期支付，首期于签约后") == SourceLocation("chk_2", 2, 2)


def test_pdf_excerpt_straddling_chunks_is_attributed_to_the_chunk_where_it_starts() -> None:
    # "交付地点为乙方仓库，运输费用由甲方承担" starts in chunk 2 (page 3) and ends in chunk 3.
    assert _pdf().locate("交付地点为乙方仓库，运输费用由甲方承担") == SourceLocation("chk_2", 2, 3)
    # One starting in chunk 1 is still on chunk 1's page 2.
    assert _pdf().locate("伍佰万元。第二条 付款方式") == SourceLocation("chk_1", 1, 2)


def test_pdf_excerpt_starting_with_a_marker_is_on_that_page() -> None:
    assert _pdf().locate("## Page 4 第四条 违约金") == SourceLocation("chk_3", 3, 4)


def test_pdf_markers_win_over_metadata_pages() -> None:
    # Long PDFs carry the framework inferrer's guessed section page in metadata.
    chunks = [SourceChunk("chk_1", "## Page 1\n\n前言\n\n## Page 2\n\n第一节 适用范围", 1, page=7)]
    assert _pdf(chunks).locate("第一节 适用范围") == SourceLocation("chk_1", 1, 2)
    # Without any marker the metadata page is all there is.
    assert _pdf([SourceChunk("chk_1", "第一节 适用范围", 1, page=7)]).locate("适用范围") == SourceLocation("chk_1", 1, 7)


def test_markdown_with_a_literal_page_heading_has_no_page() -> None:
    chunks = [SourceChunk("chk_1", "## Page 3\n\n这一节的标题恰好写作 Page 3。", 4)]
    assert SourceIndex(chunks, "text/markdown").locate("这一节的标题恰好写作") == SourceLocation("chk_1", 4, None)
    assert SourceIndex(chunks, "text/plain").locate("这一节的标题恰好写作") == SourceLocation("chk_1", 4, None)


def test_pptx_and_images_use_the_metadata_page() -> None:
    slides = [
        SourceChunk("chk_1", "# 第 1 页\n\n年度回顾", 1, page=1),
        SourceChunk("chk_2", "# 第 3 页\n\n收入增长 12%", 2, page=3),  # slide 2 had no text
    ]
    assert SourceIndex(slides, PPTX_MIME).locate("收入增长 12%") == SourceLocation("chk_2", 2, 3)
    image = [SourceChunk("chk_1", "发票号码 No.12345", 1, page=1)]
    assert SourceIndex(image, "image/png").locate("发票号码") == SourceLocation("chk_1", 1, 1)


def test_docx_has_no_page_even_with_a_metadata_page() -> None:
    chunks = [SourceChunk("chk_1", "引言", 1), SourceChunk("chk_2", "合同金额为人民币 100 万元整。", 2, page=3)]
    assert SourceIndex(chunks, DOCX).locate("合同金额为人民币") == SourceLocation("chk_2", 2, None)


def test_mime_type_parameters_and_case_are_ignored() -> None:
    assert SourceIndex(_PDF_CHUNKS, "Application/PDF; charset=binary").locate("违约金为合同总额") == SourceLocation("chk_3", 3, 4)


def test_missing_or_empty_excerpt_is_not_located() -> None:
    assert _pdf().locate("这句话不在文件里") is None
    assert _pdf().locate("") is None
    assert _pdf().locate("   ") is None
    assert _pdf().locate(None) is None
    assert _pdf([]).locate("合同总金额") is None


def test_bind_replaces_the_model_page_with_the_derived_one() -> None:
    fields = {
        "金额": {"value": "5000000", "source_snippet": "合同总金额为人民币伍佰万元", "source_page": 1, "citation_verified": True},
        "地点": {"value": "乙方仓库", "source_snippet": "交付地点为乙方仓库，运输费用", "source_page": 9, "citation_verified": True},
    }
    assert bind_field_sources(fields, _pdf()) == 2
    assert (fields["金额"]["chunk_id"], fields["金额"]["chunk_index"], fields["金额"]["source_page"]) == ("chk_1", 1, 2)
    assert (fields["地点"]["chunk_id"], fields["地点"]["chunk_index"], fields["地点"]["source_page"]) == ("chk_2", 2, 3)


def test_bind_gives_unverified_quotes_no_chunk_and_no_page() -> None:
    # Citation grounding moved the quote it could not find aside and cleared source_snippet.
    fields = {
        "金额": {
            "value": "5000000",
            "source_snippet": "",
            "source_snippet_unverified": "总价五百万",
            "source_page": 3,
            "citation_verified": False,
        },
        "无引文": {"value": None, "source_page": 2},
    }
    assert bind_field_sources(fields, _pdf()) == 0
    for name in fields:
        assert (fields[name]["chunk_id"], fields[name]["chunk_index"], fields[name]["source_page"]) == (None, None, None)


def test_bind_drops_the_model_page_for_docx() -> None:
    fields = {"金额": {"value": "100万", "source_snippet": "合同金额为人民币 100 万元整", "source_page": 5, "citation_verified": True}}
    chunks = SourceIndex([SourceChunk("chk_9", "合同金额为人民币 100 万元整。", 9)], DOCX)
    assert bind_field_sources(fields, chunks) == 1
    assert (fields["金额"]["chunk_id"], fields["金额"]["chunk_index"], fields["金额"]["source_page"]) == ("chk_9", 9, None)


async def test_store_reads_chunks_in_order_with_their_metadata_page(tmp_path: Path) -> None:
    from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
    from bowhead_core.kb.schemas import KbFileDTO
    from bowhead_core.kb.store import KbStore

    kb_store = KbStore(str(tmp_path / "chroma"))
    file = KbFileDTO(
        file_id="kbf_deck",
        artifact_id="a",
        filename="deck.pptx",
        mime_type=PPTX_MIME,
        size_bytes=1,
        scope="workspace",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u1",
        uploaded_at="2026-01-01T00:00:00+00:00",
    )
    chunks = [
        KbChunk(
            chunk_id=make_chunk_id("kbf_deck", idx, content),
            file_id="kbf_deck",
            content=content,
            embedding=None,
            metadata=ChunkMetadata(
                file_id="kbf_deck",
                file_name="deck.pptx",
                scope="workspace",
                scope_owner="ws_a",
                workspace_id="ws_a",
                team_id="team_a",
                chunk_index=idx,
                page=page,
            ),
        )
        for idx, content, page in [(2, "第三张", 3), (1, "第一张", 1), (3, "无页码", None)]
    ]
    await kb_store.add_chunks(file, chunks)
    ordered = await kb_store.get_ordered_source_chunks(file)
    assert [(c.content, c.chunk_index, c.page) for c in ordered] == [("第一张", 1, 1), ("第三张", 2, 3), ("无页码", 3, None)]
    assert await kb_store.get_ordered_chunks_for_file(file) == [(c.chunk_id, c.content, c.chunk_index) for c in ordered]
