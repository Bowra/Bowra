"""KB loader subpackage.

Per-mime loader (PDF / DOCX / XLSX / PPTX / Markdown / image). Dispatch on mime type → loader.
"""

from bowhead_core.kb.loader.base import BaseLoader, LoadResult
from bowhead_core.kb.loader.dispatch import IMAGE_MIMES, VISION_CAPABLE_MIMES, get_loader

__all__ = [
    "IMAGE_MIMES",
    "VISION_CAPABLE_MIMES",
    "BaseLoader",
    "LoadResult",
    "get_loader",
]
