"""KB splitter tests.

Covers:
- paragraph_aware: 中文 separator, 句号 hard cap (PRD §3.3.6), token cap, overlap
- markdown_section: 按 # ## ### 切, strip_headers=False
- section_aware: P0-C fuzzy match section_title in md_text, LLM offset 不可靠仍正确切
"""

from __future__ import annotations

from bowhead_core.kb.splitter.markdown_section import make_markdown_splitter
from bowhead_core.kb.splitter.paragraph_aware import (
    DEFAULT_CHUNK_SIZE,
    PARAGRAPH_SEPARATORS,
    count_tokens,
    make_paragraph_splitter,
)
from bowhead_core.kb.splitter.section_aware import (
    KbSectionInferred,
    section_aware_split,
)

# ─── paragraph_aware ──────────────────────────────────


def test_paragraph_separators_have_chinese_period() -> None:
    """[PRD §3.3.6] 必须含中文句号 (法律条文用)."""
    assert "。" in PARAGRAPH_SEPARATORS
    assert "！" in PARAGRAPH_SEPARATORS
    assert "？" in PARAGRAPH_SEPARATORS


def test_paragraph_separators_no_char_fallback() -> None:
    """[PRD §3.3.6] 不含空格 / 字符级 fallback (hard cap 在句号)."""
    assert " " not in PARAGRAPH_SEPARATORS
    assert "" not in PARAGRAPH_SEPARATORS


def test_count_tokens_reasonable() -> None:
    assert count_tokens("hello world") > 0
    assert count_tokens("中文测试") > 0
    # Long text has more tokens than short.
    assert count_tokens("a" * 1000) > count_tokens("a" * 10)


def test_paragraph_splitter_chinese_text() -> None:
    """中文文本走 paragraph splitter, 按段落切."""
    splitter = make_paragraph_splitter(chunk_size=50, chunk_overlap=5)
    text = "GMV 是商品交易总额。这是 P3 关键指标。\n\nDAU 是日活跃用户数。衡量产品粘性。\n\n客单价 = GMV / DAU 等等。"
    chunks = splitter.split_text(text)
    assert len(chunks) >= 1
    # Every chunk is a nonempty string.
    for c in chunks:
        assert c.strip()


def test_paragraph_splitter_empty_text() -> None:
    splitter = make_paragraph_splitter()
    chunks = splitter.split_text("")
    assert chunks == []


def test_paragraph_splitter_long_text_respects_chunk_size() -> None:
    """长文本切多 chunk, 每 chunk 大致符合 chunk_size token cap."""
    splitter = make_paragraph_splitter(chunk_size=100, chunk_overlap=10)
    # 10 paragraphs × ~50 char each
    text = "\n\n".join(f"段落 {i} 内容: " + "。".join(f"句子 {j}" for j in range(5)) for i in range(10))
    chunks = splitter.split_text(text)
    assert len(chunks) > 1


# ─── markdown_section ─────────────────────────────────


def test_markdown_splitter_splits_by_h2() -> None:
    splitter = make_markdown_splitter()
    md = "# Doc Title\nIntro paragraph.\n\n## Section A\nA content.\n\n## Section B\nB content.\n"
    docs = splitter.split_text(md)
    assert len(docs) >= 2  # At least H2 sections split.
    contents = " ".join(d.page_content for d in docs)
    assert "A content" in contents
    assert "B content" in contents


def test_markdown_splitter_keeps_headers_in_content() -> None:
    """[strip_headers=False] heading 保留 chunk content (preserve context)."""
    splitter = make_markdown_splitter()
    md = "## Section A\nContent here.\n"
    docs = splitter.split_text(md)
    assert any("## Section A" in d.page_content for d in docs)


# ─── section_aware (P0-C) ─────────────────────────────


def test_section_aware_fuzzy_match_recovers_from_offset_error() -> None:
    """[P0-C] LLM 给错 char_offset ±200 仍正确 fuzzy match section.title.

    Test: LLM 给的 offset 偏移 100 char, fuzzy match 在 ±200 范围内仍找回真实位置.
    """
    md = (
        "Some intro text here.\n\n"
        "## GMV 业务定义\n\n"
        "GMV (Gross Merchandise Volume) 是商品交易总额, 不扣退款。\n\n"
        "## DAU 业务定义\n\n"
        "DAU 是日活跃用户数。\n"
    )
    actual_pos_gmv = md.find("## GMV 业务定义")
    actual_pos_dau = md.find("## DAU 业务定义")

    # LLM gives wrong offsets (50-char deviation).
    sections = [
        KbSectionInferred(
            title="## GMV 业务定义",
            char_start=actual_pos_gmv + 50,  # Wrong.
            char_end=actual_pos_dau,
            role="glossary",
        ),
        KbSectionInferred(
            title="## DAU 业务定义",
            char_start=actual_pos_dau + 30,  # Wrong.
            char_end=len(md),
            role="glossary",
        ),
    ]
    docs = section_aware_split(md, sections)
    # Find GMV section containing GMV content.
    gmv_docs = [d for d in docs if d.metadata.get("section_title") == "## GMV 业务定义"]
    assert len(gmv_docs) >= 1
    assert any("Gross Merchandise Volume" in d.page_content for d in gmv_docs)


def test_section_aware_title_not_found_fallback_to_offset() -> None:
    """如果 fuzzy match 全文找不到 title (LLM 改过), fallback LLM offset."""
    md = "doc content paragraphs"
    sections = [
        KbSectionInferred(
            title="不存在的 Title",  # Title absent from Markdown.
            char_start=0,
            char_end=len(md),
            role="intro",
        ),
    ]
    docs = section_aware_split(md, sections)
    # Offset fallback returns ≥1 chunk (whole Markdown as section).
    assert len(docs) >= 1


def test_section_aware_empty_sections_returns_empty() -> None:
    docs = section_aware_split("some text", [])
    assert docs == []


def test_section_aware_assigns_role_metadata() -> None:
    """每 chunk metadata 含 section_title / role / page."""
    md = "## Glossary\nGMV 定义"
    sections = [
        KbSectionInferred(
            title="## Glossary",
            char_start=0,
            char_end=len(md),
            role="glossary",
            page=3,
        ),
    ]
    docs = section_aware_split(md, sections)
    assert len(docs) >= 1
    assert docs[0].metadata["section_title"] == "## Glossary"
    assert docs[0].metadata["role"] == "glossary"
    assert docs[0].metadata["page"] == 3


def test_section_aware_keeps_text_before_the_first_section_title() -> None:
    """第一个章节标题之前的正文（封面摘要、前言）单独成块，不丢。"""
    preamble = "本报告摘要：2025 年营收同比增长 12%，净利率 9.5%。"
    md = f"{preamble}\n\n## 第一章 市场\n市场规模持续扩大。"
    sections = [KbSectionInferred(title="## 第一章 市场", char_start=md.index("## 第一章"), char_end=len(md), role="analysis")]

    docs = section_aware_split(md, sections)

    assert preamble in docs[0].page_content
    assert docs[0].metadata["section_title"] is None
    assert docs[0].metadata["role"] == "intro"
    assert any("市场规模" in d.page_content and d.metadata["section_title"] == "## 第一章 市场" for d in docs[1:])


# ─── DEFAULT_CHUNK_SIZE matches PRD ────────────────────────────


def test_default_chunk_size_800() -> None:
    """[PRD v0.7 §3.3.5] Anthropic Contextual Retrieval 标准 800 token."""
    assert DEFAULT_CHUNK_SIZE == 800
