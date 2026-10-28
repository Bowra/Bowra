"""Section-aware splitter — fuzzy match section.title in md_text (P0-C).

Tech design v3.6 §3.2. P0-C: framework_inferrer LLM 给的 char_offset ±200 char 不可靠,
用 fuzzy match section.title 在 md_text 找实际位置.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from bowhead_core.kb.document import KbDocument
from bowhead_core.kb.splitter.paragraph_aware import make_paragraph_splitter

logger = logging.getLogger(__name__)


SectionRole = Literal["intro", "data", "analysis", "appendix", "glossary", "sop"]


@dataclass
class KbSectionInferred:
    """framework_inferrer 输出的单 section. char_start/end 是 LLM 估的 hint."""

    title: str
    char_start: int
    char_end: int
    role: SectionRole
    page: int | None = None


def section_aware_split(
    md_text: str,
    sections: list[KbSectionInferred],
) -> list[KbDocument]:
    """[P0-C] LLM offset ±200 char 不可靠. 用 fuzzy match section.title 在 md_text 找实际位置.

    每 section 内再用 paragraph splitter cap 800 token.

    Returns: list of KbDocument, 每 chunk metadata 含 section_title / role / page.
    """
    out: list[KbDocument] = []
    para_splitter = make_paragraph_splitter()

    # Keep body before first section heading (cover, abstract, preface, etc.) in separate chunks; do not lose it.
    if sections:
        first = sections[0]
        first_pos = md_text.find(first.title, max(0, first.char_start - 200))
        preamble_end = first_pos if first_pos >= 0 else first.char_start
        preamble = md_text[: max(0, min(preamble_end, len(md_text)))]
        if preamble.strip():
            for chunk_text in para_splitter.split_text(preamble):
                out.append(
                    KbDocument(
                        page_content=chunk_text,
                        metadata={"section_title": None, "role": "intro", "page": None, "char_offset": 0},
                    )
                )

    for i, sec in enumerate(sections):
        # P0-C: fuzzy match actual title position in md_text
        # LLM char_start is only a hint; search for title string within hint ±200 chars
        hint = max(0, sec.char_start - 200)
        title_pos = md_text.find(sec.title, hint)
        if title_pos < 0:
            # Title absent from full text (LLM changed wording); unreliable LLM offset fallback → whole hint region
            start = sec.char_start
            logger.warning(
                "section title not found via fuzzy match, fallback offset",
                extra={"title": sec.title, "offset": sec.char_start},
            )
        else:
            start = title_pos

        # end: actual start of next section (not LLM offset)
        if i + 1 < len(sections):
            next_sec = sections[i + 1]
            next_hint = max(0, next_sec.char_start - 200)
            next_pos = md_text.find(next_sec.title, next_hint)
            end = next_pos if next_pos >= 0 else sec.char_end
        else:
            end = len(md_text)

        # bounds check
        start = max(0, min(start, len(md_text)))
        end = max(start, min(end, len(md_text)))
        sec_md = md_text[start:end]
        if not sec_md.strip():
            continue

        # Within each section, paragraph splitter caps at 800 tokens
        for chunk_text in para_splitter.split_text(sec_md):
            out.append(
                KbDocument(
                    page_content=chunk_text,
                    metadata={
                        "section_title": sec.title,
                        "role": sec.role,
                        "page": sec.page,
                        "char_offset": start,
                    },
                )
            )
    return out
