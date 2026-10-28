"""Markdown section splitter — MarkdownHeaderTextSplitter wrap.

Tech design v3.6 §3.2. md / marker 输出走此 splitter, 按 # ## ### #### heading 切.
strip_headers=False: 保留 heading 在 chunk content (preserve context).
"""

from __future__ import annotations

from langchain_text_splitters import MarkdownHeaderTextSplitter

HEADERS_TO_SPLIT_ON = [
    ("#", "h1"),
    ("##", "h2"),
    ("###", "h3"),
    ("####", "h4"),
]


def make_markdown_splitter() -> MarkdownHeaderTextSplitter:
    """Returns markdown splitter that keeps headers in chunk content."""
    return MarkdownHeaderTextSplitter(
        headers_to_split_on=HEADERS_TO_SPLIT_ON,
        strip_headers=False,
    )
