"""亲笔正文按章切段（tech-design-chapter-split.md §2.1 判据）。"""

from __future__ import annotations

import pytest
from bowhead_core.report_chapters import Chapter, assign_unverified, chapter_title, normalize_title, split_chapters

_DOC = (
    "2026 年 1-8 月全品类合计营业额 110.9 亿元 [1]。\n\n"
    "## 品类结构：女装一家独大 [figure:sec_a]\n\n女装占比 36.8% [2]。\n\n[figure:sec_a]\n\n"
    "## 逐月趋势\n\n### 3 月断崖\n\n营业额环比 -20.2%。\n\n"
    "```python\n## 这不是章\nprint(1)\n```\n\n"
    "## 结论\n\n女装托底。\n"
)


@pytest.mark.unit
def test_split_is_byte_preserving_and_finds_h2_chapters() -> None:
    chs = split_chapters(_DOC)
    assert "".join(c.markdown for c in chs) == _DOC
    assert [c.is_lead for c in chs] == [True, False, False, False]
    assert [c.title for c in chs] == ["", "品类结构：女装一家独大", "逐月趋势", "结论"]
    # ### stays within chapter; fenced ## is no chapter; figure anchor stays with containing chapter.
    assert "### 3 月断崖" in chs[2].markdown and "## 这不是章" in chs[2].markdown
    assert "[figure:sec_a]\n" in chs[1].markdown and "[figure:" not in chs[2].markdown


@pytest.mark.unit
def test_no_heading_means_single_section_identical() -> None:
    md = "只有一段正文。\n\n**加粗不是标题**\n\n#### 四级不算章\n\n内容。"
    assert split_chapters(md) == [Chapter(markdown=md, title="", is_lead=True)]
    assert split_chapters("") == [Chapter(markdown="", title="", is_lead=True)]
    # No ##, only one ###: also one chapter (writer adds using same ### level; 116 1.2.32 D1).
    one_h3 = "### 复购：各品牌会员复购率\n\n正文。\n"
    assert split_chapters(one_h3) == [Chapter(markdown=one_h3, title="复购：各品牌会员复购率", is_lead=False)]
    assert [(c.is_lead, c.title) for c in split_chapters("导语。\n\n### 小节\n\n正文。")] == [(True, ""), (False, "小节")]


@pytest.mark.unit
def test_empty_lead_and_heading_only_chapter_merge() -> None:
    md = "## 空章\n## 有内容\n\n正文。\n## 末尾空章\n"
    chs = split_chapters(md)
    assert "".join(c.markdown for c in chs) == md
    assert not chs[0].is_lead  # Empty introduction → no empty section.
    assert [c.title for c in chs] == ["有内容", "末尾空章"]
    assert chs[0].markdown.startswith("## 空章\n## 有内容")  # Heading-only merges into next chapter.
    # Also splits \r\n.
    crlf = "导语\r\n## 甲\r\n正文\r\n## 乙\r\n正文\r\n"
    assert [c.title for c in split_chapters(crlf)] == ["", "甲", "乙"] and "".join(c.markdown for c in split_chapters(crlf)) == crlf


@pytest.mark.unit
def test_title_and_normalize() -> None:
    assert chapter_title("## **品类结构**：女装 [figure:sec_x]") == "品类结构：女装"
    assert normalize_title("品类结构：女装 一家独大") == normalize_title("品类结构女装一家独大")
    assert normalize_title("Top 5 Brands") == "top5brands"


@pytest.mark.unit
def test_assign_unverified_by_membership() -> None:
    chs = split_chapters("导语 1.2亿。\n## 甲\n甲里 9.9 亿。\n## 乙\n乙里 3%。\n")
    per = assign_unverified(chs, ["9.9亿", "3%", "7.7万"])
    assert per == [["7.7万"], ["9.9亿"], ["3%"]]  # Unmatched goes in first section.


@pytest.mark.unit
def test_level3_fallback_when_only_one_h2() -> None:
    """116 语料 15%：第一行一个 `##` 当文档标题 + `###` 当章（tech-design §2.1 补充）。"""
    md = "## 各品牌核心指标对比表\n\n### 整体概览\n\n总营业额 112.65 亿元 [1]。\n\n[figure:sec_a]\n\n### Top 品牌\n\n金辉福 4.82 亿。\n"
    chs = split_chapters(md)
    assert "".join(c.markdown for c in chs) == md
    # Introduction only heading ⇒ merge into first chapter, no empty-shell introduction.
    assert [c.is_lead for c in chs] == [False, False]
    assert [c.title for c in chs] == ["整体概览", "Top 品牌"]
    assert chs[0].markdown.startswith("## 各品牌核心指标对比表\n\n### 整体概览") and "[figure:sec_a]" in chs[0].markdown
    # Sentence below heading ⇒ introduction section (includes ## line).
    md2 = "## 区域经营表现\n\n本节按省市拆解。\n\n### 省\n\n沪京双核。\n\n### 市\n\n上海第一。\n"
    chs2 = split_chapters(md2)
    assert [c.is_lead for c in chs2] == [True, False, False] and chs2[0].markdown == "## 区域经营表现\n\n本节按省市拆解。\n\n"
    assert [c.title for c in chs2] == ["", "省", "市"]
    # ≥2 ## still splits by ##; ### stays inside chapters.
    md3 = "## 甲\n\n### 甲1\n\n正文。\n\n### 甲2\n\n正文。\n\n## 乙\n\n正文。\n"
    assert [c.title for c in split_chapters(md3)] == ["甲", "乙"]
    # One ## + one ### cannot fall back to ###: one titled chapter, not introduction (new-turn add-chapter shape).
    md4 = "## 五、各品类退款率\n\n### 唯一小节\n\n正文。\n"
    assert split_chapters(md4) == [Chapter(markdown=md4, title="五、各品类退款率", is_lead=False)]
    # Introduction before sole ## ⇒ introduction + chapter.
    assert [(c.is_lead, c.title) for c in split_chapters("导语。\n\n## 结论\n\n正文。\n")] == [(True, ""), (False, "结论")]
