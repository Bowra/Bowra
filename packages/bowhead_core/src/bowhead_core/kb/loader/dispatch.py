"""Loader dispatch: mime → loader instance.

Tech design v3.6 §3.1. P1-S1 (round 7): text/plain → MarkdownLoader (python-magic 多
detect 为 text/plain).

image-extraction sprint v1.0 §2.4: image MIME → ImageLoader (vision LLM only).
PdfLoader / ImageLoader 接 vision_client 注入 (caller via worker.py / main.py wire).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_core.kb.errors import UnsupportedMimeError
from bowhead_core.kb.loader.csv_loader import CSV_MIME, CsvLoader
from bowhead_core.kb.loader.docx_loader import DocxLoader
from bowhead_core.kb.loader.image_loader import ImageLoader
from bowhead_core.kb.loader.md_loader import MarkdownLoader
from bowhead_core.kb.loader.pdf_loader import PdfLoader
from bowhead_core.kb.loader.pptx_loader import PPTX_MIME, PptxLoader
from bowhead_core.kb.loader.xlsx_loader import XLS_MIME, XLSX_MIME, XlsxLoader

if TYPE_CHECKING:
    from bowhead_core.kb.loader.base import BaseLoader
    from bowhead_core.kb.vision_llm import VisionLLMClient

# Mime types that need vision_client injection
VISION_CAPABLE_MIMES = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }
)

# Mime types served by ImageLoader
IMAGE_MIMES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }
)


def get_loader(
    mime_type: str,
    *,
    vision_client: VisionLLMClient | None = None,
) -> BaseLoader:
    """Get loader instance for mime type.

    Args:
        mime_type: detected MIME (python-magic + filename ext fallback per V7 P0-D19-1).
        vision_client: qwen3-vl-plus client. Required for image MIMEs (else placeholder).
            Optional for PDF (only used in L2 scan-fallback path).

    Raises:
        UnsupportedMimeError: if mime not in supported set
            (PDF / DOCX / XLSX / XLS / PPTX / MD / image).
    """
    if mime_type in IMAGE_MIMES:
        return ImageLoader(vision_client=vision_client)
    if mime_type == "application/pdf":
        return PdfLoader(vision_client=vision_client)
    if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return DocxLoader()
    if mime_type == CSV_MIME:
        return CsvLoader()
    if mime_type in (XLSX_MIME, XLS_MIME):
        # kb-doctype-config sprint v1.0: xlsx (openpyxl) + .xls (xlrd) → markdown chunks per island
        return XlsxLoader()
    if mime_type == PPTX_MIME:
        # pptx-loader sprint v1.0: python-pptx → one document per slide (text boxes + tables + notes).
        return PptxLoader()
    if mime_type in ("text/markdown", "text/x-markdown", "text/plain"):
        # [P1-S1] python-magic often labels .md/.txt text/plain.
        return MarkdownLoader()
    raise UnsupportedMimeError(mime_type)
