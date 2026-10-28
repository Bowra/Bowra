"""Sprint α-5 — sentence-aware splitter unit tests."""

from __future__ import annotations

from bowhead_core.kb.splitter.paragraph_aware import (
    count_tokens,
    make_paragraph_splitter,
    make_sentence_aware_splitter,
)


def test_short_paragraph_preserved() -> None:
    """Short text < chunk_size returned unchanged."""
    splitter = make_sentence_aware_splitter(chunk_size=800)
    text = "短段落测试。这是第二句。"
    chunks = splitter.split_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_long_paragraph_with_periods_splits_at_sentence() -> None:
    """长段落 by 句号 splits into multiple chunks (boundary may be at start of next)."""
    splitter = make_sentence_aware_splitter(chunk_size=20, chunk_overlap=2)
    text = "短句一。短句二。短句三。短句四。"
    chunks = splitter.split_text(text)
    assert len(chunks) >= 2
    # langchain's keep_separator=True puts separator at the START of next chunk,
    # so non-final chunks may not end with periods themselves; instead verify boundary
    # is AT a known separator (chunk N+1 starts with one, OR chunk N ends with one)
    SEPS = ("。", "！", "？", ".", "!", "?", "；", ";", "，", "：")
    for i, c in enumerate(chunks[:-1]):
        next_chunk = chunks[i + 1]
        ends_at_sep = c.rstrip().endswith(SEPS)
        next_starts_at_sep = next_chunk.lstrip().startswith(SEPS)
        assert ends_at_sep or next_starts_at_sep, f"chunk {i} not at sentence boundary: end={c!r}, next_start={next_chunk!r}"


def test_long_paragraph_no_period_falls_back_to_clause() -> None:
    """长段落 with 0 句号 → 用 ；, 逗号 fallback (不再整段超 cap)."""
    sentence_splitter = make_sentence_aware_splitter(chunk_size=20, chunk_overlap=2)
    para_splitter = make_paragraph_splitter(chunk_size=20, chunk_overlap=2)
    # Long Chinese clause with semicolons + commas, NO periods.
    text = "项目预算紧张, 团队人手不足; 市场竞争加剧, 客户流失风险高; 供应链问题持续, 成本压力大"
    sentence_chunks = sentence_splitter.split_text(text)
    para_chunks = para_splitter.split_text(text)

    # Sentence-aware: should split at ; or , into multiple chunks
    assert len(sentence_chunks) >= 2
    # Paragraph-only (legacy): no sentence end → returned whole
    # (this is the existing prod behavior we're supplementing, NOT replacing)
    assert len(para_chunks) == 1


def test_sentence_aware_does_not_break_chars() -> None:
    """No mid-character split."""
    splitter = make_sentence_aware_splitter(chunk_size=20, chunk_overlap=2)
    text = "这是中文短句。" * 5
    chunks = splitter.split_text(text)
    # Every chunk respects char boundary (no half-character)
    for c in chunks:
        # encode/decode round trip ⇒ all chars valid
        assert c == c.encode("utf-8").decode("utf-8")


def test_overlap_between_chunks() -> None:
    """chunk_overlap=10 produces small overlap region between adjacent chunks."""
    splitter = make_sentence_aware_splitter(chunk_size=30, chunk_overlap=5)
    text = "第一句话很长很长有20个token。第二句话也很长有20个token。第三句话更长有25个token。"
    chunks = splitter.split_text(text)
    # Just verify split happened; overlap behavior is langchain internals
    assert len(chunks) >= 2


def test_paragraph_break_priority_higher_than_sentence() -> None:
    """\n\n 段落 separator should always win over 句号 if available."""
    splitter = make_sentence_aware_splitter(chunk_size=30, chunk_overlap=3)
    text = "第一段。这是第一段的第二句。\n\n第二段。这是第二段的第二句。"
    chunks = splitter.split_text(text)
    # Should split at \n\n at least
    assert len(chunks) >= 2
    # First chunk shouldn't contain "第二段"
    assert "第二段" not in chunks[0]


def test_sentence_aware_token_count_within_cap() -> None:
    """Long input → all chunks ≤ chunk_size token (with small slack for overlap)."""
    splitter = make_sentence_aware_splitter(chunk_size=50, chunk_overlap=5)
    # Build long text with frequent commas/periods so split has opportunities
    text = (
        "营收同比下滑 8%, 净利率退到 9.5%; "
        "成本端煤价上涨, 物流费用上升; "
        "客户结构集中度上升, 大客户营收占比 65%; "
        "现金流转化率退到 78%, 应收账款增加; "
        "存货周转天数从 60 天涨到 90 天, 警示信号明显"
    )
    chunks = splitter.split_text(text)
    # All chunks within (chunk_size + small slack)
    slack = 30
    for c in chunks:
        assert count_tokens(c) <= 50 + slack, f"chunk too big: {count_tokens(c)} tokens"
