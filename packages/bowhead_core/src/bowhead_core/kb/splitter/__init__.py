"""KB splitter subpackage.

4 strategies:
- paragraph_aware: RecursiveCharacterTextSplitter wrap, 中文 separator + 句号 hard cap
- sentence_aware (α-5): paragraph_aware + clause-level fallback (长段落不超 cap)
- markdown_section: MarkdownHeaderTextSplitter wrap (md / marker 输出)
- section_aware: framework_inferrer 推 sections 后按 section_title 切 (P0-C fuzzy match)
"""

from bowhead_core.kb.splitter.markdown_section import make_markdown_splitter
from bowhead_core.kb.splitter.paragraph_aware import (
    make_paragraph_splitter,
    make_sentence_aware_splitter,
)
from bowhead_core.kb.splitter.section_aware import section_aware_split

__all__ = [
    "make_markdown_splitter",
    "make_paragraph_splitter",
    "make_sentence_aware_splitter",
    "section_aware_split",
]
