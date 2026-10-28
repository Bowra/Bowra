"""Markdown loader (L0 直读).

Tech design v3.6 §3.1. md 已结构化, 不需要 partition / OCR.
"""

from __future__ import annotations

import time
from pathlib import Path

from bowhead_core.kb.document import KbDocument
from bowhead_core.kb.loader.base import BaseLoader, LoadResult


class MarkdownLoader(BaseLoader):
    """L0 直读 markdown / plain text 文件 → 单 KbDocument."""

    async def load(self, file_path: str, *, use_llm: bool = False) -> LoadResult:
        t0 = time.time()
        path = Path(file_path)
        md_text = path.read_text(encoding="utf-8")
        # Single document, no page split; later Markdown splitter splits by heading.
        docs = [
            KbDocument(
                page_content=md_text,
                metadata={"source_path": str(path), "page": None},
            )
        ]
        return LoadResult(
            documents=docs,
            layer_used="L0",
            duration_ms=int((time.time() - t0) * 1000),
            metadata={"char_count": len(md_text), "page_count": 0},
        )
