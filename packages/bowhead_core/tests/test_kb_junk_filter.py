"""Sprint α-2 — junk-chunk filter unit tests.

Targets `_looks_like_junk_chunk`: empty-header markdown noise + super-short content.
"""

from __future__ import annotations

from bowhead_core.kb.retriever import _looks_like_junk_chunk


def test_empty_chunk_is_junk() -> None:
    assert _looks_like_junk_chunk("") is True
    assert _looks_like_junk_chunk("   \n  ") is True


def test_super_short_chunk_is_junk() -> None:
    """Empty / whitespace-only / < 5 chars is junk; short glossary entries are NOT."""
    assert _looks_like_junk_chunk("hi") is True
    assert _looks_like_junk_chunk("xx") is True
    assert _looks_like_junk_chunk("营收") is True
    # Real glossary entries (≥ 5 chars) survive
    assert _looks_like_junk_chunk("GMV 定义") is False
    assert _looks_like_junk_chunk("intro doc") is False


def test_normal_paragraph_is_not_junk() -> None:
    """Real content survives."""
    text = "2025Q3 营收同比下滑 8%, 净利率从 12% 退到 9.5%, 主要由销量驱动."
    assert _looks_like_junk_chunk(text) is False


def test_normal_markdown_table_is_not_junk() -> None:
    """Properly headered table survives."""
    md = """\
# Sheet: 利润表
| 项目 | Q3 | YoY |
|------|----|----|
| 营收 | 32.4 | +0.3% |
| 净利润 | 4.2 | -8% |
"""
    assert _looks_like_junk_chunk(md) is False


def test_empty_header_table_is_junk() -> None:
    """openpyxl flattening unlabeled header → `| | | |` row at top."""
    md = """\
| | | |
|---|---|---|
| 1 | 2 | 3 |
| 4 | 5 | 6 |
"""
    assert _looks_like_junk_chunk(md) is True


def test_partial_empty_header_is_not_junk() -> None:
    """Header with some labels (even if 1 empty) is OK — real Excel quirk."""
    md = """\
| 项目 | | 金额 |
|------|---|------|
| 营收 |   | 32.4 |
"""
    assert _looks_like_junk_chunk(md) is False


def test_empty_header_beyond_first_3_lines_not_junk() -> None:
    """Empty `| | | |` pattern only triggers when in head of chunk; deeper is OK."""
    md = """\
# 财务报表 Q3 H1 head
## Section A real content with enough chars
本文段含足量正文 用来确保不被 short-chunk filter 砍.
| | | |
|---|---|---|
| 1 | 2 | 3 |
"""
    # `| | | |` is on line 4 (idx 3) → NOT in first 3 lines (0-2) → not junk
    assert _looks_like_junk_chunk(md) is False


def test_just_at_threshold_short_chunk() -> None:
    """5 chars exactly = boundary check."""
    assert _looks_like_junk_chunk("a" * 4) is True
    assert _looks_like_junk_chunk("a" * 5) is False
