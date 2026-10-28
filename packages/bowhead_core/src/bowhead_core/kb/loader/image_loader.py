"""Image loader (jpg/png/jpeg/webp) — vision LLM only.

image-extraction sprint tech design v1.0 §2.2.
Layer: 直接 L2 (vision LLM). Image 没有 L0 — must use vision LLM.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

from bowhead_core.kb.document import KbDocument
from bowhead_core.kb.loader.base import BaseLoader, LoadResult
from bowhead_core.kb.vision_llm import (
    VisionLLMClient,
    VisionLLMError,
    VisionLLMNonRetryableError,
)

logger = logging.getLogger(__name__)


SUPPORTED_MIMES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }
)


def _guess_mime_from_ext(file_path: str) -> str:
    """Fallback when caller passes path without explicit mime."""
    ext = Path(file_path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")


class ImageLoader(BaseLoader):
    """Vision LLM only loader for jpg/png/jpeg/webp.

    `use_llm` parameter is ignored — image must use vision LLM (no L0 path exists).
    Without vision_client (None), returns placeholder doc + parse_failed=True.
    """

    def __init__(self, vision_client: VisionLLMClient | None = None) -> None:
        self._vision = vision_client

    async def load(self, file_path: str, *, use_llm: bool = True) -> LoadResult:
        t0 = time.time()
        if self._vision is None:
            logger.warning(
                "image_loader: vision_client None → placeholder. Configure a vision model in team settings to enable image extraction.",
            )
            return self._placeholder_result("vision_client_unconfigured", t0)

        try:
            with Path(file_path).open("rb") as f:
                raw = f.read()
        except OSError as exc:
            logger.warning("image_loader: file read failed: %s", exc)
            return self._placeholder_result(f"read_failed: {exc}", t0)

        if not raw:
            return self._placeholder_result("empty_file", t0)

        b64 = base64.b64encode(raw).decode("ascii")
        mime = _guess_mime_from_ext(file_path)

        try:
            text = await self._vision.extract_text(b64, mime=mime)
        except VisionLLMNonRetryableError as exc:
            logger.warning("image_loader: vision_llm 4xx error: %s", exc)
            return self._placeholder_result(f"vision_llm_4xx: {exc}", t0)
        except VisionLLMError as exc:
            logger.warning("image_loader: vision_llm transient error after retries: %s", exc)
            return self._placeholder_result(f"vision_llm_failed: {exc}", t0)

        if not text:
            # decorative image (logo / blank) → empty doc but not failed
            return LoadResult(
                documents=[
                    KbDocument(
                        page_content="(图片无可抽取文字 — 装饰 / logo / 空白)",
                        metadata={"page": 1, "decorative": True},
                    )
                ],
                layer_used="L2",
                duration_ms=int((time.time() - t0) * 1000),
                metadata={"vision_llm_extracted": True, "decorative": True},
            )

        return LoadResult(
            documents=[
                KbDocument(
                    page_content=text,
                    metadata={"page": 1, "vision_llm_extracted": True},
                )
            ],
            layer_used="L2",
            duration_ms=int((time.time() - t0) * 1000),
            metadata={"vision_llm_extracted": True, "char_count": len(text)},
        )

    @staticmethod
    def _placeholder_result(reason: str, t0: float) -> LoadResult:
        return LoadResult(
            documents=[
                KbDocument(
                    page_content=f"(图片解析失败: {reason})",
                    metadata={"page": 1, "parse_failed": True},
                )
            ],
            layer_used="L2",
            duration_ms=int((time.time() - t0) * 1000),
            metadata={"parse_failed": True, "reason": reason},
        )
