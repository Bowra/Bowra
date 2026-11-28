"""Where a quoted excerpt sits in a file: its chunk, the chunk's position and its page.

Document matrix step 1, S7 (``docs/sprints/kb-matrix-redesign-2026-11-27/tech-design-step1.md`` §7;
evidence in ``spike-step1.md`` §5). The extraction model's own ``source_page`` is an estimate and is
never kept. A page comes only from what the loaders wrote:

- **PDF:** both PDF paths (pypdfium2 text and page-by-page vision) emit the whole document as one
  text with a ``## Page N`` line before each page, so the page is the last marker before the
  excerpt, carried across chunks in ``chunk_index`` order. Markers win over chunk metadata,
  because the metadata page of a long PDF is the framework inferrer's guess for its section.
  The marker rule applies to the PDF MIME type only: a Markdown file may contain a literal
  ``## Page 3``.
- **PPTX and images:** the chunk metadata page (the slide number; 1 for an image).
- **DOCX, XLSX, Markdown and other types:** no page. The UI shows the paragraph from
  ``chunk_index`` instead.

Excerpts are matched whitespace-insensitively on their first ``MAX_SNIPPET_MATCH_CHARS``
characters, the same rule citation grounding uses. An excerpt found in no single chunk (it
straddles a chunk boundary) is searched in the joined text and attributed to the chunk where it
starts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from bowhead_core.kb.loader.dispatch import IMAGE_MIMES
from bowhead_core.kb.loader.pptx_loader import PPTX_MIME
from bowhead_core.kb.structured_extractor import MAX_SNIPPET_MATCH_CHARS

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "SourceChunk",
    "SourceIndex",
    "SourceLocation",
    "bind_field_sources",
    "chunk_metadata_page",
]

PDF_MIME = "application/pdf"
# Both PDF loader paths write exactly "## Page N" on its own line (pdf_loader.py L0 and L2).
_PAGE_MARKER_RE = re.compile(r"^##[ \t]*Page[ \t]+(\d+)[ \t]*$", re.MULTILINE)
_METADATA_PAGE_MIMES = frozenset({PPTX_MIME, *IMAGE_MIMES})


class SourceChunk(NamedTuple):
    """One published chunk of a file."""

    chunk_id: str
    content: str
    chunk_index: int
    # The page the loader recorded in the chunk metadata; None when it recorded none.
    page: int | None = None


@dataclass(frozen=True)
class SourceLocation:
    """Where an excerpt was found."""

    chunk_id: str
    chunk_index: int
    page: int | None


def chunk_metadata_page(value: object) -> int | None:
    """A stored metadata page as a page number; Chroma stores ``-1`` for "no page"."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _strip_whitespace(text: str) -> tuple[str, list[int]]:
    """``text`` without whitespace, plus each kept character's offset in ``text``."""
    kept: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            kept.append(ch)
            offsets.append(i)
    return "".join(kept), offsets


def _page_rule(mime_type: str | None) -> str | None:
    base = (mime_type or "").split(";", 1)[0].strip().lower()
    if base == PDF_MIME:
        return "markers"
    if base in _METADATA_PAGE_MIMES:
        return "metadata"
    return None


class SourceIndex:
    """A file's chunks, prepared once for locating many excerpts."""

    def __init__(self, chunks: Sequence[SourceChunk], mime_type: str | None) -> None:
        self._chunks = sorted(chunks, key=lambda c: c.chunk_index)
        self._rule = _page_rule(mime_type)
        self._stripped = [_strip_whitespace(c.content or "") for c in self._chunks]
        # Per chunk: (raw offset, page) of each "## Page N" line; and the last page marked before the chunk.
        self._markers: list[list[tuple[int, int]]] = []
        self._carried: list[int | None] = []
        if self._rule == "markers":
            carried: int | None = None
            for c in self._chunks:
                marks = [(m.start(), int(m.group(1))) for m in _PAGE_MARKER_RE.finditer(c.content or "")]
                self._markers.append(marks)
                self._carried.append(carried)
                if marks:
                    carried = marks[-1][1]
        self._joined: tuple[str, list[int]] | None = None

    def locate(self, excerpt: object) -> SourceLocation | None:
        """The chunk and page of ``excerpt``; None when it is empty or not in the file."""
        if not isinstance(excerpt, str) or not self._chunks:
            return None
        key = _strip_whitespace(excerpt[:MAX_SNIPPET_MATCH_CHARS])[0]
        if not key:
            return None
        for i, (stripped, offsets) in enumerate(self._stripped):
            at = stripped.find(key)
            if at >= 0:
                return self._location(i, offsets[at])
        # The excerpt straddles a chunk boundary: search the joined text and attribute it to
        # the chunk where it starts.
        joined, starts = self._joined_text()
        at = joined.find(key)
        if at < 0:
            return None
        i = next(n for n in range(len(starts) - 1, -1, -1) if starts[n] <= at)
        return self._location(i, self._stripped[i][1][at - starts[i]])

    def _joined_text(self) -> tuple[str, list[int]]:
        if self._joined is None:
            starts: list[int] = []
            total = 0
            for stripped, _ in self._stripped:
                starts.append(total)
                total += len(stripped)
            # A chunk without visible text contributes nothing, so a match never starts in one.
            self._joined = ("".join(s for s, _ in self._stripped), starts)
        return self._joined

    def _location(self, i: int, raw_offset: int) -> SourceLocation:
        chunk = self._chunks[i]
        return SourceLocation(chunk_id=chunk.chunk_id, chunk_index=chunk.chunk_index, page=self._page(i, raw_offset))

    def _page(self, i: int, raw_offset: int) -> int | None:
        if self._rule == "metadata":
            return chunk_metadata_page(self._chunks[i].page)
        if self._rule != "markers":
            return None
        # A marker at the excerpt's first character starts the excerpt's page.
        before = [page for start, page in self._markers[i] if start <= raw_offset]
        if before:
            return before[-1]
        if self._carried[i] is not None:
            return self._carried[i]
        return chunk_metadata_page(self._chunks[i].page)


def bind_field_sources(fields: dict[str, Any], index: SourceIndex) -> int:
    """Set each extracted field's ``chunk_id``, ``chunk_index`` and ``source_page`` from its quote.

    ``fields`` is ``StructuredExtractor`` output after citation grounding, mutated in place.
    The model's ``source_page`` estimate is always replaced: a field whose quote was not verified
    or is not found in the chunks gets no chunk and no page. Returns the number of fields bound.
    """
    bound = 0
    for fdata in fields.values():
        if not isinstance(fdata, dict):
            continue
        verified = fdata.get("citation_verified") is not False
        location = index.locate(fdata.get("source_snippet")) if verified else None
        fdata["chunk_id"] = location.chunk_id if location else None
        fdata["chunk_index"] = location.chunk_index if location else None
        fdata["source_page"] = location.page if location else None
        if location is not None:
            bound += 1
    return bound
